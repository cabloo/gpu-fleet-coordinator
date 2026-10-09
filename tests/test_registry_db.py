"""Run registry DB — CAS state machine, dedupe, rate/spend (docs/specs/run-registry.spec.md)."""

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry_db", "fleet/registry_db.py")


def _insert(conn, id="t1", grp="g", name=None, state="queued", **over):
    name = name or id
    fields = dict(
        id=id, created_at=reg.now_iso(), created_by="tester", grp=grp, name=name,
        entrypoint="smoke", args_json="[]", config_json="{}", config_hash="hash1",
        arm_hash="arm1", git_sha="deadbeef", slots=1, est_minutes=10, priority=50,
        state=state, retries_used=0, max_retries=1, instance_id=None, result_path=None,
        resource_hint_json=None, resume_checkpoint=None, updated_at=reg.now_iso(),
    )
    fields.update(over)
    conn.execute(
        f"INSERT INTO tasks({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        tuple(fields.values()))
    conn.commit()


def test_bootstrap_creates_schema_and_is_idempotent(tmp_path):
    db = str(tmp_path / "runs.sqlite")
    conn = reg.connect(db)
    assert reg.list_tasks(conn) == []
    conn.close()
    conn2 = reg.connect(db)  # re-open the created file
    assert reg.list_tasks(conn2) == []


def test_schema_version_mismatch_refuses(tmp_path):
    db = str(tmp_path / "runs.sqlite")
    reg.connect(db).close()
    import sqlite3
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(SystemExit):
        reg.connect(db)


def test_migration_v2_to_v3_adds_source_column_defaulting_to_vast(tmp_path):
    """Owned-box spec: every pre-v3 `instances` row predates the `source` column and is a real
    Vast rental — the migration's DEFAULT 'vast' must apply to it untouched, no backfill."""
    import sqlite3
    db = str(tmp_path / "old.sqlite")
    raw = sqlite3.connect(db)
    # Minimal v2-era instances table WITHOUT the source column.
    raw.executescript("""
      CREATE TABLE instances (
        id INTEGER PRIMARY KEY, machine_id INTEGER, label TEXT NOT NULL, created_at TEXT NOT NULL,
        state TEXT NOT NULL, dph_usd REAL NOT NULL, gpu_name TEXT, ssh_host TEXT, ssh_port INTEGER,
        slots_total INTEGER NOT NULL, hard_cap_at TEXT NOT NULL, destroyed_at TEXT, cost_usd REAL
      );
      CREATE TABLE tasks (id TEXT PRIMARY KEY, entrypoint TEXT NOT NULL, state TEXT NOT NULL,
                          job_manifest_json TEXT);
      PRAGMA user_version=2;
    """)
    raw.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, hard_cap_at) "
        "VALUES (123, 'runq_x', ?, 'live', 0.05, 4, ?)", (reg.now_iso(), reg.now_iso()))
    raw.commit(); raw.close()

    conn = reg.connect(db)  # should migrate 2 -> SCHEMA_VERSION in place
    cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    assert "source" in cols
    row = conn.execute("SELECT source FROM instances WHERE id=123").fetchone()
    assert row["source"] == "vast"
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == reg.SCHEMA_VERSION


