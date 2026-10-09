"""Task dispatcher — home daemon (docs/specs/task-dispatcher.spec.md).

Rents Vast boxes, packs queued tasks onto them (or preempts lower-priority running ones), ships
work over ssh, ingests results (including periodic checkpoints for preemption/infra-failure
resume), and tears boxes down on idle timeout or hard cap — all under a $/hr budget gate.

The pure decision functions (`place`, `slots_for_offer`, `should_teardown`, `warm_hold_grants`,
`retry_decision`,
`reconcile`) take no I/O and are exhaustively covered by `tests/fixtures/dispatch/*.json` — see
tests/test_dispatcher.py. Everything below them is the impure poll-loop wiring (`vastai`
CLI, ssh/rsync, the registry DB).

    python fleet/dispatcher.py --once --dry-run
    python fleet/dispatcher.py                      # runs forever
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import contextlib
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling imports below, even when
# this module is loaded via importlib (tests) rather than run directly as a script

import artifact_store  # noqa: E402  (queuer-built ship blobs; ship-artifact-build spec)
import bundle  # noqa: E402
import capacity  # noqa: E402  (time-of-day resource-cap schedule -> inferred slots)
import code_snapshot  # noqa: E402  (ship persisted working-tree snapshots; code-snapshot spec)
import entrypoints  # noqa: E402
import est_defaults  # noqa: E402  (invariant 24: in-flight per-group est_minutes recalibration)
import registry_db  # noqa: E402
# R2.3: the ONE definition of the worker fingerprint. A second implementation of a comparison
# that gates fleet-wide delivery is the reference-leg-sharing-the-defect trap — two that agree
# prove nothing, two that silently diverge roll the fleet forever or never.
from spool_worker import _source_fingerprint  # noqa: E402
import res_defaults  # noqa: E402  (invariant 26: in-flight per-lane resource footprint calibration)

# Tasks with WORK STILL TO DO — deliberately NOT `registry_db.OPEN_STATES`, which means "occupying a
# slot on a box" and therefore EXCLUDES `queued`. The scheduling-side lookups (invariants 4f and 24)
# care about the opposite population: the queued tail of a campaign is exactly what a learned
# estimate must reach, and a queued big-card job is exactly what must widen the offer search. Reusing
# OPEN_STATES here silently made both features no-ops for the case they exist to serve.
PENDING_STATES = ("queued",) + registry_db.OPEN_STATES

# Stock box image (also the default ABI target for compiled bundles — the compiler must run in an
# image matching what the box runs, or the .so won't load there; task-bundle spec invariant 6).
BOX_IMAGE = "pytorch/pytorch:2.12.1-cuda12.6-cudnn9-runtime"

# Bump when the compile OUTPUT shape changes (flags, keep-set, layout) so stale cache entries retire.
_COMPILE_CACHE_VER = 2  # v2: compile-everything + entry-source overlay + -O0/-g0




# The shared queue/results location every worktree converges on by default
# (registry_db.shared_experiments_root's docstring) -- NOT worktree-relative, unlike ROOT above
# (which stays worktree-scoped: sys.path/`git archive`/bootstrap-ship all need THIS worktree's
# own code, since testing on Vast has never required merging to master, only committing).
EXPERIMENTS_ROOT = registry_db.shared_experiments_root()
DEFAULT_DB = str(EXPERIMENTS_ROOT / "runs.sqlite")
LOCK_PATH = EXPERIMENTS_ROOT / ".dispatcher.lock"
# Static, hand-maintained seed list -- committed to git, never written by the daemon itself.
# Site data: `<FLEET_SITE_DIR>/machines.deny` when a site directory is named (registry_db.site_file).
MACHINES_DENY = registry_db.site_file("machines.deny", Path(__file__).resolve().parent / "machines.deny")
# Auto-blacklist target (bug found live 2026-07-09): this file used to BE `MACHINES_DENY` above,
# but that path lives inside the canonical checkout's own git working tree -- every automatic
# append (a bad box, several times a day) left that checkout permanently dirty, which silently
# defeats `dispatcher_ctl.sh restart`'s auto-fast-forward-to-master (it refuses to touch a dirty
# checkout, so a coordinator fix merged to master would sit unused until someone noticed and ran
# a manual `git pull`). Runtime-learned entries now go to the shared, gitignored experiments root
# instead -- `load_deny_machines` unions both files, so nothing already learned is lost.
MACHINES_DENY_RUNTIME = EXPERIMENTS_ROOT / ".dispatcher" / "machines.deny"

DEFAULT_SETTINGS = {
    # 1.00 -> 1.50 by owner directive 2026-07-30 ("you can bump to $1.5/hr"), raised while the fleet
    # was sitting at 84% of the old cap with a 33-task backlog. This is the ONLY brake on fleet size
    # (invariant 11): `place()` refuses to rent when `current_rate + offer.dph_total` would exceed it,
    # so it is a MONEY cap standing in for a throughput limit — see the placement/ship-throughput
    # Open question, which is why headroom alone does not guarantee the extra boxes get work.
    "max_hourly_usd": 1.50,
    "balance_floor_usd": 3.00,
    # 0.40 -> 0.08 (owner directive 2026-07-31), a GPU-CLASS ceiling. This line's workload does not
    # use the GPU: across 2381 `box_measured` samples GPU util was median 0% / mean 3.6% (79% of
    # samples exactly 0%) and VRAM use averaged 0.27 GB of 13.6 GB rented — 2%. So a dearer card buys
    # nothing, and measured over 7d the dear boxes were also the WORSE buy on the axis that matters:
    # pooled, offers > $0.07/hr cost 1.91x more AND ever-ran a task only 24% of the time vs 54% for
    # the cheap ones (RTX 5060 Ti 25%, V100 25%, RTX 3090 0/2). $0.08 is the knee: it retains 90% of
    # the last 7 days' actual rentals while blocking exactly that class (Tesla V100 x9, RTX 3090 x6,
    # Q RTX 6000 x4). Supply is not the binding constraint — decisions saw ~431 qualifying offers.
    # OVERRIDABLE PER TASK via `resource_hint.max_dph` for the jobs that genuinely need a big card
    # (24 GB class); see `task_max_dph`. Raise this global only if the whole line's needs change.
    "max_instance_dph": 0.08,
    "idle_timeout_min": 10,
    # Invariant 11a (owner directive 2026-07-30). How many EMPTY paid boxes may be held warm against
    # the queue by `feasible_task_waiting`. Without a cap that check is per-box, so ONE queued 1-slot
    # task retains EVERY idle box and `idle_timeout_min` never fires — measured live at four empty
    # boxes idle 24-80 min costing $0.2162/hr. A NEW key, so `_ensure_settings` seeds it into an
    # existing registry on its own; no `_SETTING_MIGRATIONS` row is needed (contrast
    # `consolidate_enabled`, which already existed and therefore did need one).
    #
    # 1 -> 2 by owner directive 2026-07-30: "warm_idle_max boxes should probably be 2, that was just
    # a hack to close the gap." The gap it was closing is now held by `max_warm_free_slots` below,
    # which counts free slots FLEET-WIDE — so the box count no longer has to stand in for a slot
    # bound it was never expressing, and 2 is the honest burst-absorption number.
    "warm_idle_max": 2,
    # The SLOT half of the same invariant, and the second unit the owner stated it in the same day:
    # "we have 58/98 slots in use so almost 50% of our spend is going to waste ... no more than 10
    # free slots kept warm." The budget is FLEET-WIDE — free slots on live boxes that still hold
    # work (owned or paid) are spent against it FIRST ("make sure max_warm_free_slots includes
    # non-empty boxes that have free slots"), because those slots are exactly as available to the
    # queue as a warm box's are and cost nothing extra. Only the remainder may be held on an empty
    # PAID box. This, not the box count, is the real constraint.
    #
    # WHAT THE PAIR FIXES is a DEADLOCK, not a warm-pool feature — nobody ever wrote one.
    # `_pack_cost` (4b) scores an EMPTY paid box at its FULL `dph x window` (there is no occupant to
    # ride along with) versus $0.0000 for a spare slot on a busy box, so an empty paid box ranks
    # STRICTLY LAST for placement, while `feasible_task_waiting` holds it alive for as long as ANY
    # queued task merely FITS it. Placement won't use it because it assumes the box is about to die;
    # teardown won't kill it because it assumes the queue will use it. MEASURED 2026-07-30: at 20:49
    # twelve tasks packed onto boxes with 1-4 free slots at "$0.0000 marginal" while FOUR empty
    # 6-slot boxes got nothing, then all four were torn down within 4 SECONDS of each other
    # (21:29:53-57) the instant the queue emptied. Over 24h: 27 of 57 torn-down boxes never had a
    # single task `start` (22.1 box-hours, $1.32), and the 30 that did work billed a further 6.0
    # box-hours empty ($0.34). Also a NEW key — same `_ensure_settings` seeding note as above.
    "max_warm_free_slots": 10,
    "rent_patience_min": 15,
    "vram_per_lane_gb": 0.6,
    "cores_per_lane": 1,
    # Invariant 23 (measured headroom). Reserves are what we always leave FREE on a box so a poll
    # never schedules it to the edge; `ram_per_lane_gb` is the RAM footprint hints rarely declare.
    "headroom_enabled": True,
    "headroom_cpu_reserve": 1.0,
    "headroom_ram_reserve_gb": 2.0,
    "headroom_vram_reserve_gb": 1.0,
    "headroom_max_stale_min": 15,
    "ram_per_lane_gb": 2.0,
    # ⛔ REVERTED 16 -> 8 (2026-08-01) after it caused five over-pack unschedules in 30 minutes.
    # The throughput result below still STANDS — what it does not establish is that the fleet can
    # DELIVER that depth, and that is the thing that broke. Two independent ceilings sit downstream
    # of this one, in `sweep_supervisor.AUTO_DEFAULTS`, which the probe never exercised because it
    # was ONE task self-forking K processes rather than K tasks placed through the launch gate:
    #   * `max_slots: 8` — `should_launch` refuses past 8 live lanes NO MATTER what `--max-slots`
    #     the dispatcher passed the worker, so a box advertised at 16 can never reach it;
    #   * `settle_minutes: 3.0` — one lane per 3 min, so 16 lanes take 48 MINUTES to fill.
    # `_reap_overpacked_boxes` then reads "running < advertised" after `ship_launch_grace_min` and
    # ratchets the machine's learned cap DOWN — permanently, since `_learn_overpack_cap` only ever
    # takes min(). Measured damage: machine m10004 ratcheted 5 -> 3 -> 1, and the owned laptop went
    # from a manually-validated 6 to 1. Raising THIS number alone was the error; it is safe to try
    # again only once the launch gate can actually fill the slots (see `ship_launch_grace_min`).
    #
    # The evidence that deeper packing PAYS, kept for whoever fixes the delivery path:
    # on the pre-registered criterion of `hw/deepk16`
    # (`configs/diagnostics/lane_scaling_bench_deep.json`, curve in `experiments/hw/summary.md`).
    # Measured on an exclusively-rented Xeon E5-2650 v4 (12 physical / 24 threads, 30 MiB L3):
    # effective lanes 6.47 @ K=8 -> 8.42 @ K=12 -> 10.18 @ K=16, i.e. +3.71 lanes over K=8 and
    # $0.0081 -> $0.0051 per effective-lane-hour (37% cheaper) on the same box at the same dph.
    # The criterion was "raise to the largest tested K beating K=8 by >= 1.0 effective lane".
    #
    # SAFE ONLY BECAUSE INVARIANT 27 LANDED FIRST. 16 lanes x the measured 2.34 GB/lane is 37.4 GB,
    # and RAM is the binding axis on 31 of 59 gated offers — without a RAM term in `slots_for_offer`
    # this raise would have made the fleet rent MORE big-core boxes it then refuses to fill.
    # It binds rarely by design (3% of the current offer pool reaches 16 lanes; the median is 5) —
    # it exists to stop truncating the occasional genuinely large box, not to pack small ones deeper.
    # NOTE efficiency still falls with depth (100% -> 80.9% -> 70.1% -> 63.6%) and per-lane time IS
    # task wall-clock (1.57x dilation at K=16); invariant 24's learned `est_minutes` re-converges at
    # the new depth, but watch `stalled` / hard-cap evictions after this lands.
    #
    # ── RAISED 8 -> 12 (2026-08-04). Both blockers above are now GONE; this is why. ──────────────
    # (a) The reaper half fixed itself: invariant 19h-3(d)'s RECENT-LAUNCH guard landed 2026-08-02,
    #     AFTER the text above was written. `_reap_overpacked_boxes` now skips any box that started
    #     a lane within `ship_launch_grace_min`, and at `settle_minutes` 3.0 a filling box starts one
    #     every 3 min — so `min(launched)` is ~3 against a 35 min grace and the ratchet cannot fire
    #     mid-fill at ANY K. `ship_launch_grace_min` was also raised 20 -> 35 in the same period.
    # (b) The delivery half is fixed in this change: `spool_worker` now passes its OWN per-box
    #     `--max-slots` into `should_launch` instead of `AUTO_DEFAULTS["max_slots"]`, removing the
    #     second fleet-wide ceiling of 8 that made a box advertised at 12+ unfillable regardless.
    #     (Superseded 2026-10-03 by invariant 8a: the worker holds no lane count at all.)
    #
    # 11, not 16 and not 12 — this is the largest K that fits the EXISTING grace under the
    # CONSERVATIVE fill bound, and it is chosen that way on purpose. The true gated fill is
    # (K-1) x settle, because `should_launch` returns "first lane" at `n_live == 0` before the settle
    # check; `TestGraceExceedsFillTime` deliberately budgets the cruder K x settle instead, and that
    # extra interval is a fair allowance for the per-lane ship/launch time the ideal arithmetic
    # ignores. Weakening that guard to the exact bound would buy K=12 by removing the margin that
    # makes the bound safe, so: 11 x 3.0 = 33 <= 35, unchanged guard, one constant moved.
    # Interpolating the measured curve (6.47 @ K=8, 8.42 @ K=12) puts K=11 near 8.0 effective lanes,
    # ~+1.5 over K=8 — comfortably past the pre-registered +1.0 bar. Going beyond this needs a
    # LARGER `ship_launch_grace_min` or a shorter `settle_minutes`, as its own decision with its own
    # evidence; do not take it by loosening the guard.
    # `settle_minutes` is deliberately UNCHANGED at 3.0. Measured 2026-08-04, it is not protecting
    # anything close to binding: the `cpu_load` gate it paces (`load1 >= cores - 1`) ran with 10-32
    # cores of HEADROOM at every depth the fleet has ever reached (load1 rises ~1.0 per lane: 0.7 at
    # 0 lanes, 3.1 at 2, 6.2 at 6, 8.3 at 7, against a threshold of 19-55) and has never fired. And
    # there is no throughput ramp to protect either — 95% of 930 runs were already at >=90% of their
    # steady-state steps/sec by the FIRST logged interval (median 0 warmup steps), with startup to
    # first step ~1 min. Shortening it would buy fill latency, not correctness; that is a separate
    # decision and it is not needed for K=12.
    "max_slots_cap": 11,
    # Hardware-quality gate (invariant 4e, 2026-07-21 owner directive). The coordinator was booking
    # cheap-but-weak single-slot boxes (e.g. a 5.25c 9-core GTX 1080 over a 5.47c 24-core RTX 3060
    # Ti) because (1) `search offers` defaults to the 64 CHEAPEST boxes — a junk-dominated window —
    # and (2) the demand-capped $/slot rule degenerated to raw price on a short queue.
    "offer_search_limit": 1000,   # --limit on `vastai search offers` (CLI default 64 hides good HW)
    # gpu_name substrings (case-insensitive) that make an offer ineligible: obsolete consumer
    # Pascal/Maxwell + old datacenter classes, overlapping the reliability audit's worst offenders.
    # A proxy for host generation (old GPU -> old/slow CPU) on the CPU-bound workload. Operator-
    # editable; the cores-per-$ ranking is the primary lever, this just hard-excludes known junk.
    "gpu_deny": ["GTX", "Titan Xp", "Titan X", "Titan V", "Quadro P", "Quadro M", "Quadro K",
                 "Tesla K", "Tesla M", "Tesla P", "P100", "K80"],
    # Drop offers below this effective-core floor — kills 1-2 core slivers (the 1.71-core Titan Xp
    # slice booked 4x at full dph) the fit filter alone admits for cores_per_lane=1 tasks. Kept low
    # so it never touches a legitimate small modern card (a 4-core RTX 4070S/A4000).
    "min_cpu_cores_effective": 2.0,
    # ── Invariant 4f: per-core SPEED in the ranking (2026-08-04) ────────────────────────────────
    # The 4e ranking above prices lane COUNT and is blind to lane SPEED. Measured over 4,987
    # `box_measured` samples + 608 TB-instrumented runs: GPU utilisation is 0% at p50 AND p90 (above
    # 20% in 0.1% of rented samples), VRAM used is 0.00 GB against the 0.6 GB/lane we declare, and
    # GPU model does NOT predict throughput (RTX 3060 1.00, RTX 3090 1.00, RTX 3060 Ti 1.08, RTX
    # 2080 Ti 0.96, all normalised to their own group's rented median). The workload is CPU-bound, so
    # what separates boxes is per-core speed — the fleet's own laptop-gpu runs 2.1x the desktop at
    # comparable co-residency. `gpu_deny` was already reaching for this signal by proxy ("old GPU ->
    # old/slow CPU"); these read the CPU fields the marketplace publishes directly.
    #
    # `cpu_speed_weight` is the exponent on (cpu_ghz / cpu_ghz_reference); 0 disables the term
    # entirely and restores the pure 4e cores-per-$ ranking. 1.0 = linear in clock.
    "cpu_speed_weight": 1.0,
    # Reference clock the factor is relative to — the MEASURED median of the live qualifying pool
    # (800 offers sampled 2026-08-04: p10 2.25, p50 3.53, p90 4.80 GHz, and 100% of them publish
    # `cpu_ghz`, so this term is live on essentially every offer rather than abstaining). Centring
    # here matters: at a 3.0 reference the median offer already scores 1.18 and the p90 saturates the
    # clamp, so the term would inflate nearly every density and bite asymmetrically. At 3.5 the
    # median lands at ~1.01 and the clamp bounds both tails instead of only the fast one. Re-measure
    # if the marketplace's CPU mix shifts.
    "cpu_ghz_reference": 3.5,
    # CLAMP — the honest limit on the proxy. Base clock ignores IPC, so a 3.0 GHz Xeon E5-2686 v4
    # scores identically to a 3.0 GHz modern Ryzen and is not the same machine. Bounded so the term
    # can break ties between similarly-priced offers but can never override the cores-per-$ or
    # reliability decisions that ARE grounded in measurement. Widen it only from a re-fit against
    # realised steps/sec per `cpu_name` (which `rent_created` now accumulates), never from intuition.
    "cpu_speed_clamp": (0.75, 1.35),
    # Invariant 27: how much of an offer's advertised `cpu_ram` we trust as real container RAM.
    # Measured `mem_limit_gb` on the 8 boxes we hold is 2.18 GB/effective-core against the market's
    # advertised 2.61 — offers over-promise by ~20%, so 0.8 turns that into an under-promise. Raise
    # toward 1.0 only with more (advertised, measured) pairs; `rent_created` now records both sides.
    "offer_ram_derate": 0.8,
    "backlog_min_tasks": 3,
    "backlog_min_task_minutes": 60,
    # ★ THE GLOBAL PREEMPT SWITCH (owner directive 2026-07-30): "let's just disable preempts for now.
    # The checkpoints are introducing noise and fragility and they don't seem to be buying us much
    # today. Our jobs tend to cost <$1 each so just letting the existing ones finish seems like the
    # right move." — followed by "disable all of them -> wire this as a global control flag".
    #
    # FALSE means the fleet never interrupts a running task on its own initiative. It gates all THREE
    # fleet-initiated preempt sources at once:
    #   1. priority preemption   (inv. 4d/17 — a higher-priority task evicting a lower one)
    #   2. capacity scale-down   (inv. 20 `_reap_over_capacity` — an owned box's window cap dropping)
    #   3. consolidation drains  (inv. 21 — vacating a paid box to reclaim it)
    #
    # NOT gated, deliberately: `_signal_drain`, i.e. `make drain` / `make pause`'s soft-timeout. That
    # is the OPERATOR pressing a button to get their own laptop back, not the fleet choosing to churn
    # — gating it would remove the ability to reclaim a personal machine on demand, which is the
    # opposite of what this directive is protecting.
    #
    # WHY the trade favours this: a preempt is only free if resume is free, and it is not —
    # `test_resuming_from_a_checkpoint_reproduces_the_uninterrupted_run` is RED, so every interruption
    # costs that arm some comparability, and measured on 2026-07-29 8.4% of preempts lost their
    # checkpoint outright. Against that, the whole benefit being bought was ~$0.011 per preempt
    # (24h: 81 drains, 313 preempts, an upper-bound $3.51 saved). At <$1/job, running to completion
    # is simply cheaper than interrupting.
    # WHAT IT COSTS: paid boxes idle-bill until their last occupant finishes and teardown (inv. 11)
    # collects them, `--probe` no longer starts by evicting (it queues at priority 90 and rents), and
    # an owned box can sit ABOVE its day cap until its tasks finish — placement still refuses to
    # ADMIT over the cap (inv. 18a/23), so the overshoot is bounded and drains on its own.
    # To re-enable: set this True (and `consolidate_enabled` True) — nothing was removed.
    "preempt_enabled": False,
    "preempt_priority_margin": 30,
    "ssh_fallback_fails": 3,
    "checkpoint_pull_every_min": 5,
    # Invariant 23c — how many BOXES ingest may pull per-task payloads from concurrently (one worker
    # per box; within a box the pulls stay serial). A NEW key, so `_ensure_settings` seeds it into an
    # existing registry on its own; no `_SETTING_MIGRATIONS` row needed.
    # MEASURED, not guessed: per-box throughput is ~0.3-0.62 MB/s (probed 2026-07-31, and matching
    # `rsync_push`'s independently measured figure) against a 1 Gb/s home uplink — so the ceiling is
    # each box's OWN network path, not ours. Concurrency within a box contends for one saturated
    # link and buys nothing; across boxes the paths are independent and it is ~linear. 8 covers the
    # observed live fleet (9-16 boxes) while bounding local load — the coordinator shares this
    # desktop with interactive sessions and the owned-box worker, and each stream is a `rsync`+`ssh`
    # process pair. Set to 1 to disable the fan-out entirely (the serial path is kept and tested).
    "ingest_parallel_boxes": 8,
    # The SHIP half of the same fan-out (invariant 23d, 2026-07-31). New key, so `_ensure_settings`
    # inserts it into the live registry on its own — no migration row needed.
    # 8 for the same measured reasons as ingest above, and the axis is likewise PER-BOX: each box's
    # own uplink is the ceiling, so N workers on one box contend for one saturated link while N
    # across boxes is ~linear. Set to 1 to disable the fan-out and take the serial path, which is
    # kept and tested.
    # WHY THIS PHASE: ship was 37% of a poll cycle whose median is 20.8 min, and `ship_budget_spent`
    # fired on EVERY pass (4-12 shipped, 6-31 deferred). What that cost is not coordinator seconds
    # but IDLE HARDWARE — median 52 tasks `running` against 115 `claimed`-and-waiting, median
    # add->start 36 min (mean 82, p90 3.6 h) on boxes that were measurably not the constraint.
    "ship_parallel_boxes": 8,
    "poll_seconds": 30,
    "pull_margin_min": 10,
    "est_safety": 1.25,
    # 30, not the original 15: this was never CONSUMED until 2026-07-28 (invariant 10b), so its
    # value had no empirical basis. Measured over 2197 real ship->start pairs: p50 1.5min,
    # p90 6.9min, p99 22.9min, with the legitimate tail topping out at 27.5min before jumping to
    # 49min+. A 15min timeout would have destroyed 63 healthy boxes (2.87% of ships) that went on
    # to start normally — the worker logs `start` only AFTER `pip install` of the job's setup
    # extras, so a slow install legitimately looks identical to a dead worker. 30min is ~2x p99,
    # clears that entire cluster (8 false positives, 0.36%), and still caps the leak this reaps
    # at half an hour instead of the 7.5h observed.
    "claim_timeout_min": 30,
    # Invariant 7c — wall-clock cap on ONE poll's ship phase. MEASURED, not guessed (2026-07-29):
    # compile is 89% cache hits at ~10s but 11% MISSES at a median 155s / max 272s, and per-task
    # ship cost (compile+push) is median 30s, p90 135s, max 366s. `_ingest_and_complete` runs at the
    # TOP of a serial `poll_once`, so an unbounded ship phase postpones the next one by its own
    # length — and that is where every worker.jsonl pull, checkpoint pull and reaper lives. Observed
    # unbounded: 10 ships in 15 min with ZERO starts/dones and `box_measured` 27 min stale.
    # 300s keeps the whole cycle near the ~7 min p50 this fleet historically ran at, while
    # `_ship_all`'s always-ship-at-least-one rule keeps a single 366s task from being starved by a
    # budget smaller than itself.
    "ship_budget_sec": 300,
    # inv. 10d(fast): consecutive SHIP failures on one box before it is ship-quarantined. Arming was
    # previously reachable ONLY from `_reap_undeliverable_claims`, which requires a task to sit
    # CLAIMED past `undeliverable_after_min` and then be requeued — so a box that failed delivery
    # SLOWLY was caught and a box that failed FAST was invisible. Measured 2026-08-30 on instance
    # 40000055: a poisoned blob cache failed every ship in ~11s, so tasks went straight to
    # `task_failed`, `requeued` stayed 0, the quarantine never armed, and the box ate 39 tasks
    # across two unrelated sessions while staying `live` and winning placements.
    # 3, not 1: a single failure is a blip and barring a box on one is how a healthy box gets
    # stranded. A successful delivery clears the counter AND lifts the quarantine, so a recovering
    # box needs no operator action.
    "ship_quarantine_after_fails": 3,
    # inv. 20i: how often each live box's worker code is refreshed on disk. The worker re-execs
    # itself when it changes, so this is the whole update cadence.
    "worker_refresh_min": 30,

    # Invariant 10d — how long a task may sit `claimed` on a live box we are demonstrably FAILING to
    # deliver to before it is requeued elsewhere. The complement of `claim_timeout_min`: that one
    # covers "we delivered, the worker never claimed"; this one covers "we can never deliver", which
    # NO reaper watched (10b requires state `shipped` and additionally skips any box with
    # `consecutive_fails > 0`, which an undeliverable box always has; 10c needs a HEARTBEAT, and the
    # worker here is alive and well; 19/19h only look at `running`/`shipped`).
    # MEASURED over the 44 tasks that ever recovered from >=1 `ship_failed`: claim->ship clusters at
    # 8.5-58.6min (34 of 44) and then jumps to 124.0min. 90 sits in that gap — ~1.5x the legitimate
    # cluster's max, so it cannot requeue work that was merely slow, while still bounding the leak.
    # Note the cluster is itself INFLATED by the two bugs fixed alongside this (non-resumable pushes
    # + serial ship starvation); post-fix a real recovery takes a poll or two, so the real margin is
    # far wider than 1.5x. Deliberately NOT keyed on failure COUNT: owned box -1 recovered from
    # streaks of 311 and 284 consecutive ship failures (a laptop that goes away and comes back), so
    # a count threshold would requeue healthy work off a box that was about to be fine.
    "ship_timeout_min": 90,
    # 15, not 5: this is compared against OUR PULLED COPY's mtime, so it must exceed the poll
    # CYCLE time (the copy can only refresh once per cycle), not the worker's 60s touch interval.
    # MEASURED on the live fleet: the pulled copy reaches 9.3 min of age, i.e. one poll CYCLE
    # (per-instance rsyncs + compiles + ships) runs ~9 min, so at a 5-min threshold the copy was past
    # it every single cycle — sampled ageing 53 -> 278s
    # with no refresh — and the reaper fired on demonstrably healthy boxes (worker alive, heartbeat
    # touched every 60s, verified over ssh), requeueing 8 tasks across three campaigns. A dead
    # worker stops touching HEARTBEAT permanently, so 15 min detects it just as surely while
    # leaving room for a slow cycle. Belt-and-braces with `seconds_since_heartbeat_pull` (which
    # refuses to reap on an unconfirmed read) and `_refresh_heartbeats` (which pulls it first).
    "heartbeat_stale_min": 15,
    "hard_cap_hours": 48,
    # 2026-07-10 spend audit: `_provision` runs INLINE in the poll loop (invariant 5), so reconcile
    # can never observe a box mid-provision — a row still `provisioning` across polls is always an
    # orphan (its daemon died mid-`_provision`) that will never come up on its own. 45 min just
    # billed each such orphan for 30 extra min. Median successful provision is <2 min, p90 ~4 min,
    # so 15 catches orphans fast with a wide margin over any legitimate provision.
    "provision_timeout_min": 15,
    "stall_timeout_min": 90,
    # Invariant 5: retry the post-`actual_status==running` ssh probe before judging a fresh box
    # dead. sshd/the Vast proxy often aren't accepting connections for 20-60s after the container
    # is "running"; a single 20s probe that fails then PERMANENTLY denies the machine turned
    # transient readiness blips into a doom loop (98 machines banned, zero fleet growth, 2026-07-15).
    "ssh_probe_attempts": 5,
    "ssh_probe_interval_s": 15,
    # Invariant 5: how long to wait for a freshly-rented box to reach actual_status=running before
    # giving up. Set to the empirical p99 boot time (2026-07-15: median 1.7min, p90 4.2, p99 20.3,
    # max 29.9 over 144 successful boots) — abandons only the ~1.4% slowest-booting boxes while
    # capping the worst-case wait. Was a hardcoded 40min (160 x 15s).
    "provision_boot_max_min": 21,
    # Invariant 5c: CONSECUTIVE never-reached-running failures on ONE machine before it is denied.
    # 5b tolerates a single such failure on purpose (a transient slow image pull must not ban a good
    # host — that is the 2026-07-15 doom loop above), but nothing counted REPEATS, so a host that
    # can never boot was re-rented forever: it is destroyed, the offer is still the cheapest by
    # value density, and `place` picks it again on the very next poll.
    # MEASURED 2026-08-16 — machine 100004 (offer 40000002, RTX 3080 Ti):
    #   7 rentals 17:12 -> 20:19, EVERY ONE with ssh_host NULL, ZERO successes, each destroyed at
    #   the ~21-min boot ceiling and re-rented within ~20 s. 7 of the last 8 fleet-wide rentals and
    #   7 of the last 8 teardowns were this one machine; the fleet grew by ZERO boxes in 3 hours
    #   while ~10 cells sat queued, at ~$0.022 per attempt.
    # 3 strikes = ~63 min of the host proving it cannot boot, with no success in between. Strictly
    # more conservative than the neighbouring 5b branch, which denies permanently after ONE failure.
    "stuck_provision_strikes_before_deny": 3,
    # Invariant 19h: how long a task may sit `shipped` on a live, worker-alive box before it's
    # judged gate-held by the box's launch gate (the box's real concurrency is below its advertised
    # slots_total) and requeued. Generous — covers a slow launch/compile/stagger; only a genuinely
    # stuck task exceeds it. Learned per-machine caps live in `overpack_cap_*` settings keys.
    # 20 -> 35 (2026-08-01). THE GRACE MUST EXCEED THE TIME THE BOX NEEDS TO FILL ITS OWN SLOTS,
    # and at 20 it did not: `sweep_supervisor.AUTO_DEFAULTS["settle_minutes"]` is 3.0, so the
    # on-box launch gate starts ONE lane per 3 minutes and an 8-slot box takes **24 minutes** to
    # fill — 4 minutes past the old grace. `_reap_overpacked_boxes` therefore read "running <
    # advertised" on a box that was merely still filling, and ratcheted its learned concurrency
    # down PERMANENTLY (`_learn_overpack_cap` only ever takes min()). That is a pre-existing defect
    # — it explains the older `overpack_cap` entries sitting at 1, 4 and 5 — which the 2026-07-31
    # `max_slots_cap` 8 -> 16 attempt turned from occasional into systematic (16 lanes x 3 min =
    # 48 min against a 20 min grace ⇒ five unschedules in 30 minutes, the owned laptop driven from
    # a manually-validated 6 to 1). 35 clears 8 x 3 = 24 with margin for ship/compile jitter.
    #
    # The asymmetry that sets the direction: a FALSE POSITIVE here permanently pins a machine (the
    # cap is keyed per machine_id and survives teardown, so it poisons every future rental of that
    # host), while a false negative merely delays detecting a genuinely over-packed box by 15 min.
    # Round toward the cheap error. If `settle_minutes` or `max_slots_cap` ever changes, re-check
    # this product — it is the invariant, not the number.
    "ship_launch_grace_min": 35,
    # Invariant 8b: THE WORKER'S LAUNCH PACING, OWNED HERE. These were constants in box-side code
    # (`sweep_supervisor`), so changing one meant shipping worker code to every box — while the
    # setting above was tuned against `settle_minutes` without owning it. The coordinator now
    # pushes this to every box on its measure probe (`box_assert_cmd`) and the worker reads the
    # pushed copy, which doubles as the box's cache while the coordinator is unreachable. Read
    # LIVE from the DB at push time, so an edit needs no restart. The values are the constants the
    # box used to carry; `tests/test_dispatcher.py` holds them equal to the worker's built-ins.
    #   settle_minutes     minutes between launches on a box that is not measurably idle
    #   settle_floor_min   ...and the shorter gap an idle box may use instead
    #   settle_idle_frac   "idle" = load1 below this share of the box's cores
    #   util_ceiling       refuse a launch at or above this GPU utilisation (%)
    #   cpu_reserve_cores  refuse once load1 is within this many cores of the box
    #   vram_lane_mult     free VRAM needed = this x the largest lane seen so far...
    #   vram_free_frac     ...or this share of the card before any lane has been measured
    "launch_gate": {"settle_minutes": 3.0, "settle_floor_min": 0.5, "settle_idle_frac": 0.5,
                    "util_ceiling": 90.0, "cpu_reserve_cores": 1.0, "vram_lane_mult": 1.25,
                    "vram_free_frac": 0.2},
    # Invariant 19h-3: consecutive 19h-2 evidence-floor refusals before the floor is overridden and
    # the learn allowed through. 2 = two attempts. Each strike is already past the
    # "a lane launched within the grace" guard, so 2 means NO lane started in ~70 min while the box
    # held gate-held work — at which point "still filling" is refuted by repetition. Set higher to
    # be more forgiving of a slow box, 1 to disable the floor's protection entirely (don't).
    "overpack_refusals_before_override": 2,
    # -- worker rolling upgrade (worker-rolling-upgrade.spec.md R4.1/R6.2) --
    # How many boxes may hold for an upgrade at once. 1 = a true ROLL: every box goes stale the
    # instant a worker change lands, so an uncapped hold parks the WHOLE fleet and triggers a rent
    # stampede — strictly worse than the bug it fixes. ⚠ READ LIVE from the DB by
    # `_worker_roll_max_draining`, NOT from this snapshot, so an operator can widen the roll to push
    # something out urgently WITHOUT restarting the coordinator (owner, 2026-08-03).
    "worker_roll_max_draining": 1,
    # The ONE identity every owned box is reached with (`_sync_ssh_config`). A box overrides it with
    # `ssh_key_i<id>` only when it genuinely needs its own; per-box keys otherwise bound nothing,
    # since every private key lives in this container anyway (owner, 2026-09-17).
    "fleet_ssh_key": "fleet_ed25519",
    # Bound on the hold (R6.2). Measured task runtime: median 1.1h, p90 4.8h, p99 13.3h (n=2304), so
    # 12h clears p99 — past that the occupant is wedged rather than long, and holding the roll's only
    # slot for it stalls every other box's upgrade behind it. Expiry returns the box STILL STALE.
    "worker_roll_hold_max_min": 720,
    # Invariant 19h-3: how long a struck-out box takes no new work. Defaults to one
    # `ship_launch_grace_min` — the window the reaper already judges a gate-held task by, so no new
    # timescale is invented. ⚠ MEASURED CAVEAT: on 2026-08-02 the per-box burn cadence was HOURS
    # (m100006 struck at 11:25, 14:42, 15:20), so a 35-min window caught only 4 of that day's 14
    # burns in replay. Raise it if the burns persist — the cooldown is self-expiring and cleared the
    # moment the box launches, so a longer window costs idle capacity only on a box that is not
    # launching anyway. It is a separate knob precisely so it can be tuned without moving the grace.
    "overpack_cooldown_min": 35,
    # Task-bundle packaging (docs/specs/task-bundle.spec.md). Both default OFF so the shipped
    # default path is the fully-tested source bundle; each is opt-in and degrades gracefully.
    "bundle_sign": False,        # sign manifests (needs DISPATCHER_BUNDLE_SIGN_KEY + a pubkey)
    "bundle_compile": True,      # compile code to .so before shipping (bar-raising, not secrecy)
    "bundle_compile_backend": "local",  # "local" (coordinator toolchain; no Docker) | "docker"
    "bundle_compile_image": BOX_IMAGE,  # ABI target / build image for backend="docker"
    # The box image (BOX_IMAGE) runs Python 3.12, so compiled .so must be cpython-312. The local
    # backend refuses to build unless its interpreter's EXT_SUFFIX matches this, so a build
    # interpreter of the wrong version fails loud -> fall back to source (spec inv. 6). Because the
    # coordinator's own interpreter is 3.11, `bundle_compile_python` must point at a 3.12 build
    # interpreter (e.g. one from `uv python install 3.12`) for compilation to actually engage.
    "bundle_compile_abi": "cpython-312-x86_64-linux-gnu",
    "bundle_compile_python": None,      # base interpreter for the build venv; None -> sys.executable
    "bundle_compile_packages": ["src/native", "src/shared"],
    # LRU cap, applied PER cache dir (compile-everything trees AND overlaid ship trees).
    # 24 -> 64 (2026-07-31). Two corrections in one: the "~11MB each" written here was measured at
    # **36MB**, and 24 entries was too FEW to cover the fleet's snapshot churn — 227 distinct code
    # snapshots over 3 days (~76/day, one per active worktree per commit) against a 24-entry cap
    # evicted trees that were still in live use. Measured consequence: 18 commits took a SECOND cold
    # compile (`M…H…M` in the compile event stream — hits after the first miss prove it was eviction,
    # not a race), 22 redundant rebuilds at ~171s = 63 min of `ship_budget_sec` over 3 days.
    # 64 holds most of a day's churn. Cost is disk only, and it is noise: 64 x 36MB x 2 dirs = 4.6GB
    # against 389GB free on this volume.
    "bundle_compile_cache_max": 64,
    # 2026-07-10 spend audit: rent strictly-cheapest was picking flaky hosts (measured: GTX 1660
    # 4/6 lost, GTX 1070 Ti 5/5 dead) — ~12% of spend went to boxes that vanished or never ran.
    # Drop offers whose Vast reliability is below this floor (fail-open if the field is absent).
    "min_reliability": 0.90,
    # Invariant 20h: consecutive ssh/rsync failures against a `source='owned'` box before it's
    # soft-quarantined (`live -> unreachable`) so a dead home-farm box can't wedge the fleet. The
    # same ConnectionTracker counter every pull/ship feeds; 3 consecutive hard failures (each with
    # its own internal retries) on a LAN box is a solid dead signal, and recovery is one poll away.
    "owned_unreachable_fails": 3,
    # Box-pause spec: a `source='owned'` box put in a SOFT pause (`box_pause.py pause` — frozen in
    # place, work NOT requeued) auto-escalates to a hard drain after this many minutes, so a
    # forgotten short pause can't strand the laptop's jobs frozen forever. 30 min matches the
    # operator's "back soon, but requeue if I forget" contract.
    "soft_pause_timeout_min": 30,
    # Cost consolidation (invariant 21, 2026-07-22 owner directive). Placement packs owned-first and
    # teardown releases IDLE boxes, but a task that landed on a PAID box while the owned box was busy
    # rides that box to completion even after the owned box frees up. This pass gracefully drains a
    # paid box whose whole load can move onto strictly-cheaper reclaimed capacity, so the box goes
    # idle -> torn down. Reuses the invariant-17 graceful PREEMPT path (checkpoint-then-exit).
    "consolidate_enabled": True,
    # Don't drain a task within this many minutes of its `est_minutes × est_safety` window — the
    # checkpoint-cycle + re-warm cost exceeds the savings for near-done work (invariant 21d).
    "consolidate_min_remaining_min": 30,
    # Invariant 21g — a drain must EARN its disruption (owner directive 2026-07-29: "we do want
    # repacking to happen, but it should be as little disruption as practical - saving a few cents
    # on a repack is penny wise, pound foolish").
    #
    # MEASURED over 95 real drains (2026-07-28/29): the median drain preempted **10 tasks** to save
    # an UPPER-BOUND $0.174 — and that bound uses the full `est_minutes` window, which runs ~3.4x
    # long, so the real saving was nearer 5 cents. Ten interrupted runs for five cents.
    #
    # The bar is PER TASK PREEMPTED, because that is the unit of disruption: a preempt costs the
    # run's progress since its last checkpoint, a re-ship (median 30s, p90 135s), and — the part
    # that cannot be priced — one more resume cycle, which makes that arm less comparable to its
    # siblings (resume does not reproduce; see the run-registry spec + m50_bypass2). $0.05/task
    # against a ~1-2c mechanical cost leaves margin for the unpriceable half.
    #
    # Replayed against those 95 drains: $0.05/task alone allows 19, cutting 1201 preempts to 65.
    "consolidate_min_savings_per_task_usd": 0.05,
    # Anti-repack hysteresis. Repack latency is MEDIAN 5 MIN (p90 30, max 149) — far inside the
    # 10-min idle timeout a drain needs to end in a teardown, so on a busy fleet the box is refilled
    # before it can be reclaimed and the drain is pure loss. 60 min blocks 94% of observed repacks.
    # It only ever penalises a box that SURVIVED a drain: one that was torn down is gone.
    # With the per-task rule this takes 95 drains -> 10 and 1201 preempts -> 33 (98% less churn).
    "consolidate_cooldown_min": 60,
    # Invariant 21j — a HARD CEILING on how many runs one reclaim may interrupt. The owner, after
    # 21g/21h/21i were all live: "I still keep hearing about preempts that cause trouble for the
    # task owner." They were right, and the earlier gates could not have fixed it: 21a requires a
    # WHOLE-BOX drain, so a full box means one preempt per occupant, and the per-task value test
    # (21g) still clears a 10-task drain whenever the box has a long enough remaining life.
    #
    # MEASURED over 24h / 81 drains / 313 preempts: consolidation caused **93.4% of every preempt in
    # the fleet**, 27 of which LOST their checkpoint outright, and bought an upper-bound $3.51 —
    # **~$0.011 per preempt inflicted.** One cent per interrupted run, against the owner's own
    # standard that a few cents is already penny wise and pound foolish.
    #
    # The distribution is what makes a ceiling the right instrument rather than a finer price:
    # **a drain that SUCCEEDED never preempted more than 8 tasks and the median was 3**, while the
    # futile ones ran 20-77. Big drains are almost pure harm. Replayed against those 81 drains, a
    # cap of 4 keeps 19 of 31 reclaims and $2.10 of $3.51 while avoiding **215 of 313 preempts** —
    # 60% of the money for 31% of the disruption. Occupant count is known at decision time and is
    # exactly the number of preempts a whole-box drain will cause, so this is not an estimate.
    "consolidate_max_preempts": 4,
    # Invariant 21h — the DRAIN HOLD, and the actual fix for the drain-repack race (21g reduced how
    # OFTEN we drain; it did nothing about a drain undoing itself).
    #
    # A drain requeues its occupants. Those tasks are then ordinary `queued` work, and the box they
    # just left is — by construction — a perfect fit for them, so the very next `_place_queue` ships
    # them straight back. Measured 2026-07-29 on box 40000015: drained 16:51, and 3 of its 5
    # preempted tasks were running on it again by 16:57, WITH THE QUEUE AT ZERO. Fleet-wide, 79 of
    # 111 drains ever (71%) were repacked before any teardown. The drain cost 5 preempts and
    # reclaimed nothing; one victim (`azsc-p1e/…seed3`) reached 11 resume cycles against siblings at
    # 7-9, wrecking that campaign's comparability (resume does not reproduce).
    #
    # So a drained box is held OUT of the placement pool until it empties and is torn down. The hold
    # is time-bounded, because a box that cannot empty must return to service rather than idle-bill:
    # MEASURED over the 31 clean drains (never repacked, ended in teardown), drain->teardown is
    # median 13.2 min, p90 33.5, p95 42.2. 40 min covers 90% of them and stays UNDER
    # `consolidate_cooldown_min` (60), so a box released from hold still cannot be re-drained
    # immediately — the two guards compose instead of oscillating.
    "consolidate_drain_hold_min": 40,
    # VRAM headroom a target must have BEYOND the drained box's measured footprint before a drain is
    # initiated (invariant 21c) — the packer is slot-count-only/VRAM-blind, so this is what prevents
    # a consolidation from OOMing the target. Only gates when both sides are measured (invariant 22).
    "consolidate_vram_margin_gb": 1.0,
    # Invariant 22: how often to sample each live box's real GPU memory (`nvidia-smi`), so packing /
    # consolidation run on measured VRAM instead of the static `vram_per_lane_gb` hint. Best-effort,
    # in-memory (a restart just re-measures within a cadence).
    "resource_measure_every_min": 5,
    # box-pause inv. 20b: re-push an owned box's capacity schedule to its host enforcer at least this
    # often even when unchanged, so a recreated worker container is healed without a restart.
    "capacity_push_every_min": 30,
}
DEFAULT_PRIORITY = 50  # registry spec schema default — invariant 4d's bypass threshold

# Guarded default migrations (key, superseded_default, new_default): on startup, a settings row
# still sitting on a SUPERSEDED default is moved to the new default; an operator-customized value
# (anything other than the old default) is left untouched. This is how a lowered default reaches an
# already-seeded live registry without a manual SQL edit or clobbering a hand-tuned value.
_SETTING_MIGRATIONS = (
    # 8 -> 11 (2026-08-04). WITHOUT THIS ROW THE RAISE IS A SILENT NO-OP, which is the exact trap
    # the three rows below were each written for: `_ensure_settings` only INSERTs keys the registry
    # LACKS, and `max_slots_cap` has been seeded at 8 since July — so the live fleet would keep
    # sizing every box at 8 lanes forever, a `dispatch-restart` would change nothing, and the code
    # default, the spec and the tests would all agree on 11 while the running system did 8. Verified
    # against the live registry before adding: it held 8. Leaves a deliberately customized value
    # alone (the migration matches only the superseded default).
    ("max_slots_cap", 8, 11),
    ("provision_timeout_min", 45, 15),
    # 5 -> 15: the old value was sized against the WORKER's 60s HEARTBEAT touch, but it is compared
    # against our PULLED COPY, whose freshness is bounded by the POLL CYCLE (MEASURED ~9 min on a
    # busy fleet — do not lower this without re-measuring that). It tripped every cycle on healthy boxes. Without this row the code default never
    # reaches an already-seeded registry: `_ensure_settings` only INSERTs keys it does not have, so a
    # live DB keeps 5 forever and a restart changes nothing (observed — the reaper kept logging
    # ">= 5" for six minutes after restarting onto the new default).
    ("heartbeat_stale_min", 5, 15),
    # 15 -> 30, for the same reason as the row above: the live registry was seeded with 15 back when
    # NOTHING consumed this key, so `_ensure_settings` would keep 15 forever and invariant 10b would
    # deploy against an unvalidated number. 30 is measured — see the `claim_timeout_min` default.
    ("claim_timeout_min", 15, 30),
    # Owner directive 2026-07-30 ("disable preempts"). `preempt_enabled` is a NEW key so
    # `_ensure_settings` inserts its False default on its own, but `consolidate_enabled` was seeded
    # True back in July and `_ensure_settings` only INSERTs keys it lacks — without this row the live
    # registry would keep True forever and the dashboard/`runq` would still report consolidation as
    # ON. (It could not actually drain, since `consolidation_drains` now checks the global switch
    # first; this row is so the two knobs cannot disagree about what the fleet is doing.)
    ("consolidate_enabled", True, False),
    # Owner directive 2026-07-30: raise the hourly ceiling 1.00 -> 1.50. Same reason every row here
    # exists — the live registry was seeded with 1.0 in July, and `_ensure_settings` only INSERTs
    # keys it lacks, so changing the code default alone would leave the running fleet capped at 1.0
    # forever and this change would silently do nothing.
    ("max_hourly_usd", 1.00, 1.50),
    # Owner directive 2026-07-31: the GPU-class ceiling, 0.40 -> 0.08 (invariant 4f — the rationale
    # and the measurements are on the DEFAULT_SETTINGS entry). Same reason as every row above, and
    # CAUGHT THE HARD WAY: a smoke test against a copy of the live registry read `max_instance_dph
    # 0.4` from the settings table AFTER the code default was changed to 0.08 — `_ensure_settings`
    # only INSERTs keys it lacks, so the running fleet would have kept renting $0.17 V100s forever
    # and the cap would have been a silent no-op. Exactly the "it exists but never fires" failure
    # this whole change set was written to fix.
    ("max_instance_dph", 0.40, 0.08),
    # 24 -> 64, same reason as every row above: the live registry was seeded with 24, and
    # `_ensure_settings` only INSERTs keys it lacks, so raising the code default alone would leave
    # the running fleet evicting compile trees it is about to need. See the default's own note for
    # the measurement (18 commits recompiled after eviction, 63 min over 3 days).
    ("bundle_compile_cache_max", 24, 64),
)


# --------------------------------------------------------------------------------------------
# Pure decision core (spec Public API) — no I/O, everything sampled/injected.
# --------------------------------------------------------------------------------------------

@dataclass
class Placement:
    action: str  # "pack" | "preempt" | "rent" | "hold"
    target: object = None  # instance id for pack/preempt
    victims: list | None = None  # task ids, for preempt
    reason: str = ""
    offer: dict | None = None  # the chosen offer, for rent
    # Invariant 4i: None for every ordinary placement. A list (possibly EMPTY) marks a FORCED pack —
    # one entry per admission gate that would have refused the task, with the numbers that decided
    # it. `_apply_placement` turns it into the `forced_placement` audit event.
    bypassed: list | None = None


def _occupant_slots(inst: dict) -> int:
    return sum(o["slots"] for o in inst.get("occupants", []))


def _free_slots(inst: dict) -> int:
    return inst["slots_total"] - _occupant_slots(inst)


def _window_minutes_needed(task: dict, settings: dict) -> float:
    return task["est_minutes"] * settings["est_safety"] + settings["pull_margin_min"]


def lane_vram_gb(hint: dict | None, settings: dict) -> float:
    """Invariant 26m — the VRAM ONE lane of this task is charged. 0.0 means the task does not use
    the GPU, and then NOTHING about the card gates it: not headroom (23), not a window's VRAM
    budget (18a), not rent sizing (27), not the box's own launch gate (8c).

    A task uses the GPU when it says so — `requires_gpu`, or a positive `vram_per_lane_gb` — or
    when its own group has been measured doing so (`res_defaults.resolve_hint` writes that
    measurement into the effective hint only above `GPU_USE_MIN_VRAM`).

    THE INCIDENT (2026-10-02..03): ~9.4 GB of non-fleet VRAM on a 12 GB owned laptop put measured
    VRAM headroom at -0.2 GB by day, and every task was charged the settings lane (0.6 GB) whether
    it touched the card or not — so a 32-core box refused a queue of CPU-only work for 31 hours."""
    h = hint or {}
    v = h.get("vram_per_lane_gb")
    v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    if v > 0:
        return v
    return float(settings["vram_per_lane_gb"]) if requires_gpu(h) else 0.0


def task_footprint(hint: dict | None, slots: int, settings: dict) -> tuple[float, float]:
    """A task's declared resource footprint `(cores, vram_gb)` = its per-lane hint x `slots`. Cores
    fall back to the global settings lane when it declares none; VRAM is `lane_vram_gb` — 0.0 for
    a task that does not use the GPU (invariant 26m). Pure (invariant 18a)."""
    h = hint or {}
    return (float(h.get("cores_per_lane", settings["cores_per_lane"])) * slots,
            lane_vram_gb(h, settings) * slots)


# One ssh, every axis. Every line is KEYED (`NPROC 20`), never positional, and the parser ignores any
# line whose key it does not know. That is not tidiness — a positional format is unsafe here, because
# **`nvidia-smi` writes its failure text to STDOUT**: on the NVML-blocked desktop (owned box -2) the
# GPU arm emits *four* lines ("Failed to initialize NVML: GPU access blocked by the operating
# system", a shutdown warning, a blank, then the fallback), which silently shifts every field after
# it. Measured live 2026-07-31: a positional draft of this probe read that box's CPU-time counter as
# its anonymous memory and reported **520 GB of RSS on a 31 GB machine**. Keys make stray output
# inert instead of corrupting.
#
# `NPROC`/`LOAD`/`MEM`/`GPU` carry the ORIGINAL probe's four values with their meaning frozen —
# `box_headroom` (invariant 23) admits against them, so re-pointing them at a different denominator
# would silently re-tune live admission. `free -m` for portability (older coreutils lack `--giga`).
#
# `CPUQ`/`CPUU`/`MEMCG`/`MEMANON` are the CONTAINER-TRUE axes added for invariant 25 (fleet
# performance observability), and they exist because the four above measure the HOST, not our slice
# of it. Measured live on rental 40000039: `nproc` 96 and `free -m` 94 GB, while that container's OWN
# cgroup allowed 18.43 CPUs and 40.4 GB. Reading "load 13.90/96 cores" as 14% busy understated our
# occupancy of what we PAY FOR by 5.2x — a fleet-saturation dashboard built on that denominator would
# be confidently wrong. `/proc/loadavg` is likewise the host's run queue, so it counts other tenants'
# processes as ours; `CPUU` is the only per-container CPU signal, and it is cumulative (a RATE needs
# two samples — see `_measure_box_resources`).
#
# Both cgroup layouts are live in the fleet TODAY (40000039 is v1, 40000032 is v2), so every read
# tries v2 then v1 then prints `-`; a box that answers `-` everywhere degrades exactly to the pre-25
# behavior. Nothing here can fail the command: each arm ends in a fallback.
BOX_PROBE_CMD = (
    "echo NPROC $(nproc); "
    "echo LOAD $(cat /proc/loadavg); "
    "free -m | awk '/^Mem:/{print \"MEM\", $2, $7}'; "
    # Prefixed per line, with NO `||` fallback: a box with no GPU, or one whose driver is shouting
    # prose at stdout, simply produces no parseable GPU line and that axis reads unmeasured.
    "nvidia-smi --query-gpu=memory.total,memory.used,utilization.gpu,utilization.memory,name "
    "--format=csv,noheader,nounits 2>/dev/null | awk '{print \"GPU\", $0}'; "
    # CPU quota in cores ('-' when unlimited/unreadable -> the host core count stands in)
    "if [ -r /sys/fs/cgroup/cpu.max ]; then "
    "awk '{if($1==\"max\")print \"CPUQ -\";else printf \"CPUQ %.4f\\n\",$1/$2}' "
    "/sys/fs/cgroup/cpu.max; "
    "elif [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then "
    "awk 'NR==FNR{q=$1;next}{if(q<=0)print \"CPUQ -\";else printf \"CPUQ %.4f\\n\",q/$1}' "
    "/sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/cpu/cpu.cfs_period_us; fi; "
    # Cumulative container CPU time, microseconds (v1's cpuacct is nanoseconds -> /1000)
    "if [ -r /sys/fs/cgroup/cpu.stat ]; then "
    "awk '/^usage_usec/{print \"CPUU\", $2}' /sys/fs/cgroup/cpu.stat; "
    "elif [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then "
    "awk '{printf \"CPUU %d\\n\",$1/1000}' /sys/fs/cgroup/cpuacct/cpuacct.usage; fi; "
    # Container memory 'current limit' in bytes ('max' = unlimited, v1 uses a huge sentinel)
    "if [ -r /sys/fs/cgroup/memory.current ]; then "
    "echo MEMCG $(cat /sys/fs/cgroup/memory.current) "
    "$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max); "
    "elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then "
    "echo MEMCG $(cat /sys/fs/cgroup/memory/memory.usage_in_bytes) "
    "$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo max); fi; "
    # Anonymous (non-reclaimable) container memory — `memory.current` counts page cache, which makes
    # a box look near its limit while every byte over the working set is droppable.
    "if [ -r /sys/fs/cgroup/memory.stat ]; then "
    "awk '/^anon /{print \"MEMANON\", $2}' /sys/fs/cgroup/memory.stat; "
    "elif [ -r /sys/fs/cgroup/memory/memory.stat ]; then "
    "awk '/^total_rss /{print \"MEMANON\", $2}' /sys/fs/cgroup/memory/memory.stat; fi")

def box_assert_cmd(frozen: bool, launch_gate: str | None = None) -> str:
    """The shell prefix that RE-ASSERTS coordinator-owned state on a box. Sent ahead of
    `BOX_PROBE_CMD` in the measure loop's own ssh call, so it costs no connection (box-pause 14a).

    A box holds exactly one piece of pause state — the `~/spool/FREEZE` marker — and it is a
    PROJECTION of the registry, not a fact of its own. It used to be written once, by the verb that
    changed the state, over a best-effort ssh; when that one call did not land the box and the
    registry disagreed forever, silently. Asserting it on every probe makes the disagreement last
    at most one measure interval.

    The first line reports what was there BEFORE the assert (`FRZ 0|1`), which is how a drift is
    seen and logged rather than quietly papered over. `parse_box_probe` drops lines it does not
    know, so the report rides the probe's output without touching the measurement.

    `launch_gate` (invariant 8b) is the coordinator's launch pacing as canonical JSON. The box's
    copy, `~/spool/launch_gate.json`, is rewritten only when it differs, and the box says which
    happened (`LG same|updated`). Base64 so the JSON never meets a shell quoting rule, and written
    to a temp file then moved, so the worker never reads half of it."""
    cmd = ("if [ -e ~/spool/FREEZE ]; then echo FRZ 1; else echo FRZ 0; fi; "
           + ("mkdir -p ~/spool && touch ~/spool/FREEZE; " if frozen
              else "rm -f ~/spool/FREEZE; "))
    if launch_gate is not None:
        import base64
        b64 = base64.b64encode(launch_gate.encode()).decode()
        f = "~/spool/launch_gate.json"
        cmd += (f"mkdir -p ~/spool && echo {b64} | base64 -d > {f}.tmp && "
                f"if cmp -s {f}.tmp {f}; then rm -f {f}.tmp; echo LG same; "
                f"else mv -f {f}.tmp {f} && echo LG updated; fi; ")
    return cmd


def parse_freeze_report(text: str) -> bool | None:
    """Whether the box's FREEZE marker existed before `box_assert_cmd` ran, or None if the box did
    not say (an older probe, a truncated reply) — in which case nothing is concluded."""
    for ln in (text or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] == "FRZ" and parts[1] in ("0", "1"):
            return parts[1] == "1"
    return None


def parse_launch_gate_report(text: str) -> str | None:
    """`"same"` / `"updated"` — what `box_assert_cmd` did to the box's launch-pacing copy — or None
    if the box did not say (the write failed, or the reply was cut short)."""
    for ln in (text or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] == "LG" and parts[1] in ("same", "updated"):
            return parts[1]
    return None


def launch_gate_payload(setting) -> str:
    """The canonical JSON the coordinator pushes for its `launch_gate` setting (invariant 8b).

    The settings table is hand-editable, so this is a trust boundary of its own: only the known
    keys travel, each must be a real number, and one that is missing or is not takes the code
    default. Canonical form (sorted keys, fixed separators) is what lets the box compare its copy
    byte-for-byte and report `same`."""
    default = DEFAULT_SETTINGS["launch_gate"]
    given = setting if isinstance(setting, dict) else {}
    out = {}
    for key, dflt in default.items():
        v = given.get(key, dflt)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
        out[key] = float(v if ok else dflt)
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


# A cgroup v1 "unlimited" memory limit is a sentinel near 2^63, not a real number. Anything past this
# is not a cap we could ever hit, so it reads as absent rather than as a 8-exabyte allowance.
_CGROUP_UNLIMITED_BYTES = 1 << 53  # 8 PiB


def _probe_num(tok: str) -> float | None:
    """One optional numeric field from the probe: `-`/`max`/junk -> None (that axis is unmeasured)."""
    try:
        v = float(tok)
    except (TypeError, ValueError):
        return None
    return v


def parse_box_probe(text: str) -> dict | None:
    """Parse `BOX_PROBE_CMD` output into measured box resources (invariants 22/23/25). Pure. Returns
    None if the CPU/RAM lines are unusable — a box we cannot measure carries NO measurement and the
    headroom gate then abstains, rather than guessing (same best-effort convention as invariant 22).

    Keyed, order-independent, and tolerant of unknown lines by construction: the probe runs on boxes
    whose drivers print prose to stdout (see `BOX_PROBE_CMD`), so anything unrecognised is dropped
    rather than consumed as the next field's value.

    GPU is optional and independent: an absent or unparseable GPU line still yields a usable CPU/RAM
    measurement, because a GPU that is absent or blocked must not cost us CPU admission — the
    desktop's GPU was NVML-blocked for a whole day while its CPUs were perfectly schedulable.

    Every invariant-25 field (`cpu_quota_cores`, `cpu_usage_usec`, `mem_limit_gb`, `mem_used_gb`,
    `mem_anon_gb`, `gpu_mem_util`, `gpu_name`, `gpu_count`) is independently optional and defaults to
    None — an older box image, a cgroup layout we don't recognise, or a truncated reply costs only
    that field. The HOST-basis keys keep their exact pre-25 meaning and units."""
    keyed: dict = {}
    gpu_lines: list = []
    for ln in (text or "").splitlines():
        parts = ln.strip().split()
        if not parts:
            continue
        if parts[0] == "GPU":
            gpu_lines.append(" ".join(parts[1:]))
        elif parts[0] in ("NPROC", "LOAD", "MEM", "CPUQ", "CPUU", "MEMCG", "MEMANON"):
            keyed.setdefault(parts[0], parts[1:])
    try:
        cores = int(float(keyed["NPROC"][0]))
        load1 = float(keyed["LOAD"][0])
        ram_total_mb, ram_avail_mb = (float(x) for x in keyed["MEM"][:2])
    except (KeyError, ValueError, IndexError):
        return None
    out = {"cores": cores, "load1": load1,
           "ram_total_gb": ram_total_mb / 1024.0, "ram_avail_gb": ram_avail_mb / 1024.0,
           "vram_total_gb": None, "vram_used_gb": None, "gpu_util": None,
           # invariant 25 — container-true axes, all optional
           "gpu_mem_util": None, "gpu_name": None, "gpu_count": None,
           "cpu_quota_cores": None, "cpu_usage_usec": None,
           "mem_limit_gb": None, "mem_used_gb": None, "mem_anon_gb": None}
    # First PARSEABLE GPU row wins, and the frozen fields stay first-GPU (never a sum) so a
    # multi-GPU box's VRAM gate keeps admitting exactly as it did; `gpu_count` reports the rest.
    parsed_gpus = 0
    for row in gpu_lines:
        parts = [p.strip() for p in row.split(",")]
        try:
            tot, used, util = (float(v) for v in parts[:3])
        except (ValueError, IndexError):
            continue  # driver prose, a blank, a header — not a reading
        parsed_gpus += 1
        if parsed_gpus > 1:
            continue
        out.update(vram_total_gb=tot / 1024.0, vram_used_gb=used / 1024.0, gpu_util=util)
        # `utilization.memory` is memory-BANDWIDTH busyness, not occupancy — the axis that
        # separates "the GPU is full" from "the GPU is working". Not every driver reports it.
        if len(parts) >= 4:
            out["gpu_mem_util"] = _probe_num(parts[3])
        # The name nvidia-smi reports, which is not always the name Vast advertised.
        if len(parts) >= 5 and parts[4]:
            out["gpu_name"] = parts[4][:64]
    if parsed_gpus:
        out["gpu_count"] = parsed_gpus
    if "CPUQ" in keyed:
        out["cpu_quota_cores"] = _probe_num(keyed["CPUQ"][0]) if keyed["CPUQ"] else None
    if "CPUU" in keyed:
        out["cpu_usage_usec"] = _probe_num(keyed["CPUU"][0]) if keyed["CPUU"] else None
    if "MEMCG" in keyed:
        toks = keyed["MEMCG"]
        cur = _probe_num(toks[0]) if toks else None
        lim = _probe_num(toks[1]) if len(toks) > 1 else None
        if cur is not None:
            out["mem_used_gb"] = cur / (1024.0 ** 3)
        if lim is not None and lim < _CGROUP_UNLIMITED_BYTES:
            out["mem_limit_gb"] = lim / (1024.0 ** 3)
    if "MEMANON" in keyed:
        anon = _probe_num(keyed["MEMANON"][0]) if keyed["MEMANON"] else None
        if anon is not None:
            out["mem_anon_gb"] = anon / (1024.0 ** 3)
    return out


def _cpu_used_cores(prev: dict, cur: dict) -> float | None:
    """Cores actually burned by OUR container between two probes (invariant 25). Pure.

    None whenever the rate is not derivable — no previous sample (a fresh box, or the first pass
    after a dispatcher restart), either sample missing the counter, or the counter having gone
    BACKWARDS, which means the container was recreated and the two readings are not comparable.
    Returning None rather than 0.0 matters: 0.0 is a claim that the box was idle, and a restart
    silently reporting every box as idle is exactly the sort of confident-wrong number this panel
    exists to remove."""
    p_usec, c_usec = prev.get("cpu_usage_usec"), cur.get("cpu_usage_usec")
    p_at, c_at = prev.get("at"), cur.get("at")
    if p_usec is None or c_usec is None or p_at is None or c_at is None:
        return None
    dt = float(c_at) - float(p_at)
    d_usec = float(c_usec) - float(p_usec)
    if dt < 1.0 or d_usec < 0:
        return None
    return (d_usec / 1e6) / dt


# Invariant 30: a box registered WITH a GPU is "lost" after this many consecutive successful probes
# that see none. Measured over 30,585 probes on GPU-registered boxes: every null streak was a real
# loss and there were ZERO one-sample blips, so 2 costs one poll of latency and nothing else.
# `fleet_util`'s GPU-LOST detector imports this, so the alert and the report cannot disagree.
GPU_LOST_MIN_SAMPLES = 2
# The push channel is an ENVIRONMENT variable, set only on the live coordinator (the site's deployment sets
# it). Never a code default here: tests and every other process that
# builds a Dispatcher would push to the real phone. Unset or empty = logged but not pushed.
NTFY_TOPIC_ENV = "DISPATCHER_NTFY_TOPIC"
NTFY_SERVER_ENV = "DISPATCHER_NTFY_SERVER"


def gpu_alert_transition(registered_gpu, recent: list, alerted: bool,
                         min_samples: int = GPU_LOST_MIN_SAMPLES) -> str | None:
    """Invariant 30, pure. `recent` = `gpu_name` of the box's newest successful probes, oldest
    first, `None` where the probe saw no GPU. `alerted` = the box's last alert was `gpu_lost`.

    Returns "lost" (first time the newest `min_samples` probes are all GPU-less), "restored" (the
    newest probe sees a GPU again after a loss) or None. Edge-triggered: one push per drop and one
    per recovery, never one per poll."""
    if not registered_gpu or not recent:
        return None
    if recent[-1]:
        return "restored" if alerted else None
    if alerted:
        return None
    tail = recent[-min_samples:]
    return "lost" if len(tail) >= min_samples and not any(tail) else None


def ntfy_post(topic: str, title: str, message: str, *, tags: str = "", priority: str = "default",
              server: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Push one message to ntfy (https://ntfy.sh by default). Returns (ok, error) and NEVER raises:
    an alert channel that can throw is one more way for the poll loop to die."""
    import urllib.request
    base = (server or os.environ.get(NTFY_SERVER_ENV) or "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(f"{base}/{topic}", data=message.encode(), headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (200 <= r.status < 300), ("" if 200 <= r.status < 300 else f"HTTP {r.status}")
    except Exception as e:       # noqa: BLE001 — see docstring
        return False, f"{type(e).__name__}: {e}"[:300]


def _pending_footprint(inst: dict, settings: dict) -> tuple[float, float, float]:
    """Declared `(cores, ram_gb, vram_gb)` of occupants a measurement CANNOT yet see — those
    `claimed`/`shipped`/`reserved`, plus anything placed earlier in this same poll. A `running`
    occupant is already inside the sampled load/VRAM, so counting it again would double-charge."""
    cores = ram = vram = 0.0
    for o in inst.get("occupants", []):
        if o.get("state") in ("claimed", "shipped", "reserved"):
            cores += o.get("cores", 0.0)
            ram += o.get("ram_gb", 0.0)
            vram += o.get("vram_gb", 0.0)
    return cores, ram, vram


def _cpu_pair(m: dict) -> tuple[float, float]:
    """Invariant 28 — `(total_cores, used_cores)` for the headroom gate, taken as a PAIR.

    The container's cgroup pair (`cpu_quota_cores`, `cpu_used_cores`) when BOTH are measured, else
    the host pair (`cores` = `nproc`, `load1`). **Never one term from each** — that is the one
    genuinely dangerous combination: on instance 40000049 the container quota was 23.04 cores while
    the HOST run queue read 32.45, so `quota - load1` = **-9.4** would have refused every task on a
    box whose own container was burning 0.00 cores.

    `cpu_used_cores` is a RATE differenced from two consecutive probes (invariant 25), so it is
    None on a box's first measurement and after a dispatcher restart — the host pair legitimately
    stands in until the second probe rather than the axis abstaining. An unconstrained machine
    (both owned boxes: `cpu_quota_cores` is None, no cgroup limit) takes the host pair forever,
    which is correct: there the host numbers ARE the container's."""
    quota, used = m.get("cpu_quota_cores"), m.get("cpu_used_cores")
    if quota is not None and used is not None:
        return float(quota), float(used)
    return float(m["cores"]), float(m["load1"])


def _ram_free_gb(m: dict) -> float:
    """Invariant 28 — free RAM in GB, container-true when measured.

    `mem_limit_gb - mem_anon_gb` when both are present. **ANON, not `memory.current`**: `current`
    counts reclaimable page cache, which the kernel evicts under pressure rather than OOM-killing,
    so charging it as used is wrong rather than conservative — measured on instance 40000045,
    `current` was 19.2 GB of which **10.7 GB was cache** (anon 8.5). Falls back to
    `mem_limit_gb - mem_used_gb` when anon is missing (conservative, the old cgroup pair), and to
    the host's `ram_avail_gb` when the cgroup is unlimited (`mem_limit_gb` is None on both owned
    boxes — an unlimited cgroup, NOT a limit of zero)."""
    lim = m.get("mem_limit_gb")
    if lim is not None:
        anon = m.get("mem_anon_gb")
        if anon is not None:
            return float(lim) - float(anon)
        used = m.get("mem_used_gb")
        if used is not None:
            return float(lim) - float(used)
    return float(m["ram_avail_gb"])


def box_headroom(inst: dict, settings: dict, now: float) -> dict | None:
    """Invariant 23 — MEASURED spare capacity on a box, as `{cores, ram_gb, vram_gb}`. None when the
    box carries no fresh measurement (the gate then abstains).

    Headroom is measured against the OPERATOR'S ALLOWANCE, not the raw hardware: a capacity window's
    `cpu`/`vram` fractions say how much of the machine the fleet may occupy while its owner is using
    it, so the cap is `min(hardware, allowance)` and TOTAL observed load counts against it — the
    owner's own processes included. That is the point: when the human is working, the fleet's
    headroom genuinely shrinks, which is what a slot count could never express.

    Invariant 28 (2026-08-02): CPU and RAM are read from OUR CONTAINER's cgroup when it is measured,
    not from the host — see `_cpu_pair` / `_ram_free_gb`. The host pair over-reported free cores by
    up to **10x** on shared multi-tenant rentals (192 `nproc` against a 23.04-core quota, with a
    `load1` that counted other tenants as ours), always in the permissive direction. VRAM stays
    whole-device on purpose: a GPU is not cgroup-namespaced, and a co-tenant's allocation really
    does deny us that memory, so `vram_used_gb` including their usage is the correct quantity."""
    m = inst.get("measured")
    if not m:
        return None
    stale_s = settings.get("headroom_max_stale_min", 15) * 60
    if now - m.get("at", 0.0) > stale_s:
        return None  # a stale sample is not evidence; abstain rather than act on old numbers
    caps = inst.get("resource_cap") or {}
    pend_c, pend_r, pend_v = _pending_footprint(inst, settings)

    # Invariant 28: container-true denominators when the cgroup is measured, host pair otherwise.
    cpu_total, cpu_used = _cpu_pair(m)
    cpu_cap = min(cpu_total, caps.get("cores", cpu_total))
    cores = cpu_cap - cpu_used - pend_c - settings.get("headroom_cpu_reserve", 1.0)
    ram = _ram_free_gb(m) - pend_r - settings.get("headroom_ram_reserve_gb", 2.0)
    if m.get("vram_total_gb") is None:
        vram = None  # unmeasured GPU -> that axis abstains, CPU/RAM still gate
    else:
        vram_cap = min(m["vram_total_gb"], caps.get("vram_gb", m["vram_total_gb"]))
        vram = (vram_cap - m["vram_used_gb"] - pend_v
                - settings.get("headroom_vram_reserve_gb", 1.0))
    return {"cores": cores, "ram_gb": ram, "vram_gb": vram}


def _headroom_fits(task: dict, inst: dict, settings: dict, now: float) -> bool:
    """Invariant 23 gate: admit only if MEASURED headroom holds this task's declared footprint.
    Abstains (True) when the box carries no fresh measurement, and per-axis when that axis is
    unmeasured — never blocks on absent data. The VRAM axis applies only to a task with a VRAM
    footprint (26m): a card someone else has filled leaves NEGATIVE headroom, which used to refuse
    even a task charged 0."""
    if not settings.get("headroom_enabled", True):
        return True
    hr = box_headroom(inst, settings, now)
    if hr is None:
        return True
    cores, vram = task_footprint(task.get("resource_hint"), task["slots"], settings)
    ram = float((task.get("resource_hint") or {}).get(
        "ram_per_lane_gb", settings.get("ram_per_lane_gb", 2.0))) * task["slots"]
    if hr["cores"] < cores or hr["ram_gb"] < ram:
        return False
    if vram <= 0:
        return True  # 26m: the card's state does not gate a task that never touches it
    return not (hr["vram_gb"] is not None and hr["vram_gb"] < vram)


def _budget_fits(task: dict, inst: dict, settings: dict) -> bool:
    """Invariant 18a: a box carrying a capacity-window budget admits `task` only if the REAL summed
    footprint of its occupants plus this task still fits that budget. A slot count is one scalar and
    cannot stay honest across a heterogeneous hint mix, and `_place_queue` snapshots it once per
    poll — so a slots-only cap can be spent entirely within one pass. Boxes with no budget (every
    rented box, an owned box with no schedule) are unaffected."""
    caps = inst.get("resource_cap")
    if not caps:
        return True
    used_cores = sum(o.get("cores", 0.0) for o in inst.get("occupants", []))
    used_vram = sum(o.get("vram_gb", 0.0) for o in inst.get("occupants", []))
    cores, vram = task_footprint(task.get("resource_hint"), task["slots"], settings)
    if used_cores + cores > caps["cores"] + 1e-9:
        return False
    # 26m: a task charged no VRAM spends none of the window's, so GPU occupants that fill (or,
    # after a window change, exceed) the VRAM budget do not refuse CPU-only work.
    return vram <= 0 or used_vram + vram <= caps["vram_gb"] + 1e-9


_FS_COMPONENT_MAX_BYTES = 200


def _fs_safe_component(name: str) -> str:
    """One path component a filesystem will actually accept (invariant 9e).

    ext4/xfs cap a single component at **255 bytes**, and `runq sweep` builds an arm name by
    concatenating every axis value — so a wide sweep silently produces a name no directory can hold.
    That is not a cosmetic failure: `_result_dir` is called from `_ingest_and_complete`, so the
    `OSError` propagated out of `poll_once` and killed the DAEMON. The self-heal supervisor then
    respawned it straight back into the same task — 2 crash-respawns at 2026-07-29T15:07-15:08, with
    every task in the fleet stalled meanwhile, and it only stopped because a human cancelled the
    task. One 264-char name can wedge the entire fleet, indefinitely.

    Truncate-with-hash: the digest of the FULL name keeps distinct arms distinct (a plain truncation
    would collide two arms of the same sweep, which differ only in their tail) and keeps the mapping
    STABLE across polls and restarts, since it is a pure function of the name. A name already inside
    the limit is returned UNCHANGED, so no existing result directory moves.
    """
    raw = name.encode("utf-8")
    if len(raw) <= _FS_COMPONENT_MAX_BYTES:
        return name
    digest = hashlib.sha256(raw).hexdigest()[:12]
    head = raw[:_FS_COMPONENT_MAX_BYTES - len(digest) - 1].decode("utf-8", "ignore")
    return f"{head}-{digest}"


def _fits_now(task: dict, inst: dict, settings: dict, now: float | None = None) -> bool:
    # Invariant 10d: a quarantined box is one we could not push bytes to, so it takes no new work
    # until a delivery succeeds and lifts it. Excluded HERE rather than only in `place()`'s pack
    # filter so the backlog test (`_infeasible_everywhere`) and `_soonest_wait` agree with it —
    # otherwise the fleet would count an undeliverable box's free slots as incoming capacity and
    # decline to rent the box that could actually run the work.
    # Invariant 21h: a box we are deliberately draining takes no new work, or the drain undoes
    # itself — the preempted tasks requeue and the box they just left is the best fit for them.
    # Excluded HERE, alongside the quarantine and for the same reason: `_infeasible_everywhere` and
    # `_soonest_wait` must agree, so the fleet doesn't count a draining box's slots as incoming
    # capacity and decline to rent the box that could actually run the work.
    # Invariant 19h-3: a box that gate-held across consecutive reap passes takes no new work for one
    # grace window. Excluded HERE, beside the quarantine and the drain hold, for the same stated
    # reason — `_infeasible_everywhere` and `_soonest_wait` must agree with this filter, or the
    # fleet counts a wedged box's free slots as incoming capacity and declines to rent the box that
    # could actually run the work.
    return (not inst.get("ship_quarantined")
            and not inst.get("drain_held")
            and not inst.get("worker_roll_held")   # R3.1 (rolling upgrade)
            and not inst.get("overpack_cooldown")
            and _free_slots(inst) >= task["slots"]
            and _window_minutes_needed(task, settings) <= inst["minutes_to_hard_cap"]
            and _budget_fits(task, inst, settings)
            and _headroom_fits(task, inst, settings, time.time() if now is None else now))


def _infeasible_everywhere(task: dict, live: list, settings: dict) -> bool:
    """Can this task fit on NO live box it is allowed to board?

    ⚠ Routed through `_boardable` (2026-08-08). It used to scan raw `live`, which is the third
    incident in `_boardable`'s docstring wearing a different hat: for a TARGETED task it answered
    about boxes that task can never use, so a target-restricted task read as "fits somewhere" and
    was excluded from the backlog it was actually stuck behind. `_soonest_wait` was already given
    `boardable` by its caller; this one was missed because it takes `live` as a parameter and so
    looked like it had already been scoped. Route it here and the branches cannot disagree."""
    return not any(_fits_now(task, inst, settings) for inst in _boardable(task, live))


def _remaining_est(occupant: dict, settings: dict) -> float:
    return max(0.0, occupant["est_minutes"] * settings["est_safety"]
               - occupant["running_minutes_ago"])


def _est_overdue_by(occupant: dict, settings: dict) -> float:
    """Minutes an occupant is PAST its `est_minutes × est_safety` window (negative if still within
    it). A large positive value means its runtime estimate has expired — finish time unknown."""
    return occupant["running_minutes_ago"] - occupant["est_minutes"] * settings["est_safety"]


def _pack_cost(task: dict, inst: dict, settings: dict) -> float:
    """Invariant 4b — the marginal dollar cost of running `task` on live instance `inst`: the value
    of the box-time this task ADDS on top of what the box is already committed to bill. A free box
    (`dph_usd == 0`, e.g. an owned home box — invariant 20) is always `$0` and so is preferred by the
    pack sort. A paid box costs `dph × extension_hours`, where the extension is only the part of the
    task's window that outlasts the box's current occupants — riding a spare slot on a box that stays
    alive for a longer-running occupant anyway is `$0` (truly marginal). Keyed on the same
    `est_minutes × est_safety` estimates the backlog/preempt paths use; an occupant not yet running
    (claimed/shipped/reserved — no elapsed clock) counts its full estimate as still-to-run."""
    dph = inst.get("dph_usd", 0.0) or 0.0
    if dph <= 0.0:
        return 0.0
    paid_remaining = 0.0
    for o in inst.get("occupants", []):
        elapsed = o.get("running_minutes_ago") or 0.0  # None (not yet running) -> nothing elapsed
        remaining = max(0.0, o.get("est_minutes", 0.0) * settings["est_safety"] - elapsed)
        paid_remaining = max(paid_remaining, remaining)
    extension = max(0.0, _window_minutes_needed(task, settings) - paid_remaining)
    return dph * extension / 60.0


def _box_preference(inst: dict) -> int:
    """Invariant 4b': the operator's PACK PREFERENCE for this box — higher wins, default 0 (inert).

    Sits in the pack sort immediately after marginal cost and BEFORE tightest-fit, so it reorders
    only boxes that already cost the same (the all-free / all-riding-a-spare-slot case) and can
    never make the fleet spend money it would otherwise have saved. Cost still wins outright.

    WHY IT IS NEEDED, and why tightest-fit alone gets this exactly backwards. Every `$0` box ties at
    cost, so the FIRST live tie-break decides which free box fills, and that tie-break is *fewest
    free slots*. An idle box therefore sorts LAST among its equals, and the emptier it is the worse
    it sorts — so a freshly added, wholly-idle box is the last one the fleet will ever pack onto,
    and a big one stays idle longest of all. Measured on `tower` (2026-08-09, i9-13900K,
    24 slots, joined empty): against a `laptop-gpu` holding running work it lost every tie, so the
    laptop would fill to its cap before the new box took a single task — while the new box measured
    **2.5-2.8x an ordinary rented box's aggregate throughput** and costs nothing to run.

    Tightest-fit is not wrong, it is just aimed at a different problem: it minimises FRAGMENTATION
    so a PAID box can be emptied and torn down (invariant 21). That justification does not exist for
    an owned box — it is always on and always `$0`, so there is nothing to consolidate it toward and
    no teardown to enable. Consolidating work AWAY from free capacity in order to defragment it is
    the tail wagging the dog; this term lets the operator say so per box.

    Read from the settings table as `box_preference_i<instance_id>` (JSON int), mirroring the
    `overpack_cap_i<id>` keying convention. It is a POLICY knob and is deliberately hand-set, like
    the capacity-window cpu/vram fractions — the fleet learns footprints and caps, but which machine
    it would RATHER use is a statement about the operator's hardware, not something to infer.
    ⚠ `self.settings` is loaded once in `Dispatcher.__init__`, so a changed preference needs a
    `make dispatch-restart` to take effect."""
    return int(inst.get("pack_preference") or 0)


def _soonest_wait(task: dict, live: list, settings: dict) -> float | None:
    """Invariant 4c: minutes until some instance frees enough capacity for `task`, honoring
    the window check after that wait. None if no instance ever qualifies. A running task overdue by
    more than `rent_patience_min` is excluded — its estimate has expired, so it can't be counted as
    "freeing soon" (else a fleet of chronically-overdue long-runs holds a real backlog forever
    instead of renting; observed live 2026-07-15)."""
    best = None
    for inst in live:
        shortfall = task["slots"] - _free_slots(inst)
        if shortfall <= 0:
            continue
        running = sorted(
            (o for o in inst.get("occupants", [])
             if o["state"] == "running"
             and _est_overdue_by(o, settings) <= settings["rent_patience_min"]),
            key=lambda o: _remaining_est(o, settings))
        taken, wait = 0, 0.0
        for o in running:
            taken += o["slots"]
            wait = _remaining_est(o, settings)
            if taken >= shortfall:
                break
        if taken < shortfall:
            continue
        needed = _window_minutes_needed(task, settings)
        if needed > inst["minutes_to_hard_cap"] - wait:
            continue
        if best is None or wait < best:
            best = wait
    return best


def _preempt_shortfall(task: dict, inst: dict, settings: dict) -> tuple[int, float, float]:
    """What must be FREED on `inst` for `task` to fit: `(slots, cores, vram_gb)`. Any positive
    component means eviction is required; all non-positive means it already fits.

    The cores/VRAM components exist because invariant 18a can block a task on a box that has a free
    SLOT (live 2026-07-27: a 2-slot desktop holding one 6 GB occupant against a 6 GB window budget).
    A slots-only shortfall reads 0 there, so preemption skipped the box entirely and a high-priority
    probe could never displace the low-priority job actually holding the budget — it just held
    forever. A box with no `resource_cap` yields 0 for both, i.e. exactly the old behaviour."""
    slots = task["slots"] - _free_slots(inst)
    caps = inst.get("resource_cap")
    if not caps:
        return slots, 0.0, 0.0
    cores, vram = task_footprint(task.get("resource_hint"), task["slots"], settings)
    used_cores = sum(o.get("cores", 0.0) for o in inst.get("occupants", []))
    used_vram = sum(o.get("vram_gb", 0.0) for o in inst.get("occupants", []))
    need_vram = used_vram + vram - caps["vram_gb"] if vram > 0 else 0.0   # 26m
    return slots, used_cores + cores - caps["cores"], need_vram


def _find_preemption(task: dict, live: list, settings: dict) -> tuple[int, list] | None:
    """Invariant 17a: smallest eligible victim set (by combined slots) clearing the shortfall,
    on the instance requiring the fewest victims; None if no instance has an eligible set. The
    shortfall is measured on all three axes (slots + the invariant-18a cores/VRAM budget), so a
    task blocked by the BUDGET rather than by slot count can still preempt."""
    margin = settings["preempt_priority_margin"]
    best = None  # (n_victims, instance_id, instance_dict, victims)
    for inst in live:
        need_slots, need_cores, need_vram = _preempt_shortfall(task, inst, settings)
        if need_slots <= 0 and need_cores <= 1e-9 and need_vram <= 1e-9:
            continue
        eligible = sorted(
            (o for o in inst.get("occupants", [])
             if o["state"] == "running" and o["priority"] <= task["priority"] - margin),
            key=lambda o: (o["priority"], o["id"]))

        def _cleared(s, c, v):
            return s >= need_slots and c >= need_cores - 1e-9 and v >= need_vram - 1e-9

        chosen, taken, freed_cores, freed_vram = [], 0, 0.0, 0.0
        for o in eligible:
            chosen.append(o)
            taken += o["slots"]
            freed_cores += o.get("cores", 0.0)
            freed_vram += o.get("vram_gb", 0.0)
            if _cleared(taken, freed_cores, freed_vram):
                break
        if not _cleared(taken, freed_cores, freed_vram):
            continue
        needed = _window_minutes_needed(task, settings)
        if needed > inst["minutes_to_hard_cap"]:
            continue
        key = (len(chosen), inst["id"])
        if best is None or key < (best[0], best[1]):
            best = (len(chosen), inst["id"], chosen)
    if best is None:
        return None
    _, inst_id, chosen = best
    return inst_id, [o["id"] for o in chosen]


def _relief_already_in_flight(task: dict, live: list) -> bool:
    """True iff some instance's already-`preempting` occupant(s) alone would cover this task's
    shortfall once they vacate — invariant 4d's race-avoidance note."""
    for inst in live:
        shortfall = task["slots"] - _free_slots(inst)
        if shortfall <= 0:
            continue
        preempting = sum(o["slots"] for o in inst.get("occupants", []) if o["state"] == "preempting")
        if preempting >= shortfall:
            return True
    return False


def _offer_lane_counts(offer: dict, resource_hint: dict | None, settings: dict) -> list:
    """Lanes this offer supports on each axis the ADMISSION gate will later enforce (invariant 27).

    RAM is an axis here because `_headroom_fits` (23) gates on it. Without it the rent filter and
    the admit filter disagree and the fleet BUYS BOXES IT THEN REFUSES: measured live 2026-07-31,
    instance 40000042 was rented for an 8-slot task, came up with 7.4 GB, was refused on RAM, and
    was torn down 5 s after its first measurement — $0.019 for zero tasks, while the dispatcher
    rented a replacement 2 s earlier. Of 400 live offers, 78% of those passing our quality gates
    advertise under 8 GB, so this is the common case, not a corner.

    RAM ABSTAINS when the offer does not carry it (`ram_gb` falsy), exactly as the VRAM gate
    abstains on an unmeasured GPU — a missing field must never silently deny every offer.

    VRAM is an axis only for a task that uses the GPU (26m, `lane_vram_gb`) — the admission gate
    skips it for the others, so sizing must too. That also retires a crash: a declared
    `vram_per_lane_gb: 0`, which the probe template recommends, divided by zero here."""
    vram = lane_vram_gb(resource_hint, settings)
    cores = (resource_hint or {}).get("cores_per_lane", settings["cores_per_lane"])
    ram = float((resource_hint or {}).get("ram_per_lane_gb", settings.get("ram_per_lane_gb", 2.0)))
    counts = [math.floor(offer["cpu_cores_effective"] / cores)]
    if vram > 0:   # 26m: the card does not size a task that never touches it (and never divides by 0)
        counts.append(math.floor(offer["gpu_ram_gb"] / vram))
    ram_gb = offer.get("ram_gb")
    if ram_gb and ram > 0:
        counts.append(math.floor(ram_gb / ram))
    return counts


def slots_for_offer(offer: dict, resource_hint: dict | None, settings: dict) -> int:
    """Invariant 4a'. Returns 0 when the offer cannot host even ONE lane under the hint (fit
    filter, 2026-07-21 owner directive — replaces the old floor-up-to-1): 4e's fittability
    filter then drops the offer instead of booking a box that under-provisions every lane it
    runs. Live incident: a 1.71-effective-core Titan Xp slice was rented 4+ times for
    cores_per_lane=4 tasks, billing full dph while giving each lane under half its hinted
    cores on a CPU-bound workload.

    Sizes on VRAM, cores AND RAM (invariant 27) — every axis `_headroom_fits` will enforce."""
    n = min(_offer_lane_counts(offer, resource_hint, settings))
    if n < 1:
        return 0
    return min(n, settings["max_slots_cap"])


def task_max_dph(task: dict, settings: dict) -> float:
    """Invariant 4f — the per-hour price ceiling THIS task may rent at.

    `max_instance_dph` is a global GPU-class ceiling tuned for a workload that does not use the GPU
    (see DEFAULT_SETTINGS). A task that genuinely needs a big card declares `resource_hint.max_dph`
    and lifts its own ceiling without lifting anyone else's. Only ever RAISES: a hint below the
    global cap is ignored, because the cap is a spend guard and a task must not be able to widen the
    fleet's exposure downward-then-upward by declaring a smaller one. A non-numeric or non-positive
    hint falls back to the global (invariant 12 style: a malformed hint never crashes placement)."""
    v = (task.get("resource_hint") or {}).get("max_dph")
    cap = settings["max_instance_dph"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return cap
    return max(cap, float(v))


def cpu_name_allowed(offer: dict, resource_hint: dict | None) -> bool:
    """Invariant 4f — may THIS task rent THIS offer's CPU? Per-task, opt-in, off by default.

    WHY THIS EXISTS. Ranking (4e/4f) can only ever express a PREFERENCE for fast cores: it divides
    price by lanes x `offer_speed_factor`, so a modern desktop chip at $0.56/hr loses to an old
    24-core Xeon at $0.05/hr every time, however much faster its lanes are. That is the right
    default for research work — but it makes one job IMPOSSIBLE: deliberately renting a NAMED CPU
    class. Two live needs, both already written down and neither reachable before this:

      * `offer_speed_factor` is admittedly a PRIOR — `cpu_ghz` is base clock and ignores IPC, and
        its docstring asks for the weight to be re-fit against realised steps/sec per `cpu_name`.
        You cannot fit that curve from observational data alone: the ranker only ever samples the
        cheap end, so the fast half of the axis is never observed. Fitting it needs the ability to
        rent a chosen point on purpose.
      * hardware-purchase questions (`scripts/diagnostics/lane_scaling_bench.py`) price a candidate
        by measuring the SAME substrate on that candidate's silicon. A bench that cannot choose its
        CPU measures whatever was cheapest, which answers a different question.

    CONTRACT. `resource_hint.cpu_name_include` is a list of case-insensitive substrings (a bare
    string is accepted as a one-element list). An offer qualifies if its `cpu_name` contains ANY of
    them. Absent, empty or malformed => no filtering at all, so every existing task is unaffected.

    ⚠ FAIL-CLOSED, unlike the reliability gate in `eligible_offers`. An offer with no `cpu_name`
    is REFUSED when the filter is set. That is deliberate and it is the opposite of the usual
    fail-open rule here: fail-open exists so a missing metric cannot silently starve the QUEUE,
    but this filter is opt-in per task, so failing open would silently hand a targeted bench the
    anonymous box it was written to exclude — i.e. it would answer the wrong question quietly.
    A task that sets it would rather HOLD (`no_offer`) than be placed on an unidentified CPU."""
    pats = cpu_name_patterns(resource_hint)
    if not pats:
        return True
    name = (offer.get("cpu_name") or "").lower()
    return any(p in name for p in pats) if name else False


def gpu_allowed(offer: dict, resource_hint: dict | None) -> bool:
    """The RENT-path half of invariant 4h — `_boardable` is the pack-path half.

    ⛔ BOTH PATHS OR NEITHER. `cpu_name_include` shipped guarding the rent path alone and the
    targeted cell promptly PACKED onto someone else's box (incident 1 in `_boardable`). The
    symmetric mistake here would be to guard only packing and then rent a GPU-less offer.
    Fail-closed on a missing `gpu_name`, consistent with `cpu_name_allowed`."""
    if not requires_gpu(resource_hint):
        return True
    return bool(str(offer.get("gpu_name") or "").strip())


def cpu_name_patterns(resource_hint: dict | None) -> list:
    """The normalised `cpu_name_include` substrings, or [] when the task is not CPU-targeted."""
    inc = (resource_hint or {}).get("cpu_name_include")
    if isinstance(inc, str):
        inc = [inc]
    if not isinstance(inc, (list, tuple)):
        return []
    return [str(p).lower() for p in inc if isinstance(p, (str, int, float)) and str(p).strip()]


def is_cpu_targeted(task: dict) -> bool:
    """Does this task name the CPU it must run on? See `_boardable` for what that restricts."""
    return bool(cpu_name_patterns(task.get("resource_hint")))


def requires_gpu(resource_hint: dict | None) -> bool:
    """Does this task need a box with a REAL GPU? `resource_hint["requires_gpu"]`, default False.

    WHY THIS EXISTS (invariant 4h, 2026-09-04). A pixel bed cannot run without a rendering NVIDIA
    driver, and until now the fleet had no way to SAY so. `vram_gb` does not express it: it sizes a
    footprint and every owned box satisfies it trivially. The result was structural, not unlucky:

      * placement packs OWNED-FIRST because owned boxes are free, and
      * BOTH live owned boxes carry `gpu_name IS NULL` (`-3 tower`, `-2 desktop`); the only
        GPU-bearing owned box, `-1 laptop-gpu`, is `unreachable`.

    So every `madrona --obs depth|rgb` cell landed on a renderer-less box whenever tower was
    idle. MEASURED (`madrona_tau1_n3`): all four cells of a 3-seed escalation packed onto `-3`, built
    Madrona SUCCESSFULLY (clone 52s, cmake 14s, make 31s) and died at the smoke import on
    `ImportError: libcuda.so.1`. Earlier runs of the same bed had succeeded only because the box
    happened to be busy — luck, not design.

    CONTRACT. Truthy => the task may only occupy an instance whose `gpu_name` is a non-empty string,
    and may only rent an offer that has one. Absent or falsey => no filtering at all, so every
    existing task is bit-identical.

    ⚠ FAIL-CLOSED, deliberately, and for the same reason as `cpu_name_allowed`: an instance with no
    recorded `gpu_name` is REFUSED rather than assumed fine. Fail-open exists here so a missing
    metric cannot starve the QUEUE, but this filter is opt-in per task — failing open would hand a
    pixel bed exactly the renderer-less box it asked to avoid, and it would do it silently.

    ⚠ WHAT IT IS NOT. It does NOT promise the driver can RENDER — `gpu_name` is a name, and WSL
    carries `libcuda.so.1` while being unable to render. The job keeps its own preflight for that;
    this filter removes the machine class that has no GPU at all, which is the one placement can see.
    """
    return bool((resource_hint or {}).get("requires_gpu"))


def instance_has_gpu(inst: dict) -> bool:
    """Fail-closed: a non-empty `gpu_name` on the instances row. See `requires_gpu`."""
    return bool(str((inst or {}).get("gpu_name") or "").strip())


def box_target(resource_hint: dict | None) -> str | None:
    """The BOX this task must run on — `resource_hint["box"]`, an instance id or a label — else None.

    WHY THIS EXISTS (owner, 2026-08-08: "a task should be able to specify which box it wants"). Some
    reads are about a MACHINE, not about the work: "does this reproduce on a real GPU", "what does
    this box deliver when saturated", "reproduce the failure that only happens on the laptop". The
    fleet had no way to say so. `cpu_name_include` targets a CPU MODEL and works by renting a box for
    the task; there was nothing that could name a box the fleet ALREADY HAS.

    Measured cost of not having it, the day this landed: a GPU parity probe had to be requeued FOUR
    times and landed three consecutive times on owned instance -2, whose GPU is blocked by the OS —
    burning four placements to answer nothing. The available workarounds were all worse than the
    feature: over-declare `vram_gb` to dodge that box (the anti-pattern `7c2fa76b` fixed, and it
    mis-rations real cards), or rent a box outside the coordinator (unmetered, invisible to capacity
    accounting, exactly what the fleet exists to stop).

    An id or a label, because both are the natural handle depending on which you have: rentals are
    known by id (`40000046`), owned boxes by label (`laptop-gpu`). Compared as strings so a caller
    never has to know which one they hold."""
    v = (resource_hint or {}).get("box")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def is_box_targeted(task: dict) -> bool:
    """Does this task name the BOX it must run on? See `_boardable` for what that restricts."""
    return box_target(task.get("resource_hint")) is not None


def colocate_key(resource_hint: dict | None) -> str | None:
    """The SIBLING GROUP this task must share a box with — `resource_hint["colocate"]` — else None.

    WHY THIS EXISTS, and why `box` could not do it (2026-08-16). A paired comparison is only readable
    if its arms ran on the SAME machine: the box selects the attractor on a bistable rung (results are
    bit-identical within a box and differ ~1e-3 across), so an arm measured on one box against a
    control measured on another is not a paired measurement at all — and a collapsed control
    MANUFACTURES a win. `runq sweep` had no box control whatsoever, so **every multi-arm sweep was a
    box lottery by construction**; a published campaign co-located only 2 of its 3 pairs and nobody
    could see it from the outputs.

    `--box` is the wrong instrument for that, in two ways. It forces the OPERATOR to pick the
    machine — a placement decision the coordinator is better at and that goes stale the moment the
    fleet changes — and it is all-or-nothing: pinning a campaign to one named box serialises every
    seed behind one machine, so the cost of pairing arm-vs-arm is paid again across seeds that never
    needed it. What the science needs is narrower: *co-locate these N tasks WITH EACH OTHER, on
    whichever box you like.* That is this key. Arms `1..n` of seed 1 share a key and therefore a box;
    seed 2's arms share a DIFFERENT key and are free to land anywhere, so the campaign still
    parallelises across the fleet.

    The key is free-form and namespaced by whoever queues it (`runq sweep` uses
    `<group>:<seed-axis-value>`). Membership is the whole contract — two tasks with equal keys share a
    box, and nothing else about the key is interpreted."""
    v = (resource_hint or {}).get("colocate")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def is_colocated(task: dict) -> bool:
    """Does this task belong to a sibling group? See `colocate_key`."""
    return colocate_key(task.get("resource_hint")) is not None


def colocate_group_slots(task: dict) -> int:
    """Lanes the WHOLE sibling group still needs, but only while this task's placement would PIN the
    group — 0 once the group has a box (the pin is stamped as `resource_hint["box"]`, so
    `is_box_targeted` is the test) and 0 for an ordinary task.

    `colocate_group_slots` is supplied per poll by `_place_queue`, which is the only caller that can
    see the rest of the queue. Absent (a hand-built task in a test, a dry-run view) it degrades to
    this task's own slots, i.e. to exactly the pre-feature behaviour.

    WHY THE FIRST PLACEMENT MUST LOOK AT THE GROUP: the first member to place decides the box for
    every sibling, and it is the only member whose decision is free. If it takes the last free lane
    on an otherwise-full box, the remaining arms are correct but SERIALISED behind it — which is the
    "co-location costs a campaign its parallelism" complaint, re-created by the fix. So the group's
    total demand is what the pack filter and the rent filter admit against, with a documented
    fallback to the single-task demand so a group can never DEADLOCK for want of a big enough box."""
    if not is_colocated(task) or is_box_targeted(task):
        return 0
    return max(int(task.get("colocate_group_slots") or 0), int(task["slots"]))


def _box_matches(inst: dict, target: str) -> bool:
    """Instance id OR label, as strings. Fail-closed by construction: an instance carrying neither
    matches nothing, so an unknown target holds forever rather than placing somewhere arbitrary."""
    return str(inst.get("id")) == target or str(inst.get("label") or "") == target


# The `task.json` env marker a forced task carries to the box (invariant 4i-7). `env` is an EXISTING
# task.json key, so a worker older than 4i still accepts the task and merely hands the variable to
# the trainer; an unknown top-level key would be rejected by every not-yet-updated worker.
# Duplicated in `spool_worker.py`, which ships to the box as a standalone script.
FORCE_BOX_ENV = "RUNQ_FORCE_BOX"
# Invariant 8c: set to "1" in `task.json["env"]` for a task charged no VRAM (26m). The box then
# skips its two whole-card launch rules for it. In `env` for the reason above.
NO_GPU_ENV = "RUNQ_NO_GPU"


def forced_box(resource_hint: dict | None) -> str | None:
    """The box this task is FORCED onto (invariant 4i) — else None.

    WHY THIS EXISTS (owner, 2026-10-02: *"queue this task on this specific box, ignoring other
    constraints"*). `--box` chooses the machine but still waits for that machine's admission gates,
    and on an owned box those gates are deliberately tight while its owner is using it: a job that
    needs the whole card cannot board during the day window at all. This is the owner's per-task
    override of exactly those gates, and nothing else.

    Fail-closed on every axis, because a bypass is the last thing that should be reachable by
    accident: `force_box` must be LITERALLY `True` (not `"true"`, not `1`), the hint must name a
    `box` itself, and it must not be a colocation member — a pinned sibling also carries a `box`
    (stamped by `_stamp_colocation`), and forcing a whole group onto the box the coordinator happened
    to choose is not what anyone asked for.

    ⚠ This is only the HINT half. Whether the named box may be forced is `place()`'s call, on the
    instance view: every instance the target names must be `source='owned'` (i1)."""
    hint = resource_hint or {}
    if hint.get("force_box") is not True or colocate_key(hint) is not None:
        return None
    return box_target(hint)


def _forced_fits(task: dict, inst: dict, settings: dict) -> bool:
    """Invariant 4i-3: may a FORCED task board `inst` right now? `_fits_now` with every RESOURCE and
    TIME-OF-DAY gate removed and every BOX-STATE gate kept.

    Kept: the quarantine (10d — we cannot push bytes to it), the drain hold (21h), the worker-roll
    hold (R3) and the 4a window. Dropped: free slots, the 18a budget, measured headroom (23) and the
    over-pack cooldown (19h-3). `state == 'live'` is the caller's filter, as it is for `_fits_now`,
    which is what makes a PAUSED box refuse a forced task: pause is the owner's switch."""
    return (not inst.get("ship_quarantined")
            and not inst.get("drain_held")
            and not inst.get("worker_roll_held")
            and _window_minutes_needed(task, settings) <= inst["minutes_to_hard_cap"])


def admission_refusals(task: dict, inst: dict, settings: dict, now: float | None = None) -> list:
    """Invariant 4i-8: every admission gate that WOULD refuse `task` on `inst`, each with the numbers
    that decided it. Empty when an ordinary task would have packed here anyway.

    This is the audit half of the bypass and it must not drift from the gates it describes, so it
    CALLS them (`_budget_fits`, `_headroom_fits`, `_free_slots`) rather than re-deriving their
    arithmetic — the strings only add the operands. Anchored the way every diagnostic here is: the
    measured value next to the threshold it was compared with."""
    now = time.time() if now is None else now
    out = []
    if inst.get("overpack_cooldown"):
        out.append("overpack_cooldown (19h-3): box is inside a no-new-work window")
    free = _free_slots(inst)
    if free < task["slots"]:
        nominal = inst.get("slots_nominal")
        capped = (f" of {nominal} nominal — capacity window / learned over-pack cap applied"
                  if nominal is not None and nominal != inst["slots_total"] else "")
        out.append(f"slots: free {free} < need {task['slots']} "
                   f"(effective slots_total {inst['slots_total']}{capped})")
    cores, vram = task_footprint(task.get("resource_hint"), task["slots"], settings)
    if not _budget_fits(task, inst, settings):
        caps = inst["resource_cap"]
        used_c = sum(o.get("cores", 0.0) for o in inst.get("occupants", []))
        used_v = sum(o.get("vram_gb", 0.0) for o in inst.get("occupants", []))
        out.append(f"budget (18a): cores {used_c:.1f} used + {cores:.1f} vs {caps['cores']:.1f}; "
                   f"vram {used_v:.1f} used + {vram:.1f} vs {caps['vram_gb']:.1f} GB "
                   f"(this window's allowance)")
    if not _headroom_fits(task, inst, settings, now):
        hr = box_headroom(inst, settings, now)
        ram = float((task.get("resource_hint") or {}).get(
            "ram_per_lane_gb", settings.get("ram_per_lane_gb", 2.0))) * task["slots"]
        spare_v = "unmeasured" if hr["vram_gb"] is None else f"{hr['vram_gb']:.1f}"
        out.append(f"headroom (23): needs cores {cores:.1f} / ram {ram:.1f} GB / vram {vram:.1f} GB; "
                   f"measured spare cores {hr['cores']:.1f} / ram {hr['ram_gb']:.1f} GB / "
                   f"vram {spare_v} GB")
    return out


def _forced_hold_reason(task: dict, target: str, matches: list, settings: dict) -> str:
    """Why a forced task is NOT placing (invariant 4i-3). `matches` is every instance the target
    names, in any state. The reason must NAME the box state, or a forced task waiting on a paused
    box reads as a bypass that does not work."""
    why = []
    for i in matches:
        if i["state"] != "live":
            why.append(f"box state is {i['state']!r} (force_box bypasses capacity gates, never box "
                       f"state — a paused/draining/unreachable box still holds the task)")
            continue
        if requires_gpu(task.get("resource_hint")) and not instance_has_gpu(i):
            why.append("the task requires a GPU and this box is registered without one")
            continue
        held = [name for name, key in (("ship-quarantined (10d)", "ship_quarantined"),
                                        ("drain-held (21h)", "drain_held"),
                                        ("worker-roll-held (R3)", "worker_roll_held")) if i.get(key)]
        if held:
            why.append("box is " + ", ".join(held))
        elif _window_minutes_needed(task, settings) > i["minutes_to_hard_cap"]:
            why.append(f"window {_window_minutes_needed(task, settings):.0f}min > "
                       f"{i['minutes_to_hard_cap']:.0f}min to the box's hard cap")
    return f"force_box: waiting for box {target!r} — " + ("; ".join(why) or "box is not ready")


def _boardable(task: dict, instances: list) -> list:
    """The instances this task could EVER occupy — the ONE predicate every capacity branch of
    `place()` must agree on (invariant 4f).

    For an ordinary task that is every instance, so nothing changes. For a CPU-TARGETED task it is
    only the box rented FOR IT (`runq_<task-id>`, stamped by `_rent`), because that is the only box
    whose CPU was ever checked: the `instances` row carries `gpu_name` but no `cpu_name`, so any
    other box is unverifiable and could be the wrong silicon.

    ⚠ WHY THIS IS A SHARED HELPER AND NOT AN INLINE FILTER — three incidents, same root cause, one
    day (2026-08-07). Restricting placement in ONE branch silently changes the meaning of every
    OTHER branch that reasons about capacity, and each was found the expensive way:
      1. the rent filter alone => the task PACKED onto a wrong-CPU box (a 9950X arm measured on an
         X3D, which would have collapsed a two-arm cache comparison into one arm);
      2. blanket rent-only => the task could not board the box it had just rented, so it re-rented
         every poll — three boxes in 16 minutes, none used;
      3. the pack filter alone => `_soonest_wait` (4c) and the over-provisioning guard (5c) still
         counted OTHER tasks' boxes as incoming capacity, so the task deferred its own rental
         waiting for a slot it can never board. On a busy fleet something is nearly always freeing
         inside `rent_patience_min`, so that hold never clears — stuck, not slow.
    Every one of those is the same bug: two branches disagreeing about which boxes exist for this
    task. Route ALL of them through here, and they cannot disagree.

    ⛔ KNOWN LIMITATION — A TARGETED TASK THAT CLAIMS **ALL** SLOTS CAN BE GAZUMPED, AND WILL THEN
    RENT-LOOP. This is NOT fixed here, it is the fourth incident of 2026-08-07, and anyone queueing
    a `cpu_name_include` job needs to know it before they spend.

    A hardware BENCH wants exclusivity, and the way to buy exclusivity is to claim every slot
    (`slots == max_slots_cap`) so `_fits_now`'s `free_slots >= task.slots` can only be satisfied by
    an EMPTY box. But a box is not reserved for the task that rented it: between the rent and that
    task's next placement poll, ORDINARY queued tasks pack onto the fresh box (they are entitled to,
    and `_pack_cost` actively prefers a box already paid for). Free slots drop below the target's
    demand, the target can now never board the box IT paid for, and — because `place()` then falls
    through to rent — it buys another one. Repeat every poll.

    Measured: two targeted cells rented **nine** boxes between them, boarded none, and left five idle
    at $0.97/hr — 75% of the fleet's entire spend rate, which then tripped the `max_hourly_usd` guard
    and blocked the whole queue. `_place_queue` reserves an incoming slot only WITHIN one poll; there
    is no cross-poll reservation, and adding one is a real feature, not a patch.

    Until that exists, a targeted job must do ONE of:
      * claim FEWER slots than the box provides and accept neighbours (fine when measuring per-lane
        speed at K=1, fatal when measuring a saturation curve — neighbours ARE the contention);
      * be queued when nothing else is competing for placement;
      * or be run by hand on a box rented outside the coordinator.
    Do NOT simply requeue it and hope; that is what produced the nine boxes.

    ⛔ A BOX-TARGETED task (`resource_hint["box"]`) is narrower still, and differs in KIND from the
    CPU case: the named box either exists or it does not, so RENTING CAN NEVER SATISFY IT. `place()`
    therefore holds instead of renting for these (4e), which is the opposite of the CPU case where
    renting is precisely how the box comes to exist. Getting that backwards is incident 2 above,
    replayed — a task that rents boxes it is forbidden to board.
    """
    # ⛔ 4h: the GPU requirement is applied HERE, in the shared predicate, and NOT inline in any
    # branch of `place()`. That is the entire lesson of the three incidents above: a filter applied
    # in one branch makes every other branch disagree about which boxes exist for this task.
    # ⚠ Unlike the CPU case this needs no rent-label trick — the `instances` row HAS `gpu_name`, so
    # a packed box is directly verifiable and there is no rent-loop hazard (incident 2).
    hint = task.get("resource_hint")
    if requires_gpu(hint):
        instances = [i for i in instances if instance_has_gpu(i)]
    tgt = box_target(hint)
    if tgt is not None:                       # most specific wins; a box target implies its own CPU
        return [i for i in instances if _box_matches(i, tgt)]
    if not is_cpu_targeted(task):
        return instances
    own = f"runq_{task['id']}"
    return [i for i in instances if i.get("label") == own]


def lane_capacity(offer: dict, resource_hint: dict | None, settings: dict) -> int:
    """Invariant 4e (cores-per-$ ranking): the offer's lane count under the hint WITHOUT the
    `max_slots_cap` ceiling, floored at 1 — the denominator of the value-density rank. Uncapped
    because more effective cores per dollar means more per-lane CPU headroom (each lane runs faster
    on the CPU-bound workload), a real advantage even above the cap on lanes we'll actually pack.
    The rented `slots_total` still comes from the capped `slots_for_offer`.

    Counts the same axes as `slots_for_offer` (invariant 27) so the RANKING cannot prefer an offer
    the fit filter is about to drop — a box whose RAM caps it at 2 lanes is not a cheap 18-lane box."""
    return max(1, min(_offer_lane_counts(offer, resource_hint, settings)))


def demand_slots(task: dict, queue: list, settings: dict) -> int:
    """The lane demand a fresh box could absorb near-term — the triggering task's own slots plus
    every OTHER queued task whose window fits a new rental (the 4a0 ceiling; a window-infeasible
    task can never board any box, so it adds no demand). Observability only since 2026-07-21 (the
    `usable_now` field of the rent event / counterfactual); the cores-per-$ ranking no longer uses
    it, so hardware quality is visible even on a short queue."""
    ceiling = settings["hard_cap_hours"] * 60
    return task["slots"] + sum(t["slots"] for t in queue
                                if _window_minutes_needed(t, settings) <= ceiling)


def place(task: dict, instances: list, offers: list, queue: list, settings: dict, now) -> Placement:
    """Invariant 4. `instances` is every instance regardless of state (needed to detect
    bootstrap); `queue` is every OTHER currently-queued task (for the backlog test)."""
    # 4a0: a window no freshly rented box could ever satisfy (hard_cap_at is stamped
    # now + hard_cap_hours at rent and only shrinks) — hold BEFORE any pack/preempt/rent
    # logic, or the loop rents boxes it can never pack (retrospective bug 9).
    needed = _window_minutes_needed(task, settings)
    ceiling = settings["hard_cap_hours"] * 60
    if needed > ceiling:
        return Placement("hold", None, None,
                          f"infeasible_est: window {needed:.0f}min (est_minutes x est_safety "
                          f"+ pull_margin_min) > hard-cap ceiling {ceiling:.0f}min — lower "
                          f"--est-minutes or raise hard_cap_hours")
    live = [i for i in instances if i["state"] == "live"]

    # 4a/4b: pack — lowest marginal $ cost (free owned boxes = $0 win outright), ties -> fewest free
    # slots (tightest fit) -> lower instance id.
    # A CPU-TARGETED task may pack ONLY onto the box it rented ITSELF (invariant 4f).
    #
    # TWO live incidents on 2026-08-07, and this rule is the second fix — read both, because the
    # obvious repair for the first one CAUSED the second:
    #   1. `cpu_name_include` guarded the RENT path only. The 9950X-targeted cell packed onto a box
    #      just rented for the 9950X3D cell and returned a curve labelled 9950X measured on 128 MiB
    #      of V-Cache. So packing plainly cannot be left unguarded.
    #   2. The first fix was BLANKET rent-only — and that broke placement outright. Renting is not
    #      how a task boards a box; it is how the box comes to EXIST. The task then boards it on a
    #      later poll via exactly this pack path. Refusing to pack therefore meant: rent a box, be
    #      forbidden to board it, rent ANOTHER next poll. Task 7c5f6460 rented THREE boxes in 16
    #      minutes and ran on none. A rent LOOP is far worse than the mis-pack it was preventing.
    #
    # The label is what makes the narrow rule possible: `_rent` stamps `runq_<task-id>`, so a box
    # carrying THIS task's id is one the rent path already vetted with `cpu_name_allowed` against
    # the real offer. That is a VERIFIED CPU — the only one available here, since the `instances`
    # row has `gpu_name` but no `cpu_name`. Any other box is unverifiable and is still refused, so
    # the guarantee from fix 1 holds: a targeted task never lands on an unchecked CPU, and never
    # shares with a neighbour (its own box is rented for it alone).
    # Fail-closed on a missing label, consistent with `cpu_name_allowed`.
    boardable = _boardable(task, live)
    # 4i: FORCED BOX PLACEMENT — decided here, before the ordinary pack filter, and it RETURNS on
    # both outcomes so no later branch can act for a forced task: it never preempts (the capacity a
    # preemption would free is not something it is waiting for), never reaches the backlog bar and
    # never rents. It still goes through `_boardable`, the one predicate every branch agrees on, so
    # the box target and the `requires_gpu` filter mean exactly what they mean everywhere else.
    # Forced only when EVERY instance the target names is owned: a rental id, or an unknown target,
    # falls through and is placed as the ordinary box-targeted task it otherwise is (fail-closed —
    # the bypass must never be the reason a paid box is over-packed).
    ftgt = forced_box(task.get("resource_hint"))
    if ftgt is not None:
        named = [i for i in instances if _box_matches(i, ftgt)]
        if named and all(i.get("source") == "owned" for i in named):
            ready = [i for i in boardable if _forced_fits(task, i, settings)]
            if not ready:
                return Placement("hold", None, None,
                                  _forced_hold_reason(task, ftgt, named, settings))
            target = min(ready, key=lambda i: i["id"])
            bypassed = admission_refusals(task, target, settings)
            return Placement(
                "pack", target["id"], None,
                f"forced_box: pack on instance {target['id']} ({ftgt!r}) ignoring admission gates "
                f"— {len(bypassed)} would have refused"
                + (": " + " | ".join(bypassed) if bypassed else " (it fits anyway)"),
                bypassed=bypassed)
    # 4g: a colocation group's FIRST placement chooses the box for EVERY sibling, so admit that one
    # against the group's total lane demand — otherwise arm 1 takes the last free lane somewhere and
    # arms 2..n queue behind it on a box that was never big enough. The fallback to single-task
    # demand is deliberate and is what makes the group unable to deadlock: when no box can hold the
    # whole group we still place (correct, co-located, serialised) rather than holding for a machine
    # that may never appear. Inert for every task that is not the pinning member of a group.
    group_slots = colocate_group_slots(task)
    feasible = ([i for i in boardable if _fits_now({**task, "slots": group_slots}, i, settings)]
                if group_slots > task["slots"] else [])
    whole_group = bool(feasible)
    if not feasible:
        feasible = [i for i in boardable if _fits_now(task, i, settings)]
    if feasible:
        target = min(feasible, key=lambda i: (_pack_cost(task, i, settings), -_box_preference(i),
                                              _free_slots(i), i["id"]))
        needed = _window_minutes_needed(task, settings)
        pref = _box_preference(target)
        colo = ""
        if group_slots > task["slots"]:
            colo = (f" [colocate {colocate_key(task.get('resource_hint'))!r}: pins the group here; "
                    + (f"all {group_slots} group lanes fit]" if whole_group else
                       f"NO box fits the group's {group_slots} lanes, so its siblings will "
                       f"SERIALISE here]"))
        return Placement("pack", target["id"], None,
                          f"pack on instance {target['id']}: ${_pack_cost(task, target, settings):.4f} "
                          f"marginal, fit {needed}<={target['minutes_to_hard_cap']} "
                          f"(free slots {_free_slots(target)})"
                          + (f" [preference {pref}]" if pref else "") + colo)

    # 4d: preemption, checked before any hold/backlog logic.
    # Gated by the global `preempt_enabled` switch: when off we fall straight through to the
    # hold/backlog/rent logic below, so a high-priority task WAITS or gets a box of its own rather
    # than evicting someone. That is the intended `--probe` degradation — still first in the queue at
    # priority 90, just no longer able to interrupt work that is already running.
    # ⚠ SCOPED TO `boardable`, NOT `live` (2026-08-16). This is the FOURTH branch of `place()` to
    # need `_boardable`'s docstring lesson, and the one it was missing: a TARGETED task evicting a
    # victim on a box it may never board pays the eviction (a checkpoint, a requeue, a relocation)
    # and still cannot use the slot it just cleared — then does it again next poll. Latent until now
    # because `preempt_enabled` ships FALSE and box targeting was rare; invariant 4g makes every
    # pinned sibling a targeted task, so the two would meet the moment preemption is re-enabled.
    # Exactly `live` for an ordinary task, so nothing else changes.
    found = (_find_preemption(task, boardable, settings)
             if settings.get("preempt_enabled", True) else None)
    if found:
        inst_id, victim_ids = found
        return Placement("preempt", inst_id, victim_ids,
                          f"preempt {victim_ids} on instance {inst_id} "
                          f"(priority {task['priority']} - margin {settings['preempt_priority_margin']})")

    # A preemption already in flight (victim in `preempting`, not yet vacated) satisfies this
    # task's shortfall on its own — hold for it rather than re-preempting someone else or
    # falling through to the priority bypass and renting a now-redundant second box. Without
    # this, a preempting occupant is invisible to _find_preemption (state != "running") and to
    # _soonest_wait (same reason), so a second poll before PREEMPTED lands would otherwise look
    # exactly like "nothing evictable" and wrongly trigger invariant 4d's no-candidate bypass.
    if _relief_already_in_flight(task, live):
        return Placement("hold", None, None, "preempt_wait: an eviction already in flight covers this shortfall")

    # 4e0: A BOX-TARGETED TASK NEVER RENTS. The named box either exists in this fleet or it does
    # not, and no rental can become it — so falling through to 4e would buy a box the task is
    # forbidden to board, then buy another next poll. That is incident 2 of `_boardable`'s docstring
    # (three boxes in 16 minutes, none used), and it is structural here rather than a race: for the
    # CPU-targeted case renting is how the box comes to EXIST, and for this case it never can be.
    # Hold instead, and say which box is being waited on so a queued-cold task is diagnosable.
    if is_box_targeted(task):
        tgt = box_target(task.get("resource_hint"))
        known = any(_box_matches(i, tgt) for i in instances)
        # 4g: a PINNED colocation member reaches this branch too (its pin is stamped as the box
        # target), and it must not read as an operator's `--box` typo — the box was chosen by the
        # coordinator, and waiting for it is the feature working, not a misconfiguration.
        key = colocate_key(task.get("resource_hint"))
        if key is not None:
            return Placement("hold", None, None,
                              f"colocate: group {key!r} is pinned to box {tgt} by an earlier "
                              f"sibling — waiting for room beside it rather than splitting the "
                              f"comparison across boxes")
        return Placement("hold", None, None,
                          f"box_target: waiting for box {tgt!r}"
                          + ("" if known else " — NO SUCH BOX in this fleet (id or label); it will "
                                              "hold until one appears, so check the target"))

    any_instance_exists = any(i["state"] in ("provisioning", "live", "draining") for i in instances)
    if any_instance_exists:
        # 4c: backlog-bar patience. Scoped to BOARDABLE capacity — a CPU-targeted task cannot use a
        # slot freeing on someone else's box, and holding for one means waiting forever (there is
        # nearly always something freeing within `rent_patience_min` on a busy fleet).
        wait = _soonest_wait(task, boardable, settings)
        if wait is not None and wait <= settings["rent_patience_min"]:
            return Placement("hold", None, None,
                              f"slot_freeing_soon: wait {wait} <= {settings['rent_patience_min']}")
        # 5c over-provisioning guard: a box already being provisioned counts as incoming capacity, so
        # HOLD for it rather than renting a redundant one. Async provisioning removed the inline block
        # that used to throttle renting, so without this the poll would rent one box per queued task.
        # _place_queue reserves the incoming slot (in-poll) so sibling tasks don't double-count it, and
        # a box rented earlier this poll is in `instances` as provisioning too.
        # Scoped to BOARDABLE for the same reason as 4c: another task's incoming box is not
        # incoming capacity for a CPU-targeted task, and holding for it would defer its rental
        # indefinitely on a fleet that always has something provisioning.
        for inst in _boardable(task, instances):
            if (inst["state"] == "provisioning" and _free_slots(inst) >= task["slots"]
                    and _window_minutes_needed(task, settings) <= inst["minutes_to_hard_cap"]):
                return Placement("hold", inst["id"], None,
                                  f"awaiting_provisioning: instance {inst['id']} coming up "
                                  f"(incoming slots {_free_slots(inst)})")
        # A BOX-TARGETED task is not evidence that the fleet needs another box: no rental can ever
        # serve it, so counting it toward the backlog bar would rent capacity for demand that cannot
        # consume it. Excluded from the bar for EVERY task's rent decision (it appears in other
        # tasks' `queue` too), not just its own — which is also why this filters the whole list
        # rather than short-circuiting on `task`.
        stuck = [t for t in [*queue, task]
                 if not is_box_targeted(t) and _infeasible_everywhere(t, live, settings)]
        backlog_tasks = len(stuck)
        backlog_minutes = sum(t["est_minutes"] * t["slots"] for t in stuck)
        qualifies = (backlog_tasks >= settings["backlog_min_tasks"]
                     or backlog_minutes >= settings["backlog_min_task_minutes"])
        if not qualifies and task["priority"] <= DEFAULT_PRIORITY:
            return Placement("hold", None, None,
                              f"backlog_too_small: {backlog_tasks} tasks / {backlog_minutes} min "
                              f"(need {settings['backlog_min_tasks']}/{settings['backlog_min_task_minutes']})")

    # 4e: rent.
    balance = settings["current_balance"]
    if balance <= settings["balance_floor_usd"]:
        return Placement("hold", None, None,
                          f"balance: {balance} <= floor {settings['balance_floor_usd']}")
    deny = settings.get("deny_machine_ids", set())
    # Invariant 4e fittability filter (retrospective bug 13, the slots-axis twin of 4a0): an
    # offer whose resulting slots_total can't host the triggering task would be rented and then
    # never packed — idle-bill → teardown → re-rent churn. Drop it before the cheapest pick.
    def _rentable(min_slots: int) -> list:
        return [o for o in offers if o["dph_total"] <= task_max_dph(task, settings)
                and o.get("machine_id") not in deny
                and cpu_name_allowed(o, task.get("resource_hint"))
                # 4h, the rent half of the GPU requirement whose pack half lives in `_boardable`.
                # Guarding only one path is incident 1 of `_boardable`, replayed on a different axis.
                and gpu_allowed(o, task.get("resource_hint"))
                and slots_for_offer(o, task.get("resource_hint"), settings) >= min_slots]

    # 4g, the rent half of the same rule as the pack filter above: the member that rents is the
    # member that PINS, so buy a box that can hold the whole sibling group. Falls back to this
    # task's own demand when the market has nothing that big — a group must never be unable to run
    # for want of a box big enough to run it all at once.
    candidates = _rentable(max(task["slots"], group_slots))
    group_undersized = not candidates and group_slots > task["slots"]
    if group_undersized:
        candidates = _rentable(task["slots"])
    if not candidates:
        return Placement("hold", None, None, "no_offer: no qualifying offers")
    # Cores-per-$ ranking (2026-07-21, owner directive — supersedes the demand-capped $/slot rule
    # that degenerated to raw price on a short queue and so kept booking weak single-slot boxes).
    # Rank by value density = dph / lane_capacity (UNCAPPED by max_slots_cap and demand): more
    # effective cores per dollar = more per-lane CPU headroom, so a core-rich box is genuinely
    # better even when few lanes run today. Live incident 2026-07-21: on a near-empty queue a 5.25c
    # 9-core GTX 1080 was booked over a 5.47c 24-core RTX 3060 Ti (0.58c vs 0.42c per lane).
    # Invariant 4f (2026-08-04): the density is now per unit of lane THROUGHPUT, not per lane —
    # `value_density` multiplies lane count by a `cpu_ghz`-derived speed factor, because the
    # workload is measurably CPU-bound (GPU util 0% at p50 AND p90 across 4,987 box samples) and
    # this ranking was blind to how fast a lane actually runs. See `offer_speed_factor`.
    hint = task.get("resource_hint")

    def _value_density(o):
        return value_density(o, hint, settings)

    cheapest = min(candidates, key=lambda o: (_value_density(o), o["dph_total"], o.get("id") or 0))
    rate = settings["current_rate"]
    if rate + cheapest["dph_total"] > settings["max_hourly_usd"]:
        return Placement("hold", None, None,
                          f"budget: rate {rate}+{cheapest['dph_total']} > cap {settings['max_hourly_usd']}")
    cap = lane_capacity(cheapest, hint, settings)
    # demand only annotates the log (near-term absorbable lanes), it no longer picks the box.
    usable = min(slots_for_offer(cheapest, hint, settings), demand_slots(task, queue, settings))
    return Placement("rent", None, None,
                      f"rent offer dph={cheapest['dph_total']} lanes={cap} "
                      f"dph_per_lane={cheapest['dph_total'] / cap:.4f} (usable_now={usable})"
                      + (f" [colocate {colocate_key(task.get('resource_hint'))!r}: no offer holds "
                         f"the group's {group_slots} lanes, so its siblings will SERIALISE here]"
                         if group_undersized else ""),
                      offer=cheapest)


def should_teardown(instance: dict, queued_tasks: list, now, settings: dict) -> tuple[bool, str]:
    """Invariant 11.

    Owned-box carve-out: a `source='owned'` instance (self-owned, e.g. a home laptop/desktop)
    costs nothing while idle — unlike a Vast rental, there's never a reason to tear it down, and
    doing so would be actively harmful (nothing re-adopts a `destroyed` row; see `_destroy`)."""
    if instance.get("source", "vast") == "owned":
        return False, "owned_box_never_torn_down"
    if instance["minutes_to_hard_cap"] <= 0:
        return True, "hard_cap"
    if instance.get("occupants"):
        return False, "feasible_task_waiting"
    # Invariant 10d: a quarantined box is undeliverable, so `feasible_task_waiting` would hold it
    # alive forever against a queue it can never serve — the exact idle-bill 10b's `_destroy` exists
    # to prevent, reached here without a bespoke destroy rule. Placed after the occupants check so a
    # box still holding shipped/running work from before the quarantine is never torn down under it,
    # and after the owned carve-out above so an owned box merely waits to recover.
    if instance.get("ship_quarantined"):
        return True, "undeliverable"
    # Invariant 21h: an EMPTY box we deliberately drained is done — reclaim it now. Without this the
    # `feasible_task_waiting` loop below keeps it alive against the very tasks the drain evicted (they
    # are queued and they obviously fit), which is how 79 of 111 drains ended with the box still live.
    # Placed after the occupants check, so a box still holding work is never destroyed mid-drain, and
    # after the owned carve-out. No idle wait: the hold guarantees nothing new was placed, so "empty"
    # is already stable — waiting `idle_timeout_min` would only add billed idle time.
    if instance.get("drain_held"):
        return True, "drained"
    # Invariant 11a (owner directive 2026-07-30: "we probably want no more than 1 box kept warm and
    # idle"). `feasible_task_waiting` below keeps an EMPTY box alive whenever ANY queued task fits it,
    # and it is evaluated per box — so a single 1-slot task retains EVERY idle box in the fleet, and
    # `idle_timeout_min` never gets a chance to fire. Observed live: four empty boxes idle 24/42/48/80
    # minutes against a 10-minute timeout, costing $0.2162/hr for zero work, because work arrived every
    # few minutes and the queue was almost never empty at the instant this check ran.
    # `warm_idle_keep` is set by the caller from `warm_hold_grants` — at most `warm_idle_max` boxes,
    # at most `max_warm_free_slots` free slots, and only as much as the queue's demand actually needs
    # once the fleet's already-free capacity is counted. A box that is empty and NOT designated falls
    # through to the ordinary idle timer instead of being held indefinitely. Boxes with occupants
    # returned above and are unaffected — this only bounds how much EMPTY capacity is retained.
    # The `not occupants` clause is REDUNDANT — the occupants check above already returned for any
    # box holding work, so it is always true here. Kept as a belt-and-braces guard against a future
    # reorder of these checks, since the cost of getting it wrong is destroying a box mid-run. Noted
    # because it also makes "drop the occupants clause" an EQUIVALENT mutant: no test can kill it,
    # and that is correct rather than a coverage gap.
    warm_capped = not instance.get("occupants") and not instance.get("warm_idle_keep", True)
    if not warm_capped:
        for t in queued_tasks:
            if t["slots"] <= instance["slots_total"] and _window_minutes_needed(t, settings) <= instance["minutes_to_hard_cap"]:
                return False, "feasible_task_waiting"
    if instance.get("idle_minutes", 0) >= settings["idle_timeout_min"]:
        return True, "idle_over_warm_cap" if warm_capped else "idle"
    return False, "idle_not_yet"


def warm_hold_grants(instances: list, queued_tasks: list, settings: dict) -> set:
    """Invariant 11a — which EMPTY paid boxes may keep their `feasible_task_waiting` hold.

    Returns the set of instance ids to stamp as `warm_idle_keep`. Computed once per poll over the
    WHOLE fleet, because every bound here is a fleet property that a per-instance predicate
    structurally cannot see — which is how the unbounded version survived so long: each individual
    box's hold looked locally reasonable.

    TWO bounds, both of which must hold — the two units the owner stated the same directive in on
    2026-07-30:
      * `warm_idle_max` — BOX COUNT ("no more than 1 box kept warm and idle"; raised to 2 the same
        day, "that was just a hack to close the gap", once the slot bound below became the real
        constraint).
      * `max_warm_free_slots` — FREE SLOTS ("we have 58/98 slots in use so almost 50% of our spend
        is going to waste ... no more than 10 free slots kept warm").

    **The slot budget is FLEET-WIDE, not per-candidate** (owner directive: "make sure
    `max_warm_free_slots` includes non-empty boxes that have free slots"). `committed_free` — every
    free slot on a live box that STILL HOLDS WORK, owned or paid — is spent against the cap FIRST,
    because those slots are exactly as available to the queue as a warm box's are, and they cost
    nothing extra: an owned box is $0 and a busy paid box bills for its occupant regardless. Only
    the remainder may be held warm on an empty PAID box, which is the only capacity we can actually
    release. Counting only the candidates would let a fleet already carrying 20 free slots rent a
    third pool of them and still report itself inside the cap.

    Consequence worth knowing: on a busy fleet `committed_free` alone routinely exceeds the cap, so
    the allowance is ZERO and nothing is kept warm — which is the intent (there is no reason to pay
    to keep a box warm when the queue already has somewhere free to land) but it also means
    `warm_idle_max` only binds on a comparatively empty fleet. Raise `max_warm_free_slots` if warm
    capacity should survive a busy fleet.

    Only EMPTY PAID boxes are candidates and only they are ever denied; `should_teardown`'s owned
    carve-out runs first regardless, so nothing here can destroy a home box. Grants go cheapest
    first (ascending `dph_usd`, then MOST slots, then id — deterministic, because a designation that
    wobbles between polls would spare a different box each time and cull none of them), so the boxes
    that fall to the idle timer are the EXPENSIVE ones. A grant that would push the total OVER the
    cap is refused: the cap is a ceiling, not a target.

    NOT bounded here, deliberately: queue DEMAND. Nothing distinguishes "one small task is queued"
    from "a real backlog is waiting". The fleet-wide accounting above already covers the case that
    motivated it (free capacity existing elsewhere), and gating on demand as well would trade
    against re-rent churn that is NOT yet measured — a rental takes ~14 min to become useful and 27
    of 57 boxes torn down in 24h never ran a task at all. Left as the spec's Open question."""
    max_boxes = max(0, int(settings.get("warm_idle_max", 2)))
    max_slots = max(0, int(settings.get("max_warm_free_slots", 10)))
    live = [i for i in instances if i.get("state") == "live"]
    # Free slots the queue can already reach without keeping anything warm. Keyed on "still holds
    # work", not on source: a busy OWNED box's spare lanes are free capacity too, and an owned box
    # is never torn down, so they are at least as durable as a rental's.
    committed_free = sum(_free_slots(i) for i in live if i.get("occupants"))
    allowance = max(0, max_slots - committed_free)
    empty_paid = [i for i in live
                  if not i.get("occupants") and i.get("source", "vast") != "owned"]
    empty_paid.sort(key=lambda i: ((i.get("dph_usd") or 0.0), -(i.get("slots_total") or 0), i["id"]))
    granted, held = set(), 0
    for inst in empty_paid[:max_boxes]:
        free = _free_slots(inst)
        if held + free > allowance:
            break
        granted.add(inst["id"])
        held += free
    return granted


def _measured_free_vram(inst: dict):
    """Measured free GPU VRAM (GB) on `inst`, or None if it was never sampled (invariant 22).
    None means UNKNOWN — the VRAM gate is skipped (fall back to slot-count), never treated as 0."""
    tot, used = inst.get("vram_total_gb"), inst.get("vram_used_gb")
    if tot is None or used is None:
        return None
    return max(0.0, tot - used)


def _consolidation_near_done(occ: dict, settings: dict) -> bool:
    """Invariant 21d: True if `occ` is close enough to finishing that draining it would waste more
    (checkpoint + re-warm) than the box-time it saves. The signal is the est-based remaining window
    (`est_minutes × est_safety − running_minutes_ago`); a task PAST its estimate (remaining < 0) is
    NOT protected — a chronically-overdue paid box is exactly what to reclaim. A real per-task ETA /
    progress fraction would sharpen this (deferred: harness `progress.json`, spec Open questions)."""
    rem = occ.get("est_minutes", 0.0) * settings["est_safety"] - (occ.get("running_minutes_ago") or 0.0)
    return 0.0 <= rem <= settings.get("consolidate_min_remaining_min", 30)


def consolidation_drains(instances: list, settings: dict, now, queued_tasks: list | None = None,
                         last_drain_at: dict | None = None) -> list:
    """Invariant 21 (pure). The missing third leg of fleet sizing: placement (4b) packs owned-first
    and teardown (11) releases IDLE boxes, but a task that landed on a PAID box while the owned box
    was busy rides that box to completion. Given the post-placement instances view, return the
    graceful drains that vacate a paid box whose whole load fits onto OTHER capacity that stays alive
    regardless — `[{"instance_id", "task_ids", "targets", "dph_reclaimed"}]`. Empty when nothing is
    SAFELY consolidatable. No side effects; the caller executes each via the shared preempt path.

    A box is worth tearing down when its tasks fit on capacity that would be billing ANYWAY — the
    owned box ($0), or another box that stays alive for its OWN work. That captures both the
    paid→owned move AND the equal/any-priced COLLAPSE (two half-full boxes → one): killing the box
    saves its full `dph`, and riding a survivor's spare slot is ~$0 marginal (`_pack_cost`). It does
    NOT relocate onto an empty paid box (that box is itself idle → about to be torn down).

    Guards: whole-box only (21a); targets stay alive regardless & are never themselves torn down, and
    a box that RECEIVES a relocation is pinned so it can't also be vacated (21b — no ping-pong, no
    circular strand); measured-VRAM headroom when both sides measured, else slot-count (21c); never
    drain near-done work (21d, `_consolidation_near_done`). Sources are vacated most-expensive-first
    (largest $/hr reclaimed), ties broken toward the emptiest box (cheapest to clear)."""
    # A whole-box drain IS a preempt per occupant, so the global switch governs it too — checked
    # first so consolidation can never reintroduce churn the operator switched off.
    if not settings.get("preempt_enabled", True):
        return []
    if not settings.get("consolidate_enabled", True):
        return []
    live = [i for i in instances if i.get("state") == "live"]
    by_id = {i["id"]: i for i in live}
    margin = settings.get("consolidate_vram_margin_gb", 1.0)
    # Mutable free capacity per box, consumed as relocations are assigned (no double-booking).
    free = {i["id"]: {"slots": _free_slots(i), "vram": _measured_free_vram(i),
                       "dph": i.get("dph_usd", 0.0) or 0.0} for i in live}

    def needed_for_backlog(s: dict) -> bool:
        """Invariant 21e — a box we are about to NEED is not a box to reclaim.

        Draining is only worth its cost if the box then goes IDLE and is torn down. But
        `should_teardown` refuses to destroy any box a queued task still fits
        (`feasible_task_waiting`), so under a backlog the drained box stays `live`, gets REPACKED,
        and the drain buys nothing while costing a preempt per occupant.

        Measured 2026-07-29: **10 of 10 consolidations that day ended with the box still `live`** —
        zero teardowns — while 51 tasks were preempted and 6 lost their checkpoints outright. One
        box was drained at 04:49 and had work shipped back to it 10 min later. The contradiction was
        visible inside 50 seconds: `rent_created` x4 at 06:39:24-56 (26 tasks queued, no capacity)
        and `consolidate` at 06:40:14 draining a working paid box.

        Mirrors `should_teardown`'s own feasibility test exactly, so the two cannot disagree about
        whether a box is needed."""
        for t in (queued_tasks or []):
            # `minutes_to_hard_cap` is supplied by `_instances_view` in production; default to
            # infinity if absent so a missing field can never cause us to drain a NEEDED box —
            # the conservative direction for a guard whose whole job is to prevent pointless churn.
            if (t["slots"] <= s["slots_total"]
                    and _window_minutes_needed(t, settings)
                    <= s.get("minutes_to_hard_cap", float("inf"))):
                return True
        return False

    def projected_savings_usd(s: dict) -> float:
        """What tearing this box down actually saves: its rate x how much longer it would otherwise
        live. The box lives until its LAST occupant finishes, so the max est-based remaining window
        governs. This is an UPPER BOUND — `est_minutes` runs ~3.4x long on this fleet — which is the
        conservative direction for a test that must clear a floor before disrupting anything."""
        dph = s.get("dph_usd", 0.0) or 0.0
        remaining = 0.0
        for o in s.get("occupants", []):
            left = (o.get("est_minutes", 0.0) * settings["est_safety"]
                    - (o.get("running_minutes_ago") or 0.0))
            remaining = max(remaining, max(0.0, left))
        return dph * remaining / 60.0

    def worth_the_disruption(s: dict) -> bool:
        """Invariant 21g. A drain must save more than the disruption it inflicts, priced PER TASK
        preempted — measured median was 10 tasks for an upper-bound $0.174.

        EXEMPTION — a box with an OVERDUE occupant is always eligible. `projected_savings_usd` is
        est-based, and that estimate only means anything while a task is still inside it: past the
        window the remaining life is genuinely unknown, so pricing it at $0 would be an artefact,
        not a measurement. Invariant 21d already settled the policy for this case in the opposite
        direction — a chronically-overdue paid box is exactly what to reclaim, because it is
        over-billing with no predictable end. The cooldown still governs repeats, so this cannot
        reopen the drain-repack race. (Caught by the `overdue_task_is_drain_eligible` fixture, which
        my first cut of this test broke.)"""
        for o in s.get("occupants", []):
            if (o.get("est_minutes", 0.0) * settings["est_safety"]
                    - (o.get("running_minutes_ago") or 0.0)) < 0.0:
                return True
        n = max(1, len(s.get("occupants", [])))
        return (projected_savings_usd(s)
                >= settings["consolidate_min_savings_per_task_usd"] * n)

    def drained_too_recently(s: dict) -> bool:
        """Invariant 21g. Repack latency is median 5 min, well inside the 10-min idle timeout a
        drain needs to end in a teardown — so a box refilled that fast is drained again and again,
        each cycle costing a preempt per occupant and reclaiming nothing. Only a box that SURVIVED
        a drain can be re-drained, so this penalises exactly the pathological case."""
        prev = (last_drain_at or {}).get(s["id"])
        if prev is None:
            return False
        return (now - prev) < settings["consolidate_cooldown_min"] * 60

    def is_source_candidate(s: dict) -> bool:
        if (s.get("dph_usd", 0.0) or 0.0) <= 0.0 or s.get("source", "vast") == "owned":
            return False  # only PAID boxes are worth tearing down
        if s.get("drain_held"):
            return False  # 21h: already draining — re-issuing the drain just re-preempts stragglers
        if needed_for_backlog(s):
            return False  # 21e: queued work fits here — draining it cannot end in a teardown
        if drained_too_recently(s):
            return False  # 21g: drained recently and refilled — re-draining reclaims nothing
        if not worth_the_disruption(s):
            return False  # 21g: the saving does not justify preempting this many runs
        occ = s.get("occupants", [])
        if not occ or any(o.get("state") != "running" for o in occ):
            return False  # 21a: whole-box only; unsettled / drain already in flight
        if len(occ) > settings["consolidate_max_preempts"]:
            return False  # 21j: too many runs to interrupt for what one box-reclaim is worth
        return not any(_consolidation_near_done(o, settings) for o in occ)  # 21d

    def stays_alive(tid: int) -> bool:
        # A valid relocation target survives regardless of the source: the owned box ($0), or a paid
        # box kept alive by its OWN occupants. An empty paid box is going away — never a target.
        b = by_id[tid]
        if b.get("drain_held"):
            return False  # 21h: we are emptying this one — relocating ONTO it would undo that drain
        return b.get("source", "vast") == "owned" or len(b.get("occupants", [])) >= 1

    torn, pinned, drains = set(), set(), []
    # Most-expensive first (max $/hr reclaimed); ties -> fewest occupants (least to relocate) -> id.
    cands = sorted((s for s in live if is_source_candidate(s)),
                    key=lambda s: (-(s.get("dph_usd", 0.0) or 0.0), len(s.get("occupants", [])), s["id"]))
    for src in cands:
        sid = src["id"]
        if sid in pinned:
            continue  # already hosting someone else's relocated work -> must stay alive
        need = sum(o.get("slots", 1) for o in src.get("occupants", []))
        src_vram = src.get("vram_used_gb")  # footprint to relocate; None if unmeasured
        # Targets: surviving boxes (not this src, not torn, stay alive), cheapest/owned first so we
        # ride the least-cost survivors and minimize any lifetime extension.
        targets = sorted((tid for tid in free
                          if tid != sid and tid not in torn and stays_alive(tid)),
                         key=lambda tid: (free[tid]["dph"], tid))
        alloc, left, vram_avail, vram_known = [], need, 0.0, True
        for tid in targets:
            if left <= 0:
                break
            take = min(free[tid]["slots"], left)
            if take <= 0:
                continue
            alloc.append((tid, take))
            left -= take
            if free[tid]["vram"] is None:
                vram_known = False
            else:
                vram_avail += free[tid]["vram"]
        if left > 0:
            continue  # whole load doesn't fit on surviving capacity
        # 21c: VRAM safety — gate only when BOTH sides measured; else slot-count governs (as today).
        if src_vram is not None and vram_known and vram_avail < src_vram + margin:
            continue
        # Commit: tear down src, consume + pin the targets (charge footprint cheapest-first).
        rem_vram = src_vram or 0.0
        for tid, take in alloc:
            free[tid]["slots"] -= take
            pinned.add(tid)
            if free[tid]["vram"] is not None:
                charge = min(free[tid]["vram"], rem_vram)
                free[tid]["vram"] -= charge
                rem_vram -= charge
        torn.add(sid)
        drains.append({"instance_id": sid, "task_ids": [o["id"] for o in src.get("occupants", [])],
                        "targets": [tid for tid, _ in alloc], "dph_reclaimed": src.get("dph_usd", 0.0) or 0.0})
    return drains


def retry_decision(task: dict) -> str:
    """Invariant 10."""
    return "requeue" if task["retries_used"] < task["max_retries"] else "terminal"


def stall_decision(task: dict, now: float, settings: dict) -> bool:
    """Invariant 19 (2026-07-09, owner directive, following the utilization audit that found the
    48h hard cap alone doesn't catch a box that's `live` and billing but making no real progress).
    A `running` task's checkpoint file mtime not advancing for `stall_timeout_min` means the box
    is billing for no visible work.

    Invariant 19c' (bug 12): the clock restarts on every (re)start — `resume_checkpoint`
    survives a requeue by design (it's the retry's --init-from payload), so its mtime alone
    would instantly re-reap every retry. `running_since` (the task's latest CAS into `running`)
    caps the age.

    Invariant 19d (bug 13, 2026-07-13): `ckpt_mtime` is None until the FIRST checkpoint is ever
    pulled, which used to make a task that hangs before its first checkpoint (stuck in
    provisioning/startup) permanently invisible to this reaper — no mtime ever advances because
    none ever existed. The anchor is now the LATEST of whichever of `ckpt_mtime` /
    `running_since` are known, so a checkpoint-less task still ages off `running_since` (when
    it was last (re)started) once no checkpoint has shown up for `stall_timeout_min`. Only when
    BOTH are unknown (pre-d callers/fixtures) is the task left unflagged.

    Invariant 19f (2026-07-14): TB-event freshness is ALSO a liveness anchor, not just the
    checkpoint. `_pull_tb_events` rsyncs (with `-t`, preserving the remote mtime) a running task's
    `tb/` every poll, so the newest local `events.out.tfevents*` mtime tracks when the trainer last
    emitted a scalar. Many trainers stream TB continuously but write `ckpt_latest.pt` rarely, under
    a non-canonical name, or never (audit 2026-07-14) — anchoring on the checkpoint alone
    false-reaped those as "stalled" at `stall_timeout_min` even while they were demonstrably alive
    and making progress, killing the run and losing its `results.json` (so the dashboard showed
    nothing). `tb_mtime` closes that: a job actively logging is never reaped; a genuinely hung one
    (no new TB and no new checkpoint) still ages off the later of the two and is reaped. Checkpoint
    mtime still counts, so a checkpointing trainer keeps its resume guarantee — this only removes
    the false positive."""
    ckpt_mtime = task.get("ckpt_mtime")
    tb_mtime = task.get("tb_mtime")
    running_since = task.get("running_since")
    known = [v for v in (ckpt_mtime, tb_mtime, running_since) if v is not None]
    if not known:
        return False
    anchor = max(known)
    return (now - anchor) >= settings["stall_timeout_min"] * 60


def reconcile(db_instances: list, vast_instances: list, db_task_ids=frozenset(),
              now=None, provision_timeout_min=None) -> list:
    """Invariant 3. Adopt (3b, as revised 2026-07-09 — retrospective bug 7) requires the
    `runq_<task-id>` label's task-id to exist in THIS registry: a runq box for a task we've
    never heard of belongs to a DIFFERENT registry's live campaign (per-worktree split-brain)
    and adopting it — which stamps `hard_cap_at = now`, i.e. destroy-on-next-teardown — would
    shoot down someone else's active work. Unknown-task runq boxes are foreign: never touched.

    Provisioning-zombie reaper (invariant 3d, bug 10, observed live 2026-07-09 — twice,
    including once caused by `dispatcher_ctl.sh restart` itself): a daemon killed or restarted
    mid-`_provision` leaves the instance row stuck in `provisioning` forever — it's still on
    Vast (not `lost`), no task ever got assigned to it (`_rent` doesn't set a task's
    `instance_id` until a later pack), and nothing else ever revisits it. `provision_timeout_min`
    is opt-in (only checked when given, so existing callers/fixtures are unaffected) — a
    `provisioning` row older than it is reaped like any other stuck resource, no task-side
    cleanup needed since the triggering task, if still queued, was never taken out of `queued`.

    Owned-box carve-out: a `source='owned'` row (a statically-registered self-owned box, e.g. a
    home laptop/desktop — see `register_owned_box.py`) is never a Vast rental, so it will never
    appear in `vast_instances` and must never be flagged `lost` on that basis alone — skipped
    entirely, before either check, rather than only suppressing the `lost` branch, since the
    provisioning-zombie check is equally meaningless for a box that's never `_rent`/`_provision`ed."""
    vast_by_id = {v["id"]: v for v in vast_instances}
    db_ids = {i["id"] for i in db_instances}
    divergences = []
    for inst in db_instances:
        if inst.get("source", "vast") == "owned":
            continue
        if inst["state"] in ("provisioning", "live", "draining") and inst["id"] not in vast_by_id:
            divergences.append({"type": "lost", "instance_id": inst["id"]})
        elif (inst["state"] == "provisioning" and provision_timeout_min is not None
              and inst["id"] in vast_by_id
              and _age_minutes(inst.get("created_at"), now) >= provision_timeout_min):
            divergences.append({"type": "stuck_provisioning", "instance_id": inst["id"]})
    for v in vast_instances:
        if v["id"] in db_ids:
            continue
        label = v.get("label") or ""
        if label.startswith("runq_") and label[len("runq_"):] in db_task_ids:
            divergences.append({"type": "adopt", "instance_id": v["id"], "data": v})
        else:
            divergences.append({"type": "foreign_instance", "instance_id": v["id"]})
    return divergences


def _gpu_denied(gpu_name, gpu_deny) -> bool:
    """Invariant 4e: True iff `gpu_name` contains any `gpu_deny` substring (case-insensitive). A
    missing/empty name never matches — fail-open, an unlabelled offer is kept, not banned."""
    name = (gpu_name or "").lower()
    return any(str(s).lower() in name for s in (gpu_deny or ()))


def eligible_offers(raw_offers: list, settings: dict) -> tuple[list, int]:
    """Map raw `vastai search offers` records to the fields the placer needs (invariant 4a'/4e) and
    apply the three quality gates: (1) Vast reliability below `min_reliability` — read from
    `reliability2` (smoothed 0-1 score) falling back to `reliability`, an offer exposing NEITHER is
    KEPT (fail-open, so a missing/renamed metric can never silently starve the queue); (2) `gpu_name`
    matching a `gpu_deny` substring; (3) `cpu_cores_effective` below `min_cpu_cores_effective`.
    Returns (mapped_offers, n_dropped); `n_dropped` is for observability only."""
    min_reliability = settings.get("min_reliability", 0.0)
    gpu_deny = settings.get("gpu_deny", ())
    min_cores = settings.get("min_cpu_cores_effective", 0.0)
    mapped, dropped = [], 0
    for o in raw_offers:
        rel = o.get("reliability2")
        if rel is None:
            rel = o.get("reliability")
        cores = o.get("cpu_cores_effective") or 0
        if (rel is not None and rel < min_reliability) \
                or _gpu_denied(o.get("gpu_name"), gpu_deny) or cores < min_cores:
            dropped += 1
            continue
        mapped.append({"id": o.get("id"), "dph_total": o.get("dph_total", 0.0),
                       "machine_id": o.get("machine_id"), "gpu_name": o.get("gpu_name"),
                       "gpu_ram_gb": (o.get("gpu_ram") or 0) / 1024,
                       "cpu_cores_effective": cores,
                       "ram_gb": offer_ram_gb(o, settings),
                       # Invariant 4f: the CPU identity the speed term ranks on, and the identity a
                       # later re-fit needs. Carried even when the term is disabled, because the
                       # measurement that would justify tuning it is impossible without them.
                       "cpu_ghz": o.get("cpu_ghz"),
                       "cpu_name": (o.get("cpu_name") or "").strip() or None,
                       "cpu_arch": o.get("cpu_arch"),
                       "reliability": rel})
    return mapped, dropped


def offer_speed_factor(offer: dict, settings: dict) -> float:
    """Invariant 4f: per-core SPEED multiplier for the 4e value-density ranking, from `cpu_ghz`.

    WHY THIS EXISTS. The workload is CPU-bound, and measurably so: across 4,987 `box_measured`
    samples GPU utilisation is **0% at both p50 and p90**, exceeding 20% in 0.1% of rented-box
    samples, with **0.00 GB VRAM used** against the 0.6 GB/lane we declare. GPU model accordingly
    does not predict throughput — normalising each run's measured online steps/sec against its own
    group's rented-fleet median (608 TB-instrumented runs, 2026-08-04) gives RTX 3060 1.00, RTX 3090
    1.00, RTX 3060 Ti 1.08, RTX 2080 Ti 0.96. The one class that stands out is Tesla V100 at 1.48,
    and those are datacenter hosts whose distinguishing feature is the CPU, not the card. Meanwhile
    the fleet's own two owned boxes differ by 2.1x from each other (laptop-gpu 2.39 vs desktop 1.14
    on the same normalisation) at comparable co-residency, which is per-core speed and nothing else.

    So 4e's cores-per-$ ranking prices lane COUNT and is blind to lane SPEED: two 24-core offers at
    the same price rank identically even when one's cores are half as fast. This multiplies lane
    capacity by a speed estimate, turning the ranking from dollars-per-lane into dollars-per-unit-
    throughput. `gpu_deny` already tried to reach this signal — its own comment calls itself "a proxy
    for host generation (old GPU -> old/slow CPU) on the CPU-bound workload". This reads the CPU
    fields the marketplace actually publishes instead of inferring them from the card.

    HONEST LIMIT, and why it is CLAMPED. `cpu_ghz` is base clock, not throughput: it ignores IPC, so
    a 3.0 GHz Xeon E5-2686 v4 and a 3.0 GHz modern Ryzen score identically here and do not perform
    identically. It is a PRIOR, deliberately bounded by `cpu_speed_clamp` so it can re-order offers
    of similar price but never override the cores-per-$ or reliability decisions that are grounded in
    measurement. The right long-run signal is realised steps/sec per CPU model, which is exactly what
    the `cpu_name` recorded on `rent_created` (4f) accumulates toward; re-fit this from that history
    rather than widening the clamp on intuition.

    Fail-open: an offer that publishes no usable `cpu_ghz` scores 1.0 (neutral), never 0 — a missing
    or renamed marketplace field must never silently starve the queue of every offer, which is the
    same failure mode `eligible_offers`' reliability gate is written to avoid.
    """
    weight = float(settings.get("cpu_speed_weight", 0.0) or 0.0)
    if weight <= 0.0:
        return 1.0
    try:
        ghz = float(offer.get("cpu_ghz") or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if ghz <= 0.0:
        return 1.0
    ref = float(settings.get("cpu_ghz_reference", 3.0) or 3.0)
    if ref <= 0.0:
        return 1.0
    lo, hi = settings.get("cpu_speed_clamp", (0.75, 1.35))
    return max(float(lo), min(float(hi), (ghz / ref) ** weight))


def value_density(offer: dict, hint, settings: dict) -> float:
    """Invariant 4e+4f ranking key: dollars per unit of LANE THROUGHPUT (lower is better).

    `lane_capacity` counts lanes; `offer_speed_factor` prices how fast each one runs. Shared verbatim
    by `place()` and `offer_counterfactual` so the counterfactual can never report a premium against
    a ranking the placer did not actually use."""
    return offer["dph_total"] / (lane_capacity(offer, hint, settings)
                                 * offer_speed_factor(offer, settings))


def offer_ram_gb(raw_offer: dict, settings: dict) -> float | None:
    """The container RAM an offer will actually give us, in GB (invariant 27). None when the offer
    does not say — the RAM axis then abstains rather than denying the offer.

    `cpu_ram` is ALREADY THE SLICE'S RAM, not the host's, and this is the one fact the whole axis
    turns on. It scales with `cpu_cores_effective`, NOT `cpu_cores`: across 400 live offers the
    median is **2.61 GB per EFFECTIVE core** (a sane machine) against **0.38 GB per HOST core** (a
    56-core host with 10.5 GB, which is absurd). So it must NOT be re-scaled by
    `cpu_cores_effective / cpu_cores` — doing that under-predicts ~3x and would make the fit filter
    drop nearly every offer, starving the queue. That mistake was made and caught here.

    DERATED because the advertised figure runs OPTIMISTIC: measured `mem_limit_gb` on the 8 boxes we
    hold is 2.18 GB per effective core against the market's advertised 2.61 — offers over-promise by
    ~20%. `offer_ram_derate` (0.8) turns that into an under-promise, which is the safe direction for
    a filter that decides what to BUY. Small n (8 boxes, 1.25-4.27 range), so it is a settings knob,
    not a constant — and `rent_created` now records `ram_gb` beside the later-measured
    `mem_limit_gb`, so the pair accumulates and the derate can be re-fit from the fleet's own
    history instead of from this snapshot."""
    mb = raw_offer.get("cpu_ram")
    if not mb or mb <= 0:
        return None
    return (mb / 1024.0) * float(settings.get("offer_ram_derate", 0.8))


def offer_counterfactual(raw_offers: list, chosen_offer: dict, task: dict, settings: dict,
                          demand: int | None = None) -> dict:
    """Invariant 5d (observability only). Given the RAW `search offers` set the placer chose from,
    the offer we rented, the triggering task, and the placer's lane demand (`demand_slots`;
    defaults to the task's own slots and feeds only the `usable_now` context field), return a
    bounded summary of the cheapest offer we passed over and WHY. Recomputes the three quality gates
    (reliability / gpu_deny / cores floor) + `slots_for_offer` + the 4e cores-per-$ ranking over the
    raw set (independent of `eligible_offers`' return shape). Fail-open: a malformed offer is
    skipped, never raised — this must never abort a rent."""
    min_rel = settings.get("min_reliability", 0.0)
    gpu_deny = settings.get("gpu_deny", ())
    min_cores = settings.get("min_cpu_cores_effective", 0.0)
    hint = task.get("resource_hint")
    need = int(task.get("slots", 1) or 1)
    usable_now = max(int(demand or need), need)  # near-term absorbable lanes; context only
    chosen_dph = float(chosen_offer.get("dph_total", 0.0) or 0.0)
    try:
        chosen_cap = lane_capacity(chosen_offer, hint, settings)
        # Invariant 4f: rank on the SAME key the placer used, or the "worse_value_density" verdict
        # below is measured against a ranking nobody ran.
        chosen_per_lane = value_density(chosen_offer, hint, settings)
    except (TypeError, KeyError, ValueError, ZeroDivisionError):
        chosen_cap = 1  # fail-open: a chosen offer lacking capacity fields
        chosen_per_lane = chosen_dph
    chosen_speed = offer_speed_factor(chosen_offer, settings)
    considered = []
    n_qualifying = 0
    for o in raw_offers:
        try:
            dph = float(o.get("dph_total", 0.0) or 0.0)
            rel = o.get("reliability2")
            if rel is None:
                rel = o.get("reliability")
            cores = o.get("cpu_cores_effective") or 0
            mapped = {"gpu_ram_gb": (o.get("gpu_ram") or 0) / 1024, "cpu_cores_effective": cores,
                      "ram_gb": offer_ram_gb(o, settings), "dph_total": dph,
                      "cpu_ghz": o.get("cpu_ghz")}
            slots = slots_for_offer(mapped, hint, settings)
            cap = lane_capacity(mapped, hint, settings)
            per_lane = value_density(mapped, hint, settings)
        except (TypeError, ValueError, ZeroDivisionError):
            continue  # fail-open: a bad offer field never denies the rest of the counterfactual
        passes_rel = rel is None or rel >= min_rel
        passes_gpu = not _gpu_denied(o.get("gpu_name"), gpu_deny)
        passes_cores = cores >= min_cores
        passes_slots = slots >= need
        if passes_rel and passes_gpu and passes_cores and passes_slots:
            n_qualifying += 1
        considered.append({"dph": dph, "gpu": o.get("gpu_name"), "reliability": rel,
                           "passes_rel": passes_rel, "passes_gpu": passes_gpu,
                           "passes_cores": passes_cores, "passes_slots": passes_slots,
                           "cpu_ghz": o.get("cpu_ghz"), "cpu_name": o.get("cpu_name"),
                           "per_lane": per_lane})

    summary = {"n_considered": len(considered), "n_qualifying": n_qualifying,
               "chosen": {"dph": round(chosen_dph, 6), "gpu": chosen_offer.get("gpu_name"),
                          "reliability": chosen_offer.get("reliability"),
                          "lanes": chosen_cap, "usable_now": usable_now,
                          # 4f: both the CPU identity and the speed multiplier it produced, so the
                          # ranking is auditable after the fact from the event alone.
                          "cpu_name": chosen_offer.get("cpu_name"),
                          "cpu_ghz": chosen_offer.get("cpu_ghz"),
                          "speed_factor": round(chosen_speed, 4),
                          "dph_per_lane": round(chosen_per_lane, 6)},
               "cheapest_alt": None, "premium_dph": 0.0}
    if not considered:
        return summary
    cheapest = min(considered, key=lambda c: c["dph"])
    if cheapest["dph"] < chosen_dph - 1e-9:
        if not cheapest["passes_rel"]:
            reason = "reliability_below_floor"
        elif not cheapest["passes_gpu"]:
            reason = "gpu_denied"
        elif not cheapest["passes_cores"]:
            reason = "below_cores_floor"
        elif not cheapest["passes_slots"]:
            reason = "too_few_slots"
        elif cheapest["per_lane"] > chosen_per_lane + 1e-9:
            reason = "worse_value_density"  # loses the 4e cores-per-$ ranking — expected, not a bug
        else:
            reason = "qualified_not_chosen"   # cheaper AND wins/ties per lane — placer bug signal
        summary["cheapest_alt"] = {"dph": round(cheapest["dph"], 6), "gpu": cheapest["gpu"],
                                   "reliability": cheapest["reliability"],
                                   "dph_per_lane": round(cheapest["per_lane"], 6),
                                   "reason": reason}
        summary["premium_dph"] = round(chosen_dph - cheapest["dph"], 6)
    return summary


# --------------------------------------------------------------------------------------------
# ssh/rsync with proxy->direct fallback (invariant 9a) — impure, but the transport is injected
# (`run=`) so callers can stub it out in tests without a real network.
# --------------------------------------------------------------------------------------------

class ConnectionTracker:
    """Per-instance consecutive-failure counter driving the proxy->direct switch. Never
    switches back once switched (invariant 9a)."""

    def __init__(self, settings: dict):
        self._settings = settings
        self._fails: dict[int, int] = {}
        self._hb_pull_ok: dict[int, float] = {}
        self._direct: set[int] = set()
        self._direct_cache: dict[int, tuple[str, int]] = {}

    def is_direct(self, instance_id: int) -> bool:
        return instance_id in self._direct

    def consecutive_fails(self, instance_id: int) -> int:
        return self._fails.get(instance_id, 0)

    def record_heartbeat_pull(self, instance_id: int, ok: bool) -> None:
        """Stamp a successful HEARTBEAT pull — the ONLY operation that refreshes the mtime
        `_reap_dead_workers` judges a worker by. Deliberately separate from `record`: that one is fed
        by every ssh/rsync op on the box (ship, ingest, compile, teardown), so it answers "is the box
        reachable", not "is our heartbeat copy current". Conflating them is what let the reaper fire
        on a box the daemon was busy re-shipping to (2026-07-26)."""
        if ok:
            self._hb_pull_ok[instance_id] = time.time()

    def seconds_since_heartbeat_pull(self, instance_id: int) -> float | None:
        """Age of the last SUCCESSFUL HEARTBEAT pull, or None if this process never made one.
        `consecutive_fails == 0` says the last ATTEMPT succeeded but never WHEN, and a poll cycle
        that does not reach `_pull_worker_state` for this instance leaves our copy ageing with no
        failure recorded anywhere. `_reap_dead_workers` needs this age (invariant 10c)."""
        t = self._hb_pull_ok.get(instance_id)
        return None if t is None else max(0.0, time.time() - t)

    def record(self, instance_id: int, ok: bool) -> bool:
        """Returns True iff this call just triggered a proxy->direct switch."""
        if ok:
            self._fails[instance_id] = 0
            return False
        self._fails[instance_id] = self._fails.get(instance_id, 0) + 1
        if (not self.is_direct(instance_id)
                and self._fails[instance_id] >= self._settings["ssh_fallback_fails"]):
            self._direct.add(instance_id)
            return True
        return False

    def direct_endpoint(self, instance_id: int, vastai_run) -> tuple[str, int] | None:
        """`vastai ssh-url <id>` gives the DIRECT ip:port (VAST-TEST.md: sometimes firewalled,
        which is exactly why proxy is preferred first — but it's the only fallback we have)."""
        if instance_id in self._direct_cache:
            return self._direct_cache[instance_id]
        out = _run_or_timeout(vastai_run, ["vastai", "ssh-url", str(instance_id)], 30)
        if out.returncode != 0:
            return None
        m = re.match(r"ssh://root@([^:]+):(\d+)", out.stdout.strip())
        if not m:
            return None
        endpoint = (m.group(1), int(m.group(2)))
        self._direct_cache[instance_id] = endpoint
        return endpoint


def endpoint_for(instance: dict, tracker: ConnectionTracker, vastai_run=subprocess.run) -> tuple[str, int]:
    """Invariant 9a: prefer the proxy (`ssh_host`/`ssh_port` from the instance record); switch
    to direct after `ssh_fallback_fails` consecutive failures, never back."""
    if tracker.is_direct(instance["id"]) or not instance.get("ssh_host"):
        direct = tracker.direct_endpoint(instance["id"], vastai_run)
        if direct:
            return direct
    return instance["ssh_host"], instance["ssh_port"]


def _identity_opts() -> list:
    """`DISPATCHER_SSH_KEY`, if set, points at a private key to use instead of ssh-agent/the
    default identity — real Vast rentals never need this (the account's own keypair is already
    trusted), but it lets tests (docker integration test) point at an ephemeral test keypair
    without touching the real user's `~/.ssh`."""
    key = os.environ.get("DISPATCHER_SSH_KEY")
    return ["-i", key, "-o", "IdentitiesOnly=yes"] if key else []


def ssh_cmd(host: str, port: int) -> list:
    return ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", *_identity_opts(), f"root@{host}"]


def _rsync_ssh_opt(port: int) -> str:
    # ConnectTimeout matches ssh_cmd: a dead/hung proxy fails in 20s instead of eating the
    # whole per-attempt subprocess timeout (paid-campaign retrospective bug 6).
    extra = " ".join(_identity_opts())
    return (f"ssh -p {port} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
            + (f" {extra}" if extra else ""))


# Invariant 7g: where an interrupted pull parks its partial so the NEXT attempt resumes from it
# instead of leaking a fresh full-size temp. One well-known name per destination, so the reaper can
# recognise it — unlike rsync's default `.name.<6 random>`, of which every attempt makes a new one.
PARTIAL_DIR = ".rsync-partial"
# rsync's DEFAULT temp form, which is what leaked before 7g: a leading dot, then the real filename,
# then a 6-char random suffix — created mode 0600. Verified against the live tree 2026-07-31: this
# pattern AND that mode together matched 210.1 GB in 340 files and NOTHING else, so the two checks
# are used in conjunction and a file failing either is left alone.
_RSYNC_TEMP_RE = re.compile(r"^\..+\.[A-Za-z0-9]{6}$")
# A legacy orphan is garbage the moment it exists; this window only protects an IN-FLIGHT transfer
# (one rsync is typically mid-write at any moment) from having its own temp deleted underneath it.
_PULL_TEMP_MIN_AGE_H = 1.0
# A `.rsync-partial` entry is LOAD-BEARING while its task runs — 7g's whole point is that the next
# attempt resumes from it — so it is aged against the longest plausible run, not against a retry.
_PARTIAL_MAX_AGE_H = 48.0
# Terminal-task snapshots are kept this long so a just-failed task can still be inspected.
_SNAPSHOT_KEEP_H = 72.0


def _run_or_timeout(run, cmd, timeout: int) -> subprocess.CompletedProcess:
    """Transport calls NEVER raise on a hang (retrospective bug 6): a TimeoutExpired becomes an
    ordinary failed CompletedProcess (rc 124, the timeout(1) convention), so invariant 9a's
    consecutive-failure counter sees it and the poll loop survives it.

    Retrospective bug (2026-07-14, live fleet-wide wedge): `subprocess.run(timeout=...)` SIGKILLs
    only the DIRECT child on timeout, then blocks FOREVER in `communicate()` reading a pipe a
    surviving GRANDCHILD still holds (rsync spawns ssh; `vastai` spawns an http client). The daemon
    sat in `pipe_read`/`do_sys_poll` for 45+ min and every restart re-wedged on the first poll's
    reconcile/ingest — a timeout that doesn't actually free the loop. Fix: for the real subprocess
    path, launch the command in its OWN process group (`start_new_session`) and SIGKILL the WHOLE
    group on timeout, so no orphaned grandchild can keep the pipe open. An injected/fake `run` (unit
    tests, decision-core doubles) keeps the simple path — it never spawns a real process."""
    if run is not subprocess.run:
        try:
            return run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, stdout="", stderr=f"timeout after {timeout}s")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)  # own process group -> group-kill on timeout
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # child + every grandchild it spawned
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=5)  # reap the now-dead group; pipes close so this returns fast
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr=f"timeout after {timeout}s")


# Invariant 23b: a process-wide count of transport calls, so the cycle log can report rsync COUNT
# beside rsync SECONDS. Deliberately not on the Dispatcher: `rsync_pull` is a module-level function
# used by helpers that hold no dispatcher ref.
# ⚠ LOCKED, not a bare `+= 1`: invariant 23c calls `rsync_pull` from a thread per box, and `+=` on a
# dict value is LOAD/ADD/STORE — three bytecodes the interpreter may switch between, so concurrent
# increments silently drop counts. It is only instrumentation, but an undercount here would read as
# "barely any rsyncs" exactly when the fleet is busiest, which is the misreading this counter exists
# to prevent. `itertools.count` would also do; a lock is clearer about why.
_RSYNC_CALLS = {"n": 0}
_RSYNC_LOCK = threading.Lock()


def _rsync_calls() -> int:
    with _RSYNC_LOCK:
        return _RSYNC_CALLS["n"]


def rsync_pull(host: str, port: int, remote_path: str, local_path: str, includes: list,
               append: bool = False, run=subprocess.run) -> bool:
    """Pull from a box. **`--inplace` ONLY on the append path** — on any other pull it corrupts the
    destination, and the bigger the file the more certain that is.

    Invariant 7f (2026-07-30). `--inplace` writes straight into the destination instead of rsync's
    default temp-file-then-atomic-rename, so when `_run_or_timeout` SIGKILLs the transfer at 60s the
    local file is left TRUNCATED — while still carrying the name of a valid checkpoint. That is not a
    tail risk, it is the guaranteed outcome for any payload the link cannot move in 60s, so it fires
    EVERY pull and therefore corrupts `ckpt_latest.pt` AND `ckpt_latest.pt.prev` in turn. The `.prev`
    spare (`shared.infra.checkpoint.save_atomic`) exists precisely to survive a torn transfer and
    CANNOT help here, because the same mechanism destroys both generations.

    Measured live on `m49_pc_dream` (dream arms checkpoint the replay store, ~1.5 GB vs a 27 MB
    control): local `.prev` 1,092,354,048 B against 1,375,119,537 B on the box — 283 MB short. Box
    40000019 then died, five cells requeued with `resume_checkpoint` pointing at those files, and all
    five logged `PytorchStreamReader … archive is corrupted` → `STARTING FRESH`, losing ~3.7 h each.

    Without `--inplace` a killed pull leaves the previous good copy untouched, so the failure degrades
    to a STALE checkpoint instead of a corrupt one. ⚠ It does NOT make a large checkpoint pullable —
    60s is still 60s, so >~100-300 MB never lands and such a task simply has no resume point. Raising
    that timeout is a separate, fleet-wide call: `_pull_checkpoints` runs inside
    `_ingest_and_complete` at the TOP of `poll_once`, so a long per-task pull starves ingest exactly
    the way invariant 7c bounds the ship phase for.

    `--append` keeps `--inplace` (it is append's whole point) and is used only for `tb/**`, which is
    growth-only. Note the `--append` hazards already recorded above for `HEARTBEAT`/`worker.jsonl`.

    **`--partial-dir` (invariant 7g, 2026-07-31) — what 7f's "60s is still 60s" caveat cost.**
    Removing `--inplace` was right, but it restored rsync's temp-file-then-rename, and the SIGKILL
    above is UNCATCHABLE — so rsync can never clean its temp up. A payload the link cannot move in
    60s therefore leaks a FULL-SIZE `.name.XXXXXX` on every attempt, and `_pull_checkpoints` retries
    every `checkpoint_pull_every_min`. MEASURED 2026-07-31, 1.5 days after 7f landed: **342 orphans,
    210.1 GB — 62% of `experiments/`** (worst single run dir 25.8 GB), all mode 0600, accumulating
    ~160 GB per 12h while the 1.5-1.9 GB dream arms ran.

    `--partial-dir` fixes the leak and the caveat together. The partial goes to ONE well-known
    subdir per destination and is REUSED as the basis for the next attempt, so a big checkpoint
    CONVERGES across retries instead of restarting from zero every time — which is what makes it
    land at all, and therefore what gives those tasks a resume point. It cannot reintroduce 7f's
    corruption: the partial is only moved onto the destination name once complete, which is exactly
    the step `--inplace` skipped. rsync excludes the partial-dir from the transfer itself."""
    flags = ["-rtz"] + (["--inplace", "--append"] if append else [f"--partial-dir={PARTIAL_DIR}"])
    cmd = ["rsync", *flags, "-e", _rsync_ssh_opt(port)]
    for inc in includes:
        cmd += ["--include", inc]
    cmd += ["--exclude", "*", f"root@{host}:{remote_path}", local_path]
    with _RSYNC_LOCK:                           # invariant 23b/23c: count, seconds/call
        _RSYNC_CALLS["n"] += 1
    return _run_or_timeout(run, cmd, 60).returncode == 0


def rsync_push(host: str, port: int, local_paths: list, remote_path: str, run=subprocess.run) -> bool:
    """Invariant 7 — RESUMABLE. `--partial --inplace` is load-bearing, not a tuning nicety: without
    it a push that outruns the 120s budget is pure waste, because `_run_or_timeout` SIGKILLs the
    process group and the receiver then DELETES its temp file, so all three of `_ship`'s attempts
    restart at byte 0 and a payload the link cannot move in 120s can never be delivered — no matter
    how many polls try.

    Live 2026-07-29: instance 40000013 degraded to ~70 KB/s (siblings measured ~550-615 KB/s at the
    same moment, so this was the box, not our uplink). A 37.6 MB bundle needs ~9 min there. Watched
    `.bundle.tar.pXSN4I` reach 8.4 MB and vanish; every other `incoming/*/` on that box was empty.
    Each task burned 6m07s (3 x 120s + backoff) for zero bytes delivered, six tasks were claimed
    onto it, and the whole fleet's ships are SERIAL behind them — 36.5 min of every poll spent
    achieving nothing, repeating forever.

    With `--partial` the interrupted destination survives and rsync's delta pass sends only the
    missing tail, so a slow-but-real link converges across attempts instead of looping. `--inplace`
    writes straight to the destination (no temp file to discard). Safe against a partial being
    mistaken for a delivery: the worker claims only on `READY`, which is pushed strictly after this
    returns ok, and `_ship`'s idempotency check tests `READY`/`active/<id>` rather than bare
    existence (the 2026-07-09 fix) — and the bundle's sha256 manifest is verified on the box before
    extraction, so a truncated payload cannot silently run."""
    return rsync_push_detail(host, port, local_paths, remote_path, run=run)[0]


# rsync's own exit codes, for the two failures that are NOT a transport problem and that a message
# saying "transport failure" actively misdirects you away from. 11 is the one that matters here.
_RSYNC_EXIT = {11: "error in file I/O", 12: "protocol data stream error", 23: "partial transfer",
               24: "source files vanished", 30: "timeout in data send/receive", 255: "ssh error"}


def rsync_push_detail(host: str, port: int, local_paths: list, remote_path: str,
                      run=subprocess.run) -> tuple:
    """`(ok, reason)` — the same push, but the FAILURE SAYS WHAT HAPPENED.

    ⛔ WHY THIS EXISTS, measured 2026-08-12. `rsync_push` returned a bare bool, so every push failure
    reached the event log as `rsync push failed after 3 attempts to <host>:<port>` and the box-level
    line above it as **"transport failure on instance N"**. Both were WRONG on the incident that
    prompted this: owned box `tower` had a **100%-full disk** (98G/98G, 0 free). The box was
    up, sshd answered, the worker was alive and heartbeating — the push failed with ENOSPC, rsync
    exit **11**, and the message pointed at the network.

    The cost is not the words. `_ship_box_deferred` re-claimed and re-failed every ~68s for 34
    minutes, holding SIX tasks from FOUR unrelated campaigns (none of which ever started), and the
    only way to find out why was to ssh in and run `df` by hand. A reader who trusted the message
    would have gone looking at rsync flags, the proxy/direct fallback, or the link — none of which
    was the problem. This is the repo's own "a check must be able to fail for the reason it names",
    applied to a diagnostic: the message named a cause the evidence did not support.

    So: keep rsync's exit code and the tail of its stderr, and NAME the disk-full case explicitly,
    because it is the one an operator can fix in one command and the one the generic wording buries.
    Cheap, and it makes every future ship failure legible at the source instead of via `ssh + df`."""
    cmd = ["rsync", "-rtz", "--partial", "--inplace", "--mkpath", "-e", _rsync_ssh_opt(port),
           *local_paths, f"root@{host}:{remote_path}"]
    out = _run_or_timeout(run, cmd, 120)
    if out.returncode == 0:
        return True, ""
    err = " ".join((out.stderr or "").split())[-300:]
    rc = out.returncode
    if "No space left on device" in (out.stderr or ""):
        return False, (f"REMOTE DISK FULL on {host} (rsync exit {rc}) — this is NOT a transport "
                       f"fault; the box is reachable and out of space. Check `df -h /` and "
                       f"`du -sh ~/spool/*` on it. {err}")
    named = _RSYNC_EXIT.get(rc)
    return False, (f"rsync exit {rc}" + (f" ({named})" if named else "") + (f": {err}" if err else ""))


def ssh_run(host: str, port: int, remote_cmd: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return _run_or_timeout(run, ssh_cmd(host, port) + [remote_cmd], 30)


# box-pause inv. 20b: where the coordinator drops an owned box's capacity schedule inside the worker
# container. `owned_box_setup.sh` bind-mounts the host's `/var/lib/fleet-worker/control` here.
CAPACITY_PUSH_DIR = "~/fleet_host"


def capacity_push_cmd(payload: str | None) -> str:
    """Remote command writing `payload` (schedule JSON) atomically, or removing it when None.
    Base64 so the JSON never meets a shell quoting rule."""
    path = f"{CAPACITY_PUSH_DIR}/capacity.json"
    if payload is None:
        return f"rm -f {path}"
    import base64
    b64 = base64.b64encode(payload.encode()).decode()
    return (f"mkdir -p {CAPACITY_PUSH_DIR} && echo {b64} | base64 -d > {path}.tmp "
            f"&& mv -f {path}.tmp {path}")


# --------------------------------------------------------------------------------------------
# vastai CLI wrapper — thin, defensive JSON parsing (Input contract trust boundary).
# --------------------------------------------------------------------------------------------

def vastai_json(*args, run=subprocess.run):
    out = _run_or_timeout(run, ["vastai", *args, "--raw"], 30)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def account_balance(user_info: dict) -> float:
    """`vastai show user` splits spendable funds across `balance` (card-linked) and `credit`
    (purchased credit) — a credit-only account (`billing_creditonly=1`, common when no card is
    on file) reports `balance=0` even with real spendable credit, so the gate must sum both
    rather than reading `balance` alone."""
    return float(user_info.get("balance") or 0.0) + float(user_info.get("credit") or 0.0)


# Registry `settings` rows carrying the last balance the coordinator actually OBSERVED, and when
# (ISO-Z, `registry_db.now_iso`). Written by `Dispatcher._record_balance` on a successful reading
# only; read by the dashboard (`scripts/dashboard/dashboard_core.py`, a sanctioned direct consumer
# of this schema). The PAIR is the contract — a value with no fresh timestamp is a stale reading,
# not the current balance, and consumers must render it as such.
BALANCE_KEY = "observed_balance_usd"
BALANCE_AT_KEY = "observed_balance_at"


def _parse_deny_file(path: Path) -> set:
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split()[0]
        if head.isdigit():
            ids.add(int(head))
    return ids


def load_deny_machines() -> set:
    """Union of the static hand-maintained seed list and the runtime-learned denylist (the
    latter is where the daemon itself writes new entries -- see MACHINES_DENY_RUNTIME)."""
    return _parse_deny_file(MACHINES_DENY) | _parse_deny_file(MACHINES_DENY_RUNTIME)


# --------------------------------------------------------------------------------------------
# Poll loop (impure) — reconcile -> ingest -> place -> ship -> teardown.
# --------------------------------------------------------------------------------------------

def _iso_plus_hours(hours: float) -> str:
    import datetime
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_until(iso_ts: str) -> float:
    import datetime
    target = datetime.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
    return (target - datetime.datetime.utcnow()).total_seconds() / 60


def _age_minutes(created_at: str | None, now=None) -> float:
    """Minutes elapsed since `created_at` (invariant 3d). `now` is a real datetime or an ISO
    string (tests pass a fixed one for determinism); defaults to the real clock."""
    import datetime
    if not created_at:
        return 0.0
    created = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    ref = now
    if ref is None:
        ref = datetime.datetime.utcnow()
    elif isinstance(ref, str):
        ref = datetime.datetime.strptime(ref, "%Y-%m-%dT%H:%M:%SZ")
    return (ref - created).total_seconds() / 60


def _compile_error_summary(err: str, cap: int = 200) -> str:
    """Pull the actionable line out of a Cython compile failure, for the `fallback` note + `[ALERT]`.
    Cython's stderr ends with the golden `<file>:<line>:<col>: <message>` line and a final
    `...CompileError: <file>`; the FRONT (which the old `str(e)[:160]` note showed) is just
    multiprocessing-pool traceback noise. Prefer the golden line, then the CompileError line, else
    the last non-empty line — so a fallback is legible ('grounded_nav_world.py:219: Incompatible
    types ...'), not 'return fut.result(timeout)'."""
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    golden = [ln for ln in lines if re.search(r"\.py:\d+:\d+:", ln)]
    if golden:
        return golden[-1][:cap]
    ce = [ln for ln in lines if "CompileError:" in ln]
    if ce:
        return ce[-1][:cap]
    return (lines[-1] if lines else err.strip())[:cap]


@dataclass
class Dispatcher:
    db_path: str
    dry_run: bool = False
    run: object = field(default=subprocess.run)  # ssh/rsync/git — real by default
    vastai_run: object = None  # vastai CLI calls — separately injectable (tests stub this)
    tracker: ConnectionTracker | None = None
    # inv. 30: (title, message, tags, priority) -> (ok, error). None = ntfy via the env topic.
    notify: object = None

    def __post_init__(self):
        self.conn = registry_db.connect(self.db_path)
        self._ensure_settings()
        self.settings = self._load_settings()
        if self.tracker is None:
            self.tracker = ConnectionTracker(self.settings)
        if self.vastai_run is None:
            self.vastai_run = self.run
        self._last_ckpt_pull: dict[int, float] = {}
        # Last balance actually OBSERVED from the API. In-memory by design (a restart simply
        # re-reads it on the first successful poll, like `_box_res`): its only job is to stop a
        # TRANSIENT API failure from being scored as a 0.0 balance and shutting the fleet down.
        self._last_known_balance: float | None = None
        # Invariant 22: measured box GPU memory, refreshed on a cadence. In-memory by design (a
        # restart re-measures within `resource_measure_every_min`), mirroring the owned-unreachable
        # counter (invariant 20h) — no teardown/rent decision depends on it (invariant 1).
        self._box_res: dict[int, dict] = {}
        self._last_res_measure: dict[int, float] = {}
        # box-pause inv. 20b: (payload, pushed_at) last delivered to each owned box's host enforcer.
        # In-memory by design — a restart simply re-pushes once.
        self._cap_pushed: dict[int, tuple] = {}
        # Invariant 4i: set by `_apply_placement` when a FORCED claim lands, read and reset by
        # `_place_queue`, which then re-pushes the host schedule in the same poll (box-pause 20e-b).
        self._forced_placed = False
        self._seed_box_res()
        # inv. 11: which boxes run a worker that understands a `code_ref` bundle. In-memory only —
        # a capability is re-probed from scratch after a restart, which is correct: it is a property
        # of the box's live worker, not of anything the registry knows.
        self._box_caps: dict[int, bool] = {}
        # inv. 20i: last time each box's bootstrap code was refreshed on disk.
        self._last_worker_refresh: dict[int, float] = {}
        self._worker_refresh_held: dict[int, bool] = {}   # inv 20j-1, edge-triggered hold logging
        self._box_reattach_caps: dict[int, bool] = {}    # inv 20j-3, boxes whose worker re-adopts

    def _seed_box_res(self) -> None:
        """Invariant 28b: restore each live box's PREVIOUS CPU counter from the event log at startup.

        `cpu_used_cores` is a rate differenced against the prior sample, and `_box_res` is in-memory
        (invariant 1), so a restart leaves every box without a predecessor and `_cpu_pair` falls back
        to the HOST basis — the exact permissive reading invariant 28 exists to remove. Measured live
        2026-08-02, minutes after a restart: **all 6 cgroup-constrained boxes read
        `cpu_used_cores=None`**, i.e. the CPU half of 28 was inert fleet-wide, and would have stayed
        so for a full `resource_measure_every_min` on every box at once. Restarts are frequent
        (every coordinator merge is one), so this is the common case, not an edge case.

        The counter IS persisted (`_box_perf_payload`), so the predecessor is recoverable — this is
        the same "derive from persistent state, never an in-memory accumulator" rule
        `_learned_footprints` already follows.

        **Bounded by age on purpose.** A rate differenced across a long outage is an AVERAGE over
        that outage, which understates a box that has since become busy — and understating usage
        overstates headroom, the over-admit direction. Beyond `2 x resource_measure_every_min` the
        seed is discarded and the host fallback stands for one interval, exactly as today. Failures
        are non-fatal: this runs in `__post_init__` and must never stop the daemon booting."""
        try:
            every = self.settings.get("resource_measure_every_min", 5) * 60
            rows = self.conn.execute(
                "SELECT e.instance_id, e.t, e.detail FROM events e "
                "JOIN instances i ON i.id = e.instance_id "
                "WHERE e.event='box_measured' AND i.state IN ('live','paused') "
                "ORDER BY e.seq DESC LIMIT 400").fetchall()
        except sqlite3.Error:
            return
        now, seeded = time.time(), 0
        for r in rows:
            iid = r["instance_id"]
            if iid in self._box_res or not r["detail"] or "{" not in r["detail"]:
                continue  # first (newest) row per box wins; pre-25 samples carry no payload
            try:
                p = json.loads(r["detail"][r["detail"].index("{"):])
                at = datetime.datetime.strptime(
                    r["t"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except (ValueError, KeyError):
                continue
            if p.get("cpu_usage_usec") is None or now - at > 2 * every:
                continue
            self._box_res[iid] = {"cpu_usage_usec": p["cpu_usage_usec"], "at": at}
            seeded += 1
        if seeded:
            self.log("box_res_seeded",
                     f"restored {seeded} box CPU counter(s) from the event log "
                     f"(invariant 28b: without this the fleet reads the HOST basis for one cycle)")

    def _ensure_settings(self):
        existing = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}
        for k, v in DEFAULT_SETTINGS.items():
            if k not in existing:
                self.conn.execute("INSERT INTO settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
        # Move settings still on a superseded default forward (leaves customized values alone).
        for key, old, new in _SETTING_MIGRATIONS:
            if key in existing and existing[key] == json.dumps(old):
                self.conn.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(new), key))
        self.conn.commit()

    def _load_settings(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def log(self, event: str, detail: str, task_id=None, instance_id=None):
        registry_db.log_event(self.conn, event, detail, task_id=task_id, instance_id=instance_id)
        self.conn.commit()

    # -- reconcile (invariant 3) --
    def do_reconcile(self):
        db_instances = [dict(r) for r in self.conn.execute(
            "SELECT * FROM instances WHERE state IN ('provisioning','live','draining')")]
        # ⚠ FAIL-SAFE, NOT FAIL-OPEN. `vastai_json` returns None on ANY failure (non-zero exit,
        # unparseable output, DNS down, API 5xx), and `or []` used to turn that into an EMPTY
        # INSTANCE LIST — which reconcile reads as "every rented box has vanished". Measured
        # 2026-08-03: one failed poll marked 40000051 and 40000052 `lost`, requeued all 6 running
        # tasks onto the owned boxes, and 97 SECONDS LATER the API recovered and the coordinator
        # re-`adopt`ed both instances — by which time their work had been evacuated, so they read as
        # IDLE and the reaper destroyed two healthy paid-for boxes mid-run (13:48:38 lost →
        # 13:50:15 adopt → 13:50:33 teardown idle). An UNKNOWN fleet is not an EMPTY fleet: skip the
        # reconcile for this cycle and let the next poll decide.
        vast = vastai_json("show", "instances", run=self.vastai_run)
        if vast is None:
            self.log("api_unavailable",
                     "vastai show instances failed — SKIPPING reconcile this cycle "
                     "(an unreachable API is not an empty fleet)")
            return
        known_tasks = {r["id"] for r in self.conn.execute("SELECT id FROM tasks")}
        # Async provisioning (invariant 5b): a `provisioning` row is now visible to reconcile every
        # poll (it's advanced incrementally, not inline), so the stuck-provisioning ceiling must be
        # the BOOT ceiling (provision_boot_max_min=21), not the smaller provision_timeout_min (15) —
        # else a slow-but-good box booting at 15-21min is reaped before it comes up. reconcile owns
        # the timeout reap; _advance_provisioning (run just after) owns forward progress.
        divergences = reconcile(db_instances, vast, known_tasks,
                                 provision_timeout_min=self.settings["provision_boot_max_min"])
        for d in divergences:
            if d["type"] == "lost":
                self._mark_lost(d["instance_id"])
            elif d["type"] == "adopt":
                self._adopt(d["data"])
            elif d["type"] == "stuck_provisioning":
                self._destroy_stuck_provisioning(d["instance_id"])
            else:
                self.log("foreign_instance", f"instance {d['instance_id']} not ours", instance_id=d["instance_id"])

    def _destroy_stuck_provisioning(self, instance_id: int) -> None:
        row = self.conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if row is None:
            return
        row = dict(row)
        # Invariant 5b: async provisioning must not lose the inline path's dud-denial. A box past the
        # boot ceiling that REACHED running (ssh_host set) but never became usable has broken
        # ssh/spool-init — DENY its machine so we don't re-rent it. Safe (no doom-loop): it fires only
        # after ~21min of across-poll probe retries, not a single probe (contrast invariant 5's fix).
        # A box that never even reached running (ssh_host NULL) may be a transient slow image pull —
        # destroy WITHOUT denying, so a momentarily-overloaded host isn't banned permanently.
        if row.get("ssh_host"):
            self._blacklist_and_destroy(instance_id, MACHINES_DENY_RUNTIME,
                                         "boot ceiling: reached running but never became usable",
                                         machine_id=row.get("machine_id"))
            return
        # Invariant 5c: ...but "transient" has to be able to be REFUTED BY REPETITION. Counted, a
        # host that never boots is denied on its Nth consecutive failure; uncounted it is re-rented
        # forever, because the destroy leaves the offer in the market and `place` ranks it first
        # again on the next poll. See `stuck_provision_strikes_before_deny` for the live incident.
        strikes = self._consecutive_stuck_provisions(row.get("machine_id"))
        need = int(self.settings.get("stuck_provision_strikes_before_deny",
                                     DEFAULT_SETTINGS["stuck_provision_strikes_before_deny"]))
        if row.get("machine_id") is not None and strikes >= need:
            self._blacklist_and_destroy(
                instance_id, MACHINES_DENY_RUNTIME,
                f"stuck_provisioning x{strikes}: never reached running on {strikes} consecutive "
                f"rentals of this machine, no successful boot in between",
                machine_id=row.get("machine_id"))
        else:
            self._destroy(row, f"stuck_provisioning: never reached running "
                               f"(strike {strikes}/{need} for machine {row.get('machine_id')})")

    def _consecutive_stuck_provisions(self, machine_id) -> int:
        """How many of this machine's MOST RECENT rentals in a row never reached running, counting
        the one being torn down now.

        `ssh_host` is the record of having reached running — `_rent`/reconcile stamp it only once
        the box is actually up — so a rental with it NULL is one that never got there. The streak is
        read from OUR OWN `instances` table rather than the event log because that table is the
        authority on what we rented, survives a coordinator restart, and cannot be confused by an
        event a different code path wrote.

        ⚠ IT IS A STREAK, NOT A TOTAL, and that is what makes denial safe: one successful boot at
        any point resets it, so a machine that is merely having a bad hour is never banned for
        yesterday's failures. A machine with no `machine_id` (the API did not tell us) returns 0 and
        is never denied — invariant 21f's rule that an unidentifiable box cannot be blamed."""
        if machine_id is None:
            return 0
        n = 0
        for r in self.conn.execute(
                "SELECT ssh_host FROM instances WHERE machine_id=? ORDER BY created_at DESC, id DESC",
                (machine_id,)):
            if dict(r).get("ssh_host"):
                break
            n += 1
        return n

    def _realized_cost(self, instance_id) -> float:
        """dph_usd x hours(created_at -> now) — the same formula invariant 12's Output contract
        requires at both `destroy` and `lost` (registry spec: cost_usd stamped at either)."""
        row = self.conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if row is None:
            return 0.0
        import datetime
        # Tolerant parse. This used to run ONCE per box, at teardown, so a malformed `created_at`
        # could cost at most one cost stamp. Invariant 12b now calls it every poll cycle for every
        # live box, so the same exception would abort the WHOLE cycle — a far worse failure than an
        # unpriced box. `now_iso()` writes '...T...Z'; accept the bare SQL 'YYYY-MM-DD HH:MM:SS'
        # form too (what `datetime('now')` yields), and give up to $0 rather than raise.
        raw = (row["created_at"] or "").strip().replace("Z", "").replace(" ", "T")
        try:
            created = datetime.datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            self.log("cost_unpriced", f"unparseable created_at {row['created_at']!r}",
                      instance_id=instance_id)
            return 0.0
        hours = max(0.0, (datetime.datetime.utcnow() - created).total_seconds() / 3600)
        return (row["dph_usd"] or 0.0) * hours

    def _book_live_costs(self):
        """Invariant 12b — accrue `cost_usd` on LIVE boxes every cycle, not only at teardown.

        `cost_usd` used to be stamped exactly once, at `destroy`/`lost`. Every still-running box
        therefore read `cost_usd IS NULL`, so a spend query saw a spike only AFTER it ended — and
        `spend()` attributes a box's whole cost to one row, which a by-creation-date report then
        books entirely on the day the box was CREATED. Measured 2026-07-31: the registry showed
        $5.59 for the day while the fleet was actually burning $0.859/hr ($20.61/day), with $11.05
        already accrued and invisible on 13 live boxes. Booking it here makes the running total
        honest between teardowns; the value is idempotent (recomputed from dph x elapsed, never
        incremented), so repeated cycles and a later teardown stamp all converge on the same number.

        Owned boxes have `dph_usd = 0`, so they book $0 and are left alone by the same formula."""
        # fetchall() BEFORE updating: the UPDATEs below write the same table this selects from, and
        # mutating under a live cursor is undefined in sqlite3.
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM instances WHERE destroyed_at IS NULL "
            "AND COALESCE(dph_usd,0) > 0").fetchall()]
        for instance_id in ids:
            self.conn.execute("UPDATE instances SET cost_usd=? WHERE id=?",
                              (self._realized_cost(instance_id), instance_id))
        self.conn.commit()

    def _mark_lost(self, instance_id):
        cost = self._realized_cost(instance_id)
        self.conn.execute("UPDATE instances SET state='lost', destroyed_at=?, cost_usd=? WHERE id=?",
                           (registry_db.now_iso(), cost, instance_id))
        self.log("lost", f"instance {instance_id} missing from vastai show instances", instance_id=instance_id)
        for t in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN ('claimed','shipped','running','preempting')",
                (instance_id,)):
            self._infra_fail(dict(t))

    def _adopt(self, data: dict):
        # Idempotent: reconcile can emit `adopt` for a Vast id that already has a row (e.g. a
        # stale/terminal record for the same instance), which crashed the poll loop on the
        # UNIQUE(id) constraint and stalled the whole fleet (2026-07-11). On conflict, re-adopt
        # in place — mark live and refresh the mutable connection/hardware fields; leave
        # created_at/cost_usd/slots_total untouched. Clear destroyed_at: a `lost`/`destroyed` row
        # carries a terminal timestamp, and the spend query sums cost_usd `WHERE destroyed_at >= ?`,
        # so a box re-adopted back to `live` must drop it or it keeps counting against spend-since.
        #
        # ⚠ `slots_total` IS OURS, NOT VAST'S — it is derived from the OFFER at rent time
        # (`slots_for_offer`, invariant 4a'), and NO `vastai show instances` record carries such a
        # field (verified against the live API 2026-08-03). So the adopt payload never has it, and
        # the old `ON CONFLICT … slots_total=excluded.slots_total` wrote the `.get(…, 1)` DEFAULT
        # over the correct rent-time sizing — silently capping a re-adopted box at ONE lane for the
        # rest of its life, since `_effective_slots` only ever takes `min` and nothing recomputes it.
        # Measured: of 200+ rentals since 2026-07-25 the only three rows with `slots_total=1` are the
        # only three that were ever re-adopted. The last was a 7.4c/hr RTX 3070 whose own offer
        # (8 GB VRAM / 28 effective cores / 50 GB RAM against a 1 GB, 2-core hint) sized it at the
        # full 8 lanes — it packed one task instead of eight after a transient `vastai` failure
        # marked it `lost` and the next poll re-adopted it.
        #
        # The re-adopt path is the common one (8 of 11 adopts in registry history). A genuinely
        # untracked box (no prior row, so no offer was ever sized) still falls back to 1 lane and
        # SAYS so: under-packing an unknown box is the safe error, and the instance record cannot be
        # sized like an offer without care — its `cpu_ram` is the HOST's, not the slice's (the 3070
        # above: 128675 MB on the instance vs 64337.5 MB on the offer, i.e. exactly `cpu_ram *
        # gpu_frac`), so feeding it to `offer_ram_gb` would over-provision by 1/gpu_frac.
        known = self.conn.execute("SELECT slots_total FROM instances WHERE id=?",
                                  (data["id"],)).fetchone()
        self.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
            "ssh_host, ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state='live', destroyed_at=NULL, machine_id=excluded.machine_id, "
            "label=excluded.label, dph_usd=excluded.dph_usd, gpu_name=excluded.gpu_name, "
            "ssh_host=excluded.ssh_host, ssh_port=excluded.ssh_port",
            (data["id"], data.get("machine_id"), data.get("label", ""), registry_db.now_iso(),
             "live", data.get("dph_total", 0.0), data.get("gpu_name"), data.get("ssh_host"),
             data.get("ssh_port"), data.get("slots_total", 1),
             registry_db.now_iso()))
        if known is None:
            self.log("adopt", f"adopted untracked instance {data['id']} — no rent-time sizing on "
                              f"record, defaulting to 1 lane", instance_id=data["id"])
        else:
            self.log("adopt", f"adopted untracked instance {data['id']} "
                              f"(kept slots_total={known['slots_total']})", instance_id=data["id"])
        self.conn.commit()

    def _infra_fail(self, task: dict, reason: str = "instance lost or heartbeat/claim timeout"):
        r = registry_db.transition(self.conn, task["id"], "infra_failed", "infra_failed", reason)
        if not r.ok:
            return
        decision = retry_decision(dict(registry_db.get_task(self.conn, task["id"])))
        if decision == "requeue":
            registry_db.transition(
                self.conn, task["id"], "queued", "requeue", "retrying after infra failure",
                # Invariant 10 (2026-07-09, owner directive): an infra failure is not the task's
                # fault, so it only costs HALF a retry — `max_retries=10` allows up to 20
                # infra-driven requeues before terminal. `task_failed` (a real crash/science
                # failure) never reaches this path at all and stays permanently terminal, so
                # the starter gets the fastest possible signal that it needs attention.
                extra_set={"retries_used": task["retries_used"] + 0.5, "instance_id": None})
        else:
            self.log("terminal", "max_retries exhausted after infra failure", task_id=task["id"])

    # -- one poll iteration --
    def poll_once(self):
        """Invariant 23 (2026-07-31): the cycle TIMES ITSELF, per phase, into one `poll_cycle` event.

        `poll_seconds` is 30, but a real cycle is dominated by serial network I/O — ~5 rsyncs per box
        in `_ingest_and_complete`, each up to a 60 s timeout, plus a 300 s ship budget — so its true
        length is an EMERGENT property of how many boxes are live and how slow they are, and it was
        measured NOWHERE. That mattered because at least three tuned settings are really predicates
        on it: `heartbeat_stale_min` (15) silently disables the dead-worker reaper once the cycle
        exceeds it (invariant 10c(g)), `ship_budget_sec` (300) sets ship throughput as a DUTY CYCLE
        of it, and `checkpoint_pull_every_min` (5) cannot outpace it.

        Measured 2026-07-31, the incident this came from: cycle median 31.0 min against a ~7-9 min
        design point — so ship duty had collapsed to 16% (~14 ships/hr, a 26-task `claimed` backlog,
        one cell sitting 4h42m between claim and ship) and the reaper was skipping 97.9% of boxes.
        Both numbers were derivable only by reverse-engineering timestamps out of unrelated events
        (`ship_budget_spent` gaps), which is hours of work to answer "is the loop keeping up?".

        One event per cycle (~70/day at the observed rate) makes that a single query, and names the
        phase to blame rather than just the total. Read it with:
            SELECT t, detail FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 20;
        """
        phase_sec: dict[str, float] = {}

        def timed(name, fn):
            t0 = time.monotonic()
            try:
                fn()
            finally:
                phase_sec[name] = round(time.monotonic() - t0, 1)

        timed("reconcile", self.do_reconcile)
        # ingest/completion/checkpoint pulls and ship/teardown I/O are invoked here in the live
        # (non-dry-run) daemon; kept out of --dry-run (spec: no vastai/ssh calls, no DB writes).
        if not self.dry_run:
            # BEFORE any ssh this cycle: the identity a box is reached with is registry state, not
            # a hand-edited file (see _sync_ssh_config).
            timed("ssh_config", self._sync_ssh_config)
            # cheap + FIRST: see _refresh_heartbeats for why order matters
            timed("heartbeats", self._refresh_heartbeats)
            # invariant 5b: move provisioning boxes toward live, non-blocking
            timed("provision", self._advance_provisioning)
            timed("ingest", self._ingest_and_complete)
            timed("cancels", self._signal_cancels)
            timed("drain", self._signal_drain)  # box-pause spec: evict a hard-paused owned box's work
            timed("over_capacity", self._reap_over_capacity)  # capacity spec: shed a box to its cap
            timed("capacity_push", self._push_capacity_schedules)  # inv. 20b: host enforcer's copy
            timed("probe", self._consume_box_probes)
            timed("box_requests", self._consume_box_requests)  # on-demand reachability (remote-submit 19a)
            timed("measure", self._measure_box_resources)  # invariant 22: sample real box VRAM
            timed("book_cost", self._book_live_costs)  # invariant 12b: accrue live spend in-flight
        timed("place", self._place_queue)
        if not self.dry_run:
            timed("consolidate", self._consolidate)  # invariant 21: drain paid boxes onto free capacity
            timed("ship", self._ship_all)
            timed("teardown", self._teardown_idle)
            timed("gc_staging", self._gc_ship_staging)  # inv. 12: .ship/ leaked 106GB / 2783 dirs
            timed("gc_temps", self._gc_pull_temps)      # inv. 7g: pull temps leaked 210GB / 342
            timed("gc_snapshots", self._gc_code_snapshots)  # inv. 12a: snapshots leaked 55GB / 2686
            timed("worker_refresh", self._refresh_workers)  # inv. 20i: workers self-update on new code
        # Logged even on the exception path (each `timed` records in `finally`), so a phase that
        # BLEW UP still shows the time it burned — a silent crash mid-cycle is otherwise invisible.
        # Not in `--dry-run`: every phase this measures is one that mode skips, so the numbers would
        # be meaningless, and the mode's contract is no ssh/vastai calls and no DB writes.
        if self.dry_run:
            return
        total = round(sum(phase_sec.values()), 1)
        n_live = self.conn.execute(
            "SELECT COUNT(*) FROM instances WHERE state IN ('live','paused')").fetchone()[0]
        self.log("poll_cycle", json.dumps(
            {"total_sec": total, "n_boxes": n_live,
             # the two settings this cycle length silently governs — logged so a future reader sees
             # the comparison without having to know it matters (10c(g) / invariant 7c).
             # ⚠ NOT "the reaper is off" — that is what this field used to mean and the name caused a
             # live misdiagnosis (2026-07-31): an operator read `reaper_disabled: true` on a HEALTHY
             # fleet and was one step from ordering an unnecessary `dispatch-restart`. Before 3681a1cb
             # the dead-worker reaper really was gated on `cycle_time < heartbeat_stale_min`, because
             # `since_ok` could only refresh once per cycle. 10c(g) removed that dependence — it now
             # pulls the HEARTBEAT inline and re-stats — so a long cycle no longer disables anything.
             # Measured after the fix: 0 `dead_worker_skipped` vs 183 in the 12h before it, with
             # `dead_worker` reaps still landing. The cycle-vs-threshold comparison is STILL worth
             # logging (it drives ship duty and checkpoint pull cadence), so it keeps its place under
             # a name that states the fact instead of an obsolete consequence.
             "cycle_over_heartbeat_stale": total >= self.settings["heartbeat_stale_min"] * 60,
             "ship_duty": round(phase_sec.get("ship", 0.0) / total, 2) if total else 0.0,
             "phases": dict(sorted(phase_sec.items(), key=lambda kv: -kv[1])),
             # invariant 23b: the dominant phase's own breakdown + transport call count
             "ingest_detail": getattr(self, "_ingest_detail", None)}))

    def _refresh_heartbeats(self):
        """Pull ONLY `HEARTBEAT`, for every live/paused box, at the TOP of the poll cycle.

        The reaper judges a worker by our pulled copy's mtime, but that copy was refreshed only inside
        `_ingest_and_complete`, which does ~4 rsyncs PER INSTANCE (worker.jsonl, HEARTBEAT, TB events,
        markers, checkpoints — each up to a 60s timeout) and is followed by `_ship_all`'s compiles and
        ships (15-560s apiece). So a "30-second" poll cycle routinely runs many minutes, and the one
        transfer the reaper depends on — a 0-byte file — sat behind all of it. Measured 2026-07-26 on
        owned box -2: the copy ran 5-8 min stale while the worker was demonstrably alive (heartbeat
        touched every 60s, verified over ssh), which first DESTROYED healthy work and, once the
        `seconds_since_heartbeat_pull` guard stopped that, left the guard skipping every reap
        (`dead_worker_skipped` at 23:48 and 23:54) — i.e. the dead-worker reaper effectively disabled
        on the busiest box in the fleet.

        Hoisting it here is the actual fix: one tiny rsync per box, before any expensive work, so the
        mtime tracks the remote regardless of how slow the rest of the cycle is — which both keeps the
        reaper able to see a genuinely dead worker and keeps the guard's skips rare and meaningful.
        `_pull_worker_state` still pulls it too (harmless, and it keeps that function correct on its
        own); this only guarantees a floor on freshness."""
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state IN ('live','paused')")]:
            local = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}"
            local.mkdir(parents=True, exist_ok=True)
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            ok = rsync_pull(host, port, "~/spool/", str(local) + "/",
                             ["HEARTBEAT"], append=False, run=self.run)
            self.tracker.record_heartbeat_pull(inst["id"], ok)

    # -- ingest + completion (invariant 9) --
    def _ingest_and_complete(self):
        # Box-pause spec inv. 7: pull `paused` boxes too, not just `live` — a hard drain's PREEMPTED
        # markers + checkpoints must be ingested for the drain to complete, and a soft-frozen box's
        # HEARTBEAT/fail-count stay current (harmless — no checkpoint changes while frozen).
        # Invariant 23b: ingest is the cycle's dominant phase (measured 960s of 1475s, 65%), so it
        # gets the same per-sub-phase breakdown 23 gives the cycle — otherwise "ingest is slow" is
        # as unactionable as "the cycle is slow" was. The rsync COUNT is logged beside the seconds
        # because the two decide different fixes: many cheap calls ⇒ parallelise the transport (they
        # are latency-bound and `_run_or_timeout` is already thread-safe); few expensive ones ⇒ the
        # payload is the problem and a budget/size cap is the lever. Note the per-TASK sub-phases
        # (`tb`, `checkpoints`) scale with running tasks, not boxes, so they dominate a full fleet.
        sub: dict[str, float] = {"worker_state": 0.0, "markers": 0.0, "payloads_wall": 0.0}
        n_rsync = dict.fromkeys(sub, 0)
        before = _rsync_calls()

        def _sub(name, fn, *a):
            """Time a sub-phase and count the transport calls it made, recording both even if it
            raises. Returns whatever `fn` returned, so it can wrap a fan-out as easily as a call."""
            t0 = time.monotonic()
            start_n = _rsync_calls()
            try:
                return fn(*a)
            finally:
                sub[name] += time.monotonic() - t0
                n_rsync[name] += _rsync_calls() - start_n

        insts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM instances WHERE state IN ('live','paused')")]
        # Resolve every endpoint SERIALLY first: `endpoint_for` can shell out to `vastai ssh-url`
        # on the proxy->direct switch AND mutates tracker state, so it may not run off-thread.
        endpoints = {i["id"]: endpoint_for(i, self.tracker, self.vastai_run) for i in insts}

        # -- The two sub-phases that drive state transitions and terminal completions.
        # `worker_state` must precede the payload pulls (it is what moves `shipped -> running`, and
        # the payload plan selects on `running`); `markers` before them is a deliberate improvement
        # on the old order — a task that finished this cycle is completed first, and `_complete_done`
        # pulls `tb/**` itself, so nothing is lost by not also pulling it as a running task.
        #
        # PARALLEL I/O, SERIAL APPLY (invariant 23e, 2026-08-02) — the same shape the payload pass
        # below already uses, and the fix the 23b comment above predicted ("many cheap calls ⇒
        # parallelise the transport"). These are three latency-bound round trips per box (two rsyncs
        # + one ssh) that were run strictly one box after another, so they cost ~9-15 s PER BOX and
        # made the whole cycle scale linearly with fleet size: measured over 400 live cycles, total
        # 96 s median / 195 s p90 / 415 s max against a `poll_seconds` of 30, with ingest 70% of it
        # and these two sub-phases ~29% of ingest.
        # The ORDER CONSTRAINT is per-box, not global — box A's worker_state has nothing to do with
        # box B's — so fanning out the I/O preserves it as long as every mutation still happens on
        # this thread, in `insts` order, after all the I/O lands.
        ws_plan = [(inst, *endpoints[inst["id"]]) for inst in insts]
        mk_plan = [(inst, *endpoints[inst["id"]]) for inst in insts if self._has_open_tasks(inst)]

        def _fan(plan, fn):
            """Fan `fn` across boxes, returning {instance_id: result}. A box that RAISES is simply
            absent from the result — never propagated — because one unreachable box must not take
            the whole ingest phase (and so every other box's completions) with it. Same contract
            the payload pass keeps via its per-box `error` field."""
            if not plan:
                return {}
            def guard(i, h, p):
                try:
                    return (True, fn(i, h, p))
                except Exception as e:                       # noqa: BLE001 — per-box isolation
                    return (False, e)
            w = max(1, min(int(self.settings["ingest_parallel_boxes"]), len(plan)))
            if w == 1:
                raw = {i["id"]: guard(i, h, p) for i, h, p in plan}
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=w) as pool:
                    futs = {pool.submit(guard, i, h, p): i["id"] for i, h, p in plan}
                    raw = {futs[f]: f.result() for f in concurrent.futures.as_completed(futs)}
            out = {}
            for iid, (ok, val) in raw.items():
                if ok:
                    out[iid] = val
                else:
                    self._ingest_box_failed(iid, val)
            return out

        ws_io = _sub("worker_state", _fan, ws_plan,
                     lambda i, h, p: self._pull_worker_state_io(i, h, p))
        mk_io = _sub("markers", _fan, mk_plan, lambda i, h, p: self._pull_markers_io(h, p))
        for inst in insts:                             # SERIAL: all DB + tracker mutation
            if inst["id"] in ws_io:
                self._apply_worker_state(inst, *ws_io[inst["id"]])
            if inst["id"] in mk_io:
                self._apply_markers(inst, *endpoints[inst["id"]], mk_io[inst["id"]])

        # -- PARALLEL: per-task payloads, ONE WORKER PER BOX (invariant 23c). Plan serially (DB
        # reads + the per-box checkpoint throttle), fan out pure I/O, then apply serially.
        plan, now = [], time.time()
        ckpt_interval = self.settings["checkpoint_pull_every_min"] * 60
        for inst in insts:
            tasks = self._running_on(inst["id"])
            if not tasks:
                continue
            due = now - self._last_ckpt_pull.get(inst["id"], 0.0) >= ckpt_interval
            if due:
                self._last_ckpt_pull[inst["id"]] = now
            host, port = endpoints[inst["id"]]
            plan.append((inst["id"], host, port, tasks, due))

        t0, start_n = time.monotonic(), _rsync_calls()
        results: list = []
        if plan:
            width = max(1, min(int(self.settings["ingest_parallel_boxes"]), len(plan)))
            if width == 1:
                results = [(iid, self._pull_box_payloads(h, p, ts, d))
                           for iid, h, p, ts, d in plan]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=width) as pool:
                    futs = {pool.submit(self._pull_box_payloads, h, p, ts, d): iid
                            for iid, h, p, ts, d in plan}
                    results = [(futs[f], f.result())
                               for f in concurrent.futures.as_completed(futs)]
        sub["payloads_wall"] = time.monotonic() - t0
        n_rsync["payloads_wall"] = _rsync_calls() - start_n
        for iid, res in results:                       # SERIAL: all DB + tracker mutation
            self._apply_box_payloads(iid, res)

        work = {"tb": round(sum(r["tb_sec"] for _, r in results), 1),
                "checkpoints": round(sum(r["ckpt_sec"] for _, r in results), 1)}
        wall = sub["payloads_wall"]
        self._ingest_detail = {
            "sec": {k: round(v, 1) for k, v in sorted(sub.items(), key=lambda kv: -kv[1])},
            "rsyncs": dict(n_rsync),
            "total_rsyncs": _rsync_calls() - before,
            # summed per-thread work vs wall-clock == the parallelism actually achieved. If this
            # sits near 1.0 while several boxes had tasks, the fan-out is not working — that is the
            # number to look at before touching `ingest_parallel_boxes`.
            "payload_work_sec": work,
            "payload_boxes": len(plan),
            "payload_speedup": round(sum(work.values()) / wall, 1) if wall > 0.05 else None,
        }
        self._reap_stalled()
        self._reap_dead_workers()
        self._reap_orphaned_tasks()
        self._reap_overpacked_boxes()
        # AFTER the over-pack reaper: 19h owns a box that is provably launching and requeues at no
        # retry cost, so it must get first refusal on a gate-held `shipped` task before 10b (whose
        # shorter 15min timeout would otherwise charge half a retry for the same symptom).
        self._reap_unclaimed_ships()
        # AFTER 10b: that one owns a task we DID deliver (state `shipped`), this one a task we never
        # could (state `claimed`). Disjoint by state, so the order is for readability, not conflict.
        self._reap_undeliverable_claims()
        self._reap_unreachable_owned()
        self._reap_paused_soft_timeout()

    def _reap_orphaned_tasks(self):
        """Invariant 19g (2026-07-15): requeue any task in an on-box state (`claimed`/`shipped`/
        `running`/`preempting`) whose instance is NOT live-ish (destroyed/lost/gone).

        `_mark_lost` only requeues occupants of an instance that was still tracked as
        provisioning/live/draining and then VANISHED from `vastai show instances`; once a box is
        `_destroy`ed (teardown or dead-worker reap), or the daemon was wedged when it died, its
        still-`claimed` occupant is examined by nothing — `_ship_all` skips a non-live box, the stall
        reaper watches only running/preempting, and the dead-worker reaper needs a live box with a
        stale HEARTBEAT. So the task strands forever. Observed live 2026-07-15: two tasks sat
        `claimed` on boxes destroyed 8h earlier. This pass closes the gap by looking at TASKS whose
        instance isn't live rather than at instances. `provisioning`/`draining` are intentionally
        treated as live-ish (a task claimed onto a box still coming up is normal, not orphaned).
        Runs after `do_reconcile` (which settles instance states this poll), so states are current;
        `_infra_fail` requeues via the same half-retry path as every other infra loss."""
        # Box-pause spec inv. 3: `paused` is live-ish here — a task frozen (soft) or draining (hard)
        # on a deliberately-paused owned box is NOT an orphan; the soft-timeout watchdog / drain path
        # own its fate, not this reaper (which would wrongly requeue frozen work).
        live_ish = {r["id"] for r in self.conn.execute(
            "SELECT id FROM instances WHERE state IN ('provisioning','live','draining','paused')")}
        for t in [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE state IN ('claimed','shipped','running','preempting')")]:
            if t["instance_id"] not in live_ish:
                self.log("orphaned",
                          f"instance {t['instance_id']} not live -> requeue", task_id=t["id"])
                self._infra_fail(t, reason="orphaned: instance destroyed/lost")

    def _reap_unreachable_owned(self):
        """Invariant 20h (2026-07-16): soft, self-healing quarantine for a fully-unreachable owned
        box, so a dead home-farm box (SSH/rsync failing outright — laptop off/asleep/off-LAN) can't
        wedge the fleet. An owned box stays `live` through reconcile/`_destroy` (20a/20c) and
        pack-first placement always prefers it, yet none of the other reapers can free a
        `claimed`-but-unshipped task on it (the dead-worker reaper is suppressed by
        `consecutive_fails > 0` — exactly what an unreachable box produces; stall/orphan need a
        running clock or a non-live box) and it can't be denied (`machine_id` is NULL). This closes
        the gap WITHOUT `_destroy` (owned boxes refuse it and never re-adopt) or the denylist (keys
        on machine_id): quarantine to a new `unreachable` state (excluded from `place`'s
        `state=='live'` pool) and probe back to `live` the moment it answers ssh."""
        thr = self.settings["owned_unreachable_fails"]
        # Quarantine: consecutive ssh/rsync failures (pulls AND ships feed the same tracker) past the
        # threshold -> `unreachable`, releasing stranded on-box tasks via the usual half-retry path.
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='live' AND source='owned'")]:
            fails = self.tracker.consecutive_fails(inst["id"])
            if fails < thr:
                continue
            self.conn.execute("UPDATE instances SET state='unreachable' WHERE id=?", (inst["id"],))
            self.log("owned_unreachable",
                      f"{fails} consecutive ssh/rsync failures (>= {thr}) -> quarantine",
                      instance_id=inst["id"])
            for t in self.conn.execute(
                    "SELECT * FROM tasks WHERE instance_id=? AND state IN "
                    "('claimed','shipped','running','preempting')", (inst["id"],)):
                self._infra_fail(dict(t), reason="owned box unreachable: quarantined")
            self.conn.commit()
        # Recovery: each poll, try to bring each quarantined owned box's worker back up. A box goes
        # `unreachable` because ssh/rsync failed outright (laptop off/asleep/off-LAN), and the usual
        # way it returns — a reboot or power-cycle — kills the box-resident `spool_worker.py`. A bare
        # `echo` reachability check would flip it to `live` with NO worker to claim the tasks packed
        # onto it: pack-first placement keeps shipping, nothing claims, and `_destroy` is refused for
        # owned boxes (20c) — the exact wedge 20h exists to prevent, in a new disguise. So recovery
        # re-runs the full worker bring-up (whose own ssh probe subsumes the `echo`); only its success
        # (worker up — freshly started, or already alive and left untouched, idempotently) resets the
        # failure counter and re-admits the box to `live`. A still-dead box, or one whose worker won't
        # start, records another failure and stays quarantined.
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='unreachable' AND source='owned'")]:
            ok = self._bring_up_worker(inst, MACHINES_DENY_RUNTIME)
            self.tracker.record(inst["id"], ok)
            if ok:
                self.conn.execute("UPDATE instances SET state='live' WHERE id=?", (inst["id"],))
                self.log("owned_recovered", "worker brought back up -> live",
                          instance_id=inst["id"])
                self.conn.commit()

    # -- owned-box pause / drain (box-pause spec) --
    def _pause_key(self, iid: int) -> str:
        # Per-box, mirroring `_overpack_cap_key`'s `i<id>` form (owned box id is negative, e.g. i-1).
        return f"pause_i{iid}"

    def _pause_meta(self, iid: int):
        """The live pause record for a box, or None. Read fresh from the DB (NOT `self.settings`,
        a start-time snapshot) so a runtime pause/resume issued by `box_pause.py` is seen."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (self._pause_key(iid),)).fetchone()
        return json.loads(row["value"]) if row else None

    def _set_pause_meta(self, iid: int, meta: dict) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self._pause_key(iid), json.dumps(meta)))
        self.conn.commit()

    def _clear_pause_meta(self, iid: int) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._pause_key(iid),))
        self.conn.commit()

    def _live_setting(self, key: str):
        """One setting read from the DB NOW, falling back to the start-time snapshot. `self.settings`
        is loaded once in `__init__`, so anything an operator must be able to change without a
        coordinator restart is read through here (same reasoning as `_worker_roll_max_draining`)."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return self.settings.get(key)
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return self.settings.get(key)

    def _should_be_frozen(self, inst: dict) -> bool:
        """box-pause 14a: the box's `~/spool/FREEZE` marker should exist iff the registry says the
        box is SOFT-paused. A `hold` never freezes, a `hard` drain runs to checkpoint, and a box in
        any other state is not paused at all."""
        if inst.get("state") != "paused":
            return False
        meta = self._pause_meta(inst["id"])
        return bool(meta) and meta.get("mode") == "soft"

    def _note_freeze_reconciled(self, inst: dict, frozen: bool, probe_stdout: str) -> None:
        """Log the drift `box_assert_cmd` just corrected, if there was one (box-pause 14a). The box
        reports what it held BEFORE the assert; silence — an old reply shape, a truncated one —
        concludes nothing. One event per correction, and the next probe finds agreement."""
        was = parse_freeze_report(probe_stdout)
        if was is None or was == frozen:
            return
        meta = self._pause_meta(inst["id"]) or {}
        should = (f"paused ({meta.get('mode')})" if inst.get("state") == "paused"
                  else str(inst.get("state")))
        self.log("freeze_reconciled",
                 f"box held a FREEZE marker but the registry says {should}: removed — it was "
                 f"launching nothing and holding its trainers stopped"
                 if was else
                 f"box had no FREEZE marker but the registry says {should}: created",
                 instance_id=inst["id"])

    def _evict_task_graceful(self, host: str, port: int, task: dict, reason: str) -> None:
        """The shared graceful-drain primitive (box-pause inv. 12 / capacity scale-down / cost
        consolidation inv. 21, all on top of the invariant-17 preempt path): mark a RUNNING task
        `preempting` and touch its `PREEMPT` marker ONCE. The trainer checkpoints then exits at its
        next save (invariant 17b — no in-flight work is interrupted before a checkpoint), requeues
        with resume (invariant 16), and re-packs. Touch-ONCE is load-bearing: re-touching every poll
        keeps the marker mtime ahead of every checkpoint so the box-side kill guard (`ckpt newer than
        marker`) can never fire for a trainer that checkpoints slower than the poll (2026-07-22 live
        bug). Callers must pass only `running` occupants and commit the transaction."""
        registry_db.transition(self.conn, task["id"], "preempting", "preempt_intent", reason)
        ssh_run(host, port, f"touch ~/spool/active/{task['id']}/PREEMPT", run=self.run)

    def _signal_drain(self) -> None:
        """Box-pause spec inv. 12: evict a hard-paused owned box's work so it requeues on the fleet,
        then the box holds `paused` (no new work) until an explicit resume. A box reaches mode 'hard'
        either directly (`box_pause.py drain`) or by soft-pause timeout escalation. Reachable box →
        graceful PREEMPT (checkpoint-then-exit, no retry cost — the existing preempt path); an
        unreachable box (laptop asleep/off-LAN) → `_infra_fail` (requeue at half a retry), the same
        tradeoff `_reap_unreachable_owned` makes. Idempotent: re-touching PREEMPT each poll is safe,
        and a box with no open occupants is skipped."""
        thr = self.settings["owned_unreachable_fails"]
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='paused' AND source='owned'")]:
            meta = self._pause_meta(inst["id"])
            if not meta or meta.get("mode") != "hard":
                continue
            occ = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN ('running','preempting')",
                (inst["id"],))]
            # Invariant 12b: UNDELIVERED work (`claimed`/`shipped`) is drained too. It has no process
            # to PREEMPT, so a marker cannot evict it and the `running` half above is structurally
            # blind to it — which is the whole bug: on 2026-08-12 a box whose disk filled held three
            # `claimed` cells that never shipped, was hard-drained with ZERO running occupants, and
            # this method `continue`d for 9 hours while every other reaper skipped it by design
            # (invariant 12c enumerates why none of them can fire). Queried separately from `occ`
            # because the two halves take different actions, and disjoint by state so a mixed box
            # drains both in one poll.
            undelivered = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN ('claimed','shipped')",
                (inst["id"],))]
            if not occ and not undelivered:
                continue
            unreachable = self.tracker.consecutive_fails(inst["id"]) >= thr
            if unreachable:
                for t in occ + undelivered:
                    self._infra_fail(t, reason="drain: owned box paused + unreachable -> requeue")
                continue
            if undelivered:
                self._drain_undelivered(inst, undelivered)
            # Act ONLY on `running` occupants — a task already `preempting` has its PREEMPT marker
            # and belongs to the worker now. Re-touching the marker every poll (the original bug,
            # found live 2026-07-22) keeps its mtime fresher than every checkpoint, so the box-side
            # kill guard (`ckpt newer than marker`, invariant 17b) can NEVER fire for a trainer that
            # checkpoints slower than the poll interval — it would run undrained forever. Touch once,
            # at the transition, then leave the marker stable so the trainer's next checkpoint evicts
            # it. (Mirrors the once-touch semantics of the `_apply_placement` preempt path.)
            newly = [t for t in occ if t["state"] == "running"]
            if not newly:
                continue  # every occupant already carries its PREEMPT marker; nothing to re-issue
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            # A drain never freezes; clear any stale FREEZE (e.g. left by a soft phase we escalated)
            # so the worker isn't holding these procs stopped while they run to their checkpoint.
            ssh_run(host, port, "rm -f ~/spool/FREEZE", run=self.run)
            for t in newly:
                self._evict_task_graceful(host, port, t,
                                          "drain: owned box paused (hard) -> evict + requeue")
            self.log("drain_signal",
                      f"{len(newly)} task(s) evicted (PREEMPT) from paused owned box",
                      instance_id=inst["id"])
            self.conn.commit()

    def _drain_undelivered(self, inst: dict, undelivered: list) -> None:
        """Box-pause inv. 12b: requeue the `claimed`/`shipped` half of a REACHABLE hard-drained box's
        load. Split out of `_signal_drain` because the action is different in kind, not degree — this
        work has no process, so there is nothing to PREEMPT and nothing to wait for a checkpoint on;
        it is simply handed back to the queue.

        Two things are load-bearing:

        * **Clear the box's copy BEFORE the requeue.** A hard drain explicitly `rm -f`s `FREEZE`
          (invariant 11), so unlike a soft pause the worker on this box is NOT stopped — a `shipped`
          task still sitting in its spool would be launched by `launch_ready` while the queue hands
          the same task to another box. That is the double-run the same pre-clear guards against in
          invariants 10b/10d/19h; a drain needs it MORE than they do, not less, because the box is
          alive and polling by construction. A failed cleanup therefore skips that task entirely and
          retries next poll — never requeue what we could not delete.
        * **No retry cost.** A drain is the operator emptying the box; the task did nothing wrong,
          and charging it half a retry for our scheduling decision is how a task that has bounced
          across a few pauses exhausts `max_retries` and dies for no reason. Same reasoning as 10d's
          free requeue ("the scheduler failing to deliver, not the task misbehaving"). The
          unreachable branch in `_signal_drain` still pays half a retry — there we cannot clear the
          spool, so the requeue is a genuine infra loss with a real double-run risk, and that
          asymmetry is deliberate."""
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        requeued = 0
        for t in undelivered:
            res = ssh_run(host, port,
                          f"rm -rf ~/spool/incoming/{t['id']} ~/spool/active/{t['id']}",
                          run=self.run)
            if res.returncode != 0:
                self.log("drain_undelivered_defer",
                          f"box cleanup failed rc={res.returncode} — will retry next poll",
                          task_id=t["id"], instance_id=inst["id"])
                continue
            registry_db.transition(
                self.conn, t["id"], "queued", "drain_undelivered",
                f"drain: owned box paused (hard) with this task still {t['state']} (never started) "
                "-> requeue elsewhere; retry cost unchanged (the operator emptied the box, not a "
                "task failure)",
                extra_set={"instance_id": None})
            requeued += 1
        if requeued:
            self.log("drain_undelivered",
                      f"{requeued} undelivered task(s) (claimed/shipped) requeued from paused owned "
                      "box at no retry cost",
                      instance_id=inst["id"])
            self.conn.commit()

    def _reap_paused_soft_timeout(self) -> None:
        """Box-pause spec inv. 10: a soft pause ("back soon, don't requeue") auto-escalates to a hard
        drain once its owner hasn't resumed within `soft_pause_timeout_min`, so a forgotten short
        pause can't strand the laptop's jobs frozen forever. Escalation just flips the mode to
        'hard' (log `pause_timeout`); `_signal_drain` does the eviction. Durable across a dispatcher
        restart — the clock reads `pause_i<ID>.at` (wall-clock ISO), no in-memory timer (invariant 1)."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        limit = self.settings.get("soft_pause_timeout_min", 30) * 60
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='paused' AND source='owned'")]:
            meta = self._pause_meta(inst["id"])
            if not meta or meta.get("mode") != "soft":
                continue
            try:
                paused_at = datetime.datetime.strptime(
                    meta.get("at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            except (TypeError, ValueError):
                continue  # malformed timestamp -> leave it to a human, never mis-escalate
            if (now - paused_at).total_seconds() < limit:
                continue
            self._set_pause_meta(inst["id"], {"mode": "hard", "at": meta["at"],
                                              "escalated_at": registry_db.now_iso()})
            self.log("pause_timeout",
                      f"soft pause exceeded {self.settings.get('soft_pause_timeout_min', 30)}min "
                      "-> escalate to hard drain (requeue elsewhere + hold)",
                      instance_id=inst["id"])

    # -- time-of-day capacity schedule (capacity spec) --
    def _capacity_schedule(self, inst: dict):
        """The parsed `configs/capacity/<label>.json` for an owned box, or None (no box / no file /
        malformed). A malformed schedule is logged and treated as uncapped — never wedges the poll."""
        if inst.get("source") != "owned":
            return None
        name = f"{inst['label']}.json"
        path = registry_db.site_file(f"capacity/{name}", ROOT / "configs" / "capacity" / name)
        if not path.exists():
            return None
        try:
            return capacity.load(path.read_text())
        except Exception as e:  # noqa: BLE001 — a bad schedule must never abort a poll
            self.log("capacity_error", f"{path.name}: {e}", instance_id=inst["id"])
            return None

    def _push_capacity_schedules(self) -> None:
        """box-pause inv. 20b: deliver each owned box's schedule to its HOST enforcer (inv. 20c).

        The host has no repo checkout and the coordinator has no shell on the host — only on the
        worker container. So the schedule rides the ssh the coordinator already holds, into
        `~/fleet_host/capacity.json`, which `owned_box_setup.sh` bind-mounts from the host. Pushed
        on change and every `capacity_push_every_min`. A failed push records nothing, so it retries
        next poll.

        ⛔ A BOX WITH NO SCHEDULE IS PUSHED A FULLY-OPEN ONE, NEVER A MISSING FILE (inv. 20b-1).
        The file used to be removed on the reading that the enforcer treats absence as uncapped.
        It could not: it lifted the cap with `docker update --cpus 0`, and Docker reads that zero as
        "field not supplied". A box whose schedule was REMOVED therefore kept its last window's CPU
        cap forever, the update exiting 0 every minute. Measured on `desktop`, 2026-10-03: schedule
        deleted to make the box fleet-only, and eleven minutes later the container still read
        `cpu 9.82/10.00 cores` with eleven tasks on it."""
        every = self.settings.get("capacity_push_every_min", 30) * 60
        now = time.time()
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE source='owned' AND state IN ('live','paused')")]:
            sched = self._capacity_schedule(inst)
            # Inv. 20e: while a FORCED task occupies the box the HOST gets an uncapped schedule, so
            # the job is not throttled by the window's `--cpus` / GPU-power caps it was admitted
            # past. Only the pushed copy changes — `_capacity_slots`/`_capacity_budget` keep reading
            # the configured schedule, so every other task is still gated by the real window.
            forced = self._forced_occupants(inst) if sched is not None else []
            if sched is None:
                # 20b-1: sized from the box's MEASURED cores. Unmeasured yet (the measure phase runs
                # later in the poll, and not at all on an unreachable box) ⇒ push nothing this pass
                # rather than guess; and never remove what is there.
                cores = (self._box_res.get(inst["id"]) or {}).get("cores")
                if not cores:
                    continue
                pushed_sched = capacity.fully_open(cores)
            else:
                pushed_sched = capacity.uncapped(sched) if forced else sched
            payload = json.dumps(
                {k: v for k, v in pushed_sched.items() if not k.startswith("_")},
                sort_keys=True, separators=(",", ":"))
            last = self._cap_pushed.get(inst["id"])
            # The override is recorded in the settings kv, so a transition is detected even across
            # a coordinator restart (where `last` is None anyway) and is never throttled by the
            # re-push interval.
            recorded = self._capacity_override(inst["id"])
            overridden = recorded is not None
            if (last is not None and last[0] == payload and now - last[1] < every
                    and overridden == bool(forced)):
                continue
            try:
                host, port = endpoint_for(inst, self.tracker, self.vastai_run)
                proc = ssh_run(host, port, capacity_push_cmd(payload), run=self.run)
            except Exception:  # noqa: BLE001 — delivery is best-effort and must never break the poll
                continue
            if getattr(proc, "returncode", 1) != 0:
                continue
            self._cap_pushed[inst["id"]] = (payload, now)
            if last is None or last[0] != payload:
                self.log("capacity_pushed",
                         payload if sched is not None else f"fully open (no schedule): {payload}",
                         instance_id=inst["id"])
            # Recorded only AFTER the push succeeded, so the record never claims a state the host
            # does not hold; a failed push leaves it untouched and the transition retries next poll.
            if forced and not overridden:
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (f"capacity_override_i{inst['id']}",
                     json.dumps({"at": registry_db.now_iso(), "tasks": forced})))
                self.log("capacity_override",
                          f"host caps LIFTED (cpu 1.0 / vram 1.0 / default GPU power, all day) while "
                          f"forced task(s) {forced} occupy this box; the coordinator still admits "
                          f"other work against the configured schedule",
                          # Keyed to the task as well, so `runq show <task>` carries the whole
                          # trail: forced_placement -> capacity_override -> ..._lifted.
                          task_id=forced[0], instance_id=inst["id"])
            elif overridden and not forced:
                self.conn.execute("DELETE FROM settings WHERE key=?",
                                  (f"capacity_override_i{inst['id']}",))
                was_for = (recorded or {}).get("tasks") or [None]
                self.log("capacity_override_lifted",
                          "no forced task left on this box — the configured schedule is back on "
                          "the host" if sched is not None else
                          "no forced task left on this box (it has no configured schedule)",
                          task_id=was_for[0], instance_id=inst["id"])

    def _capacity_override(self, instance_id: int):
        """The recorded host-schedule override for an owned box (inv. 20e-d), or None. Lives in the
        settings kv beside `pause_i<ID>` so a restart cannot forget that the host is uncapped."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                (f"capacity_override_i{instance_id}",)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            value = None
        # Unreadable or not an object -> still "an override is recorded", so it gets lifted.
        return value if isinstance(value, dict) else {}

    def _is_forced_on(self, task_row: dict, inst_row: dict) -> bool:
        """Is this task a FORCED occupant of this box (invariant 4i)? The DB-row twin of `place()`'s
        test: the hint forces a box, that box is THIS one, and this one is owned. A forced hint on
        a task sitting anywhere else is an ordinary task and is treated as one."""
        if inst_row.get("source") != "owned":
            return False
        try:
            hint = json.loads(task_row["resource_hint_json"]) if task_row.get(
                "resource_hint_json") else None
        except (TypeError, ValueError):
            return False
        if not isinstance(hint, dict):
            return False
        tgt = forced_box(hint)
        return tgt is not None and _box_matches(inst_row, tgt)

    def _forced_occupants(self, inst_row: dict) -> list:
        """Ids of the forced tasks occupying this box — the same occupant states `_instances_view`
        uses, so "on the box or about to be" means one thing in the packer and in the host push."""
        return [t["id"] for t in (dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE instance_id=? AND state IN "
            "('claimed','shipped','running','preempting') ORDER BY id", (inst_row["id"],)))
            if self._is_forced_on(t, inst_row)]

    def _capacity_slots(self, inst: dict, now=None):
        """Slots an owned box may run RIGHT NOW under its `configs/capacity/<label>.json` schedule,
        inferred from the current window's CPU + VRAM caps (whichever binds). None if the box has no
        schedule (uncapped). The lane footprint comes from the schedule when it declares one, else
        the global settings default (inv. 18)."""
        sched = self._capacity_schedule(inst)
        if sched is None:
            return None
        if now is None:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
        return capacity.effective_slots(sched, now, self.settings["cores_per_lane"],
                                        self.settings["vram_per_lane_gb"])

    def _capacity_budget(self, inst: dict, now=None):
        """The current window's ABSOLUTE `{cores, vram_gb}` budget for an owned box (inv. 18a), which
        placement admits real summed task footprints against. None if the box has no schedule."""
        sched = self._capacity_schedule(inst)
        if sched is None:
            return None
        if now is None:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
        return capacity.window_budget(sched, now)

    def _reap_over_capacity(self):
        """Scheduled capacity scale-DOWN: when a live owned box's time-of-day cap drops below its
        running load (e.g. the day window kicks in), gracefully evict the excess — checkpoint-then-
        exit + requeue via the drain preempt path — newest-first, so it sheds down to the cap without
        losing the longest-running work. running->preempting is immediate, so the next poll sees the
        lower load and won't over-evict. A paused box (cap 0) is handled by `_signal_drain` instead."""
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='live' AND source='owned'")]:
            eff = self._capacity_slots(inst)
            budget = self._capacity_budget(inst)
            if eff is None and budget is None:
                continue
            running = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state='running' ORDER BY updated_at DESC",
                (inst["id"],))]
            # Keep OLDEST-first (shed newest-first, as before), admitting each against BOTH the slot
            # cap and the window's absolute cores/VRAM budget (inv. 19 + 18a) — a slot count alone
            # under-counts a box whose tasks are bigger than the settings lane.
            kept_cores = kept_vram = 0.0
            kept, shed = 0, []
            # Invariant 4i-5: a FORCED occupant is OUTSIDE this reaper's arithmetic altogether —
            # neither shed nor charged. Not shed, whatever the preempt switch says: it was admitted
            # past this very cap on the owner's say-so, so evicting it the next time the reaper runs
            # would undo the bypass one poll later. Not charged, because a forced task must never
            # EVICT what was already running (4i-1): counting its footprint here would push the
            # box's existing occupants over the cap and shed them one poll after it arrived. The
            # other occupants are therefore judged against the window exactly as if it were absent.
            # (ADMISSION is different and unchanged — there its footprint does count, 4i-4.)
            running = [t for t in running if not self._is_forced_on(t, inst)]
            for t in reversed(running):  # oldest-started first
                hint = json.loads(t["resource_hint_json"]) if t.get("resource_hint_json") else None
                cores, vram = task_footprint(hint, t["slots"], self.settings)
                over_slots = eff is not None and kept + 1 > eff
                over_budget = budget is not None and (
                    kept_cores + cores > budget["cores"] + 1e-9
                    or kept_vram + vram > budget["vram_gb"] + 1e-9)
                if over_slots or over_budget:
                    shed.append(t)
                else:
                    kept, kept_cores, kept_vram = kept + 1, kept_cores + cores, kept_vram + vram
            if not shed:
                continue
            bound = (f"cap {eff} slots" if eff is not None else "")
            if budget is not None:
                bound = (bound + ", " if bound else "") + (
                    f"budget {budget['cores']:.1f} cores / {budget['vram_gb']:.1f}GB")
            # Global preempt switch: do not evict, but SAY SO rather than going silent — the box is
            # genuinely over its window cap and the operator should be able to see that. Placement
            # still refuses to admit anything new over the cap (inv. 18a/23), so the overshoot is
            # bounded by the running tasks' own remaining time and drains without interrupting them.
            if not self.settings.get("preempt_enabled", True):
                self.log("capacity_over_cap_tolerated",
                          f"{len(running)} running exceeds {bound} by {len(shed)} — NOT evicting "
                          f"(preempt_enabled=false); will drain as tasks finish",
                          instance_id=inst["id"])
                continue
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            for t in reversed(shed):  # newest-started first
                self._evict_task_graceful(host, port, t,
                                          f"capacity scale-down: {len(running)} running exceeds {bound}")
            self.log("capacity_scaledown",
                      f"evicting {len(shed)} of {len(running)} running (this window: {bound}; "
                      f"keeping {kept} = {kept_cores:.1f} cores / {kept_vram:.1f}GB)",
                      instance_id=inst["id"])
            self.conn.commit()

    # -- ssh identity, owned by the REGISTRY (spec: `remote-submit.spec.md` inv. 22a, branch `spec/remote-submit`) --
    def _sync_ssh_config(self) -> None:
        """Write `~/.ssh/config.fleet` from the registry, and Include it FIRST from `~/.ssh/config`.

        WHY THIS EXISTS (owner, 2026-09-17: *"this should be managed at the coordinator level. I
        should be able to trivially add these"*). The identity for a box used to be chosen by a
        `Host` pattern in a root-owned file inside the secrets mount, which nothing in the registry
        knew about. So the registered ADDRESS and the config PATTERN had to agree, and nothing
        enforced it: re-pointing owned box -2 from `172.17.0.1` to its real address silently matched
        no pattern, ssh fell back to a default identity, and every connection failed
        `Permission denied (publickey)` while the key itself was perfectly good. Registering a box
        should not require a second, invisible edit as root.

        ONE FLEET KEY by default (`fleet_ssh_key`, owner's call 2026-09-17): every private key already
        lives in this one container, so per-box keys bound nothing an attacker could use — they only
        added a mapping to get wrong. A box may still override with `ssh_key_i<id>` when it genuinely
        needs isolation.

        ⚠ FAIL-SAFE: a box whose key file is ABSENT gets no block at all, so the base config's existing
        entries still decide — this can only ever ADD working boxes, never break one that works today.
        `Include` comes first because ssh takes the FIRST value it sees for a keyword, so the registry
        wins over any stale pattern below it."""
        # ⛔ ONLY THE LIVE DAEMON MAY TOUCH ~/.ssh. `poll_once` is called by tests and by anyone
        # exploring locally, where `Path.home()` is a HUMAN's home: the first version of this wrote
        # `config.fleet` into a developer's ~/.ssh and prepended an `Include` to their own ssh
        # config, from a plain `make test`. Editing a developer's ssh configuration is not a side
        # effect a test may have. `COORD_ROLE=live` is set only by the coordinator service in
        # docker-compose, so outside the container — and in the test bed — this is inert.
        if os.environ.get("COORD_ROLE") != "live":
            return
        ssh_dir = Path.home() / ".ssh"
        if not ssh_dir.is_dir():
            return

        def live(key, default=None):
            """LIVE from the DB, never `self.settings` — that is loaded once in `__init__`, so an
            operator's `runq box key` would otherwise need a coordinator restart to take effect,
            which is exactly the invisible second step this feature exists to remove. Same reasoning
            as `worker_roll_max_draining`."""
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (ValueError, TypeError, json.JSONDecodeError):
                return row["value"]

        default_key = str(live("fleet_ssh_key", self.settings.get("fleet_ssh_key"))
                          or "fleet_ed25519")
        blocks, skipped = [], []
        for r in self.conn.execute(
                "SELECT id, label, ssh_host, ssh_port FROM instances "
                "WHERE source='owned' AND ssh_host IS NOT NULL AND ssh_host != ''"):
            inst = dict(r)
            key = str(live(f"ssh_key_i{inst['id']}") or default_key)
            if not re.fullmatch(r"[A-Za-z0-9._-]+", key):   # a key NAME, never a path
                skipped.append((inst["label"], f"unsafe key name {key!r}"))
                continue
            if not (ssh_dir / key).exists():
                skipped.append((inst["label"], f"no such key ~/.ssh/{key}"))
                continue
            blocks.append(
                f"# instance {inst['id']} ({inst['label']}) — from the registry, do not hand-edit\n"
                f"Host {inst['ssh_host']}\n"
                f"    HostName {inst['ssh_host']}\n"
                f"    Port {inst['ssh_port']}\n"
                f"    User root\n"
                f"    IdentityFile ~/.ssh/{key}\n"
                f"    IdentitiesOnly yes\n")
        managed = ("# GENERATED by dispatcher._sync_ssh_config from the run registry. Every edit is\n"
                   "# overwritten on the next poll; change `ssh_key_i<id>` / `fleet_ssh_key` instead.\n\n"
                   + "\n".join(blocks))
        target = ssh_dir / "config.fleet"
        if not target.exists() or target.read_text() != managed:
            target.write_text(managed)
            target.chmod(0o600)
            self.log("ssh_config", json.dumps(
                {"boxes": len(blocks), "default_key": default_key,
                 "skipped": [f"{lbl}: {why}" for lbl, why in skipped]}, separators=(",", ":")))
        base = ssh_dir / "config"
        include = "Include ~/.ssh/config.fleet\n"
        existing = base.read_text() if base.exists() else ""
        if not existing.startswith(include):
            base.write_text(include + existing)
            base.chmod(0o600)

    # -- operator verbs, written by the API (remote-submit spec M2, inv. 20) --
    def _consume_box_requests(self) -> None:
        """Apply a pause/hold/drain/resume the API asked for, using `box_pause`'s own functions.

        WHY THE DISPATCHER AND NOT THE API. `coord-api` has `network_mode: none` (inv. 3), and these
        verbs need ssh: a soft pause SIGSTOPs the box's trainers via a `~/spool/FREEZE` marker. So
        the API writes a one-shot row and the daemon that already holds the fleet's keys performs it
        — the same division `roll_now.py` established, and the reason the API can stay network-less.

        ⛔ THE LOGIC IS NOT DUPLICATED. `box_pause.cmd_*` take `(conn, inst)` and are called verbatim
        here, so the CLI path and the API path cannot drift into two different meanings of "pause" —
        which is exactly the class of bug the box-pause spec's carve-outs make expensive.
        The import is function-local because `box_pause` imports THIS module."""
        rows = self.conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'pause_request_i%' "
            "OR key LIKE 'hold_request_i%' OR key LIKE 'resume_request_i%' "
            "OR key LIKE 'drain_request_i%'").fetchall()
        if not rows:
            return
        import box_pause   # noqa: PLC0415 — see the docstring: box_pause imports dispatcher
        verbs = {"pause": box_pause.cmd_pause, "hold": box_pause.cmd_hold,
                 "drain": box_pause.cmd_drain, "resume": box_pause.cmd_resume}
        for row in rows:
            key = row["key"]
            verb, _, rest = key.partition("_request_i")
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))   # ONE SHOT
            self.conn.commit()
            try:
                iid = int(rest)
            except ValueError:
                continue
            inst = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            detail = {"instance": iid, "verb": verb}
            if inst is None or verb not in verbs:
                detail["error"] = "no such instance" if inst is None else f"unknown verb {verb!r}"
            else:
                try:
                    verbs[verb](self.conn, dict(inst))
                    detail["ok"] = True
                except Exception as e:   # noqa: BLE001 — an operator verb must not break the poll
                    detail["error"] = f"{type(e).__name__}: {e}"[:300]
            self.log("box_request", json.dumps(detail, separators=(",", ":")), instance_id=iid)

    # -- on-demand box probe (spec: `remote-submit.spec.md` inv. 19a, branch `spec/remote-submit`) --
    def _consume_box_probes(self) -> None:
        """Answer "is this box reachable RIGHT NOW, and if not WHY" on demand.

        One-shot `probe_request_i<id>` rows, written by `runq box probe`, CONSUMED AND DELETED here —
        deliberately the same operator-hatch shape as the urgent-roll hatch `roll_now.py` writes:
        nothing inside the coordinator can set one, and it cannot persist into a standing behaviour.

        WHY IT EXISTS. A failed measurement logs NOTHING (`_measure_box_resources` drops failures with
        a bare `continue`, and `box_measured` is written only on success — task-dispatcher 23-Q1), so
        the registry cannot distinguish "not attempted" from "ssh refused" from "host unresolvable".
        Measured 2026-09-17: owned box -2 sat unreachable behind a wrong address, a firewalled port
        and then a rejected key, and every step of that diagnosis needed a hand-run ssh inside the
        container. The three causes have three different fixes and three different owners, so the
        stderr tail IS the deliverable here.

        DIAGNOSTIC ONLY. It never changes an instance's state, never records into the connection
        tracker (a probe must not push a box onto its direct-endpoint fallback), never seeds
        `_box_res`, and never places work. The key is deleted BEFORE the ssh runs, so a probe of a
        box that hangs cannot re-run every poll."""
        rows = self.conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'probe_request_i%'").fetchall()
        if rows:
            # ⛔ RE-SYNC THE SSH CONFIG FIRST (remote-submit 19a-1). `poll_once` writes
            # `config.fleet` once, at the START of a cycle, and a probe is asked for precisely when
            # a box's address has just changed. A probe consumed in the cycle that was already
            # running read the NEW host off the registry row and found no `Host` block for it, so
            # ssh offered a default identity and the answer was `Permission denied (publickey)` —
            # for a box whose key was fine. Measured 2026-10-03 on `desktop`, re-pointed at 18:18:00
            # and probed at 18:18:24 inside a cycle that began at 18:17:09. The probe's whole job is
            # to say WHY a box is unreachable; it must not manufacture a reason of its own.
            self._sync_ssh_config()
        for row in rows:
            key = row["key"]
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))   # ONE SHOT
            self.conn.commit()
            try:
                iid = int(key[len("probe_request_i"):])
            except ValueError:
                continue
            inst = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            if inst is None:
                self.log("box_probe", json.dumps(
                    {"instance": iid, "reachable": False, "error": "no such instance"},
                    separators=(",", ":")))
                continue
            inst = dict(inst)
            detail = {"instance": iid, "label": inst.get("label"), "state": inst.get("state")}
            try:
                host, port = endpoint_for(inst, self.tracker, self.vastai_run)
                detail["endpoint"] = f"{host}:{port}"
                proc = ssh_run(host, port, BOX_PROBE_CMD, run=self.run)
                rc = getattr(proc, "returncode", 1)
                measured = parse_box_probe(getattr(proc, "stdout", "") or "") if rc == 0 else None
                detail["rc"] = rc
                detail["reachable"] = bool(rc == 0 and measured is not None)
                if measured is not None:
                    detail["measured"] = measured
                else:
                    detail["error"] = ((getattr(proc, "stderr", "") or "").strip()[-400:]
                                       or "probe ran but its output was unparseable")
            except Exception as e:       # noqa: BLE001 — a probe must never break the poll
                detail["reachable"] = False
                detail["error"] = f"{type(e).__name__}: {e}"[:400]
            self.log("box_probe", json.dumps(detail, separators=(",", ":")), instance_id=iid)

    # -- measured box resources + cost consolidation (invariants 21/22) --
    def _measure_box_resources(self) -> None:
        """Invariant 22: sample each live box's real GPU memory on a cadence so packing / cost
        consolidation (21c) run on measured VRAM, not the static `vram_per_lane_gb` hint (live audit
        2026-07-22: hints ran 3x over on a GPU workload, ~1000x over on a CPU-bound JAX-on-CPU one).
        Best-effort + in-memory (invariant 1): a box without `nvidia-smi` or unreachable simply
        carries no measurement and the VRAM gate falls back to slot-count. Per-process attribution is
        deliberately NOT used — it reads `[N/A]` under WSL2 (the owned laptop) — so the box-level
        total is the robust signal, which a whole-box drain (21a) is all that's needed anyway."""
        every = self.settings.get("resource_measure_every_min", 5) * 60
        now = time.time()
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state IN ('live','paused')")]:
            if now - self._last_res_measure.get(inst["id"], 0.0) < every:
                continue
            self._last_res_measure[inst["id"]] = now
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            # box-pause 14a: the same call re-asserts the box's FREEZE marker to what the registry
            # says it should be, and reports what it found. Inv. 8b: it also delivers the launch
            # pacing — read LIVE, so an edit to the setting needs no restart.
            frozen = self._should_be_frozen(inst)
            gate = launch_gate_payload(self._live_setting("launch_gate"))
            proc = ssh_run(host, port, box_assert_cmd(frozen, gate) + BOX_PROBE_CMD, run=self.run)
            ok = getattr(proc, "returncode", 1) == 0
            self.tracker.record(inst["id"], ok)
            if not ok:
                continue
            self._note_freeze_reconciled(inst, frozen, getattr(proc, "stdout", "") or "")
            if parse_launch_gate_report(getattr(proc, "stdout", "") or "") == "updated":
                self.log("launch_gate_pushed", gate, instance_id=inst["id"])
            m = parse_box_probe(getattr(proc, "stdout", "") or "")
            if m is None:
                continue  # unparseable -> carry no measurement (21c/23 both fall back)
            m["at"] = now
            # Invariant 24: turn the cumulative container CPU counter into a RATE against the
            # PREVIOUS sample. Cores-used is the only honest read of "how much of what we rent are we
            # burning" — `load1` is the host's run queue and counts other tenants as if they were us.
            prev = self._box_res.get(inst["id"]) or {}
            m["cpu_used_cores"] = _cpu_used_cores(prev, m)
            self._box_res[inst["id"]] = m
            # Invariant 23 observability: log what we measured AND the headroom it implies, every
            # cadence, per box. This is the "is the fleet actually using the hardware?" signal —
            # without it, under-packing is invisible (live 2026-07-28: the desktop sat at ~20% CPU
            # with work queued because a hand-set lane constant, not real capacity, was binding).
            gpu = ("no-gpu" if m["vram_total_gb"] is None else
                   f"vram {m['vram_used_gb']:.1f}/{m['vram_total_gb']:.1f}GB util {m['gpu_util']:.0f}%")
            cpu = (f"load {m['load1']:.2f}/{m['cores']} cores"
                   if m["cpu_used_cores"] is None or m["cpu_quota_cores"] is None else
                   f"cpu {m['cpu_used_cores']:.2f}/{m['cpu_quota_cores']:.2f} cores "
                   f"(host load {m['load1']:.2f}/{m['cores']})")
            # The JSON tail is what the dashboard's fleet-performance panel reads (run-dashboard spec
            # §20). It rides on the SAME event rather than a second one so a box's occupancy, its
            # hardware reading and its timestamp can never disagree — and it carries `slots`/`running`
            # because a sample without them cannot distinguish "idle, nothing queued" from
            # "under-packed while work waited", which is the whole question this panel answers.
            self.log("box_measured",
                      f"{cpu}, ram {m['ram_avail_gb']:.1f}GB free, {gpu}"
                      f" | {json.dumps(self._box_perf_payload(inst, m), separators=(',', ':'))}",
                      instance_id=inst["id"])
            self.conn.commit()
            self._check_gpu_alert(inst)

    # -- invariant 30: GPU-loss push alert --
    def _check_gpu_alert(self, inst: dict) -> None:
        """Push one alert when a GPU-registered box's GPU disappears, and one when it returns.

        Runs only after a SUCCESSFUL probe, so an unreachable box can never read as GPU-less. The
        alert state lives in the event log (`gpu_lost` / `gpu_restored`), not in memory, so a
        restart neither re-sends nor forgets an open alert. A push that FAILS writes no transition
        event, so the next probe (~5 min) retries it rather than the alert being lost silently."""
        if not inst.get("gpu_name"):
            return
        n = GPU_LOST_MIN_SAMPLES
        recent = []
        for r in self.conn.execute(
                "SELECT detail FROM events WHERE instance_id=? AND event='box_measured' "
                "ORDER BY seq DESC LIMIT ?", (inst["id"], n)):
            try:
                tail = json.loads((r["detail"] or "").partition("| ")[2])
            except (ValueError, TypeError):
                continue
            if isinstance(tail, dict) and "gpu_name" in tail:   # missing KEY = unknown, not lost
                recent.append(tail["gpu_name"])
        recent.reverse()
        last = self.conn.execute(
            "SELECT event FROM events WHERE instance_id=? AND event IN ('gpu_lost','gpu_restored') "
            "ORDER BY seq DESC LIMIT 1", (inst["id"],)).fetchone()
        kind = gpu_alert_transition(inst["gpu_name"], recent,
                                    bool(last) and last["event"] == "gpu_lost", n)
        if kind is None:
            return
        label = inst.get("label") or inst["id"]
        if kind == "lost":
            title, tags, prio = f"{label}: GPU lost", "warning", "high"
            msg = (f"Box {inst['id']} ({label}), registered with {inst['gpu_name']}: its last {n} "
                   f"probes see NO GPU, so work placed there runs on the CPU. Diagnose with "
                   f"fleet/owned_gpu_doctor.sh on its host.")
        else:
            title, tags, prio = f"{label}: GPU back", "white_check_mark", "default"
            msg = f"Box {inst['id']} ({label}) sees its {inst['gpu_name']} again."
        ok, err = self._send_alert(title, msg, tags, prio)
        self.log("notify", json.dumps({"kind": f"gpu_{kind}", "ok": ok, "error": err},
                                      separators=(",", ":")), instance_id=inst["id"])
        if ok is False:
            self._alert(f"notify FAILED for gpu_{kind} on box {inst['id']} ({err}); retrying "
                        f"next probe")
        else:
            self.log(f"gpu_{kind}", msg, instance_id=inst["id"])
            self._alert(msg)
        self.conn.commit()

    def _send_alert(self, title: str, message: str, tags: str, priority: str) -> tuple:
        """(ok, error). ok is None when no channel is configured: the transition is still recorded
        (logged + `[ALERT]`), there is just nowhere to push it."""
        if self.notify is not None:
            return self.notify(title, message, tags, priority)
        topic = os.environ.get(NTFY_TOPIC_ENV, "").strip()
        if not topic:
            return None, "no push channel configured"
        return ntfy_post(topic, title, message, tags=tags, priority=priority)

    def _box_perf_payload(self, inst: dict, m: dict) -> dict:
        """The machine-readable half of a `box_measured` detail (invariant 25). Numbers are rounded
        to the precision the panel actually renders — the event log is append-only, so an unrounded
        float here is bytes we pay for on every sample of every box, forever."""
        def r(v, dp=2):
            return None if v is None else round(float(v), dp)
        occ = {"running": 0, "open": 0}
        for row in self.conn.execute(
                "SELECT state, COUNT(*) n FROM tasks WHERE instance_id=? AND state IN "
                f"({','.join('?' * len(registry_db.OPEN_STATES))}) GROUP BY state",
                (inst["id"], *registry_db.OPEN_STATES)):
            occ["open"] += row["n"]
            if row["state"] == "running":
                occ["running"] = row["n"]
        # Invariant 26 attribution, stamped AT SAMPLE TIME rather than reconstructed later from task
        # intervals: which workload these numbers belong to is knowable here for free, and is not
        # recoverable afterwards without replaying every start/terminal event. Null when the box is
        # running a MIX — such a sample is still a real per-lane average, so it keeps feeding the
        # fleet-wide aggregate; it just teaches no single key.
        run_keys = [(r["entrypoint"], r["grp"]) for r in self.conn.execute(
            "SELECT DISTINCT entrypoint, grp FROM tasks WHERE instance_id=? AND state='running'",
            (inst["id"],))]
        ep, grp = run_keys[0] if len(run_keys) == 1 else (None, None)
        return {
            "v": 1,
            "cores": m["cores"], "load1": r(m["load1"]),
            "ram_total_gb": r(m["ram_total_gb"], 1), "ram_avail_gb": r(m["ram_avail_gb"], 1),
            "cpu_quota_cores": r(m["cpu_quota_cores"]), "cpu_used_cores": r(m["cpu_used_cores"]),
            # Invariant 28b: the RAW cumulative counter, not just the derived rate. `cpu_used_cores`
            # is a difference between two samples, so a restart (which empties the in-memory
            # `_box_res`) leaves the next sample with no predecessor and silently drops the whole
            # fleet back to the host CPU basis. Persisting the counter lets `_seed_box_res` restore
            # the predecessor from the event log. Unrounded on purpose — it is a microsecond counter
            # and rounding it would corrupt the difference.
            "cpu_usage_usec": m.get("cpu_usage_usec"),
            "mem_limit_gb": r(m["mem_limit_gb"], 1), "mem_used_gb": r(m["mem_used_gb"], 1),
            "mem_anon_gb": r(m["mem_anon_gb"], 1),
            "vram_total_gb": r(m["vram_total_gb"], 1), "vram_used_gb": r(m["vram_used_gb"], 1),
            "gpu_util": r(m["gpu_util"], 0), "gpu_mem_util": r(m["gpu_mem_util"], 0),
            "gpu_name": m["gpu_name"], "gpu_count": m["gpu_count"],
            # Nominal slots AND the effective cap the packer actually used this poll — they differ
            # whenever invariant 19h's learned concurrency or a capacity window is binding, and the
            # difference is precisely the "why is this box only half full?" answer.
            "slots_total": inst.get("slots_total"),
            "slots_eff": self._effective_slots(inst),
            "running": occ["running"], "open": occ["open"],
            # Invariant 26: the workload these per-lane numbers describe (null = mixed box).
            "ep": ep, "grp": grp,
        }

    def _effective_slots(self, inst: dict) -> int:
        """The slot cap `_instances_view` would apply to `inst` right now (19h overpack cap + the
        capacity window), without building the whole view."""
        slots = inst["slots_total"]
        cap = self._overpack_cap(inst)
        if cap is not None:
            slots = min(slots, cap)
        cap_slots = self._capacity_slots(inst)
        if cap_slots is not None:
            slots = min(slots, cap_slots)
        return slots

    def _consolidate(self) -> None:
        """Invariant 21: drain a paid box whose whole load can move onto strictly-cheaper reclaimed
        capacity, so the box goes idle -> torn down (11). Runs AFTER `_place_queue` (so the view
        reflects placed backlog and consolidation never steals a slot a real queued task claimed) and
        BEFORE teardown. The drain is the shared graceful preempt path (`_evict_task_graceful`)."""
        if not self.settings.get("consolidate_enabled", True):
            return
        queued = [self._task_view(dict(r))
                  for r in registry_db.list_tasks(self.conn, states=["queued"])]
        # Invariant 21g cooldown state comes from the EVENT LOG, not a new settings key: the
        # `consolidate` events already record exactly when each box was last drained, and deriving
        # it keeps this correct across a dispatcher restart (invariant 1 — no in-memory timer).
        import datetime as _dt
        last_drain_at = {}
        for r in self.conn.execute(
                "SELECT instance_id, MAX(t) AS t FROM events WHERE event='consolidate' "
                "AND instance_id IS NOT NULL GROUP BY instance_id"):
            try:
                last_drain_at[r["instance_id"]] = _dt.datetime.strptime(
                    r["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
            except (TypeError, ValueError):
                continue
        drains = consolidation_drains(self._instances_view(), self.settings, time.time(),
                                       queued, last_drain_at)
        for d in drains:
            row = self.conn.execute("SELECT * FROM instances WHERE id=?", (d["instance_id"],)).fetchone()
            if row is None:
                continue
            inst = dict(row)
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            occ = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state='running'", (d["instance_id"],))]
            for t in occ:
                self._evict_task_graceful(
                    host, port, t,
                    f"consolidate: vacate paid box {d['instance_id']} "
                    f"(${d['dph_reclaimed']:.4f}/hr) -> targets {d['targets']}")
            if occ:
                # Invariant 21h: hold the box out of placement for the whole drain, BEFORE the
                # commit — otherwise the next `_place_queue` ships the tasks we just evicted
                # straight back onto it and the drain is pure loss.
                self._set_drain_hold(inst)
                self.log("consolidate",
                          f"draining {len(occ)} task(s) to reclaim ${d['dph_reclaimed']:.4f}/hr "
                          f"(targets {d['targets']}) — held out of placement for "
                          f"{self.settings['consolidate_drain_hold_min']}min",
                          instance_id=d["instance_id"])
                self.conn.commit()

    # -- undeliverable-box quarantine (invariant 10d) --
    # -- drain hold (invariant 21h) --

    def _drain_hold_key(self, inst: dict) -> str:
        return f"drain_hold_i{inst['id']}"

    def _drain_held(self, inst: dict) -> bool:
        """True while `inst` is inside its post-drain hold window. DB-backed (mirrors
        `ship_quarantine_i<id>`) so a dispatcher restart cannot resurrect a draining box into the
        placement pool — invariant 1 forbids decisions that depend on in-memory state."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (self._drain_hold_key(inst),)).fetchone()
        if row is None:
            return False
        try:
            at = json.loads(row["value"])["at"]
            held_s = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(
                          tzinfo=datetime.timezone.utc)).total_seconds()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False  # unreadable hold is no hold — never strand a box on a corrupt row
        if held_s < self.settings["consolidate_drain_hold_min"] * 60:
            return True
        # Expired: the box did not empty inside the measured p90 drain cycle, so it goes back to
        # work rather than idle-billing. Drop the row so this stays O(1) and the log says so once.
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._drain_hold_key(inst),))
        self.conn.commit()
        self.log("drain_hold_expired",
                  f"held {held_s / 60:.0f}min without emptying — returning to the placement pool",
                  instance_id=inst["id"])
        return False

    def _set_drain_hold(self, inst: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (self._drain_hold_key(inst), json.dumps({"at": registry_db.now_iso()})))

    def _clear_drain_hold(self, inst: dict) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._drain_hold_key(inst),))

    # -- worker rolling upgrade (R3/R4/R6, worker-rolling-upgrade.spec.md) --
    def _worker_roll_key(self, inst: dict) -> str:
        """Per INSTANCE, like `drain_hold_i<id>` — staleness is a property of the worker THIS box is
        running, and a re-rent of the same machine is born current (`_bring_up_worker`, R4.4)."""
        return f"worker_roll_i{inst['id']}"

    def _worker_roll_max_draining(self) -> int:
        """R4.1, default 1 — **read LIVE from the DB, not from `self.settings`** (owner, 2026-08-03:
        "default to 1, but ideally we could override it live if we wanted to get something out more
        urgently").

        `self.settings` is snapshotted in `__post_init__`, so a value read from it cannot be changed
        without restarting the coordinator — and restarting to widen a roll is exactly the kind of
        disturbance this feature exists to avoid. This is the same trap that made a
        `worker_refresh_min` override a no-op on 2026-08-02. Floored at 1: a 0 would freeze the roll
        entirely while looking like a tuning choice."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='worker_roll_max_draining'").fetchone()
        try:
            return max(1, int(json.loads(row["value"]))) if row else 1
        except (ValueError, TypeError, json.JSONDecodeError):
            return 1

    def _worker_roll_held(self, inst: dict) -> bool:
        """R3 — is this box holding for an upgrade? DB-backed (R3.2), bounded (R6.2), and an
        unreadable row is NO hold (R6.1: never strand a box on a corrupt value)."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (self._worker_roll_key(inst),)).fetchone()
        if row is None:
            return False
        try:
            at = json.loads(row["value"])["at"]
            held_s = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(
                          tzinfo=datetime.timezone.utc)).total_seconds()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if held_s < self.settings.get("worker_roll_hold_max_min", 720) * 60:
            return True
        # R6.2: one box with a wedged occupant must not hold the roll's only slot forever. Return it
        # to the pool STILL STALE, and say so in a DISTINCT event — "returned to the pool" and
        # "returned having achieved nothing" must not read identically.
        self._clear_worker_roll(inst)
        self.log("worker_roll_expired",
                  f"held {held_s / 60:.0f}min without emptying — returning to the pool STILL STALE",
                  instance_id=inst["id"])
        return False

    def _clear_worker_roll(self, inst: dict) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._worker_roll_key(inst),))
        self.conn.commit()

    def _admit_to_worker_roll(self, inst: dict, stale_from: str, occupied: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (self._worker_roll_key(inst),
             json.dumps({"at": registry_db.now_iso(), "from": stale_from})))
        self.conn.commit()
        self.log("worker_roll_admitted",
                  f"stale worker {stale_from} — taking no new work until its {occupied} occupant(s) "
                  f"finish naturally (no preempt, no requeue), then upgrading",
                  instance_id=inst["id"])

    def _ship_quarantine_key(self, inst: dict) -> str:
        # Keyed per INSTANCE, not per machine (unlike `_overpack_cap`): "we cannot push bytes to this
        # box right now" is a property of this rental's link, not of the physical machine — a re-rent
        # of the same machine deserves a clean slate. Persisted in `settings` rather than held in
        # memory because invariant 1 requires a restart to reconstruct every scheduling decision from
        # the DB; an in-memory flag would hand the box a fresh backlog on every daemon respawn.
        return f"ship_quarantine_i{inst['id']}"

    def _ship_quarantined(self, inst: dict) -> bool:
        return self.conn.execute("SELECT 1 FROM settings WHERE key=?",
                                  (self._ship_quarantine_key(inst),)).fetchone() is not None

    def _set_ship_quarantine(self, inst: dict, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self._ship_quarantine_key(inst), json.dumps({"at": registry_db.now_iso(),
                                                          "reason": reason})))
        self.conn.commit()

    def _clear_ship_quarantine(self, inst: dict) -> None:
        if not self._ship_quarantined(inst):
            return
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._ship_quarantine_key(inst),))
        self.conn.commit()
        self.log("ship_quarantine_lifted", "delivery succeeded — box returns to the placement pool",
                  instance_id=inst["id"])

    def _reap_undeliverable_claims(self):
        """Invariant 10d — a task `claimed` on a live box we are demonstrably failing to DELIVER to.

        The hole this closes (live 2026-07-29): every existing reaper watches a state that a task
        which never shipped cannot be in. 10b requires `shipped` — and additionally skips any box
        with `consecutive_fails > 0`, which is exactly what an undeliverable box always has, so it is
        excluded twice over. 10c requires a HEARTBEAT and a dead worker; here the worker is alive
        (verified over ssh: box up 11 days, `spool_worker.py` running, 39G free). 19/19h only look at
        `running`/`shipped`. So a box healthy enough to answer ssh but too degraded to accept a
        37.6 MB bundle held six tasks indefinitely, and re-attracted more on every poll, while
        nothing in the system had an opinion about it.

        Kill criterion, pre-registered — ALL of:
          * the task has been `claimed` on a `live` box longer than `ship_timeout_min` (measured; see
            the setting), and
          * we have a `ship_failed` event for THIS task on THIS instance — the discriminator that
            separates "undeliverable" from "merely queued behind a slow pass", which age alone cannot
            do (claim->ship p99 is 98.7 min fleet-wide, so an age-only rule would have requeued 70
            ships that went on to succeed), and
          * the box has no `running`/`preempting` occupant — never touch a box doing real work.

        Remediation is deliberately the CHEAP, reversible half: clear the box's spool copy (19h's
        double-run guard), requeue at NO retry cost (this is the scheduler failing to deliver, not
        the task misbehaving — same reasoning as 19h's free requeue), and quarantine the box from
        new placement. It does NOT destroy: a box can come back (owned box -1 recovered from a
        311-failure streak), and `should_teardown` already reaps a quarantined rental once it is
        empty, through the one choke point that carves out owned boxes."""
        import datetime
        now = datetime.datetime.utcnow()
        timeout = self.settings["ship_timeout_min"]
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='live'")]:
            occ = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN "
                "('claimed','shipped','running','preempting')", (inst["id"],))]
            if any(t["state"] in ("running", "preempting") for t in occ):
                continue  # the box is doing real work — this is not its problem
            stuck = [t for t in occ if t["state"] == "claimed"
                     and _age_minutes(t["updated_at"], now) > timeout
                     and self.conn.execute(
                         "SELECT 1 FROM events WHERE task_id=? AND instance_id=? AND "
                         "event='ship_failed' LIMIT 1", (t["id"], inst["id"])).fetchone()]
            if not stuck:
                continue
            self.log("undeliverable",
                      f"{len(stuck)} task(s) claimed > {timeout}min on instance {inst['id']} with a "
                      "recorded ship failure — requeueing at no retry cost and quarantining the box",
                      instance_id=inst["id"])
            requeued = 0
            for t in stuck:
                # Clear the box's copy BEFORE requeueing so a link that recovers later cannot deliver
                # and launch a task we have already given to someone else. Unlike 10b there is no
                # `_destroy` backstop here, so a failed cleanup must defer for EVERY box source, not
                # just owned ones — and on a box we cannot reach, this ssh is exactly what fails.
                res = ssh_run(*endpoint_for(inst, self.tracker, self.vastai_run),
                              f"rm -rf ~/spool/incoming/{t['id']} ~/spool/active/{t['id']}",
                              run=self.run)
                if res.returncode != 0:
                    self.log("undeliverable_defer",
                              f"box cleanup failed rc={res.returncode} — will retry next poll",
                              task_id=t["id"], instance_id=inst["id"])
                    continue
                registry_db.transition(
                    self.conn, t["id"], "queued", "undeliverable",
                    f"undeliverable: claimed > {timeout}min on instance {inst['id']} with a recorded "
                    "ship failure (transport, not the task); retry cost unchanged",
                    extra_set={"instance_id": None})
                requeued += 1
            # Quarantine only once something actually moved: if every cleanup deferred, the box keeps
            # its tasks and we must not also bar it from the pool on the strength of an ssh we could
            # not complete.
            if requeued:
                self._set_ship_quarantine(inst, f"{requeued} task(s) undeliverable > {timeout}min")

    # -- over-pack detection + auto-adjust (invariant 19h) --
    def _overpack_cap_key(self, inst: dict) -> str:
        mid = inst.get("machine_id")
        return f"overpack_cap_m{mid}" if mid else f"overpack_cap_i{inst['id']}"

    def _observed_peak_concurrency(self, inst: dict) -> int:
        """The MOST tasks ever seen RUNNING at once on this box's physical machine — the evidence
        floor no learned cap may sit below (invariant 19h-2, 2026-08-02).

        This is the number the ratchet was missing. `_learn_overpack_cap` recorded whatever happened
        to be running at the instant the reaper fired, took min() with the stored value, and never
        looked at what the machine had ALREADY demonstrated it could sustain. Measured over the 18
        most recent learns, 15 were on machines later observed running MORE concurrent tasks than
        the cap learned from them — an 83% false-positive rate. Machine m10003 sat capped at 1 while
        instance 40000043 on that same machine was observed running 8 at once.

        Derived by replaying the event stream (`start` vs the terminal events) across every instance
        this machine has hosted, because the cap is keyed per machine and outlives any one rental.
        Cached per process: the reaper walks only live boxes (~11) and the answer is monotone.

        ⚠ The per-machine keying is inherited from the cap it guards, and carries the same caveat —
        a Vast `machine_id` is the physical HOST, so a later, SMALLER rented slice of it could be
        credited with a bigger slice's peak. That is deliberately the cheap error: the settings
        comment on `ship_launch_grace_min` establishes the asymmetry — a false positive PERMANENTLY
        pins a machine across every future rental, a false negative delays detecting a genuinely
        over-packed box by one grace period."""
        key = self._overpack_cap_key(inst)
        cache = getattr(self, "_peak_cache", None)
        if cache is None:
            cache = self._peak_cache = {}
        # Rebuilt at most once per `poll_seconds` window — the same idiom `learn_group_estimates`
        # and the lane-footprint cache use, and it must NOT be a permanent memo. The peak GROWS as a
        # box fills, and the coordinator runs for days: a process-lifetime cache would freeze a
        # newly-rented box's floor at whatever it happened to be on the first poll (often 1-2 lanes,
        # because it was still filling), and that stale-LOW floor then accepts exactly the caps this
        # function exists to refuse. Cost of getting it right is nil — measured 11.2 ms cold for the
        # whole live fleet and ~2 us warm, against a cycle measured in tens of seconds.
        now = time.time()
        hit = cache.get(key)
        if hit is not None and now - hit[0] < self.settings["poll_seconds"]:
            return hit[1]
        # The box's OWN id is always in scope, unioned with every sibling rental of the same
        # machine. Deriving the list purely from `instances.machine_id` would return nothing when
        # that row is missing or not yet written, and an empty list silently yields peak 0 — i.e.
        # no floor at all, which is exactly the failure this function exists to prevent.
        ids = {inst["id"]}
        mid = inst.get("machine_id")
        if mid:
            ids |= {r["id"] for r in self.conn.execute(
                "SELECT id FROM instances WHERE machine_id=?", (mid,))}
        ids = sorted(ids)
        # PER-TASK INTERVALS, not a running +1/-1 counter over an enumerated event vocabulary.
        # The counter version leaks: it must name every event that ends a run, and it missed
        # `stalled` (128 live cases), so the count ratcheted UP and two machines resolved to a peak
        # of 10 against an on-box `max_slots` of 8 — an over-estimate silently disables the floor.
        # A task's lane is occupied from its `start` until its NEXT event, whatever that is, which
        # needs no vocabulary at all. Verified against the live registry: the event immediately
        # following a `start` is always an exit (done 2157, preempt_intent 490, cancel_requested
        # 352, task_failed 260, infra_failed 213, stalled 128) — never a mid-run event.
        # PER INSTANCE, then max — NOT a union sweep across the machine's rentals. The cap is
        # consumed per instance (`_instances_view` caps that row's `slots_total`), so the floor has
        # to be "the most ONE BOX ran", not "the most this host ran across concurrent rentals".
        # Live example: machine m100002 had two overlapping rentals peaking at 6 lanes each, which a
        # union sweep read as 10 — a floor no single box could ever justify.
        peak = 0
        for iid in ids:
            opened: dict[str, int] = {}
            deltas: list[tuple[int, int]] = []
            for tid, ev, seq in self.conn.execute(
                    "SELECT task_id, event, seq FROM events WHERE instance_id=? "
                    "AND task_id IS NOT NULL ORDER BY seq", (iid,)):
                if ev == "start":
                    if tid not in opened:            # a re-`start` of a live lane is not a 2nd lane
                        opened[tid] = seq
                        deltas.append((seq, +1))
                elif tid in opened:
                    deltas.append((seq, -1))
                    del opened[tid]
            # Anything still open ran to the end of the record, so it never closes.
            deltas.sort()
            cur = 0
            for _seq, delta in deltas:
                cur += delta
                peak = max(peak, cur)
        cache[key] = (now, peak)
        return peak

    def _overpack_cap(self, inst: dict):
        """Learned sustainable concurrency for this box's physical machine, or None.

        REPAIRS ON READ (invariant 19h-2): a stored cap below the machine's observed peak is a
        ratchet artifact — the machine has demonstrably run more than that — so it is raised back to
        the peak and logged rather than being served to the packer. This is what heals the caps a
        pre-fix daemon already wrote; without it the pin is self-perpetuating, because a machine
        held at 1 slot can never again be OBSERVED sustaining more and so can never earn its cap
        back. (Three machines were pinned at 1 this way on 2026-08-02 — m100005, m100002 and m10003
        — with observed peaks of 7, 6 and 8.)"""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (self._overpack_cap_key(inst),)).fetchone()
        if row is None:
            return None
        cap = int(json.loads(row["value"]))
        peak = self._observed_peak_concurrency(inst)
        if peak > cap:
            key = self._overpack_cap_key(inst)
            self.conn.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(peak), key))
            self.conn.commit()
            self.log("overpack_cap_repair",
                      f"raised {key} {cap} -> {peak}: the machine has been OBSERVED running {peak} "
                      "concurrent tasks, so the stored cap was a ratchet artifact",
                      instance_id=inst["id"])
            return peak
        return cap

    def _overpack_refusals(self, key: str) -> int:
        """Consecutive 19h-2 refusals for this machine (invariant 19h-3). PERSISTED, not in-memory:
        it must span consecutive grace windows (70+ min) and the coordinator restarts far more often
        than that — three times on 2026-08-02 alone — so an in-memory counter would reset on nearly
        every restart and never reach the limit. Same defect class as 28b."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (f"overpack_refusals_{key}",)).fetchone()
        try:
            return int(json.loads(row["value"])) if row else 0
        except (ValueError, TypeError):
            return 0

    def _overpack_cooldown_key(self, inst: dict) -> str:
        """Keyed per INSTANCE, unlike the cap it accompanies (invariant 19h-3). The cap answers
        "how much can this physical machine sustain", which outlives a rental; the cooldown answers
        "is this rental wedged right now", which does not — a co-tenant filling the shared GPU, or
        a worker that has stopped launching, is a property of this box today. Keying it per machine
        would carry a transient stall onto every future rental of that host, which is the exact
        mistake 19h-2 was written to undo."""
        return f"overpack_cooldown_i{inst['id']}"

    def _set_overpack_cooldown(self, inst: dict, peak: int, cap: int) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self._overpack_cooldown_key(inst), json.dumps({"at": registry_db.now_iso()})))
        self.conn.commit()
        self.log("overpack_cooldown",
                  f"no new work for {self.settings.get('overpack_cooldown_min', 35)}min: this box has "
                  f"gate-held across consecutive reap passes with no lane starting in between "
                  f"(would-be cap {cap} vs observed peak {peak}). The peak stands — the cap is NOT "
                  f"lowered — but placement stops feeding a rental that is not launching "
                  f"(invariant 19h-3)",
                  instance_id=inst["id"])

    def _overpack_cooldown_active(self, inst: dict) -> bool:
        """True while the cooldown holds. Time-boxed by `ship_launch_grace_min` and additionally
        cleared the moment the box is seen launching, so it can never wedge a box permanently —
        the failure mode of every other one-way mechanism on this path."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                 (self._overpack_cooldown_key(inst),)).fetchone()
        if not row:
            return False
        try:
            at = json.loads(row["value"])["at"]
        except (ValueError, KeyError, TypeError):
            return False
        return _age_minutes(at, datetime.datetime.utcnow()) < self.settings.get(
            "overpack_cooldown_min", self.settings["ship_launch_grace_min"])

    def _clear_overpack_cooldown(self, inst: dict) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (self._overpack_cooldown_key(inst),))
        self.conn.commit()

    def _set_overpack_refusals(self, key: str, n: int) -> None:
        """Store (or clear at 0, so the table does not accumulate a dead row per healed machine)."""
        skey = f"overpack_refusals_{key}"
        if n <= 0:
            self.conn.execute("DELETE FROM settings WHERE key=?", (skey,))
        else:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (skey, json.dumps(n)))
        self.conn.commit()

    def _learn_overpack_cap(self, inst: dict, cap: int):
        """Record (only ever lower) the box's observed sustainable concurrency, keyed per machine so
        a re-rent of the same physical box starts pre-capped.

        FLOORED at the machine's observed peak (invariant 19h-2): a cap below what the machine has
        already been seen sustaining contradicts direct evidence, so the learn is refused and logged
        rather than clamped silently — a silent clamp would leave the reaper looking like it agreed."""
        key = self._overpack_cap_key(inst)
        peak = self._observed_peak_concurrency(inst)
        if cap < peak:
            # Invariant 19h-3: the floor is a HYPOTHESIS ("still filling / transiently gated"), and
            # the reaper unschedules the gate-held task either way — so a permanently-refusing floor
            # burns `ship_launch_grace_min` per cell forever with no state change. Measured: 12
            # unschedules against 12 refusals in the 11 h after 19h-2 landed, a 1:1 pairing, one
            # cell burned twice. Two consecutive refusals means no lane started in 70 min while the
            # box held work — the evidence has beaten the hypothesis and the learn goes through.
            strikes = self._overpack_refusals(key) + 1
            limit = self.settings.get("overpack_refusals_before_override", 2)
            self.log("overpack_cap_refused",
                      f"refused to learn cap {cap} on {key}: this machine has been OBSERVED "
                      f"running {peak} concurrent tasks, so the box is still filling or "
                      f"transiently gated, not over-packed (strike {strikes}/{limit})",
                      instance_id=inst["id"])
            if strikes >= limit:
                # The floor is RIGHT that the machine can do more — so do not lower the cap (and we
                # could not anyway: repair-on-read would restore it to the peak on the next read).
                # What is wrong is the box RIGHT NOW, transiently. So stop feeding this rental for
                # one grace window instead of re-deciding its permanent capacity.
                self._set_overpack_cooldown(inst, peak, cap)
                strikes = 0
            self._set_overpack_refusals(key, strikes)
            return
        self._set_overpack_refusals(key, 0)
        cur = self._overpack_cap(inst)
        new = cap if cur is None else min(cur, cap)
        if new == cur:
            return
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(new)))
        self.conn.commit()
        self.log("overpack_cap", f"learned sustainable concurrency {new} ({key})",
                  instance_id=inst["id"])

    def _reap_overpacked_boxes(self):
        """Invariant 19h. `slots_for_offer` can advertise more slots than a GPU actually sustains;
        the box-side launch gate then wedges the excess task in `shipped` on a live, healthy box that
        no other reaper covers. Detect DB-only: a `live` box whose last pull succeeded and which has
        ≥1 running/preempting occupant (proof the worker IS launching) but also a `shipped` occupant
        older than `ship_launch_grace_min` is over-packed. Learn its true concurrency (cap future
        placement) and requeue the gate-held task(s) at no retry cost. The ≥1-running guard also makes
        a false positive safe: were the box actually dead, requeue is still correct.

        ⚠ "STILL FILLING" IS NOT "OVER-PACKED", and telling them apart is what this reaper kept
        getting wrong (2026-08-02). Two guards now separate them, for DISJOINT cases:

          * `_learn_overpack_cap`'s observed-peak floor covers a machine with history;
          * the RECENT-LAUNCH check below covers a machine with none — a box filling for the first
            time has a low peak precisely BECAUSE it is filling, so the floor cannot protect it.

        The recent-launch check is the honest reading of the predicate: over-packed means the box
        CANNOT start another lane. A box that started one within the last grace period has just
        demonstrated that it can, so whatever is holding the `shipped` task is transient — and
        `sweep_supervisor.should_launch` has FOUR transient refusal reasons (`cpu_load`, `gpu_util`,
        `vram`, and `settling`), which since invariant 8a are the ONLY reasons a worker holds a
        launch — it enforces no lane count of its own. A box at 7 of 8 lanes is
        by construction the most loaded it will ever be, so the LAST lane is the one most likely to
        be refused for `cpu_load` — the reaper was reading the launch gate working correctly as
        proof of a capacity ceiling, then making that reading permanent."""
        import datetime
        now = datetime.datetime.utcnow()
        grace = self.settings["ship_launch_grace_min"]
        for inst in [dict(r) for r in self.conn.execute("SELECT * FROM instances WHERE state='live'")]:
            if self.tracker.consecutive_fails(inst["id"]) > 0:
                continue
            occ = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN ('shipped','running','preempting')",
                (inst["id"],))]
            running = sum(1 for t in occ if t["state"] in ("running", "preempting"))
            if running == 0:
                continue  # worker not proven to be launching — leave to dead-worker/stall reapers
            # Invariant 4i-7: a FORCED task is never "over-pack". It was placed past the slot cap on
            # purpose, so a worker holding it at its launch gate is not evidence about this box's
            # sustainable concurrency — and requeueing it would only see it re-forced onto the same
            # box next poll, a loop that ratchets the learned cap down each time round. A current
            # worker launches it past the gate anyway; under an older one it waits in `shipped`.
            gate_held = [t for t in occ if t["state"] == "shipped"
                         and _age_minutes(t["updated_at"], now) > grace
                         and not self._is_forced_on(t, inst)]
            # ⛔ SAFETY VALVE (invariant 7h): a task the BOX says it started is RUNNING, whatever
            # the registry thinks, and unscheduling it destroys live training. Anchoring the start
            # row on `claim` fixes the known cause of that divergence, but this reaper is the place
            # where a divergence becomes IRREVERSIBLE — it `rm -rf`s `active/<id>`, which is what
            # makes `reap_orphans` kill the trainer — so it must not be the component that trusts
            # the registry blindly. Measured before the fix: 60 of the last 60 unschedules were on
            # tasks the box had claimed AND started, one of them 52 minutes into its run.
            really_held = [t for t in gate_held if not self._box_reports_started(inst, t["id"])]
            for t in gate_held:
                if t not in really_held:
                    self.log("overpack_skipped_running",
                              "box reports this task STARTED — not over-pack, the registry is behind "
                              "(inv 7h); left alone rather than requeued",
                              task_id=t["id"], instance_id=inst["id"])
            gate_held = really_held
            if not gate_held:
                continue
            # A lane that started within the grace window is proof the gate is still launching, so
            # this box is filling (or cycling), not wedged. Same window as the one we judge the
            # `shipped` task by, so the two reads are symmetric and no new constant is introduced.
            launched = [_age_minutes(t["updated_at"], now) for t in occ
                        if t["state"] in ("running", "preempting")]
            if launched and min(launched) <= grace:
                # Invariant 19h-3(d): reset on POSITIVE evidence, not on elapsed time. A lane
                # starting inside the grace window is exactly what the evidence floor predicted, so
                # the machine has vindicated it — drop both the strikes and any cooldown rather than
                # making a recovered box sit out the rest of the window.
                self._set_overpack_refusals(self._overpack_cap_key(inst), 0)
                self._clear_overpack_cooldown(inst)
                continue
            self._learn_overpack_cap(inst, max(1, running))
            for t in gate_held:
                self._unschedule_overpacked(t, inst)

    def _last_launch_gate(self, inst: dict) -> str | None:
        """The box's most recent `launch_gate` reason (invariant 19h-4), or None.

        This is the whole point of emitting it: an `overpack` event that says only "gate-held > 35
        min" forces the reader to GUESS between over-pack, a co-tenant on the shared GPU, a settling
        stagger and a wedged worker — which is exactly how 2026-08-02 was spent. With the reason
        attached the event answers its own question. Best-effort and never fatal: a missing or
        unparseable file simply yields None and the message reads as it did before.

        Reads the TAIL only. `worker.jsonl` is append-only over a box's whole life and the whole
        point of this call is that it runs on the unschedule path, which is already slow."""
        path = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}" / "worker.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a partial first line from the tail seek, or a torn write
            if rec.get("event") == "launch_gate":
                return f"{rec.get('detail')} (reported {rec.get('t')})"
        return None

    def _unschedule_overpacked(self, task: dict, inst: dict):
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        # Clear the box's copy first so the worker can't launch it after we requeue (double-run). The
        # worker tolerates active/<id> vanishing (same race the re-ship path relies on).
        res = ssh_run(host, port, f"rm -rf ~/spool/incoming/{task['id']} ~/spool/active/{task['id']}",
                       run=self.run)
        if res.returncode != 0:
            self.log("overpack_defer",
                      f"box cleanup failed rc={res.returncode} — will retry next poll",
                      task_id=task["id"], instance_id=inst["id"])
            return
        # Invariant 19h-4: carry the box's OWN reason, so the event does not need to be guessed at.
        gate = self._last_launch_gate(inst)
        registry_db.transition(
            self.conn, task["id"], "queued", "overpack",
            f"over-pack unschedule from instance {inst['id']}: gate-held in shipped > "
            f"{self.settings['ship_launch_grace_min']}min (box concurrency below advertised slots); "
            f"retry cost unchanged | box reports: {gate or 'no launch_gate line (pre-19h-4 worker)'}",
            extra_set={"instance_id": None})

    def _reap_unclaimed_ships(self):
        """Invariant 10b — a task `shipped` with no worker claim within `claim_timeout_min`.

        `claim_timeout_min` was DEFINED (settings) and SPECIFIED (invariant 10b) but never
        consumed by any code path — the same "declared but never wired" shape `heartbeat_stale_min`
        had before invariant 10c implemented it. The consequence is that a box whose worker never
        starts at all is reaped by NOTHING, and idle-bills until a human notices:

          * `_reap_stalled` only queries `state IN ('running','preempting')` — never `shipped`;
          * `_reap_overpacked_boxes` covers `shipped` past `ship_launch_grace_min`, but bails on
            `running == 0` ("worker not proven to be launching — leave to dead-worker/stall");
          * `_reap_dead_workers` only fires once a HEARTBEAT has been pulled at least once, i.e.
            once the worker definitely started; a worker that never started leaves none, so it
            `continue`s. Its comment defers to `provision_timeout_min`/`rent_patience_min`, but
            those govern the PROVISIONING phase — once the box logs `live` they no longer apply.

        Live cost of the hole (2026-07-28): instance 40000011 (Tesla V100) went live 12:41, was
        shipped a task 12:44, then sat at vram 0.0/32.0GB and 0% util for 7.5 HOURS with no `start`
        ever, until a human cancelled it. $1.07 — 25% of that day's entire fleet spend — for zero
        work. Two smaller twins the same day brought it to $1.48, 34% of the day's $4.30, making
        the rarest paid-box failure mode by far the most expensive, precisely because it is the
        only one nothing reclaims.

        SCOPING — this fires only for a box that has never started ANY task, which makes it the
        exact complement of the two reapers above rather than a competitor:
          * a box that started work and then went quiet HAS a HEARTBEAT ⇒ invariant 10c's job;
          * a box provably launching (>=1 running occupant) with excess wedged in `shipped` ⇒
            invariant 19h's job, which requeues at NO retry cost and learns the box's real
            concurrency. The scoping is what keeps them disjoint — NOT the relative timeouts,
            which must not be relied on: at the original `claim_timeout_min` of 15 an unscoped
            10b fired BEFORE `ship_launch_grace_min` (20) and robbed 19h of both, charging half
            a retry for what is not an infra failure at all. So: skip any box with a
            running/preempting occupant, whatever the timeouts happen to be.

        Invariant 10c's discipline applies unchanged — a box we cannot currently reach is a
        different problem (ssh-fallback / teardown paths). If the last pull attempt for this
        instance did not succeed we cannot prove the worker failed to claim, so we abstain.
        """
        import datetime
        now = datetime.datetime.utcnow()
        timeout = self.settings["claim_timeout_min"]
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='live'")]:
            if self.tracker.consecutive_fails(inst["id"]) > 0:
                continue  # inv. 10c: unreachable != worker-never-claimed
            occ = [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN "
                "('claimed','shipped','running','preempting')", (inst["id"],))]
            if any(t["state"] in ("running", "preempting") for t in occ):
                continue  # box is provably launching -> invariant 19h owns this
            ever_started = self.conn.execute(
                "SELECT 1 FROM events WHERE instance_id=? AND event='start' LIMIT 1",
                (inst["id"],)).fetchone()
            if ever_started:
                continue  # worker HAS claimed before -> a dead worker, invariant 10c's job
            stuck = [t for t in occ if t["state"] == "shipped"
                     and _age_minutes(t["updated_at"], now) > timeout]
            if not stuck:
                continue
            self.log("claim_timeout",
                      f"worker never claimed: {len(stuck)} task(s) in shipped > {timeout}min and "
                      f"this box has never started ANY task",
                      instance_id=inst["id"])
            # Clear the box's copy BEFORE requeueing so a worker that wakes up later cannot launch
            # a task we have already given to someone else (the double-run `_unschedule_overpacked`
            # guards against). Best-effort: for a rental `_destroy` below removes the whole box, so
            # a failed cleanup is harmless there; for an owned box (which `_destroy` refuses) it is
            # the only guard, so skip that task and retry next poll.
            for t in occ:
                res = ssh_run(*endpoint_for(inst, self.tracker, self.vastai_run),
                              f"rm -rf ~/spool/incoming/{t['id']} ~/spool/active/{t['id']}",
                              run=self.run)
                if res.returncode != 0 and inst.get("source") == "owned":
                    self.log("claim_timeout_defer",
                              f"box cleanup failed rc={res.returncode} — will retry next poll",
                              task_id=t["id"], instance_id=inst["id"])
                    continue
                self._infra_fail(t, reason=f"claim timeout: shipped > {timeout}min, "
                                           "worker never claimed (box never started any task)")
            # Stop the meter. `_destroy` refuses owned boxes at its own choke point, so an owned
            # box just keeps its (now requeued) slot free; a rental goes away instead of sitting
            # idle-billing while `should_teardown` reports `feasible_task_waiting` forever.
            self._destroy(inst, "claim_timeout: worker never claimed any task")

    def _reap_stalled(self):
        """Invariant 19. Piggybacks entirely on the checkpoint pull already done above — no new
        vastai/ssh round trip. `rsync -t` (invariant 9c) preserves the REMOTE write time on the
        local copy, so the local file's own mtime already is "when the box last made real
        progress," durable across a dispatcher restart (invariant 1 — nothing here depends on an
        in-memory timer).

        Invariant 19d (bug 13): every `running` task is considered, not just ones with a
        checkpoint on disk — a task that hangs before its first checkpoint (still `NULL`, or
        pulled but not yet materialized locally) has no mtime to check but is still aged off
        `running_since` inside `stall_decision`.

        Invariant 19e (retrospective bug, 2026-07-14): `preempting` tasks are watched too, not
        just `running` ones. A preempt whose box-side checkpoint guard never fires (invariant 17b
        — no fresh checkpoint means the worker never kills it) used to be invisible to this
        reaper entirely once it left `running`, so it could sit `preempting` indefinitely with no
        automatic recovery — observed live overnight 2026-07-13/14, discovered only by manual
        monitoring 7+ hours later. `updated_at` is restamped at the CAS into `preempting`
        (`preempt_intent`), so `running_since` here naturally reads as "how long this preempt
        attempt has been outstanding" and a stuck one gets the same `stall_timeout_min` grace
        period before reaping as a stalled `running` task. Remediation is identical
        (`_infra_fail` → requeue): the box process isn't killed, but the DB slot frees and, once
        the process eventually reaches its own natural outcome, invariant 17b's added
        `preempting`->{done,task_failed,infra_failed} transitions mean it's no longer stranded
        even if this reaper already moved the task on."""
        now = time.time()
        # Box-pause spec inv. 4: a task on a `paused` box is frozen (soft) or draining (hard) — its
        # checkpoint mtime deliberately stops advancing, so it must NOT be false-reaped as stalled.
        # The soft-timeout watchdog (30 min) / drain preempt path govern it instead.
        paused = {r["id"] for r in self.conn.execute(
            "SELECT id FROM instances WHERE state='paused'")}
        for t in [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE state IN ('running','preempting')")]:
            if t["instance_id"] in paused:
                continue
            ckpt_mtime = None
            if t["resume_checkpoint"] is not None:
                ckpt = Path(t["resume_checkpoint"])
                if ckpt.exists():
                    ckpt_mtime = ckpt.stat().st_mtime
            # Invariant 19f (2026-07-14): TB freshness is a second liveness anchor, so a trainer
            # that streams scalars but doesn't write ckpt_latest.pt isn't false-reaped (see
            # stall_decision). Pulled with rsync -t, so this mtime is the remote last-write time.
            tb_mtime = self._latest_tb_mtime(self._result_dir(t))
            # Invariant 19c' (bug 12): updated_at was stamped by this task's CAS into
            # `running`, so it anchors the stall clock for retries whose pulled checkpoint
            # (deliberately kept as the --init-from payload) predates the restart.
            import datetime
            running_since = datetime.datetime.strptime(
                t["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
            view = dict(t, ckpt_mtime=ckpt_mtime, tb_mtime=tb_mtime, running_since=running_since)
            if not stall_decision(view, now, self.settings):
                continue
            self.log("stalled",
                      f"no checkpoint or TB progress in {self.settings['stall_timeout_min']}min",
                      task_id=t["id"])
            self._clear_stalled_box_copy(t)
            self._infra_fail(t, reason="stalled: no checkpoint or TB progress")

    def _clear_stalled_box_copy(self, task: dict) -> None:
        """Invariant 19j: a stall-reaped task's trainer must not outlive the requeue on its box.

        `_infra_fail` frees the DB slot and requeues, but nothing stopped the box process (19e's own
        docstring: "the box process isn't killed"). On a RENTED box that mostly ended at teardown; on an
        OWNED box, which is never torn down, the trainer ran on as an orphan with no owner — invisible to
        `reap_orphans` because `active/<id>` still existed without a terminal marker. Measured
        2026-09-30: two G1 LLM cells reaped mid-evaluation kept the laptop's GPU at 100% / 11.1 of 12 GB
        with zero fleet tasks on it, blocking the requeued work — and a requeued copy shipped back to
        the same box would have run beside its own ghost.

        Removing `incoming/<id>` and `active/<id>` is the act the 18e and 19h paths already use: it makes
        an already-running trainer reapable (its owning dir is GONE) and stops a not-yet-claimed one. The
        requeue resumes from the checkpoint already pulled home, so the box copy holds nothing needed.
        Best-effort: an unreachable box, a failed ssh or any error leaves exactly the pre-19j behaviour."""
        iid = task.get("instance_id")
        if iid is None:
            return
        row = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        if row is None or dict(row)["state"] != "live":
            return
        try:
            host, port = endpoint_for(dict(row), self.tracker, self.vastai_run)
            rc = ssh_run(host, port,
                         f"rm -rf ~/spool/incoming/{task['id']} ~/spool/active/{task['id']}",
                         run=self.run).returncode
        except Exception as e:  # noqa: BLE001 — cleanup must never block the requeue
            rc = f"error: {e}"
        self.log("stall_box_copy_cleared",
                  f"removed the stalled task's box copy so its trainer is reaped rather than left as an "
                  f"orphan (cleanup rc={rc})", task_id=task["id"], instance_id=iid)

    def _reap_dead_workers(self):
        """`heartbeat_stale_min` (previously defined but never consumed): `spool_worker.py`
        touches `spool/HEARTBEAT` on its very first tick and every `HEARTBEAT_SECONDS`
        thereafter, independent of whether any task is running -- unlike `_reap_stalled`
        (which only watches `running`/`preempting` occupants against checkpoint progress), a
        dead worker process (OOM-killed, box hiccup, uncaught exception) stops touching it even
        while a task sits `claimed`/`shipped` waiting to be picked up. `should_teardown` treats
        any occupant task as `feasible_task_waiting` forever, so a box with a dead worker never
        goes idle and idle-bills indefinitely with no automatic recovery.

        Only fires once a HEARTBEAT has been pulled at least once (i.e. the worker definitely
        started) -- a box with no HEARTBEAT yet is still provisioning/booting and is covered by
        `provision_timeout_min`/`rent_patience_min` instead, so this never misfires during
        normal boot.

        Invariant 10c: single-source staleness is not enough on its own -- an old local mtime
        is exactly what a run of failed rsync PULLS looks like too (a slow poll cycle across
        many live instances, or a transient ssh/rsync hiccup, per VAST-TEST.md), and that's
        indistinguishable from a genuinely dead worker if all we look at is our own copy's
        mtime. `ConnectionTracker.consecutive_fails` is the same counter `_pull_worker_state`
        already feeds on every pull attempt for this instance -- 0 means the most recent pull
        actually succeeded, so the mtime we're reading is a fresh, confirmed read of the
        REMOTE file (real staleness = real death). Any nonzero count means we can't currently
        prove that; skip this box and let the next successful pull (or the ssh-fallback /
        teardown paths for a truly unreachable box) resolve it -- an incident on
        2026-07-14 destroyed multiple healthy, still-training boxes this way when the ingest
        loop over several live instances routinely took longer than heartbeat_stale_min."""
        now = time.time()
        for inst in [dict(r) for r in self.conn.execute("SELECT * FROM instances WHERE state='live'")]:
            hb = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}" / "HEARTBEAT"
            if not hb.exists():
                continue
            age_min = (now - hb.stat().st_mtime) / 60
            if age_min < self.settings["heartbeat_stale_min"]:
                continue
            if self.tracker.consecutive_fails(inst["id"]) > 0:
                continue
            # Invariant 10c, second half: `consecutive_fails == 0` says the last pull ATTEMPT
            # succeeded, never WHEN. A pull that is not attempted at all (a busy poll cycle that
            # does not reach this instance — exactly what a reap/re-ship churn produces) leaves the
            # counter at 0 while our local copy ages past the threshold, so a long-ago success is
            # indistinguishable from a fresh one and the reaper destroys a HEALTHY box. Live
            # recurrence 2026-07-26 on owned box -2: worker alive and touching HEARTBEAT (verified
            # over ssh at 22:51), our pulled copy frozen at 22:42, reaper fired at 22:50 and took 8
            # tasks across three campaigns with it — the same destruction invariant 10c was written
            # for, through un-attempted rather than failed pulls. So require the confirming read to
            # itself be RECENT. `None` (no pull recorded in this process, e.g. just after a daemon
            # restart) keeps the pre-existing behaviour; the next pull records a timestamp.
            # Invariant 10c(g) (2026-07-31): OBTAIN the confirmation instead of giving up on it.
            #
            # The version below this comment used to `continue` here, and that turned the guard into
            # a fleet-wide OFF SWITCH for the reaper, triggered by the very condition the reaper
            # exists to clean up. `since_ok` is the age of the last HEARTBEAT pull, which can only be
            # refreshed once per POLL CYCLE — so the guard is really the predicate
            # `cycle_time < heartbeat_stale_min`. Both settings were calibrated against a ~7-9 min
            # cycle (see `ship_budget_sec` / `heartbeat_stale_min` in DEFAULTS), and the cycle grows
            # with the number of live boxes and with how many of them are slow — i.e. it grows
            # exactly when boxes are dying. Once it crosses 15 min, EVERY box is skipped on EVERY
            # pass and nothing reaps anything.
            #
            # MEASURED on the live fleet 2026-07-31: poll cycle median 31.0 min (min 19.4, max 41.6)
            # against the 15-min threshold, so the guard fired on essentially every box every pass —
            # **183 `dead_worker_skipped` against 4 `dead_worker`, a 97.9% skip rate** over 12 h. Four
            # boxes with genuinely dead workers survived hours apiece (40000033, 40000034, 40000035,
            # 40000030), each holding its occupants in `claimed`/`running` with NO failure signal;
            # the reaps that did land only fired on the rare unusually-fast cycle. Self-reinforcing,
            # because a dead box eats a 60 s rsync timeout on each of its ~5 pulls per cycle, which
            # lengthens the cycle that disabled the reaper.
            #
            # The fix keeps 10c's actual requirement — never reap on an UNCONFIRMED read — and drops
            # only its accidental dependence on cycle timing: pull `HEARTBEAT` for THIS box, right
            # here, then re-stat. One 0-byte rsync, and only for a box already past the staleness
            # threshold, so the cost is bounded and rare. `rsync -t` preserves the REMOTE mtime, so
            # after a successful pull the local mtime IS the worker's last touch:
            #   * pull fails            -> unconfirmable, skip (unchanged 10c behaviour);
            #   * pull ok, mtime fresh  -> our copy was merely stale; the box is HEALTHY, skip;
            #   * pull ok, mtime stale  -> confirmed fresh read of a stale remote = the worker is
            #                              genuinely dead, reap.
            # A live worker touches HEARTBEAT every `HEARTBEAT_SECONDS` (60 s), so the middle branch
            # is what a healthy box always takes — this is strictly stronger evidence than the
            # pre-10c code acted on, and strictly more actionable than never firing at all.
            since_ok = self.tracker.seconds_since_heartbeat_pull(inst["id"])
            if since_ok is not None and since_ok >= self.settings["heartbeat_stale_min"] * 60:
                host, port = endpoint_for(inst, self.tracker, self.vastai_run)
                confirmed = rsync_pull(host, port, "~/spool/",
                                        str(hb.parent) + "/", ["HEARTBEAT"],
                                        append=False, run=self.run)
                self.tracker.record_heartbeat_pull(inst["id"], confirmed)
                if not confirmed:
                    self.log("dead_worker_skipped",
                              f"last successful pull was {since_ok / 60:.0f}min ago (>= "
                              f"{self.settings['heartbeat_stale_min']}) and a confirming HEARTBEAT "
                              f"pull just failed — staleness cannot be attributed to the worker",
                              instance_id=inst["id"])
                    continue
                age_min = (time.time() - hb.stat().st_mtime) / 60
                if age_min < self.settings["heartbeat_stale_min"]:
                    self.log("dead_worker_confirm_fresh",
                              f"confirming pull refreshed a {since_ok / 60:.0f}min-stale copy to "
                              f"{age_min:.1f}min — box is healthy, our copy was lagging the poll "
                              f"cycle (not the worker)",
                              instance_id=inst["id"])
                    continue
            self.log("dead_worker",
                      f"no heartbeat in {age_min:.0f}min (>= {self.settings['heartbeat_stale_min']})",
                      instance_id=inst["id"])
            occ = [dict(t) for t in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state IN "
                "('claimed','shipped','running','preempting')", (inst["id"],))]
            # Invariant 10c(f): the dead-worker path gets the SAME forensic pull as every other
            # terminal path, and it was the FOURTH place this identical omission had to be fixed
            # (9d `task_failed`, 18f `cancelled`, 9e(f) `artifact_missing`, now here). Until this,
            # `_destroy` ran immediately below and took the only copy of the log with it, so the whole
            # record of every dead worker was the bare string "dead_worker: heartbeat stale" — while
            # this method's own docstring names three DIFFERENT causes (OOM-killed, box hiccup,
            # uncaught exception) that it cannot distinguish. Measured 2026-07-30: four boxes reaped
            # in 56 minutes taking ~14 tasks, dozens more across prior days, and not one diagnosable.
            #
            # This pull is UNUSUALLY likely to succeed, which is the point: 10c above already proved
            # the box is reachable (`consecutive_fails == 0` AND a recent confirmed heartbeat pull),
            # so unlike a genuinely unreachable box we are pulling from a live host. A `claimed` task
            # that never shipped simply has no `run.log` — an empty tail, which is itself the useful
            # signal that the worker died before it ever started work.
            #
            # Rooted ONE LEVEL UP (`active/<id>/`) because `run.log` is a SIBLING of `out/` and an
            # rsync `--include` only matches under its own root — the same structural trap 9d and
            # 9e(f) document. A test must pin this ROOT, not merely "a pull happened". Best-effort
            # throughout: any failure yields an empty tail and the bare reason, and NOTHING here may
            # block the `_infra_fail` transitions or the `_destroy` below.
            tails = {}
            host, port = inst.get("ssh_host"), inst.get("ssh_port")
            if host and port:
                for t in occ:
                    try:
                        local_out = self._result_dir(t)
                        rsync_pull(host, port, f"~/spool/active/{t['id']}/",
                                    str(local_out) + "/", ["run.log"], run=self.run)
                        tails[t["id"]] = self._run_log_tail(local_out / "run.log")
                    except Exception as e:            # noqa: BLE001 - forensics must never block a reap
                        self.log("dead_worker_forensics_failed", f"{type(e).__name__}: {e}",
                                  task_id=t["id"], instance_id=inst["id"])
            for t in occ:
                reason = "dead_worker: heartbeat stale"
                if tails.get(t["id"]):
                    reason += f"\n--- run.log tail ---\n{tails[t['id']]}"
                self._infra_fail(t, reason=reason)
            got = sum(1 for v in tails.values() if v)
            self._alert(f"dead_worker on instance {inst['id']}: {len(occ)} task(s) infra_failed, "
                        f"run.log captured for {got}/{len(occ)} (box was reachable per inv. 10c)")
            self._destroy(inst, "dead_worker: heartbeat stale")

    def _pull_worker_state(self, inst: dict, host: str, port: int) -> None:
        """I/O then apply, for callers that want the whole thing on one thread (tests, and any
        single-box path). `_ingest_and_complete` calls the two halves separately so the I/O can fan
        out across boxes while every mutation stays serial — see invariant 23c."""
        self._apply_worker_state(inst, *self._pull_worker_state_io(inst, host, port))

    def _pull_worker_state_io(self, inst: dict, host: str, port: int) -> tuple[bool, bool]:
        """PURE I/O half: the two rsyncs, into this instance's own scratch dir. No DB, no tracker,
        no logging — safe to run off-thread, one call per box in parallel."""
        local = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}"
        local.mkdir(parents=True, exist_ok=True)
        # NOT `--append` — the same size-comparison trap documented below for HEARTBEAT, reached by
        # TRUNCATION instead of by staying the same size. `--append` skips any file whose source is
        # the same size or SHORTER than the destination, and an on-box `spool_worker.py` restart
        # RECREATES ~/spool/worker.jsonl from empty. Once that happens the local copy is frozen
        # forever, and the damage is silent and total: `_pull_worker_state` reads the frozen file for
        # `start` events, so a task never leaves `shipped`; `_pull_markers` still sees its DONE marker
        # and `_complete_done` still rsyncs the results down — but `shipped -> done` is NOT in
        # LEGAL_TRANSITIONS (only `running -> done` is), so the completion silently no-ops and the
        # task sticks in `shipped`, which is an OPEN state, holding its slot for good. One worker
        # restart therefore wedges that box for every task shipped to it afterwards.
        # Observed live 2026-07-27 on instance -2 (desktop): worker restarted 19:30, local copy stuck
        # at 143345 bytes / 19:26:44 while the remote sat at 1010 bytes / 20:28; two tasks finished
        # (rc=0, results.json pulled) and both still read `shipped` an hour later.
        # Dropping `--append` costs nothing: rsync's ordinary delta transfer already sends only the
        # changed tail of an append-only file, so this stays O(new bytes) while also being correct
        # when the file shrinks.
        ok = rsync_pull(host, port, "~/spool/", str(local) + "/",
                         ["worker.jsonl"], append=False, run=self.run)
        # HEARTBEAT is a 0-byte file that's only ever re-touched, never grown -- `--append`
        # mode decides whether to re-transfer by comparing SIZE, not mtime, so a same-size file
        # looks "already fully appended" and rsync silently skips it forever after the first
        # successful pull, freezing the local mtime even while the remote keeps advancing
        # (verified live: reproduced with a bare rsync --append against a touch'd 0-byte file).
        # `_reap_dead_workers` reads exactly that frozen mtime, so this alone (not any real
        # connectivity failure) was destroying healthy, actively-training boxes ~heartbeat_
        # stale_min after their first pull, 2026-07-14. Pulled as an ordinary (non-append)
        # transfer instead, every cycle, so its mtime always reflects the remote's real one.
        hb_ok = rsync_pull(host, port, "~/spool/", str(local) + "/",
                            ["HEARTBEAT"], append=False, run=self.run)
        return ok, hb_ok

    def _apply_worker_state(self, inst: dict, ok: bool, hb_ok: bool) -> None:
        """MUTATING half: tracker stamps + the `shipped -> running` transitions. Runs serially on
        the main thread, reading the local copy `_pull_worker_state_io` just refreshed."""
        local = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}"
        # Stamp THIS pull specifically. `tracker.record` is fed from 11 call sites (ship, ingest,
        # compile, teardown, ...), so "the last ssh op on this box succeeded" says nothing about
        # when the HEARTBEAT copy the reaper reads was last refreshed — a box being hammered with
        # re-ships looks perfectly connected while its heartbeat copy silently ages. Only the pull
        # that actually rewrites that mtime may vouch for it.
        self.tracker.record_heartbeat_pull(inst["id"], hb_ok)
        if self.tracker.record(inst["id"], ok and hb_ok):
            self.log("ssh_fallback", f"switched instance {inst['id']} to direct endpoint",
                      instance_id=inst["id"])
        wf = local / "worker.jsonl"
        # Process whatever the local copy HOLDS, regardless of this cycle's transport outcome
        # (2026-07-29). Previously this returned early on `not (ok and hb_ok)`, so a failed
        # HEARTBEAT pull — a completely independent 0-byte transfer — discarded a perfectly good
        # worker.jsonl that was already on disk, and any `start` rows in it went unread until some
        # later cycle happened to succeed at BOTH. Until that cycle arrived the task stayed
        # `shipped`, which (before the `shipped -> done` fix) could strand it permanently.
        # Re-reading rows is safe and idempotent: only the LATEST claim/start per task counts, it
        # must be no older than the moment the task most recently entered `shipped`, and the CAS
        # itself no-ops for any task that has already moved on.
        if not wf.exists():
            return
        # worker.jsonl is append-only and covers a task's ENTIRE history across every ship
        # attempt (preemption/infra-failure requeues reuse the same task_id) — a stale
        # claim/start row from an earlier attempt must never be mistaken for this attempt
        # starting, so only the LATEST row per task counts, and only if it belongs to THIS
        # delivery attempt.
        #
        # ⛔ THE ANCHOR IS `claim`, NOT `updated_at` — invariant 7h (2026-08-04). Anchoring on the
        # `shipped` stamp looks right and is a RACE THE BOX WINS ROUTINELY: the CAS to `shipped` is
        # stamped when the dispatcher FINISHES its bookkeeping, in a per-pass BATCH, while the box
        # claims and launches within seconds of the payload landing. So `start` lands BEFORE
        # `updated_at`, the row is rejected, and — because the row never gets any newer — it is
        # rejected FOREVER. The task then runs on the box while the registry holds it in `shipped`.
        #
        # That is not a cosmetic staleness. `_reap_overpacked_boxes` reads a `shipped` task older
        # than `ship_launch_grace_min` on a box with running siblings as OVER-PACKED, so it
        # unschedules and requeues a task that is ACTIVELY TRAINING, `rm -rf`s its `active/<id>`
        # (which makes `reap_orphans` kill the trainer), re-ships it, and the same race repeats — a
        # loop that destroys live work every grace window. Measured 2026-08-04: 11 tasks stuck in
        # `shipped`, every one with a `start` row had `start < updated_at` (gaps 8s-110s, five
        # sharing one batched stamp), and 60 of the last 60 over-pack unschedules were on tasks the
        # box had CLAIMED AND STARTED. It is the m49/m50/m54 silent-restart class again.
        #
        # `claim` (the queued -> claimed CAS) is when THIS delivery attempt BEGAN, so it is both
        # correct and strictly earlier than any start the box could report for it — the box cannot
        # hold a payload before we started sending it. It keeps the stale-row property the guard
        # exists for: a previous attempt's rows precede this attempt's `claim`.
        latest: dict[str, str] = {}
        for line in wf.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") in ("claim", "start"):
                tid = row.get("task_id")
                if tid is not None:
                    latest[tid] = row.get("t", latest.get(tid, ""))
        for tid, t_str in latest.items():
            t = registry_db.get_task(self.conn, tid)
            if t and t["state"] == "shipped" and t_str >= self._attempt_started_at(t):
                registry_db.transition(self.conn, tid, "running", "start", "worker reported start")

    def _attempt_started_at(self, task: dict) -> str:
        """When THIS delivery attempt began — the task's most recent `claim` event (invariant 7h).

        The reference a box-reported `start`/`claim` row must beat to count as belonging to the
        current attempt. See `_apply_worker_state` for why this is `claim` and not the `shipped`
        stamp: the box routinely starts BEFORE that stamp is written, so `updated_at` rejects a
        perfectly good start forever and the over-pack reaper then requeues a running task.

        Falls back to `updated_at` when no `claim` event exists — a pre-event-log row, or a task
        placed by a path that did not log one. That is the OLD behaviour, so the fallback can only
        ever be as wrong as before, never worse, and it never accepts a stale row it should reject
        (`updated_at` is the later of the two anchors)."""
        row = self.conn.execute(
            "SELECT t FROM events WHERE task_id=? AND event='claim' ORDER BY seq DESC LIMIT 1",
            (task["id"],)).fetchone()
        return row["t"] if row and row["t"] else task["updated_at"]

    def _box_reports_started(self, inst: dict, task_id: str) -> bool:
        """Whether the BOX's own log says it started this task under the current attempt.

        The safety valve for invariant 7h: even with the anchor fixed, anything that leaves a task
        `shipped` while the box runs it turns `_reap_overpacked_boxes` into a killer of live work.
        This makes the reaper check the box's account before acting, so the destructive half can
        never fire on a task that is demonstrably training. Read from the ALREADY-PULLED local copy
        — no extra ssh, and it is the same file `_apply_worker_state` reads."""
        wf = EXPERIMENTS_ROOT / ".dispatcher" / f"instance_{inst['id']}" / "worker.jsonl"
        try:
            if not wf.exists():
                return False
            started = None
            for line in wf.read_text(errors="ignore").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("task_id") != task_id:
                    continue
                ev = row.get("event")
                if ev == "start":
                    started = row.get("t", "")
                elif ev == "exit":            # the run ended — a later start would re-set this
                    started = None
            if started is None:
                return False
            t = registry_db.get_task(self.conn, task_id)
            return bool(t) and started >= self._attempt_started_at(dict(t))
        except OSError:
            return False                      # unreadable copy must never block the reaper

    def _pull_tb_events(self, inst: dict, host: str, port: int) -> None:
        """Invariant 9b: TB events are append-only, so every poll — not just the periodic
        checkpoint pull (9c) or terminal completion (9d) — rsyncs them with `--append` into
        `experiments/<grp>/<name>/tb/`. Without this, a `running` task's scalars never land
        locally until it finishes, so the dashboard (which discovers runs by local `tb/`/
        `results.json` presence) can't show it as in-progress."""
        for t in [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state='running'", (inst["id"],))]:
            local_out = self._result_dir(t)
            ok = rsync_pull(host, port, f"~/spool/active/{t['id']}/out/", str(local_out) + "/",
                             ["*/", "tb/**"], append=True, run=self.run)
            self.tracker.record(inst["id"], ok)

    def _has_open_tasks(self, inst: dict) -> bool:
        """Whether this box is worth an ssh for markers at all — hoisted so the parallel ingest can
        decide that in its SERIAL planning pass (it is a DB read)."""
        return self.conn.execute(
            "SELECT 1 FROM tasks WHERE instance_id=? AND state IN "
            "('shipped','running','preempting','cancelling') LIMIT 1", (inst["id"],)).fetchone() \
            is not None

    def _pull_markers_io(self, host: str, port: int):
        """PURE I/O half: the one marker-listing ssh. No DB, no tracker — safe off-thread."""
        remote_cmd = ("bash -c 'shopt -s nullglob; for f in ~/spool/active/*/DONE "
                      "~/spool/active/*/FAILED_* ~/spool/active/*/PREEMPTED "
                      "~/spool/active/*/CANCELLED; do echo \"$f\"; done'")
        return ssh_run(host, port, remote_cmd, run=self.run)

    def _pull_markers(self, inst: dict, host: str, port: int) -> None:
        """I/O then apply on one thread — see `_pull_worker_state` for why both forms exist."""
        if not self._has_open_tasks(inst):
            return
        self._apply_markers(inst, host, port, self._pull_markers_io(host, port))

    def _apply_markers(self, inst: dict, host: str, port: int, out) -> None:
        """MUTATING half: every terminal completion the markers imply. Serial, main thread.

        Still takes `host`/`port` because the completion helpers pull results home themselves —
        those transfers stay on this thread deliberately: they write into `experiments/<grp>/<name>/`
        and drive terminal CASes, so they are mutation, not the latency-bound listing above."""
        open_tasks = {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE instance_id=? AND state IN "
            "('shipped','running','preempting','cancelling')",
            (inst["id"],))}
        if not open_tasks:
            return
        ok = out.returncode == 0
        if self.tracker.record(inst["id"], ok):
            self.log("ssh_fallback", f"switched instance {inst['id']} to direct endpoint",
                      instance_id=inst["id"])
        if not ok:
            return
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rstrip("/").split("/")
            marker, task_id = parts[-1], parts[-2]
            task = open_tasks.get(task_id)
            if task is None:
                continue
            # A cancelling task terminates as `cancelled` on ANY marker (its CANCELLED marker
            # normally, or a DONE/FAILED/PREEMPTED if the run happened to finish mid-cancel).
            if task["state"] == "cancelling" or marker == "CANCELLED":
                self._complete_cancelled(task, host, port)
            elif marker == "DONE":
                self._complete_done(task, inst, host, port)
            elif marker.startswith("FAILED_"):
                self._complete_failed(task, inst, host, port, marker)
            elif marker == "PREEMPTED":
                self._complete_preempted(task, inst, host, port)

    def _result_dir(self, task: dict) -> Path:
        d = EXPERIMENTS_ROOT / _fs_safe_component(task["grp"]) / _fs_safe_component(task["name"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _gc_box_task_dir(self, task: dict, host: str | None, port: int | None, why: str) -> None:
        """Invariant 29a: drop `~/spool/active/<id>` (+ any `incoming/` remnant) once a task has
        reached a TERMINAL registry state and its evidence is home.

        Called only from the three terminal completions, and only AFTER their pull, because the
        whole hazard here is deleting the one copy of a finished run before it is safe. Note what
        `_complete_done` gates on: `artifact.exists()` — the completion artifact actually present
        locally — NOT the rsync exit code, which invariants 9e/9g established can be false on a
        transfer that nonetheless landed (and true on one that timed out mid-checkpoint).

        Best-effort by design: an unreachable box, a dead worker, a torn-down rental all make this a
        no-op, and none of them may block or reverse the terminal CAS the caller just performed.
        That is exactly why invariant 29b exists — the worker sweeps its own finished dirs on a
        12h marker age, so anything this misses is collected without the coordinator's help."""
        if host is None or port is None:
            return
        res = ssh_run(host, port,
                      f"rm -rf ~/spool/active/{task['id']} ~/spool/incoming/{task['id']}",
                      run=self.run)
        if res.returncode != 0:
            self.log("spool_gc_defer",
                      f"box cleanup failed rc={res.returncode} after {why} — the worker's own "
                      "finished-dir sweep (inv. 29b) will collect it",
                      task_id=task["id"])

    # Files whose loss is unrecoverable and which the evidence check below therefore insists on.
    # Deliberately NOT `tb/**`: TensorBoard events have their own puller (`_pull_tb_events`) and a
    # missing event file costs a graph, not a result.
    _EVIDENCE_GLOBS = ("*.json", "*.jsonl", "*.log", "ckpt_*.pt")

    def _evidence_home(self, task: dict, host: str, port: int, local_out) -> tuple[bool, str]:
        """Invariant 29a, precondition: is EVERY evidence file actually home, at full size?

        ⛔ WHY THIS EXISTS. `_complete_done` declares `done` on `artifact.exists()` — the completion
        artifact alone — and then `_gc_box_task_dir` `rm -rf`s the box's copy. The comment on the 9g
        retry states the premise that made that safe: "The bulk artifacts are not lost — the next
        ingest pass keeps pulling them; only the COMPLETION DECISION is decoupled". THERE IS NO NEXT
        INGEST PASS: the same branch deletes the source, so whatever the 60s `rsync_pull` budget did
        not move is destroyed.

        MEASURED 2026-08-13, `m55e_bid_scout2`: three arms of one sweep, each writing nine per-stage
        substrate checkpoints at ~42 MB. `ctrl` landed 9/9; `bid` landed **2 of 9** and `moe` **4 of
        9**, each with a `.rsync-partial` remnant and a `ckpt_latest.pt` truncated to 10,775 bytes
        against a 40 MB predecessor. All three were `done` with "completion artifact verified", and
        the box dirs were already gone — the campaign's pre-registered per-stage collapse gate was
        UNCOMPUTABLE and unrecoverable. The same signature is recorded a campaign earlier
        (`m55_fanin_scout3`: "only 3 and 6 of 9 stage checkpoints pulled"), read at the time as a
        flaky transfer rather than deletion.

        ⚠ THE EXIT CODE IS NOT THE TEST, and `_gc_box_task_dir`'s docstring is right about why:
        invariants 9e/9g established that rsync's status can be false on a transfer that landed and
        true on one that timed out mid-checkpoint. So compare the FILE SET instead — remote name and
        size against local — which is the "its evidence is home" proof that docstring already claims
        to be making.

        Best-effort in the safe direction: if the box cannot be reached or the listing cannot be
        parsed, report NOT home. The cost of a false negative is a directory that survives until the
        worker's own 12h sweep (inv. 29b) collects it; the cost of a false positive is a destroyed
        experiment. Those are not comparable, so this never guesses in the second direction."""
        if host is None or port is None:
            return False, "box unreachable"
        pat = " -o ".join(f"-name '{g}'" for g in self._EVIDENCE_GLOBS)
        res = ssh_run(host, port,
                      f"find ~/spool/active/{task['id']}/out/ -maxdepth 1 -type f "
                      f"\\( {pat} \\) -printf '%s %f\\n'", run=self.run)
        if res.returncode != 0:
            return False, f"remote listing failed rc={res.returncode}"
        missing, short = [], []
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            size, _, name = line.partition(" ")
            try:
                want = int(size)
            except ValueError:
                return False, f"unparseable listing line {line!r}"
            p = local_out / name
            if not p.exists():
                missing.append(name)
            elif p.stat().st_size < want:
                short.append(f"{name} {p.stat().st_size}<{want}")
        if missing or short:
            bits = []
            if missing:
                bits.append(f"absent {len(missing)}: {', '.join(sorted(missing)[:4])}")
            if short:
                bits.append(f"truncated {len(short)}: {', '.join(sorted(short)[:4])}")
            return False, "; ".join(bits)
        return True, "all evidence home"

    def _complete_done(self, task: dict, inst: dict, host: str, port: int) -> None:
        local_out = self._result_dir(task)
        remote = f"~/spool/active/{task['id']}/out/"
        includes = ["*/", "tb/**", "*.json", "*.jsonl", "*.log", "ckpt_*.pt"]
        ok = rsync_pull(host, port, remote, str(local_out) + "/", includes, run=self.run)
        self.tracker.record(inst["id"], ok)
        entry = entrypoints.resolve(task)  # manifest contract if present, else the named table
        artifact = local_out / entry.completion_artifact
        # The worker only raises DONE after the run finished and wrote its completion artifact, so
        # the artifact's PRESENCE is the real proof of completion — not the transport's exit code.
        # A transient rsync failure (box torn down mid-pull, ssh drop) can return ok=False even
        # after the artifact itself transferred; retry once, then decide on presence. Gating on
        # `ok` alone marked provably-completed runs task_failed on a network blip — observed
        # fleet-wide 2026-07-11, where runs with a valid results.json on disk were lost as
        # `artifact_missing`.
        if not artifact.exists():
            # Invariant 9g (2026-07-30): the retry pulls ONLY the completion artifact, NOT the bulk
            # includes again. Repeating them re-attempts the very transfer that just failed, and when
            # the cause is SIZE rather than a network blip that retry can never succeed — it is the
            # same doomed 60s budget against the same bytes, which is why the reason reads "absent
            # after 2 pulls".
            #
            # Live 2026-07-30, `m49_dreamfix/fleetcheck`: the run COMPLETED (11m23s, `DONE` marker,
            # final summary line in run.log) and `results.json` was verified present ON THE BOX at
            # 6,082 bytes by ssh — alongside `ckpt_substrate_seed0.pt` at **235,420,985 bytes**. The
            # `out/`-rooted pull is ONE rsync whose includes carry `ckpt_*.pt`, so the 235 MB file
            # blows the 60s `rsync_pull` budget and NOTHING lands, including the 6 KB artifact that
            # proves success. Comparable runs whose substrate checkpoints were 13 MB pulled fine.
            # The task then went `task_failed` — terminal, never auto-requeues — discarding a
            # finished run, and the alert told the owner to check their `completion_artifact`
            # declaration, which was not the problem at all.
            #
            # This is the SAME failure class the 2026-07-11 note above describes ("runs with a valid
            # results.json on disk were lost as artifact_missing"); that fix added the retry, and this
            # one makes the retry actually able to succeed. The bulk artifacts are not lost — the next
            # ingest pass keeps pulling them; only the COMPLETION DECISION is decoupled from having to
            # move hundreds of MB inside one timeout.
            ok = rsync_pull(host, port, remote, str(local_out) + "/",
                             [entry.completion_artifact], run=self.run) or ok
        if artifact.exists():
            registry_db.transition(self.conn, task["id"], "done", "done",
                                    "completion artifact verified",
                                    extra_set={"result_path": str(local_out)})
            # Inv. 29a — but gated on ALL the evidence being home, not just the completion artifact.
            # The `done` CAS above is deliberately NOT gated on this: the run finished and saying so
            # is correct. What is not correct is destroying the only copy of the bytes we failed to
            # pull. When they are not home, leave the box dir for the next ingest pass and let the
            # worker's 12h sweep (inv. 29b) bound the disk — which is exactly the guarantee that
            # makes deferring safe, and it did not exist until `_prune_finished` landed.
            home, why = self._evidence_home(task, host, port, local_out)
            if home:
                self._gc_box_task_dir(task, host, port, "done")
            else:
                self.log("spool_gc_defer",
                          f"box cleanup DEFERRED after done — evidence not fully home ({why}); "
                          "inv. 29b's 12h sweep will collect it if it is never pulled",
                          task_id=task["id"])
                self._alert(f"task {task['id']} ({task.get('grp')}/{task.get('name')}) completed but "
                            f"its artifacts are NOT fully home: {why}. Box dir kept for re-pull.")
        else:
            # Invariant 9e: `artifact_missing` gets the SAME forensic pull as every other terminal
            # failure. This was the last uncovered terminal path — 9d covered `task_failed` from a
            # FAILED_<rc> marker and 18f covered `cancelled`, but a task the worker declared DONE
            # while its completion artifact never appeared landed here with the bare string
            # "artifact_missing" and no log at all.
            #
            # It is the WORST case to leave dark, because the worker claimed SUCCESS: the run is not
            # obviously broken, so the owner has no hypothesis to start from. Live 2026-07-30,
            # `m49_phase1_eye/p1_gv0`: ran **2h07m**, wrote ckpt_latest.pt + .prev + two substrate
            # checkpoints + TB events — so it plainly worked — then died terminally
            # (`task_failed` never auto-requeues) with nothing to explain which of "the trainer never
            # wrote its artifact" / "it wrote it under a different name" / "the job config declares
            # the wrong `completion_artifact`" happened. Two hours of compute, no diagnosis, and the
            # box gets torn down minutes later taking the only copy of the log.
            #
            # Rooted ONE LEVEL UP (`active/<id>/`) because `run.log` is a SIBLING of `out/` and an
            # rsync `--include` only matches under its own root — the pull above cannot reach it, the
            # same structural trap 9d documented. Best-effort: a failed pull yields an empty tail and
            # the bare reason, never blocking the CAS.
            rsync_pull(host, port, f"~/spool/active/{task['id']}/", str(local_out) + "/",
                        ["run.log"], run=self.run)
            tail = self._run_log_tail(local_out / "run.log")
            expected = entry.completion_artifact
            reason = f"artifact_missing ({expected} absent after 2 pulls)"
            if tail:
                reason += f"\n--- run.log tail ---\n{tail}"
            # The artifact is absent while the worker said DONE, so this is a contract mismatch
            # between the trainer and its declared `completion_artifact` — a code/config problem
            # that will repeat for every sibling arm. Same reasoning as the zero-progress tripwire:
            # make it one loud alarm instead of N indistinguishable red rows.
            self._alert(f"{task['grp']}/{task['name']}: DONE but {expected} missing — check the "
                        f"job's completion_artifact declaration. {tail.splitlines()[-1] if tail else ''}")
            registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)

    def _complete_failed(self, task: dict, inst: dict, host: str, port: int, marker: str) -> None:
        local_out = self._result_dir(task)
        rsync_pull(host, port, f"~/spool/active/{task['id']}/out/", str(local_out) + "/",
                    ["*.log", "*.json"], run=self.run)
        # The trainer's stdout+stderr -- the actual crash traceback -- is captured by the box worker
        # to `active/<id>/run.log` (spool_worker.py), a SIBLING of `out/` one level ABOVE it, so the
        # `out/`-rooted pull above (and every other ingest pull) structurally can never reach it: an
        # rsync `--include` only matches paths UNDER its remote root. A crash while BUILDING the
        # model writes nothing to `out/` at all, so before this the entire record of WHY a task_failed
        # was the bare "worker exit <rc>" code, and the traceback lived only on a box that idle-
        # teardown then destroyed -- unrecoverable (live 2026-07-24: a substrate that built cleanly
        # in the identical local trainer path failed on the box, exit code was all we had). Pull
        # run.log home ROOTED ONE LEVEL UP (`active/<id>/`, so the `run.log` include can match) into
        # experiments/<grp>/<name>/run.log for full inspection, then fold its tail into the failure
        # reason (surfaced by `runq show`) and the [ALERT] (coordinator.log) so the crash cause is
        # visible without ssh'ing to a box that may already be gone. Best-effort: a failed pull just
        # yields an empty tail and the old bare-exit reason -- never blocks the task_failed CAS.
        rsync_pull(host, port, f"~/spool/active/{task['id']}/", str(local_out) + "/",
                    ["run.log"], run=self.run)
        tail = self._run_log_tail(local_out / "run.log")
        rc = marker.split("_", 1)[1] if "_" in marker else "?"
        # Zero-scalar tripwire: a task_failed that died before writing so much as one checkpoint
        # or TB event almost certainly crashed on a code/config bug (bad import, device mismatch,
        # bad CLI arg) -- near-incident 2026-07-14 (e6278c5), where a GPU device mismatch crashed
        # every F arm identically and it took a while to notice among routine infra failures.
        # `task_failed` never auto-requeues (registry_db.LEGAL_TRANSITIONS), so this isn't about
        # retry policy -- it's making the "your code is broken, stop shipping it" signal loud and
        # immediate instead of ten identical red rows a human has to notice by hand.
        progressed = self._task_made_progress(local_out)
        label = "progress" if progressed else "ZERO PROGRESS (likely code/config bug, not infra)"
        # Invariant 9d(g): PREFER the trainer's own structured crash record over the log tail.
        # `shared.infra.run` writes `out/crash.json` ({"traceback": "..."}) on an uncaught exception,
        # so it is a deliberate, parse-free record of exactly the thing a human needs — and it comes
        # home on the FIRST pull above (`out/`, include `*.json`), which is the reliable one. The
        # `run.log` tail needs the SECOND, one-level-up pull, and that is the one observed to miss:
        # live 2026-07-30, `m50_stage_reset/armnostagereset…` landed `run.log` (9190 bytes) AND
        # `crash.json` (8635 bytes) in its result dir while its `task_failed` reason was the bare
        # string "worker exit 1" — the tail read empty at reason-construction time even though the
        # log arrived. Two arms of that campaign failed identically within 60s, each recording
        # nothing, while the traceback (an unexpected-keyword error naming every valid param — a
        # knob-reachability bug, cf. `native.diagnostics.knob_reachability`) sat unread on disk.
        # Both sources are used, crash.json FIRST because it is the deliberate one; each is bounded
        # the same way. Best-effort: unreadable/absent/malformed JSON contributes nothing and never
        # blocks the terminal CAS.
        crash_tb = self._crash_traceback(local_out / "crash.json")
        reason = f"worker exit {rc}" if progressed else f"worker exit {rc} ({label})"
        if crash_tb:
            reason = f"{reason}\n--- crash.json traceback ---\n{crash_tb}"
        if tail:
            reason = f"{reason}\n--- run.log tail ---\n{tail}"
        # ⛔ INVARIANT 20j-2 — a CheckpointRegression over REAL PROGRESS is INFRA, not a task fault.
        #
        # `shared.infra.checkpoint` refuses a write that moves progress backwards. That is not the
        # trainer failing; it is the trainer being RESTARTED underneath — a worker re-exec, retire,
        # crash or reboot relaunching a task that already has valid work on disk. The task's own code
        # is fine, and it is fully resumable from the very checkpoint the guard just protected.
        #
        # Classifying it `task_failed` was wrong twice over: that state is TERMINAL and never
        # auto-requeues, so every one of these needed a human to notice and hand-requeue it (17 did,
        # 2026-08-02), and it burns the "your code is broken" signal on the scheduler's mistake.
        # `_infra_fail` requeues at HALF a retry with `resume_checkpoint` still on the row, so the
        # task warm-starts from where it was — which is what should have happened all along.
        #
        # ⚠ GATED ON `progressed`, deliberately. The guard also fires on a genuine code bug that
        # clobbers its own checkpoint (m49 162127c is exactly that), and routing THAT to infra would
        # convert a loud terminal failure into up to 2*max_retries silent retries — fail-closed
        # turned back into fail-silent, which is the defect this whole class keeps producing. With
        # ZERO progress there is no valid work to protect, so a regression there is the code, and it
        # stays terminal. The ALERT fires either way, so neither path is silent.
        if progressed and "CheckpointRegression" in f"{crash_tb}\n{tail}":
            infra_reason = ("VOID (infra), not a result: CheckpointRegression — the task was "
                            f"RESTARTED over valid progress, not a code fault (exit {rc}). "
                            "Requeued to resume from its own checkpoint.\n" + reason)
            self._alert(f"infra_failed [checkpoint_regression]: {task['grp']}/{task['name']} "
                        f"({task['entrypoint']}) was restarted over valid progress — requeued to "
                        f"RESUME, not rerun"
                        + (f"\n--- crash.json traceback ---\n{crash_tb}" if crash_tb else ""))
            self._infra_fail(task, reason=infra_reason)
            return
        self._alert(f"task_failed [{label}]: {task['grp']}/{task['name']} "
                    f"({task['entrypoint']}) exit {rc}"
                    + (f"\n--- crash.json traceback ---\n{crash_tb}" if crash_tb else "")
                    + (f"\n--- run.log tail ---\n{tail}" if tail else ""))
        registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)
        # Inv. 29a — `run.log` + `crash.json` were pulled above, so the forensics are already home.
        self._gc_box_task_dir(task, host, port, "task_failed")

    def _crash_traceback(self, path: Path, max_lines: int = 40, max_bytes: int = 4096,
                          max_line: int = 300) -> str:
        """The TAIL of the `traceback` string in a trainer-written `out/crash.json` (invariant 9d(g)).

        Tailed, not headed: a Python traceback ends with the exception type and message, which is the
        line that identifies the bug — the head is just the call chain into it. Bounded identically to
        `_run_log_tail` so the DB reason and coordinator.log stay small.

        Best-effort by construction: a missing file, unreadable bytes, non-JSON content, a JSON value
        that is not an object, or a missing/blank `traceback` key all yield '' and the caller falls
        back to the bare exit-code reason. This must never raise — it runs immediately before a
        terminal CAS, and a task stuck non-terminal is far worse than a reason without a traceback."""
        try:
            payload = json.loads(path.read_text(errors="replace"))
        except (OSError, ValueError):
            return ""
        tb = payload.get("traceback") if isinstance(payload, dict) else None
        if not isinstance(tb, str) or not tb.strip():
            return ""
        # Clip each LINE to its HEAD, then drop whole lines from the FRONT to fit. The first version
        # of this took `tb[-max_bytes:]` and it defeated itself on the exact failure class that
        # motivated 9d(g): when the exception MESSAGE is huge — `TypeError: … got an unexpected
        # keyword argument 'stage_reset'` followed by a `dict_keys([…])` dump of every valid
        # parameter, ~8 KB on ONE line — a byte-tail lands mid-dump and keeps the useless end while
        # dropping the exception name and the offending argument entirely. Live 2026-07-30,
        # `m50_navdefault/ctl_s1184f5`: the stored tail was 4120 chars over 2 lines (longest 4095)
        # and contained no exception name at all. Head-clipping per line inverts that — a frame line
        # is short and survives whole, and the exception line keeps the part that names the bug.
        # Dropping from the front to fit preserves the LAST lines, where the exception lives.
        # (`_run_log_tail` reads bytes from EOF and has the same weakness on such a line; the crash
        # record is the preferred source precisely because it can be parsed properly.)
        lines = [ln for ln in tb.splitlines() if ln.strip()][-max_lines:]
        lines = [ln if len(ln) <= max_line
                 else f"{ln[:max_line]}… (+{len(ln) - max_line} more chars)" for ln in lines]
        while len(lines) > 1 and len("\n".join(lines)) > max_bytes:
            lines.pop(0)
        return "\n".join(lines).strip()[:max_bytes]

    def _run_log_tail(self, path: Path, max_lines: int = 40, max_bytes: int = 4096) -> str:
        """The tail of a pulled `run.log` (the trainer's captured stdout+stderr) -- a Python crash
        traceback ends with the exception type + message, so a bounded tail is the useful failure
        signal without dragging a possibly multi-MB log into the DB reason / coordinator.log. Reads
        only the last `max_bytes` (seek from EOF, dropping any partial leading line) then the last
        `max_lines` of that. Best-effort: a missing/unreadable log (pull failed, box torn down) or
        an empty one yields '' so the caller falls back to the bare exit-code reason."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                data = f.read()
        except OSError:
            return ""
        text = data.decode("utf-8", errors="replace")
        if size > max_bytes:
            text = text.split("\n", 1)[-1]  # drop the partial first line the byte-window cut
        return "\n".join(text.rstrip("\n").splitlines()[-max_lines:]).strip()

    def _task_made_progress(self, local_out: Path) -> bool:
        if (local_out / "ckpt_latest.pt").exists():
            return True
        tb = local_out / "tb"
        return tb.exists() and any(tb.rglob("events.out.tfevents*"))

    def _latest_tb_mtime(self, local_out: Path) -> float | None:
        """Newest local TB event-file mtime for a run, or None if none pulled yet. `_pull_tb_events`
        rsyncs `tb/` with `-t`, so this mirrors the trainer's last scalar write on the box — the
        liveness signal `stall_decision` uses so a TB-streaming-but-not-checkpointing run isn't
        false-reaped (invariant 19f)."""
        tb = local_out / "tb"
        if not tb.exists():
            return None
        mtimes = [p.stat().st_mtime for p in tb.rglob("events.out.tfevents*")]
        return max(mtimes) if mtimes else None

    def _alert(self, message: str) -> None:
        # Cheap fail-fast surfacing: `dispatcher_ctl.sh start` redirects stdout/stderr into
        # coordinator.log, so this lands there immediately without a new transport -- no
        # dashboard/notify plumbing needed for the signal to be visible.
        print(f"[ALERT] {message}", file=sys.stderr, flush=True)

    def _complete_preempted(self, task: dict, inst: dict, host: str, port: int) -> None:
        local_out = self._result_dir(task)
        # Pull the `.prev` SPARE alongside the head. `checkpoint.save_atomic` keeps one previous
        # generation precisely so "a resume that meets a corrupt head can fall back one step instead
        # of losing the task" — but the spare lives on the box, and pulling only `ckpt_latest.pt`
        # meant it never crossed, so the mitigation could never fire. Measured 2026-07-29: 3 of the
        # 12 most recent fleet-wide pulls were truncated by interrupted transfers, across three
        # campaigns, and NONE had a local spare. Consumed by `checkpoint.load_checkpoint`.
        ok = rsync_pull(host, port, f"~/spool/active/{task['id']}/out/", str(local_out) + "/",
                         ["ckpt_latest.pt", "ckpt_latest.pt.prev"], run=self.run)
        self.tracker.record(inst["id"], ok)
        # Invariant 17f: a FAILED final pull must not DISCARD a checkpoint we already hold.
        #
        # This used to require `ok and ckpt.exists()`, so one transient rsync failure at preempt time
        # threw away the copy the 5-minutely `_pull_checkpoints` had already fetched, and the task
        # restarted from ZERO with a perfectly good checkpoint sitting on our disk. Measured
        # 2026-07-29: 27 of 313 preempts in 24h (8.4%) reported "no checkpoint pulled", and their
        # median run length was 14.6 min — one had run **121 min** with a 5-min pull cadence, so a
        # never-pulled checkpoint cannot explain them. 22 of 23 had a checkpoint on disk.
        #
        # Dropping `ok` is SAFE, not optimistic, because `checkpoint.load_checkpoint` was built for
        # exactly this: it tries the head, falls back to `<path>.prev` when the head is corrupt or
        # truncated (which `--inplace` CAN leave behind on a failed pull), and returns None — "start
        # fresh" — only when both are unreadable. So the three cases are: intact head ⇒ resume
        # (previously LOST); truncated head ⇒ resume from the spare (previously LOST); both bad ⇒
        # start fresh, identical to today. Worst case equals the old behaviour; there is no case
        # where this is worse. Pointing at the head path when only `.prev` exists is fine — the
        # loader skips a missing candidate.
        ckpt = local_out / "ckpt_latest.pt"
        have = ckpt.exists() or (local_out / "ckpt_latest.pt.prev").exists()
        extra = {"resume_checkpoint": str(ckpt)} if have else {}
        if have and ok:
            reason = "preempted, checkpoint carried forward"
        elif have:
            # The distinction the old single message hid, and why those 27 were undiagnosable.
            reason = ("preempted, carrying forward the last PULLED checkpoint — the final pull "
                      "FAILED, so it may be stale or truncated (resume falls back to .prev)")
        else:
            reason = "preempted, no checkpoint pulled (none on the box, and none held locally)"
        registry_db.transition(
            self.conn, task["id"], "queued", "preempt_requeue", reason,
            extra_set={**extra, "instance_id": None})

    def _complete_cancelled(self, task: dict, host: str | None = None,
                            port: int | None = None) -> None:
        """Terminal completion for a cancel-in-flight (2026-07-09): the worker stopped, so free the
        slot and mark `cancelled`. No RESULTS are pulled — a cancelled run is discarded, not resumed.

        The `run.log` IS pulled though (invariant 18f, 2026-07-29). Invariant 9d's forensic pull was
        scoped to `task_failed`, which left the most diagnostically valuable case uncovered: cancel
        is how an operator stops a task that is MISBEHAVING, and the box is torn down right after, so
        a hung-then-cancelled cell left no forensic trail whatsoever — the evidence died with the
        box. Same one-level-up rooting as `_complete_failed` (`active/<id>/`, so the `run.log`
        include can match; every other ingest pull is rooted at `out/` and structurally cannot reach
        it). Best-effort and BEFORE the CAS only in ordering, never in dependency: a missing endpoint
        (the cancelling task's box was already lost — invariant 18c) or a failed pull just leaves no
        log, and must never block the terminal transition."""
        if host is not None and port is not None:
            local_out = self._result_dir(task)
            rsync_pull(host, port, f"~/spool/active/{task['id']}/", str(local_out) + "/",
                        ["run.log"], run=self.run)
        registry_db.transition(
            self.conn, task["id"], "cancelled", "cancelled",
            "worker stopped on cancel request", extra_set={"instance_id": None})
        # Inv. 29a — a cancelled run is discarded by definition; `run.log` (18f) is already home.
        self._gc_box_task_dir(task, host, port, "cancelled")

    def _signal_cancels(self) -> None:
        """Execute cancel-in-flight requests: for each `cancelling` task, tell its worker to stop
        (idempotent CANCEL marker; the worker's CANCELLED marker completes it next poll). A
        cancelling task with no live worker (never shipped, or its box was lost) has nothing to
        stop -> complete it terminally here."""
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM tasks WHERE state='cancelling'")]
        if not rows:
            return
        # Box-pause spec: a `paused` box's worker is still alive and reachable, so a CANCEL issued
        # while the box is paused must be delivered (the worker's SIGKILL fallback stops even a
        # SIGSTOP-frozen proc), not short-circuited to a terminal cancel with the proc left running.
        live = {r["id"] for r in self.conn.execute(
            "SELECT id FROM instances WHERE state IN ('live','paused')")}
        for task in rows:
            iid = task["instance_id"]
            if iid is None or iid not in live:
                self._complete_cancelled(task)
                continue
            inst = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            host, port = endpoint_for(dict(inst), self.tracker, self.vastai_run)
            # ⛔ THE SIGNAL'S FAILURE MUST BE OBSERVED, or a cancel can never terminate. This used to
            # be a bare `touch` whose exit code was discarded, so the dispatcher could not tell
            # "signalled, now waiting for the worker's CANCELLED marker" from "the spool dir is GONE,
            # so no marker will EVER be written". The second case loops forever: the task sits in
            # `cancelling`, `_signal_cancels` re-touches every poll, and because a non-terminal task
            # still counts as an occupant it LEAKS A LANE for the life of the daemon.
            #
            # MEASURED 2026-08-14, `m55e_boxcheck2/bid_tower`: cancelled at 15:52 (the worker
            # SIGKILLed it, `exit rc=-9`), and a SIGKILLed process cannot write its own marker. The
            # dir was then cleaned up, so `touch` failed silently from 15:52 onward — CANCEL markers
            # re-written at 18:23, 18:25, 18:29, 18:32, 18:38, 18:45, 18:53, 19:01 … while the task
            # held one of tower's 16 slots (14 running + 1 phantom).
            #
            # `test -d` FIRST separates the three cases by exit code, which a bare `touch` cannot:
            #   0   dir present, marker written  -> the worker will answer; keep waiting
            #   1   dir ABSENT                   -> nobody is left to answer; complete it here
            #   else (255, timeout, …)           -> the SSH itself failed; say nothing, retry next poll
            # The third case is why this does not simply treat "nonzero" as absent: a transient ssh
            # error on a live box would otherwise short-circuit to a terminal cancel while the
            # trainer kept running — exactly what the box-pause note above forbids.
            res = ssh_run(host, port,
                          f"test -d ~/spool/active/{task['id']} && "
                          f"touch ~/spool/active/{task['id']}/CANCEL", run=self.run)
            if res.returncode == 0:
                self.log("cancel_signal", "CANCEL marker written", task_id=task["id"],
                         instance_id=iid)
            elif res.returncode == 1:
                self.log("cancel_orphaned",
                         "spool dir absent on a LIVE box — the worker no longer holds this task "
                         "(SIGKILL leaves no marker), so no CANCELLED will ever arrive; completing "
                         "terminally instead of re-signalling forever",
                         task_id=task["id"], instance_id=iid)
                self._complete_cancelled(task)
            else:
                self.log("cancel_signal_failed",
                         f"ssh rc={res.returncode} — cannot tell whether the task dir exists; "
                         "leaving in `cancelling` for the next poll",
                         task_id=task["id"], instance_id=iid)

    def _pull_checkpoints(self, inst: dict, host: str, port: int) -> None:
        interval = self.settings["checkpoint_pull_every_min"] * 60
        last = self._last_ckpt_pull.get(inst["id"], 0.0)
        if time.time() - last < interval:
            return
        self._last_ckpt_pull[inst["id"]] = time.time()
        for t in [dict(r) for r in self.conn.execute(
                "SELECT * FROM tasks WHERE instance_id=? AND state='running'", (inst["id"],))]:
            local_out = self._result_dir(t)
            # Trainers write ckpt_latest.pt under out/<tag>/ (run_dir = out/cfg.tag), not at
            # out/'s top level. A bare ["ckpt_latest.pt"] include never matched: rsync's trailing
            # --exclude '*' blocks descent into out/<tag>/ without a "*/" include, so the pull
            # fetched nothing AND the post-pull `local_out/"ckpt_latest.pt"` check looked at the
            # wrong depth. resume_checkpoint therefore stayed None, so an infra-driven requeue
            # (_infra_fail) restarted the run from scratch instead of warm-starting from the
            # latest weights — silently torching hours of progress on every box loss/preemption
            # (2026-07-15, ff_rec_linear_L2 pretrain restarted at upd~1600). Mirror _complete_done:
            # include "*/" so rsync descends, then locate the checkpoint wherever it landed.
            ok = rsync_pull(host, port, f"~/spool/active/{t['id']}/out/", str(local_out) + "/",
                             ["*/", "ckpt_latest.pt", "ckpt_latest.pt.prev"], run=self.run)
            self.tracker.record(inst["id"], ok)
            if not ok:
                continue
            ckpts = sorted(local_out.rglob("ckpt_latest.pt"), key=lambda p: p.stat().st_mtime)
            if ckpts:
                self.conn.execute("UPDATE tasks SET resume_checkpoint=? WHERE id=?",
                                   (str(ckpts[-1]), t["id"]))
                self.conn.commit()

    # -- invariant 23c: the per-TASK payload pulls, split so they can run one-thread-per-BOX --
    #
    # MEASURED 2026-07-31 (invariant 23b): of a 962 s ingest, `checkpoints` was 559.9 s over 42 calls
    # (13.3 s/call) and `tb` 202.7 s over 47 (4.3 s/call) — together 79% of ingest and 43% of the
    # whole poll cycle. Both are per-RUNNING-TASK, and both are nearly pure I/O, which is what makes
    # them the safe half to parallelise: `tb` writes nothing to the DB at all and `checkpoints`
    # writes one deferrable UPDATE, whereas `worker_state` and `markers` drive state transitions and
    # terminal completions and therefore stay serial.
    #
    # ⚠ THE AXIS IS PER-BOX, NOT PER-TASK, and that is a measurement not a preference. Throughput is
    # ~0.3-0.62 MB/s per box (measured here, and independently in `rsync_push`'s docstring) against a
    # 1 Gb/s home uplink, so the ceiling is each box's own network path: concurrency WITHIN a box
    # contends for one saturated link and buys nothing, while concurrency ACROSS boxes is ~linear
    # because the paths are independent. One worker per box, serial within it.
    #
    # ⚠ `rsync -z` was measured and REJECTED as a lever: 32.8/19.8 s with it vs 21.3/23.4 s without,
    # on identical 9.9 MB pulls — the per-call variance swamps any compression effect, so there is no
    # evidence the box's CPU (where the trainers live) is the constraint. Do not "optimise" it away
    # on intuition; re-measure.
    #
    # The `_*_io` halves below are PURE I/O — no `self.conn`, no `self.tracker`, filesystem and
    # subprocess only — because `registry_db.connect` omits `check_same_thread` and the single
    # connection therefore cannot be touched off-thread at all. Every DB write and every tracker
    # mutation happens in `_apply_box_payloads`, which the poll loop runs serially. Keeping ALL
    # tracker mutation on the serial side is also why no lock is needed on its counters.
    def _running_on(self, instance_id: int) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE instance_id=? AND state='running'", (instance_id,))]

    def _tb_io(self, tasks: list, host: str, port: int) -> list:
        """PURE I/O half of `_pull_tb_events`. Returns [(task_id, ok)]."""
        out = []
        for t in tasks:
            local_out = self._result_dir(t)
            ok = rsync_pull(host, port, f"~/spool/active/{t['id']}/out/", str(local_out) + "/",
                             ["*/", "tb/**"], append=True, run=self.run)
            out.append((t["id"], ok))
        return out

    def _ckpt_io(self, tasks: list, host: str, port: int) -> list:
        """PURE I/O half of `_pull_checkpoints`. Returns [(task_id, ok, newest_ckpt_path|None)].
        The `rglob` stays here: it is filesystem-only, and doing it beside the pull keeps the
        'locate the checkpoint wherever it landed' fix (2026-07-15) in one place."""
        out = []
        for t in tasks:
            local_out = self._result_dir(t)
            ok = rsync_pull(host, port, f"~/spool/active/{t['id']}/out/", str(local_out) + "/",
                             ["*/", "ckpt_latest.pt", "ckpt_latest.pt.prev"], run=self.run)
            newest = None
            if ok:
                ckpts = sorted(local_out.rglob("ckpt_latest.pt"), key=lambda p: p.stat().st_mtime)
                if ckpts:
                    newest = str(ckpts[-1])
            out.append((t["id"], ok, newest))
        return out

    def _pull_box_payloads(self, host: str, port: int, tasks: list, ckpt_due: bool) -> dict:
        """The THREAD BODY — one per box. Pure I/O; never touches the DB or the tracker.
        Times each half so the cycle log can report summed work against wall-clock (= achieved
        parallelism). Never raises: a box that blows up must not take the whole ingest with it."""
        res = {"tb": [], "ckpt": [], "tb_sec": 0.0, "ckpt_sec": 0.0, "error": None}
        try:
            t0 = time.monotonic()
            res["tb"] = self._tb_io(tasks, host, port)
            res["tb_sec"] = time.monotonic() - t0
            if ckpt_due:
                t1 = time.monotonic()
                res["ckpt"] = self._ckpt_io(tasks, host, port)
                res["ckpt_sec"] = time.monotonic() - t1
        except Exception as e:                     # noqa: BLE001 - one box may not fail the phase
            res["error"] = f"{type(e).__name__}: {e}"
        return res

    def _ingest_box_failed(self, instance_id: int, exc: BaseException) -> None:
        """One box's parallel pull raised. Record it the same way a failed payload pull is recorded
        — an event plus a tracker failure — so the existing reapers see the box as unreachable
        instead of silently skipping it with no trace."""
        self.log("ingest_box_failed", f"{type(exc).__name__}: {exc}", instance_id=instance_id)
        self.tracker.record(instance_id, False)

    def _apply_box_payloads(self, instance_id: int, res: dict) -> None:
        """SERIAL half: every DB write and tracker mutation the parallel pulls implied."""
        if res.get("error"):
            self.log("ingest_box_failed", res["error"], instance_id=instance_id)
        for _tid, ok in res["tb"]:
            self.tracker.record(instance_id, ok)
        for tid, ok, newest in res["ckpt"]:
            self.tracker.record(instance_id, ok)
            if ok and newest:
                self.conn.execute("UPDATE tasks SET resume_checkpoint=? WHERE id=?", (newest, tid))
        self.conn.commit()

    def _record_balance(self, balance: float) -> None:
        """Persist an OBSERVED balance to the registry so read-only consumers can see it.

        The balance is otherwise a purely in-memory fact (`_last_known_balance`), reachable only by
        the 4e rent gate — so the one number that decides whether the fleet may rent at all was
        invisible to the dashboard, to `runq`, and to anything that is not this process. Two rows,
        both written ONLY on a reading actually observed from the API (never on the carried-forward
        fallback), because the pair's whole value is that a consumer can tell a CURRENT balance from
        a STALE one: the 2026-08-03 incident was an unreachable API, and a dashboard that reprinted
        the last good figure as if it were fresh would have hidden exactly that. An absent/old
        `..._at` is the signal; there is deliberately no "unknown" sentinel written over the value.

        Not a knob, so deliberately NOT in `DEFAULT_SETTINGS` — this is runtime OBSERVATION parked
        in the same kv table as `pause_i<ID>` / `drain_hold_i<ID>` / `overpack_cap_i<ID>`, and
        nothing reads it back for a decision (the 4e gate still uses the reading it just took).
        Skipped in `--dry-run`, whose contract is no DB writes.
        """
        if self.dry_run:
            return
        for key, value in ((BALANCE_KEY, float(balance)), (BALANCE_AT_KEY, registry_db.now_iso())):
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                              (key, json.dumps(value)))
        self.conn.commit()

    def _place_queue(self):
        instances = self._instances_view()
        queued = [self._task_view(dict(r)) for r in registry_db.list_tasks(self.conn, states=["queued"])]
        settings = dict(self.settings)
        settings["current_rate"] = registry_db.rate(self.conn)
        # Same fail-open defect as reconcile, one call site over: `or {}` made an unreachable API
        # compute a balance of 0.0, which trips the 4e floor gate and refuses ALL rentals — measured
        # 2026-08-03 ("balance: 0.0 <= floor 3.0") while the account was funded and only DNS to
        # vast.ai was down. `None` means UNKNOWN: carry the last good reading forward so a transient
        # blip cannot shut the fleet down, and only ever gate on a value actually observed.
        user = vastai_json("show", "user", run=self.vastai_run)
        if user is None:
            if self._last_known_balance is None:
                self.log("api_unavailable", "vastai show user failed and no prior balance is known "
                                            "— holding rentals until a real reading arrives")
            settings["current_balance"] = (self._last_known_balance
                                           if self._last_known_balance is not None else 0.0)
        else:
            self._last_known_balance = account_balance(user)
            settings["current_balance"] = self._last_known_balance
            self._record_balance(self._last_known_balance)
        settings["deny_machine_ids"] = load_deny_machines()
        pins = self._resolve_colocations(queued, instances)
        self._forced_placed = False
        for i, task in enumerate(queued):
            others = queued[:i] + queued[i + 1:]
            p = place(task, instances, self._offers(), others, settings, time.time())
            if p.action == "rent":
                p = self._abandon_drain_rather_than_rent(task, instances, others, settings) or p
            packed_on = self._apply_placement(task, p, instances)
            # Invariant 4g: a group is pinned by the member that actually CLAIMED a box — never by
            # the placement DECISION, which can lose its CAS. Stamping the pin onto the siblings
            # still ahead of us in this same pass is what stops arm 2 from racing arm 1 onto a
            # different box inside one poll (`_place_queue` mutates the instance view as it goes,
            # so a full box genuinely does divert the next task without this).
            key = colocate_key(task.get("resource_hint"))
            if key is not None and packed_on is not None and key not in pins:
                pins[key] = registry_db.pin_colocation(self.conn, key, packed_on, task["id"])
                self.log("colocate_pinned",
                          f"group {key!r} pinned to box {pins[key]} — every sibling now boards it",
                          task_id=task["id"], instance_id=pins[key])
                self._stamp_colocation(queued[i + 1:], {key: pins[key]})
        # box-pause inv. 20e-b: a forced task was claimed in THIS pass. The capacity-push phase runs
        # BEFORE placement in the cycle, so without this the host would keep the day window's
        # `--cpus` / GPU-power caps until the next poll — after the ship that follows this pass.
        # Content-change driven, so it costs one ssh, to the one box whose payload just changed.
        if self._forced_placed and not self.dry_run:
            self._push_capacity_schedules()

    # ------------------------------------------------------ invariant 4g: sibling co-location
    def _resolve_colocations(self, queued: list, instances: list) -> dict:
        """Turn the recorded group pins into per-task placement facts, ONCE per poll, BEFORE any
        task is placed. Returns `{group key: instance id}` for the still-valid pins.

        Two things are stamped onto the task views (never written back to the row — same rule as
        `_effective_est`/`_effective_hint`: the row is the queue-time contract):

          * `resource_hint["box"]` for a pinned member, which routes it through the box-target
            machinery already proven by invariant 4f — `_boardable` narrows it to that one box,
            4e0 refuses to rent for it (no rental can BECOME the group's box), and it stops
            inflating any other task's backlog bar. Re-implementing those four agreements for a
            second kind of target is exactly the mistake `_boardable`'s docstring records three
            incidents of.
          * `colocate_group_slots`, the group's total lane demand, read by the pack/rent filters.

        Stamping happens before the loop rather than inside it because `others` (the backlog view a
        DIFFERENT task is placed against) is built from these same dicts: a pinned sibling that had
        not been stamped yet would count as ordinary rentable demand and could buy a box nothing in
        its group is allowed to board.

        A pin whose instance is no longer `provisioning`/`live` is DROPPED here — the box was torn
        down, lost, or is draining — and the group re-pins to wherever its next member lands. The
        alternative (hold forever on a dead box) turns every reclaimed rental into a wedged campaign
        that only a human can notice. ⚠ The re-pin is logged loudly because it is precisely the
        moment co-location BREAKS: arms placed before and after it are not comparable, and
        `runq colocate --verify` is what turns that into an answer rather than a silent 2-of-3."""
        pins = {}
        for key, pin in registry_db.colocation_pins(self.conn).items():
            inst = next((i for i in instances if i["id"] == pin["instance_id"]), None)
            if inst is not None and inst["state"] in ("provisioning", "live"):
                pins[key] = pin["instance_id"]
                continue
            registry_db.unpin_colocation(self.conn, key)
            self.log("colocate_unpinned",
                      f"group {key!r} was pinned to box {pin['instance_id']}, which is no longer "
                      f"live (state={(inst or {}).get('state', 'gone')!r}) — the group re-pins to "
                      f"whichever box its next member lands on. ⚠ MEMBERS PLACED BEFORE AND AFTER "
                      f"THIS POINT ARE NOT CO-LOCATED; check `runq colocate --verify`",
                      instance_id=pin["instance_id"])
        group_slots: dict = {}
        for t in queued:
            key = colocate_key(t.get("resource_hint"))
            if key is not None:
                group_slots[key] = group_slots.get(key, 0) + int(t["slots"])
        for t in queued:
            key = colocate_key(t.get("resource_hint"))
            if key is not None:
                t["colocate_group_slots"] = group_slots[key]
        self._stamp_colocation(queued, pins)
        return pins

    @staticmethod
    def _stamp_colocation(tasks: list, pins: dict) -> None:
        """Stamp each pinned group's box onto its members' hints (see `_resolve_colocations`).

        An EXPLICIT `box` is never overwritten — `runq` refuses `--box` together with `--colocate`,
        so the only way both exist is a hand-edited row, and honouring the operator's own choice is
        the safer reading of that."""
        for t in tasks:
            key = colocate_key(t.get("resource_hint"))
            if key in pins and not box_target(t.get("resource_hint")):
                t["resource_hint"] = {**(t.get("resource_hint") or {}), "box": str(pins[key])}

    def _abandon_drain_rather_than_rent(self, task: dict, instances: list, others: list,
                                         settings: dict):
        """Invariant 21i: a drain hold may never CAUSE a rental.

        A drain's whole justification (21) is that the box's load fits on capacity that stays alive
        REGARDLESS. `consolidation_drains` checks that at DECISION time — but a graceful drain takes
        minutes (17b waits for a checkpoint newer than the marker), and by the time the tasks requeue
        the targets can be full. The hold (21h) then removes the one box that could obviously absorb
        them, so placement rents. Destroying a box and renting its replacement in the same minute is
        incoherent, and it is exactly the failure the owner named — penny wise, pound foolish — with
        the pennies now going the WRONG WAY.

        MEASURED 2026-07-29, the second drain under 21h: box 40000015 drained 17:51:39 at
        $0.0496/hr; **a new box was rented 58 seconds later at $0.0523/hr** — dearer than the one
        being reclaimed — and took 4 of the 5 preempted tasks. Without the hold those tasks would
        have gone back to the source: no rental at all. So 21h had traded "repack the same box" for
        "rent another one", which is strictly worse (we pay for the new box AND we paid the preempts).

        A `rent` decision is PROOF the drain's premise was false, so abandon the drain: lift the hold
        and re-place. The box survives and keeps doing useful work; the preempts already spent are
        sunk either way, but nothing further is lost and no money is spent. Prefers the CHEAPEST
        held box, so if several are draining we keep the least costly one alive.
        """
        for inst in sorted((i for i in instances if i.get("drain_held")),
                            key=lambda i: (i.get("dph_usd", 0.0) or 0.0, i["id"])):
            if not _fits_now(task, {**inst, "drain_held": False}, settings):
                continue
            self._clear_drain_hold(inst)
            inst["drain_held"] = False
            self.conn.commit()
            self.log("drain_abandoned",
                      f"would have RENTED while this drained box can hold the work "
                      f"(${inst.get('dph_usd', 0.0):.4f}/hr) — the drain's premise (its load fits "
                      f"elsewhere) is false, so the box stays",
                      task_id=task["id"], instance_id=inst["id"])
            return place(task, instances, self._offers(), others, settings, time.time())
        return None

    def _apply_placement(self, task: dict, p: Placement, instances: list):
        """Returns the instance a `pack` actually CLAIMED (invariant 4g's pin evidence), else None.
        A pack whose CAS lost the race returns None too — the decision is not the claim."""
        if p.action == "hold":
            self.log("hold", p.reason, task_id=task["id"])
            # 5c: a hold FOR a provisioning box (awaiting_provisioning carries its id in p.target)
            # reserves that box's incoming slot in the in-memory view, so a sibling task this same
            # poll doesn't also count it as free and rent a redundant box.
            if p.target is not None:
                for inst in instances:
                    if inst["id"] == p.target:
                        cores, vram = task_footprint(task.get("resource_hint"), task["slots"],
                                                      self.settings)
                        inst.setdefault("occupants", []).append(
                            {"id": task["id"], "slots": task["slots"], "state": "reserved",
                             "est_minutes": task["est_minutes"], "running_minutes_ago": None,
                             "cores": cores, "vram_gb": vram,
                             "ram_gb": self._task_ram(task)})
                        break
            return
        if p.action == "pack":
            r = registry_db.transition(self.conn, task["id"], "claimed", "claim", p.reason,
                                        extra_set={"instance_id": p.target})
            # Keep the in-memory instances view consistent WITHIN this poll (bug fix 2026-07-09):
            # a just-claimed task now occupies a slot, so later place() calls this same poll must
            # see the reduced free capacity. Without this, `instances` is a stale poll-start
            # snapshot and every queued task packs onto the same box (its slots_total is never
            # decremented mid-loop) — over-packing a slots_total=1 box with N tasks.
            if r.ok:
                for inst in instances:
                    if inst["id"] == p.target:
                        # Footprint too (invariant 18a): a task claimed earlier in THIS pass must
                        # consume the box's cores/VRAM budget, not just a slot, or the budget only
                        # binds across polls and one pass can still overshoot it.
                        cores, vram = task_footprint(task.get("resource_hint"), task["slots"],
                                                      self.settings)
                        inst["occupants"].append(
                            {"id": task["id"], "slots": task["slots"], "state": "claimed",
                             "est_minutes": task["est_minutes"], "running_minutes_ago": None,
                             "cores": cores, "vram_gb": vram,
                             "ram_gb": self._task_ram(task)})
                        break
                # Invariant 4i-8: the bypass is AUDITED, and only once the claim actually held —
                # the decision is not the claim. `bypassed` is None for every ordinary pack.
                if p.bypassed is not None:
                    self._forced_placed = True
                    self.log("forced_placement",
                              f"force_box: claimed on instance {p.target} past "
                              f"{len(p.bypassed)} refusing gate(s)"
                              + (": " + " | ".join(p.bypassed) if p.bypassed
                                 else " — no gate would have refused it"),
                              task_id=task["id"], instance_id=p.target)
                return p.target
        elif p.action == "preempt":
            for vid in p.victims:
                v = registry_db.get_task(self.conn, vid)
                r = registry_db.transition(self.conn, vid, "preempting", "preempt_intent",
                                            f"evicted by {task['id']}: {p.reason}")
                if r.ok and v is not None and v["instance_id"] is not None and not self.dry_run:
                    inst = self.conn.execute("SELECT * FROM instances WHERE id=?",
                                              (v["instance_id"],)).fetchone()
                    if inst is not None:
                        host, port = endpoint_for(dict(inst), self.tracker, self.vastai_run)
                        ssh_run(host, port, f"touch ~/spool/active/{vid}/PREEMPT", run=self.run)
            self.log("preempt_wait", p.reason, task_id=task["id"])
        elif p.action == "rent":
            instance_id = self._rent(p.offer, task)
            # 5c: make the just-rented box visible to the REST of this poll as incoming provisioning
            # capacity (one slot reserved for the task that triggered it), so subsequent queued tasks
            # HOLD for it (awaiting_provisioning) instead of each renting their own redundant box —
            # N tasks then consume ceil(N / slots_per_box) boxes, not N.
            if instance_id is not None:
                slots_total = slots_for_offer(p.offer, task.get("resource_hint"), self.settings)
                instances.append({
                    "id": instance_id, "state": "provisioning", "slots_total": slots_total,
                    "minutes_to_hard_cap": self.settings["hard_cap_hours"] * 60,
                    "occupants": [{"id": task["id"], "slots": task["slots"], "state": "reserved",
                                   "est_minutes": task["est_minutes"], "running_minutes_ago": None}],
                    "idle_minutes": 0.0, "dph_usd": p.offer.get("dph_total", 0.0) if p.offer else 0.0,
                    "ssh_host": None, "ssh_port": None})

    def _rent(self, offer: dict, triggering_task: dict):
        self.log("rent_intent", json.dumps(offer), task_id=triggering_task["id"])
        # Invariant 5d: log the offer-set counterfactual for this rent (observability only, fail-open
        # — a failure here must never block the spend that follows).
        try:
            others = [dict(r) for r in registry_db.list_tasks(self.conn, states=["queued"])
                      if dict(r)["id"] != triggering_task["id"]]
            demand = demand_slots(triggering_task, others, self.settings)
            cf = offer_counterfactual(getattr(self, "_last_raw_offers", []) or [], offer,
                                      triggering_task, self.settings, demand)
            self.log("offers_considered", json.dumps(cf), task_id=triggering_task["id"])
        except Exception as e:  # noqa: BLE001 — observability must never abort a rent
            self.log("offers_considered_error", str(e), task_id=triggering_task["id"])
        if self.dry_run:
            return
        slots_total = slots_for_offer(offer, triggering_task.get("resource_hint"), self.settings)
        label = f"runq_{triggering_task['id']}"
        deny_file = MACHINES_DENY_RUNTIME
        created = vastai_json("create", "instance", str(offer["id"]), "--image",
                               offer.get("image", BOX_IMAGE),
                               "--disk", str(offer.get("disk_gb", 40)), "--ssh", "--direct",
                               "--cancel-unavail", "--label", label, run=self.vastai_run)
        instance_id = (created or {}).get("new_contract")
        if not instance_id:
            self.log("rent_failed", f"vastai create instance failed for offer {offer}",
                      task_id=triggering_task["id"])
            return
        hard_cap_at = _iso_plus_hours(self.settings["hard_cap_hours"])
        self.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
            "slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (instance_id, offer.get("machine_id"), label, registry_db.now_iso(), "provisioning",
             offer["dph_total"], offer.get("gpu_name"), slots_total, hard_cap_at))
        self.conn.commit()
        # `slots=` records what `slots_for_offer` ACTUALLY computed for this rent, beside the offer
        # it computed it from. Without it, the only way to audit a box's lane count after the fact is
        # to re-derive the hint — and the hint is partly LEARNED (inv. 26 `lane_footprint`), so a
        # re-derivation reconstructs a different number than the one used and manufactures phantom
        # defects. This is the reference leg for `fleet_util.find_sizing_defects`, which detects the
        # `_adopt` clobber class (9cd11c97) by comparing the stored `slots_total` against THIS value.
        # Invariant 4f: the CPU identity is ALSO emitted as its own flat, parseable tail. It is
        # already inside `{offer}`, but that is a Python dict repr — the whole point of recording it
        # is that a later re-fit of `cpu_speed_weight` can join realised steps/sec against `cpu_name`
        # without writing a repr parser, and the field this ranking rests on should not be reachable
        # only by `ast.literal_eval`. Values are None on an offer that publishes neither.
        self.log("rent_created",
                  f"instance {instance_id} created from offer {offer} slots={slots_total} "
                  f"cpu_ghz={offer.get('cpu_ghz')} cpu_name={offer.get('cpu_name')!r} "
                  f"speed_factor={offer_speed_factor(offer, self.settings):.4f}",
                  instance_id=instance_id)
        # Async provisioning (invariant 5b): do NOT block the poll bringing the box up. The row is
        # `provisioning`; `_advance_provisioning` (each poll) moves it to `live` without blocking, and
        # the over-provisioning guard (invariant 5c) counts it as incoming capacity so we don't rent
        # a redundant box for the same backlog.
        return instance_id

    def _bring_up_worker(self, inst: dict, deny_file: Path) -> bool:
        """A box that reached `actual_status==running`: ssh probe (ONE attempt) + ship bootstrap +
        start `spool_worker.py`. Returns True iff a worker is up afterward — freshly started, OR
        already running (the idempotency guard below). One attempt only — callers retry (across polls
        in `_advance_provisioning`; in a bounded inline loop in `_provision`; in
        `register_owned_box.py`'s own script-level loop for a statically-registered self-owned box,
        which has no poll loop of its own to retry across — owned-box spec, invariant 20e; and once
        per poll in `_reap_unreachable_owned`'s recovery path for an owned box coming back — 20h).

        Idempotency (invariant 20h): the launch is a no-op when a worker is already running against
        this spool. `spool_worker.py` takes no singleton lock, so a second process would race the
        first on claim (`claim_ready`'s check-then-rename) and both re-attach to the same
        `active/<id>` dirs on start — a genuine double-run. The 20h recovery path re-runs bring-up
        unconditionally (it can't distinguish a rebooted laptop, whose worker is gone, from a merely-
        slept one whose worker survived), so we probe for a live worker first and launch only if
        absent. `[s]pool_worker.py` is the standard trick to stop pgrep from matching the very shell
        running it (whose own command line contains the literal pattern) — and the probe shell never
        launches the worker (that's a separate ssh call), so it carries no bare `spool_worker.py` to
        false-match either."""
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        probe = ssh_run(
            host, port,
            "mkdir -p ~/spool/incoming ~/spool/active && "
            "{ pgrep -f '[s]pool_worker.py --spool' >/dev/null 2>&1 "
            "&& echo WORKER_UP || echo WORKER_DOWN; }",
            run=self.run)
        if probe.returncode != 0:
            return False  # sshd/proxy not accepting connections yet — caller retries
        # spool_worker.py imports sweep_supervisor AND bundle as sibling modules (not package
        # imports) — all three must land in the same directory on the box.
        bootstrap_dir = ROOT / "fleet"
        push = [str(bootstrap_dir / "spool_worker.py"), str(bootstrap_dir / "sweep_supervisor.py"),
                str(bootstrap_dir / "bundle.py"), str(bootstrap_dir / "reap_orphans.py")]
        pubkey_arg = ""
        if self.settings["bundle_sign"]:
            pubkey = os.environ.get("DISPATCHER_BUNDLE_PUBKEY")
            if pubkey:
                push.append(str(Path(pubkey).expanduser()))
                pubkey_arg = f" --public-key ~/spool_bin/{Path(pubkey).name}"
        rsync_push(host, port, push, "~/spool_bin/", run=self.run)
        if "WORKER_UP" in (probe.stdout or ""):
            return True  # already running — code on disk refreshed, but never a second process
        # NO LANE COUNT IS PASSED (invariant 8a). How many tasks this box carries is decided by
        # placement, every pass, from the registry. `--max-slots {slots_total}` used to ride on
        # this line, which froze the number into the worker for as long as it ran.
        start = ssh_run(
            host, port,
            f"nohup python3 ~/spool_bin/spool_worker.py --spool ~/spool"
            f"{pubkey_arg} > ~/spool_worker.log 2>&1 & echo started",
            run=self.run)
        return start.returncode == 0

    def _provision(self, instance_id: int, deny_file: Path) -> bool:
        """BLOCKING full provision of ONE box, used by the docker integration test only — production
        provisions incrementally via `_advance_provisioning` (invariant 5b), which never blocks the
        poll. Waits for actual_status=running, then brings up the worker with a bounded probe retry."""
        status = None
        for _ in range(max(1, int(self.settings["provision_boot_max_min"] * 60 / 15))):
            info = vastai_json("show", "instance", str(instance_id), run=self.vastai_run) or {}
            status = info.get("actual_status")
            if status == "running":
                self.conn.execute(
                    "UPDATE instances SET ssh_host=?, ssh_port=? WHERE id=?",
                    (info.get("ssh_host"), info.get("ssh_port"), instance_id))
                self.conn.commit()
                break
            time.sleep(15)
        if status != "running":
            self._blacklist_and_destroy(instance_id, deny_file, "never reached running")
            return False
        inst = dict(self.conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone())
        for attempt in range(max(1, self.settings["ssh_probe_attempts"])):
            if self._bring_up_worker(inst, deny_file):
                return True
            if attempt < self.settings["ssh_probe_attempts"] - 1:
                time.sleep(self.settings["ssh_probe_interval_s"])
        self._blacklist_and_destroy(instance_id, deny_file, "sshd unreachable/spool init failed")
        return False

    def _advance_provisioning(self) -> None:
        """Invariant 5b: move each `provisioning` box forward by ONE non-blocking step per poll, so a
        booting box never blocks the poll loop (ship/reap/rent/teardown). Sub-state is reconstructed
        from the DB (invariant 1 — no in-memory boot state): `ssh_host` NULL ⇒ not yet running (one
        `vastai show`); `ssh_host` set ⇒ running, so try `_bring_up_worker` (probe+bootstrap+start,
        retried across polls). Success ⇒ `live`. Boxes past the boot ceiling are reaped by
        `do_reconcile` (which runs first each poll, invariant 5b timeout reconciliation), so this only
        ever sees boxes still within their boot window."""
        if self.dry_run:
            return
        deny_file = MACHINES_DENY_RUNTIME
        for inst in [dict(r) for r in self.conn.execute(
                "SELECT * FROM instances WHERE state='provisioning'")]:
            if not inst.get("ssh_host"):
                info = vastai_json("show", "instance", str(inst["id"]), run=self.vastai_run) or {}
                if info.get("actual_status") != "running":
                    continue  # still booting — re-check next poll
                self.conn.execute("UPDATE instances SET ssh_host=?, ssh_port=? WHERE id=?",
                                   (info.get("ssh_host"), info.get("ssh_port"), inst["id"]))
                self.conn.commit()
                inst["ssh_host"], inst["ssh_port"] = info.get("ssh_host"), info.get("ssh_port")
            if self._bring_up_worker(inst, deny_file):
                registry_db.log_event(self.conn, "live", f"instance {inst['id']} live",
                                       instance_id=inst["id"])
                self.conn.execute("UPDATE instances SET state='live' WHERE id=?", (inst["id"],))
                self.conn.commit()
            # else: sshd/bootstrap not ready this poll — retry next poll (bounded by reconcile's
            # boot-ceiling reap, so a never-booting box can't be probed forever).

    def _blacklist_and_destroy(self, instance_id: int, deny_file: Path, reason: str,
                               machine_id=None) -> None:
        # `machine_id` is the caller's own record of the host, used in preference to a fresh API
        # read: `show instance` on a box that is already dying can come back empty, and a denial
        # that silently writes nothing is the failure mode this whole path exists to prevent. Falls
        # back to the API when the caller does not know it, so the older call sites are unchanged.
        if machine_id is None:
            info = vastai_json("show", "instance", str(instance_id), run=self.vastai_run) or {}
            machine_id = info.get("machine_id")
        if machine_id:
            deny_file.parent.mkdir(parents=True, exist_ok=True)
            with open(deny_file, "a") as f:
                f.write(f"{machine_id}  # {registry_db.now_iso()} instance {instance_id} {reason}\n")
        vastai_json("destroy", "instance", str(instance_id), "--yes", run=self.vastai_run)
        self.conn.execute("UPDATE instances SET state='destroyed', destroyed_at=? WHERE id=?",
                           (registry_db.now_iso(), instance_id))
        self.log("rent_failed", reason, instance_id=instance_id)
        self.conn.commit()

    def _instances_view(self) -> list:
        out = []
        for r in self.conn.execute("SELECT * FROM instances"):
            inst = dict(r)
            occupants = []
            for t in self.conn.execute(
                    "SELECT * FROM tasks WHERE instance_id=? AND state IN "
                    "('claimed','shipped','running','preempting')", (inst["id"],)):
                occupants.append(self._occupant_view(dict(t)))
            idle_minutes = 0.0 if occupants else self._idle_minutes(inst)
            # Invariant 19h: cap effective slots at the learned sustainable concurrency for this
            # machine, so the packer never re-fills a slot the box's launch gate would just wedge —
            # and then again at the time-of-day capacity window (capacity spec). `_effective_slots`
            # is the one place that composes the two, so the packer and the perf panel can never
            # disagree about how big a box currently is.
            slots_total = self._effective_slots(inst)
            # Invariant 18a: the window's ABSOLUTE cores/VRAM budget, admitted against directly by
            # `_budget_fits` — the bound that stays honest across a heterogeneous hint mix and
            # within a single placement pass (slots_total is snapshotted once per poll).
            resource_cap = self._capacity_budget(inst)
            res = self._box_res.get(inst["id"], {})
            out.append({
                "id": inst["id"], "state": inst["state"], "slots_total": slots_total,
                # The registered slot count BEFORE the 19h cap and the capacity window. Read by
                # nothing that decides a placement — only by invariant 4i's audit line, so a forced
                # placement can say "0 free of 16 nominal" rather than a bare "0 free".
                "slots_nominal": inst["slots_total"],
                "resource_cap": resource_cap,
                "minutes_to_hard_cap": _minutes_until(inst["hard_cap_at"]), "occupants": occupants,
                # Invariant 10d: read from the DB every view, so a restart keeps the quarantine.
                "ship_quarantined": self._ship_quarantined(inst),
                # Invariant 21h: likewise read every view, so the hold survives a restart.
                "drain_held": self._drain_held(inst),
                # R3.2: read every view so a restart cannot resurrect a held box.
                "worker_roll_held": self._worker_roll_held(inst),
                # Invariant 19h-3: likewise — and it self-expires, so a restart cannot extend it.
                "overpack_cooldown": self._overpack_cooldown_active(inst),
                "idle_minutes": idle_minutes, "dph_usd": inst.get("dph_usd", 0.0) or 0.0,
                "ssh_host": inst.get("ssh_host"), "ssh_port": inst.get("ssh_port"),
                "source": inst.get("source", "vast"),
                # ⚠ THE GPU NAME, and it was MISSING — the `label` note below, replayed on a new
                # axis eight lines later and one month on (2026-09-04). Invariant 4h's pack half is
                # `_boardable` -> `instance_has_gpu(inst)` -> `inst.get("gpu_name")`, and this view
                # is what `place()` actually receives: with no `gpu_name` key that read is None for
                # EVERY box, so `_boardable` returned [] for every `requires_gpu` task, on every
                # box, always. The whole of `place()` then degrades in one direction — pack and
                # preempt have no candidates, `_soonest_wait` and the 5c over-provisioning guard
                # iterate an empty list, and `_infeasible_everywhere` (`not any([])`) reports the
                # task as stuck so the backlog bar always qualifies. Every branch that could HOLD
                # was emptied, and the only branch left was RENT. MEASURED: four cells took 9 boxes
                # and boarded none, a later single cell took 3 more, peak $0.64/hr tripped the
                # budget cap — with "no hold reason logged", which is the fingerprint of every hold
                # branch being scoped to an empty `boardable`. A box-TARGETED `requires_gpu` task
                # instead holds FOREVER on a box that exists (`known` is computed against raw
                # `instances`, which still matches), which is how it was finally caught.
                # ⚠ The 2026-09-04 handoff exonerated `requires_gpu` by calling `_boardable` with
                # rows straight out of sqlite — which DO carry `gpu_name`. The predicate was right;
                # the dict shape was wrong. That is the same trap as `label`, and it is why the
                # guard below is now MECHANICAL rather than a hand-listed key.
                "gpu_name": inst.get("gpu_name"),
                # ⚠ THE LABEL, and it was MISSING — which made every label-keyed placement rule
                # INERT IN PRODUCTION while its tests passed (found 2026-08-08 by `--box`).
                # `_boardable` decides a CPU-targeted task's own box with
                # `i.get("label") == f"runq_{task_id}"`, and this view is what `place()` actually
                # receives: with no `label` key that comparison is `None == "runq_…"`, i.e. FALSE
                # forever. So the 2026-08-07 fix for `_boardable` incident 2 — "a targeted task DOES
                # pack onto the box it rented" — could never fire on the live fleet; the task would
                # refuse its own box and fall through to rent again, which is the very rent-loop it
                # was written to stop. Its unit tests hand-build instance dicts that DO carry
                # `label`, so they agreed with a broken implementation. Same class as the `_adopt`
                # `slots_total` bug: a fixture supplying a field the real producer never sends.
                "label": inst.get("label"),
                # Invariant 4b': operator pack preference (higher wins), 0/absent -> inert. Surfaced
                # HERE for the reason the `label` note above documents at length: `place()` sees only
                # this view, so a placement key read off the raw `instances` row (or off `settings`
                # inside `place`, which takes no settings-keyed box data) would be `None` forever in
                # production while hand-built test fixtures supplying the field passed happily.
                "pack_preference": int(
                    self.settings.get(f"box_preference_i{inst['id']}", 0) or 0),
                # Measured GPU memory (invariant 22); None until first sampled -> VRAM gate falls
                # back to slot-count (invariant 21c).
                "vram_total_gb": res.get("vram_total_gb"), "vram_used_gb": res.get("vram_used_gb"),
                # The FULL measurement (invariant 23: cores/load1/RAM/VRAM/gpu_util + `at`), which
                # `box_headroom` turns into real spare capacity. Empty dict -> falsy -> gate abstains.
                "measured": res or None,
            })
        return out

    def _idle_minutes(self, inst: dict) -> float:
        """Derived from persistent state, never an in-memory timer (invariant 1: a dispatcher
        restart must not reset how long an instance has looked idle) — the most recent
        `updated_at` among any task ever assigned here (its last transition, e.g. into `done`,
        is the closest persistent proxy for "when this instance's work last changed"), falling
        back to `created_at` if it was never given a task at all."""
        (last,) = self.conn.execute(
            "SELECT MAX(updated_at) FROM tasks WHERE instance_id=?", (inst["id"],)).fetchone()
        since = last or inst["created_at"]
        return max(0.0, -_minutes_until(since))

    def _occupant_view(self, t: dict) -> dict:
        running_minutes_ago = None
        if t["state"] == "running":
            import datetime
            updated = datetime.datetime.strptime(t["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
            running_minutes_ago = max(0.0, (datetime.datetime.utcnow() - updated).total_seconds() / 60)
        hint = self._effective_hint(t)
        cores, vram = task_footprint(hint, t["slots"], self.settings)
        ram = float((hint or {}).get("ram_per_lane_gb",
                                     self.settings.get("ram_per_lane_gb", 2.0))) * t["slots"]
        return {"id": t["id"], "slots": t["slots"], "state": t["state"],
                "est_minutes": self._effective_est(t), "priority": t["priority"],
                "running_minutes_ago": running_minutes_ago,
                # Invariant 18a: the occupant's REAL declared footprint, so a capacity-budget box
                # admits against summed cores/VRAM rather than a slot count alone.
                "cores": cores, "vram_gb": vram, "ram_gb": ram}

    def _task_ram(self, task: dict) -> float:
        """A task's declared RAM footprint (invariant 23). Hints rarely carry `ram_per_lane_gb`, so
        the settings default stands in — RAM is the axis most likely to be unstated and, on this
        line's ~1 GB/task trainers, the one that actually runs a shared box out of memory."""
        return float((task.get("resource_hint") or {}).get(
            "ram_per_lane_gb", self.settings.get("ram_per_lane_gb", 2.0))) * task["slots"]

    def _task_view(self, t: dict) -> dict:
        return {"id": t["id"], "slots": t["slots"], "est_minutes": self._effective_est(t),
                "priority": t["priority"], "resource_hint": self._effective_hint(t),
                "retries_used": t["retries_used"], "max_retries": t["max_retries"]}

    # ----------------------------------------------------------------- invariant 24: live est loop

    def _effective_est(self, t: dict) -> int:
        """The `est_minutes` every SCHEDULING decision uses: the learned per-group value when this
        task's campaign has enough finished siblings, else the declared one.

        Applied here, at the two view builders, so it reaches every consumer at once — the rented
        window (`_window_minutes_needed`), lane reservations (`_remaining_est`, `_soonest_wait`),
        marginal pack cost (`_pack_cost`), the backlog gate and the preempt/consolidate paths all
        key off the same estimate and would otherwise disagree with each other.

        Deliberately NOT written back to the task row, and NOT used by `_build_task_json`: the row's
        `est_minutes` is part of the queue-time contract (the `--probe` bound and the resume
        contract were both checked against it at `runq add`), and rewriting it would retroactively
        edit a decision the operator already made. This is a scheduling-time correction only."""
        declared = t["est_minutes"]
        # .get(): the view builders are also fed hand-built rows by tests/dry-run paths, and a
        # missing key must degrade to the declared estimate, never crash the poll loop.
        learned = self._learned_estimates().get((t.get("entrypoint"), t.get("grp")))
        return learned if learned else declared

    def _learned_estimates(self) -> dict:
        """Per-(entrypoint, group) learned estimates, rebuilt at most once per `poll_seconds` window.

        Cached because it is consulted once per task per view build (hundreds of times a cycle) but
        changes only as siblings finish. Scoped to groups that still have OPEN tasks — a finished
        campaign can teach us nothing we still need — and to a 14-day lookback, which keeps the scan
        proportional to live work rather than to registry history."""
        now = time.monotonic()
        cached = getattr(self, "_learned_est_cache", None)
        if cached is not None and now - cached[0] < self.settings["poll_seconds"]:
            return cached[1]
        placeholders = ",".join("?" for _ in PENDING_STATES)
        rows = self.conn.execute(
            "SELECT t.entrypoint AS entrypoint, t.grp AS grp, "
            "  (julianday(MAX(CASE WHEN e.event='done' THEN e.t END)) "
            "   - julianday(MIN(CASE WHEN e.event='start' THEN e.t END))) * 1440.0 AS minutes "
            "FROM tasks t JOIN events e ON e.task_id = t.id "
            "WHERE t.state='done' AND e.event IN ('start','done') "
            "  AND t.updated_at >= datetime('now','-14 days') "
            f"  AND t.grp IN (SELECT DISTINCT grp FROM tasks WHERE state IN ({placeholders})) "
            "GROUP BY t.id", tuple(PENDING_STATES)).fetchall()
        learned = est_defaults.learn_group_estimates(
            [(r["entrypoint"], r["grp"], r["minutes"]) for r in rows])
        self._learned_est_cache = (now, learned)
        return learned

    # ------------------------------------------------- invariant 26: live per-lane resource loop

    def _effective_hint(self, t: dict) -> dict | None:
        """The `resource_hint` every SIZING and ADMISSION decision uses (`res_defaults.resolve_hint`):
        the task's DECLARED footprint, with a fleet-wide measurement filling only axes it left
        undeclared and its own (entrypoint, grp) measurement allowed to RAISE it (26-Q1, 26k).

        Applied at the same two view builders as `_effective_est`, and for the same reason — it must
        reach `slots_for_offer` (how big a box we rent), `task_footprint` (18a budget admission) and
        `_headroom_fits` (23) together, or those three disagree about how big the same task is
        (invariant 27: the fleet then buys boxes it refuses).

        Before 26k a fleet-wide average over OTHER workloads overrode the declaration in either
        direction — 14 GB declared re-priced to 2.3 (over-admitted onto a 12 GB card), and on
        2026-09-27 every new pclm group re-priced UP to the 8 GB clamp, stranding it off every owned
        GPU. Declarations now live in tracked configs, so an over-declared one is fixed at its source.

        Like `_effective_est`, deliberately NOT written back to the task row: the row records the
        queue-time contract, and `runq add` validated `--probe` bounds against it."""
        declared = json.loads(t["resource_hint_json"]) if t.get("resource_hint_json") else None
        # .get(): the view builders are also fed hand-built rows by tests/dry-run paths, and a
        # missing key must degrade to the declared hint, never crash the poll loop.
        return res_defaults.resolve_hint(self._learned_footprints(), t.get("entrypoint"),
                                         t.get("grp"), declared)

    def _learned_footprints(self) -> dict:
        """Measured per-lane footprints, rebuilt at most once per `poll_seconds` window.

        Cached for the same reason as `_learned_estimates` — consulted once per task per view build,
        changes only as new samples land (every `resource_measure_every_min`, default 5).

        Reads the `box_measured` event log rather than `self._box_res`, which holds only the LATEST
        sample per live box: a percentile needs the distribution, and the in-memory dict would also
        lose everything on restart (invariant 1 — derive from persistent state, never an in-memory
        accumulator). The 24h window keeps the scan proportional to current fleet behaviour, so a
        workload that got heavier last week stops voting once it stops running."""
        now = time.monotonic()
        cached = getattr(self, "_learned_fp_cache", None)
        if cached is not None and now - cached[0] < self.settings["poll_seconds"]:
            return cached[1]
        samples = []
        for box, t, detail in self.conn.execute(
                "SELECT instance_id, t, detail FROM events WHERE event='box_measured' "
                "AND t >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"):
            if not detail or "{" not in detail:
                continue  # pre-invariant-25 sample: no machine-readable payload to learn from
            try:
                p = json.loads(detail[detail.index("{"):])
                at = calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
            except (TypeError, ValueError):
                continue
            samples.append((box, at, p.get("ep"), p.get("grp"), p.get("running"), p.get("open"),
                            p.get("cpu_used_cores"), p.get("vram_used_gb"), p.get("mem_used_gb")))
        # Invariant 26n: `vram_used_gb` is the WHOLE CARD. A lane is charged only for what the card
        # holds above its box's last idle reading — the display, the owner's own work and a
        # co-tenant are not the fleet's.
        rows = res_defaults.net_idle_vram(samples)
        learned = res_defaults.learn_lane_footprint(rows)
        prev = (cached or (None, {}))[1]
        if learned != prev:
            self.log("lane_footprint",
                     f"learned from {len(rows)} samples: " + json.dumps(
                         {("fleet" if k is None else f"{k[0]}/{k[1]}"): v
                          for k, v in learned.items()}, sort_keys=True, separators=(",", ":")))
        self._learned_fp_cache = (now, learned)
        return learned

    def _search_dph_ceiling(self) -> float:
        """Invariant 4f: max(global cap, every open task's own `resource_hint.max_dph`). Scanned over
        PENDING tasks only, so a finished 24 GB job stops widening the search."""
        ceiling = self.settings["max_instance_dph"]
        placeholders = ",".join("?" for _ in PENDING_STATES)
        for (hint_json,) in self.conn.execute(
                f"SELECT resource_hint_json FROM tasks WHERE state IN ({placeholders}) "
                "AND resource_hint_json IS NOT NULL", tuple(PENDING_STATES)):
            try:
                hint = json.loads(hint_json)
            except (TypeError, ValueError):
                continue
            ceiling = max(ceiling, task_max_dph({"resource_hint": hint}, self.settings))
        return ceiling

    def _offers(self) -> list:
        # Invariant 4f: the SEARCH ceiling is the widest ceiling any OPEN task could rent at, not the
        # global cap — otherwise a task that legitimately raised its own `resource_hint.max_dph`
        # could never see the offers it is entitled to, and `place()`'s per-task filter would choose
        # from a set that silently excluded them. `place()` still applies the per-task cap, so a
        # widened search never widens what an ordinary task may book.
        query = ("num_gpus=1 rentable=True rented=False verified=True disk_space>=20 "
                 f"dph_total<={self._search_dph_ceiling()}")
        # --limit: the Vast CLI default is 64, and `-o dph_total` sorts cheapest-first, so without
        # this the coordinator only ever saw the 64 CHEAPEST (= junk-dominated) boxes and good
        # hardware a few cents dearer was invisible to the ranking (invariant 4e, 2026-07-21).
        limit = str(self.settings.get("offer_search_limit", 1000))
        raw = vastai_json("search", "offers", query, "-o", "dph_total", "--limit", limit,
                          run=self.vastai_run) or []
        # Stash the RAW set so `_rent` can log the offer-set counterfactual (invariant 5d) for the
        # offer it just chose — single-threaded poll, refreshed per task right before its place().
        self._last_raw_offers = raw
        offers, _dropped = eligible_offers(raw, self.settings)
        return offers

    def _build_task_json(self, task: dict) -> dict:
        entry = entrypoints.resolve(task)  # manifest contract if present, else the named table
        resume_from = "resume.pt" if task["resume_checkpoint"] and entry.resume_flag else None
        # Invariant 4i-7: tell the BOX this task is forced, so its launch gate (settle, cpu_load,
        # gpu_util, vram) does not hold what placement deliberately admitted past
        # its own limits. Carried in `env` — an existing key — because the worker REJECTS unknown
        # top-level keys, and every not-yet-updated worker would then reject the task outright.
        # Called from `_ship_prepare`, the SERIAL half of a ship — so reading `self.conn` here is
        # inside the `_*_io` contract (the off-thread push never builds a task.json).
        env = {}
        row = dict(task)               # hand-built rows in tests/dry-run may omit `instance_id`
        inst = (self.conn.execute("SELECT * FROM instances WHERE id=?",
                                  (row["instance_id"],)).fetchone()
                if row.get("instance_id") is not None else None)
        if inst is not None and self._is_forced_on(row, dict(inst)):
            env[FORCE_BOX_ENV] = "1"
        # Invariant 8c: the COORDINATOR decides whether this task uses the GPU — from the same
        # effective hint admission just used — and the box only obeys. Without it the box's own
        # whole-card rules (gpu_util, free VRAM) hold every lane past the first on a card someone
        # else has filled, and the over-pack reaper would learn that hold as a capacity ceiling.
        if lane_vram_gb(self._effective_hint(row), self.settings) <= 0:
            env[NO_GPU_ENV] = "1"
        return {
            # "python" stays literal here -- the box's own interpreter may live somewhere
            # entirely different from this (dispatcher/home) machine's sys.executable; the
            # worker resolves it against ITS OWN sys.executable at launch time.
            "task_id": task["id"], "grp": task["grp"], "name": task["name"],
            "argv": list(entry.argv) + json.loads(task["args_json"]),
            "env": env, "est_minutes": task["est_minutes"], "git_sha": task["git_sha"],
            "pip_extras": list(entry.pip_extras), "resume_from": resume_from,
        }

    def _gc_ship_staging(self) -> None:
        """Reclaim `.ship/<task_id>/` (ship-artifact-build spec inv. 12).

        `_ship_plan` creates one staging dir per ship and NOTHING ever deleted it: measured
        2026-07-31, **2783 dirs / 106 GB**, oldest 2026-07-09, on the coordinator's own disk. Each
        holds a full ~36 MB bundle, so this grows with every ship forever.

        Age-based rather than delete-on-success, deliberately: it is the only form that also
        reclaims the dirs already leaked, and it stays out of the ship state machine entirely (a
        re-ship legitimately re-reads its staging dir, and the ship path is being reworked
        concurrently). The floor is `ship_timeout_min` — a delivery that has not completed within
        the window that declares a box undeliverable will not resume from this copy — with a wide
        safety factor, so nothing in flight is ever touched."""
        root = EXPERIMENTS_ROOT / ".ship"
        if not root.is_dir():
            return
        max_age_s = max(6.0, self.settings.get("ship_timeout_min", 90) / 60.0 * 4) * 3600
        now = time.time()
        removed = bytes_freed = 0
        for d in root.iterdir():
            if not d.is_dir():
                continue
            try:
                if now - d.stat().st_mtime < max_age_s:
                    continue
                size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
            removed += 1
            bytes_freed += size
        if removed:
            self.log("gc_staging", f"reclaimed {removed} ship-staging dir(s), "
                                   f"{bytes_freed / 1e9:.2f} GB (older than {max_age_s / 3600:.0f}h)")

    def _safe_log(self, event: str, detail: str, **kw) -> None:
        """Record an event that must never be able to kill the poll.

        The registry is a single SQLite file shared with every `runq` in every worktree, so a write
        CAN legitimately time out (`database is locked`) — and a failure handler that logs through
        the same connection that just failed turns one observation into an OUTAGE. Measured
        2026-08-01: `_refresh_workers` caught a locked-DB error, tried to log it, the log itself
        raised, and the daemon died mid-poll; the supervisor respawned it, which cleared the
        in-memory refresh throttle, so the next pass retired the same workers again — a crash loop
        built entirely out of error handling. Falls back to the coordinator log, which needs no lock.
        """
        try:
            self.log(event, detail, **kw)
        except Exception as e:                        # noqa: BLE001 — this is the last resort
            print(f"[log-failed] {event}: {detail} ({type(e).__name__}: {e})",
                  file=sys.stderr, flush=True)

    def _box_supports_reattach(self, inst: dict, iid: int) -> bool:
        """Does this box's worker RE-ADOPT its trainers across its own restart (inv 20j-3)?

        Same advert mechanism as `_box_supports_blobs`: the worker writes `~/spool/CAPS` at startup.
        Cached True for the daemon's lifetime (a capability cannot be un-learned) and re-probed on
        False, so a box that upgrades during a natural gap starts taking deliveries immediately
        without a coordinator restart.

        Best-effort: any probe failure is False, which yields the conservative hold."""
        if self._box_reattach_caps.get(iid):
            return True
        try:
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            r = ssh_run(host, port, "cat ~/spool/CAPS 2>/dev/null || true", run=self.run)
        except Exception:                      # noqa: BLE001 — never break a poll on a probe
            return False
        ok = "reattach" in (r.stdout or "")
        if ok:
            self._box_reattach_caps[iid] = True
        return ok

    def _note_worker_refresh_hold(self, iid: int, occupied: int) -> None:
        """Log a 20j-1 delivery hold WHEN IT STARTS AND WHEN IT CLEARS, never per pass.

        Edge-triggered for the same reason `_note_gate` is (invariant 19h-4): `_refresh_workers`
        runs every poll, so logging unconditionally floods `coordinator.log`, while logging nothing
        turns "this box stopped updating" into a silent state — which is just a quieter version of
        the failure being fixed. A box held for days must be READABLE as held."""
        held = bool(occupied)
        if held == self._worker_refresh_held.get(iid, False):
            return
        self._worker_refresh_held[iid] = held
        if held:
            self._safe_log("worker_refresh_deferred",
                            f"{occupied} task(s) on this box; worker code delivery deferred until it "
                            f"drains (delivering would re-exec the worker under running work)",
                            instance_id=iid)
        else:
            self._safe_log("worker_refresh_resumed", "box drained; worker code delivery resumed",
                            instance_id=iid)

    def _refresh_workers(self) -> None:
        """Keep every live box's worker CODE current (invariant 20i), WITHOUT restarting live work
        (invariant 20j-1).

        `_bring_up_worker` already rsyncs the four bootstrap files on every call and starts a worker
        only when none is running — so calling it against a healthy box is exactly "refresh the code
        on disk, touch nothing else". It was simply never called for a box already live, which is
        why an owned box could run months-old worker code and a rental only picked up a change when
        it was replaced. The worker itself notices the new bytes and re-execs at a loop boundary
        (`spool_worker._source_fingerprint`), so the coordinator never has to choose a safe moment
        it cannot see, and nothing has to relaunch anything.

        Throttled per box: this is 1 ssh + a ~100 KB rsync, worth doing every
        `worker_refresh_min` and not every poll. Owned boxes get it too — they are the ones that
        never churn, and therefore the ones that needed it."""
        interval = self.settings.get("worker_refresh_min", 30) * 60
        now = time.time()
        for row in self.conn.execute("SELECT * FROM instances WHERE state='live'"):
            inst = dict(row)
            iid = inst["id"]
            if now - self._last_worker_refresh.get(iid, 0.0) < interval:
                continue
            # ⛔ INVARIANT 20j-1 — NEVER PUSH WORKER BYTES TO AN OCCUPIED BOX.
            #
            # Delivery is what moves the fingerprint, and the fingerprint is what makes a worker
            # re-exec. A re-exec under running work re-adopts `active/<id>` and relaunches the
            # trainer — which either restarts it COLD (CheckpointRegression) or, since the
            # self-resume fix, DOUBLE-RUNS it silently beside the original (the first survives:
            # trainers run under `start_new_session`). So the delivery, not the re-exec, is the
            # event to gate.
            #
            # MEASURED 2026-08-02, twice in ninety minutes. `c3f23565` (19:28, an observability
            # commit) → five boxes re-exec'd 19:30:31-49 → six tasks across five campaigns dead in
            # eight minutes. Then `8d535aa7` — THE FIX FOR THAT VERY BUG — delivered at 20:00:51 and
            # killed THIRTEEN MORE on its way in, because a box-side guard cannot protect a box that
            # has not yet received it. That is the structural point: the worker half can never cover
            # its own rollout, so the gate has to live HERE, where it applies to every box on every
            # worker generation, including one running months-old code.
            #
            # Deferring costs only LATENCY: the box is refreshed on a later pass once it drains, and
            # `_last_worker_refresh` is deliberately NOT stamped, so this re-fires every pass instead
            # of backing off for another `worker_refresh_min`.
            #
            # ⚠ This is the DRAIN half of a rolling upgrade and it is only half. Nothing here stops
            # the placer topping the box back up, so a busy box can stay stale indefinitely — see
            # `docs/specs/worker-rolling-upgrade.spec.md` R3/R4 for the hold + roll width that make
            # deferral terminate. Landed alone first because it stops the bleeding on its own and
            # needs no owner decisions.
            (occupied,) = self.conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE instance_id=? "
                "AND state IN ('claimed','shipped','running')", (iid,)).fetchone()
            # ⛔ INVARIANT 20j-3 — the hold is only needed for a worker that CANNOT survive its own
            # restart. A worker advertising `reattach` (CAPS) re-adopts its live trainers on the way
            # back up instead of relaunching them, so delivering to it while it is busy is a no-op
            # for the running work — and delivering is the ONLY way a permanently-busy box ever
            # updates. Without this, 20j-1 is a deadlock dressed as safety: the box is held because
            # it is occupied, and it is occupied because the placer keeps it that way.
            #
            # Fail-safe by construction: a box that does not advertise the capability — an older
            # worker, or one we cannot probe — takes the conservative hold, i.e. exactly the
            # pre-20j-3 behaviour. The capability can only ever WIDEN what is allowed.
            if occupied and not self._box_supports_reattach(inst, iid):
                # R3/R4: deferral must TERMINATE. Admit this box to the roll so the placer stops
                # topping it up and it can actually drain — otherwise R1 is a deadlock (R3.3).
                self._consider_worker_roll(inst, iid, occupied)
                self._note_worker_refresh_hold(iid, occupied)
                continue
            self._note_worker_refresh_hold(iid, 0)
            self._last_worker_refresh[iid] = now      # stamp BEFORE, so a failure still backs off
            try:
                self._retire_pre_20i_worker(inst)
                self._bring_up_worker(inst, MACHINES_DENY)
                # R5.7: the box is empty and has just been delivered to. If it is now running the
                # code we ship, its roll is DONE — clear the hold so it rejoins the pool (or the
                # existing idle path reaps it, R4.3; this spec adds no teardown rule of its own).
                # Verified from WORKER_VERSION rather than assumed from "we rsynced": the re-exec
                # happens at the worker's next loop boundary, so it may take another pass, and a
                # hold cleared early would put a still-stale box back in the pool.
                if self._worker_roll_held(inst):
                    if self._box_worker_version(inst) == _source_fingerprint():
                        self._clear_worker_roll(inst)
                        self.log("worker_roll_upgraded",
                                  "empty, delivered and now RUNNING current worker code — "
                                  "hold cleared, box rejoins the placement pool",
                                  instance_id=iid)
            except Exception as e:                    # noqa: BLE001 — never break a poll
                self._safe_log("worker_refresh_failed", f"{type(e).__name__}: {e}",
                                instance_id=iid)

    def _consider_worker_roll(self, inst: dict, iid: int, occupied: int) -> None:
        """R4 — admit ONE box at a time to the roll, fewest occupants first.

        Called only for a box the delivery gate just deferred, i.e. one that is occupied AND cannot
        survive its own restart. Admitting it stops the placer refilling it (R3), so it drains and
        becomes deliverable; without this, R1 holds the fleet stale forever (R3.3).

        Already held ⇒ nothing to do (the hold is its own expiry clock, R6.2). Otherwise admit only
        if the roll has a free slot: **every box goes stale simultaneously** the instant a worker
        change lands, so an uncapped hold would park the WHOLE fleet and trigger a rent stampede —
        strictly worse than the bug (R4.1).

        ⚠ **No owned-box carve-out** (owner, 2026-08-03, open question 2: *"doesn't matter because
        (1)"*). At width 1 only one box is ever held, so an owned box in the same lane costs at most
        one box of capacity and can never hold both owned boxes at once — which is the entire reason
        a separate lane was proposed.

        Fail-safe: any probe failure leaves the box unadmitted and simply working on old code."""
        if self._worker_roll_held(inst):
            return
        draining = self._worker_roll_draining_ids()
        if iid in draining:
            return
        if len(draining) >= self._worker_roll_max_draining():
            return                                   # someone else has the slot; wait our turn
        stale_from = self._box_worker_version(inst)
        if stale_from is None:
            return                                   # R6.1: unreadable/absent version is NOT stale
        if stale_from == _source_fingerprint():
            return                                   # current — the defer is for some other reason
        # R4.2: fewest occupants first, ties by id. Deterministic, and it finishes the roll soonest.
        contenders = sorted(
            ((self._box_occupancy(i["id"]), i["id"]) for i in self._roll_candidates()),
            key=lambda t: (t[0], t[1]))
        if contenders and contenders[0][1] != iid:
            return                                   # a shorter drain is ahead of us in the queue
        self._admit_to_worker_roll(inst, stale_from, occupied)
        # Open question 3 (owner: yes). Checked ONLY here, at the moment a human-set one-shot could
        # apply — never on a schedule, never as a fallback, never retried.
        if self._worker_roll_urgent():
            self._roll_now_evict(inst)

    def _worker_roll_urgent(self) -> bool:
        """Open question 3, ANSWERED YES (owner, 2026-08-03): an operator escape hatch for when the
        worker change is ITSELF the correctness fix and waiting a day is wrong.

        **Operator-only, and structurally so.** It is a one-shot `settings` row that a human writes
        (`fleet/roll_now.py`) and that this code CONSUMES AND DELETES the moment it acts —
        so there is no automatic path that can set it, and no way for it to stay on and quietly turn
        every future roll into a preempting one. Same rule as `--resume-unverified` in the
        resume-integrity spec: the hatch exists, it is never reachable by machinery, and it does not
        persist.

        Read LIVE from the DB for the same reason as the roll width: an urgent rollout that needs a
        coordinator restart to take effect is not an urgent rollout."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='worker_roll_now'").fetchone()
        if row is None:
            return False
        try:
            on = bool(json.loads(row["value"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            on = False
        self.conn.execute("DELETE FROM settings WHERE key='worker_roll_now'")   # ONE SHOT
        self.conn.commit()
        return on

    def _roll_now_evict(self, inst: dict) -> None:
        """The urgent path: checkpoint-and-requeue this box's occupants so it empties in ~minutes
        instead of hours. Uses the SHARED graceful primitive (`_evict_task_graceful`) — the trainer
        checkpoints then exits at its next save and requeues with resume, so no in-flight work is
        interrupted before a checkpoint and nothing is lost. It is a delay, not a kill.

        ⚠ This is the ONLY path in this feature that touches an occupant, and it exists solely
        because a human asked for it in this poll."""
        occ = [dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE instance_id=? AND state='running'", (inst["id"],))]
        if not occ:
            return
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        for t in occ:
            self._evict_task_graceful(host, port, t,
                                       "worker_roll_now: operator-requested urgent worker rollout "
                                       "(checkpoint + requeue, not a kill)")
        self.log("worker_roll_now",
                  f"OPERATOR-REQUESTED urgent rollout: gracefully evicting {len(occ)} occupant(s) "
                  "so this box can be upgraded immediately",
                  instance_id=inst["id"])

    def _worker_roll_draining_ids(self) -> set:
        """Instance ids currently holding for an upgrade. Read from the DB every call (R3.2) so a
        restart cannot resurrect a held box into the pool mid-roll."""
        out = set()
        for (key,) in self.conn.execute(
                "SELECT key FROM settings WHERE key LIKE 'worker_roll_i%'"):
            try:
                iid = int(key.split("worker_roll_i", 1)[1])
            except (IndexError, ValueError):
                continue
            row = self.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            if row is not None and self._worker_roll_held(dict(row)):
                out.add(iid)
        return out

    def _box_occupancy(self, iid: int) -> int:
        (n,) = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE instance_id=? "
            "AND state IN ('claimed','shipped','running')", (iid,)).fetchone()
        return n

    def _roll_candidates(self) -> list:
        """Live boxes that are occupied and cannot survive a restart — the population the roll is
        ordering. A `reattach`-capable box is excluded: it takes its upgrade in place (20j-3) and
        never needs to be held at all."""
        out = []
        for row in self.conn.execute("SELECT * FROM instances WHERE state='live'"):
            inst = dict(row)
            if not self._box_occupancy(inst["id"]):
                continue
            if self._box_supports_reattach(inst, inst["id"]):
                continue
            out.append(inst)
        return out

    def _box_worker_version(self, inst: dict) -> str | None:
        """R2 — the fingerprint the box is RUNNING, read from `~/spool/WORKER_VERSION` CONTENT.

        Content, never mtime (R2.1): an mtime records when a delivery happened and cannot tell
        "delivered and adopted" from "delivered and deferred". The worker writes this file with the
        fingerprint it LOADED, so it is the only signal meaning "this is what is executing".
        None on any failure — unreadable means NOT STALE (R6.1), never hold a box on a bad read."""
        try:
            host, port = endpoint_for(inst, self.tracker, self.vastai_run)
            r = ssh_run(host, port, "cat ~/spool/WORKER_VERSION 2>/dev/null || true", run=self.run)
        except Exception:                            # noqa: BLE001 — never break a poll on a probe
            return None
        v = (getattr(r, "stdout", "") or "").strip()
        return v or None

    def _retire_pre_20i_worker(self, inst: dict) -> None:
        """BOOTSTRAP, once per box, ever: stop a worker that cannot update itself.

        Self-update (20i) lives in the worker, so a worker STARTED BEFORE 20i has no re-exec loop
        and will run its boot-time code forever no matter how fresh the files on disk are. That is
        exactly the state every live box was in when 20i landed — delivery worked and nothing
        happened. Such a worker is identified by the ABSENCE of `~/spool/WORKER_VERSION`, which only
        a 20i worker writes, so this can fire at most once per box and never again.

        Stopping it is safe only when the box is IDLE. Two distinct hazards:

        1. **Mid-delivery** (`claimed`/`shipped`) — a worker killed mid-`_unpack` leaves a partial
           `repo/`, and `validate_and_prepare` treats an existing `repo/` as "already extracted" on
           the way back up, so it would run a truncated tree.
        2. **⚠ `running` — MEASURED 2026-08-01, and this gate originally EXCLUDED it.** The original
           rationale was "running occupants are not at risk (they are separate sessions and
           survive)". Not true in practice: retiring workers under running tasks destroyed **12 cells
           across 8 campaigns in about an hour** (`m60_bias_init_n6`, `m60_present_curriculum` x2,
           `m60_primed_attention`, `bus_knob_n3` x3, `m49_dream_retrieval` x2, `whiten_off_scout`,
           `m61_vocab` x2). Every one died with `shared.infra.checkpoint.CheckpointRegression` —
           the task's TRAINING restarted from the first stage/seed (`sched.stage_index 3 -> 0`,
           `seed_index 2 -> 0`) and the checkpoint guard refused to write progress backwards.
           There was **no `preempt` and no `requeue` event**: the task sat in `running` the whole
           time while the work underneath it restarted, so nothing in the task state machine noticed.
           ⚠ That guard is the only reason this was visible at all — without it each run would have
           completed and emitted a plausible, complete `results.json` for a curriculum that silently
           restarted at stage 0, i.e. the m49/m50/m54 silent-restart class again.

        (The precise mechanism is not fully isolated — whether the relaunched worker re-launches the
        task cold, or the `pkill` reaches the trainer — but the gate is wrong under either, because
        both follow from REPLACING A WORKER THAT OWNS RUNNING WORK. The conservative gate costs only
        latency: a box is retired once it drains, and the probe is idempotent and cheap, so the
        rollout still completes.)

        The caller then re-runs `_bring_up_worker`, whose probe now finds no worker and launches the
        current code."""
        (occupied,) = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE instance_id=? AND state IN ('claimed','shipped','running')",
            (inst["id"],)).fetchone()
        if occupied:
            return
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        probe = ssh_run(host, port,
                         "test -f ~/spool/WORKER_VERSION && echo SELFUPDATING || echo LEGACY",
                         run=self.run)
        if "LEGACY" not in (probe.stdout or ""):
            return                                    # already a 20i worker, or unreachable
        ssh_run(host, port, "pkill -f '[s]pool_worker.py --spool' || true", run=self.run)
        self._safe_log("worker_retired",
                  "stopped a pre-20i worker that cannot self-update; relaunching on current code",
                  instance_id=inst["id"])

    def _gc_pull_temps(self) -> None:
        """Reap orphaned rsync pull temps under `experiments/` (invariant 7g).

        Two populations, deliberately aged differently:
        - **Legacy `.name.<6 rand>` orphans** — pure garbage. Nothing will ever resume one (the
          random suffix is regenerated per invocation), and they cannot be loaded: they are
          incomplete copies of a file that exists whole on the box. 210 GB of these existed when
          7g landed. Removed once past `_PULL_TEMP_MIN_AGE_H`, which exists only so an IN-FLIGHT
          transfer — one rsync is typically mid-write at any moment — is never pulled out from
          under itself.
        - **`.rsync-partial/` contents** — these are LOAD-BEARING while their task still runs, since
          the whole point of 7g is that the next attempt resumes from them. Aged far more
          generously (`_PARTIAL_MAX_AGE_H`): a partial older than that belongs to a task that has
          long since finished or died, so nothing will ever resume it either.

        Guarded by BOTH the name pattern and mode 0600 — verified against the live tree to match
        rsync temps exactly and nothing else. A file failing either check is left alone."""
        root = EXPERIMENTS_ROOT
        if not root.is_dir():
            return
        now = time.time()
        n_tmp = n_part = 0
        b_tmp = b_part = 0.0
        for dirpath, dirnames, filenames in os.walk(root):
            if ".dispatcher" in Path(dirpath).parts:
                dirnames[:] = []
                continue
            in_partial = Path(dirpath).name == PARTIAL_DIR
            max_age = (_PARTIAL_MAX_AGE_H if in_partial else _PULL_TEMP_MIN_AGE_H) * 3600
            for name in filenames:
                if not in_partial and not _RSYNC_TEMP_RE.match(name):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                    if stat.S_IMODE(st.st_mode) != 0o600 or now - st.st_mtime < max_age:
                        continue
                    size = st.st_size
                    p.unlink()
                except OSError:
                    continue
                if in_partial:
                    n_part += 1
                    b_part += size
                else:
                    n_tmp += 1
                    b_tmp += size
        if n_tmp or n_part:
            self.log("gc_pull_temps",
                      f"reclaimed {n_tmp} orphaned rsync temp(s) {b_tmp / 1e9:.2f} GB + "
                      f"{n_part} stale partial(s) {b_part / 1e9:.2f} GB")

    def _gc_code_snapshots(self) -> None:
        """Reap `code_snapshot` tars whose task is finished (ship-artifact-build spec inv. 12a).

        `runq add` persists one working-tree tar per task and NOTHING ever deleted it: measured
        2026-07-31, **2707 files / 55.5 GB**, of which 2686 (55.0 GB) belonged to tasks in a
        terminal state and can never be read again — `_source_code_tar` only ever loads a snapshot
        to SHIP it. Since the queuer now builds the ship-ready blob, a snapshot is not even the
        shipping path for a new task; it is provenance.

        REFCOUNTED, like the blob store: a snapshot whose task is still open is retained no matter
        how old, because that is the copy the ship path will read. Terminal ones are kept for
        `_SNAPSHOT_KEEP_H` so a just-failed task can still be inspected, then dropped."""
        snaps = code_snapshot.snapshots_dir(EXPERIMENTS_ROOT)
        if not snaps.is_dir():
            return
        open_ids = {r[0] for r in self.conn.execute(
            "SELECT id FROM tasks WHERE state NOT IN "
            "('done','cancelled','task_failed','infra_failed')")}
        now = time.time()
        n = 0
        freed = 0.0
        for p in snaps.glob("*.tar.gz"):
            if p.name[: -len(".tar.gz")] in open_ids:
                continue                       # still shippable — never touch it
            try:
                st = p.stat()
                if now - st.st_mtime < _SNAPSHOT_KEEP_H * 3600:
                    continue
                size = st.st_size
                p.unlink()
            except OSError:
                continue
            n += 1
            freed += size
        if n:
            self.log("gc_snapshots",
                      f"reclaimed {n} code snapshot(s) of finished tasks, {freed / 1e9:.2f} GB")

    def _build_code_tar(self, task: dict) -> tuple[bytes | None, str]:
        """The ship-ready artifact the QUEUER built (ship-artifact-build spec inv. 1).

        THE COORDINATOR DOES NOT BUILD. It reads a blob and pushes it — it holds no Cython, no ABI,
        no build venv, no compile cache and no notion of what compilation even is. Everything that
        did (`_source_code_tar`, `_compiled_tree`, `_shipped_tree`, `_log_compile`,
        `_prune_cache_dir`, `_build_venv_python`, and their two cache-key helpers) was deleted
        2026-07-31 once `backfill_ship_blobs.py` had given every open pre-cutover task a blob.

        VALIDATE AT THE TRUST BOUNDARY (inv. 6): these are bytes this process did not produce, so
        the digest recorded at queue time is re-checked before anything ships. A mismatch, or a
        blob that is gone, FAILS the task loudly and names `created_by` (inv. 15) — with no
        toolchain and no source there is nothing to rebuild from, so the only useful thing the
        coordinator can do is say who can. It never silently re-queues and never ships source."""
        bid = task.get("code_blob")
        who = task.get("created_by") or "unknown"
        if not bid:
            reason = (f"task carries no ship artifact (pre-cutover row) — the coordinator no longer "
                      f"builds. Re-queue it: owner={who}")
            registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)
            self._alert(f"no blob [{task['grp']}/{task['name']}]: {reason}")
            return None, "compiled"
        data = artifact_store.load(EXPERIMENTS_ROOT, bid)
        if data is None:
            reason = (f"ship artifact {bid} is MISSING from the store — the coordinator cannot "
                      f"rebuild it (no toolchain, no source). Re-queue: owner={who}")
            registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)
            self._alert(f"blob missing [{task['grp']}/{task['name']}]: {reason}")
            return None, "compiled"
        got, want = artifact_store.digest(data), task.get("code_sha256")
        if want and got != want:
            reason = (f"ship artifact {bid} FAILED its integrity check (sha256 {got[:12]} != "
                      f"recorded {str(want)[:12]}) — refusing to ship. Re-queue: owner={who}")
            registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)
            self._alert(f"blob corrupt [{task['grp']}/{task['name']}]: {reason}")
            return None, "compiled"
        return data, (task.get("code_format") or "compiled")

    def _ship(self, task: dict, inst: dict) -> bool:
        """One task delivered start to finish, SERIALLY — prepare, push, apply.

        Kept whole (rather than dissolved into the three halves it now delegates to) for two
        reasons: it is the unit under test for invariants 7/7b, and `_ship_all` still calls it
        verbatim whenever the fan-out width collapses to 1 — a single box holding work, or
        `ship_parallel_boxes = 1`. The parallel path calls the same three halves in the same
        order; the only difference is which thread the middle one runs on."""
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        plan = self._ship_prepare(task, inst, host, port)
        if plan is None:
            return False                       # payload could not be built (already logged)
        res = self._ship_io(plan, host, port)
        self._apply_ship_io(task, inst, res)
        return res["ok"]

    def _box_supports_blobs(self, inst: dict, host: str, port: int) -> bool:
        """Does this box's worker understand a `code_ref` bundle (inv. 11)?

        The worker advertises it by writing `~/spool/CAPS` at startup. A box still running a
        pre-inv-11 worker has no CAPS file and keeps getting self-contained v1 bundles — which is
        the whole reason `BUNDLE_VERSION` bumps only for the reference form: a worker rejects any
        version it does not know, so an unconditional bump would break every live box at once.
        Rented boxes converge within `hard_cap_hours`; an owned box needs its worker restarted once.

        A True is cached for the daemon's lifetime (a capability cannot be un-learned); a False is
        re-probed, so a box whose worker is redeployed picks it up without a coordinator restart."""
        if self._box_caps.get(inst["id"]):
            return True
        r = ssh_run(host, port, "cat ~/spool/CAPS 2>/dev/null || true", run=self.run)
        ok = "blobref" in (r.stdout or "")
        if ok:
            self._box_caps[inst["id"]] = True
        return ok

    def _ship_prepare(self, task: dict, inst: dict | None = None,
                      host: str | None = None, port: int | None = None) -> dict | None:
        """SERIAL half: everything touching `self.conn`, the compile caches, or the staging area.
        Returns the plan `_ship_io` executes, None if the payload could not be built (already
        logged). Never raises a compile error: the queuer builds, so a code bug the compiler
        rejects blocks `runq add` (exit 5) and this task never existed.

        ⚠ The BUILD moved AHEAD of the existence check and the apt step (it used to sit between
        them). It has to: the I/O half runs off-thread and must be pure, so every byte it pushes
        has to exist before it starts. Two paths therefore do work they used to skip — an
        already-delivered task (invariant 7's restart idempotency) and a task whose apt step fails
        now both build a bundle that is never sent. Neither changes an OUTCOME, and both are cheap
        now that the overlaid tree is cached: a ~0.02s read rather than the 11.55s re-gzip it would
        have been before. Do not "restore" the old order to save that — it re-couples the push to
        the DB and un-does the fan-out."""
        (n_prior_ships,) = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id=? AND event='ship'", (task["id"],)).fetchone()
        entry = entrypoints.resolve(task)  # manifest contract if present, else the named table
        staging = EXPERIMENTS_ROOT / ".ship" / task["id"]
        staging.mkdir(parents=True, exist_ok=True)
        code_tar, code_format = self._build_code_tar(task)
        if code_tar is None:
            return None  # git archive failed (already logged)
        task_json = self._build_task_json(task)
        resume = None
        if task_json["resume_from"] and task["resume_checkpoint"]:
            try:
                resume = Path(task["resume_checkpoint"]).read_bytes()
            except OSError as e:
                # ⛔⛔ ONE UNREADABLE PATH MUST NOT KILL THE FLEET. This read used to be bare, and an
                # OSError propagated out of `_ship` -> `_ship_all` -> `poll_once` -> `main`, exiting
                # the process. The container restarts, re-claims the SAME task, reads the SAME
                # missing file and dies again: a crash loop that stops ALL dispatch for everyone,
                # with the cause visible only in a traceback nobody is watching. Measured
                # 2026-09-20: one probe queued with `--init-from` naming a path on the QUEUER's
                # filesystem took the coordinator down for ~40 minutes; two other sessions' tasks
                # sat undispatched.
                # ⚠ `runq add` checks the path on the QUEUER, which is a different filesystem from
                # the dispatcher's -- so a path that passes at queue time can still be absent here.
                # That gap is why this has to be handled rather than asserted away.
                # ⇒ fail THIS task, terminally (`claimed -> task_failed` exists for exactly this:
                # "failed fast + terminal here, before it ever reaches a box"), and keep polling.
                reason = ("resume checkpoint unreadable by the dispatcher: %s (%s). `--init-from` is "
                          "resolved on the QUEUER's filesystem; the dispatcher re-reads it and does "
                          "not share that mount. Ship the checkpoint with the task instead."
                          % (task["resume_checkpoint"], e))
                self._alert("%s/%s: %s" % (task["grp"], task["name"], reason))
                registry_db.transition(self.conn, task["id"], "task_failed", "task_failed", reason)
                return None
        sign_key = os.environ.get("DISPATCHER_BUNDLE_SIGN_KEY") if self.settings["bundle_sign"] else None
        bundle_path = staging / bundle.BUNDLE_NAME
        # inv. 11: reference the box's shared copy instead of embedding a second one. 55% of
        # placements were re-sending a tree the box already held (worst case 16 copies of one 36 MB
        # tree to a single box), because a bundle is built per TASK while the code is per SNAPSHOT.
        code_ref = None
        blob_id = task.get("code_blob")
        if blob_id and inst is not None and host and self._box_supports_blobs(inst, host, port):
            code_ref = blob_id
        bundle.build_bundle(bundle_path, code_tar=code_tar, task_json=task_json,
                            git_sha=task["git_sha"], task_id=task["id"], resume=resume,
                            code_format=code_format, sign_key=sign_key, code_ref=code_ref)
        ready = staging / "READY"
        ready.write_text("")
        return {
            "task_id": task["id"],
            "reship": n_prior_ships > 0,
            # System deps (retrospective bug 8): dispatcher-side apt at ship time — task.json's
            # schema and already-deployed workers stay untouched. dpkg guard makes re-ships a
            # no-op; generous timeout because the fallback path runs apt-get update first.
            "apt_pkgs": " ".join(entry.apt_packages) if entry.apt_packages else "",
            "bundle_path": str(bundle_path),
            "ready_path": str(ready),
            "remote_dir": f"~/spool/incoming/{task['id']}/",
            # Present only for a code_ref bundle: the local blob to place on the box IF ABSENT.
            # `_ship_io` checks first, so N cells of one sweep cost one transfer, not N.
            "blob_id": code_ref,
            "blob_path": str(artifact_store.blob_path(EXPERIMENTS_ROOT, code_ref)) if code_ref else None,
            "blob_dir": f"~/spool/{bundle.BLOBS_DIRNAME}/",
        }

    def _ship_io(self, plan: dict, host: str, port: int) -> dict:
        """PURE I/O half — no `self.conn`, no `self.tracker`, subprocess only. This is the same
        contract the `_*_io` ingest halves keep, and for the same reason: `registry_db.connect`
        omits `check_same_thread`, so the single connection cannot be touched off-thread at all,
        and keeping every tracker mutation on the serial side is why its counters need no lock.

        Returns `{ok, already, ops, fail}`. `ops` is the ORDERED reachability outcome of each
        ssh/rsync op as `(ok, announce)` — replayed into the tracker by `_apply_ship_io` so the
        proxy->direct switch fires exactly where it did when this ran inline. `announce` is False
        for the final READY push, which historically recorded without logging `ssh_fallback`."""
        tid = plan["task_id"]
        ops: list[tuple[bool, bool]] = []
        if plan.get("blob_id"):
            # inv. 11: the code tree is per-SNAPSHOT, the bundle is per-TASK. Check before pushing
            # so N cells of one sweep cost ONE transfer to this box instead of N — the whole point.
            # A failed check is treated as "absent": re-pushing costs bytes, skipping a genuinely
            # missing blob costs a task that cannot start.
            remote_blob = f"~/spool/{bundle.BLOBS_DIRNAME}/{plan['blob_id']}.tar.gz"
            # ⛔⛔ PROBE THE SIZE, NOT MERE EXISTENCE — `test -f` HERE POISONED BOXES PERMANENTLY.
            # `rsync_push` runs `--partial --inplace` ON PURPOSE (inv. 7, resumability): an
            # interrupted push deliberately LEAVES the truncated destination so the next attempt
            # sends only the missing tail. Guarding that push with an EXISTENCE test made the two
            # mechanisms cancel out: `--partial` guarantees a truncated file will exist, and
            # `test -f` then reports PRESENT forever, so the resume it counts on NEVER RUNS.
            # `unpack_bundle` duly rejects the short blob on integrity — and does not evict it — so
            # every task routed to that box dies at validation, for every session, until a human
            # notices. Measured 2026-08-30 on instance 40000055: four blobs cached at 5.5-6.2 MB
            # against a real 43.8 MB, each returning the SAME wrong sha256 on every attempt
            # (10/10, 9/9, 10/10, 3/3 — transit corruption varies, a stale FILE does not); 39 tasks
            # destroyed across two unrelated sessions and reported as
            # "ZERO PROGRESS (likely code/config bug, not infra)", which it was not.
            # Comparing the BYTE COUNT restores the resume: a short blob now reads ABSENT, so the
            # `--partial` push runs and completes it. Size (not sha256) keeps this a cheap `stat` on
            # the ship path, and it is sufficient — the worker still verifies sha256 before
            # extracting, so this decides only WHETHER TO RESUME, never whether to trust.
            want_bytes = Path(plan["blob_path"]).stat().st_size
            probe = ssh_run(host, port,
                            f"test -f {remote_blob} && test \"$(stat -c%s {remote_blob})\" "
                            f"-eq {want_bytes} && echo PRESENT || echo ABSENT",
                             run=self.run)
            ops.append((probe.returncode == 0, True))
            if "PRESENT" not in (probe.stdout or ""):
                mk = ssh_run(host, port, f"mkdir -p ~/spool/{bundle.BLOBS_DIRNAME}", run=self.run)
                ops.append((mk.returncode == 0, True))
                ok_blob, why = rsync_push_detail(host, port, [plan["blob_path"]], plan["blob_dir"],
                                                 run=self.run)
                ops.append((ok_blob, True))
                if not ok_blob:
                    return {"ok": False, "already": False, "ops": ops,
                            "fail": f"code blob {plan['blob_id']} could not be delivered — {why}"}
        if plan["reship"]:
            # A RE-ship (this task already ran once — requeued after a preemption or infra
            # failure): the spool dir from the earlier attempt is still on the box (the worker
            # never deletes task dirs), but it holds the OLD task.json/resume state, which the
            # existence check below would otherwise mistake for "already delivered" and skip —
            # silently dropping the new resume.pt/`--init-from` and leaving the task looking
            # "shipped" while nothing was actually re-sent. We've already pulled everything of
            # value home by this point, so clear it before delivering the fresh attempt.
            ssh_run(host, port,
                    f"rm -rf ~/spool/incoming/{tid} ~/spool/active/{tid}",
                    run=self.run)
        else:
            # Check for READY specifically, not just the incoming/<id> directory -- a prior
            # attempt can die between pushing payload+task.json and pushing READY (invariant 7's
            # last step), leaving incoming/<id> present but never actually claimable. Found live
            # 2026-07-09: that exact partial state got waved through as "already delivered" by a
            # bare directory-existence check, silently CASing to `shipped` while the worker sat
            # forever waiting for a READY that had never arrived -- the box billed idle for over
            # an hour before this was caught by hand. `active/<id>` existing is still sufficient
            # proof of full prior delivery (the worker's claim-rename only happens after READY).
            exists = ssh_run(
                host, port,
                f"test -e ~/spool/incoming/{tid}/READY -o -e ~/spool/active/{tid} "
                "&& echo EXISTS",
                run=self.run)
            if exists.returncode == 0 and "EXISTS" in exists.stdout:
                # invariant 7: first-ship idempotency across dispatcher restarts
                return {"ok": True, "already": True, "ops": ops, "fail": None}
        if plan["apt_pkgs"]:
            pkgs = plan["apt_pkgs"]
            apt_cmd = (f"dpkg -s {pkgs} >/dev/null 2>&1 || "
                       f"(apt-get install -y -qq {pkgs} 2>/dev/null || "
                       f"(apt-get update -qq && apt-get install -y -qq {pkgs}))")
            out = _run_or_timeout(self.run, ssh_cmd(host, port) + [apt_cmd], 300)
            ops.append((out.returncode == 0, True))
            if out.returncode != 0:
                return {"ok": False, "already": False, "ops": ops,
                        "fail": f"apt install failed for {pkgs}: {out.stderr[-300:]}"}
        ok, why = False, ""
        for attempt in range(3):
            ok, why = rsync_push_detail(host, port, [plan["bundle_path"]], plan["remote_dir"],
                                        run=self.run)
            if ok:
                break
            time.sleep(2 ** attempt)
        ops.append((ok, True))
        if not ok:
            # ⚠ CARRY THE REASON. The bare version of this string said only "rsync push failed after
            # 3 attempts", which reads as a link problem and is why a full disk cost 34 minutes and
            # six stranded tasks before anyone ran `df` (see `rsync_push_detail`).
            return {"ok": False, "already": False, "ops": ops,
                    "fail": f"rsync push failed after 3 attempts to {host}:{port} — {why}"}
        ok2 = rsync_push(host, port, [plan["ready_path"]], plan["remote_dir"], run=self.run)
        ops.append((ok2, False))   # historically recorded WITHOUT an ssh_fallback log
        return {"ok": ok2, "already": False, "ops": ops, "fail": None}

    def _apply_ship_io(self, task: dict, inst: dict, res: dict) -> None:
        """SERIAL apply of one ship's I/O outcome: replay reachability into the tracker in the
        order the ops happened (so the proxy->direct switch fires exactly where it used to) and
        log the failure reason, if any. Every DB/tracker mutation of the ship path lives here."""
        for ok, announce in res["ops"]:
            switched = self.tracker.record(inst["id"], ok)
            if switched and announce:
                self.log("ssh_fallback", f"switched instance {inst['id']} to direct endpoint",
                          instance_id=inst["id"])
        if res["fail"]:
            self.log("ship_failed", res["fail"], task_id=task["id"], instance_id=inst["id"])

    def _unship_orphaned_payload(self, task: dict, inst: dict, reason: str) -> None:
        """Invariant 18e — a payload we delivered but no longer OWN must be removed from the box.

        `_ship_all` reads its task row BEFORE `_ship` runs, and `_ship` is slow (compile + apt +
        rsync: median 30s, max 366s). A `runq cancel` inside that window flips `claimed → cancelled`
        immediately (registry inv. 4a treats `claimed` as "no worker yet" — true of the BOX, false of
        the dispatcher, which is mid-delivery). `_ship` then still pushes `READY`, so the worker
        claims the task and launches the trainer, while the `claimed → shipped` CAS above is now
        illegal and — since `transition()` never raises — silently no-ops.

        The result is an orphan with no owner anywhere: terminal in the registry, running on a paid
        box, holding a slot against the worker's `should_launch` `max_slots` gate forever, and
        invisible to `reap_orphans` because `active/<id>` exists and carries no terminal marker.
        Live 2026-07-29 (`azsc-p1e`): two tasks cancelled at 12:49:38 whose `compile` events landed
        at 12:50:03 — after the cancel — ran 3h+ and held 2 of a 6-slot box, which starved a sibling
        into looking like a hung trainer for 85 min when in fact it had NO PROCESS at all.

        Removing `active/<id>` is deliberately the same act that makes an already-launched trainer
        reapable: `reap_orphans` classifies a process whose owning task dir is GONE as an orphan and
        kills its pid subtree. So this covers both sides of the race — not-yet-claimed (the `READY`
        disappears) and already-running (the reaper collects it). Best-effort: a failed cleanup is
        logged and leaves exactly the pre-fix behaviour, never worse, and never touches the CAS."""
        host, port = endpoint_for(inst, self.tracker, self.vastai_run)
        out = ssh_run(host, port,
                      f"rm -rf ~/spool/incoming/{task['id']} ~/spool/active/{task['id']}",
                      run=self.run)
        self.log("ship_unowned",
                  f"delivered payload for {task['grp']}/{task['name']} but the shipped CAS did not "
                  f"apply ({reason}) — the task went terminal mid-ship, so the box copy was removed "
                  f"to stop the worker running an untracked orphan (cleanup rc={out.returncode})",
                  task_id=task["id"], instance_id=inst["id"])

    def _ship_all(self):
        """Invariant 7b — ONE un-shippable box may not consume the whole pass.

        `poll_once` is serial and this loop walks every `claimed` task in `priority DESC,
        created_at ASC`, so a box we cannot reach is retried 3x per task, for every task packed onto
        it, AHEAD of everyone else's — and its tasks sort FIRST precisely because they have been
        stuck longest. Live 2026-07-29: six tasks claimed onto a degraded box, 6m07s burned per task
        (3 x the 120s rsync budget + backoff), 36.5 min of a single pass spent delivering zero bytes
        while four tasks on two HEALTHY boxes waited behind them — measured at ~23s each once the
        loop finally reached them. Repeating every poll, since a ship failure leaves the task
        `claimed` for the next pass to find.

        So: the first attempt on a box that records a NEW transport failure retires that box for the
        rest of this pass. Cost of a dead box drops from `6min x its whole backlog` to `6min`, once.
        Keyed on `ConnectionTracker.consecutive_fails` moving rather than on `ok` alone, because
        `_ship` also returns False for TASK-level faults (a `git archive` that failed, an entrypoint
        whose apt packages don't exist) which say nothing about the box and must not defer its
        siblings. The tracker is already fed by every ssh/rsync op in `_ship`, so this needs no new
        state and stays correct when the failure happens in the apt step rather than the push.

        Invariant 23d (2026-07-31) — the pass FANS OUT, one worker per box. Ship was the last
        serial data-movement phase: measured 37% of a poll cycle whose median is 20.8 min, with
        `ship_budget_spent` firing on EVERY pass (4-12 shipped, 6-31 deferred). The consequence was
        not a slow coordinator but an IDLE FLEET — median 52 tasks `running` against 115 `claimed`
        but not yet started, i.e. work already assigned to a box and waiting on transport, and a
        median `add`->`start` of 36 min (mean 82, p90 3.6 h).

        The axis is PER-BOX for the same measured reason ingest's is (invariant 23c): per-box
        throughput is ~0.3-0.62 MB/s against a 1 Gb/s uplink, so the ceiling is each box's own
        network path — concurrency WITHIN a box contends for one saturated link and buys nothing,
        while concurrency ACROSS boxes is ~linear. So: serial within a box, parallel across them.

        Both invariants above survive the change and 7b gets STRICTER, not looser. 7b is now
        structural — a box's ships are one worker's serial loop, so a transport failure stops that
        box's remaining tasks by `break`, and it can no longer burn other boxes' budget on the way
        (which is what `retired_cost` existed to refund). 7c's "at least one task always ships"
        becomes "at least one PER BOX", a superset: the single-task-starves-the-queue case it was
        written for cannot occur on any box.

        The serial path is kept and tested, and is what runs whenever the width collapses to 1 —
        one box holding work, or `ship_parallel_boxes = 1` to disable the fan-out outright."""
        claims = []
        for r in registry_db.list_tasks(self.conn, states=["claimed"]):
            task = dict(r)
            row = self.conn.execute("SELECT * FROM instances WHERE id=?",
                                     (task["instance_id"],)).fetchone()
            if row is not None and row["state"] == "live":
                claims.append((task, dict(row)))
        boxes = {inst["id"] for _t, inst in claims}
        width = max(1, min(int(self.settings["ship_parallel_boxes"]), len(boxes)))
        if width > 1:
            self._ship_all_parallel(claims, width)
            return
        self._ship_all_serial(claims)

    def _ship_all_serial(self, claims: list):
        """The original single-threaded pass, unchanged — see `_ship_all`'s invariants 7b/7c."""
        undeliverable: set = set()
        budget = self.settings["ship_budget_sec"]
        started = time.time()
        attempted = deferred = 0
        # Time burned on a box that 7b then RETIRES is credited back: 7b's whole guarantee is that a
        # dead box costs one attempt per pass instead of one per task, and charging that attempt to
        # the budget would hand the cost straight back to the healthy tasks queued behind it.
        # Measured regression from the first version of 7c (2026-07-29): a degraded box's tasks sort
        # FIRST (`priority DESC, created_at ASC` — they have been stuck longest), so two attempts on
        # it consumed the entire 300s; 7b retired it and 7c stopped the pass in the same instant,
        # deferring 19 healthy tasks that would each have taken ~20s. It repeated every pass, and
        # four tasks on the OWNED boxes sat `claimed` for 33 min behind it. Bounded by construction:
        # 7b admits at most one attempt per box per pass, so at most one credit each.
        retired_cost = 0.0
        for task, row in claims:
            if row["id"] in undeliverable:
                continue  # transport already proven broken this pass — don't burn 6 more minutes
            # Invariant 7c — BOUND the pass even when every ship SUCCEEDS. 7b above retires a box
            # whose transport is broken; this covers the complementary case of ship work that is
            # simply slow and plentiful, which starves ingest just as effectively. `poll_once` is
            # serial and `_ingest_and_complete` runs at the TOP of it, so an unbounded ship phase
            # postpones the next one by its full length — and that phase carries every worker.jsonl
            # pull, every checkpoint pull, and the entire reaper layer.
            #
            # Measured 2026-07-29: compile is 89% hits at ~10s but 11% MISSES at a median 155s (max
            # 272s) — every distinct code snapshot pays one, and with several worktrees queueing at
            # once nearly every task is a distinct snapshot. Per-task ship cost median 30s, p90 135s,
            # max 366s. Observed consequence: 10 ships in 15 min with ZERO starts and ZERO dones,
            # `box_measured` 27 min stale, owned boxes' worker.jsonl 36-59 min stale.
            #
            # `attempted` gates the check so at least ONE task always ships per pass: a single
            # 366s task must not be starved forever by a budget smaller than itself. Deferred tasks
            # simply stay `claimed` for the next pass — no state change, no retry cost, and invariant
            # 10d cannot mistake them for undeliverable because it additionally requires a recorded
            # `ship_failed` for that task on that instance, which a deferred task does not have.
            if attempted and (time.time() - started - retired_cost) > budget:
                deferred += 1
                continue
            attempted += 1
            attempt_started = time.time()
            fails_before = self.tracker.consecutive_fails(row["id"])
            ok = self._ship(task, row)
            if ok:
                res = registry_db.transition(self.conn, task["id"], "shipped", "ship",
                                              f"shipped to instance {task['instance_id']}")
                # A delivery proves the transport works again — lift any 10d quarantine so the box
                # returns to the placement pool without waiting for a restart.
                self._clear_ship_quarantine(row)
                if not res.ok:
                    self._unship_orphaned_payload(task, row, res.reason)
            elif self.tracker.consecutive_fails(row["id"]) > fails_before:
                undeliverable.add(row["id"])
                retired_cost += time.time() - attempt_started
                self.log("ship_box_deferred",
                          f"transport failure on instance {row['id']} — deferring its remaining "
                          "claimed task(s) to the next poll so one box cannot starve the fleet",
                          task_id=task["id"], instance_id=row["id"])
                # ⛔⛔ INVARIANT 10d(fast) — ARM THE QUARANTINE HERE, WHERE THE FAILURE IS KNOWN.
                #
                # Arming used to be reachable ONLY from `_reap_undeliverable_claims`, which fires on
                # a task that sat CLAIMED past `undeliverable_after_min` and was then requeued. That
                # catches a box which fails delivery SLOWLY and is BLIND to one that fails FAST:
                # a fast failure transitions straight to `task_failed`, so nothing is ever claimed
                # long enough, `requeued` stays 0, and the arming line never runs.
                #
                # MEASURED 2026-08-30, instance 40000055: a poisoned blob cache (see `b08c7491` —
                # `--partial` leaves a truncated blob and `test -f` trusts it) failed every ship in
                # ~11s. The box stayed `live`, kept winning placements because it had free slots,
                # and destroyed 39 tasks across two unrelated sessions before a human intervened.
                # `b08c7491` removes that CAUSE; this removes the reason one cause could run
                # unchecked for 39 tasks. Any future fast-failing box is barred after 3 tries.
                #
                # ⚠ Symmetry already existed on the other side: a successful delivery calls
                # `_clear_ship_quarantine` a few lines above, so a box that recovers rejoins the
                # pool on its own. Only the arming half was missing.
                fails_now = self.tracker.consecutive_fails(row["id"])
                if (fails_now >= self.settings["ship_quarantine_after_fails"]
                        and not self._ship_quarantined(row)):
                    self._set_ship_quarantine(
                        row, f"{fails_now} consecutive ship failures (fast-fail path)")
                    self.log("ship_quarantine_armed",
                              f"instance {row['id']} ship-quarantined after {fails_now} consecutive "
                              f"ship failures — it takes no new work until a delivery succeeds "
                              f"(inv. 10d(fast); `should_teardown` will reclaim it as undeliverable)",
                              instance_id=row["id"])
        if deferred:
            # Never silent: a bounded pass looks exactly like a stalled one from the outside, and an
            # unlogged cap reads as "we shipped everything there was" when it is not.
            self.log("ship_budget_spent",
                      f"ship budget {budget}s spent after {attempted} task(s) in "
                      f"{time.time() - started:.0f}s ({retired_cost:.0f}s of it on boxes 7b retired, "
                      f"not charged) — {deferred} claimed task(s) deferred to the next poll so "
                      "ingest/reapers/checkpoint-pulls are not starved (inv. 7c)")

    def _ship_all_parallel(self, claims: list, width: int):
        """Invariant 23d — PLAN serially, PUSH one worker per box, APPLY serially.

        The three phases exist because of the `_*_io` contract (see `_ship_io`): the middle one
        runs off-thread so it may touch neither `self.conn` nor `self.tracker`. Everything that
        does — the prior-ship count, the compile/ship caches, endpoint resolution, every
        transition, every log — is on one of the serial ends.

        The cache-safety half of this rationale RETIRED 2026-07-31: the coordinator no longer
        builds anything, so there are no compile/ship caches for two threads to race. `_ship_plan`
        now just reads a queuer-built blob and verifies its digest. The `_*_io` contract above is
        the whole reason the phases stay split."""
        budget = self.settings["ship_budget_sec"]
        started = time.time()
        by_box: dict[int, list] = {}
        insts: dict[int, dict] = {}
        for task, inst in claims:                       # preserves priority order within a box
            by_box.setdefault(inst["id"], []).append(task)
            insts[inst["id"]] = inst

        # ---- PLAN (serial): resolve endpoints and BUILD every payload. A compile ERROR is a code
        # bug — fail that task fast here rather than handing a doomed plan to a worker (spec inv. 6).
        plans: dict[int, list] = {}
        endpoints: dict[int, tuple] = {}
        deferred = 0
        for iid, tasks in by_box.items():
            endpoints[iid] = endpoint_for(insts[iid], self.tracker, self.vastai_run)
            for task in tasks:
                # 7c, per box: the budget bounds how much PLANNING a pass does, and the first task
                # of every box is always admitted so no box can be starved by a budget smaller
                # than one of its tasks.
                if plans.get(iid) and (time.time() - started) > budget:
                    deferred += 1
                    continue
                plan = self._ship_prepare(task, insts[iid], *endpoints[iid])
                if plan is None:
                    continue                    # payload could not be built (already logged)
                plans.setdefault(iid, []).append((task, plan))

        # ---- PUSH (parallel): one worker per box, serial within it.
        results: list = []
        push_wall = work_sec = 0.0
        if plans:
            w = max(1, min(width, len(plans)))
            args = [(iid, endpoints[iid][0], endpoints[iid][1], plans[iid]) for iid in plans]
            t0 = time.monotonic()
            if w == 1:
                timed = [(a[0], self._timed_box_io(a[1], a[2], a[3])) for a in args]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=w) as pool:
                    futs = {pool.submit(self._timed_box_io, h, p, ts): iid
                            for iid, h, p, ts in args}
                    timed = [(futs[f], f.result())
                             for f in concurrent.futures.as_completed(futs)]
            push_wall = time.monotonic() - t0
            work_sec = sum(secs for _iid, (_r, secs) in timed)
            results = [(iid, box_results) for iid, (box_results, _s) in timed]
            # Summed per-thread work / wall-clock == the parallelism actually ACHIEVED. Logged for
            # the same reason ingest logs `payload_speedup` (23c): a fan-out that silently stops
            # working — one box monopolising the pass, a width collapsed to 1 — is otherwise
            # invisible without re-deriving it by hand from event timestamps.
            #
            # ⚠ `speedup` ALONE CANNOT TELL YOU WHY, and the first live multi-box pass proved it:
            # 4 boxes, width 4, wall 211.4s vs work 216.2s => speedup 1.0. That is the signature of
            # a fan-out that is not fanning AND, identically, of one box holding nearly all the
            # work — Amdahl on the slowest box, which no width can fix. The two demand opposite
            # responses (fix a bug vs. spread the packing), so the per-box seconds below are what
            # discriminate them, and they are the FIRST thing to read:
            #   max(per_box) ~= wall_sec  and  sum ~= work_sec  -> SKEW. Working as designed; the
            #                                                      lever is placement, not ship.
            #   every per_box small, sum ~= wall_sec            -> SERIALISED. A real defect here.
            per_box = {str(iid): round(secs, 1) for iid, (_r, secs) in timed}
            slowest = max(per_box.values()) if per_box else 0.0
            # `speedup` is only meaningful against what was ACHIEVABLE. Per-box parallelism can
            # never beat its slowest box, so the ceiling is `work / slowest` and `efficiency` is
            # the fraction of it reached. This is what makes a low speedup self-diagnosing instead
            # of ambiguous: efficiency ~1.0 means the fan-out did everything it could and the
            # residual limit is one slow box (spread the packing, or that box's link is the floor),
            # while a LOW efficiency is the real 23d defect. MEASURED 2026-07-31T22:2xZ: 7 boxes,
            # 20 tasks, work 338.1s -> wall 93.4s, speedup 3.6 against a 3.62 ceiling => ~1.0.
            ceiling = (work_sec / slowest) if slowest > 0.05 else None
            speedup = (work_sec / push_wall) if push_wall > 0.05 else None
            self.log("ship_fanout", json.dumps({
                "boxes": len(plans), "width": w,
                "wall_sec": round(push_wall, 1), "work_sec": round(work_sec, 1),
                "speedup": round(speedup, 1) if speedup else None,
                "ceiling": round(ceiling, 1) if ceiling else None,
                "efficiency": round(speedup / ceiling, 2) if (speedup and ceiling) else None,
                "tasks": sum(len(v) for v in plans.values()),
                "per_box_sec": per_box,
                "tasks_per_box": {str(iid): len(v) for iid, v in plans.items()},
                "slowest_box_sec": round(slowest, 1),
            }, separators=(",", ":")))

        # ---- APPLY (serial): all DB + tracker mutation.
        attempted = 0
        for iid, box_results in results:
            inst = insts[iid]
            for task, res in box_results:
                attempted += 1
                self._apply_ship_io(task, inst, res)
                if res["ok"]:
                    tr = registry_db.transition(self.conn, task["id"], "shipped", "ship",
                                                f"shipped to instance {task['instance_id']}")
                    # A delivery proves the transport works again — lift any 10d quarantine so the
                    # box returns to the placement pool without waiting for a restart.
                    self._clear_ship_quarantine(inst)
                    if not tr.ok:
                        self._unship_orphaned_payload(task, inst, tr.reason)
            # 7b: a box the worker stopped early has un-attempted plans left over.
            left = len(plans[iid]) - len(box_results)
            if left:
                deferred += left
                self.log("ship_box_deferred",
                          f"transport failure on instance {iid} — deferring its remaining "
                          f"{left} claimed task(s) to the next poll so one box cannot starve "
                          "the fleet", instance_id=iid)
        if deferred:
            # Never silent: a bounded pass looks exactly like a stalled one from the outside, and an
            # unlogged cap reads as "we shipped everything there was" when it is not.
            self.log("ship_budget_spent",
                      f"ship budget {budget}s spent after {attempted} task(s) in "
                      f"{time.time() - started:.0f}s across {len(plans)} box(es), {width} in "
                      f"parallel — {deferred} claimed task(s) deferred to the next poll so "
                      "ingest/reapers/checkpoint-pulls are not starved (inv. 7c/23d)")

    def _timed_box_io(self, host: str, port: int, plans: list) -> tuple:
        """`_ship_box_io` plus its own wall-clock, so the caller can report the parallelism actually
        achieved. Timing here rather than in the caller is what makes it PER-THREAD work: timing the
        submit would measure queueing in the pool, not pushing to the box."""
        t0 = time.monotonic()
        out = self._ship_box_io(host, port, plans)
        return out, time.monotonic() - t0

    def _ship_box_io(self, host: str, port: int, plans: list) -> list:
        """PURE I/O (invariant 23d): ONE box's ships, serial within the box. Returns the
        `(task, result)` pairs actually ATTEMPTED — a short return is how 7b reports that this box
        stopped early, which the serial apply turns into `ship_box_deferred`.

        Stopping on any failed push IS 7b faithfully: the serial version keyed on
        `ConnectionTracker.consecutive_fails` moving so that TASK-level faults would not defer a
        box's siblings, and the only two faults that can still reach this half — apt and rsync —
        are exactly the two the serial version fed to the tracker as transport. The task-level ones
        it meant to exclude (a failed `git archive`, a compile error) are now resolved in
        `_ship_prepare`, on the serial side, and never produce a plan at all."""
        out = []
        for task, plan in plans:
            res = self._ship_io(plan, host, port)
            out.append((task, res))
            if not res["ok"]:
                break
        return out

    def _teardown_idle(self):
        instances = self._instances_view()
        queued = [self._task_view(dict(r)) for r in registry_db.list_tasks(self.conn, states=["queued"])]
        # Invariant 11a: ONE fleet-wide pass, BEFORE any per-box verdict, designates which empty
        # paid boxes keep the `feasible_task_waiting` hold — bounded by box count
        # (`warm_idle_max`), by free slots (`max_warm_free_slots`), and by whether the queue's
        # demand needs them at all once the fleet's already-free capacity is counted. Every other
        # empty box falls through to the ordinary idle timer. Hoisting it out of the loop is what
        # keeps the designation from drifting as boxes are destroyed inside it.
        keep = warm_hold_grants(instances, queued, self.settings)
        released = sorted(i["id"] for i in instances
                          if i["state"] == "live" and i.get("source", "vast") != "owned"
                          and (i.get("dph_usd") or 0.0) > 0.0
                          and not i.get("occupants") and i["id"] not in keep)
        if released and queued:
            self.log("warm_cap", f"warm cap ({self.settings.get('warm_idle_max', 1)} box / "
                                 f"{self.settings.get('max_warm_free_slots', 10)} slots): holding "
                                 f"{sorted(keep)}, releasing {released} to the idle timer "
                                 f"({len(queued)} queued)")
        for inst in instances:
            if inst["state"] != "live":
                continue
            inst = {**inst, "warm_idle_keep": inst["id"] in keep or bool(inst.get("occupants"))}
            down, reason = should_teardown(inst, queued, time.time(), self.settings)
            if down:
                self._destroy(inst, reason)

    def _destroy(self, inst: dict, reason: str):
        # Owned-box carve-out: this is the single choke point every teardown path funnels
        # through (idle timeout, dead-worker reap, stuck-provisioning reap) — refusing here
        # once, rather than at each call site, means a future new caller can't forget the guard.
        # A `source='owned'` box was never rented (no Vast id to destroy) and is never re-adopted
        # once `destroyed` (adopt only fires for a genuine `runq_<task>`-labeled Vast instance),
        # so destroying it here would strand it out of the placement pool permanently.
        if inst.get("source", "vast") == "owned":
            self.log("destroy_skipped", f"refusing to destroy owned box: {reason}",
                      instance_id=inst["id"])
            return
        self.log("teardown", reason, instance_id=inst["id"])
        if self.dry_run:
            return
        cost = self._realized_cost(inst["id"])
        vastai_json("destroy", "instance", str(inst["id"]), "--yes", run=self.vastai_run)
        self.conn.execute(
            "UPDATE instances SET state='destroyed', destroyed_at=?, cost_usd=? WHERE id=?",
            (registry_db.now_iso(), cost, inst["id"]))
        # Invariant 21h: the box is gone, so its hold has nothing left to protect. Dropping it keeps
        # `settings` from accumulating a dead row per drained box (the `consolidate` EVENT remains
        # the durable record, and the 21g cooldown reads that, not this).
        self._clear_drain_hold(inst)
        self.conn.commit()


@contextlib.contextmanager
def singleton_lock():
    """Invariant 1."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("dispatcher: another instance already holds the lock — exiting", file=sys.stderr)
        f.close()
        sys.exit(0)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# ⛔⛔ THE SHARED-WRITE GRANT DECAYS WITHOUT THIS, AND IT DECAYS SILENTLY AND LATER.
# `host_setup.sh` §6 gives the QUEUER (the devcontainer, a different uid) write access to
# `experiments/` through the group `coord` plus setgid on the directories. setgid propagates the
# GROUP. It does NOT propagate the group WRITE bit — that is the umask's job, and the default 022
# strips it. So every directory the dispatcher creates after the one-time `chmod -R g+rwX` comes out
# `drwxr-sr-x`: right group, no `g+w`.
#
# Measured 2026-09-17, one hour after the grant landed:
#     experiments/           drwxrwsr-x  coord    <- the one-time chmod
#     experiments/hier_pc_r4 drwxrwsr-x  coord    <- predates the grant, so the chmod reached it
#     experiments/hier_pc_r5 drwxr-sr-x  coord    <- dispatcher-created, NOT writable by the queuer
#     …/rate_s1/results.json -rw-r--r--  coord
# The queuer could not write its own campaign's `summary.md` into the directory holding that
# campaign's results. §6's comment claims setgid prevents exactly this failure; it prevents half.
#
# ⚠ 002, not 000: group write is the whole point, world write is not, and a data root the fleet
# writes to should not be world-writable. Files land 664 and directories 2775.
# ⚠ Set on the DISPATCHER, not in `host_setup.sh`: the host script can only fix what exists when it
# runs, and the thing that keeps creating new directories forever is this process.
os.umask(0o002)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    with singleton_lock():
        d = Dispatcher(a.db, dry_run=a.dry_run)
        if a.once:
            d.poll_once()
            return 0
        while True:
            d.poll_once()
            time.sleep(d.settings["poll_seconds"])


if __name__ == "__main__":
    sys.exit(main())
