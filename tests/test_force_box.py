"""Forced box placement — task-dispatcher spec inv. 4i, box-pause spec inv. 19 / 20e.

Owner, 2026-10-02: *"queue this task on this specific box, ignoring other constraints."*

`resource_hint.force_box: true` + an explicit `box` places a task on that OWNED box as soon as the
box is live, past every resource and time-of-day ADMISSION gate. A bypass is the last thing that
should pass a test for the wrong reason, so the shape of this file is deliberate:

  * every "it bypasses gate X" test carries an IDENTICAL UNFORCED CONTROL that gate X refuses —
    without it the test would pass on a fixture where X was not binding at all;
  * every "it does NOT bypass Y" test asserts the forced task and its control hold TOGETHER;
  * the hint is checked fail-closed on each axis it could be reached by accident.
"""

import base64
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNQ = ROOT / "fleet" / "runq.py"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")
reg = _load("registry_db", "fleet/registry_db.py")
cap = _load("capacity", "fleet/capacity.py")
bca = _load("box_capacity_apply", "fleet/box_capacity_apply.py")
jm = _load("job_manifest", "fleet/job_manifest.py")
sw = _load("spool_worker", "fleet/spool_worker.py")

LABEL = "gpudesktop"
FAR_CAP = "2099-01-01T00:00:00Z"
# The motivating job: the whole RTX 5080 (14 of 16 GB) and 22 of 24 cores.
BIG = {"cores_per_lane": 22, "vram_per_lane_gb": 14.0}
# gpudesktop's DAY window: cpu 0.5 x 24 cores, vram 0.5 x 16 GB.
DAY_BUDGET = {"cores": 12.0, "vram_gb": 8.0}
SCHED = {"tz": "UTC", "cores": 24, "vram_gb": 16, "_comment": "stripped before the push",
         "windows": [{"from": "23:00", "to": "07:00", "cpu": 0.9, "vram": 1.0},
                     {"from": "07:00", "to": "23:00", "cpu": 0.5, "vram": 0.5, "gpu_power": 0.7}]}


@pytest.fixture(autouse=True)
def _isolate_experiments_root(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", root)


def _settings(**over):
    s = dict(disp.DEFAULT_SETTINGS)
    s.update({"current_rate": 0.0, "current_balance": 25.0, "deny_machine_ids": set()})
    s.update(over)
    return s


def _box(**over):
    """An owned, live box as `place()` sees it — roomy and ungated unless a test says otherwise."""
    b = {"id": -4, "label": LABEL, "state": "live", "source": "owned", "gpu_name": "RTX 5080",
         "slots_total": 4, "slots_nominal": 4, "dph_usd": 0.0, "minutes_to_hard_cap": 10 ** 6,
         "occupants": [], "resource_cap": None, "measured": None}
    b.update(over)
    return b


def _task(force=True, box=LABEL, hint=None, **over):
    h = dict(BIG if hint is None else hint)
    if box is not None:
        h["box"] = box
    if force is not None:
        h["force_box"] = force
    t = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 50, "retries_used": 0,
         "max_retries": 1, "resource_hint": h}
    t.update(over)
    return t


def _occupant(**over):
    o = {"id": "x", "slots": 1, "state": "running", "est_minutes": 600, "running_minutes_ago": 0,
         "priority": 50, "cores": 4.0, "vram_gb": 2.0, "ram_gb": 2.0}
    o.update(over)
    return o


