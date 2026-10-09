# Feature: fleet utilization monitor (over/under-packing detection, and what may act on it)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet`
- **Module path:** `fleet/`
- **Status:** built <!-- draft → approved → built --> (stage 1: detect + propose, 2026-08-03)
- **Spec file:** `docs/specs/fleet-utilization-monitor.spec.md`

## Purpose

Continuously answer "are we actually using the fleet capacity we are paying for, and if not, which
lever is wrong?" — and make the answer arrive on its own rather than when someone remembers to run
a report. The measurement half already exists but is **manual and on-demand** (`calibration.py`
sections C/D); nothing runs it on a schedule, nothing alerts, and the only closed loop today is
one-directional (the fleet learns to pack *less*, never to pack *more*).

The gap is not hypothetical. The `_adopt` slot clobber (2026-08-03, `9cd11c97`) capped re-adopted
boxes at ONE lane permanently and ran undetected for a month across three boxes, because no signal
watches "a box's lane count against what its own offer supported". This monitor's first duty is to
make that class of defect loud in a day, not a month.

## What ALREADY exists — do not rebuild

Establishing this is load-bearing: most of the measurement is built, and the honest gap is narrow.

| already built | where | what it gives |
|---|---|---|
| Time-weighted slot occupancy | `calibration.occupancy`, section C | per-box and fleet slot utilisation |
| Packing diagnosis | `calibration.packing_diagnosis`, section D | idle split into `never_ran` / `over_provisioned` / `under_packed` / `healthy` |
| Learned over-pack cap | dispatcher inv. 19h (`overpack_cap_m<machine>`) | closes the loop on **over**-packing only |
| Learned `est_minutes` | dispatcher inv. 24 `learn_group_estimates` | per-(entrypoint, group) p90, scheduling view only |
| Real container utilisation | `box_measured` events, inv. 25/28 | cgroup CPU quota/used, mem limit/anon — the *resource* layer |
| Offer counterfactual | `offers_considered`, inv. 5d | what we passed over and why |
| Event-stream watcher pattern | `watch.py` | read-only over the registry, one line per actionable thing, exits itself |

**So this feature is: a scheduled read + thresholds + a proposal channel, mostly over signals that
already exist.** `calibration.py` stays the owner of the HISTORICAL, cost-attributed view — this tool
does not duplicate it and `--since`-style spend questions still belong there.

One deliberate departure: the LIVE read comes from `box_measured`, not `calibration.occupancy`. That
function needs a closed `[created_at, destroyed_at]` lifetime and returns `None` for a live box, so it
structurally cannot answer "is the fleet packed right now" — and the live question is the one a
continuous monitor exists to answer. The two are complementary and are reported as separate lines with
separate anchors (see invariant 2).

## The two-layer utilization model (invariant 1 — the whole spec turns on this)

"Utilization" is TWO different numbers and conflating them makes every alert unactionable:

1. **Slot occupancy** — are the lanes we bought filled with tasks? Source: registry
   (`calibration.occupancy`).
2. **Resource utilization** — are the filled lanes actually consuming the CPU/GPU/RAM we pay for?
   Source: `box_measured` cgroup fields (inv. 25/28).

The four quadrants have four *different* fixes, and only the pair identifies which:

| | low slot occupancy | high slot occupancy |
|---|---|---|
| **low resource util** | box should not have been rented (or nothing to run) → offer sizing / backlog | lanes are too small — `*_per_lane` hints over-declare → **under-pack**, pack more |
| **high resource util** | lanes are too big — the hint under-declares; box is correctly refusing more | healthy |

A monitor that reports only #1 cannot distinguish "under-packed" from "correctly sized for a heavy
workload", which is exactly the reading that would justify packing a box until it OOMs.

## Input contract

Read-only over the shared registry (`registry_db.shared_experiments_root()`): `instances`, `tasks`,
`events` (`box_measured`, `rent_created`, `start`, `done`, `teardown`). No `vastai` calls, no
network, no writes, no migrations — same contract `calibration.py` already holds.

## Output contract

An **event stream on stdout**, one line per actionable finding (the `watch.py` shape, so it can be
armed with a line-by-line monitor), plus an optional `--format md|json` snapshot report.

