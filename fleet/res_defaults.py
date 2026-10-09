"""Learned per-lane resource footprint — the PACKING feedback loop (invariant 26).

Spec: docs/specs/task-dispatcher.spec.md (invariant 26). Sibling of `est_defaults.py`, which
learns how LONG a task runs; this learns how BIG a lane is. Same shape, same asymmetry argument,
same "no CLI, no commit, no restart" in-flight design.

THE DEFECT IT REMOVES (measured live 2026-07-31). A task's `resource_hint` is stamped at queue time
from its job manifest's `resources` block, and that block is a HAND-WRITTEN GUESS that nobody
revisits. It feeds three consumers — `slots_for_offer` (how many lanes we rent), `task_footprint`
(invariant 18a budget admission) and `_headroom_fits` (invariant 23) — so a wrong guess mis-sizes
every box the fleet buys. The live numbers:

    declared   vram_per_lane_gb 2.0    cores_per_lane 2-4      ram_per_lane_gb 2.0 (settings)
    MEASURED   vram  0.00 GB/lane      cores 1.00-1.02/lane    ram  0.57-1.60 GB/lane

Every rented box was pinned at **5-6 slots** by `floor(12 GB / 2.0)` — a VRAM reservation against a
card measuring 0.00 GB used at 0% util — while `max_slots_cap` (8) never bound and the CPU term
never bound either. Four of five boxes were sized by the axis the workload does not use at all.

WHY A HAND-EDIT CANNOT FIX IT, which is the whole reason this file exists. The manifest lives in
`job.json`, which is deliberately UNTRACKED (commit 7d73eb77 — "per-worktree loose manifest"), so
there is no single source to correct: 25 of 33 live worktrees carry the identical stale
`{"vram_gb": 2, "cores": 4}`, every new worktree inherits it, and a fix in one worktree propagates
to none of the others. The declaration site is structurally unmaintainable, so the number has to be
MEASURED instead of declared.

WHAT IT LEARNS FROM. The `box_measured` event already carries everything needed (invariant 25's JSON
payload): `cpu_used_cores` (a container-true RATE, invariant 24), `vram_used_gb`, `mem_used_gb`, and
the `running` count at sample time. Per-lane = total / running. Attribution rides on the SAME event
rather than being reconstructed from task intervals afterwards — `_box_perf_payload` stamps `ep`/`grp`
when every running task on the box shares one, and leaves them null when the box is mixed. A signal
at the source, per the project's working rules, and it means a mixed box still contributes to the fleet aggregate
instead of being thrown away.

FLEET-WIDE IS THE PRIMARY KEY, per-(entrypoint, group) is a REFINEMENT — the opposite of
`est_defaults`, and deliberately so. Runtime genuinely varies by campaign (one entrypoint spans 270
groups, p10 6 min -> p90 182), which is why the est loop keys on group. Per-LANE SIZE does not vary
that way: `pin_torch_threads(1)` makes it ~1 core for everything, and the fleet-wide median is 1.12
over 1186 samples. Two facts force the ordering:

  * a per-group cores/task slice reads 3-5 and looks like thrash — MEASURED to be small-n noise
    (n=7-16 per group), with the groups being configurationally identical. The fleet-wide median is
    the honest anchor and the per-group slice is the trap.
  * the invariant-25 payload is NEW, so per-key samples do not exist yet (104 samples total at first
    light, 0 keys with n>=20). A learner that only keys per-group would be blind for days.

So: fleet-wide as soon as `FLEET_MIN_SAMPLE` samples exist, refined per key once a key clears
`LIVE_MIN_SAMPLE`, and silent (declared hint stands) before that.

NOT A ONE-WAY RATCHET. `_learn_overpack_cap` only ever takes `min()`, so a cap learned once can
never recover — it pinned the owned laptop at 4 slots from 07-27 until a human raised it by hand on
07-28. This learner REPLACES the declared value in both directions: it lowers `cores`/`vram` (which
frees slots) and it RAISES `ram` (measured p90 2.80 GB/lane against a 2.0 default — the one axis
where the declaration is too SMALL, and where being wrong costs an OOM-kill and a requeue).

THE STATISTIC IS p90 x SAFETY, NOT THE MEDIAN, for the same asymmetry `est_defaults` uses: an
over-estimate costs slots, an under-estimate costs a thrashing or OOM-killed box and a requeue. Cheap
direction, expensive direction — round toward the cheap one.

VRAM IS NETTED AGAINST THE BOX'S IDLE READING BEFORE IT IS LEARNED (invariant 26n, `net_idle_vram`).
`vram_used_gb` is the WHOLE CARD — display, owner, co-tenant — so dividing it by `running` charged
every lane for memory the fleet never allocated (5.94 GB per lane learned for a CPU-only group).

KNOWN BIAS, stated rather than corrected: `mem_used_gb`/`cpu_used_cores` are CONTAINER totals, so
dividing by `running` charges each lane a share of fixed per-box overhead. At low occupancy that
inflates the per-lane figure. The bias is conservative (it over-reserves, never under-reserves) and
shrinks as boxes pack deeper, so it is left in. A slope fit across occupancies would remove it and is
the obvious refinement if the numbers ever matter to a decision.
"""

