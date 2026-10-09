"""Fleet utilization monitor — are we using the capacity we PAY FOR, and which lever is wrong?

Spec: `docs/specs/fleet-utilization-monitor.spec.md` (approved 2026-08-03).

Read-only over the run registry. **stdout is an event stream**: one line per finding an operator
would act on, sized for a line-by-line monitor. Autonomy level **(b) detect + PROPOSE** — every finding
carries the measured number, what it is anchored against, the $ at stake, and the exact command that
applies the fix. This module NEVER writes, never calls `vastai`, never touches the network.

    python fleet/fleet_util.py --once                 # one-shot, human-readable
    python fleet/fleet_util.py --format md            # snapshot report
    python fleet/fleet_util.py --every 30m            # stream (leave it running)

Structure mirrors `watch.py` / `calibration.py`: a **pure core** (`achievable_ceiling`,
`box_utilization`, `find_*`, `render`) taking already-fetched rows and an injected clock — unit
tested with no DB — under an impure shell (`main`) that opens the read-only connection and prints.

WHY THIS EXISTS. The measurement was already mostly built (`calibration.py` sections C/D,
`box_measured`'s cgroup tail from inv. 25/28) but ran only when someone remembered to run it, and
the fleet's only closed loop is one-directional: inv. 19h learns to pack LESS, never more. The
`_adopt` slot clobber (fixed in `9cd11c97`) capped re-adopted boxes at ONE lane and survived a month
across three boxes because nothing watched a box's lane count against what its own offer supported.
`find_sizing_defects` is that missing signal.

THE ONE IDEA TO KEEP (spec invariant 1). "Utilization" is TWO numbers:

  * SLOT occupancy   — are the lanes we bought filled with tasks?     (`running / slots_eff`)
  * RESOURCE util    — are those lanes using the CPU/RAM we pay for?  (cgroup quota vs used)

Conflating them cannot distinguish "under-packed" from "correctly sized for a heavy lane", and
acting on the conflated number is exactly what would pack a box until it OOMs. Both come free from
the `box_measured` JSON tail, which nothing consumed for packing decisions before this.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dispatcher import DEFAULT_SETTINGS, GPU_LOST_MIN_SAMPLES  # noqa: E402
import registry_db  # noqa: E402

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# The date `slots_for_offer` changed from floor-up-to-1 to the fit filter (2026-07-21 owner
# directive). Pooling across it makes a fixed bug look like a chronic fleet defect: PRE holds 177
# one-slot boxes of 395, POST 10 of 227. Spec invariant 3.
SIZING_RULE_CHANGE = datetime(2026, 7, 21, tzinfo=timezone.utc)

# --- thresholds (spec invariant 8: settings, not constants, each justified from measured history)
# A lane is "busy" above this share of its cgroup CPU quota. 0.70 sits above the fleet's observed
# steady-state for a packed-but-idle box (measured 0.27 on a 7/8-occupied RTX 3070, 2026-08-03) and
# below genuine saturation.
BUSY_CPU = 0.70
# Below this, the lanes we bought are not consuming what we rented. Deliberately well under
# `BUSY_CPU` so the band between them is "ambiguous, say nothing" rather than a coin-flip alert.
IDLE_CPU = 0.40
# Slot occupancy above which the box is "full" in lane terms — so low CPU means the LANES are too
# small, not that the queue is empty.
FULL_SLOTS = 0.75
# A box must be seen this many times in the window before it can be proposed on: one sample is a
# poll-cycle artifact (a lane between tasks reads as idle).
MIN_SAMPLES = 3
# `est_minutes` proposals need enough finished siblings for a p90 to mean anything.
MIN_EST_SAMPLE = 8
# Report an est_minutes drift only when the ratio is this far off — below it the win is noise.
EST_DRIFT_RATIO = 0.6
# GPU-LOST fires on `GPU_LOST_MIN_SAMPLES` consecutive GPU-less probes (spec inv. 10), imported from
# the dispatcher so this report and the coordinator's push alert (task-dispatcher inv. 30) cannot
# disagree about what "lost" means.
# A box whose newest probe is older than this is unreachable, not GPU-less: abstain.
GPU_LOST_STALE_MIN = 30.0


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, _TS_FMT).replace(tzinfo=timezone.utc)


def parse_duration(s: str) -> float:
    """'30m' / '2h' / '90s' / '45' (minutes) -> seconds. Validated at the CLI boundary."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(s))
    if not m:
        raise ValueError(f"bad duration {s!r}")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)]


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = 0.9 * (len(s) - 1)
    lo = int(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


# --------------------------------------------------------------------------- the anchor

@dataclass
class Ceiling:
    """The achievable occupancy ceiling. Spec invariant 2: occupancy is divided by the box's FULL
    billed lifetime (`calibration.occupancy`), which includes provisioning/boot and the drain tail —
    so it can never reach 1.0 and a raw fraction is an unanchored scalar. Measured, not assumed."""
    ceiling: float | None
    boot_median_min: float | None
    drain_median_min: float | None
    n_boxes: int


def achievable_ceiling(boxes: list[dict]) -> Ceiling:
    """`1 - (boot + drain)/lifetime` over closed billed boxes that ACTUALLY RAN something.

    Boxes that never ran are excluded on purpose — they are a different failure mode (nothing to
    schedule, or a box that died before its first ship), and folding them in would drive the
    "ceiling" toward 0 and make every real box look healthy against it.

    Each `boxes` entry: {lifetime_min, boot_min, drain_min}. Overhead is clamped to <=1 so a
    pathological row (clock skew, a destroy before the last `done`) cannot push the ceiling
    negative — the failure mode that produced a -0.138 ceiling on the first draft of this read."""
    overheads, boots, drains = [], [], []
    for b in boxes:
        life = b["lifetime_min"]
        if not life or life <= 0:
            continue
        boots.append(b["boot_min"])
        drains.append(b["drain_min"])
        overheads.append(min(1.0, (b["boot_min"] + b["drain_min"]) / life))
    if not overheads:
        return Ceiling(None, None, None, 0)
    return Ceiling(round(1.0 - statistics.median(overheads), 4),
                   round(statistics.median(boots), 1), round(statistics.median(drains), 1),
                   len(overheads))


# --------------------------------------------------------------------------- the two-layer read

@dataclass
class BoxUtil:
    """One box's utilization over the window, both layers. `None` on a layer means the box never
    reported it — abstain, never substitute 0 (a missing cgroup read is not an idle box)."""
    instance_id: int
    gpu_name: str | None
    dph: float
    slots_eff: int | None
    slot_occ: float | None      # layer 1: running / slots_eff
    cpu_util: float | None      # layer 2: cpu_used_cores / cpu_quota_cores
    mem_util: float | None
    gpu_util: float | None
    samples: int


def box_utilization(instance_row: dict, measurements: list[dict]) -> BoxUtil:
    """Fold this box's `box_measured` samples into the two layers.

    Every field is averaged over the window rather than read off the newest sample: one poll can
    catch a box between tasks, and proposing a permanent config change off a single instantaneous
    read is how a monitor manufactures its own false positives."""
    occ, cpu, mem, gpu, eff = [], [], [], [], []
    for m in measurements:
        se, run = m.get("slots_eff"), m.get("running")
        if se and se > 0 and run is not None:
            occ.append(min(1.0, run / se))
            eff.append(se)
        q, u = m.get("cpu_quota_cores"), m.get("cpu_used_cores")
        if q and q > 0 and u is not None:
            cpu.append(min(1.0, u / q))
        ml, ma = m.get("mem_limit_gb"), m.get("mem_anon_gb")
        if ml and ml > 0 and ma is not None:
            mem.append(min(1.0, ma / ml))
        if m.get("gpu_util") is not None:
            gpu.append(m["gpu_util"] / 100.0 if m["gpu_util"] > 1 else m["gpu_util"])
    return BoxUtil(
        instance_id=instance_row["id"], gpu_name=instance_row.get("gpu_name"),
        dph=float(instance_row.get("dph_usd") or 0.0),
        slots_eff=int(round(_mean(eff))) if eff else None,
        slot_occ=_mean(occ), cpu_util=_mean(cpu), mem_util=_mem_or_none(mem),
        gpu_util=_mean(gpu), samples=len(measurements))


def _mem_or_none(mem):
    return _mean(mem)


# --------------------------------------------------------------------------- detectors → proposals

@dataclass
class Finding:
    kind: str            # UNDERPACK | OVERPACK | SIZING | GPU-LOST | EST-DRIFT
    subject: str         # the box / class / group this is about
    measured: str        # what we saw, WITH its anchor
    usd_at_stake: float  # spec invariant 1: rank by dollars, never by box count
    action: str          # the exact command a human runs — or why there isn't one

    def render(self) -> str:
        stake = f"${self.usd_at_stake:.2f}/day" if self.usd_at_stake else "—"
        return f"{self.kind:<10} {self.subject}  {self.measured}  [{stake}]\n           → {self.action}"


def find_sizing_defects(boxes: list[dict]) -> tuple[list[Finding], int]:
    """THE `_adopt`-CLOBBER DETECTOR (spec invariant 4). Returns (findings, n_abstained).

    Compares the stored `slots_total` against `rent_at_slots` — the value `slots_for_offer` ACTUALLY
    computed for this box, logged on its `rent_created` event. A box whose lane count is now LOWER
    than what it was rented at means something overwrote it after the rent, which is a DEFECT, not a
    knob to tune — so the proposal says "investigate", never "raise the cap".

    ⚠ THE FIRST DRAFT OF THIS DETECTOR RE-DERIVED the expected count by calling `slots_for_offer` on
    the box's logged offer with a hint guessed from `tasks.resource_hint_json`. Run live it produced
    ~20 "defects" — boxes reading `slots_total=5 but its offer supports 8` — because the hint used at
    rent time is partly LEARNED (inv. 26 `lane_footprint`: fleet `cores_per_lane` 1.67, and a
    different value per entrypoint/group), so the re-derivation reconstructed a DIFFERENT number than
    the one actually used. A reference leg that does not share the implementation's inputs is not a
    reference leg; it is a second opinion, and it manufactures phantom defects. Hence the logged
    value, and hence ABSTAINING (counted, never silently dropped) on boxes rented before it existed.

    Each `boxes` entry: {id, gpu_name, dph_usd, slots_total, rent_at_slots}."""
    out, abstained = [], 0
    for b in boxes:
        stored, rented = b.get("slots_total"), b.get("rent_at_slots")
        if rented is None:
            abstained += 1          # pre-`slots=` box: we cannot know, so we do not guess
            continue
        if stored is None or stored >= rented:
            continue
        wasted = (rented - stored) / rented if rented else 0.0
        out.append(Finding(
            kind="SIZING", subject=f"box {b['id']} ({b.get('gpu_name')})",
            measured=f"slots_total={stored} but it was RENTED at {rented} lanes "
                     f"({wasted:.0%} of the box unusable)",
            usd_at_stake=round(float(b.get("dph_usd") or 0.0) * 24 * wasted, 4),
            action="DEFECT, not a knob — the lane count was overwritten after the rent. Check this "
                   "box's `adopt` events (this is the 9cd11c97 clobber class)."))
    out.sort(key=lambda f: -f.usd_at_stake)
    return out, abstained


def _fmt_age(minutes: float) -> str:
    return f"{minutes / 60:.1f}h" if minutes >= 90 else f"{minutes:.0f}min"


def find_gpu_lost(boxes: list[dict], now: datetime,
                  min_samples: int = GPU_LOST_MIN_SAMPLES,
                  stale_min: float = GPU_LOST_STALE_MIN) -> tuple[list[Finding], int]:
    """THE GPU-LOST DETECTOR (spec invariant 10). Returns (findings, n_abstained).

    A box registered WITH a GPU (`instances.gpu_name`) whose newest `min_samples` probes all report
    none. Nothing else notices: the headroom gate abstains per-axis on an unmeasured GPU, so a
    GPU-less box keeps admitting work and runs it on the CPU. The desktop lost its GPU 7 times and
    the laptop ran 19 days without one before anyone looked.

    ⚠ A missing `gpu_name` KEY is unknown (a pre-inv-25 tail), a `null` VALUE is lost — only the
    second counts. And a box whose newest probe is stale is abstained on: an unreachable box's last
    reading says nothing about now.

    Each `boxes` entry: {id, label, source, gpu_name, dph_usd, samples: [{t, gpu_name?}] oldest
    first, last_gpu_t: str | None} — `last_gpu_t` being the newest probe that DID see a GPU, which
    may predate the sample window."""
    out, abstained = [], 0
    for b in boxes:
        if not b.get("gpu_name"):
            continue                            # registered without a GPU: nothing to lose
        known = [m for m in b.get("samples") or [] if "gpu_name" in m and m.get("t")]
        if not known:
            abstained += 1
            continue
        try:
            newest = parse_ts(known[-1]["t"])
        except (ValueError, TypeError):
            abstained += 1
            continue
        if (now - newest).total_seconds() / 60 > stale_min:
            abstained += 1                      # unreachable, not GPU-less
            continue
        streak = 0
        for m in reversed(known):
            if m.get("gpu_name"):
                break
            streak += 1
        if streak < min_samples:
            continue
        last = b.get("last_gpu_t")
        if last:
            since = f"last seen {last} ({_fmt_age((now - parse_ts(last)).total_seconds() / 60)} ago)"
        else:
            since = "never seen in the probe history"
        owned = b.get("source") == "owned"
        label = b.get("label") or b["id"]
        out.append(Finding(
            kind="GPU-LOST", subject=f"box {b['id']} ({label})",
            measured=f"registered with {b['gpu_name']} but its last {streak} probe(s) report NO "
                     f"GPU — {since}; work placed here runs on the CPU",
            usd_at_stake=0.0 if owned else round(float(b.get("dph_usd") or 0.0) * 24, 4),
            action=("on the box's WSL host run `bash fleet/owned_gpu_doctor.sh`; a "
                    "`docker restart` of the worker restores it until the next systemd "
                    "daemon-reload, the durable fix is recreating it with --device "
                    "nvidia.com/gpu=all (docs/operations.md)") if owned else
                   (f"a rental billing for a GPU it cannot use — check `runq box probe {label} "
                    f"--wait`, and destroy it if the GPU does not return")))
    return out, abstained


def find_packing_findings(utils: list[BoxUtil]) -> list[Finding]:
    """The two-layer quadrant read (spec invariant 1).

    UNDERPACK = lanes FULL but the box's cgroup CPU idle ⇒ the per-lane hint over-declares, so we
    could run more lanes on hardware we are already paying for.
    OVERPACK  = cgroup CPU saturated ⇒ lanes are contending; inv. 19h should already be learning a
    tighter cap, so this firing means it has not caught up.
    Anything else — including the band between IDLE_CPU and BUSY_CPU — is deliberately SILENT."""
    out = []
    for u in utils:
        if u.samples < MIN_SAMPLES or u.slot_occ is None or u.cpu_util is None:
            continue  # abstain: too few samples, or a layer never reported
        if u.slot_occ >= FULL_SLOTS and u.cpu_util <= IDLE_CPU:
            headroom = int((u.slots_eff or 0) * (1 - u.cpu_util)) if u.slots_eff else 0
            out.append(Finding(
                kind="UNDERPACK", subject=f"box {u.instance_id} ({u.gpu_name})",
                measured=f"slots {_pct(u.slot_occ)} full but cgroup CPU only {_pct(u.cpu_util)} "
                         f"of quota (gpu {_pct(u.gpu_util)}) over {u.samples} samples",
                usd_at_stake=round(u.dph * 24 * (1 - u.cpu_util), 4),
                action=f"lanes are too small — the per-lane hint over-declares. Re-queue this "
                       f"workload with a lower --cores-per-lane (about {max(1, headroom)} more "
                       f"lanes fit), or raise this box's cap: "
                       f"`runq settings set overpack_cap_i{u.instance_id} "
                       f"{(u.slots_eff or 1) + max(1, headroom)}`"))
        elif u.cpu_util >= BUSY_CPU and (u.slot_occ or 0) >= FULL_SLOTS:
            out.append(Finding(
                kind="OVERPACK", subject=f"box {u.instance_id} ({u.gpu_name})",
                measured=f"cgroup CPU {_pct(u.cpu_util)} of quota at {_pct(u.slot_occ)} slots "
                         f"over {u.samples} samples — lanes are contending",
                usd_at_stake=0.0,
                action=f"inv. 19h should be learning a tighter cap here; if this persists, set it "
                       f"directly: `runq settings set overpack_cap_i{u.instance_id} "
                       f"{max(1, (u.slots_eff or 2) - 1)}`"))
    out.sort(key=lambda f: -f.usd_at_stake)
    return out


def find_est_drift(groups: list[dict]) -> list[Finding]:
    """`est_minutes` vs measured p90 actual, per (entrypoint, group).

    The largest lever visible on 2026-08-03: runtime ratio median 0.4343 over 1758 tasks — we book
    150 minutes for work that takes 50. Over-estimation inflates the rental window and suppresses
    packing through the window-feasibility gates. Note inv. 24 ALREADY learns a per-(entrypoint,
    group) p90 for the scheduling view, so a drift surviving here is a diagnosis to chase, not
    automatically a number to hand-set — the proposal says so.

    ONLY groups with work still in flight are reported. A finished campaign's `est_minutes` is not
    something anyone can act on, and alerting on it floods the stream with history — the same reason
    spec invariant 5 excludes torn-down boxes. Run live before this filter, 20+ long-settled groups
    drowned the two that were still running.

    Each `groups` entry: {entrypoint, grp, est_minutes, actual_minutes: [...], active}."""
    out = []
    for g in groups:
        if not g.get("active"):
            continue
        actuals = [a for a in g.get("actual_minutes", []) if a and a > 0]
        est = g.get("est_minutes")
        if len(actuals) < MIN_EST_SAMPLE or not est:
            continue
        p90 = _p90(actuals)
        if p90 is None or p90 <= 0:
            continue
        ratio = p90 / est
        if ratio > EST_DRIFT_RATIO:
            continue
        proposed = max(1, int(p90 * 1.15) + 1)  # p90 + headroom; under-estimation evicts at hard cap
        out.append(Finding(
            kind="EST-DRIFT", subject=f"{g['grp']} ({g['entrypoint']})",
            measured=f"est_minutes={est} but p90 actual={p90:.0f} over {len(actuals)} finished "
                     f"tasks (ratio {ratio:.2f})",
            usd_at_stake=0.0,
            action=f"queue this group with --est-minutes {proposed}. Inv. 24 already learns this "
                   f"per-group — a drift this large surviving it is worth a look."))
    out.sort(key=lambda f: (f.subject))
    return out


# --------------------------------------------------------------------------- render

def render(ceiling: Ceiling, fleet_occ: float | None, findings: list[Finding],
           era_warning: str | None, sizing_abstained: int = 0, n_live: int = 0,
           gpu_abstained: int = 0) -> str:
    """Spec invariant 7: silence must be EARNED — with nothing to report we still print the
    headline, so a wedged monitor is distinguishable from a healthy fleet."""
    L = []
    if era_warning:
        L.append(f"ERA-SPLIT  {era_warning}")
    if sizing_abstained:
        # Never let bounded coverage read as full coverage (the project's working rules: "no silent caps").
        L.append(f"ABSTAIN    {sizing_abstained} box(es) rented before `rent_created` logged "
                 f"slots= — not auditable for the sizing defect, and NOT counted as healthy")
    if gpu_abstained:
        L.append(f"ABSTAIN    {gpu_abstained} GPU box(es) with no fresh probe (none, or newest older "
                 f"than {GPU_LOST_STALE_MIN:.0f}min) — whether the GPU is present is UNKNOWN there")
    # ⚠ TWO DIFFERENT OCCUPANCIES, REPORTED SEPARATELY AND NEVER DIVIDED BY EACH OTHER.
    # `box_measured` only fires on a box that is already LIVE, so the live read excludes boot and
    # drain by construction and its honest anchor is 1.0. The ceiling below discounts boot+drain
    # over the box's FULL BILLED LIFETIME, which is the anchor for `calibration.occupancy`. A first
    # draft printed "52% — 70% of the 75% ceiling", which double-discounts the same overhead and
    # flatters the fleet. Same word, two populations, two denominators.
    if fleet_occ is not None and n_live:
        L.append(f"OK         live: {n_live} box(es), slot occupancy {_pct(fleet_occ)} averaged over "
                 f"the window (anchor 1.0 — a live box can be fully packed)")
    else:
        L.append("OK         live: no box reported box_measured this window")
    if ceiling.ceiling:
        L.append(f"OK         rental overhead: boot {ceiling.boot_median_min}min + drain "
                 f"{ceiling.drain_median_min}min median ⇒ only {_pct(ceiling.ceiling)} of a rental's "
                 f"billed lifetime is even occupiable (n={ceiling.n_boxes} closed boxes that ran)")
    for f in findings:
        L.append(f.render())
    if not findings:
        L.append("OK         no under/over-pack, sizing or GPU-loss findings above threshold")
    return "\n".join(L)


# --------------------------------------------------------------------------- impure shell

def _load(conn, since: datetime, settings: dict):
    """Fetch everything the pure core needs. One place that touches the DB."""
    since_s = since.strftime(_TS_FMT)
    insts = [dict(r) for r in conn.execute(
        "SELECT * FROM instances WHERE source='vast' AND created_at>=?", (since_s,))]

    # --- GPU loss (inv. 10). Every live box registered WITH a GPU, owned included, not filtered by
    # `since`: the newest probes decide it, plus the newest probe that DID see a GPU (which may be
    # weeks old — the laptop's streak ran 19 days, far past any sample window).
    gpu_boxes = []
    for r in conn.execute("SELECT id,label,source,gpu_name,dph_usd FROM instances WHERE "
                          "destroyed_at IS NULL AND state!='destroyed' AND gpu_name IS NOT NULL"):
        samples = []
        for e in conn.execute("SELECT t, detail FROM events WHERE instance_id=? AND "
                              "event='box_measured' ORDER BY seq DESC LIMIT 12", (r["id"],)):
            try:
                m = json.loads((e["detail"] or "").partition("| ")[2])
            except (ValueError, TypeError):
                continue                        # a pre-inv-25 line with no JSON tail
            if isinstance(m, dict):
                m["t"] = e["t"]
                samples.append(m)
        last = conn.execute("SELECT MAX(t) FROM events WHERE instance_id=? AND event='box_measured' "
                            "AND detail LIKE ?", (r["id"], '%"gpu_name":"%')).fetchone()[0]
        gpu_boxes.append({**dict(r), "samples": samples[::-1], "last_gpu_t": last})

    # --- the anchor: closed billed boxes that ran, with their boot and drain overheads
    ceil_rows = []
    for i in insts:
        if not i.get("destroyed_at") or not (i.get("cost_usd") or 0) > 0:
            continue
        t0, t1 = parse_ts(i["created_at"]), parse_ts(i["destroyed_at"])
        life = (t1 - t0).total_seconds() / 60
        ev = list(conn.execute(
            "SELECT event,t FROM events WHERE instance_id=? AND event IN ('start','done') ORDER BY t",
            (i["id"],)))
        starts = [parse_ts(e["t"]) for e in ev if e["event"] == "start"]
        ends = [parse_ts(e["t"]) for e in ev if e["event"] == "done"]
        if not starts or life <= 0:
            continue
        ceil_rows.append({
            "lifetime_min": life,
            "boot_min": max(0.0, (starts[0] - t0).total_seconds() / 60),
            "drain_min": max(0.0, (t1 - max(ends)).total_seconds() / 60) if ends else 0.0})

    # --- the two-layer read, from box_measured's JSON tail (inv. 25/28)
    by_box: dict[int, list] = {}
    for r in conn.execute(
            "SELECT instance_id, detail FROM events WHERE event='box_measured' AND t>=?", (since_s,)):
        if r["instance_id"] is None or not r["detail"]:
            continue
        _, _, tail = (r["detail"] or "").partition("| ")
        try:
            by_box.setdefault(r["instance_id"], []).append(json.loads(tail))
        except (ValueError, TypeError):
            continue  # fail-open: an old/short box_measured line without the JSON tail

    live = {i["id"]: i for i in insts if not i.get("destroyed_at")}
    utils = [box_utilization(live[bid], ms) for bid, ms in by_box.items() if bid in live]

    # --- sizing defects: the lane count the box was RENTED at (logged on `rent_created`) vs stored
    sizing = []
    for i in insts:
        ev = conn.execute("SELECT detail FROM events WHERE instance_id=? AND event='rent_created' "
                          "ORDER BY seq LIMIT 1", (i["id"],)).fetchone()
        if not ev:
            continue
        m = re.search(r"\bslots=(\d+)\s*$", ev["detail"] or "")
        sizing.append({"id": i["id"], "gpu_name": i.get("gpu_name"), "dph_usd": i.get("dph_usd"),
                       "slots_total": i.get("slots_total"),
                       "rent_at_slots": int(m.group(1)) if m else None})

    # --- est drift, per (entrypoint, group). Only groups with work STILL IN FLIGHT are actionable.
    open_states = ("queued", "claimed", "shipped", "running", "preempting")
    active_groups = {(r["entrypoint"], r["grp"]) for r in conn.execute(
        f"SELECT DISTINCT entrypoint, grp FROM tasks WHERE state IN "
        f"({','.join('?' * len(open_states))})", open_states)}
    groups: dict[tuple, dict] = {}
    for r in conn.execute(
            "SELECT t.entrypoint, t.grp, t.est_minutes, t.id FROM tasks t "
            "WHERE t.state='done' AND t.created_at>=?", (since_s,)):
        key = (r["entrypoint"], r["grp"])
        g = groups.setdefault(key, {"entrypoint": r["entrypoint"], "grp": r["grp"],
                                    "est_minutes": r["est_minutes"], "actual_minutes": [],
                                    "active": key in active_groups})
        ev = list(conn.execute("SELECT event,t FROM events WHERE task_id=? AND event IN "
                               "('start','done') ORDER BY t", (r["id"],)))
        s = [parse_ts(e["t"]) for e in ev if e["event"] == "start"]
        d = [parse_ts(e["t"]) for e in ev if e["event"] == "done"]
        if s and d and max(d) > s[0]:
            g["actual_minutes"].append((max(d) - s[0]).total_seconds() / 60)

    return insts, ceil_rows, utils, sizing, list(groups.values()), gpu_boxes


def _fleet_occupancy(utils: list[BoxUtil]) -> float | None:
    vals = [u.slot_occ for u in utils if u.slot_occ is not None]
    return _mean(vals)


def _era_warning(since: datetime) -> str | None:
    if since < SIZING_RULE_CHANGE:
        return (f"window starts {since.date()}, before the {SIZING_RULE_CHANGE.date()} "
                f"slots_for_offer change — pre/post boxes are NOT comparable (spec inv. 3)")
    return None


def run_once(conn, since: datetime, settings: dict, now: datetime | None = None) -> str:
    _insts, ceil_rows, utils, sizing, groups, gpu_boxes = _load(conn, since, settings)
    sizing_findings, abstained = find_sizing_defects(sizing)
    findings = sizing_findings + find_packing_findings(utils) + find_est_drift(groups)
    findings.sort(key=lambda f: -f.usd_at_stake)
    now = now or datetime.now(timezone.utc)
    # ⚠ PREPENDED, NOT SORTED IN (spec inv. 10). `usd_at_stake` is the sort key and GPU-LOST on a
    # $0/hr owned box honestly scores 0.0 — sorting it among the spend findings would bury a
    # defect that is actively costing work beneath every rounding error on a rented box.
    gpu_findings, gpu_abstained = find_gpu_lost(gpu_boxes, now)
    return render(achievable_ceiling(ceil_rows), _fleet_occupancy(utils),
                  gpu_findings + findings, _era_warning(since), abstained,
                  n_live=len(utils), gpu_abstained=gpu_abstained)


def build_parser():
    p = argparse.ArgumentParser(description="Fleet utilization monitor (read-only, proposes fixes)")
    p.add_argument("--db", default=None)
    p.add_argument("--since", default=None,
                   help="ISO date; default = the slots_for_offer era cutover, so the window is "
                        "never silently pooled across it")
    p.add_argument("--every", default=None, help="stream mode: re-read on this interval (e.g. 30m)")
    p.add_argument("--once", action="store_true", help="one shot (the default)")
    p.add_argument("--format", choices=("text",), default="text")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    since = SIZING_RULE_CHANGE
    if a.since:
        try:
            since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"fleet_util: bad --since {a.since!r}", file=sys.stderr)
            return 2
    interval = None
    if a.every:
        try:
            interval = parse_duration(a.every)
        except ValueError as e:
            print(f"fleet_util: {e}", file=sys.stderr)
            return 2
    db = a.db or str(Path(registry_db.shared_experiments_root()) / "runs.sqlite")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"fleet_util: cannot read registry: {e}", file=sys.stderr)
        return 2
    conn.row_factory = sqlite3.Row
    settings = dict(DEFAULT_SETTINGS)
    while True:
        print(run_once(conn, since, settings), flush=True)
        if interval is None:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