Line kinds (each carries the box/class it refers to, the measured number, its anchor, and the $ at
stake): `UNDERPACK`, `OVERPACK`, `SIZING` (a box whose `slots_total` is below what its own
`rent_created` offer supported — the `_adopt`-clobber detector), `GPU-LOST` (a box
registered with a GPU whose probes report none — invariant 10), `EST-DRIFT`, `OK`.

**Every non-`OK` line is a PROPOSAL, not just an observation** (autonomy level (b)). A line states,
in order: what was measured, what it is anchored against, the $ at stake, and **the exact command a
human runs to apply the fix**. A finding with no applyable action is a `SIZING`-class defect and
says so explicitly rather than inventing a knob to turn.

## Public API

Module-internal only, plus one CLI:
`python fleet/fleet_util.py [--since ISO] [--once] [--every 30m] [--format md|json]`.
Reconstruction stays in `calibration.py`; this module must not duplicate it.

## Dependencies

`registry_db.py` (path resolution), `dispatcher.DEFAULT_SETTINGS`.

**It deliberately does NOT recompute `slots_for_offer` for invariant 4.** The draft said to import it
so the detector "cannot drift from the sizing rule it audits" — that was wrong, and running it live
proved it: the hint used at rent time is partly **learned** (inv. 26 `lane_footprint` — fleet
`cores_per_lane` 1.67, and a different value per entrypoint/group), so recomputing with a hint read
back from `tasks.resource_hint_json` reconstructs a different number than the one actually used and
reported ~20 phantom defects on healthy boxes. Sharing the *function* is not sharing the *inputs*.
The fix is a source signal: `_rent` now records `slots=<computed>` on its `rent_created` event, and
the detector compares against that. Boxes rented before it existed are **abstained on and counted**,
never guessed at.

## Behavior & invariants

1. **Report DOLLARS, not box counts.** Measured 2026-08-03: 370/482 boxes "never ran", which sounds
   catastrophic and is worth **4–8% of spend** — those boxes are torn down fast. Meanwhile idle time
   on boxes that *did* run is **58% of realized spend** ($81.18 of $139.79 since 2026-07-25). A box
   count ranks the wrong problem first; every alert is ranked by $ at stake.
