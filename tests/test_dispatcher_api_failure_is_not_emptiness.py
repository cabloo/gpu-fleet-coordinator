"""An UNREACHABLE Vast API must never be scored as an EMPTY fleet or a ZERO balance.

THE INCIDENT THIS PINS (2026-08-03, from the coordinator's own event log):

    13:48:38  lost      40000051  "instance 40000051 missing from vastai show instances"
    13:48:38  lost      40000052  "instance 40000052 missing from vastai show instances"
    13:48:38  infra_failed + requeue x6        (every task on both boxes)
    13:48:42  claim -> owned boxes -1/-2       (their work is evacuated)
    13:50:15  adopt     40000051  "adopted untracked instance 40000051"   <- the API RECOVERED
    13:50:15  adopt     40000052
    13:50:33  teardown  40000051  idle
    13:50:34  teardown  40000052  idle_over_warm_cap

ONE failed `vastai show instances` poll returned None, `or []` turned that into an empty instance
list, and `reconcile` read an empty list as "every rented box has vanished". It requeued six running
tasks onto the owned boxes. **97 seconds later** the API came back, the coordinator re-adopted both
instances — and because their work had already been moved, they now looked IDLE, so the reaper
destroyed two healthy, paid-for boxes with campaign cells at 72% and 82% on them.

The same defect sat one call site over on the balance read: `account_balance(... or {})` scored an
unreachable API as **0.0**, tripping the 4e floor gate and refusing ALL rentals with
"balance: 0.0 <= floor 3.0" while the account was funded and only DNS to vast.ai was down.

The shared root cause is `or []` / `or {}` collapsing THREE distinct states into one: "the API says
empty", "the API says zero", and "the API did not answer". Only the first two are observations. The
third is UNKNOWN, and acting on it picks the most destructive interpretation available — the
definition of fail-open. These tests pin fail-SAFE: on `None`, change nothing and wait a poll.

⚠ Both tests are written to FAIL against the old `or []` / `or {}` code, not merely to pass against
the new code — a guard that cannot distinguish the fix from the bug pins nothing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher_apifail", "fleet/dispatcher.py")


class _Fail:
    """A `vastai` that always fails — the DNS-down / API-5xx case. `vastai_json` returns None."""

    def __call__(self, *a, **k):
        class R:
            returncode = 1
            stdout = ""
            stderr = "socket.gaierror: [Errno -2] Name or service not known"
        return R()


def _dispatcher(tmp_path, vastai):
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Fail(), vastai_run=vastai)


def test_a_failed_instance_poll_does_NOT_mark_live_instances_lost(tmp_path):
    """The 13:48:38 event. A live instance must survive a poll the API could not answer."""
    d = _dispatcher(tmp_path, _Fail())
    d.conn.execute(
        "INSERT INTO instances (id,machine_id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (40000051, 1, "runq_x", "2026-08-03T00:00:00Z", "live", 0.1, 8, "2026-08-05T00:00:00Z"))
    d.conn.commit()

    d.do_reconcile()

    state = d.conn.execute("SELECT state FROM instances WHERE id=?", (40000051,)).fetchone()["state"]
    assert state == "live", f"a live instance was marked {state!r} on an UNANSWERED poll"
    events = [r["event"] for r in d.conn.execute("SELECT event FROM events")]
    assert "lost" not in events, "reconcile declared an instance lost from a failed API call"
    assert "api_unavailable" in events, "the skipped reconcile must be recorded, not silent"


def test_a_failed_user_poll_does_NOT_fabricate_a_zero_balance(tmp_path):
    """The 14:44 event. `None` is UNKNOWN, and unknown must not read as an empty account."""
    d = _dispatcher(tmp_path, _Fail())
    assert d._last_known_balance is None

    # With no prior reading the gate stays shut (correct — we have never observed funds), but the
    # coordinator must say so rather than assert a balance it never saw.
    d._place_queue()
    events = [r["event"] for r in d.conn.execute("SELECT event FROM events")]
    assert "api_unavailable" in events

    # ...and once a real reading exists, a later blip must CARRY IT FORWARD rather than zero it.
    d._last_known_balance = 42.0
    d._place_queue()
    assert d._last_known_balance == 42.0, "a transient failure overwrote a known-good balance"


class _OkUser:
    """A `vastai show user` that answers — credit-only account, so the funds are in `credit`."""

    def __call__(self, *a, **k):
        class R:
            returncode = 0
            stdout = '{"balance": 0.0, "credit": 17.5, "billing_creditonly": 1}'
            stderr = ""
        return R()


def _setting(d, key):
    row = d.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def test_an_OBSERVED_balance_is_recorded_with_the_time_it_was_observed(tmp_path):
    """The balance is otherwise in-memory only, so nothing outside this process can see the number
    that decides whether the fleet may rent. Persisted as a PAIR (value + when)."""
    d = _dispatcher(tmp_path, _OkUser())
    d._place_queue()
    assert _setting(d, disp.BALANCE_KEY) == "17.5"          # balance + credit, per account_balance
    at = _setting(d, disp.BALANCE_AT_KEY)
    assert at and at.startswith('"') and at.endswith('Z"'), f"expected a JSON ISO-Z string, got {at!r}"


def test_a_FAILED_poll_writes_NOTHING_so_the_stored_reading_stays_honestly_old(tmp_path):
    """The whole point of the timestamp. Re-stamping a carried-forward balance would present the
    2026-08-03 outage as a healthy account — the exact reading this file exists to prevent."""
    d = _dispatcher(tmp_path, _OkUser())
    d._place_queue()
    # Sentinels, not the real values: `now_iso` has 1-second resolution, so comparing the stored
    # timestamp before/after cannot see a re-stamp that lands in the same second. Poisoning the rows
    # detects ANY write at all, which is the actual invariant ("a failed poll writes nothing").
    for key in (disp.BALANCE_KEY, disp.BALANCE_AT_KEY):
        d.conn.execute("UPDATE settings SET value='\"SENTINEL\"' WHERE key=?", (key,))
    d.conn.commit()

    d.vastai_run = _Fail()          # the API goes dark; the in-memory value carries forward
    d._place_queue()
    assert d._last_known_balance == 17.5, "the carried-forward reading is a separate guarantee"
    assert _setting(d, disp.BALANCE_KEY) == '"SENTINEL"' \
        and _setting(d, disp.BALANCE_AT_KEY) == '"SENTINEL"', \
        "a failed poll re-stamped the stored balance — a stale figure now looks current"


def test_a_failed_poll_with_NO_prior_reading_records_nothing_rather_than_a_zero(tmp_path):
    d = _dispatcher(tmp_path, _Fail())
    d._place_queue()
    assert _setting(d, disp.BALANCE_KEY) is None and _setting(d, disp.BALANCE_AT_KEY) is None


def test_dry_run_records_nothing(tmp_path):
    """`--dry-run`'s contract is no DB writes; observability must not quietly break it."""
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Fail(), vastai_run=_OkUser(),
                        dry_run=True)
    d._place_queue()
    assert _setting(d, disp.BALANCE_KEY) is None


def test_the_two_states_are_still_DISTINGUISHABLE_from_a_real_empty_answer(tmp_path):
    """Fail-safe must not become fail-blind: a SUCCESSFUL call reporting an empty fleet or a genuinely
    zero balance still has to be believed, or the fix would mask real exhaustion."""
    class _EmptyOk:
        def __call__(self, *a, **k):
            class R:
                returncode = 0
                stdout = "[]"
                stderr = ""
            return R()

    assert disp.vastai_json("show", "instances", run=_EmptyOk()) == [], \
        "a successful empty answer must stay an empty answer"
    assert disp.vastai_json("show", "instances", run=_Fail()) is None, \
        "a failed call must be None, not []"
    assert disp.account_balance({"balance": 0.0, "credit": 0.0}) == 0.0, \
        "an OBSERVED zero balance is still zero"