def _offer():
    return {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 24.0, "cpu_cores_effective": 32.0,
            "ram_gb": 64.0, "machine_id": 11, "reliability": 0.99, "cpu_ghz": 5.7,
            "cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}


def _place(task, boxes, settings=None, offers=(), queue=()):
    return disp.place(task, boxes, list(offers), list(queue), settings or _settings(), 0)


# --------------------------------------------------------------------------------------------
# 4i-1: the hint. Fail-closed on every axis.
# --------------------------------------------------------------------------------------------
class TestTheHint:
    def test_forced_needs_literal_true_an_explicit_box_and_no_colocate(self):
        assert disp.forced_box({"box": LABEL, "force_box": True}) == LABEL
        assert disp.forced_box({"box": -4, "force_box": True}) == "-4"
        assert disp.forced_box(None) is None and disp.forced_box({}) is None
        assert disp.forced_box({"box": LABEL}) is None                       # absent
        assert disp.forced_box({"box": LABEL, "force_box": False}) is None
        assert disp.forced_box({"force_box": True}) is None                  # no box
        assert disp.forced_box({"box": "  ", "force_box": True}) is None     # blank box
        for not_true in ("true", "True", 1, "1", "yes"):                     # only the bool forces
            assert disp.forced_box({"box": LABEL, "force_box": not_true}) is None, not_true
        # A pinned colocation member ALSO carries `box` (stamped by the coordinator): never forced.
        assert disp.forced_box({"box": LABEL, "force_box": True, "colocate": "g:1"}) is None

    def test_force_without_a_box_is_an_ORDINARY_task(self):
        """It is not rejected by `place()` — it is simply not forced, and is gated like anyone."""
        full = _box(occupants=[_occupant(slots=4)])
        p = _place(_task(box=None), [full])
        assert p.action == "hold" and p.bypassed is None, p
        roomy = _place(_task(box=None, hint={}), [_box()])
        assert roomy.action == "pack" and roomy.bypassed is None, roomy

    def test_a_string_true_is_not_a_force(self):
        full = _box(occupants=[_occupant(slots=4)])
        p = _place(_task(force="true"), [full])
        assert p.action == "hold" and "box_target" in p.reason and p.bypassed is None, p

    def test_force_with_colocate_is_not_a_force(self):
        full = _box(occupants=[_occupant(slots=4)])
        t = _task()
        t["resource_hint"]["colocate"] = "camp:1"
        p = _place(t, [full])
        assert p.action == "hold" and p.bypassed is None and "colocate" in p.reason, p

    def test_a_RENTAL_is_never_forced(self):
        """Fail-closed where money is: the bypass must never be why a paid box is over-packed."""
        rental = _box(id=4242, label="runq_abc", source="vast", dph_usd=0.07,
                      occupants=[_occupant(slots=4)])
        p = _place(_task(box=4242), [rental])
        assert p.action == "hold" and "box_target" in p.reason and p.bypassed is None, p

    def test_an_unknown_box_holds_and_still_says_so(self):
        p = _place(_task(box="gamingdesktoq"), [_box()])
        assert p.action == "hold" and "NO SUCH BOX" in p.reason, p


# --------------------------------------------------------------------------------------------
# 4i-2: every ADMISSION gate is bypassed — and each one really was refusing the control.
# --------------------------------------------------------------------------------------------
def _measured(load1):
    return {"cores": 24, "load1": load1, "ram_total_gb": 29.0, "ram_avail_gb": 20.0,
            "vram_total_gb": 16.0, "vram_used_gb": 15.5, "gpu_util": 99.0, "at": time.time()}


GATES = {
    # name -> (box overrides, the substring the audit line must carry)
    "slots_full": (dict(occupants=[_occupant(slots=4)]), "slots: free 0 < need 1"),
    "day_window_slot_cap_zero": (dict(slots_total=0, slots_nominal=16),
                                 "effective slots_total 0 of 16 nominal"),
    "budget_exhausted": (dict(resource_cap=dict(DAY_BUDGET)), "budget (18a)"),
    "zero_headroom": (dict(measured=_measured(load1=24.0)), "headroom (23)"),
    "overpack_cooldown": (dict(overpack_cooldown=True), "overpack_cooldown"),
}


class TestAdmissionGatesAreBypassed:
    @pytest.mark.parametrize("gate", sorted(GATES))
    def test_the_forced_task_packs_where_its_unforced_twin_is_refused(self, gate):
        over, audit = GATES[gate]
        box = _box(**over)
        if gate == "zero_headroom":
            box["measured"]["at"] = time.time()             # a FRESH sample, or the gate abstains

        control = _place(_task(force=None), [box])
        assert control.action == "hold" and "box_target" in control.reason, (
            f"the control was NOT refused by {gate} — this fixture proves nothing: {control}")
        assert control.bypassed is None

        forced = _place(_task(), [box])
        assert forced.action == "pack" and forced.target == -4, forced
        assert forced.reason.startswith("forced_box:"), forced.reason
        assert any(audit in b for b in forced.bypassed), (gate, forced.bypassed)

    def test_every_gate_at_once_is_one_audit_entry_each(self):
        box = _box(slots_total=0, slots_nominal=16, resource_cap=dict(DAY_BUDGET),
                   measured=_measured(load1=24.0), overpack_cooldown=True)
        p = _place(_task(), [box])
        assert p.action == "pack", p
        assert len(p.bypassed) == 4, p.bypassed
        for audit in ("overpack_cooldown", "slots:", "budget (18a)", "headroom (23)"):
            assert sum(audit in b for b in p.bypassed) == 1, (audit, p.bypassed)

    def test_the_audit_carries_the_numbers_that_decided_each_gate(self):
        box = _box(resource_cap=dict(DAY_BUDGET), occupants=[_occupant(cores=4.0, vram_gb=2.0)])
        (line,) = disp.admission_refusals(_task(), box, _settings())
        assert "cores 4.0 used + 22.0 vs 12.0" in line and "vram 2.0 used + 14.0 vs 8.0" in line

    def test_a_forced_task_that_fits_anyway_is_still_forced_and_says_nothing_refused(self):
        p = _place(_task(hint={"cores_per_lane": 1, "vram_per_lane_gb": 0.5}), [_box()])
        assert p.action == "pack" and p.bypassed == [], p
        assert "it fits anyway" in p.reason

    def test_an_ordinary_pack_carries_no_bypass_marker(self):
        p = _place(_task(force=None, hint={"cores_per_lane": 1, "vram_per_lane_gb": 0.5}), [_box()])
        assert p.action == "pack" and p.bypassed is None, p
        assert not p.reason.startswith("forced_box")

    def test_admission_refusals_agrees_with_fits_now(self):
        """The audit CALLS the gates rather than re-deriving them; pin that it cannot drift."""
        s = _settings()
        for over, _ in GATES.values():
            box = _box(**over)
            t = _task(force=None)
            assert bool(disp.admission_refusals(t, box, s)) == (not disp._fits_now(t, box, s))
        assert disp.admission_refusals(_task(hint={}), _box(), s) == []
        assert disp._fits_now(_task(hint={}), _box(), s)


# --------------------------------------------------------------------------------------------
# 4i-3: box STATE is never bypassed.
# --------------------------------------------------------------------------------------------
class TestBoxStateIsRespected:
    @pytest.mark.parametrize("state", ["paused", "unreachable", "provisioning", "draining"])
    def test_a_box_that_is_not_live_refuses_BOTH(self, state):
        box = _box(state=state)
        control = _place(_task(force=None), [box])
        forced = _place(_task(), [box])
        assert control.action == "hold" and forced.action == "hold", (control, forced)
        assert forced.reason.startswith("force_box:") and repr(state) in forced.reason, forced
        assert forced.bypassed is None

    @pytest.mark.parametrize("flag,named", [("drain_held", "drain-held"),
                                            ("ship_quarantined", "ship-quarantined"),
                                            ("worker_roll_held", "worker-roll-held")])
    def test_a_held_live_box_refuses_BOTH(self, flag, named):
        box = _box(**{flag: True})
        control = _place(_task(force=None), [box])
        forced = _place(_task(), [box])
        assert control.action == "hold" and forced.action == "hold", (control, forced)
        assert forced.reason.startswith("force_box:") and named in forced.reason, forced

    def test_the_control_for_the_above_a_healthy_box_takes_the_forced_task(self):
        assert _place(_task(), [_box()]).action == "pack"

    def test_the_hard_cap_window_still_binds(self):
        p = _place(_task(est_minutes=100), [_box(minutes_to_hard_cap=30)])
        assert p.action == "hold" and "hard cap" in p.reason, p

    def test_infeasible_est_is_unchanged(self):
        s = _settings()
        too_long = int(s["hard_cap_hours"] * 60) + 1
        p = _place(_task(est_minutes=too_long), [_box()], settings=s)
        assert p.action == "hold" and "infeasible_est" in p.reason, p

    def test_a_requires_gpu_task_is_not_forced_onto_a_box_without_one(self):
        t = _task()
        t["resource_hint"]["requires_gpu"] = True
        p = _place(t, [_box(gpu_name=None)])
        assert p.action == "hold" and "GPU" in p.reason, p
        assert _place(t, [_box()]).action == "pack"                     # control: it has one

    def test_it_NEVER_RENTS(self):
        """Priority 90 clears the backlog bar, so only the forced branch stands between a forced
        task on a paused box and a rental that could never become that box."""
        t = _task(priority=90)
        p = _place(t, [_box(state="paused")], offers=[_offer()], queue=[_task(id=f"q{i}")
                                                                        for i in range(5)])
        assert p.action == "hold" and p.reason.startswith("force_box:"), p

    def test_it_NEVER_PREEMPTS_even_with_the_switch_on(self):
        """An unforced priority-90 task evicts the low-priority occupant of a full box (the
        control); the forced one packs BESIDE it instead, and a forced task that cannot place
        (drain-held box) holds rather than evicting."""
        s = _settings(preempt_enabled=True)
        victim = _occupant(slots=4, priority=10)
        small = {"cores_per_lane": 1, "vram_per_lane_gb": 0.5}
        control = _place(_task(force=None, priority=90, hint=small), [_box(occupants=[victim])],
                         settings=s)
        assert control.action == "preempt", control
        forced = _place(_task(priority=90, hint=small), [_box(occupants=[victim])], settings=s)
        assert forced.action == "pack" and forced.victims is None, forced
        held = _place(_task(priority=90, hint=small),
                      [_box(occupants=[victim], drain_held=True)], settings=s)
        assert held.action == "hold", held


# --------------------------------------------------------------------------------------------
# The real dispatcher: view, claim, audit event, and the effect on OTHER tasks (4i-4, 4i-8).
# --------------------------------------------------------------------------------------------
class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


class _Run:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return _Proc(self.rc, "")

    def remotes(self):
        return [c[-1] for c in self.calls if c]

    def pushes(self):
        return [r for r in self.remotes() if "fleet_host" in r]


def _dispatcher(tmp_path, run=None, **settings):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run or _Run(), vastai_run=_Run())
    d.settings.update(settings)
    d._offers = lambda: []
    return d