2. **Anchor every fraction — and there are TWO occupancies with TWO different anchors.** A raw
   "occupancy 0.197" is an unanchored scalar (the L0 failure the project's working rules forbid), but so is
   an occupancy divided by the *wrong* ceiling.
   - **Lifetime occupancy** (`calibration.occupancy`) divides by the box's FULL billed lifetime, so
     boot and drain make 1.0 unreachable. Its anchor is measured: `1 - (boot + drain)/lifetime` over
     closed billed boxes that actually ran. **0.753** on 2026-08-03 (boot median 25.9 min, drain
     12.2 min, n=122) — i.e. a quarter of every rental is structurally unoccupiable.
   - **Live occupancy** (`box_measured`) only samples boxes that are ALREADY live, so it excludes
     boot and drain by construction and its honest anchor is **1.0**.
   These must never be divided by each other. A first implementation printed "52% — 70% of the 75%
   achievable ceiling", which discounts the same boot+drain twice and flatters the fleet. Same word,
   two populations, two denominators; report them as separate lines.
3. **Split by ERA, never pool across a sizing-rule change.** `slots_for_offer` changed on 2026-07-21
   (floor-up-to-1 → fit filter). Pooled, the fleet looks like it has a chronic 1-slot problem: PRE
   holds 177 one-slot boxes of 395, POST holds 10 of 227. A trend line across that date measures the
   code change, not the fleet. Any window spanning a known sizing change is labelled as such.
4. **`SIZING` detector (the `_adopt`-clobber class).** Compare each box's stored `slots_total`
   against `slots=<computed>`, the value `slots_for_offer` actually produced at rent time, recorded
   on its `rent_created` event. Lower now than at rent ⇒ something overwrote it ⇒ a DEFECT, not a
   tuning signal, so the proposal says "investigate" and never "raise the cap". A box with no logged
   value is **abstained on and counted** — never guessed at (see Dependencies for why re-deriving
   the expected count is not a valid substitute). This is the signal that was missing for a month.
5. **Never alert on a box that cannot be acted on.** A box already torn down, quarantined
   (`ship_quarantine_i<id>`), paused, or `source='owned'` is excluded from spend-waste alerts —
   otherwise the stream is dominated by findings whose fix already happened.
6. **Read-only, always.** No writes, no `vastai` calls, no teardown. Detection and action are
   separate surfaces on purpose (see Staging).
7. **Silence means healthy, and must be earned.** If no threshold is crossed the stream emits a
   periodic `OK` with the headline numbers, so a wedged monitor is distinguishable from a healthy
   fleet — the `watch.py` coverage rule ("silence is not success").
8. **Thresholds are settings, not constants**, and each one's default is justified from measured
   fleet history in the same commit that introduces it.
9. **RETIRED 2026-10-03 — the `WORKERCAP` detector (the capacity-LIE class).** It reported a box
   whose on-box worker enforced a LOWER lane cap than the coordinator packed to. The worker was
   started with `--max-slots {slots_total}`, bring-up no-ops on a live worker, and the worker's
   self-update re-execs with its own argv, so that copy outlived every change to the registry row;
   raising a row left the box refusing the extra work until someone restarted its worker by hand.
   Fourth recorded occurrence, 2026-08-06: `laptop-gpu` raised 6 → 16 on 08-04 while its
   3d18h-old worker went on enforcing 6 — 3 cells from 3 unrelated campaigns claimed and never
   launched, on a 32-core box at load 6.1.

   **Why it is gone rather than kept as a guard: the class was closed by construction**
   (task-dispatcher invariant 8a). The worker holds no lane count — it is started without one and
   ignores one it is handed — so there is no on-box number left to drift, and nothing for this
   detector to read: it found the cap in the worker's `worker_max_slots:` gate line, which no
   worker emits any more. Kept, it would have abstained on every box forever and printed "worker
   cap UNKNOWN" about a cap that does not exist. The invariant number stays reserved so the
   references to it elsewhere still resolve.

10. **`GPU-LOST` detector — a box registered WITH a GPU whose probes now report none** (owner,
    2026-09-23). `instances.gpu_name` says what the box was registered with; each `box_measured`
    JSON tail's `gpu_name` says what `nvidia-smi` saw on that poll (`null` when it failed). Fire when
    the box's newest **`gpu_lost_min_samples` (default 2, ≈10 min)** consecutive samples are `null`.

    **Why a detector is needed.** Nothing else notices: the headroom gate abstains per-axis on an
    unmeasured GPU (by design — a blocked GPU must not cost CPU admission), so a GPU-less box keeps
    taking and running work, on the CPU, silently. Measured: the desktop (box −2) lost its GPU 7 times
    2026-08-12..09-23, the laptop (box −1) ran **19 days** without one (2026-08-23..09-12), and each
    was found only when a human asked. Root cause for the desktop: a systemd `daemon-reload` strips a
    `--gpus all` container's device cgroup (`docs/operations.md`).

    **The threshold is measured, not guessed.** Over 30,585 `box_measured` samples on GPU-registered
    boxes, every null streak was a real loss — 8 of ≥10 samples and one of 2 (a drop fixed by a
    container restart ten minutes in) — and there were **zero** single-sample blips. 2 samples costs
    one poll of latency and absorbs a one-off `nvidia-smi` timeout the history has not yet shown.

    **A missing KEY is unknown, a `null` VALUE is lost.** A pre-inv-25 tail carries no `gpu_name` key
    at all; those samples are skipped, never read as "no GPU".

    **Abstain (counted) when the box has no sample, or its newest is older than
    `gpu_lost_stale_min` (default 30 min).** An unreachable box is the reapers' problem, and its last
    reading is not a statement about now.

    **Owned boxes in scope; emitted AHEAD of the dollar-ranked findings** (`usd_at_stake` is 0.0 on an
    owned box, and it is the sort key, so ranking by dollars would bury a work-blocking defect
    beneath spend noise). On a rental `usd_at_stake` is its full `dph × 24` — we pay
    for a GPU it cannot use. The finding states when the GPU was last seen and the recovery commands.
    Self-extinguishing: the first probe that sees the GPU again ends the streak.

## Staging — detection lands first, action is a SEPARATE decision

**Stage 1 (this spec, if approved): detect and report.** Read-only. Lands on its own merits as
reusable observability (the project's working rules: "a signal at the SOURCE… highest leverage there is").

**Stage 2: closed-loop levers.** Each is its own spec, default OFF, pre-registered criterion, one at
a time — and **must not be built until Stage 1 has run long enough to say whether the lever would
have fired correctly.** Candidate levers, ranked by measured headroom:

- **`est_minutes` over-estimation — the largest visible lever.** Runtime ratio median **0.4343**
  (n=1758): we predict 150 min for work that takes 50. `native.training.m49_curriculum_ab` is 1534 of
  those tasks at ratio 0.399, over-estimated 1354 times. Over-estimation inflates the rental window,
  suppresses packing (window-feasibility gates), and buys hard-cap headroom nobody uses. Note inv. 24
  *already* learns per-(entrypoint, group) p90 — so the first question is why the drift survives it,
  and that is a diagnosis, not a new mechanism.
- **Symmetric pack-UP.** 19h learns a tighter cap when we over-pack but never a looser one; a box
  with high slot occupancy and low resource utilisation is never told to take more.
- **Lane-size feedback.** Per-lane hints (`cores_per_lane`, `vram_per_lane_gb`) are declared by hand
  and never checked against `box_measured` reality.

## Fixtures

The pure core takes plain dicts, so the golden cases are inline in `tests/test_fleet_util.py` rather
than `tests/fixtures/*.json` — same contract, no serialisation layer to keep in sync. Each spec
invariant has a named test; the load-bearing ones are the NEGATIVES, because this tool's failure mode
is not missing a problem, it is **inventing** one and proposing a live config change off it:

- `test_underpack_fires_when_slots_full_but_cpu_idle` — the real 2026-08-03 RTX 3070 shape (7/8
  lanes, 7.25 of 26.88 cores, GPU 0%) ⇒ `UNDERPACK`.
- `test_correctly_sized_heavy_lane_is_SILENT` — **same** slot occupancy, high CPU ⇒ `OVERPACK`, never
  a proposal to pack more. Guards invariant 1. **Mutation-tested**: collapsing the two layers into
  slot occupancy alone makes it propose packing *more* onto a box already at 93% CPU.
- `test_sizing_defect_detects_the_adopt_clobber` — pinned to the real box `40000053`.
- `test_ABSTAINS_when_the_rent_did_not_log_slots` — **mutation-tested**; guessing instead of
  abstaining reproduces the ~20 phantom defects the first draft emitted.
- `test_a_cheap_big_count_never_outranks_one_expensive_box` — 50 trivial boxes vs one costly one
  (guards invariant 1's "dollars, not box counts").
- `test_ceiling_cannot_GO_NEGATIVE` — pins the clamp that a first draft of the anchor read violated
  (it returned −0.138 by mixing box populations).
- `test_era_split_is_LABELLED_never_pooled` (invariant 3), `test_abstained_boxes_are_NEVER_silently_dropped`
  (no silent caps), `test_the_two_occupancies_are_never_divided_by_each_other`.

## Resolved decisions (were Open questions; cleared 2026-08-03)

1. **Autonomy = (b) DETECT + PROPOSE** (owner, 2026-08-03: "start with b then we'll move on to c").
   Every finding carries a concrete recommended change and the exact command that applies it; a
   human applies it. The monitor itself never writes. (c) auto-apply is the stated next stage and
   inherits the proposal record as its evidence — we will be able to ask "would this lever have
   fired correctly?" from logged proposals rather than from a live experiment on the fleet.
2. **Where it runs = a standalone read-only CLI**, `fleet/fleet_util.py`, in the `watch.py`
   shape (`--once` for a one-shot, `--every` for a stream, armable with a line-by-line monitor). NOT a
   dispatcher poll-loop duty: a monitor that audits the daemon must not be able to wedge it, and a
   threshold tweak must not need `make dispatch-restart`. Precedent: both `watch.py` and
   `calibration.py` are out-of-daemon and read-only.
3. **Occupancy reference = MEASURED, not assumed.** The achievable ceiling is
   `1 - (boot + drain)/lifetime`, computed by the tool from the registry over closed billed boxes
   that actually ran. Measured 2026-08-03 over 122 post-cutover boxes: boot→first-start median
   **25.9 min** (p90 182.7), last-done→destroy median **12.2 min**, overhead/lifetime median
   **0.247** ⇒ **achievable ceiling 0.753**. So the fleet's 0.197 raw occupancy is **~26% of
   achievable**, which is the number to report — not 20% of an unreachable 1.0. Recomputed per run
   rather than frozen as a constant, so it tracks boot/drain as those change.

## Open questions

- None blocking. Stage 2 levers each need their own spec before implementation.
