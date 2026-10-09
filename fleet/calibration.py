"""Coordinator calibration report — expected vs actual (docs/specs/calibration.spec.md).

Read-only analysis over the run registry (`experiments/runs.sqlite`, schema v1). Reconstructs
actual task runtime from the event timeline, attributes realized instance cost by active
slot-minutes, and computes time-weighted packing occupancy — against `est_minutes`, requested
slots, and realized `cost_usd`. Never writes, never migrates, never touches `vastai`/network.

Pure reconstruction helpers (`reconstruct_task_actuals`, `attribute_costs`, `occupancy`,
`build_report`) take already-fetched rows and are unit-tested without a live DB, mirroring
`dispatcher.py`'s pure-core/impure-shell split. `main` is the impure shell (opens a read-only
connection, prints/writes).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Registry public surface (path resolution only) — no dispatcher import, no network.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_db  # noqa: E402

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Invariant 2: the five events the registry logs on a transition OUT of `running`. `stalled`/`lost`
# are markers that precede an `infra_failed` transition, NOT terminators (keying on these five
# counts each running segment exactly once).
START_EVENT = "start"
TERMINATOR_EVENTS = frozenset({"done", "task_failed", "infra_failed", "preempt_intent",
                               "cancel_requested"})

# Invariant 3: calibration ratios cover only finished tasks.
FINISHED_STATES = frozenset({"done", "task_failed"})

# Invariant 4: a finished task whose reconstructed active runtime is below this floor is a
# crash/first-sleep death mismarked terminal — excluded from calibration aggregates, never dropped
# silently (its count always surfaces as `excluded_degenerate`). Module constant, not a CLI flag.
DEGENERATE_FLOOR_MIN = 2.0

# Invariant 11: a never-ran task leaves the backlog when it reaches one of these states (its last
# event timestamp closes the window). Registry `TERMINAL_STATES` plus `infra_failed` (which shows up
# as a live task state once retries are exhausted). Anything else that never ran is still queued —
# its backlog window stays open to `now`.
BACKLOG_TERMINAL_STATES = frozenset({"done", "task_failed", "cancelled", "infra_failed"})

# Invariant 14: dominant-reason thresholds (module constants, not CLI flags). A box is
# `over_provisioned` when over-half its idle capacity had no backlog at rent; `under_packed` when it
# ran but time-weighted occupancy stayed below this floor.
OVERPROVISION_IDLE_SHARE = 0.5
UNDERPACK_OCC_THRESHOLD = 0.5


# --------------------------------------------------------------------------- parsing helpers

def parse_ts(s: str) -> datetime:
    """Registry iso8601 UTC. Raises ValueError on a malformed stamp (invariant 10 boundary)."""
    return datetime.strptime(s, _TS_FMT).replace(tzinfo=timezone.utc)


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _round(x, ndigits):
    return None if x is None else round(x, ndigits)


def percentile(sorted_xs: list[float], q: float):
    """Linear-interpolation percentile (numpy 'linear' / statistics-quantiles method) over an
    already-sorted list. q in [0,1]. Empty -> None. Deterministic (invariant 9)."""
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return sorted_xs[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * frac


def _median(xs: list[float]):
    return percentile(sorted(xs), 0.5)


def _mean(xs: list[float]):
    return sum(xs) / len(xs) if xs else None


# --------------------------------------------------------------------------- section A: actuals

@dataclass
class Segment:
    instance_id: int | None
    start_dt: datetime
    end_dt: datetime
    minutes: float
    slots: int


@dataclass
class TaskActual:
    task_id: str
    grp: str
    entrypoint: str
    slots: int
    est_minutes: int
    state: str
    active_minutes: float
    segments: list[Segment] = field(default_factory=list)
    degenerate: bool = False
    # Section D (invariant 11): the fleet-backlog window this task occupied, [created_dt, exit_dt).
    # None on hand-built TaskActuals that don't exercise section D.
    created_dt: datetime | None = None
    exit_backlog_dt: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    @property
    def ratio(self):
        return self.active_minutes / self.est_minutes if self.est_minutes else None


def reconstruct_task_actuals(task_row, events_for_task, now: datetime) -> TaskActual:
    """Invariant 2. `events_for_task` is this task's events in `seq` order (the caller sorts).
    Each `start` opens a segment closed by the next running-exit terminator; the segment's instance
    is the `start` event's `instance_id`. A dangling open segment (no terminator) is closed at
    `now` only when the task is still `running`; otherwise it is dropped. A second `start` while a
    segment is open is ignored."""
    slots = int(task_row["slots"])
    segments: list[Segment] = []
    open_start: datetime | None = None
    open_inst: int | None = None
    for ev in events_for_task:
        name = ev["event"]
        if name == START_EVENT:
            if open_start is None:
                open_start = parse_ts(ev["t"])
                open_inst = ev["instance_id"]
        elif name in TERMINATOR_EVENTS and open_start is not None:
            end = parse_ts(ev["t"])
            segments.append(Segment(open_inst, open_start, end,
                                    max(0.0, minutes_between(open_start, end)), slots))
            open_start = None
            open_inst = None
    if open_start is not None and task_row["state"] == "running":
        segments.append(Segment(open_inst, open_start, now,
                                 max(0.0, minutes_between(open_start, now)), slots))

    active = sum(s.minutes for s in segments)

    # Section D (invariant 11): backlog window [created_dt, exit_backlog_dt). Exit = first run start
    # if it ran, else last-event time if it reached a backlog-terminal state without running, else
    # `now` (still queued). Uses task_row["created_at"] when present (always on registry rows;
    # absent on some hand-built unit-test rows, which don't exercise section D).
    try:
        created_dt = parse_ts(task_row["created_at"])
    except (KeyError, IndexError):
        created_dt = None
    if created_dt is None:
        exit_backlog_dt = None
    elif segments:
        exit_backlog_dt = segments[0].start_dt
    elif task_row["state"] in BACKLOG_TERMINAL_STATES:
        exit_backlog_dt = parse_ts(events_for_task[-1]["t"]) if events_for_task else created_dt
    else:
        exit_backlog_dt = now

    ta = TaskActual(
        task_id=task_row["id"], grp=task_row["grp"], entrypoint=task_row["entrypoint"],
        slots=slots, est_minutes=int(task_row["est_minutes"]), state=task_row["state"],
        active_minutes=active, segments=segments,
        created_dt=created_dt, exit_backlog_dt=exit_backlog_dt)
    ta.degenerate = ta.finished and active < DEGENERATE_FLOOR_MIN
    return ta


def _runtime_stats(items: list[TaskActual]) -> dict:
    """Invariants 3/4: finished tasks only; degenerate excluded from ratios but counted. Shared by
    section A (`_runtime_section`) and trend mode (`build_trend`) so both use identical ratio math."""
    finished = [t for t in items if t.finished]
    degen = [t for t in finished if t.degenerate]
    usable = [t for t in finished if not t.degenerate]
    ratios = sorted(t.ratio for t in usable if t.ratio is not None)
    ests = [t.est_minutes for t in usable]
    acts = [t.active_minutes for t in usable]
    return {
        "n": len(usable),
        "excluded_degenerate": len(degen),
        "ratio_median": _round(percentile(ratios, 0.5), 4),
        "ratio_mean": _round(_mean(ratios), 4),
        "ratio_p10": _round(percentile(ratios, 0.10), 4),
        "ratio_p90": _round(percentile(ratios, 0.90), 4),
        "est_minutes_median": _round(_median(ests), 1),
        "actual_minutes_median": _round(_median(acts), 1),
        "under_est_count": sum(1 for r in ratios if r > 1.0),
        "over_est_count": sum(1 for r in ratios if r <= 1.0),
    }


def _runtime_section(task_actuals: list[TaskActual]) -> dict:
    """Invariants 3/4: finished tasks only; degenerate excluded from ratios but counted."""
    bucket = _runtime_stats
    overall = bucket(task_actuals)
    overall_keep = {k: overall[k] for k in
                    ("n", "excluded_degenerate", "ratio_median", "ratio_mean",
                     "ratio_p10", "ratio_p90")}

    by_ep: dict[str, list[TaskActual]] = {}
    for t in task_actuals:
        by_ep.setdefault(t.entrypoint, []).append(t)
    rows = []
    for ep, items in by_ep.items():
        b = bucket(items)
        if b["n"] == 0 and b["excluded_degenerate"] == 0:
            continue
        rows.append({
            "entrypoint": ep, "n": b["n"], "excluded_degenerate": b["excluded_degenerate"],
            "est_minutes_median": b["est_minutes_median"],
            "actual_minutes_median": b["actual_minutes_median"],
            "ratio_median": b["ratio_median"],
            "under_est_count": b["under_est_count"], "over_est_count": b["over_est_count"],
        })
    # Most tasks first, then entrypoint name for a deterministic tie-break (invariant 9).
    rows.sort(key=lambda r: (-(r["n"] + r["excluded_degenerate"]), r["entrypoint"]))
    return {"overall": overall_keep, "by_entrypoint": rows}


# --------------------------------------------------------------------------- section B: cost

@dataclass
class CostBreakdown:
    realized_total_usd: float
    attributed_usd: float
    unattributed_idle_usd: float
    per_task_usd: dict           # task_id -> attributed usd
    per_instance_idle_usd: dict  # instance_id -> idle usd


def attribute_costs(instances, task_actuals: list[TaskActual]) -> CostBreakdown:
    """Invariant 5. For each BILLED instance (`cost_usd` NOT NULL AND > 0), split `cost_usd` across
    tasks by active slot-minutes ON THAT instance. Residual `cost_usd - sum(attributed)` is that
    box's idle overhead; a billed box with zero attributable task-minutes contributes its whole
    cost to idle."""
    # instance_id -> {task_id: slot_minutes} from segments bound to that instance.
    by_inst: dict[int, dict[str, float]] = {}
    for t in task_actuals:
        for seg in t.segments:
            if seg.instance_id is None:
                continue
            by_inst.setdefault(seg.instance_id, {})
            by_inst[seg.instance_id][t.task_id] = (
                by_inst[seg.instance_id].get(t.task_id, 0.0) + seg.minutes * seg.slots)

    per_task: dict[str, float] = {}
    per_inst_idle: dict[int, float] = {}
    realized = 0.0
    for inst in instances:
        cost = inst["cost_usd"]
        if cost is None or cost <= 0:
            continue
        realized += cost
        shares = by_inst.get(inst["id"], {})
        total_sm = sum(shares.values())
        if total_sm <= 0:
            per_inst_idle[inst["id"]] = cost  # rented + torn down without running a task
            continue
        attributed_here = 0.0
        for tid, sm in shares.items():
            amt = cost * (sm / total_sm)
            per_task[tid] = per_task.get(tid, 0.0) + amt
            attributed_here += amt
        per_inst_idle[inst["id"]] = cost - attributed_here

    attributed = sum(per_task.values())
    return CostBreakdown(realized, attributed, realized - attributed, per_task, per_inst_idle)


def _cost_section(cb: CostBreakdown, task_actuals: list[TaskActual]) -> dict:
    """Invariant 6: cost_per_done_task overall + per group (null when no done tasks)."""
    done_total = sum(1 for t in task_actuals if t.state == "done")
    by_grp: dict[str, dict] = {}
    for t in task_actuals:
        g = by_grp.setdefault(t.grp, {"done": 0, "attributed_usd": 0.0})
        if t.state == "done":
            g["done"] += 1
        g["attributed_usd"] += cb.per_task_usd.get(t.task_id, 0.0)

    grp_rows = []
    for grp, g in by_grp.items():
        grp_rows.append({
            "grp": grp, "done": g["done"],
            "attributed_usd": _round(g["attributed_usd"], 4),
            "usd_per_done_task": _round(g["attributed_usd"] / g["done"], 4) if g["done"] else None,
        })
    grp_rows.sort(key=lambda r: (-(r["done"]), r["grp"]))

    return {
        "realized_total_usd": _round(cb.realized_total_usd, 4),
        "attributed_usd": _round(cb.attributed_usd, 4),
        "unattributed_idle_usd": _round(cb.unattributed_idle_usd, 4),
        "idle_fraction": _round(cb.unattributed_idle_usd / cb.realized_total_usd, 4)
        if cb.realized_total_usd > 0 else None,
        "cost_per_done_task_usd": _round(cb.attributed_usd / done_total, 4) if done_total else None,
        "by_group": grp_rows,
    }


# --------------------------------------------------------------------------- section C: packing

def occupancy(instance_row, occupied_intervals) -> float | None:
    """Invariant 7. Time-weighted used_slots over the box's billed lifetime
    `[created_at, destroyed_at]`, divided by `slots_total * lifetime_minutes`. A LIVE box (no
    `destroyed_at`) has no closed lifetime -> None (excluded). `occupied_intervals` is a list of
    `(start_dt, end_dt, slots)` on this instance."""
    if not instance_row["destroyed_at"]:
        return None
    life_start = parse_ts(instance_row["created_at"])
    life_end = parse_ts(instance_row["destroyed_at"])
    life_min = minutes_between(life_start, life_end)
    slots_total = int(instance_row["slots_total"])
    if life_min <= 0 or slots_total <= 0:
        return None
    slot_minutes = 0.0
    for s0, s1, slots in occupied_intervals:
        # Clamp each interval to the billed lifetime.
        a = max(s0, life_start)
        b = min(s1, life_end)
        if b > a:
            slot_minutes += minutes_between(a, b) * slots
    return slot_minutes / (slots_total * life_min)


def _packing_section(instances, task_actuals: list[TaskActual]) -> dict:
    # instance_id -> occupied intervals on that instance
    intervals: dict[int, list] = {}
    for t in task_actuals:
        for seg in t.segments:
            if seg.instance_id is None:
                continue
            intervals.setdefault(seg.instance_id, []).append((seg.start_dt, seg.end_dt, seg.slots))

    billed = [i for i in instances if i["cost_usd"] is not None and i["cost_usd"] > 0]
    classes: dict[tuple, dict] = {}
    weighted_num = 0.0
    weighted_den = 0.0
    boxes_counted = 0
    for inst in billed:
        occ = occupancy(inst, intervals.get(inst["id"], []))
        if occ is None:
            continue  # live box excluded (invariant 7)
        boxes_counted += 1
        life_min = minutes_between(parse_ts(inst["created_at"]), parse_ts(inst["destroyed_at"]))
        weighted_num += occ * life_min
        weighted_den += life_min
        key = (inst["gpu_name"], int(inst["slots_total"]))
        c = classes.setdefault(key, {"occs": [], "dphs": []})
        c["occs"].append(occ)
        c["dphs"].append(inst["dph_usd"])

    rows = []
    for (gpu, slots_total), c in classes.items():
        rows.append({
            "gpu_name": gpu, "slots_total": slots_total, "boxes": len(c["occs"]),
            "occupancy_mean": _round(_mean(c["occs"]), 4),
            "dph_usd_median": _round(_median(sorted(c["dphs"])), 4),
        })
    rows.sort(key=lambda r: (-(r["boxes"]), str(r["gpu_name"]), r["slots_total"]))
    return {
        "fleet_occupancy": _round(weighted_num / weighted_den, 4) if weighted_den > 0 else None,
        "boxes": boxes_counted,
        "by_offer_class": rows,
    }


# --------------------------------------------------------------------------- section D: packing dx

@dataclass
class BacklogWindow:
    enter: datetime
    exit: datetime
    slots: int


def backlog_slots_at(rent_dt: datetime, windows: list[BacklogWindow]) -> int:
    """Invariant 11: fleet-wide queued demand at `rent_dt` — sum of `slots` over the half-open
    windows `[enter, exit)` that contain the instant (`enter <= rent_dt < exit`)."""
    return sum(w.slots for w in windows if w.enter <= rent_dt < w.exit)


# Invariant 15 tie-break: which reason wins a per-offer-class verdict when box counts tie.
_REASON_ORDER = ("never_ran", "over_provisioned", "under_packed", "healthy")


def packing_diagnosis(instances, task_actuals: list[TaskActual]) -> dict:
    """Invariants 11–15. Explains *why* occupancy fell short: splits each billed+closed box's idle
    slot-minutes into over-provision / never-shipped / under-pack (against backlog at rent), labels
    each box's dominant reason, and rolls up a cost view and per-offer-class verdicts."""
    windows = [BacklogWindow(t.created_dt, t.exit_backlog_dt, t.slots) for t in task_actuals
               if t.created_dt is not None and t.exit_backlog_dt is not None
               and t.exit_backlog_dt > t.created_dt]

    # instance_id -> occupied intervals on that instance (same derivation as section C).
    intervals: dict[int, list] = {}
    for t in task_actuals:
        for seg in t.segments:
            if seg.instance_id is None:
                continue
            intervals.setdefault(seg.instance_id, []).append((seg.start_dt, seg.end_dt, seg.slots))

    billed = [i for i in instances if i["cost_usd"] is not None and i["cost_usd"] > 0]
    tot_idle = over_idle = nevership_idle = underpack_idle = 0.0
    boxes = boxes_ran = boxes_never = 0
    reasons: dict[str, dict] = {r: {"boxes": 0, "cost_usd": 0.0} for r in _REASON_ORDER}
    classes: dict[tuple, dict] = {}

    for inst in billed:
        occ = occupancy(inst, intervals.get(inst["id"], []))
        if occ is None:
            continue  # live box excluded — same population as section C (invariant 12)
        boxes += 1
        S = int(inst["slots_total"])
        life = minutes_between(parse_ts(inst["created_at"]), parse_ts(inst["destroyed_at"]))
        cap = S * life
        used = occ * cap
        idle = max(0.0, cap - used)
        B = backlog_slots_at(parse_ts(inst["created_at"]), windows)
        unbacked = max(0, S - B)
        over = min(idle, unbacked * life)   # invariant 13: over-provision idle
        rest = idle - over
        ran = used > 0
        if ran:
            boxes_ran += 1
            underpack_idle += rest
        else:
            boxes_never += 1
            nevership_idle += rest
        over_idle += over
        tot_idle += idle

        # Invariant 14: dominant reason.
        if not ran:
            reason = "never_ran"
        elif idle > 0 and over > OVERPROVISION_IDLE_SHARE * idle:
            reason = "over_provisioned"
        elif occ < UNDERPACK_OCC_THRESHOLD:
            reason = "under_packed"
        else:
            reason = "healthy"
        reasons[reason]["boxes"] += 1
        reasons[reason]["cost_usd"] += inst["cost_usd"]

        c = classes.setdefault((inst["gpu_name"], S),
                               {"occs": [], "backlog": [], "never": 0,
                                "reasons": {r: 0 for r in _REASON_ORDER}})
        c["occs"].append(occ)
        c["backlog"].append(B)
        if not ran:
            c["never"] += 1
        c["reasons"][reason] += 1

    def frac(x):
        return _round(x / tot_idle, 4) if tot_idle > 0 else None

    by_reason = [{"reason": r, "boxes": d["boxes"], "cost_usd": _round(d["cost_usd"], 4)}
                 for r, d in reasons.items() if d["boxes"] > 0]
    by_reason.sort(key=lambda r: (-r["cost_usd"], r["reason"]))

    class_rows = []
    for (gpu, S), c in classes.items():
        # Verdict = reason held by the most boxes; tie broken by _REASON_ORDER (invariant 15).
        verdict = min(_REASON_ORDER, key=lambda r: (-c["reasons"][r], _REASON_ORDER.index(r)))
        class_rows.append({
            "gpu_name": gpu, "slots_total": S, "boxes": len(c["occs"]),
            "occupancy_mean": _round(_mean(c["occs"]), 4),
            "backlog_slots_at_rent_median": _round(_median(c["backlog"]), 1),
            "boxes_never_ran": c["never"], "verdict": verdict,
        })
    class_rows.sort(key=lambda r: (-(r["boxes"]), str(r["gpu_name"]), r["slots_total"]))

    return {
        "boxes": boxes, "boxes_ran": boxes_ran, "boxes_never_ran": boxes_never,
        "rent_time_field": "created_at",
        "idle_slot_minutes": _round(tot_idle, 1),
        "idle_partition": {
            "over_provision": frac(over_idle),
            "never_shipped": frac(nevership_idle),
            "under_pack": frac(underpack_idle),
        },
        "by_reason": by_reason,
        "by_offer_class": class_rows,
    }


