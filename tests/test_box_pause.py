"""Box-pause spec (docs/specs/box-pause.spec.md): pause/drain an owned box without wedging the
fleet. Dispatcher-side reaper/drain/watchdog behaviour, the worker's SIGSTOP freeze, and the CLI."""

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")
reg = _load("registry_db", "fleet/registry_db.py")
ro = _load("reap_orphans", "fleet/reap_orphans.py")
cap = _load("capacity", "fleet/capacity.py")
sw = _load("spool_worker", "fleet/spool_worker.py")
bp = _load("box_pause", "fleet/box_pause.py")

_SCHED = {"tz": "UTC", "cores": 20, "vram_gb": 8, "windows": [
    {"from": "23:00", "to": "07:00", "cpu": 0.875, "vram": 0.875},
    {"from": "07:00", "to": "23:00", "cpu": 0.5, "vram": 0.75}]}


def _at(h):
    return datetime(2026, 1, 1, h, 0, 0, tzinfo=timezone.utc)

FAR_CAP = "2099-01-01T00:00:00Z"


class _FakeProc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


class _RecordingRun:
    """Records every ssh/rsync command the dispatcher issues and always succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return _FakeProc(0, "")

    def remotes(self):
        return [c[-1] for c in self.calls if c]


def _iso_ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _owned_box(conn, iid=-1, state="live", slots=6, ssh_host="192.168.0.9"):
    conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
        "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (iid, None, "laptop-gpu", reg.now_iso(), state, 0.0, ssh_host, 22, slots, FAR_CAP, "owned"))
    conn.commit()


def _task(conn, tid, iid, state, updated_ago_min=0, hint=None, slots=1):
    created = _iso_ago(updated_ago_min)
    extra = {"resource_hint_json": json.dumps(hint)} if hint else {}
    reg.insert_task(conn, id=tid, created_at=created, created_by="t", grp="g", name=tid,
                    entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                    arm_hash=tid, git_sha="d", slots=slots, est_minutes=1, priority=50, max_retries=3,
                    state=state, instance_id=iid, **extra)
    conn.commit()


def _dispatcher(tmp_path, run=None):
    r = run or _RecordingRun()
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=r, vastai_run=_RecordingRun())


def _mech_dispatcher(tmp_path, run=None):
    """A dispatcher for tests of the preemption/consolidation MECHANICS, with the global preempt
    switch explicitly ON.

    `preempt_enabled` ships FALSE (owner directive 2026-07-30, "disable preempts"), and that ONE flag
    gates all three fleet-initiated sources: priority preemption in `place()`, capacity scale-down in
    `_reap_over_capacity`, and consolidation drains. The mechanics were switched off, not removed, so
    they still need coverage — and the switch has its own tests in `test_dispatcher.py`.

    Stating the override here rather than folding it into `_dispatcher` mirrors `_mech_settings` in
    `test_dispatcher.py`: the rest of this file must keep testing the PRODUCTION configuration, and a
    reader can see exactly which classes run in a world production currently does not."""
    d = _dispatcher(tmp_path, run=run)
    d.settings["preempt_enabled"] = True
    return d


# --------------------------------------------------------------------------------------------
# Admission (inv. 1): a paused box takes no new work.
# --------------------------------------------------------------------------------------------
class TestPausedTakesNoWork:
    def test_paused_owned_box_is_not_a_pack_target(self, tmp_path):
        d = _dispatcher(tmp_path)
        d._offers = lambda: []  # no rentals — isolate the pack decision to the owned box
        _owned_box(d.conn, iid=-1, state="paused")
        _task(d.conn, "Q1", None, "queued")
        d._place_queue()
        # Paused -> place() (which admits only state=='live') never packs onto it.
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='Q1'").fetchone())["state"] == "queued"
        # Control: resume the box -> the very same task now packs onto it ($0 owned wins).
        d.conn.execute("UPDATE instances SET state='live' WHERE id=-1")
        d.conn.commit()
        d._place_queue()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='Q1'").fetchone())
        assert row["state"] == "claimed" and row["instance_id"] == -1


# --------------------------------------------------------------------------------------------
# Reaper carve-outs (inv. 3, 4): a deliberately-paused box's work is not auto-requeued.
# --------------------------------------------------------------------------------------------
class TestReaperCarveouts:
    def test_orphan_reaper_keeps_task_on_paused_box(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        _task(d.conn, "OB", -1, "running")
        d._reap_orphaned_tasks()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='OB'").fetchone())["state"] == "running"

    def test_stall_reaper_skips_paused_box_task(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        _task(d.conn, "SB", -1, "running", updated_ago_min=180)  # 3h > stall_timeout_min (90)
        d._reap_stalled()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='SB'").fetchone())["state"] == "running"

    def test_stall_reaper_still_reaps_a_live_box_task(self, tmp_path):
        # Control: the same aged task on a LIVE box IS reaped -> the skip is paused-specific.
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="live")
        _task(d.conn, "SL", -1, "running", updated_ago_min=180)
        d._reap_stalled()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='SL'").fetchone())["state"] == "queued"


# --------------------------------------------------------------------------------------------
# Hard drain eviction (inv. 12).
# --------------------------------------------------------------------------------------------
class TestSignalDrain:
    def test_reachable_box_preempts_and_writes_marker(self, tmp_path):
        run = _RecordingRun()
        d = _dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hard", "at": reg.now_iso()})
        _task(d.conn, "DR", -1, "running")
        d._signal_drain()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='DR'").fetchone())["state"] == "preempting"
        remotes = run.remotes()
        assert any("touch ~/spool/active/DR/PREEMPT" in r for r in remotes)
        assert any("rm -f ~/spool/FREEZE" in r for r in remotes)

    def test_unreachable_box_requeues_at_half_retry(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hard", "at": reg.now_iso()})
        _task(d.conn, "DU", -1, "running")
        for _ in range(3):  # >= owned_unreachable_fails -> unreachable
            d.tracker.record(-1, False)
        d._signal_drain()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='DU'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 0.5

    def test_preempt_marker_touched_once_not_re_issued(self, tmp_path):
        # Regression (found live 2026-07-22): re-touching PREEMPT every poll keeps the marker mtime
        # fresher than every checkpoint, so a slow-checkpointing trainer never satisfies the box-side
        # kill guard. The marker must be touched ONCE, at the running->preempting transition.
        run = _RecordingRun()
        d = _dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hard", "at": reg.now_iso()})
        _task(d.conn, "RT", -1, "running")
        d._signal_drain()  # poll 1: running -> preempting, touch once
        touches_after_1 = sum("touch ~/spool/active/RT/PREEMPT" in r for r in run.remotes())
        d._signal_drain()  # poll 2: already preempting -> must NOT re-touch
        touches_after_2 = sum("touch ~/spool/active/RT/PREEMPT" in r for r in run.remotes())
        assert touches_after_1 == 1, f"expected exactly one touch, got {touches_after_1}"
        assert touches_after_2 == 1, f"marker re-touched on a later poll ({touches_after_2} total)"

    def test_soft_paused_box_is_not_drained(self, tmp_path):
        # _signal_drain only acts on mode 'hard' — a soft pause leaves its (frozen) work in place.
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "soft", "at": reg.now_iso()})
        _task(d.conn, "SS", -1, "running")
        d._signal_drain()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='SS'").fetchone())["state"] == "running"


# --------------------------------------------------------------------------------------------
# Hard drain of UNDELIVERED work (inv. 12b) — the 2026-08-12 9-hour strand.
#
# The laptop went unreachable, its share of the fleet packed onto the tower, that box's spool
# disk hit 100%, and three cells claimed at 05:04 never shipped. The box was hard-drained at 05:45 —
# and `_signal_drain`, which only ever queried `running`/`preempting`, saw zero occupants and
# `continue`d. Every other reaper skips a `paused` box BY DESIGN (inv. 12c), so nothing in the
# system had an opinion and the work sat `claimed` for 9h until a human cancelled it by hand.
# --------------------------------------------------------------------------------------------
class TestSignalDrainUndelivered:
    def _drained_box(self, tmp_path, run=None):
        d = _dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hard", "at": reg.now_iso()})
        return d

    def test_claimed_task_with_no_running_occupant_is_requeued(self, tmp_path):
        """⛔ THE REGRESSION. No `running` occupant is the whole point: the old code `continue`d on an
        empty running set, so a box holding ONLY undelivered work drained nothing, forever."""
        run = _RecordingRun()
        d = self._drained_box(tmp_path, run=run)
        _task(d.conn, "UD", -1, "claimed")
        d._signal_drain()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='UD'").fetchone())
        assert row["state"] == "queued", "a claimed task on a hard-drained box must be requeued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 0, "a drain is the operator emptying the box — no retry cost"

    def test_shipped_task_is_requeued_and_spool_cleared_first(self, tmp_path):
        # A hard drain rm's FREEZE, so this box's worker is LIVE and would launch the shipped task.
        # The spool clear must therefore precede the requeue, or the task double-runs.
        run = _RecordingRun()
        d = self._drained_box(tmp_path, run=run)
        _task(d.conn, "SH", -1, "shipped")
        d._signal_drain()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='SH'").fetchone())["state"] == "queued"
        assert any("rm -rf ~/spool/incoming/SH ~/spool/active/SH" in r for r in run.remotes())

    def test_cleanup_failure_leaves_the_task_claimed(self, tmp_path):
        # Never requeue behind a spool copy we could not delete — that is the double-run.
        class _FailingRun(_RecordingRun):
            def __call__(self, cmd, **kwargs):
                self.calls.append(cmd)
                return _FakeProc(1, "")

        d = self._drained_box(tmp_path, run=_FailingRun())
        _task(d.conn, "CF", -1, "claimed")
        d._signal_drain()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='CF'").fetchone())
        assert row["state"] == "claimed", "a failed cleanup must defer, not requeue"
        assert row["instance_id"] == -1

    def test_unreachable_box_infra_fails_undelivered_at_half_retry(self, tmp_path):
        # Mirrors the unreachable branch for running tasks: we cannot clear the spool, so this is a
        # genuine infra loss and pays the same half retry.
        d = self._drained_box(tmp_path)
        _task(d.conn, "UU", -1, "claimed")
        for _ in range(3):
            d.tracker.record(-1, False)
        d._signal_drain()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='UU'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 0.5

    def test_soft_paused_box_keeps_its_claimed_task(self, tmp_path):
        # The mode gate still governs: a soft pause means "back soon", and inv. 3's orphan carve-out
        # is only sound because the 30-min watchdog escalates to hard, which is what then drains it.
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "soft", "at": reg.now_iso()})
        _task(d.conn, "SC", -1, "claimed")
        d._signal_drain()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='SC'").fetchone())["state"] == "claimed"

    def test_mixed_box_drains_running_and_undelivered_in_one_poll(self, tmp_path):
        # The two halves are disjoint by state, not alternatives — a box with both must drain both.
        run = _RecordingRun()
        d = self._drained_box(tmp_path, run=run)
        _task(d.conn, "MR", -1, "running")
        _task(d.conn, "MC", -1, "claimed")
        d._signal_drain()
        states = {r["id"]: r["state"] for r in
                  d.conn.execute("SELECT id, state FROM tasks WHERE id IN ('MR','MC')")}
        assert states["MR"] == "preempting", "the running half must still PREEMPT gracefully"
        assert states["MC"] == "queued", "the undelivered half must be requeued in the same poll"
        assert any("touch ~/spool/active/MR/PREEMPT" in r for r in run.remotes())
        assert any("rm -rf ~/spool/incoming/MC ~/spool/active/MC" in r for r in run.remotes())

    def test_requeued_task_is_placeable_again(self, tmp_path):
        """End-to-end: the point of the requeue is that the work RUNS somewhere else. A task left
        with a stale `instance_id` would be requeued on paper and still unplaceable in practice."""
        run = _RecordingRun()
        d = self._drained_box(tmp_path, run=run)
        d._offers = lambda: []
        _owned_box(d.conn, iid=-2, state="live")  # a healthy sibling to receive the work
        _task(d.conn, "RP", -1, "claimed")
        d._signal_drain()
        d._place_queue()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='RP'").fetchone())
        assert row["state"] == "claimed" and row["instance_id"] == -2


# --------------------------------------------------------------------------------------------
# Soft -> hard timeout watchdog (inv. 10).
# --------------------------------------------------------------------------------------------
class TestSoftTimeout:
    def test_escalates_after_timeout(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "soft", "at": _iso_ago(40)})  # > soft_pause_timeout_min (30)
        d._reap_paused_soft_timeout()
        meta = d._pause_meta(-1)
        assert meta["mode"] == "hard"
        assert "escalated_at" in meta

    def test_within_timeout_stays_soft(self, tmp_path):
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "soft", "at": _iso_ago(5)})
        d._reap_paused_soft_timeout()
        assert d._pause_meta(-1)["mode"] == "soft"


# --------------------------------------------------------------------------------------------
# Capacity schedule (capacity spec): caps -> inferred slots, time-of-day, graceful scale-down.
# --------------------------------------------------------------------------------------------
class TestCapacitySchedule:
    def test_day_vs_night_window(self):
        assert cap.active_window(_SCHED, _at(10))["cpu"] == 0.5     # 10:00 UTC -> day
        assert cap.active_window(_SCHED, _at(2))["cpu"] == 0.875    # 02:00 -> night (wraps midnight)

    def test_effective_slots_infers_from_binding_cap(self):
        # day: cpu 0.5*20=10, vram 0.75*8/0.6=10 -> 10; night: cpu 0.875*20=17, vram 0.875*8/0.6=11 -> 11
        assert cap.effective_slots(_SCHED, _at(10), 1, 0.6) == 10
        assert cap.effective_slots(_SCHED, _at(2), 1, 0.6) == 11

    def test_vram_binds_when_tight(self):
        tight = dict(_SCHED, vram_gb=2)                             # 2GB: vram caps well below cpu
        assert cap.effective_slots(tight, _at(2), 1, 0.6) == 2      # 0.875*2/0.6=2 << cpu 17

    def test_bad_schedule_raises(self):
        with pytest.raises(ValueError):
            cap.load('{"tz": "UTC", "cores": 20}')                  # missing vram_gb/windows

    def test_bad_lane_footprint_raises(self):
        with pytest.raises(ValueError):
            cap.load(json.dumps(dict(_SCHED, cores_per_lane=0)))     # must be > 0
        with pytest.raises(ValueError):
            cap.load(json.dumps(dict(_SCHED, vram_per_lane_gb="big")))


class TestLaneFootprintCalibration:
    """Inv. 18 regression (live 2026-07-27): dividing a real CPU/VRAM budget by the GLOBAL settings
    lane, while every task in flight occupies a much bigger one, over-advertises the box."""

    _REAL = dict(_SCHED, cores_per_lane=4, vram_per_lane_gb=2.0)     # what the tasks actually hint

    def test_schedule_declared_lane_overrides_settings_default(self):
        assert cap.lane_footprint(_SCHED, 1, 0.6) == (1.0, 0.6)      # undeclared -> settings default
        assert cap.lane_footprint(self._REAL, 1, 0.6) == (4.0, 2.0)  # declared -> the box's own

    def test_declared_lane_shrinks_inferred_slots_to_the_honest_count(self):
        # THE BUG: the day window budgets 10 cores / 6.0GB. Against the settings lane that inferred
        # 10 slots -> filling them demands 10x4 = 40 cores on a 20-core box. The honest count is 2.
        assert cap.effective_slots(_SCHED, _at(10), 1, 0.6) == 10        # pre-fix (5x over)
        assert cap.effective_slots(self._REAL, _at(10), 1, 0.6) == 2     # min(10/4, 6.0/2.0)
        assert cap.effective_slots(self._REAL, _at(2), 1, 0.6) == 3      # night: min(17.5/4, 7.0/2.0)

    def test_window_budget_is_the_absolute_resource_cap(self):
        assert cap.window_budget(_SCHED, _at(10)) == {"cores": 10.0, "vram_gb": 6.0}
        assert cap.window_budget(_SCHED, _at(2)) == {"cores": 17.5, "vram_gb": 7.0}

    def test_shipped_configs_parse_and_declare_no_hand_set_lane(self):
        """The real committed schedules — a typo here silently uncaps a box in production.

        Inv. 23f: an owned box must NOT hand-set `cores_per_lane`/`vram_per_lane_gb`. Both were set
        to 4 cores / 2.0GB on 07-27, calibrated from the heaviest job class in flight; ordinary tasks
        measure 1.00 core / ~1.4GB, so the desktop's 10-core day budget was divided down to 2 slots
        and the box ran at ~20% CPU with work queued. A constant fitted to one snapshot cannot track
        a change of job class — measured headroom (23) and the per-task budget (18a) do.

        EVERY schedule in the directory is checked, found by listing it. The list used to be
        hard-coded as (desktop, laptop-gpu), which missed `gpudesktop.json` from the day it was
        added and went red the day `desktop.json` was deliberately removed (2026-10-03)."""
        cores_by_label = {"laptop-gpu": 32, "gpudesktop": 24}
        shipped = sorted((ROOT / "configs" / "capacity").glob("*.json"))
        assert {p.stem for p in shipped} == set(cores_by_label), (
            "a capacity schedule was added or removed — record its core count here")
        for path in shipped:
            label = path.stem
            sched = cap.load(path.read_text())
            assert sched["cores"] == cores_by_label[label]
            assert "cores_per_lane" not in sched and "vram_per_lane_gb" not in sched, (
                f"{label}.json hand-sets a lane footprint again — see inv. 23f")
            for hour in (2, 10):
                slots = cap.effective_slots(sched, _at(hour), 1, 0.6)
                budget = cap.window_budget(sched, _at(hour))
                assert slots >= 4, f"{label}@{hour} infers only {slots} slots — too tight a ceiling"
                assert budget["cores"] > 0 and budget["vram_gb"] > 0

    def test_the_fleet_only_desktop_has_NO_schedule(self):
        """Owner, 2026-10-03: "this desktop should not have a night/day policy - always max open".
        A schedule's fractions leave room for an owner at the machine; with none there, the day
        window's `cpu: 0.5` held the box to 6 lanes whatever its slot count said. A file reappearing
        here would quietly put that cap back."""
        assert not (ROOT / "configs" / "capacity" / "desktop.json").exists()


class TestBudgetAdmission:
    """Inv. 18a: admit against the window's absolute cores/VRAM budget, not a slot scalar alone."""

    HINT4 = {"cores_per_lane": 4, "vram_per_lane_gb": 2.0}

    def _box_with_budget(self, d, cores, vram_gb, slots=12):
        _owned_box(d.conn, iid=-1, state="live", slots=slots)
        d._capacity_slots = lambda inst, now=None: slots            # slots deliberately NOT binding
        d._capacity_budget = lambda inst, now=None: {"cores": cores, "vram_gb": vram_gb}

    def test_budget_binds_within_a_single_placement_pass(self, tmp_path):
        # The pass-level failure: _place_queue snapshots slots_total ONCE, so a slots-only cap can
        # be spent entirely in one pass. 5 queued 4-core tasks vs a 10-core budget -> only 2 claim.
        d = _dispatcher(tmp_path)
        d._offers = lambda: []
        self._box_with_budget(d, cores=10.0, vram_gb=6.0)
        for i in range(5):
            _task(d.conn, f"B{i}", None, "queued", hint=self.HINT4)
        d._place_queue()
        claimed = [r[0] for r in d.conn.execute(
            "SELECT id FROM tasks WHERE state='claimed' AND instance_id=-1")]
        assert len(claimed) == 2, f"budget overshoot: {len(claimed)} x 4 cores vs a 10-core budget"
        assert d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='queued'").fetchone()[0] == 3

    def test_vram_can_be_the_binding_dimension(self, tmp_path):
        d = _dispatcher(tmp_path)
        d._offers = lambda: []
        self._box_with_budget(d, cores=64.0, vram_gb=5.0)            # cores ample, VRAM tight
        for i in range(4):
            _task(d.conn, f"V{i}", None, "queued", hint=self.HINT4)  # 2.0GB each
        d._place_queue()
        assert d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='claimed'").fetchone()[0] == 2   # 5.0/2.0

    def test_running_occupants_consume_the_budget(self, tmp_path):
        d = _dispatcher(tmp_path)
        d._offers = lambda: []
        self._box_with_budget(d, cores=10.0, vram_gb=6.0)
        _task(d.conn, "R0", -1, "running", hint=self.HINT4)          # 4 of 10 cores already spent
        _task(d.conn, "Q0", None, "queued", hint=self.HINT4)
        _task(d.conn, "Q1", None, "queued", hint=self.HINT4)
        d._place_queue()
        assert d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='claimed'").fetchone()[0] == 1   # 4+4 <= 10 < 12

    def test_box_without_a_schedule_is_unaffected(self, tmp_path):
        d = _dispatcher(tmp_path)
        d._offers = lambda: []
        _owned_box(d.conn, iid=-1, state="live", slots=4)
        d._capacity_slots = lambda inst, now=None: None
        d._capacity_budget = lambda inst, now=None: None             # no schedule -> no budget gate
        for i in range(4):
            _task(d.conn, f"N{i}", None, "queued", hint=self.HINT4)
        d._place_queue()
        assert d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='claimed'").fetchone()[0] == 4

    def test_task_footprint_falls_back_to_settings_lane(self):
        """Cores fall back to the settings lane. VRAM does so only for a task that claims the GPU
        (task-dispatcher inv. 26m): one that never touches the card spends none of a window's
        VRAM. Until 2026-10-03 this asserted (1.0, 0.6) for a hint-less task."""
        s = {"cores_per_lane": 1, "vram_per_lane_gb": 0.6}
        assert disp.task_footprint(None, 1, s) == (1.0, 0.0)
        assert disp.task_footprint({"requires_gpu": True}, 1, s) == (1.0, 0.6)
        assert disp.task_footprint({"cores_per_lane": 4, "vram_per_lane_gb": 2.0}, 2, s) == (8.0, 4.0)