def test_dedupe_clash_and_arm_match(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", config_hash="hashA", arm_hash="armA")
    clash = reg.find_clash(conn, "hashA")
    assert clash["id"] == "t1"
    assert reg.find_clash(conn, "hashB") is None
    matches = reg.find_arm_matches(conn, "armA")
    assert [m["id"] for m in matches] == ["t1"]


def test_clash_excludes_terminal_states(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", config_hash="hashA", arm_hash="armA", state="cancelled")
    assert reg.find_clash(conn, "hashA") is None


def test_claim_race_exactly_one_wins(tmp_path):
    db = str(tmp_path / "runs.sqlite")
    conn = reg.connect(db)
    _insert(conn, id="t1", state="queued")
    conn.close()

    results = []

    def attempt():
        c = reg.connect(db)
        r = reg.transition(c, "t1", "claimed", "claim", "claimed by a worker")
        results.append(r.ok)
        c.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
    conn = reg.connect(db)
    row = reg.get_task(conn, "t1")
    assert row["state"] == "claimed"
    claims = [e for e in reg.get_events(conn, "t1") if e["event"] == "claim"]
    assert len(claims) == 1


def test_cancel_in_flight_running_goes_to_cancelling(tmp_path):
    # Cancel-in-flight (2026-07-09): a running task is no longer un-cancellable — it goes to
    # `cancelling` (a request the dispatcher executes), not straight to `cancelled`.
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", state="running")
    r = reg.cancel_task(conn, "t1", "flops budget reached")
    assert r.ok and r.reason == "ok"
    assert reg.get_task(conn, "t1")["state"] == "cancelling"
    # and cancelling -> cancelled is the terminal completion the dispatcher drives
    assert reg.transition(conn, "t1", "cancelled", "cancelled", "worker stopped").ok


def test_cancel_queued_is_immediate(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", state="queued")
    r = reg.cancel_task(conn, "t1", "superseded by demo2")
    assert r.ok
    assert reg.get_task(conn, "t1")["state"] == "cancelled"


def test_cancel_idempotent_and_terminal_noops(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", state="cancelling")
    assert reg.cancel_task(conn, "t1", "why").reason == "already_cancelling"
    _insert(conn, id="t2", state="done")
    assert reg.cancel_task(conn, "t2", "why").reason == "terminal"
    assert reg.get_task(conn, "t2")["state"] == "done"


def test_cancel_requires_a_reason_and_records_it(tmp_path):
    """Owner directive 2026-07-31. A terminal `cancelled` with no metadata cannot distinguish
    budget-complete from abandoned — the ambiguity that made a fleet cost review misread 452
    cancellations as discarded work. The reason is mandatory and lands in the event detail."""
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="t1", state="queued")
    for bad in (None, "", "   ", 7):
        with pytest.raises(ValueError):
            reg.cancel_task(conn, "t1", bad)
    assert reg.get_task(conn, "t1")["state"] == "queued"  # no partial transition on refusal

    assert reg.cancel_task(conn, "t1", "  flops budget reached  ").ok
    detail = [e["detail"] for e in reg.get_events(conn, "t1") if e["event"] == "cancel"][0]
    assert "flops budget reached" in detail  # stripped, and preserved verbatim

    # the in-flight path records it too, on the REQUEST event
    _insert(conn, id="t2", state="running")
    assert reg.cancel_task(conn, "t2", "superseded by demo2").ok
    d2 = [e["detail"] for e in reg.get_events(conn, "t2") if e["event"] == "cancel_requested"][0]
    assert "superseded by demo2" in d2


def test_spend_since_counts_live_boxes_clipped_to_the_window(tmp_path):
    """Invariant 12b. A live box accrues `cost_usd` every cycle, and `spend(since)` must count it —
    otherwise a fleet burning money right now reads $0 until teardown, which is exactly how a
    $21/day spike stayed invisible (2026-07-31). A box created BEFORE the window is clipped to it,
    so it cannot dump its whole history into a short window."""
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at,"
        "destroyed_at,cost_usd) VALUES (1,'runq_dead',?,'destroyed',0.10,4,?,?,3.0)",
        (reg.now_iso(), reg.now_iso(), reg.now_iso()))
    # live, created 10h ago at $0.10/hr => ~$1.00 accrued.
    # ⚠ `strftime(...'Z')`, NOT `datetime('now','-10 hours')`. `spend()` clips with
    # `MAX(created_at, since)`, which in SQLite is a LEXICAL comparison, and `datetime()` returns
    # `2026-08-03 19:45:00` (space) while `since` is `2026-08-03T05:45:00Z` (ISO T/Z). `' '` (0x20)
    # sorts before `'T'`, so a created_at that is chronologically LATER compared as SMALLER, MAX
    # returned `since`, and the box was billed the whole 24h window ($2.40) instead of its 10h age
    # ($1.00) — the assertion below read 5.40 vs an expected ~4.00. `julianday()` parses both forms
    # happily, which is why only the comparison was wrong and the arithmetic looked sane.
    # This was a TEST-ONLY defect: production writes `created_at` via `now_iso()` (verified against
    # the live registry: `2026-08-04T03:34:20Z`), so the two operands always match there and the
    # clipping is correct. Fixing it in the fixture keeps the guard honest without touching the
    # money path.
    conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
        "VALUES (2,'runq_live',strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 hours'),'live',0.10,4,?)",
        (reg.now_iso(),))
    conn.commit()

    since_1h = conn.execute("SELECT datetime('now','-1 hour')").fetchone()[0].replace(" ", "T") + "Z"
    windowed = reg.spend(conn, since_1h)
    # the live box contributes only the LAST hour (~$0.10), not all 10 hours ($1.00)
    assert 3.0 < windowed < 3.25, windowed

    since_24h = conn.execute("SELECT datetime('now','-24 hours')").fetchone()[0].replace(" ", "T") + "Z"
    wide = reg.spend(conn, since_24h)
    assert 3.9 < wide < 4.1, wide  # $3.00 terminal + ~$1.00 of live accrual


def test_preempt_requeue_does_not_increment_retries_vs_infra_fail_does(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="preempted", state="running", retries_used=0)
    _insert(conn, id="infra", state="running", retries_used=0)

    r1 = reg.transition(conn, "preempted", "preempting", "preempt_intent", "evicted")
    assert r1.ok
    r2 = reg.transition(conn, "preempted", "queued", "preempt_requeue", "checkpoint carried",
                         extra_set={"instance_id": None})
    assert r2.ok
    assert reg.get_task(conn, "preempted")["retries_used"] == 0

    r3 = reg.transition(conn, "infra", "infra_failed", "infra_failed", "instance lost")
    assert r3.ok
    r4 = reg.transition(conn, "infra", "queued", "requeue", "retrying",
                         extra_set={"retries_used": 0.5, "instance_id": None})
    assert r4.ok
    assert reg.get_task(conn, "infra")["retries_used"] == 0.5


def test_rate_sums_only_committed_states(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    conn.executemany(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, hard_cap_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (1, "runq_a", reg.now_iso(), "live", 0.212, 4, reg.now_iso()),
            (2, "runq_b", reg.now_iso(), "live", 0.298, 4, reg.now_iso()),
            (3, "runq_c", reg.now_iso(), "destroyed", 9.99, 4, reg.now_iso()),
        ])
    conn.commit()
    assert reg.rate(conn) == pytest.approx(0.510)


def test_preempting_reaches_every_terminal_state_running_can(tmp_path):
    # Regression (live incident 2026-07-13/14): a task the dispatcher had already CAS'd into
    # `preempting` (invariant 17b's preempt_intent) never got a fresh checkpoint, so the box-side
    # kill guard never fired and the run kept going to its own natural outcome. These three
    # transitions were missing entirely, so `transition()` silently returned ok=False (it never
    # raises) and the task was stuck `preempting` forever with its results stranded on disk.
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="done_from_preempting", state="preempting")
    r = reg.transition(conn, "done_from_preempting", "done", "done", "completion artifact verified")
    assert r.ok and r.reason == "ok"
    assert reg.get_task(conn, "done_from_preempting")["state"] == "done"

    _insert(conn, id="failed_from_preempting", state="preempting")
    r = reg.transition(conn, "failed_from_preempting", "task_failed", "task_failed", "worker exit 1")
    assert r.ok
    assert reg.get_task(conn, "failed_from_preempting")["state"] == "task_failed"

    _insert(conn, id="lost_from_preempting", state="preempting")
    r = reg.transition(conn, "lost_from_preempting", "infra_failed", "infra_failed", "instance lost")
    assert r.ok
    assert reg.get_task(conn, "lost_from_preempting")["state"] == "infra_failed"