# --------------------------------------------------------------------------- section E: offer premium

def offer_counterfactual_report(events) -> tuple[dict, int]:
    """Invariant 16. Realized offer-premium accounting over the event log: `rents_total` counts
    `rent_intent` events; each parseable `offers_considered` event (detail = the task-dispatcher
    invariant-5d JSON) contributes its `premium_dph` and (when a cheaper offer was passed) the
    reject `reason`. Returns (section_dict, n_skipped_malformed) — malformed details are skipped and
    tallied, never fatal. Coverage is always reported (zero events → a valid n=0 report)."""
    rents_total = 0
    premiums: list[float] = []          # premium_dph over rents WITH data (0.0 allowed)
    by_reason: dict[str, dict] = {}
    skipped = 0
    for ev in events:
        name = ev["event"]
        if name == "rent_intent":
            rents_total += 1
        elif name == "offers_considered":
            try:
                d = json.loads(ev["detail"])
                prem = max(0.0, float(d.get("premium_dph", 0.0) or 0.0))
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            premiums.append(prem)
            alt = d.get("cheapest_alt")
            if prem > 0 and isinstance(alt, dict) and alt.get("reason"):
                r = by_reason.setdefault(alt["reason"], {"rents": 0, "premium_dph_total": 0.0})
                r["rents"] += 1
                r["premium_dph_total"] += prem

    paid = [p for p in premiums if p > 0]
    reason_rows = [{"reason": k, "rents": v["rents"],
                    "premium_dph_total": _round(v["premium_dph_total"], 6)}
                   for k, v in by_reason.items()]
    reason_rows.sort(key=lambda r: (-r["premium_dph_total"], r["reason"]))
    section = {
        "rents_total": rents_total,
        "rents_with_offer_data": len(premiums),
        "premium_paid_rents": len(paid),
        "premium_dph_median": _round(percentile(sorted(paid), 0.5), 6),
        "premium_dph_total": _round(sum(premiums), 6),
        "by_reason": reason_rows,
    }
    return section, skipped