class TestMeasuredHeadroom:
    """Inv. 23: admission gates on MEASURED spare capacity, not declared slots/hints. Owner
    directive 2026-07-28 after a hand-set lane constant throttled the desktop to 2 slots at ~20% CPU
    with work queued."""

    S = {**disp.DEFAULT_SETTINGS}
    # A real desktop probe: 20 cores, load 3.72, 31000MB total / 18000MB avail, 8GB GPU 2788MB used.
    # Keyed since inv. 25 — the four axes this class gates on are unchanged in meaning and units.
    PROBE = ("NPROC 20\nLOAD 3.72 3.36 2.81 4/1234 5678\nMEM 31000 18000\nGPU 8192, 2788, 34")

    def test_parses_every_axis(self):
        m = disp.parse_box_probe(self.PROBE)
        assert m["cores"] == 20 and m["load1"] == 3.72
        assert round(m["ram_avail_gb"], 1) == 17.6
        assert m["vram_total_gb"] == 8.0 and round(m["vram_used_gb"], 2) == 2.72
        assert m["gpu_util"] == 34.0

    def test_absent_gpu_still_yields_cpu_and_ram(self):
        """A blocked/absent GPU must not cost CPU admission — the desktop ran NVML-blocked for a
        day while its CPUs were perfectly schedulable. Since inv. 25c the driver's failure PROSE
        (which nvidia-smi writes to stdout) is what actually arrives, and it must be inert."""
        m = disp.parse_box_probe(
            "NPROC 20\nLOAD 3.72 3.36 2.81 4/1234 5678\nMEM 31000 18000\n"
            "GPU Failed to initialize NVML: GPU access blocked by the operating system")
        assert m["cores"] == 20 and m["ram_avail_gb"] > 0
        assert m["vram_total_gb"] is None

    def test_unusable_probe_carries_no_measurement(self):
        assert disp.parse_box_probe("???") is None
        assert disp.parse_box_probe("") is None

    def _inst(self, occupants=None, cap=None, at=1000.0):
        return {"measured": {**disp.parse_box_probe(self.PROBE), "at": at},
                "resource_cap": cap or {"cores": 10.0, "vram_gb": 6.0},
                "occupants": occupants or []}

    def test_headroom_is_measured_against_the_operator_allowance(self):
        # Day cap 10 cores; load 3.72 total (owner's processes included); 1.0 core reserve.
        hr = disp.box_headroom(self._inst(), self.S, 1000.0)
        assert round(hr["cores"], 2) == round(10.0 - 3.72 - 1.0, 2)
        # VRAM: min(8, 6.0) cap - 2.72 used - 1.0 reserve
        assert round(hr["vram_gb"], 2) == round(6.0 - 2.72265625 - 1.0, 2)

    def test_pending_occupants_are_debited_but_running_ones_are_not(self):
        running = [{"id": "r", "state": "running", "cores": 4.0, "vram_gb": 2.0, "ram_gb": 2.0}]
        claimed = [{"id": "c", "state": "claimed", "cores": 4.0, "vram_gb": 2.0, "ram_gb": 2.0}]
        base = disp.box_headroom(self._inst(), self.S, 1000.0)["cores"]
        # a running task is already inside the sampled load -> must NOT be charged again
        assert disp.box_headroom(self._inst(running), self.S, 1000.0)["cores"] == base
        # a claimed one is invisible to the sample -> must be charged
        assert disp.box_headroom(self._inst(claimed), self.S, 1000.0)["cores"] == base - 4.0

    def test_stale_measurement_abstains(self):
        stale = 1000.0 + self.S["headroom_max_stale_min"] * 60 + 1
        assert disp.box_headroom(self._inst(), self.S, stale) is None

    def _task(self, cores, vram, ram=1.0):
        return {"id": "t", "slots": 1, "est_minutes": 5, "priority": 50,
                "resource_hint": {"cores_per_lane": cores, "vram_per_lane_gb": vram,
                                  "ram_per_lane_gb": ram}}

    def test_gate_admits_within_headroom_and_refuses_beyond(self):
        inst = self._inst()
        assert disp._headroom_fits(self._task(2, 2.0), inst, self.S, 1000.0)     # fits
        assert not disp._headroom_fits(self._task(2, 4.0), inst, self.S, 1000.0)  # VRAM short
        assert not disp._headroom_fits(self._task(9, 1.0), inst, self.S, 1000.0)  # CPU short

    def test_ram_can_be_the_binding_axis(self):
        inst = self._inst()
        assert not disp._headroom_fits(self._task(1, 0.5, ram=99.0), inst, self.S, 1000.0)

    def test_gate_abstains_without_measurement_and_when_disabled(self):
        bare = {"measured": None, "resource_cap": None, "occupants": []}
        assert disp._headroom_fits(self._task(99, 99.0), bare, self.S, 1000.0)
        off = {**self.S, "headroom_enabled": False}
        assert disp._headroom_fits(self._task(99, 99.0), self._inst(), off, 1000.0)

    def test_unmeasured_gpu_does_not_block_cpu_admission(self):
        inst = {"measured": {**disp.parse_box_probe(
                    "NPROC 20\nLOAD 3.72 3.36 2.81 4/1234 5678\nMEM 31000 18000\n"
                    "GPU Failed to initialize NVML: GPU access blocked by the operating system"),
                    "at": 1000.0},
                "resource_cap": {"cores": 10.0, "vram_gb": 6.0}, "occupants": []}
        assert disp._headroom_fits(self._task(2, 99.0), inst, self.S, 1000.0)