from __future__ import annotations

import bisect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling imports (fleet isn't a pkg)

import calibration  # noqa: E402  (percentile only — READ-ONLY, no registry access)

# Samples a single (entrypoint, group) needs before it may override the fleet-wide number. Higher
# than the est loop's 3 because this reads a RATE sampled on a cadence rather than one number per
# finished task — consecutive samples of the same box are strongly correlated, so n counts for less.
LIVE_MIN_SAMPLE = 12
# Samples the FLEET-WIDE number needs before it may override a declared hint at all. Low, because
# this statistic is the one measured to be stable (median 1.12 cores/lane over 1186 samples) and
# because until it engages the fleet keeps sizing boxes off a number known to be 3x wrong.
FLEET_MIN_SAMPLE = 5
PERCENTILE = 0.90
# Multiplier on the learned p90. See "THE STATISTIC IS p90 x SAFETY" above.
SAFETY = 1.25

# Floors. `slots_for_offer` divides the offer by these with NO zero-guard, so a learned 0.0 (which
# the VRAM axis produces honestly and constantly — the workload does not touch the GPU) would raise
# ZeroDivisionError inside the placement loop. The floors are also a statement of modesty: a measured
# 0.00 means "below what we can see", not "exactly nothing".
FLOOR_CORES = 0.25
FLOOR_VRAM = 0.25
FLOOR_RAM = 0.50

# Ceilings — a learned value may never exceed these, so a pathological sample (a box whose neighbour
# workload we mis-attributed, a measurement taken mid-teardown) cannot make every offer unfittable.
# `slots_for_offer` returns 0 for an offer that cannot host ONE lane, and 4e's fit filter then DROPS
# that offer — so an over-large learned footprint does not merely under-pack, it can empty the
# rentable set entirely and stall the queue. These bound that blast radius.
CEIL_CORES = 8.0
CEIL_VRAM = 8.0
CEIL_RAM = 16.0

# How far a FLEET-WIDE axis may be spread before it stops meaning anything and abstains. p90/median
# on one axis; 2.0 means "the 90th-percentile lane may be up to twice the median lane".
#
# WHY THE FLEET KEY NEEDS THIS AND A PER-KEY DOES NOT. A per-(entrypoint, group) sample set is ONE
# workload by construction, so its p90 is a statement about that workload. The fleet set is a
# MIXTURE, and a p90 over a mixture is a statement about the heaviest member, not about a typical
# lane — so a single heavy campaign would size every box in the fleet as if every lane were heavy,
# which is exactly the over-declaration this module exists to remove, re-introduced with a
# measurement's authority behind it. This is the same lesson that makes `est_defaults` key on GROUP
# rather than entrypoint: never average across a mixture, abstain instead.
#
# Abstaining is SAFE in both directions: the axis is simply absent, so the task's declared value
# stands and behaviour is exactly what it was before this module existed.
HOMOGENEITY_RATIO = 2.0

# Invariant 26m — the learned per-lane VRAM above which a task that did NOT claim the GPU is treated
# as using it anyway. Not the floor: a card's own baseline moves while lanes run (a browser, a
# video), and with one lane running that movement is the whole "per-lane" figure. MEASURED on the
# live registry 2026-10-03: the seven CPU-only groups read 0.25-0.38 GB after netting (0.38 = six
# samples of a desktop whose display grew 0.3 GB), the smallest real GPU group 1.12. 0.75 sits a
# factor of two from each; a CUDA context alone is ~0.3-0.5 GB, so anything this misses is small.
GPU_USE_MIN_VRAM = 0.75

AXES = ("cores_per_lane", "vram_per_lane_gb", "ram_per_lane_gb")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _stat(vals: list[float], floor: float, ceil: float) -> float:
    """p90 x SAFETY, clamped into [floor, ceil], rounded to 2dp so the value is stable across
    re-derivations and readable in an event line."""
    p = calibration.percentile(sorted(vals), PERCENTILE)
    return round(_clamp((p or 0.0) * SAFETY, floor, ceil), 2)


def _homogeneous(vals: list[float], floor: float) -> bool:
    """Is this axis tight enough for a fleet-wide p90 to describe a typical lane?

    Two ways to qualify. (1) `p90 <= floor`: the whole distribution sits below what the floor can
    resolve — the honest reading of the VRAM axis, which measures 0.00 GB on every box because the
    workload never touches the GPU. That is maximal agreement, not missing data, and it must NOT be
    read as spread just because `p90/median` is 0.25/0.00. (2) the ordinary ratio test."""
    s = sorted(vals)
    p90 = calibration.percentile(s, PERCENTILE) or 0.0
    if p90 <= floor:
        return True
    med = calibration.percentile(s, 0.50) or 0.0
    return med > 0 and p90 <= HOMOGENEITY_RATIO * med


def net_idle_vram(samples) -> list:
    """Pure (invariant 26n): box samples -> `learn_lane_footprint` rows whose VRAM is what the FLEET
    added to the card, not everything on it.

    `samples` is an iterable of `(box, t, entrypoint, grp, running, open, cores, vram_used_gb,
    ram_used_gb)`, one per `box_measured` sample; `t` is seconds on any one clock.

    A box's IDLE reading is a sample with no open fleet task on it — `open == 0`, or `running == 0`
    on a payload that predates `open`. `open` rather than `running`, because a `shipped` or
    `preempting` task can already (or still) hold memory. Each sample's VRAM becomes
    `max(0, vram - the box's most recent idle reading BEFORE it)`: whatever was on the card before
    the lanes started cannot be theirs.

    BEFORE, not nearest and not after. Replayed on six days of fleet history, the reading AFTER a
    GPU run was three times still at the run's own level with nothing open on the box (once for
    over seven hours), so netting against it erased two real GPU groups' VRAM entirely.

    A sample with NO earlier idle reading on its box yields VRAM None, which the learner omits — an
    unmeasured baseline is not a baseline of zero. Cores and RAM pass through untouched."""
    samples = list(samples)
    idle: dict = {}
    for box, t, _ep, _grp, running, open_n, _cores, vram, _ram in samples:
        n = running if open_n is None else open_n
        if vram is None or t is None or n is None or n > 0 or (running or 0) > 0:
            continue
        idle.setdefault(box, []).append((float(t), float(vram)))
    times = {}
    for box, readings in idle.items():
        readings.sort()
        times[box] = [t for t, _ in readings]
    rows = []
    for box, t, ep, grp, running, _open, cores, vram, ram in samples:
        net = None
        if vram is not None and t is not None and box in idle:
            j = bisect.bisect_right(times[box], float(t)) - 1
            if j >= 0:
                net = max(0.0, float(vram) - idle[box][j][1])
        rows.append((ep, grp, running, cores, net, ram))
    return rows


def learn_lane_footprint(rows, min_sample: int = LIVE_MIN_SAMPLE,
                         fleet_min_sample: int = FLEET_MIN_SAMPLE) -> dict:
    """Pure: box samples -> `{None: fleet_footprint, (entrypoint, grp): footprint,
    (entrypoint, None): entrypoint_footprint, ...}`.

    `rows` is an iterable of `(entrypoint, grp, running, cores_used, vram_used_gb, ram_used_gb)`,
    one per `box_measured` sample — VRAM already netted by `net_idle_vram` (26n), None where the
    box had no idle reading to net against. `entrypoint`/`grp` are None for a MIXED box — such a sample still
    feeds the fleet-wide aggregate (its per-lane average is still a real per-lane average) but is
    attributed to no key. `running` <= 0 is dropped: it carries no lanes to divide by, and a sample
    with nothing running measures fixed box overhead, not a lane.

    26l: `(entrypoint, None)` pools one entrypoint's samples across ALL its groups — the same
    program in other configurations. It needs `min_sample` like a per-key entry and, because it
    spans configurations, the homogeneity gate like the fleet entry. It exists because a campaign
    that names every sweep a new group never clears `min_sample` per group, and would otherwise fall
    straight through to a fleet number describing whatever workload dominates the day.

    A footprint is `{"cores_per_lane", "vram_per_lane_gb", "ram_per_lane_gb"}`. An axis whose samples
    were all None is OMITTED rather than defaulted, so a box that cannot read its GPU never teaches
    the fleet that lanes need 0 VRAM. The `None` key is absent when the fleet has fewer than
    `fleet_min_sample` samples — the caller then leaves every declared hint alone.

    The fleet-wide entry additionally drops any axis that fails `_homogeneous`, because a p90 over a
    MIXTURE describes the heaviest workload rather than a typical lane. Per-key entries are exempt:
    a key is one workload by construction, so there is no mixture to average across.
    """
    fleet: dict[str, list[float]] = {a: [] for a in AXES}
    per_key: dict[tuple, dict[str, list[float]]] = {}
    per_ep: dict[tuple, dict[str, list[float]]] = {}
    fleet_n = 0
    key_n: dict[tuple, int] = {}
    ep_n: dict[tuple, int] = {}

    for entrypoint, grp, running, cores, vram, ram in rows:
        if not running or running <= 0:
            continue
        fleet_n += 1
        key = (entrypoint, grp) if (entrypoint is not None and grp is not None) else None
        if key is not None:
            per_key.setdefault(key, {a: [] for a in AXES})
            key_n[key] = key_n.get(key, 0) + 1
        ep_key = (entrypoint, None) if entrypoint is not None else None
        if ep_key is not None:
            per_ep.setdefault(ep_key, {a: [] for a in AXES})
            ep_n[ep_key] = ep_n.get(ep_key, 0) + 1
        for axis, raw in (("cores_per_lane", cores), ("vram_per_lane_gb", vram),
                          ("ram_per_lane_gb", ram)):
            if raw is None:
                continue
            v = float(raw) / float(running)
            fleet[axis].append(v)
            if key is not None:
                per_key[key][axis].append(v)
            if ep_key is not None:
                per_ep[ep_key][axis].append(v)

    floors = {"cores_per_lane": (FLOOR_CORES, CEIL_CORES),
              "vram_per_lane_gb": (FLOOR_VRAM, CEIL_VRAM),
              "ram_per_lane_gb": (FLOOR_RAM, CEIL_RAM)}

    def _footprint(buckets: dict[str, list[float]], require_homogeneous: bool = False) -> dict:
        return {a: _stat(buckets[a], *floors[a]) for a in AXES if buckets[a]
                and not (require_homogeneous and not _homogeneous(buckets[a], floors[a][0]))}

    out: dict = {}
    if fleet_n >= fleet_min_sample:
        fp = _footprint(fleet, require_homogeneous=True)
        if fp:
            out[None] = fp
    for key, buckets in per_key.items():
        if key_n.get(key, 0) < min_sample:
            continue
        fp = _footprint(buckets)
        if fp:
            out[key] = fp
    for key, buckets in per_ep.items():
        if ep_n.get(key, 0) < min_sample:
            continue
        fp = _footprint(buckets, require_homogeneous=True)
        if fp:
            out[key] = fp
    return out


def resolve_hint(learned: dict, entrypoint, grp, declared: dict | None) -> dict | None:
    """The effective `resource_hint` for a task — rent sizing AND admission (one hint, so the two
    agree: invariant 27).

    26-Q1 (resolved 2026-09-28, owner: "this should be able to run on any of these machines"): a
    FLEET-WIDE average FILLS AN AXIS THE TASK DID NOT DECLARE and never overrides one it did. It is
    an average over OTHER workloads, and it cut both ways: it re-priced 14 GB declarations down to
    2.3 (over-admitted onto a 12 GB card, `9f38965d`), and on 2026-09-27 — once one heavy campaign
    was most of the samples — it re-priced every new pclm group UP to the 8 GB clamp, so a task
    declaring 4 GB could board none of the owned GPUs by day and never the 8 GB desktop.

    The task's OWN (entrypoint, grp) measurement is the same workload, so it may correct the
    declaration — but only UPWARD: admission takes `max(declared, per-key)`, so a measurement can
    stop an under-declared job OOMing a box and can never shrink a job below what its author said it
    needs. An undeclared axis takes per-key, else fleet-wide, as before. Returns the declared hint
    unchanged (possibly None) when nothing has been learned.

    Layered per AXIS rather than replaced wholesale, so a hint field this learner does not model
    (`max_dph`, which lifts a task's own price ceiling — invariant 4f) survives untouched.

    26l (2026-09-29): an undeclared axis takes per-key, else the task's ENTRYPOINT across its groups
    (`(entrypoint, None)`), else fleet-wide. And VRAM from either BROADER level fills only a task
    that declares `requires_gpu`: VRAM is sampled only on GPU boxes, so those averages describe GPU
    workloads (or a box's display baseline over its running count) and never a CPU-only lane. Such
    a task keeps its own per-key VRAM when it has one (26m: only above `GPU_USE_MIN_VRAM`), else
    none. The incident: a pclm-majority fleet filled every new
    CPU-only ladder group at 6.5 GB VRAM / 9.7 GB RAM, so an idle 16-slot owned laptop admitted one
    co-located sibling at a time and an owned 24-slot desktop admitted none.

    26m (2026-10-03): a task that does not claim the GPU (no `requires_gpu`, no positive VRAM
    declaration) takes its own (entrypoint, grp) VRAM only when that is ABOVE `GPU_USE_MIN_VRAM`.
    Below it its own group has been measured using no VRAM worth the name, so the axis stays absent
    (or at the 0 it declared) and `dispatcher.lane_vram_gb` charges it nothing — the card's state
    then never gates it. Above it, it has been MEASURED using the card and is gated like any GPU
    user.
    """
    per_key = learned.get((entrypoint, grp)) or {}
    per_ep = (learned.get((entrypoint, None)) or {}) if entrypoint is not None else {}
    fleet = learned.get(None) or {}
    if not per_key and not per_ep and not fleet:
        return declared
    out = dict(declared or {})
    gpu = bool((declared or {}).get("requires_gpu"))
    for axis in AXES:
        present = declared is not None and declared.get(axis) is not None
        if axis in per_key:
            if (axis == "vram_per_lane_gb" and not gpu and per_key[axis] <= GPU_USE_MIN_VRAM
                    and not (present and float(declared[axis]) > 0)):
                continue  # 26m: measured using no VRAM, and it claims none
            out[axis] = max(float(declared[axis]), per_key[axis]) if present else per_key[axis]
            continue
        if present or (axis == "vram_per_lane_gb" and not gpu):
            continue
        if axis in per_ep:
            out[axis] = per_ep[axis]
        elif axis in fleet:
            out[axis] = fleet[axis]
    return out