# --------------------------------------------------------------------------- report assembly

def build_report(tasks, instances, events, filters, now: datetime) -> dict:
    """Assemble the Output-contract object. `events` is ALL events; grouped by task_id here.
    `filters` is the already-applied filter descriptor (for `generated_from`). Malformed rows are
    skipped and tallied, never fatal (invariant 10)."""
    events_by_task: dict[str, list] = {}
    for ev in events:
        if ev["task_id"] is not None:
            events_by_task.setdefault(ev["task_id"], []).append(ev)
    # events arrive ordered by seq from the caller's query; keep that order per task.

    actuals: list[TaskActual] = []
    skipped = 0
    for trow in tasks:
        try:
            actuals.append(reconstruct_task_actuals(trow, events_by_task.get(trow["id"], []), now))
        except (ValueError, KeyError, TypeError) as e:
            skipped += 1
            print(f"calibration: skipping malformed task {trow['id']!r}: {e}", file=sys.stderr)

    cb = attribute_costs(instances, actuals)
    offers_section, offers_skipped = offer_counterfactual_report(events)
    return {
        "generated_from": {
            "db": filters.get("db"),
            "task_count": len(actuals),
            "since": filters.get("since"),
            "group": filters.get("group"),
            "entrypoint": filters.get("entrypoint"),
            "skipped_malformed": skipped + offers_skipped,
        },
        "runtime": _runtime_section(actuals),
        "cost": _cost_section(cb, actuals),
        "packing": _packing_section(instances, actuals),
        "packing_diagnosis": packing_diagnosis(instances, actuals),
        "offers": offers_section,
    }