class TestPreemptionSeesTheBudget:
    """Inv. 17a': a task blocked by the 18a BUDGET (not by slot count) must still be able to preempt.
    Regression: `shortfall = slots - free_slots` reads 0 on a box with a free slot, so preemption
    skipped it entirely and a high-priority probe held forever behind a low-priority long job."""

    # `preempt_enabled` explicitly ON: it ships FALSE (owner directive 2026-07-30), which makes
    # `place()` fall through to hold and would turn every assertion below — the negative ones
    # included — into a vacuous restatement of the switch rather than a test of invariant 17a'.
    # Same convention as `_mech_dispatcher` / `test_dispatcher.py`'s `_mech_settings`.
    S = {**disp.DEFAULT_SETTINGS, "current_rate": 0.0, "current_balance": 100.0,
         "deny_machine_ids": set(), "preempt_enabled": True}

    def _box(self, occupant_priority=50):
        # The live 2026-07-27 shape: a FREE SLOT, but the window's VRAM budget fully spent.
        return {"id": -2, "state": "live", "slots_total": 2, "dph_usd": 0.0, "source": "owned",
                "resource_cap": {"cores": 10.0, "vram_gb": 6.0}, "minutes_to_hard_cap": 999999,
                "idle_minutes": 0,
                "occupants": [{"id": "evo", "slots": 1, "state": "running", "est_minutes": 480,
                               "priority": occupant_priority, "running_minutes_ago": 30,
                               "cores": 4.0, "vram_gb": 6.0}]}

    def _probe(self, priority=90):
        return {"id": "probe", "slots": 1, "est_minutes": 5, "priority": priority,
                "resource_hint": {"cores_per_lane": 4, "vram_per_lane_gb": 6.0},
                "retries_used": 0, "max_retries": 3}

    def test_shortfall_reports_the_resource_axis_not_just_slots(self):
        box = self._box()
        assert disp._free_slots(box) == 1                      # a slot IS free ...
        s, c, v = disp._preempt_shortfall(self._probe(), box, self.S)
        assert s <= 0 and v > 0                                # ... yet VRAM is what blocks
        assert not disp._fits_now(self._probe(), box, self.S)

    def test_probe_preempts_the_low_priority_occupant(self):
        p = disp.place(self._probe(), [self._box()], [], [], self.S, 0)
        assert p.action == "preempt" and p.victims == ["evo"]

    def test_default_priority_task_does_not_preempt(self):
        # margin 30: 50 vs 50 is not enough. Must NOT evict.
        p = disp.place(self._probe(priority=50), [self._box()], [], [], self.S, 0)
        assert p.action != "preempt"

    def test_probe_does_not_evict_higher_priority_work(self):
        p = disp.place(self._probe(), [self._box(occupant_priority=95)], [], [], self.S, 0)
        assert p.action != "preempt"

    def test_box_without_a_budget_keeps_slots_only_behaviour(self):
        box = {**self._box(), "resource_cap": None, "slots_total": 1}   # full on slots
        # No cap -> both resource axes read 0, leaving exactly the original slots-only shortfall.
        assert disp._preempt_shortfall(self._probe(), box, self.S) == (1, 0.0, 0.0)
        p = disp.place(self._probe(), [box], [], [], self.S, 0)
        assert p.action == "preempt"                            # slot shortfall still drives it

    def test_displaced_long_victim_relocates_to_a_rented_box(self):
        """The other half of the ask: the evicted job must not just sit queued.

        States its own `max_instance_dph` (2026-07-31): this fixture's offer is a $0.20 24 GB
        RTX 4090, which the GPU-class ceiling (invariant 4f, default $0.08) correctly refuses for an
        ordinary task — so without an override this asserts the PRICE policy rather than the
        relocation path it is actually about. Stated here rather than quietly widened inside `S`,
        matching `_mech_settings`' convention of keeping each test honest about the world it assumes.
        A real task needing a card this dear declares `resource_hint.max_dph`; measured over 14 days,
        0 of 2531 hinted tasks are stranded by the default, so this is a fixture artifact only."""
        S = {**self.S, "max_instance_dph": 0.40}
        offer = {"id": 777, "machine_id": 1, "dph_total": 0.20, "gpu_ram_gb": 24,
                 "cpu_cores_effective": 16, "gpu_name": "RTX 4090", "reliability2": 0.99,
                 "num_gpus": 1}
        full = {**self._box(), "occupants": [
            {"id": "probe", "slots": 1, "state": "running", "est_minutes": 5, "priority": 90,
             "running_minutes_ago": 1, "cores": 4.0, "vram_gb": 6.0}]}
        victim = {"id": "evo", "slots": 1, "est_minutes": 480, "priority": 50,
                  "resource_hint": {"cores_per_lane": 4, "vram_per_lane_gb": 6.0},
                  "retries_used": 0, "max_retries": 3}
        assert disp.place(victim, [full], [offer], [], S, 0).action == "rent"
        # ...and the per-task override is the supported way a real big-card job gets there.
        big = {**victim, "resource_hint": {**victim["resource_hint"], "max_dph": 0.40}}
        assert disp.place(big, [full], [offer], [], self.S, 0).action == "rent"


