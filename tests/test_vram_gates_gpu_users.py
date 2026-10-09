"""Task-dispatcher invariants 26m / 26n / 8c — the card's state gates only tasks that use the GPU.

THE INCIDENT (2026-10-02 12:50 PM -> 10-03 8:25 PM local time). ~9.4 GB of VRAM that no fleet task had
allocated sat on `laptop-gpu` (12 GB card, day allowance 0.85 x 12 = 10.2 GB). Measured VRAM
headroom was 10.2 - 9.4 - 1.0 = -0.2 GB, every task was charged the 0.6 GB settings lane whether it
touched the card or not, and a 32-core box refused a queue of CPU-only work for every daytime hour.
The learner made it worse: whole card / running taught one CPU-only group 5.94 GB per lane.

Three pieces, one rule each:
  26m  a task that does not use the GPU is charged no VRAM, so no VRAM gate applies to it;
  26n  the learner charges a lane only for VRAM above its box's last idle reading
       (pure half in tests/test_res_defaults.py);
  8c   the box skips its two whole-card launch rules for a task the coordinator marked.
"""

import importlib.util
import json
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
res = _load("res_defaults", "fleet/res_defaults.py")
sw = _load("spool_worker", "fleet/spool_worker.py")

FAR_CAP = "2099-01-01T00:00:00Z"
DAY = {"cores": 16.0, "vram_gb": 10.2}          # laptop-gpu's day window: 0.5 x 32, 0.85 x 12
CPU_ONLY = None                                  # what the stranded tasks declared: nothing
ZERO = {"vram_per_lane_gb": 0.0, "cores_per_lane": 1}      # configs/probes/TEMPLATE.probe.json
GPU = {"requires_gpu": True}
GPU_2GB = {"vram_per_lane_gb": 2.0, "cores_per_lane": 1}


def _settings():
    s = dict(disp.DEFAULT_SETTINGS)
    s.update(cores_per_lane=1, vram_per_lane_gb=0.6, ram_per_lane_gb=2.0)   # the live settings rows
    return s


def _task(hint, slots=1, priority=50):
    return {"id": "t", "slots": slots, "est_minutes": 60, "priority": priority, "resource_hint": hint}


def _occupant(hint, state="running", priority=50, tid="occ"):
    cores, vram = disp.task_footprint(hint, 1, _settings())
    return {"id": tid, "slots": 1, "state": state, "est_minutes": 60, "priority": priority,
            "running_minutes_ago": 10.0, "cores": cores, "vram_gb": vram, "ram_gb": 2.0}


def _laptop(now, vram_used=9.4, occupants=()):
    """laptop-gpu by day. `vram_used` is the WHOLE card, as `nvidia-smi` reports it."""
    return {"id": -1, "state": "live", "slots_total": 16, "resource_cap": dict(DAY),
            "minutes_to_hard_cap": 1e9, "occupants": list(occupants),
            "measured": {"at": now, "cores": 32, "load1": 0.2, "ram_avail_gb": 38.0,
                         "vram_total_gb": 12.0, "vram_used_gb": vram_used}}