def test_used_slots_includes_preempting(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, hard_cap_at) "
        "VALUES (1,'runq_a',?,?,0.2,4,?)", (reg.now_iso(), "live", reg.now_iso()))
    _insert(conn, id="t1", state="running", instance_id=1, slots=2)
    _insert(conn, id="t2", state="preempting", instance_id=1, slots=1)
    _insert(conn, id="t3", state="done", instance_id=1, slots=1)
    assert reg.used_slots(conn, 1) == 3


def test_claimed_can_infra_fail_then_requeue(tmp_path):
    # Retrospective bug (2026-07-15): ("claimed","infra_failed") was missing, so _infra_fail silently
    # no-op'd on a claimed task whose box was destroyed/lost, stranding it. The pair is now legal and
    # infra_failed->queued (requeue) completes the recovery.
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="c1", state="claimed", instance_id=5)
    assert reg.transition(conn, "c1", "infra_failed", "infra_failed", "box destroyed").ok
    assert reg.transition(conn, "c1", "queued", "requeue", "retry",
                          extra_set={"instance_id": None, "retries_used": 0.5}).ok
    row = reg.get_task(conn, "c1")
    assert row["state"] == "queued" and row["instance_id"] is None and row["retries_used"] == 0.5


def test_a_worker_that_STARTS_before_we_record_the_ship_is_not_stranded(tmp_path):
    """The worker can launch inside the gap between the bundle landing and our `shipped` write.

    Measured on owned box -1, 2026-08-04: worker `claim` 05:02:13, worker `start` 05:02:16,
    dispatcher `ship` 05:02:26 — the trainer was already running ten seconds before we said we had
    shipped it. `_pull_worker_state` applied that `start` against state `claimed`; the pair was
    missing, `transition()` returned ok=False WITHOUT raising, and the row was never re-read, so the
    task sat in `shipped` for its whole run. `("shipped","done")` is absent too, so the completion
    would have no-opped as well and left an OPEN row holding a worker slot forever.

    Third occurrence of the same shape out of `claimed` (see the two tests/pairs above)."""
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    _insert(conn, id="r1", state="claimed", instance_id=7)
    assert reg.transition(conn, "r1", "running", "start", "worker beat the ship write").ok, \
        "a start that races our own ship bookkeeping must not silently no-op"
    assert reg.get_task(conn, "r1")["state"] == "running"
    # ...and the normal completion path is then reachable, which is the whole point: the failure was
    # not the missing `running` row, it was that `shipped` -> `done` is illegal too.
    assert reg.transition(conn, "r1", "done", "done", "finished").ok