class TestReapOverCapacity:
    """Inv. 20, the capacity scale-down MECHANICS — so every test here runs on `_mech_dispatcher`.

    The negative cases need the switch ON just as much as the positive ones: with the production
    default they would pass no matter how broken the shedding arithmetic got, because nothing is ever
    evicted. `test_dispatcher.py` owns the assertion that the switch itself tolerates over-cap."""

    def test_scales_down_excess_running(self, tmp_path):
        run = _RecordingRun()
        d = _mech_dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="live")
        for i in range(4):
            _task(d.conn, f"C{i}", -1, "running")
        d._capacity_slots = lambda inst, now=None: 2               # cap 2, 4 running -> evict 2
        d._reap_over_capacity()
        preempting = d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE instance_id=-1 AND state='preempting'").fetchone()[0]
        assert preempting == 2
        assert sum("/PREEMPT" in r for r in run.remotes()) == 2

    def test_no_scaledown_when_under_cap(self, tmp_path):
        d = _mech_dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="live")
        for i in range(2):
            _task(d.conn, f"U{i}", -1, "running")
        d._capacity_slots = lambda inst, now=None: 5               # 2 running <= cap 5 -> no-op
        d._reap_over_capacity()
        assert d.conn.execute("SELECT COUNT(*) FROM tasks WHERE state='preempting'").fetchone()[0] == 0

    def test_scales_down_on_budget_overflow_under_the_slot_cap(self, tmp_path):
        """Inv. 19 + 18a: the slot count alone says 'fine' while the real load is 3x the budget."""
        run = _RecordingRun()
        d = _mech_dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="live", slots=12)
        for i in range(3):                                          # 3 x 4 cores = 12 vs a 5-core cap
            _task(d.conn, f"F{i}", -1, "running", updated_ago_min=10 - i,
                  hint={"cores_per_lane": 4, "vram_per_lane_gb": 2.0})
        d._capacity_slots = lambda inst, now=None: 12               # slots NOT binding
        d._capacity_budget = lambda inst, now=None: {"cores": 5.0, "vram_gb": 99.0}
        d._reap_over_capacity()
        # Keeps the OLDEST that fits (4 <= 5), sheds the two newer ones.
        assert d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state='preempting'").fetchone()[0] == 2
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='F0'").fetchone())["state"] == "running"

    def test_no_scaledown_when_budget_holds(self, tmp_path):
        d = _mech_dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="live", slots=12)
        for i in range(2):
            _task(d.conn, f"G{i}", -1, "running", hint={"cores_per_lane": 4, "vram_per_lane_gb": 2.0})
        d._capacity_slots = lambda inst, now=None: 12
        d._capacity_budget = lambda inst, now=None: {"cores": 10.0, "vram_gb": 6.0}   # 8 <= 10, 4 <= 6
        d._reap_over_capacity()
        assert d.conn.execute("SELECT COUNT(*) FROM tasks WHERE state='preempting'").fetchone()[0] == 0