# --------------------------------------------------------------------------- trend mode (roadmap #5)

def _bucket_key(created_at: str, bucket: str) -> tuple[str, str]:
    """Invariant 17: (window_label, window_start_date) for a `created_at` timestamp. `week` → ISO
    year-week `YYYY-Www` with the ISO-Monday start; `day` → the date, start == label."""
    dt = parse_ts(created_at)
    if bucket == "day":
        d = dt.date().isoformat()
        return d, d
    y, w, _ = dt.isocalendar()
    from datetime import date
    return f"{y}-W{w:02d}", date.fromisocalendar(y, w, 1).isoformat()


def _direction(first, last):
    """`improving`/`worsening`/`flat`/None by whether `last` is closer to 1.0 than `first`."""
    if first is None or last is None:
        return None
    df, dl = abs(first - 1.0), abs(last - 1.0)
    return "improving" if dl < df else "worsening" if dl > df else "flat"


def build_trend(tasks, instances, events, bucket: str, now: datetime) -> dict:
    """Invariant 17. Bucket finished tasks by `created_at` into calendar windows and compute the
    section-A runtime block per window (plus idle_fraction / fleet_occupancy over the instances
    rented in that window). Pure function of the current registry — no snapshots."""
    events_by_task: dict[str, list] = {}
    for ev in events:
        if ev["task_id"] is not None:
            events_by_task.setdefault(ev["task_id"], []).append(ev)

    actuals: list[TaskActual] = []
    skipped = 0
    for trow in tasks:
        try:
            actuals.append(reconstruct_task_actuals(trow, events_by_task.get(trow["id"], []), now))
        except (ValueError, KeyError, TypeError) as e:
            skipped += 1
            print(f"calibration: skipping malformed task {trow['id']!r}: {e}", file=sys.stderr)

    # window key -> {start, acts:[TaskActual], insts:[row]}. Tasks bucket by created_dt; instances by
    # their own created_at (a box is attributed to the window it was rented in).
    windows: dict[str, dict] = {}
    for ta in actuals:
        if ta.created_dt is None:
            continue
        key, start = _bucket_key(ta.created_dt.strftime(_TS_FMT), bucket)
        windows.setdefault(key, {"start": start, "acts": [], "insts": []})["acts"].append(ta)
    for inst in instances:
        if not inst["created_at"]:
            continue
        key, start = _bucket_key(inst["created_at"], bucket)
        windows.setdefault(key, {"start": start, "acts": [], "insts": []})["insts"].append(inst)

    rows = []
    for key in sorted(windows):
        w = windows[key]
        rs = _runtime_stats(w["acts"])
        cb = attribute_costs(w["insts"], actuals)   # all actuals for segments; only these boxes' cost
        pk = _packing_section(w["insts"], actuals)
        rows.append({
            "window": key, "start": w["start"],
            "runtime": {k: rs[k] for k in ("n", "excluded_degenerate", "ratio_median", "ratio_p90",
                                            "under_est_count", "over_est_count")},
            "cost": {"idle_fraction": _round(cb.unattributed_idle_usd / cb.realized_total_usd, 4)
                     if cb.realized_total_usd > 0 else None},
            "packing": {"fleet_occupancy": pk["fleet_occupancy"]},
        })

    non_null = [r["runtime"]["ratio_median"] for r in rows if r["runtime"]["ratio_median"] is not None]
    direction = _direction(non_null[0], non_null[-1]) if len(non_null) >= 2 else None
    return {"bucket": bucket, "buckets": rows,
            "direction": {"ratio_median": direction}, "_skipped": skipped}