def _owned(conn, iid=-4, label=LABEL, state="live", slots=4, source="owned", gpu="RTX 5080"):
    conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
        "ssh_host, ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (iid, None, label, reg.now_iso(), state, 0.0, gpu, "10.0.0.9", 2222, slots, FAR_CAP, source))
    conn.commit()


def _iso_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(conn, tid, state, iid=None, hint=None, ago=0, priority=50):
    at = _iso_ago(ago)
    extra = {"resource_hint_json": json.dumps(hint)} if hint is not None else {}
    reg.insert_task(conn, id=tid, created_at=at, created_by="t", grp="g", name=tid,
                    entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                    arm_hash=tid, git_sha="d", slots=1, est_minutes=5, priority=priority,
                    max_retries=3, state=state, instance_id=iid, **extra)
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (at, tid))
    conn.commit()


def _state(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def _events(conn, event):
    return [dict(r) for r in conn.execute("SELECT * FROM events WHERE event=? ORDER BY seq",
                                          (event,))]


FORCED_HINT = {**BIG, "box": LABEL, "force_box": True}
PLAIN_HINT = {**BIG, "box": LABEL}


def _day_window(d, slots=0):
    """The box as it stands at 2pm: a slot cap of `slots` and the day budget."""
    d._capacity_slots = lambda inst, now=None: slots
    d._capacity_budget = lambda inst, now=None: dict(DAY_BUDGET)
    d._capacity_schedule = lambda inst: cap.load(json.loads(json.dumps(SCHED)))


class TestThroughTheRealDispatcher:
    def test_the_REAL_view_carries_what_the_forced_branch_reads(self, tmp_path):
        """Hand-built fixtures agree with a broken producer (`label` 2026-08-08, `gpu_name`
        2026-09-04). `place()` decides "forced" on `source`, so read it off the real view."""
        d = _dispatcher(tmp_path)
        _owned(d.conn)
        _day_window(d)
        (view,) = d._instances_view()
        assert view["source"] == "owned" and view["label"] == LABEL
        assert view["slots_total"] == 0 and view["slots_nominal"] == 4

    def test_day_window_forced_claims_and_its_unforced_twin_does_not(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned(d.conn)
        _day_window(d)
        _row(d.conn, "CONTROL", "queued", hint=PLAIN_HINT, ago=2)        # older: placed first
        _row(d.conn, "FORCED", "queued", hint=FORCED_HINT, ago=1)
        d._place_queue()

        assert _state(d.conn, "CONTROL")["state"] == "queued"
        forced = _state(d.conn, "FORCED")
        assert forced["state"] == "claimed" and forced["instance_id"] == -4

        (ev,) = _events(d.conn, "forced_placement")
        assert ev["task_id"] == "FORCED" and ev["instance_id"] == -4
        assert "slots: free 0 < need 1" in ev["detail"] and "budget (18a)" in ev["detail"]
        (claim,) = [e for e in _events(d.conn, "claim") if e["task_id"] == "FORCED"]
        assert claim["detail"].startswith("forced_box:")

    def test_a_paused_box_takes_neither(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned(d.conn, state="paused")
        _day_window(d)
        _row(d.conn, "CONTROL", "queued", hint=PLAIN_HINT, ago=2)
        _row(d.conn, "FORCED", "queued", hint=FORCED_HINT, ago=1)
        d._place_queue()
        assert _state(d.conn, "CONTROL")["state"] == "queued"
        assert _state(d.conn, "FORCED")["state"] == "queued"
        assert _events(d.conn, "forced_placement") == []
        hold = [e for e in _events(d.conn, "hold") if e["task_id"] == "FORCED"][-1]
        assert hold["detail"].startswith("force_box:") and "'paused'" in hold["detail"]
        # ...and resuming it is all it takes — the control for "the bypass itself works here".
        d.conn.execute("UPDATE instances SET state='live' WHERE id=-4")
        d.conn.commit()
        d._place_queue()
        assert _state(d.conn, "FORCED")["state"] == "claimed"

    def test_the_forced_occupant_counts_against_everyone_placed_after_it(self, tmp_path):
        """4i-4: with NO forced task, two 4-core jobs fit a 12-core day budget; once the forced
        22-core job is on the box in the same pass, neither does."""
        small = {"cores_per_lane": 4, "vram_per_lane_gb": 2.0}

        def run(with_forced):
            sub = tmp_path / ("with" if with_forced else "without")
            sub.mkdir()
            d = _dispatcher(sub)
            _owned(d.conn, slots=8)
            _day_window(d, slots=8)
            if with_forced:
                _row(d.conn, "FORCED", "queued", hint=FORCED_HINT, ago=5)
            for i in range(2):
                _row(d.conn, f"S{i}", "queued", hint=small, ago=2 - i)
            d._place_queue()
            return [_state(d.conn, f"S{i}")["state"] for i in range(2)]

        assert run(with_forced=False) == ["claimed", "claimed"]
        assert run(with_forced=True) == ["queued", "queued"]

    def test_a_lost_claim_logs_no_forced_placement(self, tmp_path):
        """The decision is not the claim: the audit event is written only when the CAS held."""
        d = _dispatcher(tmp_path)
        _owned(d.conn)
        _row(d.conn, "FORCED", "cancelled", hint=FORCED_HINT)             # cannot go -> claimed
        p = disp.Placement("pack", -4, None, "forced_box: test", bypassed=["slots: x"])
        view = d._instances_view()
        assert d._apply_placement(d._task_view(_state(d.conn, "FORCED")), p, view) is None
        assert _events(d.conn, "forced_placement") == []

    def test_task_json_carries_the_marker_only_for_a_forced_occupant(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned(d.conn)
        _owned(d.conn, iid=77, label="runq_x", source="vast")
        _row(d.conn, "FORCED", "claimed", iid=-4, hint=FORCED_HINT)
        _row(d.conn, "PLAIN", "claimed", iid=-4, hint=PLAIN_HINT)
        _row(d.conn, "ELSEWHERE", "claimed", iid=77, hint=FORCED_HINT)    # forced hint, wrong box
        env = lambda tid: d._build_task_json(_state(d.conn, tid))["env"]    # noqa: E731
        assert env("FORCED") == {disp.FORCE_BOX_ENV: "1"}
        assert env("PLAIN") == {} and env("ELSEWHERE") == {}
        assert sw.validate_task_json(d._build_task_json(_state(d.conn, "FORCED"))) is None
        assert sw.FORCE_BOX_ENV == disp.FORCE_BOX_ENV


# --------------------------------------------------------------------------------------------
# 4i-5 / box-pause 19: a window flip never sheds a forced occupant.
# --------------------------------------------------------------------------------------------
class TestOverCapacityReaperSkipsForced:
    def _box_over_cap(self, tmp_path, preempt, run=None):
        d = _dispatcher(tmp_path, run=run, preempt_enabled=preempt)
        _owned(d.conn)
        _day_window(d, slots=1)
        d._capacity_budget = lambda inst, now=None: {"cores": 999.0, "vram_gb": 999.0}
        return d

    def test_the_control_without_the_hint_the_newest_task_is_shed(self, tmp_path):
        run = _Run()
        d = self._box_over_cap(tmp_path, preempt=True, run=run)
        _row(d.conn, "OLD", "running", iid=-4, hint=PLAIN_HINT, ago=60)
        _row(d.conn, "NEW", "running", iid=-4, hint=PLAIN_HINT, ago=1)
        d._reap_over_capacity()
        assert _state(d.conn, "NEW")["state"] == "preempting"
        assert _state(d.conn, "OLD")["state"] == "running"

    def test_a_forced_task_is_kept_even_when_it_is_the_newest(self, tmp_path):
        """Same box, same ages as the control — only the hint differs, and now NOTHING is shed:
        the forced task is not evicted, and it is not charged either, so the task that was already
        running keeps the one lane the day window allows."""
        run = _Run()
        d = self._box_over_cap(tmp_path, preempt=True, run=run)
        _row(d.conn, "OLD", "running", iid=-4, hint=PLAIN_HINT, ago=60)
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT, ago=1)
        d._reap_over_capacity()
        assert _state(d.conn, "FORCED")["state"] == "running"
        assert _state(d.conn, "OLD")["state"] == "running"
        assert not any("/PREEMPT" in r for r in run.remotes())

    def test_its_arrival_never_evicts_what_was_already_running(self, tmp_path):
        """⛔ The one that pins 4i-1 ("never evicts") against the over-capacity reaper. Two 4-core
        jobs sit inside a 12-core day budget. A forced 22-core job lands. Charging its footprint
        would put the box at 30 of 12 cores and shed BOTH existing jobs one poll later — an
        eviction caused by the bypass. They must be judged as if it were not there."""
        run = _Run()
        d = _dispatcher(tmp_path, run=run, preempt_enabled=True)
        _owned(d.conn, slots=8)
        _day_window(d, slots=8)                                           # budget 12 cores / 8 GB
        small = {"cores_per_lane": 4, "vram_per_lane_gb": 2.0}
        _row(d.conn, "A", "running", iid=-4, hint=small, ago=60)
        _row(d.conn, "B", "running", iid=-4, hint=small, ago=50)
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT, ago=1)
        d._reap_over_capacity()
        assert [_state(d.conn, t)["state"] for t in ("A", "B", "FORCED")] == ["running"] * 3
        assert _events(d.conn, "capacity_scaledown") == []

    def test_a_window_flip_still_sheds_the_UNFORCED_tasks_beside_it(self, tmp_path):
        """The reaper is not switched off for the box: with a one-lane window, the newer of two
        unforced tasks is shed exactly as in the control, forced task present or not."""
        run = _Run()
        d = self._box_over_cap(tmp_path, preempt=True, run=run)
        _row(d.conn, "OLD", "running", iid=-4, hint=PLAIN_HINT, ago=60)
        _row(d.conn, "MID", "running", iid=-4, hint=PLAIN_HINT, ago=30)
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT, ago=1)
        d._reap_over_capacity()
        assert _state(d.conn, "OLD")["state"] == "running"
        assert _state(d.conn, "MID")["state"] == "preempting"
        assert _state(d.conn, "FORCED")["state"] == "running"

    def test_a_forced_task_alone_over_a_zero_cap_is_left_running(self, tmp_path):
        d = self._box_over_cap(tmp_path, preempt=True)
        d._capacity_slots = lambda inst, now=None: 0
        d._capacity_budget = lambda inst, now=None: {"cores": 0.0, "vram_gb": 0.0}
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT, ago=1)
        d._reap_over_capacity()
        assert _state(d.conn, "FORCED")["state"] == "running"
        assert _events(d.conn, "capacity_scaledown") == []

    def test_regardless_of_the_preempt_switch(self, tmp_path):
        d = self._box_over_cap(tmp_path, preempt=False)
        _row(d.conn, "OLD", "running", iid=-4, hint=PLAIN_HINT, ago=60)
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT, ago=1)
        d._reap_over_capacity()
        assert _state(d.conn, "FORCED")["state"] == "running"
        assert _state(d.conn, "OLD")["state"] == "running"               # switch off: tolerated


