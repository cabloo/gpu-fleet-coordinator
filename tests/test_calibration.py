"""Coordinator calibration report (docs/specs/calibration.spec.md).

Pure-helper unit tests + a golden end-to-end fixture seeded into a temp registry DB. The fixture is
hand-verifiable (round timings) and its expected report is committed at
tests/fixtures/calibration/basic.report.json (regenerate with REGEN=1 after an intentional change).
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "calibration"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry_db", "fleet/registry_db.py")
cal = _load("calibration", "fleet/calibration.py")

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _ev(event, t, task_id=None, instance_id=None):
    return {"seq": _ev.n, "t": t, "task_id": task_id, "instance_id": instance_id,
            "event": event, "detail": ""}


def _task(**over):
    f = dict(id="T", grp="g", entrypoint="train_x", slots=1, est_minutes=100, state="done",
             created_at="2026-07-13T00:00:00Z")
    f.update(over)
    return f


# --------------------------------------------------------------------------- pure: reconstruct

def test_reconstruct_single_segment_over_estimate():
    t = _task(id="T1", est_minutes=100)
    evs = [_ev("start", "2026-07-13T00:00:00Z", "T1", 1),
           _ev("done", "2026-07-13T01:00:00Z", "T1", 1)]
    ta = cal.reconstruct_task_actuals(t, evs, NOW)
    assert ta.active_minutes == 60.0
    assert ta.ratio == pytest.approx(0.6)
    assert ta.degenerate is False
    assert [s.instance_id for s in ta.segments] == [1]


def test_reconstruct_two_segments_preempt_requeue_summed():
    """Invariant 2: a preempted+requeued task accrues one segment per run, summed (its compute),
    NOT first-start-to-final wall clock."""
    t = _task(id="T4", est_minutes=60, grp="gB")
    evs = [_ev("start", "2026-07-13T00:00:00Z", "T4", 2),
           _ev("preempt_intent", "2026-07-13T00:30:00Z", "T4", 2),
           _ev("start", "2026-07-13T01:00:00Z", "T4", 2),
           _ev("done", "2026-07-13T01:30:00Z", "T4", 2)]
    ta = cal.reconstruct_task_actuals(t, evs, NOW)
    assert len(ta.segments) == 2
    assert ta.active_minutes == 60.0          # 30 + 30, NOT the 90-min wall clock
    assert ta.ratio == pytest.approx(1.0)


def test_reconstruct_degenerate_below_floor():
    t = _task(id="T3", est_minutes=50, entrypoint="train_y")
    evs = [_ev("start", "2026-07-13T00:00:00Z", "T3", 2),
           _ev("done", "2026-07-13T00:01:00Z", "T3", 2)]
    ta = cal.reconstruct_task_actuals(t, evs, NOW)
    assert ta.active_minutes == 1.0
    assert ta.degenerate is True              # 1 min < 2.0 floor

def test_reconstruct_stalled_and_lost_are_not_terminators():
    """`stalled`/`lost` are markers before the infra_failed transition — the infra_failed event is
    the true segment close, so a start->stalled->infra_failed run is ONE 40-min segment."""
    t = _task(id="T6", est_minutes=100, state="task_failed")
    evs = [_ev("start", "2026-07-13T00:00:00Z", "T6", 2),
           _ev("stalled", "2026-07-13T00:40:00Z", "T6", 2),
           _ev("infra_failed", "2026-07-13T00:40:00Z", "T6", 2)]
    ta = cal.reconstruct_task_actuals(t, evs, NOW)
    assert len(ta.segments) == 1
    assert ta.active_minutes == 40.0


def test_reconstruct_running_task_closes_at_now_only_when_running():
    t = _task(id="TR", state="running")
    evs = [_ev("start", "2026-07-13T11:00:00Z", "TR", 1)]
    ta = cal.reconstruct_task_actuals(t, evs, NOW)
    assert ta.active_minutes == 60.0          # 11:00 -> NOW 12:00
    # a claimed/shipped task with a dangling start but not `running` gets no open segment
    t2 = _task(id="TX", state="claimed")
    ta2 = cal.reconstruct_task_actuals(t2, [_ev("start", "2026-07-13T11:00:00Z", "TX", 1)], NOW)
    assert ta2.active_minutes == 0.0


# --------------------------------------------------------------------------- pure: attribute_costs

def test_attribute_costs_slot_minute_split_and_idle_box():
    insts = [
        {"id": 1, "cost_usd": 0.20}, {"id": 3, "cost_usd": 0.05},  # inst3 runs no tasks
    ]
    Seg = cal.Segment
    d = datetime(2026, 7, 13, tzinfo=timezone.utc)
    t1 = cal.TaskActual("T1", "gA", "e", 1, 100, "done", 60.0, [Seg(1, d, d, 60.0, 1)])
    t2 = cal.TaskActual("T2", "gA", "e", 1, 30, "done", 60.0, [Seg(1, d, d, 60.0, 1)])
    cb = cal.attribute_costs(insts, [t1, t2])
    assert cb.per_task_usd["T1"] == pytest.approx(0.10)   # 60/(60+60) * 0.20
    assert cb.per_task_usd["T2"] == pytest.approx(0.10)
    assert cb.per_instance_idle_usd[1] == pytest.approx(0.0)
    assert cb.per_instance_idle_usd[3] == pytest.approx(0.05)  # rented, zero tasks -> all idle
    assert cb.unattributed_idle_usd == pytest.approx(0.05)


# --------------------------------------------------------------------------- pure: occupancy

def test_occupancy_full_and_live_excluded():
    inst = {"created_at": "2026-07-13T00:00:00Z", "destroyed_at": "2026-07-13T02:00:00Z",
            "slots_total": 1}
    d0 = cal.parse_ts("2026-07-13T00:00:00Z"); d1 = cal.parse_ts("2026-07-13T01:00:00Z")
    d2 = cal.parse_ts("2026-07-13T02:00:00Z")
    assert cal.occupancy(inst, [(d0, d1, 1), (d1, d2, 1)]) == pytest.approx(1.0)
    # a 2-slot box with 101 slot-min over a 120-min life -> 101/240
    inst2 = dict(inst, slots_total=2)
    assert cal.occupancy(inst2, [(d0, d2, 1)]) == pytest.approx(120 / 240)  # 1 slot full life
    # live box (no destroyed_at) -> None (excluded)
    assert cal.occupancy({"destroyed_at": None, "created_at": "x", "slots_total": 1}, []) is None


# --------------------------------------------------------------------------- pure: section D

def test_backlog_slots_at_half_open_containment():
    """Invariant 11: sum slots over windows [enter, exit) containing the instant."""
    W = cal.BacklogWindow
    d = cal.parse_ts
    wins = [
        W(d("2026-07-13T00:00:00Z"), d("2026-07-13T02:00:00Z"), 1),  # spans the probe
        W(d("2026-07-13T00:30:00Z"), d("2026-07-13T00:30:00Z"), 4),  # empty window, never counts
        W(d("2026-07-13T01:00:00Z"), d("2026-07-13T03:00:00Z"), 2),  # opens at the probe (inclusive)
        W(d("2026-07-13T02:00:00Z"), d("2026-07-13T04:00:00Z"), 8),  # opens after -> excluded
    ]
    probe = d("2026-07-13T01:00:00Z")
    assert cal.backlog_slots_at(probe, wins) == 3            # 1 (spanning) + 2 (opens at probe)
    # exit is exclusive: at 02:00 the first window (exit 02:00) drops out, the third still spans it,
    # and the fourth opens → 2 (01:00–03:00) + 8 (02:00–04:00) = 10
    assert cal.backlog_slots_at(d("2026-07-13T02:00:00Z"), wins) == 10


def test_reconstruct_sets_backlog_window():
    """Invariant 11: exit = first start when it ran; = now when never-ran and still queued."""
    ran = cal.reconstruct_task_actuals(
        _task(id="R", created_at="2026-07-13T00:00:00Z", state="done"),
        [_ev("start", "2026-07-13T00:20:00Z", "R", 1),
         _ev("done", "2026-07-13T01:00:00Z", "R", 1)], NOW)
    assert ran.created_dt == cal.parse_ts("2026-07-13T00:00:00Z")
    assert ran.exit_backlog_dt == cal.parse_ts("2026-07-13T00:20:00Z")   # first start
    queued = cal.reconstruct_task_actuals(
        _task(id="Q", created_at="2026-07-13T00:00:00Z", state="claimed"), [], NOW)
    assert queued.exit_backlog_dt == NOW                                  # never ran, not terminal
    failed = cal.reconstruct_task_actuals(
        _task(id="F", created_at="2026-07-13T00:00:00Z", state="infra_failed"),
        [_ev("infra_failed", "2026-07-13T00:05:00Z", "F", None)], NOW)
    assert failed.exit_backlog_dt == cal.parse_ts("2026-07-13T00:05:00Z")  # last event, never ran


def _seed_packing(path):
    """Four deliberately-distinct boxes for section D (invariants 13–15). Backlog is evaluated at
    each box's OWN created_at, so rent times are staggered to give each box its intended backlog:
    a fat 8-slot queue exists 00:00–02:00 (tasks B1/B2 queued long), a thin queue exists at 03:00."""
    conn = reg.connect(str(path))

    def inst(i, slots, created, destroyed, cost, gpu="RTX 3060"):
        conn.execute(
            "INSERT INTO instances(id,machine_id,label,created_at,state,dph_usd,gpu_name,"
            "ssh_host,ssh_port,slots_total,hard_cap_at,destroyed_at,cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, i, f"box{i}", created, "destroyed", 0.1, gpu, "h", 22, slots,
             "2026-07-15T00:00:00Z", destroyed, cost))

    def task(tid, slots, state, created):
        conn.execute(
            "INSERT INTO tasks(id,created_at,created_by,grp,name,entrypoint,args_json,config_json,"
            "config_hash,arm_hash,git_sha,slots,est_minutes,priority,state,retries_used,max_retries,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, created, "t", "g", tid, "train_x", "[]", "{}", tid, tid, "sha",
             slots, 60, 50, state, 0, 3, created))

    def ev(event, t, tid, iid):
        conn.execute("INSERT INTO events(t,task_id,instance_id,event,detail) VALUES(?,?,?,?,?)",
                     (t, tid, iid, event, ""))

    # Fat backlog 00:00–02:00: two 4-slot tasks queued at 00:00, don't start until 02:00 (run on
    # box1, whose lifetime ends at 01:00 → clamped out of box1's occupancy). 8 slots waiting.
    task("B1", 4, "done", "2026-07-13T00:00:00Z")
    ev("start", "2026-07-13T02:00:00Z", "B1", 1); ev("done", "2026-07-13T02:10:00Z", "B1", 1)
    task("B2", 4, "done", "2026-07-13T00:00:00Z")
    ev("start", "2026-07-13T02:00:00Z", "B2", 1); ev("done", "2026-07-13T02:10:00Z", "B2", 1)

    # box1: 1 slot, 00:00–01:00, one task runs the whole time → occ 1.0 → HEALTHY
    inst(1, 1, "2026-07-13T00:00:00Z", "2026-07-13T01:00:00Z", 0.10)
    task("H1", 1, "done", "2026-07-13T00:00:00Z")
    ev("start", "2026-07-13T00:00:00Z", "H1", 1); ev("done", "2026-07-13T01:00:00Z", "H1", 1)

    # box3: 4 slots, rented 00:00 INTO the fat queue (backlog ≥ 8 ≥ 4) but only one 1-slot task ever
    #   runs on it → low occ, backlog ≥ slots → UNDER_PACKED
    inst(3, 4, "2026-07-13T00:00:00Z", "2026-07-13T01:00:00Z", 0.30, gpu="RTX 4060")
    task("U1", 1, "done", "2026-07-13T00:00:00Z")
    ev("start", "2026-07-13T00:05:00Z", "U1", 3); ev("done", "2026-07-13T00:20:00Z", "U1", 3)

    # box4: 8 slots, 00:00–00:30, NEVER runs a task though the fat queue exists → NEVER_RAN
    #   (its idle is `never_shipped`, since backlog ≥ 8 = slots leaves zero unbacked)
    inst(4, 8, "2026-07-13T00:00:00Z", "2026-07-13T00:30:00Z", 0.05, gpu="Titan Xp")

    # box2: 4 slots, rented 03:00 into a THIN queue (only its own 1-slot O1 waiting) → unbacked = 3
    #   → over-provision idle dominates → OVER_PROVISIONED
    inst(2, 4, "2026-07-13T03:00:00Z", "2026-07-13T04:00:00Z", 0.20, gpu="RTX 3070")
    task("O1", 1, "done", "2026-07-13T03:00:00Z")
    ev("start", "2026-07-13T03:10:00Z", "O1", 2); ev("done", "2026-07-13T04:00:00Z", "O1", 2)

    conn.commit()
    conn.close()


def test_packing_diagnosis_four_buckets(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed_packing(db)
    r = _run_report(db)
    pd = r["packing_diagnosis"]

    assert pd["boxes"] == 4 and pd["boxes_ran"] == 3 and pd["boxes_never_ran"] == 1

    reasons = {row["reason"]: row for row in pd["by_reason"]}
    assert set(reasons) == {"healthy", "over_provisioned", "under_packed", "never_ran"}
    assert reasons["healthy"]["boxes"] == 1
    assert reasons["over_provisioned"]["boxes"] == 1
    assert reasons["under_packed"]["boxes"] == 1
    assert reasons["never_ran"]["boxes"] == 1
    assert reasons["never_ran"]["cost_usd"] == 0.05          # box4 realized cost

    # idle partition fractions sum to ~1.0 and every reason has positive idle here
    ip = pd["idle_partition"]
    assert abs(ip["over_provision"] + ip["never_shipped"] + ip["under_pack"] - 1.0) < 1e-6
    assert ip["never_shipped"] > 0                            # box4 (8 slots × 30 min) idle
    assert ip["over_provision"] > 0                           # box2 unbacked slots
    assert ip["under_pack"] > 0                               # box3 idle-with-backlog

    verdicts = {(c["gpu_name"], c["slots_total"]): c["verdict"] for c in pd["by_offer_class"]}
    assert verdicts[("RTX 3060", 1)] == "healthy"
    assert verdicts[("RTX 3070", 4)] == "over_provisioned"
    assert verdicts[("RTX 4060", 4)] == "under_packed"
    assert verdicts[("Titan Xp", 8)] == "never_ran"
    tx = next(c for c in pd["by_offer_class"] if c["gpu_name"] == "Titan Xp")
    assert tx["boxes_never_ran"] == 1 and tx["occupancy_mean"] == 0.0


# --------------------------------------------------------------------------- pure: section E

def _oc(**d):
    return {"event": "offers_considered", "detail": json.dumps(d)}


def test_offer_counterfactual_report_premium_split():
    """Invariant 16: coverage + premium split by reject reason, malformed skipped."""
    events = [
        {"event": "rent_intent", "detail": "{}"},
        _oc(premium_dph=0.02, cheapest_alt={"reason": "reliability_below_floor"}),
        {"event": "rent_intent", "detail": "{}"},
        _oc(premium_dph=0.05, cheapest_alt={"reason": "reliability_below_floor"}),
        {"event": "rent_intent", "detail": "{}"},
        _oc(premium_dph=0.03, cheapest_alt={"reason": "too_few_slots"}),
        {"event": "rent_intent", "detail": "{}"},
        _oc(premium_dph=0.0, cheapest_alt=None),                 # rented global cheapest, no premium
        {"event": "rent_intent", "detail": "{}"},
        {"event": "offers_considered", "detail": "{bad json"},   # malformed -> skipped, not fatal
    ]
    section, skipped = cal.offer_counterfactual_report(events)
    assert skipped == 1
    assert section["rents_total"] == 5
    assert section["rents_with_offer_data"] == 4          # 4 parseable offers_considered
    assert section["premium_paid_rents"] == 3             # the 0.0 one doesn't count
    assert section["premium_dph_total"] == 0.1            # 0.02+0.05+0.03+0.0
    assert section["premium_dph_median"] == 0.03          # median of [0.02,0.03,0.05]
    # by_reason sorted by premium desc: reliability (0.07) before slots (0.03)
    assert [r["reason"] for r in section["by_reason"]] == \
        ["reliability_below_floor", "too_few_slots"]
    assert section["by_reason"][0]["rents"] == 2 and section["by_reason"][0]["premium_dph_total"] == 0.07


def test_offer_counterfactual_report_empty():
    """Zero offers_considered events -> valid n=0 report (coverage honest), never an error."""
    section, skipped = cal.offer_counterfactual_report(
        [{"event": "rent_intent", "detail": "{}"}, {"event": "rent_intent", "detail": "{}"}])
    assert skipped == 0
    assert section["rents_total"] == 2 and section["rents_with_offer_data"] == 0
    assert section["premium_paid_rents"] == 0
    assert section["premium_dph_median"] is None and section["premium_dph_total"] == 0
    assert section["by_reason"] == []


# --------------------------------------------------------------------------- pure: trend mode

def _finished(tid, created, est, start, done):
    """A done task with one segment start..done, created on `created`."""
    t = _task(id=tid, created_at=created, est_minutes=est, state="done")
    return t, [_ev("start", start, tid, 1), _ev("done", done, tid, 1)]


def test_build_trend_daily_improving_and_ordered():
    """Invariant 17: buckets by created_at day, per-window section-A ratio, improving direction."""
    tasks, events = [], []
    # day 1 (2026-07-08): active 50 / est 100 -> ratio 0.5 (far from 1.0)
    for tid in ("A1", "A2"):
        t, e = _finished(tid, "2026-07-08T00:00:00Z", 100,
                         "2026-07-08T00:00:00Z", "2026-07-08T00:50:00Z")
        tasks.append(t); events += e
    # day 2 (2026-07-10): active 60 / est 60 -> ratio 1.0 (converged)
    for tid in ("B1", "B2"):
        t, e = _finished(tid, "2026-07-10T00:00:00Z", 60,
                         "2026-07-10T00:00:00Z", "2026-07-10T01:00:00Z")
        tasks.append(t); events += e

    tr = cal.build_trend(tasks, [], events, "day", NOW)
    assert [b["window"] for b in tr["buckets"]] == ["2026-07-08", "2026-07-10"]   # oldest->newest
    assert tr["buckets"][0]["start"] == "2026-07-08"
    assert tr["buckets"][0]["runtime"]["ratio_median"] == 0.5
    assert tr["buckets"][1]["runtime"]["ratio_median"] == 1.0
    assert tr["direction"]["ratio_median"] == "improving"   # 1.0 closer to target than 0.5


def test_build_trend_weekly_bucketing():
    """--bucket week keys by ISO year-week with the Monday start (2026-07-08 is in W28)."""
    t, e = _finished("W", "2026-07-08T00:00:00Z", 100, "2026-07-08T00:00:00Z", "2026-07-08T01:00:00Z")
    tr = cal.build_trend([t], [], e, "week", NOW)
    assert tr["buckets"][0]["window"] == "2026-W28"
    assert tr["buckets"][0]["start"] == "2026-07-06"        # ISO Monday of W28


def test_build_trend_single_and_empty():
    t, e = _finished("S", "2026-07-08T00:00:00Z", 100, "2026-07-08T00:00:00Z", "2026-07-08T00:50:00Z")
    one = cal.build_trend([t], [], e, "day", NOW)
    assert len(one["buckets"]) == 1 and one["direction"]["ratio_median"] is None   # <2 non-null
    empty = cal.build_trend([], [], [], "day", NOW)
    assert empty["buckets"] == [] and empty["direction"]["ratio_median"] is None    # invariant 8


# --------------------------------------------------------------------------- golden end-to-end

def _seed_basic(path):
    conn = reg.connect(str(path))
    def inst(i, slots, dph, cost, life_min, gpu="RTX 3060"):
        conn.execute(
            "INSERT INTO instances(id,machine_id,label,created_at,state,dph_usd,gpu_name,"
            "ssh_host,ssh_port,slots_total,hard_cap_at,destroyed_at,cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, i, f"box{i}", "2026-07-13T00:00:00Z", "destroyed", dph, gpu, "h", 22, slots,
             "2026-07-15T00:00:00Z",
             f"2026-07-13T{life_min//60:02d}:{life_min%60:02d}:00Z", cost))
    inst(1, 1, 0.10, 0.20, 120)
    inst(2, 2, 0.20, 0.40, 120)
    inst(3, 1, 0.10, 0.05, 60)   # rented, runs no task -> 100% idle

    def task(tid, grp, ep, est, state):
        conn.execute(
            "INSERT INTO tasks(id,created_at,created_by,grp,name,entrypoint,args_json,config_json,"
            "config_hash,arm_hash,git_sha,slots,est_minutes,priority,state,retries_used,max_retries,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "2026-07-13T00:00:00Z", "t", grp, tid, ep, "[]", "{}", tid, tid, "sha",
             1, est, 50, state, 0, 3, "2026-07-13T00:00:00Z"))

    def ev(event, t, tid, iid):
        conn.execute("INSERT INTO events(t,task_id,instance_id,event,detail) VALUES(?,?,?,?,?)",
                     (t, tid, iid, event, ""))

    task("T1", "gA", "train_x", 100, "done")   # inst1 60min, ratio 0.6
    task("T2", "gA", "train_x", 30, "done")    # inst1 60min, ratio 2.0
    task("T3", "gA", "train_y", 50, "done")    # inst2 1min, DEGENERATE
    task("T4", "gB", "train_x", 60, "done")    # inst2 preempt+requeue, 60min
    task("T5", "gC", "train_x", 100, "task_failed")  # inst2 40min, ratio 0.4
    ev("start", "2026-07-13T00:00:00Z", "T1", 1); ev("done", "2026-07-13T01:00:00Z", "T1", 1)
    ev("start", "2026-07-13T01:00:00Z", "T2", 1); ev("done", "2026-07-13T02:00:00Z", "T2", 1)
    ev("start", "2026-07-13T00:00:00Z", "T3", 2); ev("done", "2026-07-13T00:01:00Z", "T3", 2)
    ev("start", "2026-07-13T00:00:00Z", "T4", 2); ev("preempt_intent", "2026-07-13T00:30:00Z", "T4", 2)
    ev("start", "2026-07-13T01:00:00Z", "T4", 2); ev("done", "2026-07-13T01:30:00Z", "T4", 2)
    ev("start", "2026-07-13T00:00:00Z", "T5", 2); ev("task_failed", "2026-07-13T00:40:00Z", "T5", 2)
    conn.commit()
    conn.close()


def _run_report(db_path):
    conn = cal._connect_ro(str(db_path))
    try:
        tasks, events, instances = cal._fetch(conn, None, None, None)
    finally:
        conn.close()
    return cal.build_report(tasks, instances, events,
                            {"db": "DB", "since": None, "group": None, "entrypoint": None}, NOW)


def test_golden_basic(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed_basic(db)
    report = _run_report(db)

    golden = FIX / "basic.report.json"
    if os.environ.get("REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(report, indent=2) + "\n")
    expected = json.loads(golden.read_text())
    assert report == expected


def test_golden_basic_key_values_hand_verified(tmp_path):
    """Independent hand-computed assertions so the golden snapshot can't silently drift wrong."""
    db = tmp_path / "runs.sqlite"
    _seed_basic(db)
    r = _run_report(db)

    # A: finished=5, T3 degenerate excluded; usable ratios sorted [0.4,0.6,1.0,2.0]
    assert r["runtime"]["overall"] == {
        "n": 4, "excluded_degenerate": 1,
        "ratio_median": 0.8, "ratio_mean": 1.0, "ratio_p10": 0.46, "ratio_p90": 1.7}
    # B: realized 0.65, idle = inst3's 0.05 (rented, ran nothing)
    assert r["cost"]["realized_total_usd"] == 0.65
    assert r["cost"]["unattributed_idle_usd"] == 0.05
    assert r["cost"]["idle_fraction"] == pytest.approx(0.0769, abs=1e-4)
    assert r["cost"]["cost_per_done_task_usd"] == 0.15   # 0.60 attributed / 4 done
    gC = next(g for g in r["cost"]["by_group"] if g["grp"] == "gC")
    assert gC["done"] == 0 and gC["usd_per_done_task"] is None   # T5 failed -> null, no div-by-0
    # C: inst1 full (1.0), inst2 101/240, inst3 idle (0.0); fleet weighted by life
    assert r["packing"]["boxes"] == 3
    assert r["packing"]["fleet_occupancy"] == pytest.approx(0.5683, abs=1e-4)
    cls1 = next(c for c in r["packing"]["by_offer_class"] if c["slots_total"] == 1)
    assert cls1["boxes"] == 2 and cls1["occupancy_mean"] == 0.5   # inst1=1.0, inst3=0.0
    # D: inst3 (rented, ran nothing) is the sole never-ran box; idle partition sums to 1.0
    pd = r["packing_diagnosis"]
    assert pd["boxes"] == 3 and pd["boxes_never_ran"] == 1
    ip = pd["idle_partition"]
    assert abs(ip["over_provision"] + ip["never_shipped"] + ip["under_pack"] - 1.0) < 1e-3
    assert next(x for x in pd["by_reason"] if x["reason"] == "never_ran")["cost_usd"] == 0.05