# --------------------------------------------------------------------------- md rendering

def _fmt(x, unit=""):
    return "—" if x is None else (f"{x}{unit}")


def render_md(report: dict) -> str:
    rt = report["runtime"]["overall"]
    co = report["cost"]
    pk = report["packing"]
    pd = report["packing_diagnosis"]
    of = report["offers"]

    def _pct(x):
        return _fmt(None if x is None else round(x * 100, 1), "%")

    ip = pd["idle_partition"]
    offer_head = (f" · offer premium ${_fmt(of['premium_dph_total'])}/hr over "
                  f"{of['premium_paid_rents']} rents ({of['rents_with_offer_data']}/{of['rents_total']} "
                  f"with data)") if of["rents_with_offer_data"] else \
                 f" · offer premium: no data yet (0/{of['rents_total']} rents)"
    L = []
    L.append(
        f"calibration: runtime ratio median {_fmt(rt['ratio_median'])} "
        f"(n={rt['n']}, {rt['excluded_degenerate']} degenerate excluded) · "
        f"idle overhead {_fmt(None if co['idle_fraction'] is None else round(co['idle_fraction']*100,1), '%')} · "
        f"fleet occupancy {_fmt(pk['fleet_occupancy'])} · "
        f"{pd['boxes_never_ran']}/{pd['boxes']} boxes never ran · "
        f"idle split {_pct(ip['under_pack'])} under-pack / "
        f"{_pct(ip['never_shipped'])} never-shipped / {_pct(ip['over_provision'])} over-provision"
        + offer_head)
    gf = report["generated_from"]
    L.append("")
    L.append(f"_db: {gf['db']} · {gf['task_count']} tasks"
             + (f" · since {gf['since']}" if gf["since"] else "")
             + (f" · group {gf['group']}" if gf["group"] else "")
             + (f" · entrypoint {gf['entrypoint']}" if gf["entrypoint"] else "")
             + (f" · {gf['skipped_malformed']} malformed skipped" if gf["skipped_malformed"] else "")
             + "_")

    L.append("\n## A. Runtime calibration (est_minutes vs actual active runtime)")
    L.append(f"Overall (finished, non-degenerate): n={rt['n']}, degenerate excluded="
             f"{rt['excluded_degenerate']} · ratio median {_fmt(rt['ratio_median'])} "
             f"mean {_fmt(rt['ratio_mean'])} p10 {_fmt(rt['ratio_p10'])} p90 {_fmt(rt['ratio_p90'])}")
    L.append("\n| entrypoint | n | degen | est med (min) | actual med (min) | ratio med | under-est | over-est |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|")
    for r in report["runtime"]["by_entrypoint"]:
        L.append(f"| {r['entrypoint']} | {r['n']} | {r['excluded_degenerate']} | "
                 f"{_fmt(r['est_minutes_median'])} | {_fmt(r['actual_minutes_median'])} | "
                 f"{_fmt(r['ratio_median'])} | {r['under_est_count']} | {r['over_est_count']} |")

    L.append("\n## B. Cost attribution & idle overhead")
    L.append(f"Realized ${_fmt(co['realized_total_usd'])} · attributed ${_fmt(co['attributed_usd'])} "
             f"· unattributed idle ${_fmt(co['unattributed_idle_usd'])} "
             f"(idle fraction {_fmt(co['idle_fraction'])}) · $/done-task {_fmt(co['cost_per_done_task_usd'])}")
    L.append("\n| group | done | attributed $ | $/done-task |")
    L.append("|---|--:|--:|--:|")
    for r in co["by_group"]:
        L.append(f"| {r['grp']} | {r['done']} | {_fmt(r['attributed_usd'])} | "
                 f"{_fmt(r['usd_per_done_task'])} |")

    L.append("\n## C. Packing occupancy (time-weighted slot utilisation)")
    L.append(f"Fleet occupancy {_fmt(pk['fleet_occupancy'])} over {pk['boxes']} billed boxes")
    L.append("\n| gpu | slots | boxes | occupancy mean | dph median |")
    L.append("|---|--:|--:|--:|--:|")
    for r in pk["by_offer_class"]:
        L.append(f"| {r['gpu_name']} | {r['slots_total']} | {r['boxes']} | "
                 f"{_fmt(r['occupancy_mean'])} | {_fmt(r['dph_usd_median'])} |")

    L.append("\n## D. Packing diagnosis (why occupancy fell short)")
    L.append(f"{pd['boxes_never_ran']}/{pd['boxes']} billed boxes never ran a task · "
             f"{pd['idle_slot_minutes']} idle slot-min · idle split: "
             f"under-pack {_pct(ip['under_pack'])} · never-shipped {_pct(ip['never_shipped'])} · "
             f"over-provision {_pct(ip['over_provision'])}")
    L.append("\n| reason | boxes | realized $ |")
    L.append("|---|--:|--:|")
    for r in pd["by_reason"]:
        L.append(f"| {r['reason']} | {r['boxes']} | {_fmt(r['cost_usd'])} |")
    L.append("\n| gpu | slots | boxes | occupancy mean | backlog@rent med | never-ran | verdict |")
    L.append("|---|--:|--:|--:|--:|--:|---|")
    for r in pd["by_offer_class"]:
        L.append(f"| {r['gpu_name']} | {r['slots_total']} | {r['boxes']} | "
                 f"{_fmt(r['occupancy_mean'])} | {_fmt(r['backlog_slots_at_rent_median'])} | "
                 f"{r['boxes_never_ran']} | {r['verdict']} |")

    L.append("\n## E. Offer-set counterfactual (premium paid over the cheapest rejected offer)")
    if not of["rents_with_offer_data"]:
        L.append(f"No `offers_considered` data yet — 0 of {of['rents_total']} rents in scope carry the "
                 f"event (it is logged going forward from the invariant-5d deploy).")
    else:
        L.append(f"{of['premium_paid_rents']}/{of['rents_with_offer_data']} rents-with-data paid a "
                 f"premium · total ${_fmt(of['premium_dph_total'])}/hr · median "
                 f"${_fmt(of['premium_dph_median'])}/hr · coverage {of['rents_with_offer_data']}/"
                 f"{of['rents_total']} rents")
        L.append("\n| reason cheaper offer was skipped | rents | premium $/hr total |")
        L.append("|---|--:|--:|")
        for r in of["by_reason"]:
            L.append(f"| {r['reason']} | {r['rents']} | {_fmt(r['premium_dph_total'])} |")
    return "\n".join(L) + "\n"