class TestOverpackReaperLeavesForcedAlone:
    """4i-7: a forced task held at an OLD worker's launch gate is neither requeued (it would be
    re-forced onto the same box next poll) nor used as evidence for a learned cap."""

    def _wedged(self, tmp_path, hint):
        d = _dispatcher(tmp_path)
        _owned(d.conn)
        grace = d.settings["ship_launch_grace_min"]
        _row(d.conn, "R1", "running", iid=-4, ago=grace + 20)
        _row(d.conn, "S1", "shipped", iid=-4, hint=hint, ago=grace + 10)
        d._reap_overpacked_boxes()
        return d

    def test_the_control_an_unforced_gate_held_task_is_requeued_and_a_cap_learned(self, tmp_path):
        d = self._wedged(tmp_path, PLAIN_HINT)
        assert _state(d.conn, "S1")["state"] == "queued"
        assert _events(d.conn, "overpack_cap") or _events(d.conn, "overpack_cap_refused")

    def test_a_forced_one_stays_shipped_and_teaches_nothing(self, tmp_path):
        d = self._wedged(tmp_path, FORCED_HINT)
        assert _state(d.conn, "S1")["state"] == "shipped"
        assert _events(d.conn, "overpack_cap") == [] and _events(d.conn, "overpack_cap_refused") == []