def test_golden_empty(tmp_path):
    """Invariant 8: zero tasks is a valid report (n=0), exit 0; invariant 4: excluded_degenerate
    present even when zero."""
    db = tmp_path / "runs.sqlite"
    reg.connect(str(db)).close()   # schema only, no rows
    r = _run_report(db)
    assert r["generated_from"]["task_count"] == 0
    assert r["runtime"]["overall"]["n"] == 0
    assert r["runtime"]["overall"]["excluded_degenerate"] == 0   # present, not missing
    assert r["cost"]["realized_total_usd"] == 0
    assert r["cost"]["cost_per_done_task_usd"] is None
    assert r["packing"]["fleet_occupancy"] is None
    assert r["offers"]["rents_total"] == 0 and r["offers"]["rents_with_offer_data"] == 0  # inv 16


# --------------------------------------------------------------------------- read-only invariant

def test_read_only_never_writes(tmp_path):
    """Invariant 1: connection is query_only; a write attempt raises and the file mtime is
    unchanged after a full report run."""
    db = tmp_path / "runs.sqlite"
    _seed_basic(db)
    mtime0 = db.stat().st_mtime_ns
    conn = cal._connect_ro(str(db))
    with pytest.raises(Exception):
        conn.execute("INSERT INTO settings(key,value) VALUES('x','y')")
    conn.close()
    _run_report(db)
    assert db.stat().st_mtime_ns == mtime0


def test_connect_ro_missing_db_errors(tmp_path):
    with pytest.raises(SystemExit):
        cal._connect_ro(str(tmp_path / "nope.sqlite"))


_ev.n = 0