def render_trend_md(trend: dict) -> str:
    dirn = trend["direction"]["ratio_median"]
    arrow = {"improving": "↓→1.0 improving", "worsening": "↑ worsening",
             "flat": "flat", None: "n/a (<2 windows)"}[dirn]
    L = [f"calibration trend ({trend['bucket']}): est ratio median {arrow} "
         f"across {len(trend['buckets'])} windows", ""]
    L.append("| window | start | n | degen | ratio med | ratio p90 | under | over | idle frac | occupancy |")
    L.append("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for b in trend["buckets"]:
        rt, co, pk = b["runtime"], b["cost"], b["packing"]
        L.append(f"| {b['window']} | {b['start']} | {rt['n']} | {rt['excluded_degenerate']} | "
                 f"{_fmt(rt['ratio_median'])} | {_fmt(rt['ratio_p90'])} | {rt['under_est_count']} | "
                 f"{rt['over_est_count']} | {_fmt(co['idle_fraction'])} | {_fmt(pk['fleet_occupancy'])} |")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- impure shell

def _connect_ro(path: str) -> sqlite3.Connection:
    """Invariant 1/8 + Open-question 1: open READ-ONLY, never create. A missing DB is a clear
    error (does NOT route through registry_db.connect, which would init a fresh schema)."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"calibration: no registry db at {path}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _fetch(conn, group, entrypoint, since):
    clauses, params = [], []
    if group:
        clauses.append("grp=?"); params.append(group)
    if entrypoint:
        clauses.append("entrypoint=?"); params.append(entrypoint)
    if since:
        clauses.append("created_at>=?"); params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    tasks = conn.execute(f"SELECT * FROM tasks{where} ORDER BY created_at", params).fetchall()
    task_ids = {t["id"] for t in tasks}
    # Events for the filtered tasks, in seq order (invariant 2 needs seq ordering per task).
    events = [e for e in conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
              if e["task_id"] in task_ids]
    instances = conn.execute("SELECT * FROM instances").fetchall()
    return tasks, events, instances


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Coordinator calibration report (expected vs actual).")
    ap.add_argument("--db", default=None, help="registry db (default: shared experiments root)")
    ap.add_argument("--group", default=None)
    ap.add_argument("--entrypoint", default=None)
    ap.add_argument("--since", default=None, help="ISO8601; filter tasks by created_at >=")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--out", default=None, help="write to FILE instead of stdout")
    ap.add_argument("--trend", action="store_true",
                    help="emit the per-window trend time series instead of the single report")
    ap.add_argument("--bucket", choices=("week", "day"), default="week",
                    help="trend window granularity (--trend only)")
    args = ap.parse_args(argv)

    db_path = args.db or str(registry_db.shared_experiments_root() / "runs.sqlite")
    conn = _connect_ro(db_path)
    try:
        tasks, events, instances = _fetch(conn, args.group, args.entrypoint, args.since)
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    if args.trend:
        trend = build_trend(tasks, instances, events, args.bucket, now)
        trend["generated_from"] = {
            "db": db_path, "bucket": args.bucket, "task_count": len(tasks), "since": args.since,
            "group": args.group, "entrypoint": args.entrypoint,
            "skipped_malformed": trend.pop("_skipped", 0)}
        text = json.dumps(trend, indent=2) if args.format == "json" else render_trend_md(trend)
        if args.out:
            Path(args.out).write_text(text)
        else:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0

    filters = {"db": db_path, "since": args.since, "group": args.group,
               "entrypoint": args.entrypoint}
    report = build_report(tasks, instances, events, filters, now)

    text = json.dumps(report, indent=2) if args.format == "json" else render_md(report)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