# --------------------------------------------------------------------------------------------
# box-pause 20e: the HOST's hard caps lift while a forced task occupies the box.
# --------------------------------------------------------------------------------------------
def _decoded(remote_cmd):
    return json.loads(base64.b64decode(remote_cmd.split("echo ", 1)[1].split(" ", 1)[0]))


def _at(h):
    return datetime(2026, 1, 1, h, 0, 0, tzinfo=timezone.utc)


class TestUncappedSchedule:
    def test_it_is_a_valid_schedule_that_is_full_at_every_hour(self):
        u = cap.load(cap.uncapped(cap.load(SCHED)))
        assert set(u) == {"tz", "cores", "vram_gb", "windows"}
        for h in range(24):
            assert cap.active_window(u, _at(h)) is not None, h
            assert cap.cpu_cores_cap(u, _at(h)) == 24.0
            assert cap.gpu_power_fraction(u, _at(h)) is None
            assert cap.window_budget(u, _at(h)) == {"cores": 24.0, "vram_gb": 16.0}

    def test_the_control_the_configured_schedule_really_is_capped_by_day(self):
        s = cap.load(SCHED)
        assert cap.cpu_cores_cap(s, _at(14)) == 12.0 and cap.gpu_power_fraction(s, _at(14)) == 0.7

    def test_the_installed_enforcer_reads_it_as_no_cpu_cap_and_default_gpu_power(
            self, monkeypatch, tmp_path):
        """Nothing is re-installed on the host: the override is an ordinary schedule, applied by
        the enforcer exactly as shipped."""
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, f"{12 * 10 ** 9}\n", "")   # day cap held
            if cmd[0] == "nvidia-smi" and cmd[1].startswith("--query-gpu"):
                return subprocess.CompletedProcess(cmd, 0, "0, 252.00, 360.00, 250.00, 360.00\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bca.subprocess, "run", run)
        monkeypatch.setattr(bca.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(bca.os, "cpu_count", lambda: 24)
        cfg = tmp_path / "capacity.json"
        cfg.write_text(json.dumps(cap.uncapped(cap.load(SCHED))))
        assert bca.main(["--config", str(cfg), "--container", "w"]) == 0
        assert ["docker", "update", "--cpus", "24.0", "w"] in calls
        assert ["nvidia-smi", "-i", "0", "-pl", "360"] in calls


class TestHostScheduleOverride:
    def _d(self, tmp_path, run):
        d = _dispatcher(tmp_path, run=run)
        _owned(d.conn)
        d._capacity_schedule = lambda inst: cap.load(json.loads(json.dumps(SCHED)))
        return d

    def _configured(self, body):
        return len(body["windows"]) == 2 and body["windows"][1].get("gpu_power") == 0.7

    def _uncapped(self, body):
        return (all(w["cpu"] == 1.0 and w["vram"] == 1.0 and "gpu_power" not in w
                    for w in body["windows"]) and body["cores"] == 24 and body["tz"] == "UTC")

    def test_configured_then_uncapped_while_forced_then_configured_again(self, tmp_path):
        run = _Run()
        d = self._d(tmp_path, run)
        d._push_capacity_schedules()
        assert len(run.pushes()) == 1 and self._configured(_decoded(run.pushes()[0]))
        assert d._capacity_override(-4) is None

        # An UNFORCED occupant changes nothing (the control for "any occupant lifts the caps").
        _row(d.conn, "PLAIN", "running", iid=-4, hint=PLAIN_HINT)
        d._push_capacity_schedules()
        assert len(run.pushes()) == 1

        # A forced occupant: pushed AT ONCE (well inside the 30-min re-push interval).
        _row(d.conn, "FORCED", "claimed", iid=-4, hint=FORCED_HINT)
        d._push_capacity_schedules()
        assert len(run.pushes()) == 2 and self._uncapped(_decoded(run.pushes()[1]))
        assert d._capacity_override(-4)["tasks"] == ["FORCED"]
        (ev,) = _events(d.conn, "capacity_override")
        assert ev["instance_id"] == -4 and ev["task_id"] == "FORCED" and "FORCED" in ev["detail"]

        # Every occupant state keeps it lifted, and it is not re-pushed each poll.
        for state in ("shipped", "running", "preempting"):
            d.conn.execute("UPDATE tasks SET state=? WHERE id='FORCED'", (state,))
            d.conn.commit()
            d._push_capacity_schedules()
            assert len(run.pushes()) == 2, state
            assert d._forced_occupants({"id": -4, "source": "owned", "label": LABEL}) == ["FORCED"]

        # It leaves: the configured schedule goes straight back.
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='FORCED'")
        d.conn.commit()
        d._push_capacity_schedules()
        assert len(run.pushes()) == 3 and self._configured(_decoded(run.pushes()[2]))
        assert d._capacity_override(-4) is None
        (lifted,) = _events(d.conn, "capacity_override_lifted")
        assert lifted["task_id"] == "FORCED" and lifted["instance_id"] == -4
        # ...so `runq show FORCED` carries the whole trail.
        trail = [e["event"] for e in reg.get_events(d.conn, "FORCED")]
        assert trail[-2:] == ["capacity_override", "capacity_override_lifted"], trail
        d._push_capacity_schedules()
        assert len(run.pushes()) == 3                                     # and it is stable

    def test_the_coordinator_keeps_gating_OTHER_tasks_on_the_configured_schedule(self, tmp_path):
        """20e-c: only the host's copy changes. `_capacity_budget` is the configured window."""
        d = self._d(tmp_path, _Run())
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT)
        d._push_capacity_schedules()
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=-4").fetchone())
        assert d._capacity_budget(inst, now=_at(14)) == DAY_BUDGET
        assert d._capacity_slots(inst, now=_at(14)) == cap.effective_slots(
            cap.load(SCHED), _at(14), d.settings["cores_per_lane"], d.settings["vram_per_lane_gb"])

    def test_a_failed_push_records_nothing_and_is_retried(self, tmp_path):
        run = _Run()
        d = self._d(tmp_path, run)
        d._push_capacity_schedules()
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT)
        run.rc = 255
        d._push_capacity_schedules()
        assert d._capacity_override(-4) is None and _events(d.conn, "capacity_override") == []
        run.rc = 0
        d._push_capacity_schedules()
        assert d._capacity_override(-4) is not None and len(_events(d.conn, "capacity_override")) == 1

    def test_a_restarted_coordinator_still_lifts_the_override(self, tmp_path):
        """The record is in the registry, not in memory: a restart that finds the host uncapped and
        no forced task left must put the configured schedule back."""
        run = _Run()
        d = self._d(tmp_path, run)
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT)
        d._push_capacity_schedules()
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='FORCED'")
        d.conn.commit()
        d.conn.close()

        run2 = _Run()
        d2 = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run2, vastai_run=_Run())
        d2._capacity_schedule = lambda inst: cap.load(json.loads(json.dumps(SCHED)))
        assert d2._capacity_override(-4) is not None
        d2._push_capacity_schedules()
        assert len(run2.pushes()) == 1 and self._configured(_decoded(run2.pushes()[0]))
        assert d2._capacity_override(-4) is None
        assert len(_events(d2.conn, "capacity_override_lifted")) == 1

    def test_placing_a_forced_task_pushes_in_the_SAME_poll(self, tmp_path):
        """20e-b: the push phase precedes placement in the cycle, so without the re-push inside
        `_place_queue` the host would stay capped until the next poll — after the ship."""
        run = _Run()
        d = self._d(tmp_path, run)
        d._capacity_slots = lambda inst, now=None: 0
        d._capacity_budget = lambda inst, now=None: dict(DAY_BUDGET)
        d._push_capacity_schedules()                                       # this poll's push phase
        assert len(run.pushes()) == 1
        _row(d.conn, "FORCED", "queued", hint=FORCED_HINT)
        d._place_queue()
        assert _state(d.conn, "FORCED")["state"] == "claimed"
        assert len(run.pushes()) == 2 and self._uncapped(_decoded(run.pushes()[1]))

    def test_an_ordinary_placement_pushes_nothing_extra(self, tmp_path):
        run = _Run()
        d = self._d(tmp_path, run)
        d._capacity_slots = lambda inst, now=None: 4
        d._capacity_budget = lambda inst, now=None: {"cores": 99.0, "vram_gb": 99.0}
        _row(d.conn, "PLAIN", "queued", hint={"cores_per_lane": 1, "vram_per_lane_gb": 0.5})
        d._place_queue()
        assert _state(d.conn, "PLAIN")["state"] == "claimed" and run.pushes() == []

    def test_a_box_with_no_schedule_has_nothing_to_lift(self, tmp_path):
        run = _Run()
        d = _dispatcher(tmp_path, run=run)
        _owned(d.conn)
        d._capacity_schedule = lambda inst: None
        _row(d.conn, "FORCED", "running", iid=-4, hint=FORCED_HINT)
        d._box_res[-4] = {"cores": 24}
        d._push_capacity_schedules()
        # box-pause 20b-1: a schedule-less box is pushed an explicit fully-open schedule (never an
        # `rm`), forced occupant or not — so there is no override to record on top of it.
        (push,) = run.pushes()
        assert "rm -f" not in push and "capacity.json" in push
        assert d._capacity_override(-4) is None and _events(d.conn, "capacity_override") == []