# ------------------------------------------------------------------------------- 26m, pure
class TestCardStateDoesNotGateCpuOnlyTasks:
    def test_THE_INCIDENT_a_full_card_admits_cpu_only_work_and_still_refuses_gpu_work(self):
        s, now = _settings(), time.time()
        box = _laptop(now)
        assert disp.box_headroom(box, s, now)["vram_gb"] == pytest.approx(-0.2)
        assert disp._fits_now(_task(CPU_ONLY), box, s, now)
        assert disp._fits_now(_task(ZERO), box, s, now)
        # The control: the gate still works for what it is for.
        assert not disp._fits_now(_task(GPU), box, s, now)
        assert not disp._fits_now(_task(GPU_2GB), box, s, now)
        assert disp.admission_refusals(_task(CPU_ONLY), box, s, now) == []
        (why,) = disp.admission_refusals(_task(GPU_2GB), box, s, now)
        assert why.startswith("headroom (23)") and "vram -0.2 GB" in why

    def test_the_refusal_was_the_card_a_freed_card_admits_the_gpu_task(self):
        s, now = _settings(), time.time()
        assert disp._fits_now(_task(GPU_2GB), _laptop(now, vram_used=0.5), s, now)

    def test_it_fills_the_box_not_just_one_lane(self):
        """Before, the first CPU-only task was refused. After, cores bind — as they should."""
        s, now = _settings(), time.time()
        box, n = _laptop(now), 0
        while n < 32 and disp._fits_now(_task(CPU_ONLY), box, s, now):
            box["occupants"].append(_occupant(CPU_ONLY, state="claimed", tid=f"o{n}"))
            n += 1
        assert n == 14          # 16-core day allowance - 0.2 load - 1.0 reserve, at 1 core each

    @pytest.mark.parametrize("hint,gb", [
        (None, 0.0), ({}, 0.0), ({"cores_per_lane": 4}, 0.0), (ZERO, 0.0),
        ({"vram_per_lane_gb": None}, 0.0), ({"vram_per_lane_gb": "2"}, 0.0),
        (GPU, 0.6), ({"requires_gpu": True, "vram_per_lane_gb": 0}, 0.6),
        ({"requires_gpu": True, "vram_per_lane_gb": 14.0}, 14.0), (GPU_2GB, 2.0)])
    def test_lane_vram_gb_is_the_one_definition(self, hint, gb):
        s = _settings()
        assert disp.lane_vram_gb(hint, s) == gb
        assert disp.task_footprint(hint, 3, s)[1] == pytest.approx(3 * gb)

    def test_a_vram_budget_spent_by_gpu_occupants_still_admits_cpu_only_work(self):
        s = _settings()
        full = {"resource_cap": dict(DAY), "occupants": [
            _occupant({"requires_gpu": True, "vram_per_lane_gb": 10.0})]}
        assert disp._budget_fits(_task(CPU_ONLY), full, s)
        assert not disp._budget_fits(_task(GPU_2GB), full, s)
        # ... and one already OVER it (the window shrank under running GPU work).
        over = {"resource_cap": dict(DAY), "occupants": [
            _occupant({"requires_gpu": True, "vram_per_lane_gb": 11.5})]}
        assert disp._budget_fits(_task(CPU_ONLY), over, s)

    def test_cpu_only_occupants_spend_none_of_the_vram_budget(self):
        """Ten CPU-only lanes used to hold 6 GB of a 10.2 GB window on paper. A 6 GB GPU task
        beside them was refused against a card nobody was using."""
        s = _settings()
        box = {"resource_cap": dict(DAY), "occupants": [_occupant(CPU_ONLY, tid=f"o{i}")
                                                        for i in range(10)]}
        assert sum(o["vram_gb"] for o in box["occupants"]) == 0.0
        assert disp._budget_fits(_task({"requires_gpu": True, "vram_per_lane_gb": 6.0}), box, s)

    def test_a_cpu_only_task_in_flight_reserves_no_vram(self):
        box = {"occupants": [_occupant(CPU_ONLY, state="shipped"), _occupant(GPU_2GB, state="claimed")]}
        assert disp._pending_footprint(box, _settings())[2] == 2.0

    def test_the_cores_and_ram_gates_still_apply_to_cpu_only_work(self):
        s, now = _settings(), time.time()
        busy = _laptop(now)
        busy["measured"]["load1"] = 15.5                   # 16 - 15.5 - 1.0 reserve < 1 core
        assert not disp._fits_now(_task(CPU_ONLY), busy, s, now)
        tight = _laptop(now)
        tight["measured"]["ram_avail_gb"] = 3.0            # 3.0 - 2.0 reserve < 2.0 GB lane
        assert not disp._fits_now(_task(CPU_ONLY), tight, s, now)

    def test_a_cpu_only_task_needs_no_vram_freed_to_preempt_its_way_in(self):
        s = _settings()
        over = {"slots_total": 4, "resource_cap": dict(DAY), "occupants": [
            _occupant({"requires_gpu": True, "vram_per_lane_gb": 11.5})]}
        assert disp._preempt_shortfall(_task(CPU_ONLY), over, s)[2] == 0.0
        assert disp._preempt_shortfall(_task(GPU_2GB), over, s)[2] == pytest.approx(3.3)

    def test_rent_sizing_leaves_the_card_out_for_a_task_that_never_touches_it(self):
        """Invariant 27: sizing and admission must count the same axes. And a declared 0 — which
        the probe template recommends — raised ZeroDivisionError here on the rent path."""
        s = _settings()
        small_card = {"gpu_ram_gb": 2.0, "cpu_cores_effective": 8.0, "ram_gb": 64.0}
        assert disp.slots_for_offer(small_card, ZERO, s) == 8
        assert disp.slots_for_offer(small_card, CPU_ONLY, s) == 8
        assert disp.lane_capacity(small_card, ZERO, s) == 8
        assert disp.slots_for_offer(small_card, GPU, s) == 3            # 2.0 / 0.6: unchanged
        assert disp.slots_for_offer(small_card, GPU_2GB, s) == 1
        assert disp.slots_for_offer({**small_card, "gpu_ram_gb": 1.0}, GPU_2GB, s) == 0