# --------------------------------------------------------------------------------------------
# CLI (inv. 8, 11, 13): DB-state flips. ssh_host=None -> _ssh_marker no-ops (no network).
# --------------------------------------------------------------------------------------------
class TestHoldQuiescesWithoutTouchingWork:
    """`hold` — the third situation: stop packing NEW work, leave RUNNING work strictly alone.

    Owner, 2026-08-08: "can you drain the desktop without interrupting current work — just don't add
    new work". `pause` SIGSTOPs the trainers and auto-escalates to a drain after 30 min; `drain`
    checkpoints them out and requeues them elsewhere. Both are right when you want the machine back
    NOW, and neither expresses "let it finish what it has, then be idle" — which is what you want
    before recreating a container or handing the desktop back for the evening.

    The value of this mode is entirely in what it does NOT do, so that is what these pin. Each test
    below corresponds to one mechanism that WOULD have touched the work if `hold` had reused an
    existing mode instead of adding one."""

    def test_a_held_box_takes_no_new_work(self, tmp_path):
        """The one thing it must DO. `place()` admits only `live`, so `paused` blocks packing."""
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hold", "at": reg.now_iso()})
        assert not [i for i in d._instances_view() if i["state"] == "live"]

    def test_the_drain_NEVER_evicts_a_held_box(self, tmp_path):
        """⛔ THE ONE THAT MATTERS. `_signal_drain` acts on paused owned boxes and `continue`s past
        any mode that is not 'hard' — so a held box's running task is never preempted. If this ever
        goes red, `hold` has silently become `drain` and someone's six live tasks requeue."""
        run = _RecordingRun()
        d = _dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hold", "at": reg.now_iso()})
        _task(d.conn, "H1", -1, "running")
        d._signal_drain()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='H1'").fetchone())["state"] == "running"
        assert not any("PREEMPT" in r for r in run.remotes()), run.remotes()

    def test_a_hold_never_auto_escalates(self, tmp_path):
        """A soft pause becomes a hard drain after `soft_pause_timeout_min` so a forgotten pause
        cannot strand work frozen. A HOLD is not forgetful — nothing is frozen and the box empties on
        its own — so the watchdog must leave it alone however long it sits."""
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hold", "at": _iso_ago(600)})   # 10h, way past the 30min limit
        d._reap_paused_soft_timeout()
        assert d._pause_meta(-1)["mode"] == "hold"

    def test_the_running_task_is_not_reaped_as_an_orphan(self, tmp_path):
        """`paused` is already live-ish to the orphan reaper (box-pause inv. 3). Pinned for `hold`
        because the whole point is that the occupant runs to completion on a non-live box."""
        d = _dispatcher(tmp_path)
        _owned_box(d.conn, iid=-1, state="paused")
        d._set_pause_meta(-1, {"mode": "hold", "at": reg.now_iso()})
        _task(d.conn, "H2", -1, "running")
        d._reap_orphaned_tasks()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='H2'").fetchone())["state"] == "running"

    def test_cli_hold_writes_the_mode_and_freezes_NOTHING(self, tmp_path):
        """No FREEZE marker is the difference from `pause`: the trainers keep running."""
        db, conn = TestCli()._db(tmp_path)
        run = _RecordingRun()
        bp._ssh_marker = lambda inst, cmd: run.remotes().append(cmd) or True
        assert bp.main(["hold", "--db", db]) == 0
        assert dict(conn.execute(
            "SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "paused"
        assert bp._pause_meta(conn, -1)["mode"] == "hold"
        assert not any("touch ~/spool/FREEZE" in r for r in run.remotes()), run.remotes()
        assert bp.main(["resume", "--db", db]) == 0
        assert bp._pause_meta(conn, -1) is None


class TestCli:
    def _db(self, tmp_path):
        db = str(tmp_path / "runs.sqlite")
        conn = reg.connect(db)
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (-1, None, "laptop-gpu", reg.now_iso(), "live", 0.0, None, 22, 6, FAR_CAP, "owned"))
        conn.commit()
        return db, conn

    def test_pause_then_resume(self, tmp_path):
        db, conn = self._db(tmp_path)
        assert bp.main(["pause", "--db", db]) == 0
        assert dict(conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "paused"
        assert bp._pause_meta(conn, -1)["mode"] == "soft"
        assert bp.main(["resume", "--db", db]) == 0
        assert dict(conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"
        assert bp._pause_meta(conn, -1) is None

    def test_drain_sets_hard(self, tmp_path):
        db, conn = self._db(tmp_path)
        assert bp.main(["drain", "--db", db]) == 0
        assert dict(conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "paused"
        assert bp._pause_meta(conn, -1)["mode"] == "hard"

    def test_status_is_readonly(self, tmp_path):
        db, conn = self._db(tmp_path)
        assert bp.main(["status", "--db", db]) == 0
        assert dict(conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"


# --------------------------------------------------------------------------------------------
# Worker freeze (inv. 9): a real process group is SIGSTOP'd and resumes.
# --------------------------------------------------------------------------------------------
def _pstate(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return data[data.rindex(")") + 2]  # single-char state after the "(comm) " field
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None


def _wait(pid, pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred(_pstate(pid)):
            return True
        time.sleep(0.02)
    return pred(_pstate(pid))


@pytest.mark.skipif(not Path("/proc").exists(), reason="freeze test reads /proc for process state")
class TestWorkerFreeze:
    def _spawn(self, w, tid):
        d = w.active_dir / tid
        d.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-c", "import time\nwhile True:\n time.sleep(0.05)"],
                                start_new_session=True)
        at = sw.ActiveTask(task_id=tid, dir=d, proc=proc)
        w.active[tid] = at
        return at

    def test_freeze_stops_then_resumes(self, tmp_path):
        w = sw.Worker(tmp_path / "spool")
        at = self._spawn(w, "W1")
        try:
            (w.spool / "FREEZE").touch()
            w.apply_freeze()
            assert _wait(at.proc.pid, lambda s: s == "T"), "SIGSTOP should stop the process group"
            assert at.stopped
            (w.spool / "FREEZE").unlink()
            w.apply_freeze()
            assert _wait(at.proc.pid, lambda s: s not in ("T", None)), "SIGCONT should resume it"
            assert not at.stopped
        finally:
            at.proc.kill()

    def test_preempt_marked_task_is_exempt_from_freeze(self, tmp_path):
        w = sw.Worker(tmp_path / "spool")
        at = self._spawn(w, "W2")
        try:
            (at.dir / "PREEMPT").touch()   # being preempted -> must keep running to checkpoint-exit
            (w.spool / "FREEZE").touch()
            w.apply_freeze()
            time.sleep(0.2)
            assert not at.stopped
            assert _pstate(at.proc.pid) != "T"
        finally:
            at.proc.kill()


# --------------------------------------------------------------------------------------------
# Orphan/zombie reaper (fleet hygiene): only a proc under active/ whose task is gone/terminal.
# --------------------------------------------------------------------------------------------
@pytest.mark.skipif(not Path("/proc").exists(), reason="reaper reads /proc")
class TestReapOrphans:
    def _spawn(self, repo: Path):
        repo.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen([sys.executable, "-c", "import time\nwhile True:\n time.sleep(0.05)"],
                                cwd=str(repo), start_new_session=True)

    def test_live_task_not_reaped(self, tmp_path):
        spool = tmp_path / "spool"
        p = self._spawn(spool / "active" / "abc" / "repo")
        try:
            assert ro.find_orphans(spool) == []  # dir present, no terminal marker -> NOT an orphan
        finally:
            p.kill()

    def test_terminal_task_is_orphan_and_reaped(self, tmp_path):
        spool = tmp_path / "spool"
        p = self._spawn(spool / "active" / "abc" / "repo")
        try:
            (spool / "active" / "abc" / "CANCELLED").write_text("")  # task finished, proc lingers
            assert [o["pid"] for o in ro.find_orphans(spool)] == [p.pid]
            # a live-tracked pid is never reaped, even with a terminal marker
            assert ro.find_orphans(spool, live_pids=frozenset({p.pid})) == []
            ro.reap(spool, grace=3, log=lambda m: None)
            assert _wait(p.pid, lambda s: s in (None, "Z")), "orphan should be killed"
        finally:
            if p.poll() is None:
                p.kill()

    def test_deleted_dir_is_orphan(self, tmp_path):
        spool = tmp_path / "spool"
        p = self._spawn(spool / "active" / "abc" / "repo")
        try:
            shutil.rmtree(spool / "active" / "abc")  # requeue/teardown removed the dir
            orph = ro.find_orphans(spool)
            assert [o["pid"] for o in orph] == [p.pid]
            assert "deleted" in orph[0]["reason"]
        finally:
            p.kill()


# --------------------------------------------------------------------------------------------
# Cost consolidation (task-dispatcher spec inv. 21/22): drain a paid box onto reclaimed free
# capacity via the shared graceful-preempt path; sample real box VRAM on a cadence (inv. 22).
# --------------------------------------------------------------------------------------------
def _paid_box(conn, iid=99, dph=0.076, slots=4, ssh_host="2.2.2.2", state="live"):
    conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
        "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (iid, 12345, "runq_paid", reg.now_iso(), state, dph, ssh_host, 34112, slots, FAR_CAP, "vast"))
    conn.commit()


def _running_task(conn, tid, iid, est_minutes):
    reg.insert_task(conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
                    entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                    arm_hash=tid, git_sha="d", slots=1, est_minutes=est_minutes, priority=50,
                    max_retries=10, state="running", instance_id=iid)
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (reg.now_iso(), tid))
    conn.commit()


class _NvidiaRun(_RecordingRun):
    """Recording run that answers the box-resource probe and succeeds on all other ssh/rsync calls,
    so `_measure_box_resources` gets a real payload to parse. The probe covers all four axes since
    inv. 23 (nproc / loadavg / free / nvidia-smi) and the container-true axes since inv. 25, and it
    is KEYED rather than positional, so the fake emits the whole keyed document."""

    def __init__(self, mib_by_host, cores=20, load1=1.0, ram_mb=(31000, 18000),
                 cgroup=True, usec=1_000_000_000):
        super().__init__()
        self.mib_by_host = mib_by_host
        self.cores, self.load1, self.ram_mb = cores, load1, ram_mb
        self.cgroup, self.usec = cgroup, usec

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "nvidia-smi" in joined:
            host = next((h for h in self.mib_by_host if any(h in c for c in cmd)), None)
            tot, used = self.mib_by_host.get(host, (0, 0))
            doc = (f"NPROC {self.cores}\nLOAD {self.load1} 1.0 1.0 1/1 1\n"
                   f"MEM {self.ram_mb[0]} {self.ram_mb[1]}\n"
                   f"GPU {tot}, {used}, 30, 12, NVIDIA GeForce RTX 3060\n")
            if self.cgroup:   # inv. 25: the container's own quota / CPU-time / memory
                doc += (f"CPUQ {self.cores / 2:.4f}\nCPUU {self.usec}\n"
                        f"MEMCG {2 * 1024 ** 3} {16 * 1024 ** 3}\nMEMANON {1024 ** 3}\n")
            return _FakeProc(0, doc)
        return _FakeProc(0, "")


class TestConsolidation:
    def test_measure_box_resources_parses_nvidia_smi(self, tmp_path):
        run = _NvidiaRun({"1.1.1.1": (12282, 1459)})
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        _owned_box(d.conn, iid=-1, state="live", ssh_host="1.1.1.1")
        d._measure_box_resources()
        res = d._box_res[-1]
        assert round(res["vram_total_gb"], 1) == 12.0 and round(res["vram_used_gb"], 2) == 1.42
        assert res["cores"] == 20 and res["load1"] == 1.0   # inv. 23: CPU/RAM measured too
        assert round(res["ram_avail_gb"], 1) == 17.6

    def test_measure_cadence_throttles(self, tmp_path):
        run = _NvidiaRun({"1.1.1.1": (12282, 1459)})
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        _owned_box(d.conn, iid=-1, state="live", ssh_host="1.1.1.1")
        d._measure_box_resources()
        n1 = sum("nvidia-smi" in " ".join(c) for c in run.calls)
        d._measure_box_resources()  # within the cadence window -> no re-probe
        n2 = sum("nvidia-smi" in " ".join(c) for c in run.calls)
        assert n1 == 1 and n2 == 1

    def test_consolidate_drains_paid_box_onto_owned(self, tmp_path):
        # Inv. 21 MECHANICS: a whole-box drain is a preempt per occupant, so `preempt_enabled` gates
        # it BEFORE `consolidate_enabled` is even consulted. `test_dispatcher.py` owns the assertion
        # that the production default suppresses this path.
        run = _RecordingRun()
        d = _mech_dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="live", slots=6, ssh_host="1.1.1.1")
        _paid_box(d.conn, iid=99, dph=0.076, slots=4, ssh_host="2.2.2.2")
        _running_task(d.conn, "bb1", -1, 360)
        _running_task(d.conn, "bb2", -1, 360)
        _running_task(d.conn, "tower", 99, 480)
        d._box_res = {-1: {"vram_total_gb": 12.0, "vram_used_gb": 1.5},
                      99: {"vram_total_gb": 11.0, "vram_used_gb": 5.0}}
        d._consolidate()
        assert reg.get_task(d.conn, "tower")["state"] == "preempting"
        assert reg.get_task(d.conn, "bb1")["state"] == "running"
        assert "touch ~/spool/active/tower/PREEMPT" in run.remotes()
        events = [dict(r) for r in d.conn.execute("SELECT event FROM events WHERE event='consolidate'")]
        assert len(events) == 1

    def test_consolidate_holds_when_owned_full(self, tmp_path):
        # Also on `_mech_dispatcher`: this is the NEGATIVE half of inv. 21c, and under the production
        # default it would pass on the global switch rather than on "nowhere cheaper to go" — which
        # is the only thing it is meant to be checking.
        run = _RecordingRun()
        d = _mech_dispatcher(tmp_path, run=run)
        _owned_box(d.conn, iid=-1, state="live", slots=2, ssh_host="1.1.1.1")
        _paid_box(d.conn, iid=99, dph=0.076, slots=4, ssh_host="2.2.2.2")
        _running_task(d.conn, "bb1", -1, 360)
        _running_task(d.conn, "bb2", -1, 360)   # owned full (2/2)
        _running_task(d.conn, "tower", 99, 480)
        d._box_res = {-1: {"vram_total_gb": 12.0, "vram_used_gb": 1.5},
                      99: {"vram_total_gb": 11.0, "vram_used_gb": 5.0}}
        d._consolidate()
        assert reg.get_task(d.conn, "tower")["state"] == "running"  # nowhere cheaper to go
        assert not any("PREEMPT" in r for r in run.remotes())


# --------------------------------------------------------------------------------------------
# 14a: `~/spool/FREEZE` is derived state, re-asserted on every measure probe.
# --------------------------------------------------------------------------------------------
class _FreezeReportingRun(_NvidiaRun):
    """`_NvidiaRun` whose box also says whether it held a FREEZE marker before the assert ran
    (`frz` = 1 / 0, or None for a box that does not report — an older reply shape)."""

    def __init__(self, frz):
        super().__init__({"1.1.1.1": (8192, 300)})
        self.frz = frz

    def __call__(self, cmd, **kwargs):
        proc = super().__call__(cmd, **kwargs)
        if "nvidia-smi" in " ".join(cmd) and self.frz is not None:
            proc.stdout = f"FRZ {self.frz}\n" + proc.stdout
        return proc

    def probes(self):
        return [c[-1] for c in self.calls if c and "nvidia-smi" in c[-1]]


class TestFreezeMarkerIsReasserted:
    """Invariant 14a. The marker is the registry's `paused`+`soft` projected onto the box, but it
    was written or removed ONCE, by the verb that changed the state, over a best-effort ssh whose
    failure `resume` discarded. When that call did not land, the registry said `live`, the packer
    filled the box, and the worker launched nothing — returning from its launch gate on FREEZE
    before it logs a reason, so there was no `launch_gate` line and no event at all."""

    def _box(self, tmp_path, frz, state="live", mode=None):
        run = _FreezeReportingRun(frz)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        _owned_box(d.conn, iid=-1, state=state, ssh_host="1.1.1.1")
        if mode:
            d._set_pause_meta(-1, {"mode": mode, "at": reg.now_iso()})
        return d, run

    def _reconciled(self, d):
        return [r["detail"] for r in d.conn.execute(
            "SELECT detail FROM events WHERE event='freeze_reconciled' ORDER BY seq")]

    def test_a_LIVE_box_still_holding_the_marker_is_unfrozen_and_it_is_logged(self, tmp_path):
        """The failure this exists for: a `resume` whose ssh never landed."""
        d, run = self._box(tmp_path, frz=1, state="live")
        d._measure_box_resources()
        (probe,) = run.probes()
        assert "rm -f ~/spool/FREEZE" in probe and "touch ~/spool/FREEZE" not in probe
        (line,) = self._reconciled(d)
        assert "removed" in line and "live" in line
        assert d._box_res[-1]["cores"] == 20, "the measurement must survive the extra report line"

    def test_a_SOFT_paused_box_missing_the_marker_gets_it_back(self, tmp_path):
        """The other direction: a `pause` issued while the box was asleep."""
        d, run = self._box(tmp_path, frz=0, state="paused", mode="soft")
        d._measure_box_resources()
        (probe,) = run.probes()
        assert "touch ~/spool/FREEZE" in probe and "rm -f ~/spool/FREEZE" not in probe
        (line,) = self._reconciled(d)
        assert "created" in line and "soft" in line

    @pytest.mark.parametrize("mode", ["hold", "hard"])
    def test_a_hold_and_a_hard_drain_assert_NO_marker(self, tmp_path, mode):
        """Only a SOFT pause freezes. A hold must leave trainers running, and a drain needs them
        running to reach their checkpoint."""
        d, run = self._box(tmp_path, frz=1, state="paused", mode=mode)
        d._measure_box_resources()
        (probe,) = run.probes()
        assert "rm -f ~/spool/FREEZE" in probe and "touch ~/spool/FREEZE" not in probe
        assert len(self._reconciled(d)) == 1

    @pytest.mark.parametrize("frz,state,mode", [(0, "live", None), (1, "paused", "soft"),
                                                (0, "paused", "hold")])
    def test_a_box_already_in_its_desired_state_logs_nothing(self, tmp_path, frz, state, mode):
        d, _ = self._box(tmp_path, frz=frz, state=state, mode=mode)
        d._measure_box_resources()
        assert self._reconciled(d) == []

    def test_a_box_that_does_not_report_concludes_nothing(self, tmp_path):
        """Silence is not a drift: an old reply shape must not be read as 'marker absent'."""
        d, run = self._box(tmp_path, frz=None, state="paused", mode="soft")
        d._measure_box_resources()
        assert self._reconciled(d) == [] and "touch ~/spool/FREEZE" in run.probes()[0]

    def test_the_assert_rides_the_probes_own_ssh_call(self, tmp_path):
        """No extra connection per box: one ssh carries the report, the assert and the probe."""
        d, run = self._box(tmp_path, frz=0, state="live")
        d._measure_box_resources()
        assert len(run.calls) == 1
        probe = run.probes()[0]
        assert probe.index("FRZ") < probe.index("FREEZE;") < probe.index("NPROC")

    def test_the_report_line_is_read_and_nothing_else_is(self):
        assert disp.parse_freeze_report("FRZ 1\nNPROC 20\n") is True
        assert disp.parse_freeze_report("NPROC 20\nFRZ 0\n") is False
        for junk in ("", "NPROC 20\n", "FRZ\n", "FRZ 2\n", "FRZ 1 extra\n", "FROZEN 1\n"):
            assert disp.parse_freeze_report(junk) is None, junk
        assert disp.parse_box_probe("FRZ 1\nNPROC 4\nLOAD 0.5 0 0 1/1 1\nMEM 100 50\n")["cores"] == 4