# --------------------------------------------------------------------------------------------
# 4i-7: the on-box launch gate.
# --------------------------------------------------------------------------------------------
class _Live:
    pid = 4321

    def poll(self):
        return None


class TestWorkerLaunchGate:
    def _worker(self, tmp_path, monkeypatch, env):
        w = sw.Worker(tmp_path / "spool")
        busy = sw.ActiveTask("busy", tmp_path / "busy", proc=_Live())      # one trainer already live
        d = tmp_path / "spool" / "active" / "new"
        (d / "repo").mkdir(parents=True)
        spec = {"task_id": "new", "grp": "g", "name": "new", "argv": ["python", "x.py"],
                "env": env, "est_minutes": 1, "git_sha": "d", "pip_extras": [], "resume_from": None}
        assert sw.validate_task_json(spec) is None
        w.active = {"busy": busy, "new": sw.ActiveTask("new", d, spec=spec)}
        launched = {}

        def popen(argv, **kw):
            launched["env"] = kw.get("env")
            return _Live()

        monkeypatch.setattr(sw.subprocess, "Popen", popen)
        # The gate would refuse on every arm; a launch can only come from the bypass.
        monkeypatch.setattr(sw, "should_launch", lambda *a, **k: (False, "cpu_load"))
        # An unforced task now reaches the hardware read (inv. 8a removed the count check that
        # used to return ahead of it), and the real one shells out through the Popen double above.
        monkeypatch.setattr(sw, "sample_hw", lambda: {
            "load1": 9.0, "cores": 8, "gpu_util": None, "vram_free_gb": None,
            "vram_total_gb": None, "proc_vram_gb": {}})
        return w, launched

    def _log(self, w):
        return [json.loads(line) for line in w.worker_log.read_text().splitlines()]

    def test_the_control_an_unforced_task_is_held_by_the_launch_gate(self, tmp_path, monkeypatch):
        w, launched = self._worker(tmp_path, monkeypatch, env={})
        w.launch_ready()
        assert launched == {} and w.active["new"].proc is None
        assert any(r["event"] == "launch_gate" and r["detail"].startswith("cpu_load:")
                   for r in self._log(w))

    def test_a_forced_task_launches_past_the_launch_gate(self, tmp_path, monkeypatch):
        w, launched = self._worker(tmp_path, monkeypatch, env={sw.FORCE_BOX_ENV: "1"})
        w.launch_ready()
        assert w.active["new"].proc is not None
        assert launched["env"][sw.FORCE_BOX_ENV] == "1"
        (rec,) = [r for r in self._log(w) if r["event"] == "launch_forced"]
        assert rec["task_id"] == "new" and "1 live" in rec["detail"]
        assert any(r["event"] == "start" and r["task_id"] == "new" for r in self._log(w))

    def test_only_the_exact_marker_forces(self, tmp_path, monkeypatch):
        w, launched = self._worker(tmp_path, monkeypatch, env={sw.FORCE_BOX_ENV: "true"})
        w.launch_ready()
        assert launched == {}

    def test_FREEZE_still_holds_a_forced_task(self, tmp_path, monkeypatch):
        """Soft pause is the owner's switch on the box itself (box-pause inv. 9)."""
        w, launched = self._worker(tmp_path, monkeypatch, env={sw.FORCE_BOX_ENV: "1"})
        (w.spool / "FREEZE").touch()
        w.launch_ready()
        assert launched == {} and w.active["new"].proc is None