# ---------------------------------------------------------------------- through the real dispatcher
class _Proc:
    returncode, stdout, stderr = 0, "", ""


class _Run:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return _Proc()


def _dispatcher(tmp_path):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Run(), vastai_run=_Run())
    d.settings.update(cores_per_lane=1, vram_per_lane_gb=0.6, ram_per_lane_gb=2.0)
    d._offers = lambda: []
    return d


def _box(d, iid=-1, label="lap", vram_used=9.4):
    """An owned 12 GB GPU box in its day window, measured a moment ago."""
    d.conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
        "ssh_host, ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (iid, None, label, reg.now_iso(), "live", 0.0, "RTX 4080 Laptop GPU", "10.0.0.9", 2222, 16,
         FAR_CAP, "owned"))
    d.conn.commit()
    d._capacity_slots = lambda inst, now=None: 16
    d._capacity_budget = lambda inst, now=None: dict(DAY)
    d._box_res[iid] = {"at": time.time(), "cores": 32, "load1": 0.2, "ram_total_gb": 39.0,
                       "ram_avail_gb": 38.0, "vram_total_gb": 12.0, "vram_used_gb": vram_used}


def _row(d, tid, state="queued", iid=None, hint=None, grp="g", ago_min=0):
    at = (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra = {"resource_hint_json": json.dumps(hint)} if hint is not None else {}
    reg.insert_task(d.conn, id=tid, created_at=at, created_by="t", grp=grp, name=tid,
                    entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                    arm_hash=tid, git_sha="d", slots=1, est_minutes=5, priority=50, max_retries=3,
                    state=state, instance_id=iid, **extra)
    d.conn.commit()


def _state(d, tid):
    return dict(d.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def _measured(d, iid, minutes_ago, vram, running=0, open_n=None, grp=None, ep="smoke"):
    """One `box_measured` event as the daemon writes it (the JSON tail is what the learner reads)."""
    t = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tail = {"v": 1, "cores": 32, "load1": float(running), "cpu_used_cores": float(running),
            "mem_used_gb": 1.5 * running, "vram_total_gb": 12.0, "vram_used_gb": vram,
            "running": running, "open": running if open_n is None else open_n,
            "ep": ep if running else None, "grp": grp if running else None}
    d.conn.execute("INSERT INTO events(t,task_id,instance_id,event,detail) VALUES (?,?,?,?,?)",
                   (t, None, iid, "box_measured", "load | " + json.dumps(tail, separators=(",", ":"))))
    d.conn.commit()


class TestThroughTheRealDispatcher:
    def test_THE_INCIDENT_the_queue_boards_the_box_with_the_full_card(self, tmp_path):
        d = _dispatcher(tmp_path)
        _box(d)
        _row(d, "CPU0", ago_min=3)
        _row(d, "CPU1", hint=ZERO, ago_min=2)
        _row(d, "GPU", hint={"requires_gpu": True, "vram_per_lane_gb": 3.0, "cores_per_lane": 1},
             ago_min=1)
        d._place_queue()
        assert [_state(d, t)["state"] for t in ("CPU0", "CPU1", "GPU")] == ["claimed", "claimed", "queued"]

    def test_the_GPU_task_boards_once_the_card_is_free(self, tmp_path):
        """The control for the test above: its `queued` was the card, not something else."""
        d = _dispatcher(tmp_path)
        _box(d, vram_used=0.5)
        _row(d, "GPU", hint={"requires_gpu": True, "vram_per_lane_gb": 3.0, "cores_per_lane": 1})
        d._place_queue()
        assert _state(d, "GPU")["state"] == "claimed"

    def test_the_occupant_view_charges_a_cpu_only_task_no_vram(self, tmp_path):
        d = _dispatcher(tmp_path)
        _box(d)
        _row(d, "CPU", state="running", iid=-1)
        _row(d, "GPU", state="running", iid=-1, hint={"requires_gpu": True, "vram_per_lane_gb": 3.0})
        (view,) = d._instances_view()
        assert sorted(o["vram_gb"] for o in view["occupants"]) == [0.0, 3.0]

    def test_the_task_ships_with_the_marker_exactly_when_it_is_charged_no_vram(self, tmp_path):
        """Invariant 8c: the COORDINATOR decides who uses the GPU; the box only reads the answer."""
        d = _dispatcher(tmp_path)
        _box(d)
        _row(d, "CPU", state="claimed", iid=-1)
        _row(d, "ZERO", state="claimed", iid=-1, hint=ZERO)
        _row(d, "GPU", state="claimed", iid=-1, hint=GPU)
        _row(d, "VRAM", state="claimed", iid=-1, hint=GPU_2GB)
        env = lambda tid: d._build_task_json(_state(d, tid))["env"]            # noqa: E731
        assert env("CPU") == {disp.NO_GPU_ENV: "1"} and env("ZERO") == {disp.NO_GPU_ENV: "1"}
        assert env("GPU") == {} and env("VRAM") == {}
        assert sw.validate_task_json(d._build_task_json(_state(d, "CPU"))) is None
        assert sw.NO_GPU_ENV == disp.NO_GPU_ENV

    def test_THE_INCIDENT_the_learner_does_not_charge_the_lane_for_the_owners_memory(self, tmp_path):
        """26n on the real event log: an idle reading of 9.5 GB, then one CPU-only lane running
        beside it. Whole card / running was the 8.0 GB clamp — which fits no owned GPU by day."""
        d = _dispatcher(tmp_path)
        _box(d)
        _measured(d, -1, minutes_ago=200, vram=9.5)
        for i in range(20):
            _measured(d, -1, minutes_ago=190 - 5 * i, vram=9.5, running=1, grp="cpu_grp")
        learned = d._learned_footprints()
        assert learned[("smoke", "cpu_grp")]["vram_per_lane_gb"] == res.FLOOR_VRAM
        _row(d, "NEXT", grp="cpu_grp")
        hint = d._effective_hint(_state(d, "NEXT"))
        assert disp.lane_vram_gb(hint, d.settings) == 0.0
        assert hint["cores_per_lane"] == learned[("smoke", "cpu_grp")]["cores_per_lane"]
        d._place_queue()
        assert _state(d, "NEXT")["state"] == "claimed"

    def test_POSITIVE_CONTROL_a_group_measured_using_the_card_is_gated_though_it_declared_nothing(
            self, tmp_path):
        """The safety net must be able to fire: 3 GB per lane above a 0.5 GB idle reading."""
        d = _dispatcher(tmp_path)
        _box(d)                                                   # card full: -0.2 GB headroom
        _measured(d, -1, minutes_ago=200, vram=0.5)
        for i in range(20):
            _measured(d, -1, minutes_ago=190 - 5 * i, vram=3.5, running=1, grp="quiet_gpu_grp")
        _row(d, "NEXT", grp="quiet_gpu_grp")
        hint = d._effective_hint(_state(d, "NEXT"))
        assert hint["vram_per_lane_gb"] == 3.75                   # 3.0 x 1.25
        assert d._build_task_json(_state(d, "NEXT"))["env"] == {}
        d._place_queue()
        assert _state(d, "NEXT")["state"] == "queued"

    def test_a_box_never_seen_idle_teaches_nothing_about_vram(self, tmp_path):
        d = _dispatcher(tmp_path)
        _box(d)
        for i in range(20):
            _measured(d, -1, minutes_ago=190 - 5 * i, vram=9.5, running=1, grp="g2")
        fp = d._learned_footprints()[("smoke", "g2")]
        assert "vram_per_lane_gb" not in fp and "cores_per_lane" in fp


# ------------------------------------------------------------------------------- 8c, the box
FULL_CARD = {"gpu_util": 100.0, "vram_free_gb": 0.0, "vram_total_gb": 12.0, "proc_vram_gb": {},
             "load1": 1.0, "cores": 16}


class _Live:
    pid = 4321

    def poll(self):
        return None


def _worker(tmp_path, monkeypatch, hw, waiting, n_live=2):
    """A worker with `n_live` trainers running and `waiting` = [(task_id, env), ...] prepared, on a
    box measuring `hw`, well past its settle. `should_launch` is the real one."""
    w = sw.Worker(tmp_path / "spool")
    w.active = {f"busy{i}": sw.ActiveTask(f"busy{i}", tmp_path / f"b{i}", proc=_Live())
                for i in range(n_live)}
    for tid, env in waiting:
        d = tmp_path / "spool" / "active" / tid
        (d / "repo").mkdir(parents=True)
        spec = {"task_id": tid, "grp": "g", "name": tid, "argv": ["python", "x.py"], "env": dict(env),
                "est_minutes": 1, "git_sha": "d", "pip_extras": [], "resume_from": None}
        w.active[tid] = sw.ActiveTask(tid, d, spec=spec)
    monkeypatch.setattr(sw.subprocess, "Popen", lambda argv, **kw: _Live())
    monkeypatch.setattr(sw, "sample_hw", lambda: dict(hw))
    w._last_launch = sw.time.time() - 99 * 60
    return w


def _launched(w):
    return sorted(tid for tid, at in w.active.items() if not tid.startswith("busy") and at.proc)


def _holds(w):
    if not w.worker_log.exists():
        return []
    rows = [json.loads(line) for line in w.worker_log.read_text().splitlines()]
    return [(r["task_id"], r["detail"]) for r in rows if r["event"] == "launch_gate"]


MARK = {sw.NO_GPU_ENV: "1"}


class TestNoGpuTasksSkipTheGpuRules:
    def test_a_full_card_holds_an_unmarked_task(self, tmp_path, monkeypatch):
        """The control: this is the hold the marker lifts."""
        w = _worker(tmp_path, monkeypatch, FULL_CARD, [("plain", {})])
        w.launch_ready()
        assert _launched(w) == []
        ((tid, why),) = _holds(w)
        assert tid == "plain" and why.startswith("gpu_util: 100.00% >= ceiling 90.0%")

    def test_a_full_card_does_not_hold_a_marked_task(self, tmp_path, monkeypatch):
        w = _worker(tmp_path, monkeypatch, FULL_CARD, [("cpu", MARK)])
        w.launch_ready()
        assert _launched(w) == ["cpu"] and _holds(w) == []

    def test_the_free_vram_rule_alone_is_skipped_too(self, tmp_path, monkeypatch):
        """The incident's shape: the card idle but 9.7 of 12 GB taken (under 20% free)."""
        hw = {**FULL_CARD, "gpu_util": 3.0, "vram_free_gb": 2.3}
        held = _worker(tmp_path / "a", monkeypatch, hw, [("plain", {})])
        held.launch_ready()
        assert _launched(held) == [] and _holds(held)[0][1].startswith("vram: free 2.30GB < need 2.40GB")
        w = _worker(tmp_path / "b", monkeypatch, hw, [("cpu", MARK)])
        w.launch_ready()
        assert _launched(w) == ["cpu"]

    def test_a_marked_task_is_not_held_behind_a_gpu_task_the_card_is_holding(self, tmp_path, monkeypatch):
        w = _worker(tmp_path, monkeypatch, FULL_CARD, [("gpu", {}), ("cpu", MARK)])
        w.launch_ready()
        assert _launched(w) == ["cpu"]
        assert w.active["gpu"].proc is None

    def test_one_launch_per_call_still(self, tmp_path, monkeypatch):
        w = _worker(tmp_path, monkeypatch, FULL_CARD, [("cpu0", MARK), ("cpu1", MARK)])
        w.launch_ready()
        assert _launched(w) == ["cpu0"]

    def test_cpu_load_still_holds_a_marked_task(self, tmp_path, monkeypatch):
        w = _worker(tmp_path, monkeypatch, {**FULL_CARD, "load1": 15.5}, [("cpu", MARK)])
        w.launch_ready()
        assert _launched(w) == []
        assert _holds(w) == [("cpu", "cpu_load: load1 15.50 >= cores-1 15")]

    def test_the_settle_still_holds_a_marked_task(self, tmp_path, monkeypatch):
        w = _worker(tmp_path, monkeypatch, {**FULL_CARD, "load1": 12.0}, [("cpu", MARK)])
        w._last_launch = sw.time.time() - 60                     # 1 min ago, box not idle
        w.launch_ready()
        assert _launched(w) == [] and _holds(w)[0][1].startswith("settling: 1.0min")

    def test_only_the_exact_marker_counts(self, tmp_path, monkeypatch):
        for i, env in enumerate(({sw.NO_GPU_ENV: "0"}, {sw.NO_GPU_ENV: ""}, {"OTHER": "1"})):
            w = _worker(tmp_path / str(i), monkeypatch, FULL_CARD, [("t", env)])
            w.launch_ready()
            assert _launched(w) == [], env