# --------------------------------------------------------------------------------------------
# 4i-1 at the submission boundaries: registry_db.force_box_error, the manifest, and `runq add`.
# --------------------------------------------------------------------------------------------
class TestQueueTimeValidation:
    def _conn(self, tmp_path):
        conn = reg.connect(str(tmp_path / "runs.sqlite"))
        _owned(conn)
        _owned(conn, iid=77, label="runq_x", source="vast")
        return conn

    def test_a_hint_without_the_key_is_always_fine(self, tmp_path):
        conn = self._conn(tmp_path)
        for hint in (None, {}, {"box": "nope"}, '{"box": "nope"}', "not json at all"):
            assert reg.force_box_error(conn, hint) is None, hint

    def test_the_four_refusals_and_the_one_acceptance(self, tmp_path):
        conn = self._conn(tmp_path)
        err = lambda h: reg.force_box_error(conn, h)                        # noqa: E731
        assert err({"box": LABEL, "force_box": True}) is None
        assert err(json.dumps({"box": LABEL, "force_box": True})) is None   # the stored JSON text
        assert err({"box": "-4", "force_box": True}) is None                # by id
        assert err({"box": LABEL, "force_box": False}) is None              # inert
        assert "requires --box" in err({"force_box": True})
        assert "mutually exclusive" in err({"box": LABEL, "force_box": True, "colocate": "g:1"})
        assert "no registered box" in err({"box": "typo", "force_box": True})
        assert "not an owned box" in err({"box": "runq_x", "force_box": True})
        assert "must be a boolean" in err({"box": LABEL, "force_box": "true"})
        assert "not valid JSON" in err('{"force_box": tru')

    def test_without_a_registry_only_the_structural_half_runs(self):
        """The API-transport client: the registry of record is the coordinator's, so "is it an owned
        box" is left to the server (which runs the full check — see test_coord_api.py)."""
        err = lambda h: reg.force_box_error(None, h)                         # noqa: E731
        assert err({"box": "anything", "force_box": True}) is None
        assert "requires --box" in err({"force_box": True})
        assert "mutually exclusive" in err({"box": LABEL, "force_box": True, "colocate": "g:1"})
        assert "must be a boolean" in err({"box": LABEL, "force_box": 1})

    def test_the_manifest_accepts_a_bool_and_rejects_anything_else(self):
        base = {"manifest_version": jm.JOB_MANIFEST_VERSION, "run": ["python", "x.py"],
                "completion_artifact": "done.json"}
        ok = jm.parse({**base, "resources": {"box": LABEL, "force_box": True}})
        assert ok.resources["force_box"] is True
        assert jm.parse(jm.to_dict(ok)).resources == ok.resources          # survives the round trip
        with pytest.raises(jm.JobManifestError, match="force_box"):
            jm.parse({**base, "resources": {"box": LABEL, "force_box": "yes"}})


class TestManifestPassthrough:
    """`resources.force_box` beside `resources.box` in a config's `job` section reaches the hint
    through the same passthrough as `box`, and is checked on the FINAL hint like the flag is."""

    BASE = {"manifest_version": jm.JOB_MANIFEST_VERSION, "run": ["python", "x.py"],
            "completion_artifact": "done.json"}

    def _resolve(self, tmp_path, resources, **flags):
        import argparse
        runq = _load("runq", "fleet/runq.py")
        db = tmp_path / "runs.sqlite"
        conn = reg.connect(str(db))
        if not conn.execute("SELECT 1 FROM instances").fetchone():
            _owned(conn)
        conn.close()
        a = argparse.Namespace(est_minutes=5, vram_per_lane_gb=None, cores_per_lane=None,
                               box=None, colocate=None, force_box=False, db=str(db))
        for k, v in flags.items():
            setattr(a, k, v)
        return runq._manifest_est_and_hint(a, jm.parse({**self.BASE, "resources": resources}))

    def test_the_config_can_declare_it(self, tmp_path):
        _, hint, err = self._resolve(
            tmp_path, {"vram_gb": 14, "cores": 22, "box": LABEL, "force_box": True})
        assert err is None
        assert json.loads(hint) == {"vram_per_lane_gb": 14.0, "cores_per_lane": 22,
                                    "box": LABEL, "force_box": True}

    def test_the_flag_adds_it_to_a_config_declared_box(self, tmp_path):
        _, hint, err = self._resolve(tmp_path, {"box": LABEL}, force_box=True)
        assert err is None and json.loads(hint) == {"box": LABEL, "force_box": True}

    def test_a_config_declaring_force_without_a_box_is_refused(self, tmp_path):
        _, hint, err = self._resolve(tmp_path, {"force_box": True})
        assert hint is None and "requires --box" in err

    def test_a_config_without_the_key_never_opens_the_check(self, tmp_path):
        _, hint, err = self._resolve(tmp_path, {"box": "not-registered"})
        assert err is None and json.loads(hint) == {"box": "not-registered"}


def _runq(db, *args):
    env = dict(os.environ)
    env["RUNQ_ACTOR"] = "force-box-tests"
    # Belt and braces over conftest's `_never_queue_into_production`: this devcontainer carries
    # RUNQ_TRANSPORT=api and live credentials, and these tests queue FORCED tasks.
    env["RUNQ_TRANSPORT"] = "local"
    for var in ("COORD_API_URL", "COORD_API_CA", "COORD_API_CERT", "COORD_API_KEY"):
        env.pop(var, None)
    return subprocess.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def _add(db, name, *extra):
    return _runq(db, "add", "--group", "fb", "--name", name, "--entrypoint", "smoke",
                 "--est-minutes", "5", *extra, "--", "--updates", "10")


def _rows(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM tasks")]


class TestRunqAdd:
    @pytest.fixture()
    def db(self, tmp_path):
        path = tmp_path / "runs.sqlite"
        conn = reg.connect(str(path))
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile','false')")
        _owned(conn)
        _owned(conn, iid=77, label="runq_x", source="vast")
        conn.close()
        return path

    @pytest.mark.parametrize("extra,why", [
        (["--force-box"], "requires --box"),
        (["--force-box", "--colocate", "fb:1"], "requires --box"),
        (["--force-box", "--box", LABEL, "--colocate", "fb:1"], "mutually exclusive"),
        (["--force-box", "--box", "typo"], "no registered box"),
        (["--force-box", "--box", "runq_x"], "not an owned box"),
    ])
    def test_it_is_refused_and_queues_nothing(self, db, extra, why):
        r = _add(db, "a", *extra)
        assert r.returncode == 2 and why in r.stderr, r.stderr
        assert _rows(db) == []

    def test_an_owned_box_is_accepted_and_the_hint_lands_on_the_row(self, db):
        r = _add(db, "a", "--box", LABEL, "--force-box")
        assert r.returncode == 0, r.stderr
        (row,) = _rows(db)
        assert json.loads(row["resource_hint_json"]) == {"box": LABEL, "force_box": True}
        assert disp.forced_box(json.loads(row["resource_hint_json"])) == LABEL

    def test_plain_box_and_plain_force_are_untouched(self, db):
        """`--force` (the dedupe override) is a different flag and must not have become this one."""
        assert _add(db, "a", "--box", LABEL).returncode == 0
        assert _add(db, "b", "--box", LABEL, "--force").returncode == 0
        hints = {r["name"]: json.loads(r["resource_hint_json"]) for r in _rows(db)}
        assert hints == {"a": {"box": LABEL}, "b": {"box": LABEL}}

    def test_the_help_text_says_owner_authorized(self):
        r = _runq("unused.sqlite", "add", "--help")
        flat = "".join(r.stdout.split())                  # argparse may wrap at the hyphen
        assert "--force-box" in flat and "OWNER-AUTHORIZED" in flat, r.stdout + r.stderr
