# Feature: task dispatcher (home daemon) + spool worker (box resident)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (experiment tooling; no `src/*` changes)
- **Module path:** `fleet/dispatcher.py` (home), `fleet/spool_worker.py` (box),
  `fleet/entrypoints.py` (code-owned entrypoint table, shared with the registry spec)
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/task-dispatcher.spec.md`

## Purpose

Turn the run registry's queue into finished runs on Vast without an agent babysitting: the
dispatcher rents boxes, **packs queued tasks onto already-rented boxes with free capacity**
(box-sharing across agents), ships work over ssh (push model — no secrets ever on the box),
retries infrastructure failures, pulls results into `experiments/`, and tears boxes down on idle
timeout or hard cap — all under a **max-$/hr budget gate** and a balance floor. The box side is a
thin resident worker that executes spooled tasks under the sweep supervisor's hardware-adaptive
launch rules. Structural fixes over the current scripts: teardown/idle-billing no longer depends
on a home-side babysitter process staying alive against one box, and every rented instance is
recorded in the DB *before* provisioning proceeds (closes the launcher-death → idle-billing hole
in VAST-TEST.md).

Two further learnings from real campaigns are load-bearing here, not optional polish: (1) the ssh
proxy endpoint can silently die for hours while the box itself is fine (VAST-TEST.md, 2026-07-07)
— every network operation this feature performs, not just one babysitter script, needs the
proxy→direct fallback; (2) a lost instance today loses ALL progress on its running tasks, because
nothing pulls a checkpoint until the very end. This feature pulls `ckpt_latest.pt` periodically
while a task runs, so a lost/preempted task resumes near where it left off instead of from
scratch (`--init-from`, already supported by the affected `native` trainers — see invariant 16).
The same checkpoint-carry mechanism also backs **priority preemption**: a sufficiently
higher-priority task can evict a lower-priority running one, but only after that task's own next
checkpoint write, so no trainer changes are needed and no progress is thrown away (invariant 17).

## Input contract

- **Registry schema** — `docs/specs/run-registry.spec.md` (tasks/instances/events/settings).
  The dispatcher is the only writer of dispatcher-side transitions (registry spec invariant 4).
- **Settings** (rows in `settings`; defaults written at first dispatcher start; all overridable
  via `runq`-less direct SQL or a future `runq set`). Money knobs (resolved — owner sign-off
  2026-07-08, spec/DECISIONS.md entry retired):
  - `max_hourly_usd = 1.00` — committed $/hr cap across all live boxes combined.
  - `balance_floor_usd = 3.00` — refuse new rents once the Vast balance drops below this;
    running work continues uninterrupted.
  - `observed_balance_usd` / `observed_balance_at` — **written, not read**: the last balance the
    coordinator actually observed from `vastai show user`, and its ISO-Z timestamp. Not knobs and
    not in the defaults; runtime observation parked in the same kv table as `pause_i<ID>`. Written
    by the place phase ONLY when the API answered (never for the carried-forward fallback of 4e's
    unknown-balance rule, and never in `--dry-run`), so a consumer can tell a current balance from
    a stale one — the pair, not the value, is the contract. The rent gate itself is unaffected: it
    still uses the in-memory reading it just took. Read by the dashboard (Behavior §14 of
    `run-dashboard.spec.md`), which is why the balance exists outside this process at all.
  - `max_instance_dph = 0.40` — per-offer $/hr ceiling at search time.
  - `idle_timeout_min = 10` — teardown after this long with zero running/claimable tasks and
    none feasible.
  - `warm_idle_max = 2` — invariant 11a: how many EMPTY paid boxes may be held warm against the
    queue by `feasible_task_waiting`. Owner directive 2026-07-30: first 1 ("no more than 1 box kept
    warm and idle"), then **2** once `max_warm_free_slots` became the real constraint — *"that was
    just a hack to close the gap."*
  - `max_warm_free_slots = 10` — invariant 11a, the same directive stated in SLOTS ("58/98 slots in
    use so almost 50% of our spend is going to waste … no more than 10 free slots kept warm"). A
    **fleet-wide** ceiling: free slots on live boxes that still hold work — owned or paid — are
    spent against it FIRST, so only the remainder may be held warm on an empty paid box.
  - `rent_patience_min = 15` — see invariant 4c/4d: the "short enough to just wait" threshold in
    the backlog-bar rent rule, no longer a rule on its own.
  Slot-sizing knobs (invariant 4a', replaces the flat `default_slots_total` — sizing must track
  the actual rented hardware, not a single global guess):
  - `vram_per_lane_gb = 0.6`, `cores_per_lane = 1` — global default per-lane footprint (the
    proven MinAtar/breakout-class co-tenancy ratio, VAST-TEST.md).
  - `max_slots_cap = 11` — hard ceiling on computed `slots_total` regardless of hardware. Raised to
    16 on 2026-07-31 by the `hw/deepk16` probe, **REVERTED on 2026-08-01** (see 27e), and **raised
    8 → 11 on 2026-08-04** once both original blockers were gone:
      * *the reaper half fixed itself.* Invariant 19h-3(d)'s RECENT-LAUNCH guard landed 2026-08-02:
        `_reap_overpacked_boxes` now skips any box that started a lane within `ship_launch_grace_min`,
        and a filling box starts one every `settle_minutes` (3.0), so the permanent ratchet that
        punished the 16-lane attempt cannot fire mid-fill at any K.
      * *the delivery half is fixed in this change.* `spool_worker` now passes its OWN per-box
        `--max-slots` into `should_launch` instead of `AUTO_DEFAULTS["max_slots"]`. That constant is
        for the pre-dispatcher manual sweep lane; feeding it to the worker put a second, fleet-wide
        ceiling of 8 behind the per-box one, so a box the dispatcher sized at 11+ silently stopped at
        8 and `max_slots_cap` was untunable above 8 **by construction**. The dispatcher's per-box
        sizing is now authoritative and there is exactly one ceiling. (Superseded 2026-10-03 by
        invariant 8a: the worker no longer holds a lane count at all, so that one ceiling is
        placement's.)
    11 rather than 12 or 16: it is the largest K fitting the EXISTING `ship_launch_grace_min` under
    the conservative `cap × settle` fill bound below (11 × 3.0 = 33 ≤ 35), so the raise moves one
    constant and weakens no guard. Measured payoff (`hw/deepk16`, `experiments/hw/summary.md`):
    6.47 effective lanes @ K=8 → 8.42 @ K=12, so K=11 interpolates to ≈8.0, ≈+1.5 over K=8 against a
    pre-registered bar of +1.0, at $0.0081 → ~$0.0055 per effective-lane-hour on the same box at the
    same price. Going beyond 11 requires raising the grace or shortening `settle_minutes` as its own
    evidenced decision — **not** by loosening the fill guard.
  - `ship_launch_grace_min = 35` — must stay `> max_slots_cap x AUTO_DEFAULTS["settle_minutes"]`
    or the over-pack reaper fires on boxes that are still filling (27e2). This is deliberately the
    CRUDE bound: true gated fill is `(cap-1) × settle`, since `should_launch` returns "first lane" at
    `n_live == 0` before the settle check, and the extra interval is the allowance for per-lane
    ship/launch time the ideal arithmetic ignores. Keep the margin; do not tighten it to buy a cap.
  - `AUTO_DEFAULTS["settle_minutes"] = 3.0` — **unchanged by the 2026-08-04 raise, deliberately.**
    It paces the launch loop so the gates after it read steady state, chiefly `load1 >= cores - 1`,
    whose signal is a 1-minute load average and so lagged by construction. Measured over ~5k
    `box_measured` samples, load1 rises ≈1.0 per lane (0.7 at 0 lanes, 3.1 at 2, 6.2 at 6, 8.3 at 7)
    against a threshold of `cores-1` = 19..55 — **10-32 cores of headroom at every depth the fleet
    has ever run, and the gate has never fired.** There is also no throughput ramp to protect: 95% of
    930 TB-instrumented runs were at ≥90% of steady-state steps/sec by the first logged interval
    (median 0 warmup steps), with startup to first step ≈1 min. So shortening settle would buy fill
    LATENCY, not correctness — a separate decision, and not needed at K=11.
  - `offer_ram_derate = 0.8` — fraction of an offer's advertised `cpu_ram` trusted as real
    container RAM (invariant 27b). Note the per-lane footprint itself is no longer a global guess:
    invariant 26 MEASURES it and layers it over each task's declared hint.
  Hardware-quality gate (invariant 4e, **2026-07-21 owner directive** — the coordinator was booking
  cheap-but-weak single-slot boxes, e.g. a GTX 1080 at 5.25¢ over a 24-core RTX 3060 Ti at 5.47¢,
  because the $/slot-hour rule degenerated to raw price on a short queue and no signal captured that
  an old box churns the (CPU-bound) workload slower):
  - `offer_search_limit = 1000` — the `--limit` passed to `vastai search offers`. **The Vast CLI
    default is 64**, and the query sorts `-o dph_total` (cheapest-first), so the coordinator only
    ever *saw* the 64 cheapest boxes — a pool structurally dominated by obsolete cards (measured
    2026-07-21: the full market under 12¢ held 199 boxes; the 64-box window cut off at ~5.5¢, below
    every RTX 3070/3080/3090/4060-Ti/5060-Ti/A4000). Fetching ~1000 makes the good hardware visible
    to the ranking at all.
  - `gpu_deny = ["GTX", "Titan Xp", "Titan X", "Titan V", "Quadro P", "Quadro M", "Quadro K",
    "Tesla K", "Tesla M", "Tesla P", "P100", "K80"]` — an offer whose `gpu_name` contains any of
    these substrings (case-insensitive) is dropped from the candidate set. Obsolete consumer Pascal/
    Maxwell + old datacenter classes; overlaps the reliability audit's worst offenders (GTX 1660
    4/6 lost, GTX 1070 Ti 5/5 dead). A proxy for host generation (an old GPU pairs with an old,
    slow CPU) on a CPU-bound workload. Operator-editable; **not** a completeness guarantee — the
    cores-per-$ ranking below is the primary lever, this just hard-excludes the known junk.
  - `min_cpu_cores_effective = 2.0` — drop any offer whose `cpu_cores_effective` is below this
    floor. Kills 1–2-core slivers (the 1.71-core Titan Xp slice that was booked 4× at full dph)
    that the fit filter alone lets through for `cores_per_lane=1` tasks; deliberately low so it
    never touches a legitimate small modern card (e.g. a 4-core RTX 4070S/A4000). A global floor,
    independent of the per-task `resource_hint`.
  Backlog-bar rent knobs (invariant 4c/4d):
  - `backlog_min_tasks = 3`, `backlog_min_task_minutes = 60` — either bar trips the "worth a
    second box" test.
  Preemption knobs (invariant 17):
  - `preempt_priority_margin = 30` — a task may only evict a running task whose priority is at
    least this much lower.
  Networking/checkpoint knobs (invariants 9, 16):
  - `ssh_fallback_fails = 3` — consecutive network-op failures against an instance before
    switching that instance's connection from the proxy endpoint to direct ip:port.
  - `checkpoint_pull_every_min = 5` — cadence for the periodic mid-run `ckpt_latest.pt` pull
    (independent of the `poll_seconds` cadence used for `worker.jsonl`/`HEARTBEAT`/markers).
  Unchanged: `poll_seconds = 30` · `pull_margin_min = 10` · `est_safety = 1.25` ·
  `claim_timeout_min = 30` (invariant 10b; raised from 15 on 2026-07-28 when the setting was
  first actually consumed — measured over 2197 real ship->start pairs, p99 is 22.9min and the
  legitimate tail ends at 27.5min, so 15 would have reaped 63 healthy boxes, 2.87% of ships;
  30min leaves 8, 0.36%. The worker logs `start` only AFTER installing a job's `setup.pip`
  extras, so a slow install is indistinguishable from a dead worker until then) ·
  `heartbeat_stale_min = 5`.
  - `ship_timeout_min = 90` (invariant 10d, 2026-07-29) — how long a task may sit `claimed` on a
    live box we are demonstrably failing to DELIVER to before it is requeued elsewhere. The
    complement of `claim_timeout_min`: that one covers "we delivered, the worker never claimed";
    this one covers "we can never deliver". MEASURED over the 44 tasks that ever recovered from
    ≥1 `ship_failed`: claim→ship clusters at 8.5–58.6 min (34 of 44), then jumps to 124.0 min. 90
    sits in that gap — ~1.5× the legitimate cluster's max, so it cannot requeue work that was
    merely slow, while still bounding the leak. The cluster is itself inflated by the two defects
    fixed alongside it (non-resumable push, serial ship starvation), so post-fix the real margin is
    far wider. Deliberately **not** keyed on failure COUNT: owned box −1 recovered from streaks of
    311 and 284 consecutive ship failures, so a count threshold would strip work off a box that was
    about to be fine. Plus the per-instance `ship_quarantine_i<instance_id>` settings key it writes
    — learned state, not a tuning knob, and per-instance rather than per-machine because "we cannot
    push bytes to this box" is a property of this rental's link, not the physical machine.
  - `ship_launch_grace_min = 35` (invariant 19h; 20 → 35 on 2026-08-01) — how long a task may sit
    `shipped` on a live, worker-alive box before it's judged gate-held (the box's real concurrency is
    below its advertised `slots_total`) and requeued. Plus the learned per-machine
    `overpack_cap_m<machine_id>` / `overpack_cap_i<instance_id>` settings keys (invariant 19h),
    written by the over-pack detector, read by `_instances_view` to cap effective slots — these are
    learned state, not tuning knobs.
    ⚠ **This number is NOT the safety property, and treating it as one failed twice.** Raising it
    20 → 35 did not stop the false positives: measured over the 18 most recent learns, 15 were on
    machines later observed running MORE concurrent tasks than the cap learned from them. The grace
    is a threshold on a predicate that conflates "still filling" with "over-packed" — the on-box
    launch gate has four TRANSIENT refusal reasons (`settling`, `cpu_load`, `gpu_util`, `vram`), and
    a box at 7 of 8 lanes is the most loaded it will ever be, so the last lane is the one most likely
    to be refused for load. The real protection is invariant 19h-2 below.
  - `overpack_probe_after_min = 240`, `overpack_probe_backoff_max_min = 1440`,
    `overpack_probe_enabled = true` (invariant **19i**, draft) — the recovery path for the above.
    These ARE tuning knobs, unlike the `overpack_cap_*` rows they govern. 19i also changes those
    rows from a bare int to `{"cap", "at", "backoff_min", "probed_at"}` **with bare-int reads kept
    working** (19i.e) — every row in the live registry is currently a bare int.
    ⚠ **19h-2 landed first and reads a BARE INT.** When 19i is implemented, `_overpack_cap`'s
    repair-on-read and `_learn_overpack_cap`'s evidence floor must be moved onto the dict form with
    it — they are the two writers of that row besides the probe.
  - `hard_cap_hours = 48` (raised from 9, 2026-07-09 — owner: "it's meant to catch extreme
    accidents", not bound normal-length runs; was an unexamined carry-over from
    `watch_and_pull.sh`, never one of the money-knob wizard's 6).
  - `provision_timeout_min = 15` (invariant 3d, bug 10; **lowered from 45, 2026-07-10 spend
    audit**). The original 45 was calibrated above `_provision`'s own worst-case legitimate wait
    (160 × 15s = 40 min polling for `actual_status == running`) — but that margin is unnecessary:
    `_provision` runs **inline** in the poll loop (invariant 5), so `reconcile` (also in the poll
    loop, single-threaded) can never observe a box *mid*-provision. A row still `provisioning`
    across polls is therefore always an orphan whose driving `_provision` died, and an orphan never
    comes up on its own (its worker was never shipped). 45 min just billed each orphan for 30
    surplus minutes; measured provision latency is <2 min median / ~4 min p90, so 15 catches
    orphans ~3× faster with a wide safety margin. Registries seeded on the old 45 default are moved
    to 15 by the guarded settings migration (see **Settings migrations** below).
  - `provision_boot_max_min = 21` (invariant 5, 2026-07-15) — how long `_provision` waits for a
    freshly-rented box to reach `actual_status == running` before abandoning it (replaces the
    hardcoded `range(160)` = 40 min). Set to the **empirically measured p99 boot time**: over 144
    successful boots the distribution was median 1.7 min / p90 4.2 / **p99 20.3** / max 29.9, so 21
    min abandons only the ~1.4 % slowest-booting boxes that would have eventually come up, while
    halving the worst-case wait. NOTE for the async-provision work: once `_provision` no longer runs
    inline, a `provisioning` row IS visible to `reconcile` every poll, so this boot-wait ceiling and
    `provision_timeout_min` (the orphan reaper) must be reconciled — the boot-wait must win for a box
    that is legitimately still booting, or a slow-but-good box gets reaped before it comes up.
  - `max_instance_dph = 0.08` (invariant **4f**, **2026-07-31 owner directive**, lowered from 0.40)
    — a GPU-CLASS ceiling, **overridable per task** via `resource_hint.max_dph`
    (`task_max_dph(task, settings)`; the hint may only RAISE the cap, never lower it, and a
    malformed hint falls back to the global). This line's workload does not use the GPU: across
    2381 `box_measured` samples GPU util was median 0% / mean 3.6% (79% of samples exactly 0%) and
    VRAM use averaged **0.27 GB of 13.6 GB rented — 2%**. A dearer card therefore buys nothing, and
    measured over 7 days the dear boxes were also the WORSE buy on the axis that matters: pooled,
    offers > $0.07/hr cost **1.91× more AND ever-ran a task only 24% of the time vs 54%** for the
    cheap ones. $0.08 is the knee — it retains 90% of the last 7 days' actual rentals while blocking
    exactly that class (Tesla V100 ×9, RTX 3090 ×6, Q RTX 6000 ×4); supply is not the binding
    constraint, decisions saw ~431 qualifying offers. The per-task override exists for jobs that
    genuinely need a 24 GB card. ⚠ `_offers()`'s SEARCH ceiling is `max(global, every PENDING task's
    own `max_dph`)` — otherwise a task that legitimately raised its ceiling could never see the
    offers it is entitled to; `place()` still applies the per-task cap, so a widened search never
    widens what an ordinary task may book.
  - `min_reliability = 0.90` (invariant 4e, **new 2026-07-10 spend audit**) — the minimum Vast
    host-reliability score an offer must clear to be rentable. Renting strictly-cheapest was
    selecting flaky hosts (measured over one campaign: GTX 1660 4/6 lost, GTX 1070 Ti 5/5 dead;
    ~12% of realized spend went to boxes that vanished or never ran a task). Read from the offer's
    `reliability2` (Vast's smoothed 0–1 score), falling back to `reliability`; an offer exposing
    **neither** is kept (fail-open — a missing metric must never starve the queue).
  - `stall_timeout_min = 90` (invariant 19, 2026-07-09 owner directive, raised from 60) — the
    real gap between checkpoint writes is update/step-count-gated (`eval_every`/
    `eval_interval_steps`/`eval_interval`), not wall-clock, so it varies by entrypoint. Measured:
    train_atari/train_minatar_multi/train_stream (`eval_every=400`) write every ~40-175s at the
    2.3-9.8 upd/s measured in `VAST-TEST.md`'s ROI matrix; train_dmc (`eval_interval_steps=
    40_000`) every ~4.6min at the ~145 steps/s measured in the m13-planet-h2h spec — both leave
    60min a comfortable 10x+ margin. train_chess (`eval_interval≈50-62.5k` plies, self-play) and
    train_minatar_dream (`eval_interval_frames=3_276_800`) have no equivalently measured
    steps/sec in this repo — 90min is cheap insurance for those two specifically, still trivial
    next to the 48h hard cap if a genuine stall takes 30min longer to catch.
  Cost-consolidation knobs (invariant 21/22):
  - `consolidate_enabled = True` — the poll-loop pass that drains a paid box whose whole load fits
    onto other stays-alive-regardless capacity (owned, or a box kept up by its own work — the
    collapse), so it goes idle → torn down. Off = the pre-existing behavior (paid boxes ride their
    work to completion).
  - `consolidate_min_remaining_min = 30` (invariant 21d) — don't drain a task within this of its
    `est_minutes × est_safety` window (near-done: the checkpoint + re-warm cost exceeds the savings).
  - `consolidate_vram_margin_gb = 1.0` (invariant 21c) — measured free-VRAM headroom a target must
    have beyond the drained box's footprint before a drain is initiated; only gates when both sides
    are measured (invariant 22), else slot-count governs.
  - `resource_measure_every_min = 5` (invariant 22) — how often to sample each live box's real GPU
    memory so packing/consolidation run on measured VRAM, not the static `vram_per_lane_gb` hint.
  **Settings migrations.** Defaults are written only for *missing* keys at first start, so lowering
  a default never touches an already-seeded registry on its own. A small allowlist of *guarded*
  migrations (`dispatcher._SETTING_MIGRATIONS`, each `(key, superseded_default, new_default)`) runs
  at every start: a row still holding the exact superseded default is moved to the new default; any
  other (operator-customized) value is left untouched. This lets a lowered default reach the live
  coordinator on the next `dispatch-restart` without a manual SQL edit and without clobbering a
  hand-tuned value. Current entry: `provision_timeout_min 45 → 15`.
- **`vastai` CLI output** (`show instances --raw`, `search offers … --raw`, `create instance`,
  `destroy instance --yes`, `show user --raw` for balance) — trust boundary: every JSON payload is
  parsed defensively; a malformed/errored CLI call aborts that poll iteration with an `event`
  (never a crash, never a guessed default). The `search offers` call passes `--limit
  offer_search_limit` (the Vast CLI default is 64, which — with the `-o dph_total` cheapest-first
  sort — hid all but the cheapest boxes). Offer records used for invariant 4a' must expose
  `gpu_ram` (GB) and `cpu_cores_effective`, both already used by the existing launch scripts;
  invariant 4e additionally reads `reliability2`/`reliability` (optional — fail-open when absent)
  and `gpu_name` (for the `gpu_deny` gate; a missing name never matches, i.e. kept).
- **`fleet/machines.deny`** — existing blacklist format, honored at offer selection;
  auto-appended on never-reached-`running` exactly as the cwm-era `launch_probe.sh` did
  (retired 2026-07-07; the behavior is normative here). **Split into two files (bug found live
  2026-07-09):** `fleet/machines.deny` itself is now a static, hand-maintained seed list
  only — never written by the daemon. Auto-learned entries (`_blacklist_and_destroy`) go to
  `<EXPERIMENTS_ROOT>/.dispatcher/machines.deny` instead (gitignored, shared fleet-wide like
  `runs.sqlite`/`.dispatcher.lock`). `load_deny_machines` unions both at read time. Root cause:
  the daemon always runs from the canonical (non-worktree) checkout, so every auto-append to a
  git-tracked path permanently dirtied it — `dispatcher_ctl.sh restart`'s `cmd_sync` refuses to
  fast-forward a dirty checkout (by design, to never clobber uncommitted state), so in practice
  every merged coordinator fix silently sat unused on `origin/master` until someone noticed and
  ran a manual `git pull`. Live incident: 26+ auto-appended entries accumulated over 2026-07-09
  alone, meaning this had already defeated the "just restart it" deploy workflow for hours.
- **`fleet/entrypoints.py`** — code-owned dict `name -> {argv, completion_artifact,
  resume_flag: str | None, live: bool, pip_extras}` (the "entrypoint table" referenced by the
  registry spec's `--print-run-identity` handshake and by invariants 9/16 here). Resolution is via
  `entrypoints.resolve(task_row)`, which prefers a task's self-describing `job_manifest_json`
  manifest (see `docs/specs/job-artifact-contract.spec.md`) and falls back to this named table —
  so a new job type needs no `entrypoints.py` edit or dispatch-restart. The uniform
  contract: a generic `--out DIR` and (if resumable) `--init-from FILE` CLI flag, plus
  `--print-run-identity`. **All 9 `src/native` trainers were adopted onto this contract
  2026-07-08** (`train_atari`, `train_minatar_multi`, `train_stream`, `train_craftax`,
  `train_minatar`, `train_grid`, `train_continuous`, `train_chess`, `train_dmc`) via a shared
  `run_identity.print_run_identity` helper — none of them changed default behavior
  when `--out`/`--print-run-identity` are omitted (the old env-var/hardcoded-path fallback is
  preserved as the fallback). `fleet/smoke_entrypoint.py` remains the synthetic
  stdlib-only entrypoint this feature's own tests/docker-integration-test/paid-smoke actually
  dispatch (implements the same contract plus `--fail-at N`/`--sleep-seconds N` test knobs,
  writes `out/ckpt_latest.pt` on a configurable cadence and `out/summary.json` on completion) —
  cheap and fast to exercise the dispatcher plumbing itself without a real trainer's runtime.
- **Spool task file** `task.json` (validated ON THE BOX by the worker — trust boundary):
  `{"task_id": str, "grp": str, "name": str, "argv": [str…], "env": {str: str},
  "est_minutes": int, "git_sha": str, "pip_extras": [str…], "resume_from": str | null}` —
  unknown keys → reject. `resume_from`, when non-null, names a file that must be shipped
  alongside `task.json` (invariant 16). `git_sha` is provenance only (may be `""` for a non-git
  job); it is no longer used for payload reachability — the shipped code is content-addressed by
  the working-tree snapshot's `code_hash` (see `docs/specs/code-snapshot.spec.md`).

## Output contract

- Registry rows/events driven through the task state machine; `instances.cost_usd` stamped at
  destroy/lost (`dph_usd × hours(created_at → destroyed_at)`).
- **Pulled artifacts** in `experiments/<grp>/<name>/` via the existing allowlist rsync pattern
  (TB events, `*.json`, `*.jsonl`, logs first; checkpoints last), plus `run_identity.json`
  (registry spec) written by the dispatcher at pull time.
- **`tasks.resume_checkpoint`** (registry spec schema addition) — local path to the most
  recently pulled checkpoint for a task, set by invariant 16 and carried across a
  `preempting → queued` or `infra_failed → queued` requeue.
- **Spool layout on the box** (`~/spool/`, i.e. `$HOME/spool` — these boxes always run as
  `root`): `incoming/<task_id>/{task.json, payload.tar.gz, READY[, resume.pt]}` → worker renames
  the whole dir to `active/<task_id>/` (atomic claim) → extracts payload to
  `active/<task_id>/repo/` and runs the task with `cwd=active/<task_id>/repo/`,
  `PYTHONPATH=src`, `--out ../out` appended (i.e. writes land in `active/<task_id>/out/`), and
  `--init-from ../resume.pt` appended iff `resume_from` was set and the entrypoint has a
  `resume_flag` (invariant 16) → touches `active/<task_id>/DONE` (exit 0), `FAILED_<rc>`, or
  `PREEMPTED` (invariant 17). Worker appends lifecycle rows to `~/spool/worker.jsonl`
  (`{"t", "task_id", "event": "claim|start|exit|reject", "rc", "detail"}`) and touches
  `~/spool/HEARTBEAT` every 60 s. The worker never deletes task dirs. The shipped payload is a
  single verified `bundle.tar` (sha256-integrity `manifest.json` + `code.tar.gz` + `task.json` +
  optional `resume.pt`, verified on the box before extraction) — governed by
  `docs/specs/task-bundle.spec.md`.
- Dispatcher exit status: runs forever until signaled; `--once` performs a single poll iteration
  and exits 0 (the fixture/testing entry point, like the supervisor's `--dry-run`).

## Public API

- CLI only:
  - `python fleet/dispatcher.py [--db experiments/runs.sqlite] [--once] [--dry-run]`
    (`--dry-run`: full decision pass, prints intended actions, no `vastai`/ssh calls, no DB
    writes). `make dispatch` runs it foreground; a VS Code task autostarts it in the devcontainer
    alongside TensorBoard/dashboard.
  - `python fleet/spool_worker.py --spool ~/spool` (started on the box at
    rent time via the shipped bootstrap, exactly how `sweep_supervisor.py` is started today).
    `--max-slots N` is accepted and ignored (invariant 8a).
  - `python fleet/register_owned_box.py --label L --host H [--port P] [--slots N]
    [--gpu-name G] [--no-bootstrap]` (invariant 20) — registers/updates a self-owned always-on
    box's `instances` row and deploys `spool_worker.py` to it; idempotent by `--label`.
- **Pure decision functions** importable by tests (hardware/API sampling injected, never called
  inside them):
  - `place(task, instances, offers, queue, settings, now) -> Placement(action:
    "pack"|"preempt"|"rent"|"hold", target, victims: list[task_id] | None, reason)` — `queue` is
    every other currently-`queued` task, needed for the backlog-bar test (4c/4d) and to find
    preemption candidates (invariant 17) that `task` itself doesn't shadow.
  - `slots_for_offer(offer, resource_hint, settings) -> int` — invariant 4a', pure. Returns **0**
    when the offer cannot host even one lane under the hint (fit filter, 2026-07-21); otherwise the
    lane count capped at `max_slots_cap`.
  - `lane_capacity(offer, resource_hint, settings) -> int` — invariant 4e (cores-per-$ ranking),
    pure: `slots_for_offer`'s lane count **without** the `max_slots_cap` ceiling, floored at 1. The
    denominator of the value-density rank; the rented `slots_total` still uses the capped
    `slots_for_offer`.
  - `demand_slots(task, queue, settings) -> int` — pure: the triggering task's `slots` plus the
    `slots` of every other queued task whose window fits a fresh box (the 4a0 ceiling) — the lane
    demand a new rental could absorb near-term. **Observability only** since 2026-07-21 (the
    `usable_now` log field); no longer part of the ranking.
  - `should_teardown(instance, open_tasks, now, settings) -> (bool, reason)`
  - `consolidation_drains(instances, settings, now, queued_tasks=None) -> [{instance_id, task_ids, targets,
    dph_reclaimed}]` — invariant 21, pure: given the post-placement instances view, the graceful
    drains that vacate a paid box onto other stays-alive-regardless capacity with room (slot +
    measured-VRAM) — owned, or a box kept up by its own work (the collapse). Empty when nothing is
    safely consolidatable. The impure `_consolidate` executes each via the shared
    `_evict_task_graceful` preempt path.
  - `retry_decision(task) -> "requeue"|"terminal"`
  - `reconcile(db_instances, vast_instances) -> [Divergence]`
  - `eligible_offers(raw_offers, settings) -> (mapped_offers, n_dropped)` — invariant 4e: maps raw
    `search offers` records to the placer's field set and applies the three quality gates
    (reliability floor, `gpu_deny` substrings, `min_cpu_cores_effective` floor); reliability is
    fail-open on a missing field. Pure; `_offers` wraps it around the CLI call (which now also
    passes `--limit offer_search_limit`).
  - `offer_counterfactual(raw_offers, chosen_offer, task, settings, demand=None) -> dict` —
    invariant 5c: observability-only. Given the RAW `search offers` set the placer just chose from,
    the offer we actually rented, the triggering task, and `demand` (`demand_slots`; defaults to
    `task.slots`, used only for the `usable_now` context field), returns a bounded summary of the
    offer we passed over (the single cheapest raw offer, if it was cheaper than the one we rented)
    and WHY we skipped it (`reliability_below_floor` / `gpu_denied` / `below_cores_floor` /
    `too_few_slots` / `worse_value_density` / `qualified_not_chosen`). Pure; changes no placement,
    only feeds the `offers_considered` event. Recomputes the quality gates +
    `slots_for_offer`/`lane_capacity` predicates over the raw set itself, so it does NOT depend on
    `eligible_offers`' return shape.

## Dependencies

- Run-registry schema + `run_identity` helper (its spec) — the only cross-feature contract.
- `sweep_supervisor.should_launch` (pure function, sweep-supervisor spec invariant 11e) — the
  worker reuses it verbatim for hardware-adaptive lane starts; `sweep_supervisor.py` itself is
  UNCHANGED by this feature.
- `vastai` CLI, `ssh`/`rsync` (proxy `ssh_host:ssh_port` endpoint preferred, with direct-ip
  fallback per invariant 9a — VAST-TEST.md), working-tree snapshot / `git archive` fallback
  (payload), stdlib only otherwise.
- Existing conventions carried over unchanged: working-tree snapshot payload (committed +
  uncommitted, `.gitignore`-aware; `git archive` fallback for pre-snapshot tasks),
  pip-install-by-name provisioning, allowlist rsync flags (`--inplace --append` for genuinely
  append-only files only — see invariant 9's two-pass note), `DONE`-marker discipline.

## Behavior & invariants

1. **Singleton:** an exclusive lockfile (`experiments/.dispatcher.lock`, flock) — a second
   dispatcher exits 0 immediately with a message. Restart at any moment is safe: all state is
   reconstructed from the DB + `vastai show instances` + spool pulls (invariant 3); the daemon
   holds nothing only-in-memory that money depends on. **Shared-root resolution (added
   2026-07-09, retrospective bug 7):** the default DB path AND the lockfile resolve against the
   MAIN checkout (`git rev-parse --git-common-dir`'s parent), not the invoking worktree — a
   dispatcher or `runq` started from any `worktrees/*` collapses onto the one shared
   registry and the one singleton lock. Per-worktree ROOT-relative defaults were the split-brain
   vector: each worktree silently got its own registry, its own lock, and its own fleet.
   **Self-healing daemon:** the daemon is kept alive by `dispatcher_ctl.sh`'s `_supervise` respawn
   loop — an unexpected exit is relaunched a few seconds later until `stop` drops a flag file the
   loop checks before respawning. The fcntl singleton lock prevents a SECOND daemon but does not
   revive a dead one; `_supervise` is what keeps it running (no cron/systemd).
2. **Poll loop** (every `poll_seconds`): reconcile → ingest worker state → pull completions →
   place queued tasks → ship placements → teardown. Each phase logs decisions as `events` with
   human-readable `detail`.
3. **Reconcile** (`vastai show instances --raw` is authoritative for money):
   a. DB instance in `provisioning|live|draining` absent from Vast → mark `lost`, stamp
      `cost_usd`, and every task on it in `claimed|shipped|running` → `infra_failed` →
      `retry_decision`.
   b. Vast instance whose label matches `runq_<task-id>` but is absent from the DB's instances
      **AND whose `<task-id>` exists in THIS registry's tasks table** (crash between create and
      insert — should be impossible under invariant 6, belt-and-braces) → **adopt** via an
      idempotent `INSERT … ON CONFLICT(id) DO UPDATE` (dispatcher.py:863-883): a terminal
      (`destroyed`/`lost`) row for the same instance can linger in `vastai show instances` from
      Vast API lag, so re-adopt the row in place — mark `live`, clear `destroyed_at`, refresh the
      connection/hardware fields, and leave `created_at`/`cost_usd`/**`slots_total`** untouched;
      event `adopt`.
      **`slots_total` is NOT a connection/hardware field and must never be refreshed from an
      instance record** (2026-08-03): it is derived from the OFFER at rent time (invariant 4a',
      `slots_for_offer`), and no `vastai show instances` record carries such a key — so refreshing
      it wrote the code default `1` over the correct sizing and capped a re-adopted box at ONE lane
      permanently, because `_effective_slots` only ever takes `min` of `slots_total` with the 19h
      overpack cap and the capacity window, and nothing recomputes it. Live: a 7.4c/hr RTX 3070
      sized at 8 lanes by its own offer packed one task instead of eight after a transient
      `vastai` failure marked it `lost` (see 3e) and the next poll re-adopted it. Where no prior
      row exists there is no rent-time sizing to preserve, so the box is adopted at **1 lane** —
      the safe direction for an unknown box — and the `adopt` event must say so.
      **A
      `runq_*` box whose task-id is NOT in this
      registry is treated as `foreign_instance` (never touched)** — revised 2026-07-09 after
      the split-brain incident (retrospective bug 7): such a box belongs to a DIFFERENT
      registry's live campaign, and the original unconditional adopt (which stamps
      `hard_cap_at = now`, i.e. destroy-on-next-teardown) had concurrent per-worktree
      dispatchers shooting each other's freshly-rented boxes down within a minute of creation.
      Genuinely orphaned boxes with an unknown task-id (a deleted registry) are reclaimed
      manually (`vastai destroy`) — rarer and cheaper than auto-killing live cross-registry
      work.
   c. Vast instance WITHOUT a `runq_*` label → emit one `foreign_instance` event per contiguous
      sighting and NEVER touch it (manually launched boxes coexist, e.g. the
      `launch_m11_atari.sh` pattern).
   d. **Provisioning-zombie reaper** (bug 10, 2026-07-09 — observed live twice, once caused by
      `dispatcher_ctl.sh restart` itself): a DB instance in `provisioning`, still present on Vast
      (not case a's `lost`), whose `created_at` is ≥ `provision_timeout_min` old → **reap**: emit
      `teardown` (reason `stuck_provisioning`), `vastai destroy`, stamp `cost_usd`. A daemon
      killed or restarted mid-`_provision` (the boot-poll/ssh-probe/rsync/worker-start sequence,
      invariant 5) leaves the row stuck in `provisioning` forever — it's on Vast so case (a)
      never fires, and nothing else ever revisits a `provisioning` row (invariant 11's teardown
      only scans `live`). No task-side cleanup is needed: `_rent` never sets a task's
      `instance_id` until a later `pack`, so the triggering task (if still queued) was never
      taken out of `queued` and is simply re-placed on the next poll. Live incidents: instance
      40000008 (~1.5h × $0.055 leaked) and 40000007 (165 min stuck, zero disk usage — `_provision`
      never even created `~/spool`) before manual `vastai destroy`.
4. **Placement** (`place`, run per queued task in `priority DESC, created_at ASC` order; placing
   one task updates the working view of instance/queue state before the next task in the order
   is considered — committing capacity to task N changes what's feasible for task N+1):
   a0. *Never-satisfiable window* (retrospective bug 9, 2026-07-09) — checked before every other
      step: if `T.est_minutes × est_safety + pull_margin_min > hard_cap_hours × 60`, NO instance
      can ever pass (a)'s window check — `hard_cap_at` is stamped `now + hard_cap_hours` at rent
      (invariant 5) and only shrinks — so pack, preempt, and rent are all pointless. → *hold*
      with reason `infeasible_est` naming both knobs. Without this pre-check the loop rents a
      box it can then never pack: live incident — three `est_minutes=480` tasks (window 610 >
      ceiling 540) churned rent → 10-min idle → teardown → re-rent for ~90 minutes, billing
      idle boxes the whole time, with no `hold` event ever explaining why.
   a. *Feasibility* of instance I for task T: `I.state == 'live'` and I is not 10d-quarantined and
      free slots ≥ `T.slots` (registry invariant 8) and the window fits:
      `T.est_minutes × est_safety + pull_margin_min ≤ minutes(now → I.hard_cap_at)`.
      The quarantine test lives in `_fits_now`, so the backlog test (`_infeasible_everywhere`) and
      `_soonest_wait` agree with the pack filter — otherwise the fleet would count an undeliverable
      box's free slots as incoming capacity and decline to rent the box that could run the work.
   a'. *Slot sizing* (`slots_for_offer`, used at rent time — invariant 5 — to populate
      `instances.slots_total`, replacing a flat global guess): let `(vram, cores)` be the task's
      own `resource_hint` if it declared one, else the global `(vram_per_lane_gb,
      cores_per_lane)` setting. `slots_total = min(floor(offer.gpu_ram_gb / vram),
      floor(offer.cpu_cores_effective / cores))`, capped at `max_slots_cap`; **0 if the formula
      rounds to 0** (fit filter, 2026-07-21 owner directive — replaces the old "floor up to 1").
      An offer that cannot host even ONE lane under the hint must be dropped by 4e's fittability
      filter, not floored into a box that under-provisions every lane it runs. Live incident,
      2026-07-20/21: a 1.71-effective-core Titan Xp slice was rented 4+ times for
      `cores_per_lane=4` tasks — billed full dph while giving each lane under half the cores its
      own hint declared, on a CPU-bound workload. The result is a placement-time capacity
      *assumption*; the worker's real-time `should_launch` gate (invariant 8) is the actual
      safety valve and can still refuse to start a lane even when `slots_total` says there's
      room.
   b. *Pack* into the feasible instance of **lowest marginal dollar cost** for this task —
      `pack_cost(T, M) = M.dph_usd × max(0, window(T) − paid_remaining(M)) / 60`, where
      `paid_remaining(M) = max over M's current occupants of remaining_est` (0 if M is idle) is how
      long M is already committed to bill for other work. A **free box** (`dph_usd == 0`, e.g. an
      owned home box — invariant 20) is therefore always `$0` and wins outright; a **paid box** costs
      only the box-time this task would ADD beyond what M is already paying to run — so riding a spare
      slot on a box that stays alive for a longer-running occupant anyway is also `$0` (truly
      marginal). Ties (all-equal cost — the common all-free-or-same-lifetime case) break to **operator
      pack preference (higher first, invariant 4b′ below), then fewest free slots (tightest fit), then
      lower instance id**, preserving the old fragmentation-minimizing consolidation among equal-cost
      boxes that carry no preference.

      b′. *Pack preference* (2026-08-09) — an OPTIONAL per-box integer, `settings.box_preference_i<id>`
      (default 0 ⇒ inert), surfaced by `_instances_view` as `pack_preference` and read by
      `_box_preference`. It orders boxes that have **already tied on cost**, and is therefore a
      tie-break, never an override: a preferred PAID box can never beat a `$0` one, so the knob can
      never cause spend. **The defect it exists to fix:** every free box ties at `$0`, so tightest-fit
      is what actually chooses among them — and it ranks a box by how FULL it is, meaning a wholly idle
      box sorts LAST among its equals and the larger it is the longer it stays idle. A newly joined
      owned box is therefore the last one the fleet will ever pack onto, which is the opposite of what
      adding capacity is for. Measured on `tower` (i9-13900K, 24 slots, joined 2026-08-09): it
      lost every tie to a `laptop-gpu` holding running work, so the laptop would fill to its cap
      first — while `lane_scaling_bench` measured the new box at **2.5–2.8× an ordinary rented box's
      aggregate throughput** at `$0`. Tightest-fit's own justification does not reach this case: it
      minimises fragmentation so a PAID box can be emptied and torn down (invariant 21), and an owned
      box is never torn down, so there is nothing to consolidate it toward. The preference is
      hand-set policy, like the capacity-window cpu/vram fractions — the fleet learns footprints and
      caps, but which machine the operator would RATHER use is a fact about their hardware, not
      something to infer. Set with `register_owned_box.py --prefer N`; **`self.settings` is read once
      at daemon start, so it needs a `make dispatch-restart` to take effect.** This replaces the previous unconditional tightest-fit,
      which ignored price and so packed onto paid boxes ahead of an idle free one (observed live
      2026-07-17: a freed task cascaded onto a paid RTX 3060 while the `$0` laptop sat empty). Packing
      is always allowed regardless of budget — it never rents, only fills already-live capacity. The
      cost is a marginal-`$` *heuristic* keyed on the same `est_minutes × est_safety` occupant
      estimates the backlog/preempt paths already trust; a free box is exactly `$0` (no estimate
      risk), so it is robustly preferred over any paid box that would require real lifetime extension.
   c. *Backlog-bar patience* (replaces a flat renting timer): applies only when at least one
      instance is already `live`/`provisioning`/`draining` — bootstrapping the very first box
      always proceeds straight to (e), no patience or backlog test. Otherwise:
      - `wait` = the soonest an instance would free enough capacity for T: among instances whose
        running task(s) have combined slots ≥ T's shortfall AND whose window (a) still passes
        after the wait, the minimum `remaining_est(T') = max(0, T'.est_minutes × est_safety −
        minutes since T' entered running)`; `None` if no instance ever qualifies. **A running task
        overdue by more than `rent_patience_min`** (its `est_minutes × est_safety` elapsed more than
        `rent_patience_min` ago) is **excluded from this set** — its estimate has expired, so its
        true finish time is unknown and it cannot be treated as "freeing soon." A box whose
        remaining (non-expired) running tasks no longer cover T's shortfall simply doesn't qualify,
        so `wait` falls back toward `None` and placement proceeds to the backlog/rent path. Without
        this, a fleet whose slots are all held by chronically-overdue long-runs (their
        `remaining_est` clamped to 0) reports "a slot frees in 0 min" forever and holds a real
        backlog indefinitely instead of renting — observed live 2026-07-15: 8 tasks queued 48 min
        behind 21-hour runs (est 188 min) on a 2-box, 0-free-slot fleet with 0.12/1.00 budget used.
      - If `wait is not None and wait ≤ rent_patience_min` → *hold* with reason
        `slot_freeing_soon` (cheaper to wait a short while than pay for a new box). A slightly
        overdue occupant (within `rent_patience_min`) is kept — it is probably genuinely about to
        finish (its estimate was merely a little low), so holding briefly is still the cheap choice.
      - Else compute the *backlog*: among every task in `queue ∪ {T}`, those also infeasible on
        every live instance right now — `backlog_tasks` = their count, `backlog_task_minutes`
        = Σ `est_minutes × slots`. If `backlog_tasks ≥ backlog_min_tasks OR
        backlog_task_minutes ≥ backlog_min_task_minutes` → proceed to (e). Else → *hold* with
        reason `backlog_too_small` — this hold has **no timeout**; it is re-evaluated every
        poll and only resolved by the backlog growing, a slot freeing, or (d) firing.
   d. *Priority preemption* (invariant 17 owns the full mechanics): checked BEFORE (c)'s backlog
      test is ever evaluated. If T has an eligible preemption candidate anywhere → action
      `preempt` (tried before renting — it's free; T never sees a backlog/patience hold at all).
      Else, if a preemption already in flight (invariant 17d — a `preempting` occupant not yet
      vacated) alone covers T's shortfall → *hold* (`preempt_wait`); never re-preempt someone
      else or rent while that's still resolving. Only once NEITHER applies, if (c) would
      otherwise resolve to `hold(backlog_too_small)`, that hold is bypassed straight to (e) rent
      iff T's own `priority` is above the schema default (50) — an operator who explicitly
      raised a task's priority is asking it not to sit in a small-backlog hold even when
      nothing turned out to be evictable.
   e. *Rent* iff ALL of: `runq rate` + offer dph ≤ `max_hourly_usd`; Vast balance >
      `balance_floor_usd`; a `search offers` result exists with `dph_total ≤ max_instance_dph`,
      machine not in `machines.deny`, **reliability ≥ `min_reliability`**, **`gpu_name` not matching
      any `gpu_deny` substring**, and **`cpu_cores_effective ≥ min_cpu_cores_effective`** (the three
      `eligible_offers` quality gates — an offer failing any is dropped from the candidate set
      before the ranking; an offer exposing no reliability field is kept, fail-open),
      **and the triggering task fits the box the offer would become** (retrospective bug 13,
      2026-07-12): `slots_for_offer(offer, T.resource_hint, settings) ≥ T.slots` — an offer whose
      resulting `slots_total` is below the task's own slot claim is dropped from the candidate
      set before the ranking, exactly like the quality gates. This is the
      slots-axis twin of invariant 4a0 (bug 9's window axis): without it, a task whose `slots`
      exceeds the cheapest offer's capacity rents a box it can then never pack — live incident,
      2026-07-12: two `slots=7` tasks (sole-tenancy claims against VRAM-oversubscribed packing)
      rented four 2–4-slot boxes in ~10 minutes, each box idling toward teardown → re-rent,
      billing the whole time, with no hold event explaining why.
      Query defaults are otherwise inherited from the retired cwm-era `launch_sweep.sh`.
      **Cores-per-$ ranking (2026-07-21 owner directive — supersedes the 2026-07-21-AM demand-capped
      $/slot-hour rule, which degenerated to raw price on a short queue and so kept booking weak
      single-slot boxes):** among the qualifying candidates the winner minimizes
      `dph_total / lane_capacity(offer)`, where `lane_capacity(offer) = max(1, min(⌊gpu_ram_gb /
      vram_per_lane⌋, ⌊cpu_cores_effective / cores_per_lane⌋))` under T's hint — i.e.
      `slots_for_offer` **without** the `max_slots_cap` ceiling and **without** any demand cap. This
      is value density: for the (CPU-bound) workload, more effective cores per dollar = more
      per-lane CPU headroom = each lane runs faster, so a core-rich box is genuinely better even
      when only a few lanes run today. Ties break to lower `dph_total` (less absolute burn), then
      lower offer id (determinism). The budget gate (`rate + dph ≤ max_hourly_usd`) still applies to
      the winner's FULL `dph_total`; the rented `slots_total` still comes from `slots_for_offer`
      (cap applied). `demand_slots(T, queue, settings)` (= T.slots + Σ slots of OTHER queued tasks
      whose window fits a fresh box) is **no longer part of ranking** — it is computed only for the
      `usable_now` field of the rent-event log/counterfactual (near-term absorbable lanes). Removing
      the demand cap is deliberate: the whole complaint was that a short queue hid hardware quality;
      renting one good multi-lane box and packing later tasks onto it (invariant 5c's
      over-provision hold prevents a rent-per-task storm) is the intended outcome, and idle-teardown
      bounds any waste. Live incident motivating this, 2026-07-21: on a near-empty queue the demand
      cap made `dph/usable` ≡ raw dph, so a 5.25¢ 9-core GTX 1080 was booked over a 5.47¢ 24-core
      RTX 3060 Ti (0.58¢ vs 0.42¢ per lane); under cores-per-$ the 3060 Ti wins.

      **Invariant 4f — per-core SPEED in the ranking (2026-08-04).** The 4e key above prices lane
      COUNT and is blind to lane SPEED: two 24-core offers at the same price rank identically even
      when one's cores are half as fast. The winner therefore minimizes
      `dph_total / (lane_capacity(offer) × offer_speed_factor(offer))`, where
      `offer_speed_factor = clamp((cpu_ghz / cpu_ghz_reference) ** cpu_speed_weight, *cpu_speed_clamp)`
      and is **1.0 (neutral) whenever the offer publishes no usable `cpu_ghz`** — fail-open, the same
      idiom as the reliability and RAM gates, because a renamed marketplace field must never starve
      the queue. `cpu_speed_weight = 0` restores the pure 4e key bit for bit.

      *Grounded in measurement, 2026-08-04.* Across 4,987 `box_measured` samples GPU utilisation is
      **0% at both p50 and p90** (above 20% in 0.1% of rented-box samples) with **0.00 GB VRAM used**
      against the 0.6 GB/lane declared, so the fleet is renting GPUs for a workload that never touches
      one. Consistent with that, GPU model does not predict realised throughput — normalising each
      run's measured online steps/sec against its own group's rented-fleet median over 608
      TB-instrumented runs gives RTX 3060 1.00, RTX 3090 1.00, RTX 3060 Ti 1.08, RTX 2080 Ti 0.96;
      the only class that stands out is Tesla V100 at 1.48, and those are datacenter hosts whose
      distinguishing feature is the CPU. The two owned boxes differ by 2.1× from each other
      (laptop-gpu 2.39 vs desktop 1.14 on the same normalisation) at comparable co-residency, which
      is per-core speed and nothing else. `gpu_deny` was already reaching for this signal by proxy
      (its own note: "old GPU → old/slow CPU on the CPU-bound workload"); 4f reads the CPU fields the
      marketplace publishes instead of inferring them from the card.

      *Honest limit, and why it is CLAMPED.* `cpu_ghz` is base clock, not throughput — it ignores IPC,
      so a 3.0 GHz Xeon E5-2686 v4 and a 3.0 GHz modern Ryzen score identically here and do not
      perform identically. It is a PRIOR, bounded by `cpu_speed_clamp` (default `(0.75, 1.35)`) so it
      re-orders offers of similar price but can never override the cores-per-$ or reliability
      decisions that ARE grounded in measurement — in particular a fast 2-core sliver still loses to
      a slower core-rich box. The intended long-run signal is realised steps/sec per CPU model, which
      is why `eligible_offers` now carries `cpu_ghz`/`cpu_name`/`cpu_arch` through and `rent_created`
      records them as a flat parseable tail (not only inside the offer repr): re-fit the weight from
      that accumulated history rather than widening the clamp on intuition. `offer_counterfactual`
      ranks on the SAME key, or its `worse_value_density` / `qualified_not_chosen` verdicts would be
      measured against a ranking the placer never ran.

      Else → *hold* with reason `budget|balance|no_offer`, task stays
      `queued`; at most one hold event per contiguous hold period per task per reason (the
      supervisor's `hold` discipline). If the reliability floor empties the candidate set while raw
      offers existed, the resulting hold reason is `no_offer` (same as a genuinely empty search) —
      a persistent `no_offer` hold with live budget is the signal to lower `min_reliability`.

   g. **Invariant 4g — SIBLING CO-LOCATION (2026-08-16, owner directive).** `resource_hint.colocate`
      is a free-form GROUP KEY. Every task carrying the same key runs on the same box; **the
      coordinator picks which box**, and nothing about the key is otherwise interpreted.

      *Why it is not `--box`.* A paired comparison is only readable if its arms ran on the same
      machine — the box selects the attractor on a bistable rung (results bit-identical within a
      box, ~1e-3 across), so an arm on box A against a control on box B is not a paired
      measurement, and a control that collapsed on the other machine MANUFACTURES a win. The only
      instrument for this was `--box`, which is wrong twice: it makes the OPERATOR choose the
      machine (a placement decision the coordinator is better at, and one that goes stale as the
      fleet changes) and it is all-or-nothing — pinning a campaign to one named box pairs seed 1's
      arms with each other by also pairing them with seed 2's, serialising work that never needed
      it. And `runq sweep` had no `--box` at all, so **every multi-arm sweep was a box lottery by
      construction**; a published campaign co-located only 2 of its 3 pairs with nothing in the
      outputs saying so. What the science needs is narrower: *co-locate these N tasks WITH EACH
      OTHER, wherever you like.* Arms `1..n` of one seed share a key and a box; other seeds carry
      other keys and stay free to spread across the fleet.

      g1. *The pin.* `colocations(key, instance_id, pinned_at, pinned_by)` (registry schema v5)
      records the box a group is bound to. It is written by the member that actually **CLAIMS** a
      box — never by the placement DECISION, which can lose its CAS — and `INSERT OR IGNORE` +
      re-read makes it first-writer-wins, so a race cannot split a group. `_place_queue` resolves
      pins ONCE per poll, BEFORE placing anything, and stamps each pinned member's
      `resource_hint.box` **in the task view only** (never written back to the row — the row is the
      queue-time contract, same rule as invariants 24/26). A pin created mid-pass is stamped onto
      the siblings still ahead in that pass: placing task N mutates the capacity view task N+1 sees,
      so without that, tightest-fit splits a pair inside a single poll with both decisions looking
      locally reasonable.

      g2. *A pinned member is a box-targeted task, deliberately reusing 4f.* It boards only its
      group's box (`_boardable`), it **never rents** (4e0 — no rental can BECOME that box; renting
      for it is `_boardable` incident 2, three boxes in 16 minutes and none used), and it does not
      inflate any other task's backlog bar. Re-implementing those four agreements for a second kind
      of target is exactly the mistake `_boardable`'s docstring records three incidents of. Its hold
      reason is `colocate: …`, not `box_target: …`, so a coordinator-chosen wait is never read as an
      operator's typo.

      g3. *The FIRST placement admits against the WHOLE group's demand* (`colocate_group_slots` =
      Σ slots of that key's queued members). The pinning member is the only one whose choice is
      free, and tightest-fit would spend it on the box with one free lane — correct for a lone task,
      ruinous for a group, because arms 2..n are then serialised behind a decision made by a rule
      that could not see them. Both the pack filter and the rent filter use group demand, and both
      **fall back to the single-task demand** when nothing fits it. That fallback is what makes a
      group unable to deadlock: co-located-and-serialised is a correct outcome; a permanently held
      queue is not. Falling back is stated in the `pack`/`rent` reason (`SERIALISE`).

      g3'. *A PREEMPTED member returns to its group's box.* `preempt_requeue` clears `instance_id`
      and requeues, and today that arm relocates wherever the packer next puts it — silently
      breaking a pair mid-campaign, and doing so on the arm that ALSO carries `resumes > 0`. With a
      live pin it re-boards the same box, so co-location now survives preemption rather than being
      undone by it. If instead the whole box is drained, every member holds, the box dies, the pin
      is released (g4) and the group re-pins TOGETHER on the next poll — it moves as a unit.

      g4. *A pin on a box that is no longer `provisioning`/`live` is RELEASED*, and the group
      re-pins to wherever its next member lands. The alternative — hold forever for a box that was
      torn down on idle or at the hard cap — turns every reclaimed rental into a wedged campaign
      only a human can notice. This is BEST-EFFORT by construction, and the release is logged
      (`colocate_unpinned`) because it is the exact moment co-location BREAKS.

      g5. *Therefore the AUDIT is part of the feature, not an extra.* "I asked for co-location" is a
      weaker claim than "the arms were co-located", and only the second licenses a paired verdict.
      `runq colocate --verify` exits **non-zero** on any group spanning two boxes (`SPLIT`); `OK` =
      every member ran on one and the same box, `PENDING` = nothing started yet. A check that can
      fail for the reason it names. ⚠ It reads the distinct `start` EVENTS
      (`registry_db.boxes_started_on`), never `tasks.instance_id` — that column holds only the
      LATEST placement, so a requeued arm reads as if it had only ever run on its second box, which
      hides the split exactly where it is real (that arm is also the one with `resumes > 0`).
      `paired_seed_diff.boxes_for` paid for this lesson; both callers now share the one function.

      g6. *`box` and `colocate` are mutually exclusive*, refused at `runq add` on the FINAL hint (a
      config's `resources.box` counts). They are opposite requests; any precedence rule silently
      makes one of them a no-op, which is the class of failure this invariant exists to remove.

   i. **Invariant 4i — FORCED BOX PLACEMENT (2026-10-02, owner directive).** Owner, verbatim:
      *"Implement the bypass in the coordinator. This should just be something like: queue this task
      on this specific box, ignoring other constraints."* `resource_hint.force_box: true` on a task
      that also names a `box` places it on that box as soon as the box can take delivery, IGNORING
      every resource and time-of-day ADMISSION gate. It is the per-task form of "the owner says run
      it there now": the motivating case is a job that needs a whole owned GPU at an hour when the
      box's capacity schedule (box-pause inv. 17–20) would admit nothing that size.

      ⛔ **It is box pinning, and the strongest form of it** — the project's working rules § Box pinning: only
      with the owner's direct authorization. `runq add --force-box` says so in its help text.

      i1. *The hint, and who may carry it.* A task is FORCED iff its hint has `force_box` **literally
      `true`** (a string or a number is not a force), an explicit `box`, and **no** `colocate`; and
      every instance the `box` names is `source='owned'`. Checked at THREE trust boundaries, each on
      the FINAL hint:
        - `runq add`/`submit` (`--force-box`, or `resources.force_box` in the config's `job`
          section): exit 2, nothing queued, when `force_box` is not a boolean, when there is no
          `box`, when `colocate` is also set, or when the named box is not a registered owned box.
          Under `RUNQ_TRANSPORT=api` the client runs the three structural checks and leaves the
          registry check to the coordinator — the registry of record is the coordinator's, and a
          remote client may hold no copy of it.
        - the coordinator API (`POST /v1/submissions`): the same four refusals → 422, nothing
          written (one bad cell refuses the whole submission, remote-submit inv. 15).
          `resource_hint_json` is an opaque string to the envelope allowlist, so the key survives
          the transport by construction; the server re-validates because a client is not trusted.
        - `place()`: a hint that slipped through anyway (a hand-edited row, a rental's id) is simply
          **not forced** — it is placed as the ordinary box-targeted task it otherwise is (4e0).
      It never causes a rental (a forced task is box-targeted, so 4e0 already holds instead of
      renting) and it **never preempts or evicts** an occupant, whatever `preempt_enabled` says.

      i2. *What is IGNORED.* For a forced task, on its named box only: the free-slot check — i.e.
      the nominal `slots_total`, the learned over-pack cap (19h) and the time-of-day capacity slot
      cap (box-pause 18), which `_effective_slots` composes into one number; the capacity-window
      budget (box-pause 18a, `_budget_fits`); measured headroom (box-pause 23, `_headroom_fits`);
      and the over-pack cooldown (19h-3), which is a consequence of the same learned-concurrency
      machinery rather than an operator switch.

      i3. *What is RESPECTED — box STATE, never bypassed.* The box must be `live` (so `paused` —
      the owner's explicit switch — `unreachable`, `provisioning` and `draining` all hold the
      task), and not ship-quarantined (10d), drain-held (21h) or worker-roll-held (R3). The 4a
      window check (`est_minutes × est_safety + pull_margin_min ≤ minutes to hard cap`) and 4a0
      (`infeasible_est`) are unchanged, as is the `requires_gpu` filter in `_boardable` (a forced
      GPU task does not board a box registered without one). While it cannot place, the hold reason
      starts `force_box: waiting for box '<box>'` and NAMES the state that is holding it, so a
      forced task waiting on a paused box is not mistaken for a broken bypass.
      Est/infeasibility, stall detection and the retry rules are unchanged.

      i4. *Everyone else is unaffected.* An unforced task is gated exactly as before. A forced
      occupant is an ordinary occupant in the instance view: its slots, its declared footprint
      (18a) and its measured usage (23) count against every later admission, within the pass and
      across polls. So a forced whole-card job makes the box refuse other GPU work by the normal
      arithmetic — the bypass is for ONE task, it is not a raised cap.

      i5. *Never shed by a window flip — and never the cause of a shed.* `_reap_over_capacity`
      (box-pause 19) leaves a forced occupant OUT of its arithmetic entirely, regardless of
      `preempt_enabled`: it is not shed, and its footprint is not charged. Charging it would make
      the box's existing occupants read as over the cap the poll after it arrived and evict them,
      which is the eviction i1 forbids; so the other occupants are judged against the window
      exactly as if the forced task were absent. (i4 is about ADMISSION, where its footprint does
      count.) A hard pause (`_signal_drain`, box-pause 12) still evicts it — pause is the owner's
      switch.

      i6. *The host's hard caps lift while it is on the box* — box-pause inv. 20e.

      i7. *The on-box launch gate is bypassed too.* Placement is not launch: `spool_worker`'s
      `launch_ready` holds a delivered task behind `should_launch`'s
      settle / `cpu_load` / `gpu_util` / `vram` arms, and 19h then requeues a task held there past
      `ship_launch_grace_min` — which for a forced task would be re-placed on the same box next
      poll, a requeue loop that also ratchets the box's learned cap down. So:
        - `_build_task_json` sets `env.RUNQ_FORCE_BOX = "1"` for a forced task. `env` is an
          existing `task.json` key, so a worker older than this invariant accepts the task (an
          unknown top-level key would be REJECTED — the self-update-window hazard) and merely
          passes the variable to the trainer.
        - A worker that knows the marker launches a forced pending task FIRST and skips the
          `should_launch` checks for it, logging one `launch_forced` line with the
          live-lane count it ignored. `~/spool/FREEZE` (soft pause, box-pause 9) still holds it.
        - `_reap_overpacked_boxes` (19h) never counts a forced task as gate-held: it is neither
          unscheduled nor evidence for a learned cap. Under a pre-4i worker it therefore waits in
          `shipped` for the box's own gate rather than looping.

      i8. *Audit — a bypass must be visible.* A forced claim logs a `forced_placement` event
      (task + instance) whose detail lists every gate that WOULD have refused it with the numbers
      that decided each (`slots: free 0 < need 1 …`, `budget (18a): …`, `headroom (23): …`,
      `overpack_cooldown`), or says that none refused. It is logged only when the claim's CAS
      succeeded — the decision is not the claim. The `claim` event's reason starts `forced_box:`.
      `runq show <task>` prints both.

      i9. *Fail-closed defaults.* Absent, `false`, or non-boolean `force_box` ⇒ the task is not
      forced and every pre-4i behaviour is bit-for-bit unchanged; `place()` reads nothing new for
      it.
5. **Rent sequence** (crash-safe money ordering): event `rent_intent` (with offer JSON, before
   any spend) → `vastai create instance` → insert the `instances` row using the id Vast just
   returned (state `provisioning`, label `runq_<task_id>`, `hard_cap_at = now + hard_cap_hours`,
   `slots_total` from `slots_for_offer` against the triggering task's `resource_hint`) → COMMIT
   → update ssh fields; then provision exactly as the retired `launch_sweep.sh` did (poll
   `actual_status`, blacklist-and-destroy on stuck image pull, ship bootstrap, pip install,
   start `spool_worker.py` — with no lane count, invariant 8a) → state `live`. **The ssh probe that follows
   `actual_status == running` is RETRIED** `ssh_probe_attempts` times (default 5) with
   `ssh_probe_interval_s` (default 15) between tries before it's judged a failure: Vast flips
   `actual_status` to `running` when the *container* is up, but sshd (and the Vast ssh proxy) often
   aren't accepting connections for another 20–60 s, so a single 20 s-timeout probe fails on a
   perfectly good box. Because a probe failure runs `_blacklist_and_destroy` (permanent
   per-machine deny), a single-shot probe turned transient sshd-readiness blips into a doom loop —
   observed live 2026-07-15: **every** fresh rental failing `sshd unreachable/spool init failed`,
   98 machines permanently denied, the offer pool shrinking daily, and (once the rent path was
   un-starved — invariant 4c) zero fleet growth. The retry lets a genuinely-up box connect; a
   genuinely dead one still fails all attempts and is denied as before. `instances.id` IS the Vast
   instance id (schema, registry spec) so it cannot be chosen before `create instance` returns
   it — the crash window this opens (created but our process dies before the INSERT commits) is
   exactly what invariant 3b's `adopt` reconcile rule exists to close; the `rent_intent` event is
   the paper trail that a spend was committed even if the row itself never lands. Any failure →
   destroy if created, state `lost`/`destroyed`, task back through `retry_decision`.
5b. **Provisioning is ASYNC (incremental, non-blocking) — 2026-07-15 owner directive.** The original
   `_provision` ran INLINE in the poll loop, blocking every other stage (ship, reap, rent, teardown)
   for the whole boot (up to `provision_boot_max_min`). Live consequences 2026-07-15: a single slow
   box froze the daemon 20–40 min at a time, which STARVED the stall reaper (a stale trainer went
   unreaped 120+ min because no poll completed) and serialized all fleet growth. Redesign: `_rent`
   only inserts the `provisioning` row and returns; a new poll step **`_advance_provisioning`** moves
   each `provisioning` box forward by ONE non-blocking step per poll, reconstructing its sub-state
   from the DB (no in-memory boot state — invariant 1): `ssh_host` NULL ⇒ hasn't reached
   `actual_status==running` yet (one `vastai show` probe/poll); `ssh_host` set ⇒ running, so try the
   ssh probe + bootstrap + worker start ONCE (retried across polls, not an inline loop — subsumes the
   invariant-5 probe retry); success ⇒ `live`. A box past `provision_boot_max_min` is destroyed. This
   makes provisioning **restart-resumable** (a box mid-boot at restart is picked up from its DB
   sub-state, not abandoned) and never blocks the poll. **Timeout reconciliation:** because a
   `provisioning` row is now visible to `reconcile` every poll, the stuck-provisioning reaper
   (invariant 3d) must use `provision_boot_max_min` (21), NOT the smaller `provision_timeout_min`
   (15) — otherwise a slow-but-good box booting at 15–21 min is reaped before it comes up. (The old
   15 was correct ONLY because inline provisioning made a mid-boot box invisible to reconcile.)
   **Dud-denial preserved:** async must not lose the inline path's blacklisting. A box reaped at the
   boot ceiling that had REACHED running (`ssh_host` set) but never became usable has broken
   ssh/spool-init → its machine is DENIED (`_blacklist_and_destroy`). This is safe — unlike the
   invariant-5 single-probe doom loop, it fires only after ~21 min of across-poll probe retries, so a
   genuinely-good box is never denied. A box that NEVER reached running (`ssh_host` NULL) may be a
   transient slow image pull, so it is destroyed WITHOUT denial (a momentarily-overloaded host is not
   banned). Without this, async provisioning would churn duds every 21 min instead of denying them —
   observed live 2026-07-15 after a deny-list cleanup re-admitted known-bad GTX-1070-Ti-class hosts.
5c. **Over-provisioning guard (the async correctness requirement).** With inline provisioning, the
   poll blocked during each rent, which *accidentally* throttled renting; async removes that throttle,
   so without a guard the poll loop would rent one box per queued task per poll (dozens). Capacity
   already being provisioned MUST count. In `place()`, after pack/preempt but before the backlog/rent
   path, a queued task whose slots fit a `provisioning` box's incoming free slots (and whose window
   still clears that box's hard-cap) → **hold** (`awaiting_provisioning`, target = that box) instead
   of renting. To make this correct across many queued tasks in one poll, `_place_queue` RESERVES the
   incoming slot (appends an in-memory pseudo-occupant to that box) so the next task doesn't double-
   count it — the same in-poll mutation the pack path already does (bug fix 2026-07-09). And a box
   rented earlier THIS poll is appended to the in-memory `instances` view as a `provisioning` box
   (with one slot reserved for its triggering task), so subsequent tasks hold for it rather than
   renting again. Net: N queued tasks consume `ceil(N / slots_per_box)` boxes, and a box being
   provisioned from a prior poll is never re-rented for. If a provisioning box later fails to boot
   (destroyed at the ceiling), the tasks holding for it simply re-evaluate next poll and rent then —
   self-correcting, never stranded.
5d. **Offer-set counterfactual event (observability only, 2026-07-15; calibration Q2).** Every rent
   logs an `offers_considered` event (task_id = triggering task) alongside `rent_intent`, carrying the
   `offer_counterfactual(raw_offers, chosen_offer, task, settings)` summary as JSON. Today only the
   CHOSEN offer is logged (`rent_intent` detail), so "was there a cheaper box we passed over, and why?"
   is unanswerable after the fact; this event makes it answerable without changing any placement. The
   summary is **bounded** (never the full offer list): `{n_considered, n_qualifying, chosen:{dph, gpu,
   reliability, lanes, dph_per_lane, usable_now}, cheapest_alt:{dph, gpu, reliability, dph_per_lane,
   reason}|null, premium_dph}`. `chosen` is the rented offer; `cheapest_alt` is the single cheapest
   RAW offer *iff* it was strictly cheaper than `chosen` (else null → we rented the global cheapest,
   `premium_dph = 0`); `reason` classifies why that cheaper offer was skipped, in precedence order —
   `reliability_below_floor` (its `reliability2`/`reliability` < `min_reliability`), else
   `gpu_denied` (its `gpu_name` matches a `gpu_deny` substring), else `below_cores_floor`
   (`cpu_cores_effective < min_cpu_cores_effective`), else
   `too_few_slots` (`slots_for_offer` < `task.slots` — includes the fit-filter 0-slot case), else
   `worse_value_density` (2026-07-21: it qualifies but loses the 4e cores-per-$ ranking — its
   `dph / lane_capacity` exceeds the chosen offer's; the expected outcome whenever a core-richer box
   is worth its slightly higher price, NOT a bug signal), else
   `qualified_not_chosen` (a cheaper offer existed that also wins or ties per lane — a placer
   bug signal that should never occur, surfaced rather than hidden). `demand` (the placer's
   `demand_slots`; fail-open to `task.slots`) feeds only the `usable_now` context field, not the
   ranking. `premium_dph = max(0, chosen.dph − cheapest_alt.dph)` is the
   $/hr we paid for the reliability floor / sizing on THIS rent. The event is fail-open: computing it
   must never block or abort a rent (a malformed offer field → the event is logged best-effort or
   skipped, the rent proceeds). Because it is logged only going forward, it is answerable for **future**
   rents only; the consuming report (calibration §E) states coverage rather than assuming history.
6. **Every instance this feature creates carries a `runq_*` label** — the reconcile adoption and
   foreign-instance rules key on it.
7. **Ship** (task `claimed → shipped`): first, if the entrypoint declares `apt_packages`
   (retrospective bug 8, 2026-07-09), install them over ssh — dpkg-guarded (re-ships no-op),
   apt-update fallback, 300s budget; a failure is a ship failure and feeds invariant 9a's
   tracker. Dispatcher-side deliberately: `task.json`'s schema and already-deployed box workers
   stay untouched. Then build the code tar from the working-tree snapshot `runq add` persisted
   (committed + uncommitted, `.gitignore`-aware, content-addressed by `code_hash` — see
   `docs/specs/code-snapshot.spec.md`), falling back to `git archive <task.git_sha>` only for
   pre-snapshot tasks (queued before snapshots existed, or whose snapshot file is gone). If
   `task.resume_checkpoint` is set and
   the entrypoint's `resume_flag` is non-null (invariant 16), copy that local file into the same
   staging dir as `resume.pt` and set `task.json.resume_from = "resume.pt"`; else leave it null.
   The shipped payload is a single verified `bundle.tar` (sha256-integrity `manifest.json` +
   `code.tar.gz` [snapshot | compiled | git-archive] + `task.json` + optional `resume.pt`,
   verified on the box before extraction; Cython compile default-on, ed25519 signing opt-in) —
   governed by `docs/specs/task-bundle.spec.md`.
   rsync `incoming/<task_id>/` (payload + `resume.pt` if present, `task.json`, then `READY`
   last), 3 attempts with backoff, then CAS to `shipped`.
   **The push is RESUMABLE (`--partial --inplace`) — load-bearing, not tuning (2026-07-29).**
   `_run_or_timeout` SIGKILLs the whole process group at the 120s budget, and without `--partial`
   the receiver then discards its temp file, so all three attempts restart at byte 0 and a payload
   the link cannot move in 120s is **undeliverable forever**, however many polls try. Live:
   instance 40000013 degraded to ~70 KB/s (siblings measured ~550–615 KB/s from the same uplink at
   the same moment, so this was the box, not us); a 37.6 MB bundle needs ~9 min there. Observed
   `.bundle.tar.pXSN4I` reach 8.4 MB and vanish; every other `incoming/*/` on the box was empty.
   With `--partial` the interrupted destination survives and rsync's delta pass sends only the
   missing tail, so a slow-but-real link converges. Safe against a partial being mistaken for a
   delivery: the worker claims only on `READY` (pushed strictly after the payload push returns ok),
   the idempotency check below tests `READY`/`active/<id>` rather than bare existence, and the
   bundle's sha256 manifest is verified on the box before extraction.
7b. **One un-shippable box may not consume the whole ship pass** (2026-07-29). `poll_once` is
   serial and `_ship_all` walks every `claimed` task in `priority DESC, created_at ASC` — so a box
   we cannot reach is retried 3× per task, for every task packed onto it, AHEAD of everyone else's,
   because its tasks sort first precisely by having been stuck longest. Live: six tasks on one
   degraded box, 6m07s burned each (3 × 120s + backoff), **36.5 min of a single pass delivering
   zero bytes** while four tasks on two healthy boxes waited behind them — ~23s each once the loop
   reached them — and repeating every poll, since a ship failure leaves the task `claimed`.
   Therefore: the first attempt on a box that records a NEW transport failure retires that box for
   the remainder of the pass; its other claimed tasks are simply retried next poll. Cost of a dead
   box falls from `6min × its whole backlog` to `6min`, once. The trigger is
   `ConnectionTracker.consecutive_fails` **moving**, not `ok == False` alone, because `_ship` also
   returns False for TASK-level faults (a failed `git archive`, an entrypoint whose apt packages
   don't exist) that say nothing about the box and must not stall its siblings.
   **First-ship vs re-ship (a task can be shipped more than once — invariant 8's "the worker
   never deletes task dirs" means a REQUEUED task's dir from its earlier attempt is still on
   the box):** on the very first ship attempt for a task (no prior `ship` event exists for it),
   shipping is idempotent purely for dispatcher-restart safety — if `incoming/<id>` or
   `active/<id>` already exists, skip the copy and proceed straight to the `shipped` CAS. On any
   SUBSEQUENT ship (a prior `ship` event exists — this is a preemption or infra-failure
   requeue), that stale directory is actively removed first (`rm -rf`) before shipping the
   fresh payload: by this point everything of value has already been pulled home (invariant
   9c/17b), and treating the old attempt's `active/<id>` as "already delivered" would silently
   ship the FIRST attempt's `task.json` forever — dropping the resumed run's `resume.pt`/
   `--init-from` on every re-ship without ever surfacing an error.
7c. **A ship pass is BOUNDED even when every ship SUCCEEDS** (2026-07-29). 7b retires a box whose
   transport is broken; this is the complementary case — ship work that is merely slow and
   plentiful, which starves ingest just as effectively. `_ingest_and_complete` runs at the TOP of a
   serial `poll_once`, so an unbounded ship phase postpones the next one by its own full length,
   and that phase carries every `worker.jsonl` pull, every checkpoint pull, and the entire reaper
   layer. Observed live: **10 ships in 15 min with ZERO starts and ZERO dones**, `box_measured`
   27 min stale, both owned boxes' `worker.jsonl` 36-59 min stale.
   The driver is COMPILE, not transport: 89% of compiles are cache hits at ~10s, but **11% are
   MISSES at a median 155s (max 272s)** — the cache is keyed on the code snapshot, so with several
   worktrees queueing at once nearly every task is a distinct snapshot and pays one. Per-task ship
   cost is median 30s, p90 135s, max 366s.

   7c-i. **A compile-cache HIT was not free either, and half of it was pure redundancy** (measured
   2026-07-31, FIXED). "Cache hits at ~10s" above is accurate but was read for years as if ~10s were
   the irreducible cost of a hit. It was not: a hit still ran `overlay_entry_source`, which streams
   every member of the **36MB** compiled tree from `r:gz` into a fresh `w:gz` to swap ONE file, and
   gzip level 9 over 36MB is **11.55s measured** on the coordinator. Over 2 days: n=1076 hits at a
   median 10.86s = **196 min of `ship_budget_sec`**, and those 1076 hits spanned only **142 distinct
   commits** — so ~934 of them re-derived a tree byte-for-byte identical to one already built that
   day. A sweep of N cells at one commit on one trainer paid it N times.
   ⇒ `_shipped_tree` now caches the OVERLAID tree in `.dispatcher/ship-cache`, keyed by the compile
   key plus the entry path. That key is the complete input set (`overlay_entry_source` reads exactly
   `entry_rel` from a source tree the compile key already content-addresses), so it is stale-proof
   for the same reason the compile key is. Measured on the real 36MB tree: **11.33s → 0.02s**, and
   the decompressed tar is byte-identical across all 1446 members (the `.tar.gz` differs only in the
   gzip mtime header, which the box discards on extract). The `compile` event now carries an
   `overlay` field — `cached` (no gzip ran) | `overlaid-warm` (compile cached, this entry was new) |
   `compile-cold` — so a run of `overlaid-warm` reads as "(commit, entry) pairs are churning",
   not as a broken cache.
   **This is deliberately none of the (a)-(d) candidates in the compile cache-key Open question
   below.** Those all concern the MISS path and need an owner decision precisely because narrowing a
   key can ship STALE BINARIES; this adds a cache in front of a pure function of inputs already in
   the key, so it cannot, and it composes with whichever of (a)-(d) eventually lands.
   Cache caps went `24 → 64` per dir in the same change: 227 distinct snapshots over 3 days (~76/day)
   against a 24-entry cap evicted trees still in use, costing 18 commits a SECOND cold compile
   (22 rebuilds x ~171s = 63 min over 3 days). The two dirs are pruned INDEPENDENTLY so the cheap
   11.55s overlay entries can never evict the ~171s compiled trees they are built from.
   `ship_budget_sec = 300` caps the phase, keeping the whole cycle near the ~7 min p50 this fleet
   historically ran at. **At least one task always ships per pass** regardless of the budget — a
   single 366s task must not be starved by a budget smaller than itself, which would halt the queue
   permanently. Deferred tasks simply remain `claimed` for the next pass: no state change, no retry
   cost, and invariant 10d cannot mistake them for undeliverable because it additionally requires a
   recorded `ship_failed` for that task on that instance.
   **Time burned on a box that 7b RETIRES is credited back, not charged to the budget.** 7b's whole
   guarantee is that a dead box costs one attempt per PASS rather than one per task; charging that
   attempt to the budget hands the cost straight back to the healthy tasks queued behind it — and a
   degraded box's tasks sort FIRST (`priority DESC, created_at ASC`, stuck longest), so they get the
   charge every time. Measured regression from 7c's first revision: two attempts on one degraded box
   consumed the whole 300s, 7b retired it and 7c stopped the pass in the SAME instant, deferring 19
   healthy tasks that would each have taken ~20s — repeating every pass, with four tasks on the
   OWNED boxes stuck `claimed` for 33 min behind it. Bounded by construction: 7b admits at most one
   attempt per box per pass, hence at most one credit each. A bounded pass logs `ship_budget_spent`
   with the deferred count — silence would be indistinguishable from "there was nothing left to
   ship".

7h. **A BOX-REPORTED `start` IS ANCHORED ON `claim`, NOT ON THE `shipped` STAMP — AND THE OVER-PACK
   REAPER MUST NEVER UNSCHEDULE A TASK THE BOX IS RUNNING (2026-08-04).**

   `_apply_worker_state` accepts a `claim`/`start` row from `worker.jsonl` only if it belongs to the
   CURRENT delivery attempt — `worker.jsonl` is append-only across every attempt (a requeue reuses
   the task_id), so a stale row must never read as this attempt starting. That requirement is right;
   the reference point was wrong. It compared against `updated_at`, the moment the `claimed →
   shipped` CAS FINISHED.

   **That is a race, and the box wins it routinely.** The box claims and launches within SECONDS of
   the payload landing, while the dispatcher stamps `shipped` afterwards and in a per-pass BATCH. So
   `start` lands BEFORE `updated_at`, the row is rejected — and since the row never gets any newer,
   it is rejected **forever**. The task then trains on the box while the registry holds it `shipped`.

   **The damage is not staleness, it is destroyed work.** `_reap_overpacked_boxes` (19h) reads a
   `shipped` task older than `ship_launch_grace_min` on a box with running siblings as over-packed,
   so it requeues an ACTIVELY TRAINING task and `rm -rf`s its `active/<id>` — which is exactly what
   makes `reap_orphans` kill the trainer. Then it re-ships, the same race repeats, and the loop
   destroys work every grace window. This is the m49/m50/m54 silent-restart class again.

   **Measured 2026-08-04, live:** 11 tasks stuck in `shipped` simultaneously; every one carrying a
   `start` row had `start < updated_at` (gaps 8s-110s, five sharing one batched stamp of 21:14:01);
   and **60 of the last 60** over-pack unschedules were on tasks the box had CLAIMED AND STARTED,
   one of them 52 minutes into its run. It also explains the whole 19h thread: 99.6% of gate-refusal
   time is `settling` (the boxes were fine), the evidence floor kept refusing caps (the machines
   genuinely had capacity — they were RUNNING the work), and 19h-3's cooldown caught ~nothing
   because over-packing was never the trigger.

   a. **The anchor is `_attempt_started_at(task)`** — the task's most recent `claim` event, i.e.
      when THIS delivery attempt BEGAN. It is necessarily earlier than any start the box could
      report for this attempt (the box cannot hold a payload before we started sending it), and it
      preserves the stale-row property the guard exists for (a previous attempt's rows precede this
      attempt's `claim`). Falls back to `updated_at` when no `claim` event exists — the OLD
      behaviour, so the fallback can only be as wrong as before and never accepts a row it should
      reject, `updated_at` being the later of the two anchors.
   b. **Safety valve: `_box_reports_started`.** The reaper consults the box's own account before
      unscheduling, and skips (logging `overpack_skipped_running`) any task the box says it started.
      Fixing (a) removes the known cause of the divergence, but this reaper is where a divergence
      becomes IRREVERSIBLE, so it must not be the component that trusts the registry blindly. Read
      from the already-pulled local `worker.jsonl` — no extra ssh. An `exit` row clears the flag, so
      a task that ran and finished under an EARLIER attempt cannot shield a genuinely wedged
      re-ship, and an unreadable copy fails open rather than disarming the reaper.
   c. **The unschedule stays unconditional for a task the box never started** — that is 19h's real
      case and no other reaper covers a gate-held `shipped` task (19 ignores `shipped`, 10b is
      scoped to a box that never started ANYTHING, 10c needs a dead worker).

   ⚠ The general lesson, and it is the third time this shape has appeared here: **a timestamp
   comparison between two clocks/actors is a race unless one side is provably earlier by
   construction.** Anchor on the event that STARTS the window, never the one that closes it.

8. **Worker** (`spool_worker.py`): single process per box. Claim = atomic rename
   `incoming/<id> → active/<id>` (only after `READY` exists); reject invalid `task.json` with
   `event=reject` + `FAILED_validation` (trust boundary — never execute unvalidated argv).
   Launch gating: a new task starts only when `should_launch(...)` allows (imported pure
   function; hardware sampling identical to sweep-supervisor invariant 11) — its settle /
   `cpu_load` / `gpu_util` / `vram` arms, i.e. what the box can MEASURE. The box holds no lane
   count (8a). Extract payload into `active/<id>/repo/`, `pip install` any
   `pip_extras` not already present, run `argv` with `cwd=active/<id>/repo/`,
   `PYTHONPATH=src`, `--out ../out` appended (lands in `active/<id>/out/`), and — iff
   `task.json.resume_from` is set — `--init-from ../resume.pt` appended (the file was shipped to
   `active/<id>/resume.pt` alongside `task.json`). **`task.json`'s `argv` keeps a literal
   `"python"` as its first element** — the dispatcher never substitutes its own `sys.executable`
   (the home machine's own interpreter path has no reason to exist on the rented box); the
   worker resolves `"python"` against ITS OWN `sys.executable` at launch time, the only point
   where a concrete interpreter path is meaningful. On exit: rc 0 → `DONE`, else `FAILED_<rc>`;
   always an `exit` row in `worker.jsonl`. The worker never deletes task dirs. It additionally
   watches for a `PREEMPT` marker on any of its active tasks — invariant 17 owns this path.

   8a. **⛔ THE BOX HOLDS NO LANE COUNT — how many tasks a box carries is decided by PLACEMENT,
   and only there** (2026-10-03; owner: *"boxes should be pretty dumb, since it's harder to manage
   updates across a fleet"* and *"it's not even clear to me that the box should know what the
   coordinator thinks its lane cap is"*).

   *What it replaces.* The worker used to be started with `--max-slots {slots_total}` and refuse
   to launch past it. That number was a COPY of the registry's, taken once at launch, and nothing
   refreshed it: bring-up no-ops on a live worker, and the worker's self-update (20i) re-execs
   with its own `sys.argv`, so the copy survived every code delivery. Raising a box's
   `slots_total` therefore made the coordinator ship work the box refused to start, and 19h then
   read the unstarted tasks as over-packing and ratcheted the box's learned cap DOWN. Four
   recorded occurrences (fleet-utilization-monitor inv. 9); what prompted this was the `desktop`
   box on 2026-10-03, re-registered at 4 after a reinstall and unable to be raised without a
   command run on the box. The copy never bound when it was fresh — placement already packs to
   `min(slots_total, capacity slots, learned over-pack cap)`, all `<= slots_total` — so it could
   only ever bind when it was WRONG.

   *The rule.*
     - `_bring_up_worker` starts `spool_worker.py --spool ~/spool` and passes no lane count.
     - `launch_ready` has no count check. It calls `should_launch` with the `max_slots` arm
       disabled, so only the measured arms can hold a task; a held task is still logged
       (`launch_gate`, 19h-4) and still handled by 19h.
     - `spool_worker.py` still ACCEPTS `--max-slots N` and ignores it. Every worker started
       before this invariant carries the flag in the argv it re-execs with, and a worker that
       dies parsing its own arguments is relaunched by nothing on a rental.
     - Consequence: a change to `slots_total` (or to anything else placement reads) takes effect
       at the next placement pass, on every box, with no action on the box.

   *Rollout.* A worker started before 8a keeps enforcing its launch-time number until it
   self-updates, which happens within `worker_refresh_min` of the coordinator deploy that
   delivers this code (20i, 20j-3). Do not raise a box's `slots_total` until its
   `WORKER_VERSION` matches the deployed sources.

   8b. **LAUNCH PACING IS THE COORDINATOR'S; THE BOX HOLDS A CACHED COPY** (2026-10-03; owner:
   *"move the launch pacing thresholds to be managed by the coordinator (fine if the box keeps a
   cache)"*).

   *What it replaces.* The numbers `should_launch` paces by — how long to settle between launches,
   when a box counts as idle enough to skip the settle, the GPU-utilisation ceiling, the CPU
   reserve, the free-VRAM requirement — were constants in box-side code (`sweep_supervisor`), so
   changing one meant shipping worker code to every box. Coordinator settings were meanwhile tuned
   AGAINST them without owning them: `ship_launch_grace_min` is only correct while it exceeds
   `max_slots_cap × settle_minutes` (inv. 27e), a product of one coordinator setting and one
   constant on the box.

   *The rule.*
     - One coordinator setting, `launch_gate`, holds them:
       `settle_minutes` 3.0 · `settle_floor_min` 0.5 · `settle_idle_frac` 0.5 · `util_ceiling`
       90.0 · `cpu_reserve_cores` 1.0 · `vram_lane_mult` 1.25 · `vram_free_frac` 0.2 — the values
       the box-side constants had, so adopting this changes no behaviour.
     - *Delivery* rides the measure probe's own ssh call, like the `FREEZE` assert (box-pause
       14a): for every `live`/`paused` box, rentals included, the probe command first writes the
       setting — read LIVE from the registry, so an edit needs no restart — to
       `~/spool/launch_gate.json` when the box's copy differs, and reports `LG same|updated`. An
       update is logged as `launch_gate_pushed`. So a change reaches every box within one
       `resource_measure_every_min`, with no worker code delivery and no worker restart.
     - *The box* reads that file whenever it changes and uses it; that file IS its cache, so a
       box keeps pacing by the last values it was given while the coordinator is unreachable, and
       across its own restarts. With no file yet (a box before its first probe) it uses built-in
       defaults, which a test holds equal to the coordinator's.
     - *Trust boundary, on the box.* Each key must be a finite number inside its allowed range
       (fractions in [0,1], `util_ceiling` in [0,100], minutes and cores ≥ 0 and bounded); a key
       that is missing or fails that falls back to its built-in default ALONE, and unknown keys are
       ignored — a worker older than a new key must not reject the file. The worker logs one
       `launch_gate_config` line whenever the values in force change, naming them and anything it
       refused.
     - `should_launch` takes `cpu_reserve_cores`, `vram_lane_mult` and `vram_free_frac` from the
       config it is handed, defaulting to the old constants, so the manual sweep lane is
       unchanged. A `launch_gate` hold line (19h-4) quotes the thresholds actually in force.
     - The lane count stays out of it: 8a.

   8c. **THE BOX SKIPS ITS GPU LAUNCH RULES FOR A TASK THE COORDINATOR SAYS DOES NOT USE THE GPU**
   (2026-10-03; the box-side half of 26m).

   *Why.* Two of `should_launch`'s rules read the WHOLE CARD: `gpu_util >= util_ceiling`, and free
   VRAM under `vram_free_frac` of the card (the per-lane form never applies — the worker has no
   per-lane VRAM measurement). Past the first lane they hold EVERY launch, so a card the owner has
   filled or is driving lets a box start one CPU-only task at a time however many the coordinator
   placed there. Admitting those tasks (26m) without this would wedge them in `shipped`, and the
   over-pack reaper (19h) would read that as a capacity ceiling and learn it.

   *The rule.*
     - The coordinator decides, per task, at ship time: `task.json["env"]["RUNQ_NO_GPU"] = "1"`
       exactly when `lane_vram_gb(effective hint) == 0`. It rides in `env`, an existing key, for
       the reason `RUNQ_FORCE_BOX` does (4i-7): a worker that predates it ignores it and gates as
       before, where a new top-level key would be rejected.
     - The box, for a task so marked, evaluates `should_launch` with the card unmeasured
       (`gpu_util`, `vram_free_gb` → None), so those two arms abstain. Settle and `cpu_load`
       apply unchanged. The box decides nothing: it has no opinion on which tasks use the GPU.
     - The gate is evaluated for each waiting task in arrival order and the first that passes
       launches (still at most one per call), so a GPU task held on the card does not hold a
       CPU-only task behind it. When none passes, the hold line (19h-4) is the first task's.
     - A forced task (4i-7) and `FREEZE` are unchanged.
9. **Ingest + completion:**
   a. *Connection, per instance* (applies to every network op this feature performs — ship,
      ingest, teardown-pull — not just one script): prefer the proxy endpoint
      (`ssh_host`/`ssh_port` from the instance record); after `ssh_fallback_fails` consecutive
      failures against that specific instance, switch to the direct endpoint (`vastai ssh-url`)
      for the remainder of that instance's lifetime and never switch back (VAST-TEST.md,
      2026-07-07: bouncing back just re-hides the same failure). Each switch is one `event`.
   b. *Per-poll ingest*: rsync-pull `worker.jsonl`, `HEARTBEAT`, and any `DONE`/`FAILED_*`/
      `PREEMPTED` markers (small files) from every live instance. Use two passes, matching the
      fix already carried in `watch_and_pull.sh`: `--inplace --append` ONLY for genuinely
      append-only files (TB events, `worker.jsonl`); plain `--inplace` for anything that gets
      rewritten or merely re-touched in place (`*.json`, checkpoints, **and `HEARTBEAT`**) —
      `--append` on a rewritten file franken-globs stale local prefix with a new remote tail, and
      on a same-size re-touched file (`HEARTBEAT` is always 0 bytes) rsync's size-only quick
      check for `--append` sees "nothing new to append" and skips the transfer FOREVER after the
      first successful pull, freezing the local mtime while the remote keeps advancing (2026-07-14
      incident: this alone, no real connectivity problem, made `_reap_dead_workers` (invariant
      10c) destroy several healthy, actively-training boxes). `claim`+`start` rows drive
      `shipped → running` — but
      `worker.jsonl` is append-only across a task's ENTIRE lifetime (every re-ship after a
      preemption/infra-failure requeue reuses the same task_id), so only the LATEST claim/start
      row per task_id counts, and only counts at all if its own timestamp is no older than the
      task's `updated_at` (stamped at the exact moment it most recently entered `shipped`) — a
      stale row from an earlier attempt must never be mistaken for THIS attempt starting.
   b-ii. **7g — an interrupted pull PARKS its partial instead of leaking it** (2026-07-31).
      Invariant 7f correctly dropped `--inplace` from non-append pulls (it truncated
      `ckpt_latest.pt` AND its `.prev` spare, producing `archive is corrupted → STARTING FRESH`).
      But that restored rsync's temp-file-then-rename, and `_run_or_timeout` SIGKILLs the whole
      process group at 60s — **uncatchable**, so rsync can never clean the temp up. A payload the
      link cannot move in 60s therefore leaks a FULL-SIZE `.name.<6 random>` on **every** attempt,
      and the checkpoint pull retries every `checkpoint_pull_every_min`. MEASURED 1.5 days after 7f
      landed: **342 orphans, 210.1 GB — 62 % of `experiments/`**, worst single run dir 25.8 GB,
      ~160 GB accumulated in one 12 h window while the 1.5–1.9 GB dream arms ran.
      Non-append pulls now pass **`--partial-dir=.rsync-partial`**. The partial goes to one
      well-known subdir per destination and is REUSED as the basis for the next attempt, so a large
      checkpoint **converges across retries** instead of restarting from zero — which is what makes
      it land at all, and therefore what finally gives those tasks a resume point (7f's docstring
      recorded ">~100–300 MB never lands" as an open caveat; this is its answer). It cannot
      reintroduce 7f's corruption: the partial is moved onto the destination name only when
      complete, exactly the step `--inplace` skipped. `--append` pulls are unchanged.
      Two reapers run as poll phases, aged differently on purpose: a legacy `.name.<6 random>`
      orphan is garbage the moment it exists (its random suffix is regenerated per invocation, so
      nothing will ever resume it) and goes after 1 h — a window that exists only so an IN-FLIGHT
      transfer is never pulled out from under itself; a `.rsync-partial` entry is **load-bearing**
      while its task runs and is aged against the longest plausible run (48 h). Both are guarded by
      the name pattern **and** mode 0600, which together matched 210.1 GB in 340 files and nothing
      else on the live tree — a file failing either check is left alone.
   c. *Periodic checkpoint pull*: at most every `checkpoint_pull_every_min`, additionally pull
      `out/ckpt_latest.pt` for every task currently `running`, into
      `experiments/<grp>/<name>/ckpt_latest.pt`; on success, stamp `tasks.resume_checkpoint` with
      that local path (invariant 16). A failed pull is not an infra-failure signal on its own —
      it's folded into the same consecutive-failure counter as (a).
   d. *Completion*: `DONE` → full allowlist pull into `experiments/<grp>/<name>/`, verify the
      entrypoint's expected completion artifact exists (`entrypoints.py`; e.g. probe →
      `summary.json`) → `done` + `result_path`, else → `task_failed` (`artifact_missing`). **A
      missing artifact triggers ONE retry pull before that decision** (dispatcher.py:1276-1284):
      a transient transport failure (box torn down mid-pull, ssh drop) can return `ok=False` even
      after the artifact itself transferred, so artifact PRESENCE — not the transport exit code —
      is the completion proof. `FAILED_<rc>` → pull logs, → `task_failed`. **The "logs" pull MUST
      include the trainer's captured stdout+stderr `run.log`, which the box worker writes to
      `active/<id>/run.log` — a SIBLING of `out/`, one level ABOVE it** (`spool_worker.py`). Every
      other ingest pull is rooted at `active/<id>/out/`, and an rsync `--include` can only match
      paths UNDER its root, so a pull rooted at `out/` STRUCTURALLY cannot reach `run.log`; a crash
      while building the model writes nothing to `out/` at all, so `run.log` is then the ONLY record
      of the traceback and it dies with the box at idle-teardown (live 2026-07-24: a substrate that
      built cleanly in the identical local trainer path failed on the box, leaving only the exit
      code). `_complete_failed` therefore issues a dedicated pull rooted one level up
      (`active/<id>/`, include `run.log`) into `experiments/<grp>/<name>/run.log`, and folds a
      bounded tail of it (`_run_log_tail`, last ~40 lines / 4KB) into BOTH the `task_failed` event
      reason (so `runq show` carries the crash cause) and the `[ALERT]` line. Best-effort — a failed
      pull yields an empty tail and the old bare `worker exit <rc>` reason, never blocking the CAS.
      **`task_failed` is
      terminal — science failures are never auto-retried.** **Zero-progress tripwire:** a
      `task_failed` task with no checkpoint and no TB event ever pulled (`_task_made_progress`
      false, dispatcher.py:1291-1309) is flagged ZERO-PROGRESS and printed as a loud `[ALERT]` to
      `coordinator.log` (`_alert`); it does NOT change retry policy (`task_failed` stays terminal,
      no requeue) — it just makes one bug crashing a whole sweep read as a single alarm instead of
      many identical red rows. `PREEMPTED` → invariant 17.
    h. *The completion RETRY pulls only the artifact, never the bulk includes again* (invariant 9g,
       2026-07-30). The 2026-07-11 note above added a retry because a transient rsync failure could
       return `ok=False` after the artifact had transferred — correct, but the retry repeats the SAME
       includes, so when the cause is SIZE rather than a blip it re-attempts the very transfer that
       just failed, against the same 60s budget and the same bytes. That is why the reason reads
       "absent after 2 pulls": two identical doomed attempts.
       **Live: `m49_dreamfix/fleetcheck` COMPLETED — 11m23s, `DONE` marker, final summary line in
       `run.log` — and `results.json` was verified present ON THE BOX at 6,082 bytes over ssh**,
       alongside `ckpt_substrate_seed0.pt` at **235,420,985 bytes**. The `out/`-rooted ingest is ONE
       rsync whose includes carry `ckpt_*.pt`, so the 235 MB sibling blows the budget and NOTHING
       lands — including the 6 KB file that proves success. Comparable runs whose substrate
       checkpoints were 13 MB pulled fine, so the discriminator is payload size, not the box: this one
       had 4 successful ships and 0 failures. The task then went `task_failed`, which never
       auto-requeues, **discarding a finished run**, and the `[ALERT]` told the owner to check their
       `completion_artifact` declaration — which was not the problem.
       Now the retry's includes are `[entry.completion_artifact]` alone: small, fast, and decisive.
       The bulk artifacts are not lost — the next ingest pass keeps pulling them; only the COMPLETION
       DECISION is decoupled from having to move hundreds of MB inside one timeout. It still cannot
       INVENT a completion: an artifact absent from the box after both pulls still fails.
       **General shape worth remembering: a retry that is byte-identical to the attempt that just
       failed only helps against transient faults. If the failure is deterministic in the payload,
       the retry must be NARROWER, not merely repeated.**

    g. *PREFER the trainer's own structured crash record over the log tail* (2026-07-30). On an
       uncaught exception `shared.infra.run` writes **`out/crash.json`** (`{"traceback": "…"}`) — a
       deliberate, parse-free record of exactly what a human needs — and it comes home on the
       **FIRST**, `out/`-rooted pull, which is the reliable one. The `run.log` tail depends on the
       SECOND, one-level-up pull, and **that is the one observed to miss**: live 2026-07-30,
       `m50_stage_reset/armnostagereset…` had `run.log` (9190 bytes) AND `crash.json` (8635 bytes) in
       its result dir while its `task_failed` reason was the bare string `worker exit 1` — the tail
       read empty at reason-construction time even though the log had arrived. **Two arms of that
       campaign failed identically inside 60 seconds, each recording nothing**, while the traceback
       — a `TypeError: … unexpected keyword argument 'stage_reset'` listing every valid parameter,
       i.e. a knob-reachability bug (`python -m native.diagnostics.knob_reachability`) that dooms every arm of the
       campaign — sat unread on disk. So (d)/9d's tail is necessary but not sufficient; this is the
       source that survives when it fails.
       Both sources are folded in, **`crash.json` FIRST** because it is the deliberate one, each
       bounded the same way (~40 lines / 4 KB) but **per LINE, keeping each line's HEAD**, and whole
       lines are dropped from the FRONT to fit.
       **⚠ The first implementation used a byte-tail (`tb[-max_bytes:]`) and that DEFEATED ITSELF on
       the exact failure class 9d(g) was written for.** When the exception MESSAGE is huge — a
       `TypeError: … unexpected keyword argument 'stage_reset'` followed by a `dict_keys([…])` dump of
       every valid parameter, ~8 KB on ONE line — a byte-tail lands mid-dump and keeps the USELESS end
       while dropping the exception name and the offending argument entirely. Measured on
       `m50_navdefault/ctl_s1184f5`: the stored tail was 4120 chars over **2 lines** (longest 4095)
       and contained no exception name at all. Head-clipping per line inverts it: a short frame line
       survives whole (so the call site stays readable) and the exception line keeps the part that
       names the bug, with the elision marked (`… (+N more chars)`) rather than silent. Trimming pops
       from the FRONT because the exception is on the LAST line — sacrifice the oldest frames, never
       the cause. (A byte-tail is still the general rule for a line-oriented LOG; it is wrong only for
       a single line that is itself larger than the bound, which is why the structured record is the
       preferred source.) Best-effort by
       construction: a missing file, unreadable bytes, non-JSON content, a JSON value that is not an
       object, or a missing/blank/non-string `traceback` all contribute nothing and leave the bare
       exit-code reason. It must never raise — it runs immediately before a terminal CAS, and a task
       stranded non-terminal is far worse than a reason without a traceback. An absent section is
       deliberate: an empty one would falsely read as *we looked and the crash record said nothing*.
    f. *`artifact_missing` gets the same forensic pull as every other terminal failure*
       (2026-07-30) — the LAST uncovered terminal path. (d)/9d gave `task_failed` from a
       `FAILED_<rc>` marker a `run.log` pull, and 18f gave `cancelled` one, but a task the worker
       declared **DONE** whose `completion_artifact` never appeared reached `task_failed` carrying the
       bare string `artifact_missing` and no log at all.
       **It is the worst case to leave dark precisely because the worker claimed SUCCESS** — the run
       is not obviously broken, so the owner has no hypothesis to start from. Live 2026-07-30,
       `m49_phase1_eye/p1_gv0`: ran **2h07m**, wrote `ckpt_latest.pt` + `.prev` + two substrate
       checkpoints + TB events — so it plainly worked — then died terminally (`task_failed` never
       auto-requeues) with nothing to distinguish *never wrote the artifact* from *wrote it under
       another name* from *the job config declares the wrong `completion_artifact`*. The box is torn
       down minutes later carrying the only copy of the log.
       Now: the same one-level-up pull (`active/<id>/`, include `run.log` — rooted ABOVE `out/` or the
       include can never match, the identical structural trap (e)/9d documents), the tail folded into
       the failure reason so `runq show` explains it, the EXPECTED artifact named in the reason so it
       can be compared against what the trainer actually wrote, and an `[ALERT]`, because a
       trainer-vs-`completion_artifact` contract mismatch repeats for every sibling arm and must read
       as one loud alarm rather than N silent red rows. Best-effort throughout: a failed pull yields
       an empty tail and the bare reason, never blocking the terminal CAS.

    e. *A task NAME may not be able to wedge the fleet* (2026-07-29). `_result_dir` is
       `experiments/<grp>/<name>`, and ext4/xfs cap a single path component at **255 bytes** — but
       `runq sweep` builds an arm name by concatenating every axis value, so a wide sweep produces
       names past that limit. `_result_dir` is called from `_ingest_and_complete`, so the resulting
       `OSError: File name too long` propagated out of `poll_once` and **killed the daemon**; the
       self-heal supervisor respawned it straight back into the same task. Live: 2 crash-respawns at
       15:07-15:08 on a 264-char `m50_nav_module` arm, the whole fleet stalled meanwhile, ending only
       because a human cancelled the task. **One over-long name can wedge the entire fleet
       indefinitely** — the same "one item halts the serial loop" shape as invariants 7b/7c.
       `_fs_safe_component` truncates to 200 bytes and appends a 12-char sha256 of the FULL name.
       The digest is required, not decorative: sweep arms share a long prefix and differ in their
       TAIL, so a plain truncation would map every arm of a sweep onto one directory and silently
       overwrite their results. It is a pure function of the name, so the mapping is stable across
       polls and restarts, and a name already inside the limit is returned UNCHANGED — no existing
       result directory moves.

10. **Infra failure detection** → `infra_failed`, then `retry_decision` (`requeue` iff
    `retries_used < max_retries`, incrementing it; else terminal). **Retry budget (owner
    directive, 2026-07-09): `max_retries` default is 3, but an infra failure only costs HALF a
    retry** (`retries_used += 0.5`) since it isn't the task's fault — a task can survive up to 6
    consecutive infra failures before going terminal. `retries_used`/`max_retries` are `REAL`
    columns (registry spec schema) to hold the half-unit increments cleanly. `task_failed` (a
    real crash/science failure, invariant 9d) never reaches `retry_decision` at all and stays
    **permanently terminal, no retry ever** — the owner's reasoning: a science failure is
    unlikely to be fixed by blindly retrying the identical config, so the fastest signal to the
    task's starter that it needs manual attention is to fail it once and stop, not spend budget
    re-running a broken config.
    a. instance `lost` (reconcile) — a task caught in `claimed|shipped|running|preempting` on a
       lost instance goes through this path, not invariant 17's (there is no graceful eviction
       possible once the box itself is gone; a `resume_checkpoint` from the last periodic pull,
       invariant 9c, is used if one exists, same as any other infra failure);
    b. `shipped` with no worker `claim` within `claim_timeout_min` (`_reap_unclaimed_ships`).
       **Implemented 2026-07-28 — it had been specified here and defined in settings but never
       consumed by any code path**, the same "declared but never wired" shape `heartbeat_stale_min`
       had before 10c. The gap was not cosmetic: a box whose worker never starts is covered by NO
       other reaper (19 ignores `shipped`; 19h bails on `running == 0`; 10c needs a HEARTBEAT that
       a never-started worker never writes, and its deferral to
       `provision_timeout_min`/`rent_patience_min` is wrong once the box has logged `live`). Live
       cost: a Tesla V100 shipped a task at 12:44 and idle-billed 7.5 h at 0% GPU util until a
       human cancelled it — $1.07, 25% of that day's fleet spend, for zero work; $1.48 across three
       such boxes, 34% of the day's total.
       **Scoped to a box that has never started ANY task** (no `start` event in its history), which
       makes it the exact complement of 19h rather than a competitor. The SCOPING is what keeps
       them disjoint, deliberately not the relative timeouts: at the original
       `claim_timeout_min` of 15 — SHORTER than `ship_launch_grace_min` (20) — an unscoped rule
       fired first on a gate-held task and charged half a retry for over-packing, which is the
       scheduler's fault, not the task's, and which 19h requeues for free while learning the
       box's real concurrency.
       A box with a running/preempting occupant is therefore skipped, as is one we cannot currently
       reach (10c's discipline: unreachable ≠ worker-never-claimed). Remediation clears the box's
       spool copy before requeueing (the double-run guard 19h uses), then `_destroy`s the box so it
       stops billing — `should_teardown` would otherwise report `feasible_task_waiting` forever
       while queued work exists, re-shipping to the same broken box until the retry budget burns
       out. `_destroy`'s owned-box carve-out means an owned box only frees the slot;
    c. `HEARTBEAT` older than `heartbeat_stale_min` AND the most recent pull attempt for that
       instance actually succeeded (`ConnectionTracker.consecutive_fails == 0`) — single-source
       staleness is not enough: an old local mtime is also exactly what a run of failed rsync
       pulls looks like (a slow ingest cycle over several live instances, or a transient
       ssh/rsync hiccup, per VAST-TEST.md), and that's indistinguishable from a genuinely dead
       worker from mtime alone. Requiring the last pull to have succeeded means the mtime being
       read is a fresh, confirmed copy of the REMOTE file — real staleness there means real
       death. A box we currently can't reach is a different problem (ssh-fallback / teardown
       paths), not this one.
       **(f) FORENSIC PULL — the dead-worker path pulls `run.log` home BEFORE `_destroy`
       (2026-07-30).** This was the FOURTH place the identical omission had to be fixed: 9d covered
       `task_failed` from a `FAILED_<rc>` marker, 18f covered `cancelled`, 9e(f) covered
       `artifact_missing`, and this path still called `_destroy` immediately after `_infra_fail`,
       taking the only copy of the log with it. So the entire record of every dead worker was the
       bare string `dead_worker: heartbeat stale` — while this reaper's own docstring names three
       DIFFERENT causes it cannot distinguish (worker OOM-killed, box hiccup, uncaught exception).
       **Measured 2026-07-30: four boxes reaped in 56 minutes taking ~14 tasks with them, dozens
       more across prior days (the module notes count 58 historical `dead_worker`), and not one of
       them diagnosable after the fact.**
       This pull is unusually likely to SUCCEED, which is the point: (c) above has already proved
       the box is reachable (`consecutive_fails == 0` AND a recent confirmed heartbeat pull), so
       unlike a genuinely unreachable box we are pulling from a live host. Rooted ONE LEVEL UP
       (`active/<id>/`) because `run.log` is a SIBLING of `out/` and an rsync `--include` only
       matches under its own root — the same structural trap 9d and 9e(f) document, and a test must
       pin the ROOT rather than merely "a pull happened", since re-rooting it at `out/` looks
       correct and silently restores the bug.
       The tail is folded into the **`infra_failed` EVENT**, not the task row, because `_infra_fail`
       requeues at half a retry (invariant 10) so the task's resting state is `queued` — a tail
       attached to the row would vanish on requeue, while `runq show` reads the event. A `claimed`
       task that never shipped has no `run.log`; that yields an EMPTY tail and no tail section at
       all, which is itself the signal that the worker died before starting work (an empty section
       would falsely read as "we looked and the run said nothing"). One `[ALERT]` per box names how
       many occupants' logs were captured. Best-effort throughout: any pull failure logs
       `dead_worker_forensics_failed` and proceeds — forensics may NEVER block the `_infra_fail`
       transitions or the `_destroy`, since a box left billing forever is strictly worse than a
       missing log.
       **(g) THE GUARD MUST OBTAIN THE CONFIRMATION, NOT GIVE UP ON IT (2026-07-31).** The
       freshness half of (c) — `seconds_since_heartbeat_pull < heartbeat_stale_min`, added after
       the 2026-07-26 recurrence — used to `continue` when it could not confirm. That turned the
       guard into a fleet-wide OFF SWITCH for this reaper, tripped by the very condition the reaper
       exists to clean up. `since_ok` can only be refreshed once per POLL CYCLE, so the predicate is
       really **`cycle_time < heartbeat_stale_min`** — and the cycle grows with the number of live
       boxes and with how many of them are SLOW, i.e. it grows exactly when boxes are dying.
       **MEASURED 2026-07-31: poll cycle median 31.0 min (min 19.4, max 41.6) against the 15-min
       threshold ⇒ 183 `dead_worker_skipped` against 4 `dead_worker`, a 97.9% skip rate over 12 h.**
       Four boxes with genuinely dead workers survived hours apiece, each holding its occupants in
       `claimed`/`running` with NO failure signal (the non-terminal-stall class `watch.py` exists
       for); the reaps that landed only fired on a rare unusually-fast cycle. Self-reinforcing: a
       dead box eats a 60 s rsync timeout on each of its ~5 pulls per cycle, lengthening the cycle
       that disabled the reaper.
       The reaper now PULLS `HEARTBEAT` for that box at the point of decision and re-stats it — one
       0-byte rsync, only for a box already past the staleness threshold. `rsync -t` preserves the
       REMOTE mtime, so after a successful pull the local mtime IS the worker's last touch:
       pull fails ⇒ skip (unchanged (c) behaviour, logged `dead_worker_skipped`); pull ok + mtime
       fresh ⇒ our copy was lagging the cycle, box HEALTHY, skip (logged `dead_worker_confirm_fresh`);
       pull ok + mtime still stale ⇒ a confirmed fresh read of a stale remote ⇒ genuinely dead, reap.
       A live worker touches `HEARTBEAT` every `HEARTBEAT_SECONDS` (60 s), so a healthy box always
       takes the middle branch — strictly stronger evidence than the pre-(c) code acted on, and
       strictly more actionable than never firing. The 2026-07-26 healthy-box-destruction case
       stays covered, and the decision no longer depends on cycle timing at all.
    d. `claimed` on a live box we are demonstrably failing to DELIVER to, for longer than
       `ship_timeout_min` (`_reap_undeliverable_claims`, 2026-07-29). **The hole: every other
       reaper watches a state a never-shipped task cannot be in.** 10b requires `shipped` — and
       additionally skips any box with `consecutive_fails > 0`, which is exactly what an
       undeliverable box always has, so it is excluded twice over; 10c requires a HEARTBEAT and a
       dead worker, but here the worker is alive (verified over ssh: box up 11 days,
       `spool_worker.py` running, 39 G free); 19/19h only look at `running`/`shipped`. So a box
       healthy enough to answer ssh but too degraded to accept a 37.6 MB bundle held six tasks
       indefinitely and re-attracted more every poll, with nothing in the system holding an
       opinion about it.
       **Kill criterion (all three):** the task has been `claimed` on a `live` box longer than
       `ship_timeout_min`; there is a `ship_failed` event for THIS task on THIS instance; and the
       box has no `running`/`preempting` occupant. The middle clause is the discriminator, and age
       alone cannot replace it — fleet-wide claim→ship p99 is 98.7 min and the longest successful
       ship took 301.8 min, so an age-only rule would have requeued 70 ships that went on to land.
       **Remediation is the cheap, reversible half:** clear the box's spool copy (19h's double-run
       guard), requeue at **no retry cost** (the scheduler failed to deliver; the task did nothing
       wrong — 19h's reasoning), and quarantine the box from new placement
       (`ship_quarantine_i<id>`, persisted per invariant 1 so a respawn does not hand it a fresh
       backlog; lifted the moment any delivery to it succeeds). It does **not** destroy: a box can
       come back (owned box −1 recovered from a 311-failure streak), and invariant 11 already
       reaps a quarantined rental once it is empty, through the one choke point that carves out
       owned boxes. A cleanup ssh that fails defers the task for EVERY box source, not just owned
       ones (unlike 10b there is no `_destroy` backstop), and the quarantine is only written once
       at least one task actually moved — barring a box on the strength of a check we could not
       complete would be acting on absent data.
11. **Teardown** (`should_teardown`): TRUE when an instance has zero tasks in
    `claimed|shipped|running|preempting` AND no queued task is feasible for it (4a) AND that
    state has persisted ≥ `idle_timeout_min` — or unconditionally when `now ≥ hard_cap_at`, or
    when the instance is EMPTY and 10d-quarantined (reason `undeliverable`). The quarantine clause
    is checked after the occupant test, so a box still holding work shipped before the quarantine
    is never torn down under it, and after the owned-box carve-out, so an owned box merely waits to
    recover. It exists because `feasible_task_waiting` holds a box alive for as long as ANY queued
    task fits — which is precisely the idle-bill 10b needs its own `_destroy` to prevent, reached
    here without a bespoke destroy rule.
    **11a. AT MOST `warm_idle_max` (default 1) EMPTY paid boxes may be held by
    `feasible_task_waiting`** (owner directive 2026-07-30: *"we probably want no more than 1 box kept
    warm and idle"*). That check is evaluated PER BOX and is satisfied by ANY queued task that fits,
    so a single 1-slot task retained EVERY idle box in the fleet and `idle_timeout_min` never got a
    chance to fire. **Measured live: four empty boxes idle 24 / 42 / 48 / 80 minutes against a
    10-minute timeout, costing $0.2162/hr for zero work** — because work arrived every few minutes and
    the queue was almost never empty at the instant the check ran. The timer was not broken; it was
    unreachable.
    `_teardown_idle` computes the designation ONCE per poll, over the whole fleet, via the pure
    function `warm_hold_grants(instances, queued_tasks, settings) -> set[instance_id]`, and passes
    `warm_idle_keep` per instance; an empty box that is not designated falls through to the ordinary
    idle timer and is torn down with reason `idle_over_warm_cap`. Hoisting the decision out of the
    loop is what stops it drifting as boxes are destroyed inside it. **Owned boxes are excluded from
    the budget**: they cost nothing idle and are never torn down anyway, so spending warm budget on
    one would cull a PAID box we are actually paying to keep. Boxes WITH occupants are untouched —
    this bounds retained EMPTY capacity only, never work in flight. `warm_idle_max` and
    `max_warm_free_slots` are both NEW keys, so `_ensure_settings` seeds them into an existing
    registry unaided; no `_SETTING_MIGRATIONS` row is needed.

    **Two bounds, both of which must hold** — the two units the owner stated the same directive in
    on 2026-07-30: `warm_idle_max` (box count) and `max_warm_free_slots` (free slots, *"we have
    58/98 slots in use so almost 50% of our spend is going to waste … no more than 10 free slots
    kept warm"*).

    **The slot budget is FLEET-WIDE, not per-candidate** (owner directive: *"make sure
    `max_warm_free_slots` includes non-empty boxes that have free slots"*):
    - `committed_free` = every free slot on a live box that STILL HOLDS WORK, owned or paid. Spent
      against the cap FIRST, because those slots are exactly as available to the queue as a warm
      box's are and cost nothing extra — an owned box is $0, a busy paid box bills for its occupant
      regardless. Counting only the candidates would let a fleet already carrying 20 free slots rent
      a third pool of them and still report itself inside the cap.
    - `allowance = max(0, max_warm_free_slots - committed_free)`.
    - Grant to empty PAID boxes cheapest first (ascending `dph_usd`, then MOST slots, then `id` —
      deterministic, because a designation that wobbles between polls would spare a different box
      each time and cull none of them), stopping at `warm_idle_max` boxes or when the next grant
      would push the held total OVER `allowance`. The cap is a ceiling, not a target: there is no
      first-grant carve-out, so an exhausted allowance holds NOTHING.

    **Consequence, stated because it is easy to be surprised by:** on a busy fleet `committed_free`
    alone routinely exceeds the cap, the allowance is ZERO, and nothing is kept warm — which is the
    intent (no reason to pay to keep a box warm when the queue already has somewhere free to land),
    but it means `warm_idle_max` only binds on a comparatively empty fleet. Measured on the live
    fleet 2026-07-30: 20 free slots on non-empty boxes (10 owned, 10 busy paid) ⇒ allowance 0 ⇒ zero
    warm boxes. Raise `max_warm_free_slots` if warm capacity should survive a busy fleet. A related
    edge: if `max_slots_cap` ever exceeds `max_warm_free_slots`, no single box can ever fit an
    allowance, and 11a degenerates to "never keep anything warm".

    **The other half of the deadlock is invariant 4b, and it is deliberately left alone.**
    `_pack_cost` scores an EMPTY paid box at its FULL `dph × window` (there is no occupant to ride
    along with) versus `$0.0000` for a spare slot on a busy box, so an empty paid box ranks STRICTLY
    LAST for placement. Placement will not use it because it assumes the box is about to be torn
    down; teardown would not destroy it because it assumed the queue would use it. Live 20:49:
    twelve tasks packed onto boxes with 1–4 free slots at `$0.0000 marginal` while four empty 6-slot
    boxes got nothing. Teaching 4b to PREFER an empty rented box would also break the deadlock, but
    it commits the fleet to that box's whole window on every placement, and the measured failure is
    the SMALL-backlog case — under a real backlog the cheap boxes fill and the empty box already
    becomes the cheapest option (observed live at 21:22: `pack on instance 40000031: $0.5642
    marginal`). Bounding the hold is the smaller change and it is the one that stops the meter. A
    test pins 4b's ranking so a future change there cannot silently make 11a moot.

    The trade being made: a torn-down box must be re-rented (~14 min to become useful, and a ~46%
    same-day dud rate), so the retained box is what absorbs a burst without that latency.

    **Open questions (both measured, neither acted on):**
    - *DEMAND-conditional holding.* Nothing here distinguishes "one small task is queued" from "a
      real backlog is waiting", so the warm box is retained either way. Holding only while
      `queued slots > free slots already available on live boxes` (counting free slots on OWNED
      boxes, which are $0, and on BUSY paid boxes, which bill for their occupant regardless) would
      tighten this further: on the 2026-07-30 fleet it would have held ZERO boxes, because 17 such
      free slots already covered a 2–3 slot queue. It trades against re-rent churn that has NOT been
      measured, so it is recorded here rather than shipped on intuition.
    - *Over-renting, which is the larger bucket.* 27 of 57 boxes torn down in 24h never had a single
      task `start` — 22.1 box-hours, $1.32, versus 6.0 box-hours ($0.34) for boxes that did work and
      then sat empty. 15 of the 27 were `stuck_provisioning`. 11a bounds how long such a box bills,
      but whether it should have been rented at all is a 4e/5c question, not a retention one.

    **"Persisted ≥ idle_timeout_min" is derived from persistent DB state, never an in-memory
    timer** (invariant 1: a dispatcher restart, or being driven via repeated `--once` instead of
    the long-running daemon, must not silently reset how long an instance has looked idle,
    which would either delay teardown indefinitely under `--once` polling or — worse — never
    fire at all if nothing keeps a same-process timer alive): idle-since is the most recent
    `updated_at` among every task ever assigned to the instance (that IS the timestamp of its
    last transition — e.g. into `done` — the closest persistent proxy for "since when did this
    instance's work last change"), or `instances.created_at` if it was never given a task.
    Sequence: state `draining` → final sweep-pull of `active/*/out` for any tasks not yet pulled
    → `vastai destroy --yes` → `destroyed` + `cost_usd` (`dph_usd × hours(created_at → now)`,
    the same formula as invariant 3a's `lost` path). The existing `watch_and_pull.sh`
    destroy-precondition spirit is kept: if the final pull retrieved nothing for a task that
    claimed DONE, event `pull_empty` fires and the instance is still destroyed at hard cap only.
12. **Budget arithmetic is committed-rate, not measured-rate:** the gate uses `runq rate`
    (registry invariant 11) — instances count from `rent_intent` commit until
    `destroyed|lost`, so a provisioning box already consumes budget headroom.
13. **No secrets on the box, ever:** Vast API key and ssh private keys stay home; all transfers
    are initiated home→box; the payload is a working-tree snapshot (committed + uncommitted,
    `.gitignore`-aware; `git archive` fallback for pre-snapshot tasks) into which no credential
    material is ever staged — same push-only, no-secrets-on-box model as all existing launch
    scripts.
14. **Composability:** a whole sweep is one task (`entrypoint` invoking
    `sweep_supervisor.py --sweep …` with `slots = slots_total`) — the dispatcher does not know
    the difference; rungs/gates stay entirely inside the supervisor per its spec.
15. **The dashboard is not modified by this feature** (its spec owns any status-column adoption;
    see plan 0004).
16. **Checkpoint carry (the mechanism, shared by invariants 9c and 17):** `tasks.resume_checkpoint`
    (registry spec schema) holds a local filesystem path to the most recent successfully-pulled
    checkpoint for a task, or `NULL` if none was ever pulled. It is written only by invariant 9c
    (periodic pull, every `checkpoint_pull_every_min` while `running`) and invariant 17b (the
    final pull on a `PREEMPTED` marker); it is read only at ship time (invariant 7) to decide
    whether to attach `resume.pt`; it survives a `→ queued` requeue untouched and is never
    cleared (stale-but-present beats absent — a two-requeue-old checkpoint is still better than
    restarting from scratch). Whether it is actually used depends on the entrypoint's
    `resume_flag` (`entrypoints.py`): entrypoints with `resume_flag=None` (`train_craftax`,
    `train_minatar`, `train_grid`, `train_continuous`) never receive `--init-from` — the pulled
    checkpoint is still kept on disk for forensics, but the requeued run restarts from scratch,
    which is correct, not a bug, for those entrypoints. For most entrypoints with
    `resume_flag="--init-from"` (`train_atari`, `train_minatar_multi`, `train_stream`, the
    `smoke` test entrypoint), it's a weight-init, not an exact-step resume (no optimizer state
    saved) — expect the resumed run to redo a small amount of warmup, not to be bit-identical to
    an uninterrupted run. **`train_chess` and `train_dmc` are the two exceptions**: their M12
    (spec decision 18, `spec/native/m12-chess.spec.md`) and M13 (Locked decision 16,
    `spec/native/m13-planet-h2h.spec.md`) checkpoints both save and restore optimizer state plus
    update/step counters, so a dispatcher-driven resume there picks up training genuinely where
    it left off, not just from the same weights (env/RNG stream state still restarts fresh in
    both cases — only weights, optimizer, and the training-loop counters carry over).
    **Checkpoint+resume compliance (cross-spec decision, resolved — retired from
    `spec/DECISIONS.md`):** the >10min-task checkpoint+resume rule is now enforced by the ratchet
    gate `tests/test_trainer_checkpoint_hygiene.py` — a new or regressed >10min trainer lacking
    both `ckpt_latest.pt` and `--init-from` FAILS the test; `KNOWN_NONCOMPLIANT` is the shrinking
    grandfather set of pre-existing violators.
0a. **THE GLOBAL PREEMPT SWITCH** (`preempt_enabled`, **default FALSE** since 2026-07-30 — owner
    directive: *"let's just disable preempts for now. The checkpoints are introducing noise and
    fragility and they don't seem to be buying us much today. Our jobs tend to cost <$1 each so just
    letting the existing ones finish seems like the right move"*, then *"disable all of them — wire
    this as a global control flag"*).

    FALSE means **the fleet never interrupts a running task on its own initiative.** One flag gates
    all THREE fleet-initiated preempt sources, because disabling them one at a time is how one
    quietly comes back:
    (1) priority preemption (inv. 4d/17 — `place()` stops returning the `preempt` action and falls
    through to hold/backlog/rent); (2) capacity scale-down (inv. 20 `_reap_over_capacity` — logs
    `capacity_over_cap_tolerated` instead of shedding); (3) consolidation drains (inv. 21 —
    `consolidation_drains` returns `[]`, checked BEFORE `consolidate_enabled` so re-enabling that
    alone cannot reintroduce churn).

    **NOT gated, deliberately: `_signal_drain`** (`make drain` / `make pause`'s soft-timeout
    escalation). That is the OPERATOR reclaiming their own machine on demand, not the fleet choosing
    to churn; gating it would remove the very capability the directive protects.

    **The measured basis.** A preempt is only cheap if resume is cheap, and it is not —
    `test_resuming_from_a_checkpoint_reproduces_the_uninterrupted_run` is RED, so every interruption
    costs its arm some comparability, and **8.4% of preempts (27 of 313 in 24h) lost their checkpoint
    outright.** Against that, the entire benefit being purchased was **~$0.011 per preempt** (81
    drains, 313 preempts, upper-bound $3.51 saved). At <$1/job, running to completion is simply
    cheaper than interrupting.

    **What it costs, stated plainly:** paid boxes idle-bill until their last occupant finishes and
    teardown (inv. 11) collects them; `--probe` no longer starts by evicting (it still queues at
    priority 90 and may rent); and an owned box can sit ABOVE its day cap until its tasks finish —
    placement still refuses to ADMIT over the cap (inv. 18a/23), so the overshoot is bounded and
    drains on its own, and it is logged rather than silent. Re-enable by setting `preempt_enabled`
    (and `consolidate_enabled`) True — nothing was removed, only switched off. Invariants 17, 20 and
    21 below describe the mechanics that remain intact behind it.

17. **Priority preemption** (the `preempt` placement action from invariant 4d):
    a. *Eligibility*: task T may preempt running task V iff `V.priority ≤ T.priority −
       preempt_priority_margin`, V's instance is otherwise a feasibility candidate for T once V
       is evicted (invariant 4a, recomputed with V's slots freed), and V is not already
       `preempting`. Candidate selection: across all live instances, pick the SMALLEST set of
       eligible victims (by combined slots) that clears T's shortfall; ties broken by lowest
       victim priority first, then lowest task id (determinism). No eligible set → no
       `preempt` action for T (invariant 4d's fallback governs what happens next).
    a'. *Shortfall is measured on THREE axes* (`_preempt_shortfall`): slots, and — on a box carrying
       an invariant-18a `resource_cap` — the window's cores and VRAM budget. A victim set must clear
       every positive component. **A slots-only shortfall is not sufficient and silently disables
       preemption exactly where it is most needed**: a box can have a FREE SLOT while the budget is
       what blocks T, which reads as `shortfall ≤ 0` and skips the box entirely. Live 2026-07-27
       (introduced with 18a, caught same day): a 2-slot desktop held one 6 GB / priority-50 occupant
       against a 6 GB window budget, so a priority-90 probe could not displace it and simply held
       forever — the free slot made it look like nothing needed evicting. A box with no
       `resource_cap` yields 0 on both resource axes, i.e. the original slots-only behaviour.
    b. *Mechanics* (no trainer changes — relies entirely on each trainer's existing periodic
       `ckpt_latest.pt` write, invariant 16): the dispatcher CASes each victim `running →
       preempting` (event `preempt_intent`, naming the evictor task id) BEFORE touching the box
       — same crash-safe ordering discipline as invariant 5's `rent_intent`. It then touches
       `active/<victim_id>/PREEMPT` on the victim's instance (one small ssh op). The worker,
       every launch-gating tick, checks each of its active tasks for a `PREEMPT` marker; once
       that task's `out/ckpt_latest.pt` mtime is newer than the `PREEMPT` marker's mtime (i.e. at
       least one checkpoint write has happened since the request — never kill against a stale
       pre-request checkpoint), it sends the same graceful-then-hard kill sequence as
       `sweep_supervisor.kill_lane` (`SIGTERM`, grace period, `SIGKILL`) and touches
       `active/<victim_id>/PREEMPTED` (a task that reaches `DONE`/`FAILED_<rc>` on its own before
       the checkpoint condition is met is NOT preempted — natural completion always wins; the
       `PREEMPT` marker is simply left stale and ignored). On the next poll, ingest (9d) sees
       `PREEMPTED`, does a final targeted pull of `out/ckpt_latest.pt`, stamps
       `resume_checkpoint` (invariant 16), and CASes `preempting → queued` — this requeue does
       NOT go through `retry_decision` and does NOT increment `retries_used` (it isn't a
       failure). The requeued task keeps its original `priority`, so it competes for placement
       normally on the next pass (which may include being packed into the very capacity just
       freed by its own eviction).
    c. A `preempting` task whose instance is lost before `PREEMPTED` is observed falls through
       to invariant 10a instead (the ordinary infra-failure path).
    d. **Race avoidance across polls:** a `preempting` occupant is invisible to (a)'s
       eligibility scan (it isn't `running` anymore) and to invariant 4c's frees-soon wait
       (same reason) — so on the poll immediately after a `preempt` decision, before
       `PREEMPTED` lands, the evictor task would otherwise look exactly like "nothing is
       evictable" and fall through invariant 4d's own no-candidate bypass straight into
       renting a now-redundant second box. Invariant 4d therefore checks, BEFORE that bypass,
       whether an already-`preempting` occupant on some instance would alone cover the task's
       shortfall once it vacates; if so → *hold* (`preempt_wait`), not rent, not a second
       preemption search.
    e. **`preempting` can reach every terminal outcome `running` can** (retrospective bug,
       2026-07-14): (b)'s "natural completion always wins" and (c)'s instance-lost fallback both
       require the registry to legally CAS `preempting → done`, `preempting → task_failed`, and
       `preempting → infra_failed` — these three were missing from the registry's
       `LEGAL_TRANSITIONS`, so a task that reached `DONE`/`FAILED_<rc>` on its own, or whose
       instance went `lost`, while still `preempting` (never checkpointed, so the box-side guard
       in (b) never killed it) got silently stuck in `preempting` forever — `transition()` never
       raises on an illegal pair, and none of the three call sites checked its return value.
       Observed live overnight 2026-07-13/14: two pretrains preempted at 00:18/00:20 ran 7+ hours
       past that with their results stranded, undetected until manual monitoring. Fixed by adding
       the three transitions (no new mechanics — invariant 19e now also reaps a `preempting` task
       stuck past `stall_timeout_min` with no fresh checkpoint, so this class of stuck-forever
       task self-heals even without ever reaching a terminal marker at all).

    f. **A FAILED final pull must not DISCARD a checkpoint we already hold** (2026-07-29). The
       sharpest form of the owner's "preempts cause trouble for the task owner" — work DESTROYED, not
       delayed. `_complete_preempted` required `ok and ckpt.exists()`, so ONE transient rsync failure
       at preempt time threw away the copy the 5-minutely `_pull_checkpoints` had already fetched, and
       the task restarted from ZERO with a good checkpoint sitting on our own disk.
       **MEASURED: 27 of 313 preempts in 24h (8.4%) reported "no checkpoint pulled", median run
       length 14.6 min, one at 121 min against a 5-min pull cadence** — a never-pulled checkpoint
       cannot explain that; 22 of 23 had a checkpoint on disk.
       Dropping `ok` is SAFE, not optimistic, because `shared.infra.checkpoint.load_checkpoint` exists
       for exactly this: head → `.prev` → `None`. So intact head ⇒ resume (previously LOST); head
       truncated by the failed `--inplace` pull ⇒ resume from the spare (previously LOST); both
       unreadable ⇒ start fresh, identical to before. **Worst case equals the old behaviour.**
       `resume_checkpoint` may name the head even when only `.prev` exists — the loader skips a
       missing candidate. The event now distinguishes clean carry-forward from
       carried-but-final-pull-failed; the single old string conflated "nothing on the box" with
       "transfer failed", which is why those 27 were undiagnosable. (The code comment pointed at
       `harness._load_resume`, which does not exist — verify the CONSUMER, not the comment.)

18. **Cancel-in-flight execution** (registry invariant 4a; the user-facing "stop a stuck/
    mis-scheduled task" and "change a running task's config" capability). `runq cancel` puts an
    in-flight task into `cancelling`; the dispatcher executes it, mirroring preemption's marker
    mechanics but terminating instead of requeuing:
    a. Each poll, for every `cancelling` task with a live instance, the dispatcher touches
       `active/<id>/CANCEL` (idempotent — safe to re-touch until the task completes). The worker,
       on any tick, treats a `CANCEL` marker as an immediate graceful-then-hard kill (`SIGTERM`,
       grace, `SIGKILL`) — **unlike `PREEMPT` it does NOT wait for a fresh checkpoint**, since a
       cancelled run is discarded, not resumed — then touches `active/<id>/CANCELLED`. A task
       cancelled before its worker ever launched writes `CANCELLED` directly.
    b. Ingest (9d) pulls `CANCELLED` alongside the other markers and CASes `cancelling → cancelled`
       (terminal), clearing `instance_id` so the slot frees and an emptied box tears down normally
       (invariant 11). No artifacts are pulled — a cancelled run is discarded. If the run happens to
       reach `DONE`/`FAILED`/`PREEMPTED` during the cancel window, the cancel intent still wins
       (the task is completed as `cancelled`).
    c. A `cancelling` task whose instance is lost before `CANCELLED` is observed is completed as
       `cancelled` directly (there is no worker left to stop) — it does NOT fall through to the
       infra-failure requeue path (invariant 10a), because cancel is terminal by intent.
    d. Cancel never increments `retries_used` and never requeues. **Reconfigure a task** = cancel it
       (this invariant) then `add` it again with new args/resource hint under a NEW lane `name`
       (`UNIQUE(grp,name)` blocks reusing the cancelled row's name; config-hash dedupe is separately
       satisfied since the cancelled task is excluded — registry invariant 4a).
    e. **A cancel that races an in-flight ship must not strand a running trainer** (live
       2026-07-29). Registry invariant 4a cancels a `queued`/`claimed` task *immediately*, on the
       rationale that neither has a running worker. That is false for `claimed`: `claimed` is
       exactly the state a task sits in **while `_ship` is delivering its bundle** — compile + apt +
       rsync, measured median 30s and max 366s (invariant 7c). If the cancel lands inside that
       window, `_ship` still completes and pushes `READY`, the worker claims it and launches the
       trainer — while the follow-up `claimed → shipped` CAS is now illegal and, because
       `transition()` never raises, **silently no-ops**. The result is an ORPHAN: a trainer running
       on a paid box that the registry has already written off as terminal, holding a worker slot
       against `should_launch`'s `max_slots` gate forever, and invisible to the on-box orphan reaper
       because its `active/<id>` dir exists and carries no terminal marker.
       Observed: `azsc-p1e` seed8/seed9 cancelled 12:49:38, their `compile` events landed 12:50:03 —
       *after* the cancel — and both trainers were still running 3h later, holding 2 of box
       40000015's 6 slots. The starved sibling (`kindgoexploit…seed4`) was re-claimed at 14:26:23,
       never started, and sat with a frozen checkpoint for 85 min looking exactly like a hung
       trainer. It was not hung: **there was no process**, which is also why the harness
       `checkpoint.grace_min` watchdog never fired — that watchdog is a thread *inside* the trainer,
       so a task that never launches is the one case it structurally cannot catch.
       So: `_ship_all` MUST check the result of the post-ship `transition`. When the CAS does not
       apply, the payload just delivered is unowned — remove both `incoming/<id>` and `active/<id>`
       from the box. Deleting `active/<id>` is also what makes an ALREADY-launched trainer reapable,
       via `reap_orphans`' "owning task dir is gone" arm. Best-effort and never fatal: a failed
       cleanup leaves the pre-existing orphan behaviour, no worse.
    f. **A cancelled task's `run.log` is pulled home** (extends invariant 9d's forensic pull, which
       covered only `task_failed`). Cancel is how an operator stops a task that is misbehaving, so
       it is precisely the case whose evidence is most worth keeping — yet the box is torn down
       right after and, before this, a hung-then-cancelled cell left **no forensic trail at all**.
       `_complete_cancelled` therefore issues the same one-level-up pull as `_complete_failed`
       (`active/<id>/`, include `run.log`) into `experiments/<grp>/<name>/run.log` before the
       terminal CAS, whenever it has a live endpoint. Best-effort: a failed or absent pull never
       blocks the cancel. This does not resurrect "no artifacts are pulled" (b) — the discarded
       thing is the run's *results*; the log is diagnostic.

19. **Stalled-running/preempting reaper** (`stall_decision`, 2026-07-09 owner directive — the
    utilization audit found `hard_cap_hours` alone only catches *extreme duration*, not a box
    that's `live`, billing, and doing nothing). A `running` (or, per 19e, `preempting`) task
    whose `resume_checkpoint` file's mtime hasn't advanced in `stall_timeout_min` is stalled:
    a. **No new round trip.** This piggybacks entirely on the periodic checkpoint pull already
       done for invariant 9c/16 — `rsync -t` (invariant 9's flags) preserves the REMOTE write
       time on the local copy, so the already-local file's own mtime IS "when the box last made
       real progress," with no separate GPU-utilization sampling or extra ssh/vastai call needed.
    b. **Durable across a restart** (invariant 1): the signal is the checkpoint file's on-disk
       mtime, not an in-memory timer — a dispatcher restart just re-reads the same file and gets
       the same age.
    c. **Scope:** every `running` task is evaluated, whether or not it has ever had a checkpoint
       pulled — this is about *progress that stopped or never started*, not just progress that
       stopped after having begun (see c'' / bug 13 below for the no-checkpoint-yet case).
    c'. **The clock restarts on every (re)start of the task** (retrospective bug 12, observed
       live 2026-07-10): `resume_checkpoint` deliberately survives an infra-failure requeue
       (it's the retry's `--init-from` payload, invariant 16), so after the retry re-enters
       `running`, the pulled file's mtime still reads "when the PREVIOUS attempt last made
       progress" — 60+ min ago by construction, since a stall reap is what killed it. Without
       a restart-aware clock the reaper kills every retry within one poll (~6 s after `start`
       in the live incident, twice per task — both M16 pixel-Atari arms burned all retries
       this way and went terminal at 92%/98% complete). Rule: stalled iff
       `now − max(ckpt_mtime, running_since) ≥ stall_timeout_min`, where `running_since` is
       the task's most recent transition into `running` (`updated_at`, stamped by that CAS —
       same source invariant 8's worker-state ingest already trusts). Fixture:
       `stall.json` case `retry_fresh_start` — `ckpt_mtime` 100 min old, `running_since`
       2 min old → NOT stalled; case `stalled_after_restart` — both 100 min old → stalled.
    c''. **A task that never reaches its first checkpoint is not invisible to the reaper**
       (retrospective bug 13, surfaced 2026-07-13 during the m21 milestone: a run hung before
       ever pulling a checkpoint — e.g. stuck in provisioning/startup — was excluded from the
       reaper's query entirely, since it originally selected only `resume_checkpoint IS NOT
       NULL`, so it billed indefinitely with no automatic recovery). The anchor is now the max
       of *whichever* of `ckpt_mtime` / `running_since` are known — a checkpoint-less task ages
       off `running_since` alone once it exceeds `stall_timeout_min` with nothing ever pulled;
       only when BOTH are unknown (no checkpoint AND no known `running_since` — pre-c'
       callers/fixtures) is it left unflagged, since there's no anchor at all in that case.
       Fixture: `stall.json` case `never_checkpointed_but_running_since_fresh` (`running_since`
       2 min old) → NOT stalled; case `never_checkpointed_and_running_since_stale`
       (`running_since` 100 min old) → stalled.
    d. **Remediation reuses invariant 10 verbatim**: a stalled task is CAS'd `running →
       infra_failed` (detail `stalled: no checkpoint progress`) and goes through the exact same
       `retry_decision`/half-retry-budget path as any other infra failure — deliberately, even
       though a genuine entrypoint-level hang would reproduce identically on retry: the box-level
       failure modes this is meant to catch (transient GPU/driver hang, host I/O freeze) are NOT
       the task's fault and are usually NOT reproducible, and the shared retry budget (invariant
       10) already bounds the worst case (an unlucky truly-hung config burns its 6-infra-failure
       budget, then goes `terminal` like any other exhausted retry) — no separate stall-specific
       budget or classification was worth the added complexity.
    e. **Scope extended to `preempting`** (retrospective bug, 2026-07-14, companion to invariant
       17e): the query this reaper runs over used to be `WHERE state='running'` only, so a task
       that left `running` for `preempting` (invariant 17b) was invisible to it for as long as it
       stayed `preempting` — a preempt whose box-side checkpoint guard never fired (no checkpoint
       ever written since the `PREEMPT` marker) could hang indefinitely with NO automatic
       recovery at all, worse than an ordinary stall since it also blocks the evictor task that
       triggered the preemption from ever placing. Fixed by widening the query to
       `state IN ('running','preempting')`; `stall_decision` itself is unchanged. `updated_at` is
       restamped at the CAS into `preempting` (event `preempt_intent`), so `running_since` here
       reads as "how long this preempt attempt has been outstanding" and gets the same
       `stall_timeout_min` grace period as a fresh `running` retry (19c') before reaping.
       Remediation is identical (d): CAS to `infra_failed` then the ordinary retry/requeue path —
       the box-side process is not killed, but the DB slot frees and, thanks to invariant 17e, a
       late natural completion on the orphaned box is no longer stranded even if it arrives after
       this reaper already moved the task on.
    f. **TB-event freshness is a second liveness anchor** (2026-07-14, from a trainer-hygiene audit):
       `stall_decision` anchors on `max(ckpt_mtime, tb_mtime, running_since)`, not `ckpt_mtime`
       alone. Many trainers stream TB scalars continuously but write `ckpt_latest.pt` rarely, under
       a non-canonical name, or never — anchoring on the checkpoint alone false-reaped those as
       "stalled" while they were demonstrably alive, killing the run and losing its `results.json`
       (so the dashboard showed nothing). `_pull_tb_events` already rsyncs (`-t`) each running task's
       `tb/` every poll, so the newest local `events.out.tfevents*` mtime is the box's real
       last-scalar time. A job actively logging is never reaped; a genuinely hung one (no new TB AND
       no new checkpoint) still ages off the later of the two. Checkpoint mtime still counts, so a
       checkpointing trainer keeps its resume guarantee — this only removes the false positive.
    j. **A stall reap clears the task's BOX COPY before the requeue** (`_clear_stalled_box_copy`,
       2026-09-30). The requeue freed the DB slot and left the trainer running — 19e said so ("the box
       process isn't killed"). On a rented box teardown mostly ended it; an OWNED box is never torn
       down, so the trainer ran on as an orphan with no owner, invisible to `reap_orphans` (its
       `active/<id>` still existed, with no terminal marker). Measured: two G1 LLM cells reaped
       mid-evaluation kept the laptop's GPU at 100% / 11.1 of 12 GB with zero fleet tasks on it,
       blocking the requeued work, and a requeued copy shipped back to that box would have run beside
       its own ghost. Now `rm -rf ~/spool/incoming/<id> ~/spool/active/<id>` on a `live` instance —
       the act 18e and 19h already use, which makes `reap_orphans` collect a running trainer (its
       owning dir is GONE). The requeue resumes from the checkpoint already pulled home. Best-effort:
       an unreachable box or a failed ssh leaves exactly the pre-19j behaviour and never blocks the
       requeue. Event `stall_box_copy_cleared` (with the cleanup rc).

19g. **Orphaned-task reaper** (`_reap_orphaned_tasks`, retrospective bug 2026-07-15): a task in an
    on-box state (`claimed`/`shipped`/`running`/`preempting`) whose instance is NOT
    `provisioning`/`live`/`draining` (i.e. destroyed/lost/gone) is requeued via the invariant-10
    `_infra_fail` path. `_mark_lost` (invariant 3's lost-instance handler) only requeues occupants
    of an instance still *tracked* as live-ish that then vanishes from `vastai show instances`; once
    a box is `_destroy`ed — or the daemon was wedged when it died — its still-`claimed` occupant is
    examined by NOTHING: `_ship_all` skips a non-live box, the stalled reaper (19) watches only
    running/preempting, and the dead-worker reaper needs a live box with a stale HEARTBEAT. So the
    task strands forever. Observed live 2026-07-15: two tasks sat `claimed` on boxes destroyed 8h
    earlier. This pass looks at TASKS whose instance isn't live (rather than at instances) and runs
    after `do_reconcile` so instance states are current. Requires the new legal transition
    `claimed → infra_failed` (registry invariant 4) — without it `_infra_fail`'s first CAS silently
    no-op'd on a claimed task, which also latently broke `_mark_lost` for claimed occupants.
    `provisioning`/`draining` are deliberately treated as live-ish: a task claimed onto a box still
    coming up is normal, not orphaned.
19h. **Over-pack detector + auto-adjust** (`_reap_overpacked_boxes`, owner directive 2026-07-15):
    `slots_for_offer` (4a') sizes a box's `slots_total` from its *advertised* VRAM/cores, which can
    exceed what the GPU actually sustains concurrently — the box-side worker's launch gate
    (`sweep_supervisor.should_launch`, reused verbatim) then refuses to launch the excess trainer,
    leaving it wedged in `shipped` on a live, healthy box indefinitely. No other reaper covers this:
    the stalled reaper watches only running/preempting, the orphaned reaper needs a non-live
    instance, and the dead-worker reaper needs a *dead* worker — but here the worker is alive and
    actively running the box's other tasks. Observed live 2026-07-15: `native_m32/c4_hebkv_ll_s0` sat
    `shipped` ~50 min on an 8-slot RTX 4060 running 7 trainers. **Detection (DB-only, no files):** for
    each `live` instance whose last pull succeeded (`ConnectionTracker.consecutive_fails == 0`) and
    which has ≥1 `running`/`preempting` occupant (proof the worker IS launching), any occupant in
    `shipped` longer than `ship_launch_grace_min` is gate-held. The ≥1-running guard also makes a
    false positive safe: if the box were actually dead, requeue is still the correct action.
    **Auto-adjust, two parts:** (a) **learn** the box's true sustainable concurrency = its current
    running+preempting count, persisted in `settings` under `overpack_cap_m<machine_id>` (falling
    back to `overpack_cap_i<instance_id>` when machine_id is null), only ever lowered (min with any
    existing). `_instances_view` caps every instance's effective `slots_total` at
    `min(slots_total, learned_cap)`, so the packer never re-fills the freed slot and — because the
    key is per *machine* — a re-rented box of the same physical machine starts pre-capped. (b)
    **recover** each gate-held task: SSH `rm -rf ~/spool/{incoming,active}/<id>` on the box (the
    worker tolerates its active dir vanishing — same race the re-ship path relies on) and, only if
    that cleanup returned 0 (else defer to the next poll — never requeue a task the box might still
    launch, which would double-run it), transition it `shipped → queued` (registry invariant 4, new
    pair) with **`retries_used` UNCHANGED and `instance_id` cleared** — over-pack is the scheduler's
    misjudgment, not the task's fault (mirrors preemption, not infra-failure). The requeued task
    re-places onto a box with real headroom (or triggers a rent), and the learned cap prevents a
    reap-loop back onto the same box. Runs each poll in `_ingest_and_complete` after the other
    reapers. New setting `ship_launch_grace_min = 20` (generous: covers a legitimately slow
    launch/compile/gate-stagger; only a genuinely stuck task exceeds it) — **raised to 35 on
    2026-08-01 and superseded as the safety property by 19h-2; see there before touching it.**

19h-2. **THE RATCHET MAY NOT CONTRADICT OBSERVED EVIDENCE (2026-08-02).** 19h as written is a
    false-positive machine, and its damage is permanent: the cap is keyed per `machine_id`, is only
    ever lowered, and survives teardown, so one bad learn poisons every future rental of that host.
    **Measured over the 18 most recent `overpack_cap` learns in the live registry: 15 were on
    machines later observed running MORE concurrent tasks than the cap learned from them (83%).**
    Machine m10003 was ratcheted 7 → 6 → 4 → 1 while an instance on it ran 8 lanes at once; m100002
    and m100005 also sat pinned at 1 against per-box peaks of 6 and 7. Three machines needed a
    MANUAL settings reset on 2026-08-02, the third such incident.

    **The cause is not the grace, and raising it has now failed twice** (20 → 35 on 2026-08-01; all
    15 false positives above post-date that change). Two distinct defects, at different layers:
      * (L0, the metric) `_learn_overpack_cap` recorded whatever happened to be running at the
        INSTANT the reaper fired, so a transient dip — tasks completing together — wrote a cap of 1
        on a box that sustains 8;
      * (L3, the model) "shipped past grace ⇒ the box cannot sustain its advertised slots" conflates
        **still filling** with **over-packed**. `should_launch` has four TRANSIENT refusal reasons
        (`settling`, `cpu_load`, `gpu_util`, `vram`) on top of the steady-state `max_slots` ceiling,
        and a box at 7 of 8 lanes is by construction the most loaded it will ever be — so the last
        lane is the one most likely to be refused for load. The reaper was reading the launch gate
        working CORRECTLY as proof of a capacity ceiling, then making that reading permanent.

    **Three rules, and none of them is a threshold.**
    (a) **An evidence floor.** `_observed_peak_concurrency(inst)` is the most tasks ever seen running
        at once on this box's machine; `_learn_overpack_cap` REFUSES (and logs `overpack_cap_refused`)
        any cap below it rather than clamping silently, since a silent clamp would leave the reaper
        looking like it agreed. Computed from per-task INTERVALS — a lane is occupied from its
        `start` until that task's next event, whatever it is — never a +1/−1 counter over a hand-
        listed exit vocabulary, which leaked on `stalled` (128 live cases) and inflated two machines
        to a peak of 10 against an on-box `max_slots` of 8. Taken PER INSTANCE then maxed, never
        summed across concurrent rentals of one host: the cap is consumed per instance, and m100002's
        two overlapping 6-lane rentals read as 10 under a union sweep. Cached for at most one
        `poll_seconds` window and **never memoised for the process lifetime** — the peak GROWS as a
        box fills and the coordinator runs for days, so a permanent memo freezes a newly-rented box's
        floor at whatever it was on the first poll (often 1-2 lanes, because it was still filling),
        and that stale-LOW floor then accepts exactly the caps this rule exists to refuse. Cost of
        being correct is nil: 11.2 ms cold for the whole live fleet, ~2 us warm.
    (b) **Repair on read.** `_overpack_cap` raises (and logs `overpack_cap_repair`) any stored cap
        below the observed peak. Without this the pin is SELF-PERPETUATING — a machine held at 1 slot
        can never again be observed sustaining more, so it can never earn its cap back, which is why
        every previous incident required a human with a SQL prompt.
    (c) **Still-filling is not over-packed.** A box that started a lane within the grace window has
        just demonstrated the gate CAN launch, so it is skipped entirely. This is the guard for a
        machine with NO history, where (a) cannot help — its peak is low precisely BECAUSE it is
        filling for the first time.

    The asymmetry that sets the direction, and which 19h stated but did not implement: a false
    positive PERMANENTLY pins a machine across every future rental; a false negative merely delays
    detecting a genuinely over-packed box by one grace period. Round toward the cheap error.

    **RELATIONSHIP TO 19i — complementary, not a substitute; neither one closes this alone.**
    19i's central finding is that the cap SUPPRESSES ITS OWN EVIDENCE (a box capped at N is only
    ever shipped N tasks, so the reaper can only re-learn N), which correctly REFUTES raising a cap
    on the strength of NEW evidence and is why recovery must deliberately place ABOVE the cap.
    That argument does not reach (b): repair-on-read uses evidence the machine already produced
    **before** it was capped, which suppression cannot erase. So the split is:
      * **19h-2 (implemented)** — stops new false positives, and repairs the ones a machine's own
        history already contradicts. On the live registry that is 10 of 11 stored caps.
      * **19i (draft)** — the only path for a machine capped from its FIRST rental, which has no
        prior peak to appeal to. Still required.
    19h-2 shrinks 19i's job rather than removing it: with far fewer caps written, the probe runs on
    a much smaller population. `min()`-only semantics are unchanged, so 19i's note that
    `test_learned_cap_only_lowers_and_caps_instances_view` stands unchanged still holds.

19h-3. **A REFUSED LEARN MUST NOT LEAVE THE REMEDY FIRING FOREVER (2026-08-02).** 19h-2's evidence
    floor refuses a cap that contradicts the machine's observed peak — and `_reap_overpacked_boxes`
    then unschedules the gate-held task **anyway**, unconditionally. The diagnosis is denied while
    the remedy is applied, so **nothing changes and the same box burns `ship_launch_grace_min`
    again on the next cell, indefinitely.**

    **Measured on the live registry, the 11 h after 19h-2 landed: 12 `overpack` unschedules and 12
    `overpack_cap_refused` — a 1:1 pairing, i.e. EVERY burn since the fix happened on a box whose
    cap the floor had just frozen.** 8 campaigns hit (`m63_module_emit_full`, `m61_vocab`,
    `m61_token_binding`, `m61_primer`, `m58_craftax_budget`, `m62_color_cost`, `bus_on_slice`,
    `m65_slice_default_n6`), ~7 h of cell latency at 35 min each. It repeats on the SAME cell —
    `m62_color_cost/m62_nocolor_s1184f5` burned at 14:43 and again at 15:20; m100006 refused at
    11:25, 14:42 and 15:20 — which is the signature of a loop with no state change in it.

    a. **TWO FAILED ATTEMPTS, THEN A PLACEMENT COOLDOWN — the cap is NOT touched.** The floor is a
       HYPOTHESIS ("this box is still filling or transiently gated, not over-packed"). One gate-held
       task is weak evidence against it — exactly the false positive 19h-2 exists to stop, and it
       must keep stopping it. But a machine that gate-holds across
       `overpack_refusals_before_override` (2) CONSECUTIVE reap passes, each already past the "a
       lane launched within the grace" guard, has had **no lane start in ~70 minutes** while holding
       work. The response is `overpack_cooldown_i<id>`: this RENTAL takes no new work for one
       `ship_launch_grace_min`.
       ⚠ **Lowering the cap instead was tried first and is IMPOSSIBLE, not merely wrong** — 19h-2's
       repair-on-read restores any cap below the peak on the very next read, so the write is undone
       before it can matter. A test asserted the override and failed with `assert 8 == 4`. That
       failure is the useful part: it says the floor is RIGHT about the machine's capacity, so the
       thing to change is not the machine's permanent capacity but whether we keep feeding a rental
       that is not launching.
       The keying follows from the same reasoning: the **cap is per MACHINE** (how much this host
       can sustain, which outlives a rental) and the **cooldown is per INSTANCE** (is this box
       wedged right now, which does not). A co-tenant filling the shared GPU is a fact about today's
       rental; carrying it onto every future rental of that host is precisely the mistake 19h-2 was
       written to undo.
    b. **The unschedule stays UNCONDITIONAL — do not "fix" this by suppressing it.** The tempting
       symmetric fix (refuse the learn ⇒ refuse the remedy) re-opens the stranding hole 19h was
       built to close: a gate-held `shipped` task is covered by no other reaper (19 watches only
       running/preempting, 19g needs a non-live instance, 10c needs a dead worker). Recovery is
       free (`retries_used` unchanged) and relocates the task immediately; delaying it would cost
       latency to fix a problem that is about the CAP, not the task.
    c. **The counter is PERSISTED, not in-memory.** It must span consecutive grace windows — 70+
       minutes — and the coordinator restarts far more often than that (three times on 2026-08-02
       alone). An in-memory counter would reset on nearly every restart and never reach 2, which is
       the same class of defect as 28b. Keyed alongside the cap it guards
       (`overpack_refusals_<capkey>`), so it follows the machine across re-rentals exactly as the
       cap does.
    d. **Reset on POSITIVE evidence, not on time — and the cooldown ALSO self-expires.** Strikes and
       cooldown both clear when the box is seen launching (the `min(launched) <= grace` branch the
       reaper already computes), and a strike clears whenever a learn actually writes a cap. On top
       of that the cooldown is time-boxed by `ship_launch_grace_min`, so it cannot wedge a box even
       if the clearing path is never reached — the failure mode of every other one-way mechanism on
       this path, and the one this whole invariant exists to stop.
    d2. **`_fits_now` is where the exclusion goes**, beside `ship_quarantined` and `drain_held` and
       for the reason already stated there: `_infeasible_everywhere` and `_soonest_wait` must agree
       with the placement filter, or the fleet counts a wedged box's free slots as incoming capacity
       and declines to rent the box that could actually run the work.
    e. *This does not re-open the 83% false-positive rate.* Those were single-shot misreads of a
       box that was still filling. Requiring the same machine to gate-hold across two consecutive
       windows with zero launches in between is strictly stronger evidence than the pre-19h-2
       reaper ever demanded — it never looked at history at all.

19h-4. **THE LAUNCH GATE MUST SAY WHY IT REFUSED (2026-08-02).** `sweep_supervisor.should_launch`
    returns `(ok, reason)` and `spool_worker.launch_ready` wrote **`ok, _reason = ...`** — computing
    the single most diagnostic signal on this whole path and discarding it. So a gate-held task
    produced no evidence at all, and every over-pack incident of 2026-08-02 was diagnosed by
    INFERENCE: the box knew whether it was full, still settling, CPU-bound, or looking at a GPU a
    co-tenant had filled, and never said. The worker's own `n_live >= self.max_slots` check above it
    was silent for the same reason.

    This is the repo's "bias toward reusable observability" rule at its highest-leverage form — a
    signal at the SOURCE. It costs one line per state transition, nobody has to remember it exists,
    and it makes every future over-pack read strictly better for every box and every campaign.

    a. **The line carries THE NUMBERS THAT DECIDED IT, not just the branch name.** `vram` tells you
       which test fired; `vram: free 0.40GB < need 0.75GB of 12.0GB total` tells you whether the box
       is genuinely full or a co-tenant took the card — and those have DIFFERENT remedies (cap the
       box vs. leave the host). Anchored the way every diagnostic here is: the measured value
       against the threshold it was compared to, never one without the other.
    b. **The VRAM and GPU-util lines say `WHOLE CARD — includes co-tenants` out loud**, because a
       GPU is not cgroup-namespaced (invariant 28.d). Without that, the natural misreading is "we
       are full" when the truth is "someone else is", which is the reading that produces a wrong
       permanent cap.
    c. **EDGE-TRIGGERED, not per tick.** `launch_ready` runs every worker tick and `worker.jsonl` is
       rsynced home on every ingest pass, so an unconditional log would flood both the file and the
       wire. One line per state CHANGE is exactly the diagnostic content — when the hold began, why,
       and (by its absence) when it cleared.
    d. **The dispatcher attaches it to the `overpack` event** (`_last_launch_gate`, a bounded tail
       read of the already-pulled `worker.jsonl`). The event stops being "gate-held > 35min", which
       the reader must interpret, and becomes self-explaining. Best-effort by contract: a missing,
       torn or pre-19h-4 worker log yields None and the message degrades to its old wording — this
       runs on the unschedule path and must never block a task's recovery.
    e. *An unrecognised reason still produces a line.* A future `should_launch` branch must not
       silently vanish from the record, so the formatter falls through to `<reason>: n_live=N`
       rather than dropping it.

19i. **Over-pack cap RECOVERY — the cap must PROBE, because it SUPPRESSES ITS OWN EVIDENCE**
    (`_probe_overpack_cap`, owner directive 2026-08-02; closes the "still open" item in 27.e2).
    Status: **draft — not yet approved, not implemented.**

    ⚠ **Read 19h-2 first — it landed after this was drafted and changes two of the premises below.**
    (1) The population this probe must recover is now much smaller: 19h-2 refuses to write a cap that
    contradicts a machine's observed peak, and repairs the ones already stored (10 of the 11 live
    rows). (2) The `zero recoveries` observation stands as history but is no longer the live
    behaviour — repair-on-read is a recovery path for any machine with prior evidence. What 19h-2
    does NOT cover, and what keeps this invariant necessary, is a machine capped from its FIRST
    rental: it has no peak above the cap to appeal to, and suppression means it never will. The
    open questions below are unaffected and still block implementation.

    **The defect.** 19h's learned cap only ever falls (`_learn_overpack_cap` takes `min()`) and is
    keyed per `machine_id`, so it survives teardown and poisons every future rental of that host.
    Three incidents now: a hand-written `MANUAL raise` on 2026-07-28; the `max_slots_cap` 8→16
    revert on 2026-08-01; and **2026-08-02, when both owned boxes sat pinned at `overpack_cap = 1`**
    — 1 of 6 and 1 of 12 — while the fleet held 13 tasks queued and rented more boxes. Fleet-wide
    effective capacity was **34 of 90 advertised slots**. The ratchet had fired **27 more times
    after the 08-01 fix landed**, every trajectory monotone down, zero recoveries: `m100001`
    13→10→8→5→7→6→2→**1**, `m100007` 7→6→3→**2**, `m10003` 7→6→4→**1**. Because the key is per
    machine, three concurrent rentals of m100001 each paid ~$0.069/hr to run exactly **one** lane.

    a. **⛔ THE CONSTRAINT THAT KILLS THE OBVIOUS FIX — "raise on sustained success" CANNOT WORK.**
       A box capped at N is only ever *shipped* N tasks, so `_reap_overpacked_boxes` can only ever
       observe `running ≤ N` and re-learn `max(1, running) = N`. **The observation that would
       justify raising the cap can never be generated while the cap is in force.** Success at N is
       not evidence for N+1; it is evidence only that the cap is being obeyed. A cap of 1 is
       therefore a fixed point, which is precisely why all three incidents needed a human. Any
       recovery mechanism MUST deliberately place ABOVE the cap to create the missing evidence.
    b. **`_learn_overpack_cap` KEEPS its min()-only semantics — recovery is a SEPARATE mechanism.**
       `TestGraceExceedsFillTime`'s sibling
       `test_learned_cap_only_lowers_and_caps_instances_view` pins this deliberately: within the
       detector, learning must be monotone, or a box that is merely still filling would ratchet
       itself back UP mid-fill and re-trigger 19h forever. Recovery is a distinct, explicitly-
       scheduled probe with its own audit trail; it must not be smuggled into the learner.
    c. **The probe, and why it is cheap.** A machine whose cap has stood unchanged for
       `overpack_probe_after_min` becomes **probe-eligible**. `_effective_slots` then returns
       `min(slots_total, cap + 1)` for that machine — exactly ONE lane above the cap, never more —
       so the packer may ship one extra task. Two outcomes, both already-implemented machinery:
       - **The extra lane LAUNCHES** (reaches `running` within `ship_launch_grace_min`) ⇒ the cap
         was wrong. Raise it to the newly observed running count and reset the backoff.
       - **The extra lane is GATE-HELD** ⇒ the cap was right. 19h fires as it already does: it
         re-learns `max(1, running)` (a no-op at the current cap) and requeues the task **with
         `retries_used` UNCHANGED**. Double the probe interval, bounded by
         `overpack_probe_backoff_max_min`.
       The entire cost of a failed probe is **one task delayed by up to `ship_launch_grace_min`,
       then requeued for free**. 19h's zero-retry recovery path is what makes this affordable, and
       it is the reason the probe is preferable to a blind TTL expiry (which drops the cap to
       `slots_total` and can wedge `slots_total − cap` tasks at once, not one).
    d. **A probe requires REAL queued work — never manufacture it.** The probe is an *allowance*,
       not a placement: it widens `_effective_slots` by one and lets normal placement (4a/4b) use
       the slot if a genuine queued task fits. An empty queue simply means no probe happens and the
       eligibility clock keeps running. The probe must never rent a box, never preempt, and never
       displace a `--probe`-priority task.
       ⚠ **Naming hazard:** `runq add --probe` is an unrelated PRIORITY CLASS (a decision-blocking
       read that preempts lower-priority work). This is a *capacity* probe. Do not reuse the word in
       settings keys, events or CLI flags — the names here are `overpack_probe_*` throughout.
    e. **Cap rows become a struct, with backward compatibility.** Recovery needs per-machine state
       the current bare-int row cannot carry. New shape:

           {"cap": 3, "at": "2026-08-02T03:02:22Z", "backoff_min": 240, "probed_at": null}

       `_overpack_cap` MUST still accept a legacy bare int (`3`) and treat it as
       `{"cap": 3, "at": <epoch-unknown>, "backoff_min": <initial>, "probed_at": null}` — every
       existing row in the live registry is a bare int, and a migration that dropped them would
       silently uncap the fleet. An unparseable row is treated as ABSENT (fail-open to
       `slots_total`), matching 27.c's "a missing or renamed field must never silently deny".
    f. **Owned boxes recover on the same mechanism but SHOULD NOT be the common case.** For
       `source='owned'` the operator has *declared* capacity in `configs/capacity/<label>.json`, and
       the designed gates are the time-of-day window plus measured headroom (23) and the per-task
       budget check (18a) — all of which adapt on their own. A learned cap below the declared
       window is nearly always the 19h false positive, not physics. See open question **19i-Q1**.
    g. **Observability is part of the deliverable, not a follow-up.** Emit an `overpack_probe` event
       on every probe START, and on its resolution (`raised` with the new cap / `held` with the new
       backoff). The `_instances_view` payload already carries `slots_eff`; add `slots_probe` and
       `cap_age_min` so the dashboard answers "why is this box only half full, and when will it try
       again?" without a registry query. This is the reusable-observability rule: all three
       incidents were diagnosed by hand-querying `settings` and reconstructing the trajectory from
       `overpack_cap` events.
    h. **The asymmetry INVERTS once this lands, and that is the point.** 19h/27.e2 currently round
       toward the cheap error because "a false positive pins a machine permanently while a false
       negative delays detection by 15 min". With recovery, a false positive becomes **temporary**
       (bounded by `overpack_probe_after_min`), so the standing justification for rounding hard
       toward pinning weakens. Do NOT re-tune `ship_launch_grace_min` or `max_slots_cap` in the same
       change — that is exactly the coupled edit that caused the 08-01 incident. Land recovery,
       observe the probe outcomes, then revisit the grace with data.
    i. **New settings** (all in `DEFAULT_SETTINGS`, all overridable per-fleet):

           overpack_probe_after_min       = 240    # cap must stand this long before probe-eligible
           overpack_probe_backoff_max_min = 1440   # ceiling on the doubling
           overpack_probe_enabled         = true   # kill switch, mirrors consolidate_enabled

20. **Owned boxes** (`register_owned_box.py`, home-hardware home-farm model foreshadowed in
    VAST-TEST.md's "two-pool" note): a statically-configured, self-owned, always-on machine
    (a home laptop/desktop GPU, reachable at a fixed `host:port` on the operator's own LAN) is
    registered directly as an `instances` row — `state='live'`, `dph_usd=0`, `source='owned'`
    — bypassing `_rent`/`_provision` entirely (there is no Vast offer to rent, no boot to poll).
    **No placement changes were needed**: invariant 4a/4b's pack-first ordering already prefers
    ANY `live` instance over renting, regardless of provenance, so an owned box with free slots
    wins over a fresh Vast rental for free. What DOES need care is every OTHER lifecycle path,
    which assumed until now that every `instances` row is a Vast rental:
    a. **Reconcile (invariant 3) skips `source='owned'` rows entirely**, before either the
       lost-check or the stuck-provisioning check — an owned box will never appear in `vastai
       show instances` (it isn't a rental), so that absence must never be read as `lost` the way
       it would be for a real rental.
    b. **Teardown (invariant 11) never fires for an owned box** — `should_teardown` returns
       `(False, "owned_box_never_torn_down")` unconditionally. Unlike a Vast rental, idle time
       costs nothing, so there is never a reason to tear one down.
    c. **`_destroy` refuses to touch a `source='owned'` instance**, logging `destroy_skipped`
       instead — the single choke point every teardown path funnels through (idle timeout,
       dead-worker reap, stuck-provisioning reap), so a future caller can't forget the guard.
       This matters even with (b) in place: the dead-worker reaper (invariant 9's heartbeat
       staleness check) still calls `_destroy` when an owned box's worker goes quiet (e.g. the
       laptop asleep/off overnight) — its occupant tasks still requeue normally (the same
       `_infra_fail` path as any infra loss), but the box's row must survive so it's packed onto
       again, with no re-registration, the moment it's reachable and heartbeating.
    d. **No hard cap in practice**: `hard_cap_at` is stamped 10 years out at registration —
       there's no rental clock to bound.
    e. **`_bring_up_worker`** (ssh-probe (one attempt) → push `spool_worker.py`+siblings → launch,
       shared with the async-provisioning path's `_advance_provisioning`) deploys the identical
       worker a freshly-rented Vast box gets. `register_owned_box.py` has no poll loop of its own
       to retry a failed probe across, so it wraps the single-attempt call in its own bounded
       retry loop (`ssh_probe_attempts`/`ssh_probe_interval_s`) — the same shape the old blocking
       `_provision` used before the async-provisioning split. Idempotent by `--label`: re-running
       updates the row in place rather than duplicating it.
    e-1. **Re-registration changes what it was told to change, and nothing else** (2026-10-03).
       One function, `registry_db.upsert_owned_box`, serves both the CLI and the API route, so the
       two cannot drift. For a box already registered under that label:
         - `--host` is written;
         - `--port`, `--slots` and `--gpu-name` are written only when GIVEN, and otherwise keep
           their current value (a NEW box defaults to port 22, 1 slot, no GPU name);
         - a `paused` box STAYS `paused`, its `pause_i<id>` record untouched; any other state
           becomes `live` and `destroyed_at` clears, which is how a box that changed address is
           revived.
       Before this both paths wrote every column unconditionally and set `state='live'`. So
       re-pointing a held box at a new address handed it straight back to the packer with its pause
       record left behind, and an omitted `--port` / `--slots` / `--gpu-name` silently reset to 22 /
       1 / NULL — the last of which also disarms the GPU-loss alert (inv. 30), which only watches a
       box registered WITH a GPU. The documented recreate flow is drain → set up → register, so the
       old behaviour un-drained a box as a matter of course. The API route additionally looked the
       label up without `source='owned'`.
    f. **`id` is a negative integer** (one below the lowest id already in use), guaranteeing it
       can never collide with a real Vast instance id (always positive) — a purely defensive
       convenience; the `source` column, not the id's sign, is what every guard above actually
       checks.
    g. **Deliberately out of scope here**: occupancy-aware availability (e.g. a desktop that's
       only free ~midnight–7AM while its GPU is otherwise in interactive use) — this covers only
       an always-available box. A desktop's presence/absence would need its own signal gating
       whether it's even offered to `place()`, which is a separate piece of design saved for
       when it's actually added (VAST-TEST.md's two-pool note).
    h. **Unreachable-owned quarantine — graceful recovery** (2026-07-16, observed live wedging the
       whole fleet on `laptop-gpu`): (a)–(c) keep an owned box's *row* alive across a nap, but they
       do NOT stop a **fully-unreachable** owned box (SSH/rsync itself failing — laptop off, asleep,
       or off-LAN) from wedging the fleet. Such a box stays `state='live'` (reconcile skips it per
       (a); `_destroy` refuses it per (c)), and pack-first placement (4a/4b) always prefers ANY
       `live` box, so every queued task is re-`claimed` onto it each poll and its ship silently
       fails — while **none of the four reapers can free it**: the dead-worker reaper (invariant
       9/10c) is gated on `consecutive_fails == 0`, and any nonzero count — exactly what an
       unreachable box produces on every failed pull/ship — *suppresses* it; a `claimed`-but-never-
       shipped task has no heartbeat, no running-clock, and a still-`live` instance, so the
       stall/orphan reapers skip it too. It also **can't be denied** (4e's denylist keys on
       `machine_id`, which is NULL for an owned box). Net: an unrecoverable stuck loop only a human
       clearing the box breaks. (c)'s claim that "occupant tasks still requeue normally" holds only
       for the SSH-*reachable*-but-quiet case — it relies on the dead-worker reaper, which the
       unreachable case structurally never reaches.

       Fix — a soft, self-healing quarantine keyed on `source`/`id` (never `machine_id`, never
       `_destroy`), in `_reap_unreachable_owned`, run each poll in `_ingest_and_complete` after the
       other reapers (so this poll's pull results are already reflected in the tracker):
       - **Quarantine**: a `source='owned'`, `state='live'` box whose
         `ConnectionTracker.consecutive_fails` (the same counter every ssh/rsync pull AND ship
         already feeds) has reached `owned_unreachable_fails` (new setting, default `3`) transitions
         `live → unreachable` — a **new instance state**, distinct from `destroyed` (which is refused
         for owned boxes and never re-adopted). Its stranded on-box tasks
         (`claimed`/`shipped`/`running`/`preempting`) requeue via the same `_infra_fail` half-retry
         path as any infra loss, freeing them to pack onto another live box or rent. `unreachable`
         is excluded from placement automatically — `place`'s pool is `state == 'live'` only — so
         the fleet proceeds.
       - **Recovery**: each poll, every `unreachable` owned box is probed by attempting to **bring
         its worker back up** — `_bring_up_worker` (20e's ssh-probe → push `spool_worker.py`+siblings
         → launch), NOT a bare `echo` reachability check. This is deliberate: an owned box goes
         `unreachable` because ssh/rsync failed outright (laptop off/asleep/off-LAN), and the usual
         way it comes back — a reboot or full power-cycle — kills the box-resident `spool_worker.py`
         process. A bare `echo` would succeed the moment sshd answers and flip the box to `live` with
         **no worker running to claim the tasks packed onto it**, turning it into a black hole:
         pack-first placement (4a/4b) keeps shipping to it, nothing claims, and the dead-worker reaper
         (invariant 9) can't recover it because `_destroy` is refused for owned boxes (20c) — the very
         wedge 20h exists to prevent, wearing a new disguise. So recovery re-runs the full worker
         bring-up (its own ssh probe subsumes the `echo`); only its **success** (worker up — freshly
         started, or already alive) resets the failure counter and transitions `unreachable → live`,
         honoring (c)'s "packed onto again … the moment it's reachable **and heartbeating**" promise
         in full — not merely reachability. A box whose ssh still fails, or whose worker won't start,
         records another failure and stays quarantined.

         **Idempotent restart**: the worker launch (`_bring_up_worker`'s final step, shared by every
         provisioning path) never spawns a **second** `spool_worker.py` against a spool that already
         has a live one — the box-resident worker takes no singleton lock, and two concurrent workers
         both race on claim (`claim_ready`'s check-then-rename) and both re-attach to the same
         `active/<id>` dirs on start, a genuine double-run. Because recovery re-runs bring-up
         unconditionally (it can't tell a rebooted laptop, whose worker is gone, from a merely-slept
         one whose worker survived), the launch is guarded: `_bring_up_worker` first probes for a
         running `spool_worker.py --spool` (via `pgrep`, using the `[s]pool…` self-match dodge so the
         probe shell doesn't count itself) and launches only if absent. So re-bring-up restarts a dead
         worker (reboot) and no-ops against a live one (sleep). This also hardens the ordinary
         re-ship / re-adopt paths, which already call `_bring_up_worker` more than once.

         Cost is zero throughout (`dph_usd=0`), and the counter is in-memory so a daemon restart
         simply re-derives the state (a live-but-dead box re-quarantines within a poll or two; a
         still-quarantined row is probed back the moment it answers). Owned-box-only: a Vast rental
         that goes unreachable is destroyed and re-rented (invariants 3/9), a path an owned box
         structurally lacks.

    i. **A live box's worker UPDATES ITSELF — no manual step, no coordinator-side kill**
       (2026-08-01). `_bring_up_worker` has always rsynced the four bootstrap files on every call
       and started a worker only when none is running, so calling it against a healthy box is
       exactly "refresh the code on disk, touch nothing else". It was simply never called for a box
       already `live` — so an **owned box ran whatever worker code it booted with, indefinitely**
       (months), and a rental only picked up a change when it was replaced. Concretely: inv. 11
       shipped and not one live box could use it.

       Two halves, split so that neither has to know the other's timing.
       - **Delivery** — `_refresh_workers`, a poll phase, calls `_bring_up_worker` for every `live`
         box at most once per `worker_refresh_min` (30). Owned boxes included: they are the ones
         that never churn, and therefore the ones that needed it. Best-effort — an unreachable box
         is stamped anyway so a failure backs off rather than retrying every poll, and no exception
         escapes into the cycle.
       - **Activation** — the WORKER re-execs itself. It fingerprints its own four source files at
         startup (`_source_fingerprint`, also written to `~/spool/WORKER_VERSION`) and re-checks
         after every `tick()`; on a change it `os.execv`s the same interpreter and argv.
         **The coordinator deliberately does not kill and relaunch it**: the only safe moment is
         between ticks, which is invisible from outside, and on a rental nothing would relaunch a
         worker that failed to come back.

       Why re-exec is safe here: trainers are launched with `start_new_session`, so replacing this
       process does not touch them; `validate_and_prepare` rebuilds every in-memory spec from disk
       on the way back up, which is the same path a crash already took; and `worker.jsonl` is opened
       `"a"`, so no history is lost.

       Two guards, both load-bearing:
       - **The new fingerprint must be seen TWICE** (one `POLL_SECONDS` apart) before exec'ing. The
         four files are rsynced individually, so a single sighting can be a set caught mid-delivery.
       - **The new sources must COMPILE** (`_sources_compile`). Nothing relaunches a worker that
         dies on a rental — the box is reaped hours later by `heartbeat_stale_min`, requeueing its
         tasks — so a torn rsync or a syntax error must never be exec'd into. This is the guard that
         makes an automatic update acceptable at all.

       **The BOOTSTRAP, and why it is needed exactly once** (`_retire_pre_20i_worker`). Self-update
       lives IN the worker, so a worker started BEFORE 20i has no re-exec loop and runs its boot-time
       code forever however fresh the files on disk are. That is precisely the state every live box
       was in when 20i landed — MEASURED: delivery ran (33.2s on the first cycle, all 8 boxes) and
       not one worker changed. Such a worker is identified by the ABSENCE of `~/spool/WORKER_VERSION`,
       which only a 20i worker writes, so this fires at most once per box and never again.
       It is gated on the box having **no `claimed` or `shipped` task**: a worker killed mid-`_unpack`
       leaves a partial `repo/`, and `validate_and_prepare` treats an existing `repo/` as
       already-extracted on the way back up — it would run a TRUNCATED tree. A `running` occupant is
       not at risk and does not block (separate session, survives). The caller then re-runs
       `_bring_up_worker`, whose probe finds no worker and launches the current code.

       ⚠ A refresh failure is **logged** (`worker_refresh_failed`), not suppressed. The first
       revision swallowed it, and the phase then read `0.0s` while doing nothing with no way to tell
       whether it was throttled, broken, or complete.

       ⚠⚠ **…and that logging must itself be unable to raise** (`_safe_log`). MEASURED live
       2026-08-01, the second revision's own failure mode: the registry is one SQLite file shared
       with every `runq` in every worktree, so a write CAN legitimately time out. `_refresh_workers`
       caught a `database is locked`, tried to LOG it **through the same connection that had just
       failed**, the log raised, and the daemon died mid-poll. The supervisor respawned it, which
       cleared the in-memory refresh throttle, so the next pass retired the same workers again —
       a crash loop built entirely out of error handling, and visible only as a box being retired
       twice five minutes apart under a thirty-minute throttle. Every best-effort recorder on this
       path routes through `_safe_log`, which falls back to the coordinator log (no lock needed).

21. **Cost consolidation** (`consolidation_drains`, pure; executed by `_consolidate` on the poll
    loop AFTER placement, BEFORE teardown; gated by `consolidate_enabled`, default on) — the missing
    third leg of fleet sizing. Placement (4b) packs owned-first and teardown (11) releases *idle*
    boxes, but a task that landed on a **paid** box while the owned box was busy rides that box to
    completion even after the owned box later frees up; nothing migrates running work onto reclaimed
    capacity (live 2026-07-22: two tasks stranded on paid boxes at $0.135/hr while the owned box sat
    2/6). Once per poll, for each **live paid box** (`dph_usd > 0`, `source != 'owned'`) whose
    **entire** running load fits onto OTHER capacity that stays alive regardless of it, gracefully
    drain all of its occupants so they requeue (invariant 16 checkpoint carry) and re-pack (4b) onto
    that capacity, leaving the source idle → torn down (11). The drain is the
    invariant-17 graceful `PREEMPT` path verbatim (`_evict_task_graceful`: mark `running →
    preempting`, touch `PREEMPT` once; the trainer checkpoints then exits at its next save —
    invariant 17b — so **no work in flight is interrupted before a checkpoint**), the same primitive
    the box-pause hard drain (box-pause inv. 12) and capacity scale-down already use. A graceful
    preempt does not charge a retry (only `_infra_fail` does — invariant 10), so a drained task
    resumes with its budget intact.

    a. *Whole-box only.* A partial drain of a box that stays alive for its remaining occupants saves
       nothing (`_pack_cost`: the box keeps billing) and only burns a checkpoint cycle, so a paid box
       is a candidate only if ALL its occupants are settled `running` AND all can be relocated. Any
       occupant in `claimed`/`shipped`/`preempting` makes the box ineligible this poll — a drain is
       already in flight or the box isn't settled — which is also what makes it idempotent: after the
       first drain marks the occupants `preempting`, the box is skipped until they clear.

    b. *Targets are boxes that stay alive regardless — not merely cheaper ones.* A box is worth
       tearing down when its tasks fit on capacity that would be billing ANYWAY: the owned box
       (`$0`), or another box kept alive by its OWN occupants. Killing the source saves its full
       `dph`, and riding a survivor's spare slot is ~$0 marginal (`_pack_cost`). This is deliberately
       NOT restricted to strictly-cheaper targets — it must also capture the **collapse** (owner
       directive 2026-07-22: "if 4 tasks conclude on two boxes and those can collapse into one, do
       that"), where two equal-priced half-full boxes merge into one. It does **not** relocate onto
       an *empty paid* box (that box is itself idle → about to be torn down; riding it would only keep
       a dying box alive). "Free" honors the capped `slots_total` the packer sees (`overpack_cap` /
       capacity schedule already applied in the view), so a drain never targets capacity the box's
       launch gate would just wedge. Sources are vacated **most-expensive-first** (largest $/hr
       reclaimed; ties → the emptiest box, cheapest to clear); the shared free-capacity pool is
       consumed greedily so two sources never double-book a slot; and a box that RECEIVES a
       relocation is **pinned** (can't also be vacated) — so there is no ping-pong and no circular
       strand (A→B while B→A). Among eligible targets, cheapest/owned-first, to ride the least-cost
       survivors and minimize any lifetime extension.

    c. *VRAM safety (invariant 22).* The packer is slot-count-only and VRAM-blind, so a
       consolidation that relocated a GPU-heavy task onto a slot-free-but-VRAM-full box would OOM it.
       A drain is initiated only if the measured **free VRAM** (`vram_total_gb − vram_used_gb`,
       invariant 22) summed over the target boxes ≥ the source box's measured VRAM footprint
       (`vram_used_gb`) + `consolidate_vram_margin_gb`. When either side lacks a measurement (never
       sampled, or unreachable), the VRAM gate is skipped and slot-count governs — consolidation is
       thus **never less safe than ordinary placement** (also VRAM-blind), and strictly safer when
       measurements exist.

    d. *Never drain near-done or backlog-contended work.* A task within
       `consolidate_min_remaining_min` of its `est_minutes × est_safety` window is left to finish
       (the checkpoint-cycle + re-warm cost exceeds the savings — the near-done case); a task already
       *past* its estimate is drain-eligible (a chronically-overdue paid box is exactly what to
       reclaim). ⚠ This clause USED to claim that "running after placement over only the capacity no
       queued task claimed means consolidation never steals slots a real backlog needs." **That was
       false and is superseded by (f).** Running after placement only protects capacity placement
       ALREADY CLAIMED this pass; a box FULL of running work has no free slots, so placement never
       considers it at all — and consolidation then empties it.

    f. *A box the BACKLOG still needs is not a box to reclaim* (2026-07-29). Draining only pays off
       if the box then goes IDLE and teardown (11) destroys it — but 11 refuses to destroy any box a
       queued task still fits (`feasible_task_waiting`). Under a backlog the drained box therefore
       stays `live`, gets REPACKED, and the drain buys nothing while costing one preempt per
       occupant, each of which must re-traverse the (bottlenecked) ship path.
       **Measured 2026-07-29: 10 of 10 consolidations ended with the box still `live` — ZERO
       teardowns** — while 51 tasks were preempted and 6 lost their checkpoints outright. Box
       40000012 was drained at 04:49 and had work shipped back to it 10 min later. The contradiction
       was visible inside 50 seconds: `rent_created` ×4 at 06:39:24-56 (26 queued, no capacity)
       followed by `consolidate` at 06:40:14 draining a working paid box.
       So `consolidation_drains` takes the queued list and skips any source a queued task is
       feasible for, mirroring `should_teardown`'s own test exactly so the two cannot disagree about
       whether a box is needed. `minutes_to_hard_cap` defaults to infinity when absent, so a missing
       field can never cause a NEEDED box to be drained. A queued task too big for the box does NOT
       pin it — otherwise one oversized task would keep every rental alive.

    g. *A drain must EARN its disruption* (2026-07-29, owner directive: "we do want repacking to
       happen, but it should be as little disruption as practical — saving a few cents on a repack is
       penny wise, pound foolish"). Two gates, both MEASURED over 95 real drains:
       **(i) value, priced PER TASK preempted** (`consolidate_min_savings_per_task_usd = 0.05`).
       The median drain preempted **10 tasks to save an upper-bound $0.174** — and that bound uses
       the full `est_minutes` window, which runs ~3.4x long, so the real figure was nearer 5 cents.
       Per-task is the right unit because a preempt costs that run's progress since its last
       checkpoint, a re-ship (median 30s, p90 135s), and — unpriceable — one more resume cycle,
       which makes the arm less comparable to its siblings (resume does not reproduce; run-registry
       spec inv. 4). EXEMPTION: a box with an OVERDUE occupant stays eligible, because past its
       estimate the remaining life is unknown and pricing it at $0 is an artefact, not a
       measurement — (d) already settled that a chronically-overdue box is what to reclaim.
       **(ii) anti-repack cooldown** (`consolidate_cooldown_min = 60`). Repack latency is
       **median 5 min** (p90 30, max 149) — far inside the 10-min idle timeout a drain needs to end
       in a teardown — so on a busy fleet the box is refilled before it can be reclaimed and drained
       again: one box was drained **14x in 13h for 47 preempts and ZERO teardowns**. 60 min blocks
       94% of observed repacks, and only ever penalises a box that SURVIVED a drain (a reclaimed one
       is gone). State comes from the `consolidate` events themselves, so it survives a restart
       (inv. 1 — no in-memory timer).
       Replayed against those 95 drains, the two together take **95 drains -> 10 and 1201 preempts
       -> 33 (98% less churn)**, keeping the ones that save $0.05-$0.78.

    h. *The DRAIN HOLD — a drained box takes no new work until it empties* (2026-07-29). (g) made
       drains rarer; it did not stop a drain **undoing itself**, which is the actual race. A drain
       requeues its occupants; those become ordinary `queued` work; and the box they just left is —
       by construction, since they were running on it — a perfect fit. So the very next
       `_place_queue` ships them straight back, and `should_teardown` then reports
       `feasible_task_waiting` against the very tasks the drain evicted.
       **Measured 2026-07-29, box 40000015: drained 16:51 with 5 preempts, and 3 of those 5 tasks
       were running on it again by 16:57 — with the QUEUE AT ZERO**, so this is the drain feeding
       itself, not contention. Fleet-wide, **79 of 111 drains ever (71%) were repacked before any
       teardown.**
       **The cost is scientific, not `$/hr`.** One victim (`azsc-p1e/…_seed3`) reached **11 resume
       cycles** against siblings at 7-9 in a live campaign; resume does not reproduce an
       uninterrupted run (run-registry inv. 4), so which arms stayed comparable was decided by which
       box consolidation happened to drain next.
       A box that is drained is therefore **held out of the placement pool** (`drain_hold_i<id>` in
       `settings`, mirroring `ship_quarantine_i<id>`; DB-backed so it survives a restart per inv. 1).
       The hold is consulted in `_fits_now` — not only in `place()`'s pack filter — so
       `_infeasible_everywhere` and `_soonest_wait` agree with it and the fleet does not count a
       draining box's slots as incoming capacity and decline to rent the box that could run the work.
       A held box is additionally never a consolidation **source** (re-draining only re-preempts
       stragglers) and never a **target** (relocating onto a box we are emptying would undo its
       drain). `should_teardown` reclaims a held box the moment it is EMPTY — after the occupants
       check, so a drain still in flight is never destroyed under itself, and with no idle wait,
       because the hold already guarantees nothing new was placed.
       **The hold EXPIRES** (`consolidate_drain_hold_min = 40`), or a box whose occupants never
       checkpoint would sit held *and billing* forever — the exact idle-bill failure the reapers
       exist to prevent. 40 min is MEASURED: over the 31 **clean** drains (never repacked, ended in
       teardown) drain→teardown is median **13.2** min, p90 **33.5**, p95 **42.2**; 40 covers 90%.
       It is deliberately **shorter than `consolidate_cooldown_min` (60)** so the two guards compose
       — a box released from hold still cannot be re-drained — rather than oscillating.

    i. *A drain hold may never CAUSE a rental* (2026-07-29). (h) fixed the drain feeding itself, and
       introduced a second-order failure: the drain's justification is that the box's load fits on
       capacity that stays alive REGARDLESS, which `consolidation_drains` checks at DECISION time —
       but a graceful drain takes minutes (17b waits for a checkpoint newer than the marker), and by
       the time the tasks requeue the targets can be full. The hold then removes the one box that
       could obviously absorb them, so placement RENTS.
       **Measured on the second drain under (h): box 40000015 drained 17:51:39 at $0.0496/hr, and a
       new box was rented 58 seconds later at $0.0523/hr** — dearer than the one being reclaimed —
       taking 4 of the 5 preempted tasks. Without the hold they would have returned to the source and
       nothing would have been rented, so (h) had traded "repack the same box" for "rent another
       one", which is strictly worse: the preempts are spent AND a second box is now billing.
       A `rent` decision is PROOF the drain's premise was false, so the drain yields:
       `_place_queue` consults `_abandon_drain_rather_than_rent` before applying any `rent`
       placement, lifts the hold on the CHEAPEST held box that would fit the task (so if several are
       draining we keep the least costly alive), and re-places. The box survives and keeps working;
       the preempts already spent are sunk either way, but nothing further is lost and no money is
       spent. A held box that could not fit the task anyway is NOT abandoned — it is not the reason
       we are renting, and abandoning it would keep the box, spend the preempts, and still rent.
       Emits `drain_abandoned`.

    j. *A hard CEILING on how many runs one reclaim may interrupt* (2026-07-29, owner: "I still keep
       hearing about preempts that cause trouble for the task owner" — reported with (g), (h) and (i)
       all already live). The earlier gates could not have fixed this: (a) requires a WHOLE-BOX
       drain, so a full box costs one preempt per occupant, and (g)'s per-task price still clears a
       10-task drain whenever the box has a long enough remaining life.
       **MEASURED over 24h / 81 drains / 313 preempts: consolidation caused 93.4% of EVERY preempt
       in the fleet**, 27 of which lost their checkpoint outright, for an upper-bound $3.51 saved —
       **~$0.011 per preempt inflicted.** One cent per interrupted experiment.
       A ceiling is the right instrument rather than a finer price because of the shape of the
       distribution: **a drain that SUCCEEDED never preempted more than 8 tasks, median 3**, while
       the futile ones ran 20-77 — the big drains are almost pure harm. Replayed against those 81
       drains, `consolidate_max_preempts = 4` keeps 19 of 31 reclaims and $2.10 of $3.51 while
       avoiding **215 of 313 preempts**: 60% of the money for 31% of the disruption. Occupant count
       is known at decision time and, for a whole-box drain, IS the number of preempts, so this is a
       bound and not an estimate. The comparison `len(occupants) > cap` is deliberate — a drain of
       exactly `cap` is allowed.

    e. *Observability.* Each drained box emits a `consolidate` event (occupant count, targets, est
       `$/hr` reclaimed, and the hold window applied). A hold that runs out without the box emptying
       emits `drain_hold_expired`; a drain that yields to avoid a rental emits `drain_abandoned`.
       Pure decision function returns
       `[{"instance_id", "task_ids", "targets", "dph_reclaimed"}]`; empty when nothing is safely
       consolidatable.

22. **Measured box resources** (`_measure_box_resources`, poll loop, throttled to
    `resource_measure_every_min`): the coordinator packs on the static `vram_per_lane_gb` /
    `cores_per_lane` *hints* supplied at `runq add`, never checked against reality — live audit
    2026-07-22 found them ~3× over on a GPU workload and ~1000× over on a CPU-bound (JAX-on-CPU) one,
    so packing (and any VRAM-aware consolidation) runs blind. This samples each live box's real GPU
    memory (`nvidia-smi --query-gpu=memory.total,memory.used`, the same probe `sweep_supervisor` runs
    box-side) on a cadence and records `vram_total_gb` / `vram_used_gb` on the instances view for
    invariant 21c and hint-vs-actual visibility. Read-only, best-effort, **in-memory** (invariant 1:
    a restart just re-measures within a cadence, mirroring the owned-unreachable counter — no
    teardown/rent decision depends on it). A box without `nvidia-smi` or unreachable carries no
    measurement (21c falls back to the hint). Per-process attribution is deliberately NOT used — it
    reads `[N/A]` under WSL2 (the owned laptop) — so the box-level total is the robust signal, and a
    whole-box drain (21a) needs only the box aggregate anyway.


23. **THE POLL CYCLE TIMES ITSELF (2026-07-31).** `poll_once` wraps every phase and emits ONE
    `poll_cycle` event per cycle: `{total_sec, n_boxes, cycle_over_heartbeat_stale, ship_duty, phases}` with
    the per-phase breakdown sorted worst-first.
    **Why this is an invariant and not a nicety:** `poll_seconds` is 30, but a real cycle is
    dominated by serial network I/O (~5 rsyncs per box in `_ingest_and_complete`, each up to a 60 s
    timeout, plus a 300 s ship budget), so its true length is an EMERGENT property of fleet size and
    box latency — and it was measured NOWHERE. At least three tuned settings are really predicates
    on it: `heartbeat_stale_min` (silently disables the dead-worker reaper past it — invariant
    10c(g)), `ship_budget_sec` (makes ship throughput a DUTY CYCLE of it), and
    `checkpoint_pull_every_min` (cannot outpace it).
    Measured at the 2026-07-31 incident: a 31-min cycle against the ~7-9 min design point both
    settings were calibrated for ⇒ ship duty collapsed to **16%** (~14 ships/hr, a 26-task `claimed`
    backlog, one cell sitting **4h42m** between claim and ship) while the reaper skipped 97.9% of
    boxes. Both numbers were derivable only by reverse-engineering gaps between unrelated event
    timestamps, which is hours of work to answer "is the loop keeping up?".
    Recorded in a `finally` so a phase that RAISES still reports the time it burned (a silent crash
    mid-cycle is otherwise invisible); suppressed under `--dry-run`, whose contract is no ssh/vastai
    calls and no DB writes and which skips every phase measured. Read it with:
    `SELECT t, detail FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 20;`
    ⚠ Deliberately does NOT retune `ship_budget_sec` / `heartbeat_stale_min` — those should be
    re-picked ON this instrumentation, not by feel ahead of it.

24. **IN-FLIGHT `est_minutes` RECALIBRATION (2026-07-31).** Every scheduling decision uses a
    LEARNED per-`(entrypoint, group)` estimate once that campaign has `LIVE_MIN_SAMPLE` (3) finished
    siblings; otherwise the declared `est_minutes` stands. `est_defaults.learn_group_estimates` is
    the pure statistic (`ceil(p90)` of sibling start→done minutes); `Dispatcher._effective_est`
    applies it at the two view builders (`_task_view`, `_occupant_view`) so every consumer —
    `_window_minutes_needed`, `_remaining_est`, `_soonest_wait`, `_pack_cost`, the backlog gate, the
    preempt/consolidate paths — agrees on one number. Cached per `poll_seconds`; scoped to groups
    with PENDING tasks and a 14-day lookback.
    **Why:** `est_minutes × est_safety` is what the dispatcher believes a lane stays busy for, so a
    systematic over-estimate makes it rent instead of wait. Measured 2026-07-31: fleet median
    actual/est **0.37** over 1381 done tasks, 26 of 48 groups over-estimating by >2× (the native
    line at 0.07–0.25) — and **56% of the 140 boxes destroyed in 3 days never started a single
    task**, with 55.7% of rented lane-hours idle ($21.76 of a $39.08 3-day spend).
    **Why GROUP and not entrypoint** (the existing sidecar's key): one entrypoint
    (`native.training.m49_curriculum_ab`) spans 270 groups whose runtimes run p10 6 min → median 33 →
    p90 182, so a single per-entrypoint p90 is 5.5× the median task and 29× the p10 task — keying on
    entrypoint MANUFACTURES the over-estimation it exists to remove. Group is predictive and cheaply
    so: within-group sd 31 min vs between-group sd 81 min over the 90 groups with n≥5. `config_hash`
    is NOT usable as a key — 1172 distinct hashes across 1182 tasks, none recurring, because every
    sweep cell varies a knob.
    p90, not median, for the same asymmetry the sidecar uses: over-estimation wastes budget,
    under-estimation causes hard-cap eviction and stall-reap.
    ⚠ **Never written back to the task row, and never sent to the worker** (`_build_task_json` reads
    the row). The declared `est_minutes` is part of the queue-time contract — the `--probe` bound and
    the resume contract were both checked against it at `runq add` — so rewriting it would
    retroactively edit a decision the operator already made. This is a scheduling-time correction.
    ⚠ Scoped over `PENDING_STATES` (`queued` + `registry_db.OPEN_STATES`), NOT `OPEN_STATES`, which
    means "occupying a slot" and excludes `queued` — the queued tail of a campaign is exactly the
    population a learned estimate must reach.



23b. **INGEST REPORTS ITS OWN BREAKDOWN (2026-07-31).** `_ingest_and_complete` times its sub-phases
    and counts the transport calls each makes, into `poll_cycle.ingest_detail`. 23 named `ingest` as
    65% of the cycle, which is one level too coarse to act on. The COUNT is logged beside the SECONDS
    because they select different fixes: many cheap calls ⇒ latency/path-bound, parallelise; few
    expensive ones ⇒ the payload is the problem, cap it. A FAILED pull still counts — otherwise a
    fleet of unreachable boxes reads as "barely any rsyncs" while burning 60 s apiece, which is the
    exact misreading that let the 2026-07-31 cycle blow out unnoticed.

23c. **THE PER-TASK PAYLOAD PULLS FAN OUT ONE WORKER PER BOX (2026-07-31).**
    MEASURED via 23b: of a 962 s ingest, `checkpoints` was 559.9 s over 42 calls (13.3 s/call) and
    `tb` 202.7 s over 47 (4.3 s/call) — **79% of ingest, 43% of the whole poll cycle.**
    **The axis is per-BOX, and that is a measurement, not a preference.** Throughput is ~0.3-0.62
    MB/s per box (probed directly, and matching `rsync_push`'s independently measured figure) against
    a 1 Gb/s home uplink — so the ceiling is each box's OWN network path, not ours. Concurrency
    WITHIN a box contends for one saturated link and buys nothing; ACROSS boxes the paths are
    independent and it is ~linear. Hence one worker per box, serial inside it, bounded by
    `ingest_parallel_boxes` (8; set to 1 for a tested serial fallback).
    **Only the per-task half is parallelised.** `tb` writes nothing to the DB and `checkpoints`
    writes one deferrable UPDATE, whereas `worker_state` and `markers` drive state transitions and
    terminal completions — those stay SERIAL, so no completion can race and invariant 9e's
    "an observation gap must not discard an OUTCOME" is untouched.
    **The split is enforced, not asserted.** `_tb_io`/`_ckpt_io`/`_pull_box_payloads` are pure I/O —
    no `self.conn`, no `self.tracker` — because `registry_db.connect` omits `check_same_thread` and
    the single connection therefore cannot be used off-thread at all. A test runs the thread body on
    a non-main thread, where any leaked DB access raises `ProgrammingError` and surfaces as the
    captured `error`. Every DB write and tracker mutation happens in `_apply_box_payloads`, which the
    poll loop runs serially — which is also why the tracker's counters need no lock.
    Endpoints are resolved serially up-front (`endpoint_for` can shell out to `vastai ssh-url` and
    mutates tracker state). One box raising is recorded as `ingest_box_failed` and does not take the
    phase down. `payload_speedup` (summed per-thread work ÷ wall-clock) is logged so a fan-out that
    silently stops working is visible without re-deriving it.
    ⚠ `rsync -z` was measured as a candidate lever and REJECTED: 32.8/19.8 s with it vs 21.3/23.4 s
    without, on identical 9.9 MB pulls. Per-call variance swamps any compression effect, so there is
    no evidence the box's CPU (where the trainers live) is the constraint. Do not remove it on
    intuition — re-measure.

23d. **THE SHIP PASS FANS OUT ONE WORKER PER BOX (2026-07-31).** 23c did the pulls; this is the
    push, and it was the last serial data-movement phase.
    **MEASURED, and the cost is IDLE HARDWARE, not coordinator seconds.** Ship was 37% of a poll
    cycle whose median is **20.8 min** (ingest 55%, `place` 0.9%), and `ship_budget_spent` fired on
    **every** pass — 4-12 tasks shipped, 6-31 deferred. Over 2 days the fleet held a median of
    **52 tasks `running` against 115 `claimed`-but-not-started**: work already assigned to a box,
    waiting on transport. Median `add`->`start` **36 min**, mean 82, p90 **3.6 h**. Two live boxes
    were observed at 0 running while holding 5 claimed tasks. This is the mechanism behind the
    "fleet is under-utilised" reading — the boxes were never the constraint (and note invariant 25:
    the CPU fractions that reading was based on were themselves measured against the wrong
    denominator).
    **The axis is per-BOX for exactly the reason 23c gives** — each box's own uplink is the ceiling,
    so concurrency within a box contends for one saturated link and buys nothing, while across boxes
    it is ~linear. One worker per box, serial inside it, bounded by `ship_parallel_boxes` (8; set to
    1 for the tested serial fallback, which is kept verbatim).
    **Three phases, and the middle one is enforced pure.** `_ship` splits into `_ship_prepare`
    (SERIAL: the prior-ship count, `entrypoints.resolve`, the compile/ship caches, `build_bundle`),
    `_ship_io` (PURE I/O: ssh existence check, apt, the two rsync pushes — no `self.conn`, no
    `self.tracker`), and `_apply_ship_io` (SERIAL: replays each op's reachability into the tracker
    **in order**, so the proxy->direct switch fires exactly where it did inline). Endpoints resolve
    serially up-front, as in 23c.
    **Keeping the BUILD serial is load-bearing twice over.** It is what lets the push half be pure —
    every byte must exist before the thread starts — and it is what keeps the caches safe:
    `_compiled_tree`/`_shipped_tree` publish through a temp path derived from the cache KEY, so two
    threads missing the same key at once would write the same temp file and atomically publish the
    interleaved result. Serial building makes that unreachable. Do not move the build into the
    workers for more concurrency without first making those temp paths per-writer.
    **7b gets STRICTER and 7c gets a superset.** 7b is now structural: a box's ships are one
    worker's serial loop, so a transport failure stops that box by `break` and can no longer burn
    other boxes' budget (which is what `retired_cost` existed to refund). Its old "key on the
    tracker moving, not `ok`, so TASK-level faults don't defer siblings" caveat is now satisfied by
    construction — the faults it meant to exclude (a failed `git archive`, a compile error) resolve
    in `_ship_prepare` and never produce a plan, while the two that can still reach the push (apt,
    rsync) are exactly the two the serial version already fed the tracker as transport. 7c's "at
    least one task always ships" becomes "at least one PER BOX".
    **Tested, including the claim itself.** A `threading.Barrier` test proves two boxes are in
    flight simultaneously (it raises `BrokenBarrierError` under `ship_parallel_boxes = 1`, so it
    discriminates rather than measuring a coincidence), and a companion test asserts a single box's
    own pushes never overlap.
    **The pass reports the parallelism it ACHIEVED** — a `ship_fanout` event carrying
    `{boxes, width, wall_sec, work_sec, speedup, tasks}`, where `speedup` is summed per-thread work
    ÷ wall-clock, exactly as 23c logs `payload_speedup` and for the same reason: a fan-out that
    silently stops fanning (one box monopolising the pass, a width collapsed to 1) still completes
    the pass, just serially, and is otherwise invisible without re-deriving it by hand from event
    timestamps. **~1.0 across several boxes means it is not fanning.**
    ✅ **MEASURED LIVE 2026-07-31 — the fan-out works, at ~100% of what it could achieve.**
    `{boxes:7, width:7, tasks:20, work_sec:338.1, wall_sec:93.4, speedup:3.6}` with
    `per_box_sec {-1:1.6, 40000039:10.1, 40000038:22.0, 40000036:50.4, 40000040:79.9,
    40000037:80.7, 40000041:93.4}`. **`wall_sec == slowest_box_sec == 93.4` exactly** — the
    mathematical signature of an optimal per-box fan-out, since a pass can never cost less than its
    slowest box. Achievable ceiling was `work/slowest = 3.62`; achieved 3.6.
    ⚠ **Two earlier readings looked like answers and were not — do not repeat either.**
    (1) The first cycles after deploy read 5.3 and 6.2 min against a 20.8 min median, but `ship` was
    **0s** in both (queue drained to 39 `running`, zero `claimed`). That measures ship having no
    work; the serial code would have posted the same numbers.
    (2) The first multi-box pass read `speedup 1.0` over 4 boxes, which looks exactly like a
    fan-out that is not fanning. It was **skew**: box `-1` ships a task in **1.6s**, so the 5 tasks
    it held cost ~8s, and one SLOW RENTAL holding 1-2 tasks owned the whole wall. Skew by LINK
    SPEED, not by task count — which is why `tasks_per_box` alone would have misdiagnosed it too.
    **So read `efficiency`, never `speedup` alone.** Per-box parallelism cannot beat its slowest
    box, so `ceiling = work/slowest` is the most that was ever achievable and `efficiency =
    speedup/ceiling` is the fraction reached. Both are in the event, so no reader has to derive it:
      `efficiency ~1.0` -> the fan-out did all it could; any residual limit is ONE SLOW BOX, and
                           the lever is placement (or that box's link), NOT ship.
      `efficiency` low  -> a real 23d defect.
    Ruled out while diagnosing, so nobody re-checks them: `_RSYNC_LOCK` guards only the call
    COUNTER, not the transfer; and no pass involved was a retry storm (every task shipped).
    **Second, independent confirmation:** `ship_budget_spent` has fired **zero** times since deploy,
    against **every pass** before it (which deferred 6-31 tasks each). The 20-task pass above would
    have taken 338s serially — past `ship_budget_sec` (300) — and deferred; it deferred nothing.

23e. **`worker_state` AND `markers` FAN OUT TOO (2026-08-02).** 23c parallelised the per-task payload
    pulls and 23d the ship push; these two per-BOX sub-phases were the serial remainder, and they are
    what still makes the cycle scale linearly with fleet size.
    **MEASURED over 400 live poll cycles:** total **96 s median / 195 s p90 / 415 s max** against a
    `poll_seconds` of **30**. Ingest is **70%** of it, and its cost is **~9-15 SECONDS PER BOX** — a
    straight line in box count (2 boxes → 3.5 s median cycle; 7 → 106 s; 9 → 177 s; 11 → 355 s).
    Within ingest, `worker_state` + `markers` are ~29% (46.4 s + 11.6 s against a 142.2 s payload
    wall on a representative 8-box cycle), spent on three latency-bound round trips per box (two
    rsyncs + one ssh) run strictly one box after another.
    **The 23b comment predicted this fix and named the discriminator** — "many cheap calls ⇒
    parallelise the transport; few expensive ones ⇒ the payload is the problem". 92 rsyncs per cycle
    is the former.
    **Why it is legal:** the ordering constraint that kept it serial is PER BOX, not global —
    `worker_state` must precede the payload pulls because it is what moves `shipped → running` and
    the payload plan selects on `running`, but box A's worker_state has nothing to do with box B's.
    So each is split into a PURE-I/O half (`_pull_worker_state_io`, `_pull_markers_io`) that fans out
    under `ingest_parallel_boxes`, and a MUTATING half (`_apply_worker_state`, `_apply_markers`) that
    runs serially on the main thread, re-walking `insts` in instance order — never futures order, so
    the event log stays reproducible. `_has_open_tasks` hoists the markers DB read into the serial
    planning pass. The single-threaded `_pull_worker_state` / `_pull_markers` entry points remain for
    callers that want both halves on one thread.
    Enforced by the same property 23c relies on: `registry_db.connect` omits `check_same_thread`, so
    any DB access from the threaded half raises outright, and a test asserts it does. A box that
    raises is recorded via `ingest_box_failed` + a tracker failure and skipped — one unreachable box
    must not take every other box's state transitions and completions with it.

25. **THE BOX PROBE MEASURES THE CONTAINER, NOT THE HOST (2026-07-31).** `BOX_PROBE_CMD` /
    `parse_box_probe` gain container-true axes — `cpu_quota_cores`, `cpu_usage_usec`,
    `mem_limit_gb`, `mem_used_gb`, `mem_anon_gb`, plus `gpu_mem_util` / `gpu_name` / `gpu_count` —
    and `_measure_box_resources` turns the cumulative CPU counter into a rate (`cpu_used_cores`)
    against the previous sample. `box_measured`'s detail becomes `<human summary> | <json>`, the
    machine-readable half also carrying `slots_total` / `slots_eff` / `running` / `open` at sample
    time. Observability only: this is what the dashboard's fleet-performance panel reads
    (run-dashboard spec §20).
    **Why it is an invariant and not a nicety: the pre-existing four axes measure the HOST.**
    `nproc`, `/proc/loadavg` and `free -m` inside a Vast container report the whole machine.
    Measured live on rental 40000039: `nproc` 96 and `free` 94 GB, while that container's own cgroup
    allowed **18.43 CPUs and 40.4 GB** — so "load 13.90/96 cores" reads as 14% busy when our real
    occupancy of what we PAY FOR was ~5× higher, and `/proc/loadavg` additionally counts other
    tenants' processes as ours. Any saturation or bottleneck claim built on that denominator is
    confidently wrong, so the panel must never be built on it.
    a. *The frozen half.* `cores`/`load1`/`ram_total_gb`/`ram_avail_gb` keep their exact pre-25
       meaning and units, because `box_headroom` admits against them — re-pointing them at the
       cgroup would silently re-tune live admission, which is a packer change wearing an
       observability hat and does not belong in this invariant. Every new field is independently
       optional and defaults to `None`; a box that answers none of them degrades exactly to the
       pre-25 behavior. (The consequence — that the packer still admits on host denominators, and
       therefore that a box can read ~25% CPU while the packer believes it is full — is a REAL
       finding this panel now exposes, and a separate decision to make with the numbers in hand.)
    b. *Both cgroup layouts, because both are live.* Rental 40000039 is cgroup **v1**, 40000032 is
       **v2**. Every read tries v2, then v1, then omits the line.
    c. *The output is KEYED (`NPROC 20`), never positional, and unknown lines are ignored.* This is
       forced: **`nvidia-smi` writes its failure text to STDOUT.** On the NVML-blocked owned box the
       GPU arm emits four lines of prose, which shifts every positional field after it — a
       positional draft of this probe read that box's CPU-time counter as its anonymous memory and
       reported **520 GB of RSS on a 31 GB machine**. Keys make stray driver output inert.
    d. *A rate needs two samples, and its absence is not zero.* `_cpu_used_cores` returns `None`
       when there is no previous sample (a fresh box, or the first pass after a restart), when
       either counter is missing, or when the counter went BACKWARDS (the container was recreated,
       so the readings are not comparable). Reporting `0.0` there would claim the box was idle, and
       a restart silently reporting the whole fleet as idle is exactly the confident-wrong number
       this invariant removes.
    e. *Multi-GPU keeps the first card.* `vram_total_gb`/`vram_used_gb`/`gpu_util` stay first-GPU
       (never a sum) so the VRAM gate admits exactly as before; `gpu_count` reports the rest.
    f. *`_effective_slots` is now the single composition of the 19h overpack cap and the capacity
       window,* used by both `_instances_view` and the sampled payload — so the packer and the
       panel can never disagree about how big a box currently is.

26. **IN-FLIGHT PER-LANE RESOURCE RECALIBRATION (2026-07-31).** Every SIZING and ADMISSION decision
    uses a MEASURED per-lane footprint (`res_defaults.learn_lane_footprint`, pure) layered over the
    task's declared `resource_hint`, resolved at the two view builders by `_effective_hint` —
    exactly where invariant 24 corrects `est_minutes`, and for the same reason: `slots_for_offer`
    (how big a box we rent), `task_footprint` (18a budget admission) and `_headroom_fits` (23) must
    agree about how big the same task is. Emits a `lane_footprint` event whenever what it learned
    changes. This is the decision 25a left open, taken on the numerator; 25a's frozen denominators
    are a SEPARATE and still-open change (see below).
    **The defect.** A hint is a hand-written guess in the job manifest's `resources` block. Measured
    live: declared `vram_per_lane_gb` 2.0 / `cores_per_lane` 2-4 / `ram_per_lane_gb` 2.0, against a
    measured **0.00 GB VRAM, 1.00-1.02 cores and 0.57-1.60 GB RAM** per lane. Four of five rented
    boxes were pinned at 6 slots by `floor(12 GB / 2.0)` — a reservation against a card at 0% util —
    while `max_slots_cap` (8) never bound and the CPU term never bound. `cpu_cores_effective` in the
    offer is NOT implicated: it matches the container's cgroup quota at 0.96x on all six live boxes,
    so rent-time CPU sizing was already container-true.
    a. *Why it cannot be fixed by editing the manifest.* `job.json` is deliberately UNTRACKED
       (commit 7d73eb77, "per-worktree loose manifest"), so there is no single source to correct:
       25 of 33 live worktrees carry the identical stale `{"vram_gb": 2, "cores": 4}`, every new
       worktree inherits it, and a fix in one propagates to none. The declaration site is
       structurally unmaintainable, which is what forces the number to be measured.
    b. *Fleet-wide is the PRIMARY key; per-(entrypoint, group) is a refinement* — the opposite of
       invariant 24, deliberately. Runtime varies by campaign (one entrypoint spans 270 groups, p10
       6 min -> p90 182); per-lane SIZE does not, because `pin_torch_threads(1)` makes it ~1 core
       for everything. A per-group cores/task slice reads 3-5 and is measured small-n noise, while
       the fleet-wide median is 1.12 over 1186 samples. The 25 payload is also new, so per-key
       samples do not exist yet (0 keys with n>=20 at first light) and a group-keyed learner would
       be blind for days.
    c. *A fleet-wide axis ABSTAINS unless it is homogeneous* (`p90 <= 2.0 x median`, or the whole
       distribution below its floor). A p90 over a MIXTURE describes the heaviest workload, not a
       typical lane, so one heavy campaign would otherwise size every box in the fleet as if every
       lane were heavy — re-introducing the exact over-declaration this invariant removes, with a
       measurement's authority behind it. Abstaining is safe in both directions: the axis is absent
       and the declared value stands. Per-key entries are exempt (one workload by construction).
    d. *Attribution is stamped AT SAMPLE TIME, not reconstructed.* `_box_perf_payload` adds
       `ep`/`grp` when every running task on the box shares one, null when mixed. A mixed sample is
       still a real per-lane average, so it keeps feeding the fleet aggregate; it just teaches no
       key. Reconstructing this afterwards would mean replaying every start/terminal event.
    e. *It is NOT a one-way ratchet.* `_learn_overpack_cap` only ever takes `min()`, so a cap
       learned once never recovers — it pinned the owned laptop at 4 slots from 07-27 until a human
       raised it by hand. This learner moves BOTH ways: down on cores/VRAM (which frees slots) and
       **up** on RAM — measured p90 2.33-2.80 GB/lane against a 2.0 default, the one axis where the
       declaration is too SMALL and where being wrong costs an OOM-kill and a requeue.
    f. *Floors and ceilings, both load-bearing.* `slots_for_offer` divides by `vram_per_lane_gb`
       with no zero-guard and the workload genuinely measures 0.00, so a learned 0.0 would raise
       `ZeroDivisionError` inside placement — hence `FLOOR_VRAM`. (Since m the VRAM axis is left
       out for a task charged no VRAM, so the divisor is never zero; the floor remains as the line
       between "measured using the card" and "not".) At the other end, `slots_for_offer`
       returns 0 for an offer that cannot host one lane and 4e's fit filter then DROPS it, so an
       unclamped outlier could empty the rentable set and stall the queue — hence the ceilings.
    g. *p90 x 1.25, not the median,* for invariant 24's asymmetry: over-estimating costs slots,
       under-estimating costs a thrashing or OOM-killed box and a requeue.
    h. *Never written back to the task row.* Like `_effective_est`, this is a scheduling-time
       correction; the row records the queue-time contract that `runq add` validated `--probe`
       bounds against.
    i. *KNOWN BIAS, stated not corrected.* `cpu_used_cores`/`mem_used_gb` are CONTAINER totals, so
       dividing by `running` charges each lane a share of fixed per-box overhead, inflating the
       per-lane figure at low occupancy. The bias is conservative (over-reserves, never under-) and
       shrinks as boxes pack deeper. A slope fit across occupancies would remove it.
    j. *RESOLVED 2026-08-02 by invariant **28** — `box_headroom` no longer runs on HOST
       denominators.* It was: `cpu_cap` from `m["cores"]` (`nproc`) minus `m["load1"]` (the host run
       queue, counting other tenants as ours), wrong in both directions and the gate that binds NEXT
       once this invariant lifts the slot count. Measured error at the moment of the fix is in 28.
    k. *A DECLARED AXIS BEATS THE FLEET-WIDE AVERAGE (26-Q1, resolved 2026-09-28).* Owner, on a
       campaign stranded off every owned GPU: *"fix it, this should be able to run on any of these
       machines."* `res_defaults.resolve_hint`, per axis: the fleet-wide measurement FILLS an axis
       the task did not declare and never overrides one it did; the task's own (entrypoint, grp)
       measurement — the same workload — may RAISE a declared axis (`max(declared, per-key)`) and
       never shrink it; an undeclared axis takes per-key, else fleet-wide. **The incident:** one
       heavy campaign (`pclm_c1`, whole-step CUDA graphs) was most of the fleet's GPU samples on
       2026-09-27, the homogeneity gate (c) therefore passed, and the fleet `vram_per_lane_gb` hit
       the 8.0 clamp — so every NEW pclm group, declaring 4 GB, was admitted as 8 and held
       `box_target: waiting for box` on three idle owned GPUs (5.9 / 6.4 / 3.4 GB of day-window
       headroom; the 8 GB desktop could never admit it at any hour). The same override in the
       other direction was `9f38965d` (14 GB declared, re-priced to 2.3, packed onto a 12 GB card).
       **ONE hint serves sizing and admission**, deliberately NOT the split 26-Q1 recommended (rent
       sizing on the learned value, admission declared-first): invariant 27 requires the rent
       filter and the admission gate to agree, and a split re-creates 27's incident — a 14 GB task
       sized at the learned 2.3 buys a 12 GB box its own admission then refuses. What this gives
       up is 26's un-pinning of an OVER-declared rental (declared 2.0 GB VRAM against 0.00 used →
       6 slots on a 12 GB card); 26a's premise for measuring instead of fixing the declaration is
       gone (job-artifact-contract v2 moved hints from the untracked `job.json` into tracked
       configs), so that is now fixed at its source. Pinned by `tests/test_res_defaults.py`
       (`test_fleet_average_never_overrides_a_declared_axis_for_admission`,
       `test_own_measurement_can_raise_but_never_shrink_a_declaration`).
    l. *A BROADER AVERAGE FILLS ONLY WHAT IT CAN DESCRIBE (2026-09-29).* Two changes to how
       `resolve_hint` fills an UNDECLARED axis; declared axes are untouched (k stands).
       (1) **An entrypoint level.** `learn_lane_footprint` also learns `(entrypoint, None)` over all
       of an entrypoint's groups (`LIVE_MIN_SAMPLE`, and the homogeneity gate (c), because it spans
       configurations). An undeclared axis takes per-key, else per-entrypoint, else fleet-wide. A
       campaign that names every sweep a new group (each below `LIVE_MIN_SAMPLE`) then inherits its
       own program's footprint instead of the fleet's heaviest workload.
       (2) **VRAM from a broader average fills only a task that declares `requires_gpu`.** VRAM is
       sampled only on GPU boxes, so the per-entrypoint and fleet VRAM describe GPU workloads (or a
       box's display baseline divided by its running count), never a CPU-only lane. A task without
       `requires_gpu` keeps its OWN (entrypoint, grp) VRAM measurement when it has one, else the
       settings lane; any real GPU use it makes is still gated by measured headroom (23).
       **The incident (2026-09-28):** pclm (GPU, ~310 of the day's ~320 VRAM samples and ~86% of
       RAM samples) passed the homogeneity gate as a MAJORITY, so the fleet footprint was
       `cores 2.21 / ram 9.7 GB / vram 6.5 GB` per lane. Every new `sparse_pc_ladder` group (CPU-only,
       measured ~1 core / 0.4–1.6 GB RAM / no GPU; 1–7 samples per group) was admitted at that size.
       The owned laptop (day VRAM budget 10.2 GB) therefore admitted ONE co-located sibling at a
       time (6.5 + 6.5 > 10.2), holding four "waiting for room" for 15+ min at load 0.03/32; the
       gpudesktop (pclm 5.25 GB + 6.5 > its 8 GB day budget; 2.8 GB measured VRAM headroom)
       admitted none, holding 5 box-targeted tasks for 17 min with 23 slots free. Pinned by
       `tests/test_res_defaults.py` (`test_entrypoint_level_fills_before_fleet`,
       `test_broad_vram_never_fills_a_cpu_task`, `test_laptop_admits_more_than_one_ladder_task`,
       `test_gpudesktop_admits_ladder_tasks_beside_pclm`).
       *Superseded in part by m:* a task without `requires_gpu` and with no VRAM of its own is now
       charged **0**, not the settings lane, and is not VRAM-gated at all.
    m. *⛔ VRAM IS CHARGED ONLY TO A TASK THAT USES THE GPU (2026-10-03, owner: "do it").* The
       card's state must not decide where a CPU-only task runs.
       **The incident (2026-10-02 12:50 PM → 10-03 8:25 PM local time).** ~9.4 GB of NON-fleet VRAM sat
       on `laptop-gpu` (12 GB card). Its day window allows 0.85 × 12 = 10.2 GB, so measured VRAM
       headroom was 10.2 − 9.4 − 1.0 reserve = **−0.2 GB**, and `_headroom_fits` refused EVERY
       task — the queue was CPU-only tasks that never touch the card — for every daytime hour
       while a 32-core box sat at load ~0. Two independent causes, both fixed here (m, n).
       *The rule.* `lane_vram_gb(hint, settings)` is the ONE definition of a task's per-lane VRAM:
         - `requires_gpu` → its `vram_per_lane_gb` when positive, else the settings lane;
         - no `requires_gpu` → its `vram_per_lane_gb` when positive, else **0.0**.
       The hint is the EFFECTIVE one (`_effective_hint`), so "its `vram_per_lane_gb`" is what the
       task declared, raised by its own (entrypoint, grp) measurement (k). For a task that does not
       claim the GPU, `resolve_hint` writes that measurement only when it is ABOVE
       `GPU_USE_MIN_VRAM` (0.75 GB) — below it its own group has been measured using no VRAM worth
       the name, and the axis stays absent (or at the 0 it declared). So a task is a GPU user when
       it SAYS so (`requires_gpu`, or a positive VRAM declaration) or when its own group has been
       MEASURED using the card.
       *Why 0.75 and not the 0.25 floor — measured.* A card's own baseline moves while lanes run,
       and with one lane running that movement IS the per-lane figure. On the live registry the
       seven CPU-only groups read 0.25–0.38 GB after n's netting (the 0.38: six samples of a
       desktop whose display grew 0.3 GB), and the smallest real GPU group read 1.12. At the floor
       that 0.38 group would have been gated as a GPU user.
       *What a VRAM footprint of 0 means, everywhere:*
         - `_headroom_fits` (23) and `_budget_fits` (18a) skip their VRAM arm; cores and RAM still
           gate. A negative VRAM headroom therefore refuses GPU users and nobody else.
         - the task adds 0 to a box's occupant VRAM, so it consumes none of a window's VRAM budget
           and none of the pending VRAM a measurement cannot see yet.
         - `_offer_lane_counts` (27) leaves the VRAM axis out, so rent sizing and admission still
           agree. This also removes a crash: a declared `vram_gb: 0` — which
           `configs/probes/TEMPLATE.probe.json` recommends — divided by zero in `slots_for_offer`
           the first time such a task reached the rent path.
         - `_preempt_shortfall` needs no VRAM freed.
         - the task ships with `env.RUNQ_NO_GPU=1`, and the box skips its GPU launch rules for it (8c).
       GPU users are gated exactly as before, against the whole card (a co-tenant's allocation
       really does deny them that memory — 28).
       *Known limit, stated.* A task that uses the GPU WITHOUT declaring it is ungated on VRAM
       until its own group has `LIVE_MIN_SAMPLE` samples (~1 h). Replayed over 2026-09-28..10-03:
       19 groups cleared the sample bar, all 13 GPU-using ones declared `requires_gpu`, and all 6
       undeclared ones measured at the floor — so today this protects against nothing real, and
       the remedy if it ever does is to declare the GPU in the config (k: declarations are tracked).
       Pinned by `tests/test_vram_gates_gpu_users.py` (`TestCardStateDoesNotGateCpuOnlyTasks`,
       `TestThroughTheRealDispatcher`) and `tests/test_res_defaults.py`
       (`TestVramIsChargedOnlyToGpuUsers`).
    n. *THE LEARNER CHARGES A LANE ONLY FOR VRAM ABOVE ITS BOX'S IDLE READING (2026-10-03).*
       `vram_used_gb` is the WHOLE CARD — the display, the owner's own work, a co-tenant. Dividing
       it by `running` charged every lane a share of memory the fleet never allocated: in the
       incident above one CPU-only group (`tpc_event_cap_n3_cw`) learned **5.94 GB per lane**, so
       even with the card freed it could not board the 8 GB desktop, and five more CPU-only groups
       learned 0.38–1.50 GB from nothing but each box's display baseline.
       *The rule* (`res_defaults.net_idle_vram`, pure). A box's IDLE reading is a sample taken with
       no open fleet task on it (`open == 0`; `running == 0` on a payload that predates `open`) —
       `open`, not `running`, because a `shipped` or `preempting` task can already or still hold
       memory. Each sample's fleet VRAM is `max(0, whole card − that box's most recent idle
       reading BEFORE the sample)`: whatever was on the card before the lanes started cannot be
       theirs. **A sample with no earlier idle reading in the lookback teaches nothing about
       VRAM** (cores and RAM still learn) — an unmeasured baseline is not a baseline of zero.
       *Replayed on 2026-09-28..10-03* (19 groups cleared `LIVE_MIN_SAMPLE`): all 6 undeclared
       groups → the 0.25 floor (5.94, 1.50, 0.50, 0.50, 0.38, 0.38 before); GPU groups land near
       what they declared — `pclm_g1e_pc2` 8.00 → 3.37 against a declared 3.0, `pclm_g1e_llm`
       6.50 → 4.62 against 4.0, `pclm_g1e_pc` 6.00 → 3.87 against 3.0; two GPU groups had no earlier
       idle reading and keep their declaration.
       *BEFORE, not after and not nearest — measured.* Netting against the larger of the readings
       on either side erased two real GPU groups (`pclm_g1e_llm2` 5.19 → 0.25, `pclm_g1e_llm`
       4.62 → 0.25): on `laptop-gpu` the card was three times still at a finished run's level
       with no open fleet task (2026-09-29 11:15 AM and 2:37 PM, and 09-30 7:32 AM local time — the last
       for 7 h 20 min at 11.0 GB). ⚠ Whether that was a leftover fleet process or the owner's own
       work is NOT established (open, L2) — the registry cannot tell them apart.
       *Known limits, stated.* (1) Memory a non-fleet process takes WHILE lanes are running is
       charged to those lanes for the rest of that stretch; the samples age out with the 24 h
       lookback. Not observed in the replay — the 10-02 memory appeared on an idle box. (2) An idle
       reading that includes a leftover fleet process UNDER-charges the next stretch; the
       declaration still stands as the lower bound (k), and headroom (23) still measures the whole
       card for every GPU user.
       Pinned by `tests/test_res_defaults.py` (`TestLearnerNetsTheIdleReading`).

27. **THE RENT FILTER ADMITS ON THE SAME AXES AS THE ADMISSION GATE (2026-07-31).**
    `slots_for_offer` / `lane_capacity` size an offer on VRAM, cores **and RAM**
    (`_offer_lane_counts`), because `_headroom_fits` (23) gates admission on RAM. Without the third
    axis the two filters disagree and **the fleet buys boxes it then refuses**.
    **The incident, end to end.** Instance 40000042 was rented for an 8-slot task, came up with
    7.4 GB RAM, was refused by `_headroom_fits` (needed 18.7 GB against 3.6 GB of headroom), and was
    torn down `idle_over_warm_cap` **5 s after its first measurement** — $0.019 for zero tasks, with
    the dispatcher renting a replacement 2 s earlier in the same poll. Not a corner case: **RAM is
    the binding axis on 31 of 59 offers** passing our quality gates (cores on the other 28; VRAM
    never binds). This is a named instance of the standing "33.2% of every rented dollar buys zero
    tasks" finding.
    a. *`cpu_ram` is ALREADY THE SLICE'S RAM, and this is what the axis turns on.* It scales with
       `cpu_cores_effective`, NOT `cpu_cores`: across 400 live offers the median is **2.61 GB per
       EFFECTIVE core** (a sane machine) versus **0.38 GB per HOST core** — a 56-core host with
       10.5 GB, which is absurd. **It must not be re-scaled by `cpu_cores_effective / cpu_cores`.**
       That error was made and caught here; it under-predicts ~3x, and since `slots_for_offer`
       returning 0 makes 4e DROP the offer, it would have starved the queue rather than merely
       under-packed. The earlier "the model is 2.2x off" reading came from that same wrong scaling
       plus comparing our rented boxes against the whole market — a selection artefact, not a model.
    b. *Derated, because the advertised figure is OPTIMISTIC.* Measured `mem_limit_gb` on the eight
       boxes we hold is **2.18 GB per effective core** against the market's advertised **2.61** —
       offers over-promise ~20%. `offer_ram_derate` (0.8) turns that into an under-promise, the safe
       direction for a filter that decides what to BUY. n is small (8 boxes, 1.25–4.27 range), so it
       is a settings knob rather than a constant, and `rent_created` now records the mapped
       `ram_gb` beside the box's later-measured `mem_limit_gb` — the pair accumulates, so the derate
       can be re-fit from the fleet's own history instead of from one snapshot.
    c. *RAM ABSTAINS when the offer does not carry it* (`ram_gb` falsy), exactly as the VRAM gate
       abstains on an unmeasured GPU. A missing or renamed field must never silently deny every
       offer — the same fail-open rule as `eligible_offers`' reliability gate.
    d. *`lane_capacity` counts the same axes,* so 4e's value-density ranking cannot prefer an offer
       the fit filter is about to drop. A box whose RAM caps it at 2 lanes is not a cheap 18-lane box.
    e. *⛔ `max_slots_cap` 8 -> 16 WAS TRIED AND REVERTED (2026-08-01).* It caused five over-pack
       unschedules in 30 minutes and permanently ratcheted machines down (m10004 5 -> 3 -> 1; the
       owned laptop from a manually-validated 6 to 1). **The throughput result stands; what it did
       not establish is that the fleet can DELIVER that depth.** The probe was ONE task self-forking
       K processes, so it never crossed the launch gate, and two independent ceilings live
       downstream in `sweep_supervisor.AUTO_DEFAULTS`: `max_slots: 8` (`should_launch` refuses past
       8 live lanes regardless of the `--max-slots` the dispatcher passed the worker) and
       `settle_minutes: 3.0` (one lane per 3 min ⇒ 16 lanes need 48 minutes). Raising the
       coordinator's ceiling alone was the error. Two guards now encode this
       (`TestGraceExceedsFillTime`): `max_slots_cap <= AUTO_DEFAULTS["max_slots"]`, and
       `ship_launch_grace_min > max_slots_cap x settle_minutes`. **MAP THE DELIVERY PATH, not just
       the hardware, before raising a concurrency ceiling.**
    e2. *`ship_launch_grace_min` 20 -> 35 — a PRE-EXISTING defect the above exposed.* The grace must
       exceed the time a box needs to fill its own slots, and at 20 it did not: 8 lanes x 3 min =
       **24 minutes**. `_reap_overpacked_boxes` therefore read "running < advertised" on boxes that
       were merely still filling and ratcheted them down — which is why `overpack_cap` entries were
       already sitting at 1, 4 and 5 before any of this. The ratchet is one-way
       (`_learn_overpack_cap` takes min()) and keyed per `machine_id`, so it survives teardown and
       poisons every future rental of that host: a FALSE POSITIVE is permanent while a false
       negative only delays detection by 15 min. Round toward the cheap error. **The recovery path
       is now SPECCED as invariant 19i** (draft, 2026-08-02) after a THIRD incident — both owned
       boxes pinned at 1 slot, fleet effective capacity 34 of 90, and 27 further ratchet events
       *after* this fix landed. Note what that count establishes: the two guards below constrain a
       NECESSARY but **not sufficient** condition. `TestGraceExceedsFillTime` models fill time as
       `max_slots_cap × settle_minutes`, but ships are ALSO paced by the poll cycle, measured that
       night at **138 s median / 355 s max** against `poll_seconds = 30`. On a healthy 8-slot box
       the 8th lane shipped at +15.0 min and started at **+27.1 min** — one lane consuming a third
       of the 35-min grace. 19i's raise-on-evidence is what actually closes this; the earlier
       "expiry or raise-on-sustained-success" suggestion is **refuted** by 19i.a (a capped box can
       never generate the evidence that sustained success would need).
    f. *The evidence that deeper packing PAYS, for whoever fixes the delivery path.* The `hw/deepk16` probe's
       pre-registered criterion fired (effective lanes 6.47 @ K=8 -> 10.18 @ K=16, +3.71, and
       $0.0081 -> $0.0051 per effective-lane-hour). 16 lanes x 2.34 GB is 37.4 GB, so raising the cap
       WITHOUT this invariant would have amplified exactly the waste above. It binds rarely by design
       (3% of the current offer pool reaches 16 lanes, median 5) — it exists to stop truncating the
       occasional genuinely large box. Efficiency still falls with depth (100 -> 80.9 -> 70.1 ->
       63.6%) and per-lane time IS task wall-clock (**1.57x** dilation at K=16); invariant 24's
       learned `est_minutes` re-converges at the new depth, but `stalled` / hard-cap evictions are
       the things to watch. K=8 efficiency is BOX-DEPENDENT — 80.9% on this Xeon against 65.8% on a
       Ryzen 2700X the same day — so no single efficiency number is fleet-wide.

28. **THE HEADROOM GATE MEASURES OUR CONTAINER, NOT THE HOST (2026-08-02).** `box_headroom` (23)
    sizes free capacity from the CONTAINER's cgroup quota and the CONTAINER's own usage whenever
    both are measured, falling back to the host pair only when they are not. Resolves 26.j.

    **The defect, measured across all 10 live boxes at the moment of the fix.** The gate paired
    `m["cores"]` (`nproc`, the HOST's core count) with `m["load1"]` (the HOST run queue, which
    counts other tenants' work as ours). On shared rented hosts both terms are wrong, and both in
    the **permissive** direction — the gate over-reports free capacity and over-admits:

    | instance | host pair (what the gate used) | container truth | error |
    |---|---|---|---|
    | 40000047 | 192 − 32.52 = **159.5 cores** | 23.04 − 7.09 = **15.9** | **10.0x over** |
    | 40000049 | 192 − 32.45 = **159.6** | 23.04 − 0.00 = **23.0** | **6.9x over** |
    | 40000050 | 56 − 0.13 = **55.9** | 26.88 − 0.00 = **26.9** | **2.1x over** |
    | 40000045 | 28 − 5.29 = **22.7** | 26.88 − 7.22 = **19.7** | 1.15x over |
    | 40000048 | 24 − 8.13 = **15.9** | 23.04 − 8.05 = **15.0** | 1.06x over |

    On 40000049 our container burned **0.00 cores** while `load1` read **32.45** — that entire
    subtrahend was other tenants. RAM shows the same sign: host `ram_avail_gb` 418.3 GB on 40000047
    against a 171.0 GB container limit (**2.4x over**), 122.1 vs 85.3 on 40000050.

    **Fleet-wide over 24 h (`headroom_accuracy.py --hours 24 --all-boxes`, n = 1337 cgroup-measured
    samples) — the error is COMMON, not a tail case:**

        cores  median 2.26x   max 16.26x   over-admitting on 908/1337 (68%)
        ram    median 1.43x   max 2.97x    over-admitting on 820/1337 (61%)
        MIXED basis would go NEGATIVE on 120/1337 (min -35.89)

    ⚠ A single-moment snapshot of the 6 live boxes read median **1.05x** and looked like a pure tail
    case. It was not representative — it happened to catch mostly near-dedicated boxes. **Read this
    over a window, never off one poll**; the per-machine median ranges from 1.02x (m10007) to
    **11.56x** (m10008), so the fleet-wide figure is a mixture and the box you are looking at now
    tells you little about the one you rent next.

    a. **PAIR OR FALL BACK — never MIX denominators.** The one genuinely dangerous implementation is
       to take the container's quota and subtract the host's load: on 40000049 that is
       `23.04 − 32.45 = −9.4` cores, refusing every task on an idle box forever. So the axis switches
       as a PAIR: use `(cpu_quota_cores, cpu_used_cores)` only when BOTH are present, else
       `(cores, load1)` — never one from each. This is what 26.j meant by "the
       `cpu_used_cores`-is-None-on-first-sample fallback handled explicitly": `cpu_used_cores` is a
       RATE derived from two consecutive probes, so it is None on a box's first measurement and
       after any dispatcher restart, and the host pair legitimately stands in until the second probe.
    b. **Owned boxes keep the host pair, correctly.** Both owned boxes report `cpu_quota_cores =
       None` (no cgroup limit — they are bare machines, not containers), so they take the fallback
       and nothing changes for them. This is not a degradation: on an unconstrained machine the host
       numbers ARE the container's numbers, and the capacity window (`resource_cap`) remains the
       operator's policy bound on top. The fallback is therefore the common path for owned hardware
       and the exception for rentals — the opposite of what a "prefer container" reading suggests.
    c. **RAM uses ANON, not `memory.current`** — the same axis-switch, with one correction. Measured
       at the fix: on 40000045, `memory.current` = 19.2 GB of which **10.7 GB is reclaimable page
       cache** (anon 8.5). Treating `current` as "used" would under-report free RAM by more than
       half on that box, which is not conservative-correct but simply wrong — the kernel evicts
       cache under pressure rather than OOM-killing. So free RAM is `mem_limit_gb − mem_anon_gb`
       when both are present, `mem_limit_gb − mem_used_gb` when anon is missing (conservative), and
       host `ram_avail_gb` otherwise. `headroom_ram_reserve_gb` (2.0) remains the slack term.
       ⚠ `mem_limit_gb` is None when the cgroup is unlimited (both owned boxes) — that is a
       fallback trigger, NOT a limit of zero.
    d. **VRAM is deliberately NOT switched.** A GPU is a device, not a cgroup-namespaced resource:
       `vram_used_gb` from `nvidia-smi` is the whole card's usage including other tenants, and that
       is the correct quantity to gate on, because their allocations really do deny us memory. The
       asymmetry with CPU is the point — CPU time is multiplexed and a busy neighbour merely slows
       us, while VRAM is exclusive and a busy neighbour hard-fails our allocation.
    e. **Direction of the change is toward ADMITTING FEWER tasks on shared hosts**, which is the
       expected sign given (d)-class errors were all permissive. This is the gate that binds next
       once slots lift (26.j), so it must be measured after landing rather than assumed — see
       `scripts/diagnostics/headroom_accuracy.py`, which reports per-box host-vs-container headroom
       and the fleet-wide error distribution from the registry's own `box_measured` history.
    f. **Is this upstream of the 19h ratchet? TESTED — PARTIAL, and the hypothesis does NOT carry.**
       The proposal was: a gate over-reporting free cores over-admits, tasks wedge in `shipped`
       behind the box-side launch gate, and `_reap_overpacked_boxes` reads that as "cannot sustain
       its advertised slots". Per-machine median headroom error against the lowest cap each machine
       was ever ratcheted to:

       | machine | median err | lowest cap ever | reading |
       |---|---|---|---|
       | m10008 | **11.56x** | 3 | consistent |
       | m100001 | **8.73x** | 1 | consistent |
       | m10006 | 5.70x | 2 | consistent |
       | m100002 | 2.16x | 1 | consistent |
       | **m10004** | **1.05x** | **1** | ⛔ **counter-example** |
       | **m10003** | **1.05x** | **1** | ⛔ **counter-example** |
       | m10005 | 1.04x | 5 | counter-example |

       High error ⇒ usually ratcheted, but **ratcheted does NOT ⇒ high error**: m10004 and m10003
       were both driven to a cap of 1 while their headroom was accurate to within 5%. So
       over-admission is real, widespread and worth fixing on its own merits, but it is **not a
       sufficient explanation for the ratchet** — at least one other cause is live, and the
       fill-time/ship-latency account in 27.e2 (the 8th lane taking 27.1 min on a healthy box) is
       independent of headroom accuracy and unaffected by this fix. **19i is therefore still
       required**, and it was deliberately specced independently of this invariant.
    g. **Clearing a ratcheted cap lets the detector re-learn a HONESTER one.** After the 2026-08-02
       manual clears, m100001 re-learned **5** (was pinned at 1) and m100006 re-learned **7** (was
       4) within hours. That is evidence the cleared values were artifacts rather than physics, and
       it is the empirical case for 19i's probe: the detector converges upward when it is allowed to
       observe above the cap, and never when it is not.

28b. **A RESTART MUST NOT SILENTLY REVERT THE FLEET TO THE HOST BASIS** (`_seed_box_res`,
    2026-08-02). Found by MEASURING 28 in production immediately after deploying it, which is the
    only reason it was caught: `cpu_used_cores` is a rate differenced against the prior sample and
    `_box_res` is in-memory (invariant 1), so a restart leaves every box without a predecessor and
    `_cpu_pair` takes the host fallback. **Measured minutes after the 28 deploy: all 6
    cgroup-constrained boxes read `cpu_used_cores=None` — the CPU half of 28 was inert FLEET-WIDE**,
    and would have stayed so for a full `resource_measure_every_min` on every box at once. Restarts
    are not rare: every coordinator merge is one, and there were three that day.

    a. **Seed from PERSISTENT state.** `_box_perf_payload` now also carries the raw
       `cpu_usage_usec` counter (unrounded — it is a microsecond counter and rounding corrupts the
       difference), so `__post_init__` restores each live box's predecessor from the newest
       `box_measured` event. Same rule `_learned_footprints` already states: derive from persistent
       state, never an in-memory accumulator.
    b. **BOUNDED BY AGE, and the bound is the safety property.** A rate differenced across a long
       outage is an AVERAGE over that outage, so a box that has since become busy reads as idle —
       understating usage OVERSTATES headroom, which is the over-admit direction 28 exists to close.
       Beyond `2 x resource_measure_every_min` the seed is discarded and the host fallback stands
       for one interval, exactly as before. `_cpu_used_cores` independently refuses a backwards
       counter (the container was recreated) and a sub-second gap.
    c. **Non-fatal by construction.** It runs in `__post_init__`, so any failure must leave the
       daemon booting — a coordinator that will not start is far worse than one interval of
       host-basis admission.
    d. *RAM was never affected.* `mem_limit_gb`/`mem_anon_gb` are absolute readings, not rates, so
       the RAM half of 28 was correct from the first post-restart sample. Verified in the same live
       check: the 192-core box read `ram=167.8` (container, limit 171.0) while its CPU axis was
       still reading the host's 162.82 free cores.

29. **THE BOX SPOOL IS GARBAGE-COLLECTED — THE HAPPY PATH LEAKED IT (2026-08-12).** A task that
    completes NORMALLY left its entire `~/spool/active/<id>` on the box forever. `_complete_done`
    pulls `out/` home and transitions to `done`; `_complete_failed` and `_complete_cancelled` pull
    `run.log`; **none of the three ever deleted the directory.** Every `rm -rf ~/spool/active/<id>`
    in the dispatcher sits on an EXCEPTIONAL path — `_reap_undeliverable_claims` (10d),
    `_reap_unclaimed_ships` (10b), `_unship_orphaned_payload` (18e), `_drain_undelivered`
    (box-pause 12b). So the leak scaled with SUCCESS, not failure, which is why it went unnoticed:
    the healthier the fleet, the faster it filled.

    Measured 2026-08-12, after the tower hit 100% disk and stranded a campaign overnight:
    **laptop 248G in 1375 dirs, desktop 47G in 169, tower 69G in 291** — ~364G of pure
    garbage fleet-wide. Composition per dir: `repo/` 74M + `bundle.tar` 33M + `out/` 5.3M, i.e.
    **~95% is redundant** — the tarball is already extracted into the `repo/` beside it, `repo/`
    itself is reconstructible from the shared `blobs/` tree (inv. 11), and `out/` is the only part
    with post-hoc value and is precisely the part already pulled home. Verified before any deletion:
    of 40 sampled dirs, 40/40 were terminal in the registry and 39/40 had their results in
    `experiments/<grp>/<name>/` (the exception was a `cancelled` task, which has nothing to keep).

    a. **The dispatcher deletes at terminal completion (the source fix).** `_complete_done`,
       `_complete_failed` and `_complete_cancelled` `rm -rf ~/spool/active/<id>` **after** their
       pull, and only on the evidence they already use to decide the terminal state — for `done`
       that is `artifact.exists()`, the completion artifact actually being home, NOT the transport's
       exit code (the distinction invariants 9e/9g were built on). Deleting before the pull, or on a
       transport that merely returned 0, would destroy the only copy of a finished run. Best-effort:
       a failed `rm` never blocks or reverses the terminal CAS — the sweep in (b) will get it.
    b. **The worker sweeps independently, and THAT is what makes disk-full structurally
       impossible.** (a) alone is not enough, because it only fires when the coordinator reaches the
       box: it cannot fire for a task requeued off an unreachable box, for anything completed while
       the daemon was down, or for the historical backlog. So `spool_worker` gets `_prune_finished`,
       mirroring the `_prune_blobs` that already exists on the same `REAP_EVERY_SECONDS` throttle:
       delete `active/<id>` whose **terminal marker** (`DONE`/`PREEMPTED`/`CANCELLED`/`FAILED_*`) is
       older than `FINISHED_MAX_AGE_SECONDS` (12h). The marker — never age alone — is the gate: a
       running task has none, so a live trainer is protected by construction, and the retention
       window is a forensics grace period for reading a fresh failure's `run.log` over ssh.
       This is the layer that would have prevented the 2026-08-12 outage on its own.
    c. **`bundle.tar` is unlinked once it is successfully unpacked.** It is a byte-for-byte
       duplicate of the `repo/` extracted beside it, and `_launch` re-unpacks only when `repo/` is
       ABSENT, so nothing reads it again. Only on a clean unpack — a rejected bundle keeps its
       evidence.
    d. **The GC never races a live task.** (a) runs only after the state machine reached a terminal
       state; (b) requires a terminal marker. Deleting a terminal dir is also consistent with
       `reap_orphans`, which treats "owning dir gone" as a *reason to kill* a surviving process
       (18e) — so a GC'd dir strengthens orphan reaping rather than defeating it.

30. **A GPU-REGISTERED BOX THAT LOSES ITS GPU PUSHES AN ALERT (2026-09-23, owner).** Nothing else
    notices a GPU-less box: `box_headroom` abstains per-axis on an unmeasured GPU (by design — a
    blocked GPU must not cost CPU admission), so it keeps admitting work and runs it on the CPU. The
    desktop lost its GPU 7 times 2026-08-12..09-23 (root cause: a systemd `daemon-reload` strips a
    `--gpus all` container's device cgroup — `docs/operations.md`), each found only when a human
    asked. `fleet_util`'s `GPU-LOST` detector reports the same condition, but only to whoever runs it.
    a. **Detect in `_measure_box_resources`, after a SUCCESSFUL probe only** (`_check_gpu_alert`), so
       an unreachable box can never read as GPU-less. For a box with `instances.gpu_name` set, "lost"
       = its newest `GPU_LOST_MIN_SAMPLES` (2) `box_measured` tails all carry `gpu_name: null`; a tail
       with no `gpu_name` KEY (pre-25) is unknown and skipped. The threshold is measured: 30,585
       probes on GPU-registered boxes held zero one-sample blips. `fleet_util` imports the constant.
    b. **Edge-triggered, state in the event log.** `gpu_alert_transition` (pure) returns `lost` once
       per drop and `restored` once per recovery, reading the box's last `gpu_lost`/`gpu_restored`
       event — so a coordinator restart neither re-sends an open alert nor forgets one.
    c. **Push via ntfy** (`ntfy_post`, stdlib `urllib`, never raises, 10 s timeout) to the topic in
       env `DISPATCHER_NTFY_TOPIC` (server `DISPATCHER_NTFY_SERVER`, default `https://ntfy.sh`). The
       topic's default lives in `coordinator/docker-compose.yml` — the owner judged it not secret
       (2026-09-23: worst case is someone reading or spamming fleet alerts) — and
       `/srv/coord/coordinator.env` can override it. It is deliberately NOT a code default in
       `dispatcher.py`: every test and every non-coordinator process would then push to the real
       phone. Unset = the transition is still logged and `[ALERT]`ed, not pushed.
    d. **A failed push records no transition**, only a `notify` event with the error, so the next
       probe (~5 min) retries it; an alert must not be lost to one transient network error. Every
       attempt logs a `notify` event (`kind`, `ok`, `error`).

## Fixtures

Golden decision traces (driven through the pure functions / `--once --dry-run`); shared settings
for all fixtures: `max_hourly_usd=1.00, balance_floor_usd=3.00, max_instance_dph=0.40,
idle_timeout_min=10, rent_patience_min=15, backlog_min_tasks=3, backlog_min_task_minutes=60,
preempt_priority_margin=30, vram_per_lane_gb=0.6, cores_per_lane=1, max_slots_cap=8,
est_safety=1.25, pull_margin_min=10`. The gpu_deny / cores-floor gates are NOT consulted by
`place()` (they run earlier, in `eligible_offers`), so `place()` fixtures never need to override
them — their offers are ranked purely by cores-per-$. The gates themselves are unit-tested in
`TestEligibleOffers`/`TestOfferCounterfactual`, which set the keys explicitly.

- **Invariant 8a (the box holds no lane count)** — `tests/test_spool_worker.py`
  (`TestTheBoxHoldsNoLaneCount`) and `tests/test_dispatcher.py`:
  - a worker with 30 live trainers and a measured gate that allows launches the next pending task
    — past any number it was ever started with, and past `AUTO_DEFAULTS["max_slots"]`;
  - a measured gate that refuses (`cpu_load`) still holds the task and logs one `launch_gate` line,
    which never names a slot count;
  - `Worker` takes no lane count, and `main` given `--spool S --max-slots 4` (the argv a pre-8a
    worker re-execs with) parses and builds one;
  - the command `_bring_up_worker` starts a worker with carries no `--max-slots`.

- **Invariant 8b (launch pacing is the coordinator's)** — `tests/test_launch_gate.py`:
  - the worker's built-in pacing equals the coordinator's default, which equals the constants the
    box used to carry; `should_launch` given a config without the three new keys decides as before;
  - a pushed `settle_minutes` / `cpu_reserve_cores` changes the real `should_launch` decision on
    the box with no code delivered, and the `launch_gate` hold line quotes the value in force;
  - the copy is re-read only when it changes, survives a worker restart, and is acknowledged
    (`launch_gate_config`) by an idle worker; a worker never sent one logs nothing;
  - per-key refusal: a non-number, a bool, NaN/inf or an out-of-range value falls back to that
    key's default alone and is reported; an unknown key is ignored; a non-object leaves defaults;
  - the measure probe carries the canonical payload in its own ssh call (one call per box), for
    rentals too; `LG updated` logs `launch_gate_pushed`, `LG same` and silence do not; an edit to
    the setting reaches the next probe without a restart; a hand-edited setting is sanitised.

- **Invariant 26m / 26n / 8c (VRAM gates only GPU users)** — `tests/test_vram_gates_gpu_users.py`
  (`TestCardStateDoesNotGateCpuOnlyTasks`, `TestThroughTheRealDispatcher`,
  `TestNoGpuTasksSkipTheGpuRules`) and `tests/test_res_defaults.py`
  (`TestVramIsChargedOnlyToGpuUsers`, `TestLearnerNetsTheIdleReading`). Each fix, reverted alone,
  turns at least one of these red (checked by mutation, 11 of 11):
  - THE INCIDENT: a 12 GB card with a 10.2 GB day allowance and 9.4 GB in use (headroom −0.2 GB) →
    a task with no hint, and one declaring `vram_per_lane_gb: 0`, are admitted; the identical box
    refuses a `requires_gpu` task and one declaring 2 GB (the control that the gate still works).
  - `lane_vram_gb`: no hint → 0; `requires_gpu` with no VRAM → the settings lane; `requires_gpu`
    declaring 0 → the settings lane; a positive declaration without `requires_gpu` → itself.
  - a window VRAM budget already spent by GPU occupants still admits a CPU-only task, and CPU-only
    occupants spend none of it; a CPU-only task's pending VRAM is 0.
  - `slots_for_offer` / `lane_capacity` with a declared `vram_per_lane_gb: 0` do not raise and size
    on cores and RAM alone; with a GPU hint they are unchanged.
  - `resolve_hint`: an own-group VRAM at the floor, or at 0.38 (display movement), leaves a
    non-GPU task with no VRAM axis (and an explicit 0 at 0); above `GPU_USE_MIN_VRAM` it is
    written, so a measured GPU user is gated; a `requires_gpu` task is raised to it either way.
  - `net_idle_vram`: a busy sample is netted against the box's most recent idle reading BEFORE
    it, never a later one; never below 0; a sample with no earlier idle reading yields VRAM `None`
    and its cores/RAM pass through; a sample with `open > 0` and `running == 0` is NOT an idle reading; boxes do not
    share readings.
  - THE INCIDENT, learner half, pure and through the real event log: 9.5 GB of non-fleet VRAM
    beside one CPU-only lane learned 8.0 GB per lane before and learns the floor after, and the
    group's next task boards the box; POSITIVE CONTROL — a group that declared nothing and
    measures 3 GB per lane above the idle reading learns 3.75 and is refused on the full card.
  - `_place_queue` on a real dispatcher: two CPU-only tasks claim the full-card box and the GPU
    task stays queued; with the card free the same GPU task claims it.
  - `_build_task_json` sets `RUNQ_NO_GPU=1` for a CPU-only task and not for a GPU user.
  - the worker launches a marked task with the card at 100% util and 0 GB free, holds an unmarked
    one in the same state, and launches a marked task queued BEHIND a held unmarked one;
    `cpu_load` still holds a marked task.

- **Invariant 4i (forced box placement)** — `tests/test_force_box.py`, hand-built views plus the
  REAL `_instances_view`, each bypass paired with an identical UNFORCED control that is refused:
  - a full box (free slots 0), an exhausted 18a budget, zero measured headroom, a day window whose
    slot cap is 0, and an over-pack cooldown → the forced task `pack`s, the control `hold`s; the
    placement carries one `bypassed` entry per refusing gate, and none when nothing refused.
  - a `paused` box, and a `live` one that is drain-held / ship-quarantined / worker-roll-held →
    BOTH hold; the forced hold reason starts `force_box:` and names the state.
  - `force_box` without `box`, with `colocate`, as the string `"true"`, or naming a rental → not
    forced (placed/held as the ordinary task it is); it never rents and never preempts.
  - `_place_queue` end to end: the claim logs `forced_placement`; a later unforced task in the same
    pass is refused by the footprint the forced one now occupies.
  - `_reap_over_capacity` with the preempt switch ON: the forced occupant stays `running` even as
    the newest task over a zero cap, and its arrival sheds NO existing occupant (the unforced ones
    are shed exactly as they would be without it); `_reap_overpacked_boxes` leaves a gate-held
    forced task alone.
  - worker: a forced pending task launches past a launch gate that is refusing, an unforced one is
    held by it; `FREEZE` holds both.
  - host schedule (box-pause 20e): the pushed payload is the configured schedule with no forced
    occupant, `capacity.uncapped` (all day, cpu/vram 1.0, no `gpu_power`) while one is `claimed`…
    `preempting`, and the configured one again once it is `done`; each transition pushes at once
    and logs `capacity_override` / `capacity_override_lifted`; a failed push records neither.
  - `runq add --force-box`: refused (exit 2, nothing queued) without `--box`, with `--colocate`,
    for an unknown box and for a rental; accepted for an owned box, hint `{box, force_box: true}`.
    The API server refuses the same four with 422.
- `fixtures/dispatch/pack_tightest.json` — task `slots=1, est=60`; instance A `live`, 4 slots,
  2 used, 300 min window; instance B `live`, 4 slots, 1 used, 300 min window → `pack(A)`
  (fewest free slots), reason mentions fit `60×1.25+10=85 ≤ 300`.
- `fixtures/dispatch/deadline_miss.json` — same task; ZERO instances exist at all (the genuine
  bootstrap case — 4c's own text: "applies only when at least one instance is already
  live/provisioning/draining"; the very first box always proceeds straight to rent, no
  patience/backlog test); balance 25.0, rate 0.212, offers `[{dph 0.25, machine 123}]` →
  `rent(offer 0.25)` (0.212+0.25 ≤ 1.00). (A separate, non-fixture note: a window-infeasible
  BUT existing instance does NOT count as bootstrap — it falls through to the backlog-bar test
  like any other non-feasible-now state, per invariant 4c's literal condition.)
- `fixtures/dispatch/infeasible_est_hold.json` — invariant 4(a0): task `est=480` (window
  `480×1.25+10 = 610` > ceiling `hard_cap_hours 9 × 60 = 540`), a live instance with free slots
  and qualifying offers both present → `hold`, reason contains `infeasible_est` (never `rent`,
  never `pack`). Boundary companion (same test, `deadline_miss.json` with `est=424`): window
  `424×1.25+10 = 540 ≤ 540` → falls through to normal placement (bootstrap `rent`).
- `fixtures/dispatch/offer_unfittable.json` — invariant 4(e) fittability filter (bug 13): task
  `slots=7, resource_hint {vram 3.0, cores 2}`; offers = a cheap 6 GB box (`slots_for_offer`=2,
  can never host the task) and a pricier 24 GB/16-core box (`slots_for_offer`=8 ≥ 7) → `rent`
  picks the 24 GB offer (cheapest FITTING, not cheapest overall). Companion case in the same
  fixture: only the 6 GB offer → `hold(no_offer)`, never a rent the task can't use. Fit-filter
  cases (2026-07-21, the Titan Xp sliver): task `slots=1, hint {vram 2.0, cores 4}`; a
  12 GB/1.71-core offer (`slots_for_offer`=0) alone → `hold(no_offer)`; the same sliver next to
  a 12 GB/20-core box → `rent` picks the 20-core box, the sliver is never bookable.
- `fixtures/dispatch/offer_cores_per_dollar.json` — invariant 4(e) cores-per-$ ranking (2026-07-21):
  task `slots=1, hint {vram 2.0, cores 4}`, ranked by `dph / lane_capacity` (uncapped). Case 1 —
  the fix: offers A `{dph 0.0412, 8 GB, 6 cores}` (1 lane) and B `{dph 0.0481, 12 GB, 20 cores}`
  (5 lanes) with an EMPTY queue → `rent(B)` at 0.96¢/lane, even though A is cheaper per box (the old
  demand-capped $/slot rule wrongly picked A here). Case 2 — a box both cheaper AND denser wins.
  Case 3 — equal lane capacity → tie breaks to the cheaper `dph_total`.
- Invariant 4(e) quality gates (gpu_deny + effective-cores floor) are exercised directly against
  `eligible_offers` in `TestEligibleOffers` rather than a `place()` fixture, because they run at the
  fetch-mapping seam (before `place()` ever sees the offers): a `gpu_deny` substring drops a
  GTX 1080 / Titan Xp while an RTX 3060 Ti and a name-less offer survive; a
  `min_cpu_cores_effective=2.0` floor drops a 1.71-core sliver and keeps a 4-core modern card. Their
  counterfactual reasons (`gpu_denied`, `below_cores_floor`, `worse_value_density`) are covered in
  `TestOfferCounterfactual`.
- `fixtures/dispatch/budget_hold.json` — rate `0.72+0.30=1.02` ≥ cap 1.00, no feasible instance,
  backlog qualifies (4 stuck tasks) → `hold(budget)`; the SAME state with a feasible packed slot
  on A → `pack(A)` (packing ignores budget).
- `fixtures/dispatch/balance_hold.json` — balance 2.50 < floor 3.00, offers available, backlog
  qualifies → `hold(balance)`.
- `fixtures/dispatch/offer_filters.json` — offers `[{dph 0.55}, {dph 0.32, machine ∈ deny},
  {dph 0.38, machine 77}]` → rent picks `0.38/machine 77` (first is over `max_instance_dph`,
  second is denied).
- `fixtures/dispatch/slots_for_offer.json` — offer `gpu_ram_gb=24, cpu_cores_effective=32`,
  default hint (0.6, 1) → `slots_total = min(40, 32) → capped 8`; offer `gpu_ram_gb=8,
  cpu_cores_effective=8`, same hint → `min(13, 8) → capped 8`; offer `gpu_ram_gb=16,
  cpu_cores_effective=16`, task `resource_hint={vram_per_lane_gb: 2, cores_per_lane: 16}`
  (M11-pixel-Atari-class, CPU-bound) → `min(8, 1) → 1`. Fit-filter cases (2026-07-21): offer
  `gpu_ram_gb=12, cpu_cores_effective=1.71` with hint `{vram 2, cores 4}` → `min(6, 0) → 0`
  (the Titan Xp sliver); offer `gpu_ram_gb=1, cpu_cores_effective=8` with hint
  `{vram 2, cores 1}` → `min(0, 8) → 0`.
- `fixtures/dispatch/slot_freeing_soon.json` — one live instance (patience/backlog test applies);
  task `slots=1`; instance A full (0 free) but a running 1-slot task started 68 min ago with
  `est=60` (`remaining_est = 60×1.25−68 = 7 ≤ 15`), window fits after the wait →
  `hold(slot_freeing_soon)`; same state but the running task started 20 min ago
  (`remaining_est = 55 > 15`) → falls through to the backlog-bar test.
- `fixtures/dispatch/backlog_too_small.json` — one live instance, no slot freeing soon
  (`remaining_est=55 > 15`); queue has exactly this 1 task (`est=20`, priority 50, default) →
  `backlog_tasks=1 < 3` and `backlog_task_minutes=20 < 60` → `hold(backlog_too_small)`, no
  timeout; adding a 2nd stuck task at `est=25` → still `1+2=45 < 60` and `2 < 3` →
  still `hold(backlog_too_small)`; adding a 3rd stuck task → `backlog_tasks=3 ≥ 3` → proceeds to
  rent (money gates permitting).
- `fixtures/dispatch/priority_bypass_no_candidate.json` — same as `backlog_too_small` (1 stuck
  task, backlog under both bars) but the task's `priority=70` (above the schema default of 50)
  and every running task everywhere is priority 50 (gap only 20 < margin 30, so none is an
  eligible victim) → no `preempt` candidate, but `priority=70 > 50` bypasses
  `hold(backlog_too_small)` → proceeds to rent directly.
- `fixtures/dispatch/preempt.json` — task T `priority=80, slots=1`; instance A full, one running
  task V `priority=40, slots=1` (gap 40 ≥ margin 30) → `preempt(A, victims=[V])`; same instance
  but V `priority=55` (gap 25 < 30) → not eligible, falls through to backlog-bar/rent.
- `fixtures/dispatch/preempt_requeue.json` — task V in `preempting`; worker reports `PREEMPTED`
  with `out/ckpt_latest.pt` mtime newer than the `PREEMPT` marker → ingest pulls the checkpoint,
  sets `resume_checkpoint`, CASes `preempting → queued`; `retries_used` unchanged (0 before and
  after) — contrast with `retry.json` where an equivalent `infra_failed → queued` DOES increment
  it. A second fixture case: V's process reaches `DONE` before any `PREEMPTED` marker appears →
  treated as ordinary completion, the stale `PREEMPT` marker is ignored.
- `fixtures/dispatch/ssh_fallback.json` — instance with `ssh_fallback_fails=3`; 2 consecutive
  proxy failures → still proxy; 3rd consecutive failure → switches to direct endpoint for that
  instance, one `event`; a later transient direct-endpoint failure does NOT switch back to proxy.
- `fixtures/dispatch/teardown.json` — instance idle 12 min, no queued feasible task →
  `(True, idle)`; same but a queued task fits its window/slots → `(False, feasible_task_waiting)`;
  a task in `preempting` on the instance → `(False, feasible_task_waiting)` (graceful eviction in
  flight counts as open work); any state with `now ≥ hard_cap_at` → `(True, hard_cap)`.
- `fixtures/dispatch/retry.json` — `infra_failed, retries_used=0, max_retries=3` → `requeue`
  (1st infra failure); `retries_used=2.5, max_retries=3` → `requeue` (5th infra failure, budget
  not yet exhausted — each costs 0.5); `retries_used=3.0, max_retries=3` → `terminal` (6th infra
  failure exhausts the 3-retry budget). A `task_failed` task never reaches `retry_decision`.
- `fixtures/dispatch/stall.json` — `ckpt_mtime=T, now=T` → not stalled; `now=T+89min` (just under
  `stall_timeout_min=90`) → not stalled; `now=T+90min` → stalled; `ckpt_mtime=null` and
  `running_since` unknown → never stalled (no anchor at all); `ckpt_mtime=null` with a fresh
  `running_since` → not stalled; `ckpt_mtime=null` with a stale `running_since` → stalled (bug 13:
  a task that never reached its first checkpoint still ages off `running_since`).
- `fixtures/dispatch/overpack_probe_eligibility.json` — invariant 19i(c). Machine `m555`,
  `slots_total=8`, row `{"cap": 3, "at": T, "backoff_min": 240}`. At `now = T+239min` →
  `_effective_slots == 3` (not yet eligible, no `overpack_probe` event). At `now = T+240min` →
  `_effective_slots == 4` and one `overpack_probe` START event. **The allowance is exactly +1**: a
  box capped at 3 never reports 5, however long the cap has stood — this is the fixture that pins
  19i(c) against a "recover all the way" reading.
- `fixtures/dispatch/overpack_probe_raises.json` — invariant 19i(c), success branch. Cap 3, probe
  allowance active, 4 tasks placed; all 4 reach `running` inside `ship_launch_grace_min` →
  cap row becomes `{"cap": 4, "at": now, "backoff_min": 240 (reset), "probed_at": now}` and an
  `overpack_probe` `raised` event is emitted. Asserts the raise goes through the RECOVERY path,
  **not** `_learn_overpack_cap` — calling the learner with 4 against a stored 3 must still be a
  no-op (19i(b); the existing `test_learned_cap_only_lowers_and_caps_instances_view` must still
  pass unchanged).
- `fixtures/dispatch/overpack_probe_holds.json` — invariant 19i(c), failure branch. Cap 3, probe
  allowance active, 4th task shipped and still `shipped` at `+ship_launch_grace_min` → 19h fires:
  the task goes `shipped → queued` with **`retries_used` UNCHANGED and `instance_id` cleared**, the
  cap stays 3 (re-learning `max(1, running) = 3` is a no-op), `backoff_min` doubles 240 → 480, and
  an `overpack_probe` `held` event records the new interval. A second failure doubles to 960; from
  960 the next doubling **clamps at `overpack_probe_backoff_max_min = 1440`**, never beyond.
- `fixtures/dispatch/overpack_probe_row_compat.json` — invariant 19i(e). Legacy bare-int row `3`
  (the shape of EVERY row in the live registry as of 2026-08-02) → parses as cap 3, probe-eligible
  once `overpack_probe_after_min` has elapsed against its unknown-epoch `at`. Row `{"cap": 0}` →
  clamped to 1 (a 0-slot cap would make the box unplaceable forever). Row `"banana"` / `{}` /
  `{"cap": "x"}` → treated as ABSENT, `_effective_slots == slots_total` (fail-open, 27.c), and a
  single `overpack_cap` warning event — never a raised exception inside placement.
- `fixtures/dispatch/overpack_probe_no_work.json` — invariant 19i(d). Cap 3, probe-eligible, but
  the queue holds **zero** tasks that fit → no ship, no rent, no preempt, and `backoff_min` is
  **unchanged** (an unused allowance is not a failed probe — this is the fixture that stops the
  backoff from running away on an idle fleet). With `overpack_probe_enabled=false` →
  `_effective_slots == 3` and no `overpack_probe` event, whatever the cap's age.
- `fixtures/dispatch/reconcile.json` — DB live `{12345, 67890}`, Vast shows `{12345}` plus
  unlabeled `99999` → divergences: `67890 lost` (its running task requeued) and
  `99999 foreign_instance` (no action). Vast also showing `55555` labeled `runq_abc` absent from
  DB with `abc` IN this registry's tasks → `adopt`; `44444` labeled `runq_zzz` with `zzz` NOT in
  this registry's tasks → `foreign_instance` (invariant 3b as revised 2026-07-09 — another
  registry's live box, never touched).
- **`eligible_offers` (invariant 4e, `TestEligibleOffers`)** — raw offers with `reliability2`
  `{0.85, 0.97}`, one with no reliability field, one with fallback `reliability=0.80`, one exactly
  at the floor `0.90`, floor `0.90` → keeps the `0.97`, the field-less (fail-open), and the `0.90`
  (inclusive); drops the `0.85` and `0.80`; maps `gpu_ram=12288 → gpu_ram_gb=12.0`. Floor `0.0`
  keeps everything.
- **Settings migration (`TestSettingsMigration`)** — a registry pre-seeded
  `provision_timeout_min=45` (the superseded default) → after start it is `15` and the new
  `min_reliability=0.90` is seeded alongside; a registry pre-seeded `provision_timeout_min=30`
  (operator-tuned) → left at `30`; a fresh registry → `15` / `0.90`.
- `fixtures/dispatch/worker_reject.json` — `task.json` missing `argv` → worker `reject` event +
  `FAILED_validation`, nothing executed.
- Idempotent restart: replaying any fixture sequence twice against the same DB produces no
  duplicate events and no repeated `vastai`/rsync intents (mirror of supervisor invariant 8).
- **Paid end-to-end smoke — RUN 2026-07-08, real Vast box, total spend $0.39** (GTX 1660,
  $0.041/hr, instance 40000004): `runq add` a low-priority task, ran it to mid-flight, rented
  correctly (bootstrap ssh probe + payload ship + `spool_worker.py` start all for real), then
  `runq add`'d a high-priority task to force a real priority preemption — the running task was
  evicted only after its own next checkpoint write, requeued with `retries_used` unchanged,
  re-shipped with `--init-from`, and resumed+completed from the carried-forward checkpoint (not
  from scratch). The evictor packed and completed normally. Idle-timeout destroyed the instance
  automatically once both tasks were `done`; `vastai show instances` confirmed it was gone
  (billing stopped) afterward. Four real bugs were found and fixed by this run (none caught by
  the mocked docker integration test, since it never round-trips through a real dispatcher
  restart or a real credit-only Vast account):
  1. `vastai show user`'s `balance` field reads 0 on a credit-only account even with real
     spendable funds sitting in `credit` — the rent gate would have refused to rent at all
     (`account_balance()` now sums both).
  2. The dispatcher baked ITS OWN `sys.executable` into the shipped `argv` at ship time, instead
     of leaving `"python"` literal for the box's own worker to resolve against its own
     interpreter — crashed every real box whose Python lives somewhere other than the
     dispatcher's own machine.
  3. `spool_worker.py`'s bootstrap shipped `spool_worker.py` alone; `sweep_supervisor.py` is a
     required sibling import, so every real launch crashed with `ModuleNotFoundError` until
     both files are shipped together.
  4. A requeued task's stale spool dir from its PREVIOUS attempt (the worker never deletes task
     dirs) made the first-ship idempotency check think a re-ship was "already delivered",
     silently dropping the new `resume.pt`/`--init-from` — invariant 7's restart-safety
     idempotency and invariant 17's resume-carry actively conflicted until re-ships explicitly
     clear the stale dir first. A second, compounding bug in the same area: worker.jsonl is
     append-only across a task's whole lifetime, so ingest matched the STALE claim/start event
     from the first attempt and marked the task falsely "running" again with nothing actually
     relaunched.
  5. (also found, not preemption-specific) `cost_usd` was only ever stamped on the `lost` path;
     a normal idle/hard-cap `destroy` left it `NULL`, silently breaking `runq spend`.
  6. **(found 2026-07-09 by the first real dispatched TRAINING campaign — M13
     `native_m13/planet_cup1m_s0`, instance 40000005):** a proxy endpoint that hangs (accepts
     the connection but never moves data — the mid-life proxy death `watch_and_pull.sh` learned
     about first) made every transport helper raise `subprocess.TimeoutExpired` straight
     through the poll loop: the timeout never counted as a failure toward invariant 9a's
     proxy→direct fallback (the tracker only ever saw returncode failures), and the uncaught
     exception KILLED the daemon mid-`_ship_all`. The task sat `shipped`-but-never-claimed
     until a later `--once` pass reconciled it back to `queued`; the box idled ~86 min
     (≈$0.06). Fix: every transport helper (`rsync_push`/`rsync_pull`/`ssh_run`/`vastai_json`/
     `direct_endpoint`) converts `TimeoutExpired` into an ordinary failure return, so 9a's
     counter sees it and the loop survives; and the rsync `-e` ssh string now carries
     `ConnectTimeout=20` like `ssh_cmd` always did, so a dead proxy fails fast instead of
     eating the full rsync timeout per attempt.
  7. **(found 2026-07-09, the registry split-brain — multi-session concurrency, minutes after
     bug 6):** three sessions each started a dispatcher from their OWN worktree; the
     ROOT-relative `DEFAULT_DB`/lock defaults silently gave each its own registry, its own
     "singleton" lock, and its own fleet — and invariant 3b's then-unconditional adopt (which
     stamps `hard_cap_at = now`) made every daemon classify every OTHER registry's
     freshly-rented `runq_*` boxes as orphans and destroy them (two live boxes killed within
     ~60s of creation before the daemons were stopped; ≈$0.01 wasted, but the failure mode
     scales with fleet size). Two fixes: shared-root resolution for DB + lock (invariant 1),
     and adopt gated on the label's task-id existing in this registry (invariant 3b) so a
     foreign registry's box is never touched.
  8. **(found 2026-07-09, the M13 campaign's first actual on-box run):** `train_dmc` crashed at
     `import dm_control` — the stock Vast pytorch images ship NO system EGL loader
     (`libEGL.so.1`), which pip can't express (`pip_extras` covers Python deps only; the
     devcontainer masked this by happening to have mesa/glvnd installed). Fix: entrypoints may
     declare `apt_packages`; the DISPATCHER installs them over ssh at ship time (dpkg-guarded
     idempotent, apt-update fallback, 300s budget, failure = ship failure feeding the 9a
     tracker) — dispatcher-side deliberately, so `task.json`'s schema and already-deployed
     box workers stay untouched. `train_dmc` declares `["libegl1", "libgles2"]`; the fix was
     verified live on the failed box (40000006) before landing.
  All eight are now covered by regression tests (`tests/test_dispatcher.py`:
  `TestReshipClearsStaleBoxState`, `TestWorkerStateIngestIgnoresStaleHistoricalEvents`,
  `TestCostRecordedOnDestroy`, `TestAccountBalance`, `TestTransportTimeoutIsFailureNotCrash`,
  `TestReconcile` foreign-registry cases + `TestSharedRootResolution`), and by invariant 5's
  rent-sequence ordering fix (`instances.id` cannot be chosen before `vastai create instance`
  returns it — found during implementation, before this run, but the same "verify against
  reality" spirit).

## Open questions

- **19i-Q1 — are OWNED boxes subject to the 19h cap at all?** For `source='owned'` the operator has
  *declared* capacity in `configs/capacity/<label>.json`, and three independent gates already adapt
  on their own (the time-of-day window, measured headroom 23, the per-task budget check 18a). Every
  owned-box cap ever observed has been a 19h false positive — 2026-07-28 (laptop → 4), 2026-08-01
  (laptop → 1), 2026-08-02 (laptop AND desktop → 1). Three options, materially different: **(i)**
  exempt owned boxes from `_learn_overpack_cap` entirely and let the declared window bind; **(ii)**
  keep the cap but floor it at the window's slot count; **(iii)** treat them like any other box and
  rely on 19i's probe. Recommend **(i)** — for a box the operator owns and has written a policy
  file for, a reactively-learned ceiling *below* that policy has no authority to add — but this is
  the operator's call, and it decides whether 19i even runs on the two boxes that motivated it.
- **19i-Q2 — what is the right `overpack_probe_after_min`?** `240` above is a placeholder, not a
  measurement. The tension is real in both directions: too short and every genuinely weak box pays
  a `ship_launch_grace_min` (35 min) task delay on a cycle; too long and a mis-learned cap costs
  hours of capacity (the 08-02 incident ran ~13 h). It should be *derived*, not guessed — the
  `overpack_cap` event history plus the new `overpack_probe` outcomes give the base rate of
  false-positive caps, which is the number that sets it. Ship with a placeholder and re-fit, or
  block on the fit?
- **19i-Q3 — should the RENT FILTER avoid machines with a low learned cap?** Possibly a bigger win
  than the probe itself, and it is not covered by 19i. On 2026-08-02 the fleet held **three
  concurrent rentals of m100001**, each capped at 1, each paying ~$0.069/hr for a single lane —
  `eligible_offers` has no notion of a machine's learned cap, so it re-bought the same poisoned
  host repeatedly. Options: rank a capped machine's offers down by its cap-to-`slots_for_offer`
  ratio, or exclude machines whose cap is below some fraction of what the offer advertises. This
  interacts with 27 (the rent filter must admit on the same axes as the admission gate) and may
  belong in that invariant rather than 19i.
- **19i-Q4 — is the probe VERDICT confounded by ship latency?** ⚠ This is the same failure mode as
  the original defect and must be settled before implementation. 19h ages a gate-held task from
  `updated_at`, but the 08-02 measurements show the poll cycle itself running **138 s median /
  355 s max** against `poll_seconds = 30`, and on a healthy box the 8th lane took **12.1 min**
  between its `ship` event and `running` — a third of the 35-min grace for ONE lane. So a `held`
  verdict may record a slow ship path rather than a real launch-gate refusal, in which case 19i
  would faithfully re-confirm caps that are still wrong and the backoff would harden them. Proposal:
  age the probe from the task's **`ship` event** (proof the bytes reached the box) rather than from
  `updated_at`, and abstain from any verdict on a poll cycle whose `total_sec` exceeded some bound.
  Needs a decision — an unreadable probe is worse than no probe, because it manufactures evidence.

- **Per-task progress fraction / ETA (owner ask 2026-07-22, deferred to its own feature).** The
  consolidation near-done guard (21d) and idle-vs-active reasoning use the *est-based* remaining
  window (`est_minutes × est_safety − running_minutes_ago`), which is a crude ETA: `est_minutes` is
  the human/`est_defaults` estimate, so a task that's actually 95% done but past its estimate reads
  as "overdue" (currently drain-eligible). A real per-task **progress fraction + ETA** would sharpen
  churn-avoidance and is "nice for a lot of reasons" (dashboard, calibration, scheduling). Proposed
  shape (needs its own spec, likely under `spec/infra/trainer-harness`): the harness
  (`shared.infra.harness`) writes a small `progress.json` beside `ckpt_latest.pt` at each checkpoint
  — `{step, total, fraction, rate, eta_seconds, updated_at}` — populated from a trainer-reported
  `(current, total)` (one call next to the `progress/online_step` TB log the trainers already emit);
  the dispatcher pulls it alongside the checkpoint (invariant 9c) and stores it per task, and 21d
  skips a task whose `fraction ≥ consolidate_skip_above_fraction`. **Caveat:** *continual /
  open-ended* runs (the project north star) have no `total` → `fraction` is undefined; they report
  `rate` only, are never "near-done", and stay drain-eligible (correct). Until built, the est-based
  guard is the interim.

- **★★★ PLACEMENT DOES NOT MODEL SHIP THROUGHPUT, so a ship-path bottleneck is answered by RENTING —
  which cannot help, and burns budget until the cap binds (measured live 2026-07-30 06:47).** This is
  the compounding form of every finding above, and it is the one that costs real money.
  - **Measured, all 14 paid boxes at one instant:**

        boxes with ZERO running tasks: 10 of 14, costing $0.5641/hr = 73% OF THE ENTIRE BURN
        40000019 / 40000020 / 40000021: alive 91-92 min, 5 claimed each, 0 EVER delivered
        40000018:                       alive 117 min,  1 claimed,     0 running
        6 more boxes rented in the last 13 min, 0 tasks each
        laptop-gpu (FREE, 32 cores):   5 claimed,      0 running
        committed rate $0.7737/hr = 77% of max_hourly_usd, up from 44% ~30 min earlier

  - **The loop:** the ship path is saturated (see the pass-latency and compile entries) ⇒ delivered
    work lags claims ⇒ boxes sit `claimed`-but-never-`shipped` and run nothing ⇒ the still-large QUEUE
    reads as unmet demand ⇒ placement rents another box ⇒ that box also cannot be shipped to. Adding
    capacity cannot raise throughput when the constraint is the coordinator's SERIAL ship phase, but
    nothing in placement expresses that, so it keeps buying the wrong resource.
  - **★ CLOSED COST OF ONE INCIDENT — the 6-box burst of 06:31-06:47, all six now terminal, ZERO tasks
    shipped between them:**

        40000022  $0.0332  never reached running   (Vast dud)
        40000023  $0.0959  idle                    (OVER-RENT)
        40000024  $0.0329  never reached running   (Vast dud)
        40000025  $0.1232  idle                    (OVER-RENT)
        40000026  $0.0000  boot ceiling            (Vast dud)
        40000027  $0.1655  idle                    (OVER-RENT)
                  -------
                  $0.4507  total, for ZERO delivered work
                           $0.3847 over-rent (85%) | $0.0661 Vast dud rate (15%)

    So **one 13-minute rental burst cost $0.45 and delivered nothing**, and ~85% of that was boxes
    that came up perfectly healthy and were never given work — `box_measured` on 40000023 read
    `load 0.00/12 cores, vram 0.0/12.0GB, util 0%` at 06:51, 07:13, 07:38 and 08:09 before it was
    reaped `idle` at 08:22. **This is the per-incident price of the loop above**, and it is the number
    to weigh the fix against rather than the rates.
    **Attribution caveat worth preserving:** for the first ~90 minutes this same day's waste was
    *entirely* Vast dud rate with ZERO `idle` teardowns, because an over-rented box bills for hours
    before the idle reaper collects it. **Over-rent waste is invisible until it is reaped** — measuring
    it early reads as "just Vast duds" (I reported exactly that, correctly at the time, and it inverted
    later: $0.3847 over-rent vs $0.1327 dud). Judge this category on terminal boxes only.
  - **What bounds it (and what does not):** `max_hourly_usd` (inv. 11) is the ONLY brake — at 100% the
    budget check refuses further rentals, so the waste self-limits at the cap (~$24/day at $1.00/hr).
    Invariant 5c's over-provisioning guard counts `provisioning` boxes as incoming capacity, and it did
    NOT prevent 6 rentals in 13 minutes, so it is bounding redundancy per-task rather than fleet-wide
    against a queue this size. Note the brake is a MONEY cap standing in for a THROUGHPUT limit —
    it stops the bleeding without ever diagnosing it, which is why this went unnoticed until measured.
  - **Fix direction (NOT applied):** placement should treat *deliverable* throughput as the constraint,
    not box count — e.g. refuse to rent while the number of `claimed`-not-`shipped` tasks already
    exceeds what a pass can deliver (the ship budget divided by observed per-task ship cost), since
    those tasks are proof that capacity is not the binding gate. Cheapest partial: count
    `claimed`-not-`shipped` occupants fleet-wide as pending supply in the rent decision, so a backlog
    the fleet has already bought capacity for cannot justify buying more.

- **A FAILED `vastai create` is not remembered, so the selector re-picks the same unbookable offer
  across polls (measured 2026-07-30).** On a create failure `_rent` does exactly one thing:

      self.log("rent_failed", f"vastai create instance failed for offer {offer}", ...)
      return

  No denylist write, no offer-level memo. Nothing prevents the next pass — or the next task in the
  *same* pass — from selecting the identical offer, and the selector is deterministic
  (cheapest by value-density), so it does.
  - **Measured:** offer **40000001 failed 5 times across 41 minutes and THREE separate polls**
    (05:13:10, 05:13:16, 05:34:27, 05:34:30, 05:54:31); offer 40000003 failed twice. Same-day
    create-failure rate **47% (9 failed / 10 created)** against 7% on 07-29 and 14% on 07-28, so the
    aggregate is an outlier as well as the repetition. The two tasks behind the 05:34 pair
    (`plan_attn_cgate`, `plan_attn_cobj`) were still `queued` afterwards — the failure BLOCKS placement
    for that task that pass, it does not fall through to the next-best offer.
  - **⇒ ANSWERS the TTL question posed below, with a lower bound: a dead offer stays in the offer
    search for AT LEAST 41 minutes.** It was still being returned and selected 41 min after its first
    create failure, so any skip-TTL shorter than that would not have prevented a single one of the four
    repeats. That makes the TTL approach need a *measured, generous* value — and makes the
    fall-through fix (below), which needs no TTL at all, the more attractive first move.
  - **Why it is worse under load:** more queued tasks reach the rent path per pass, and they all pick
    the same cheapest offer, so one stale offer can burn several creates per pass. Disabling
    preemption ([0a](#0a-the-global-preempt-switch)) increases renting and therefore exposure, but the
    gap itself predates it (8 failures on 07-28).
  - **Proposed fix (NOT applied):** remember the failed **OFFER id** with a short TTL and skip it in
    the qualifying-offer filter. Deliberately *not* a machine deny: the machine is usually fine and the
    OFFER is what went stale, and this repo has already been burned by an unmeasured deny/timeout
    constant that would have destroyed 63 healthy boxes (see inv. 5b's comment, which destroys a
    transient dud WITHOUT denying for exactly this reason). A TTL needs measuring, not guessing — how
    long does a failed offer keep being returned by the offer search? Also consider falling through to
    the next-best offer within the same attempt rather than returning, which fixes the placement
    blockage independently of any memory.

- **★★★ 10d DEADLOCKS on the box it exists for: its remediation needs ssh, and the failing case is an
  unreachable box — so tasks are stranded INDEFINITELY and the box is never even quarantined
  (observed live 2026-07-30, instance 40000020).** This is the sharpest gap found so far, because the
  safety check prevents the safety action.
  - **The code:** 10d clears the box's spool copy over ssh BEFORE requeueing — correct in intent, to
    stop a link that recovers later from launching a task already handed to someone else. But:

        res = ssh_run(..., f"rm -rf ~/spool/incoming/{t['id']} ~/spool/active/{t['id']}", ...)
        if res.returncode != 0:
            self.log("undeliverable_defer", f"box cleanup failed rc={res.returncode} ...")
            continue                    # <- the task is NEVER requeued
...
        if requeued:                    # <- False when EVERY cleanup deferred
            self._set_ship_quarantine(...)   # <- so the box is NEVER quarantined either

    `rc=255` is ssh connection failure. On a box whose ssh is broken — **precisely the case 10d
    exists for** — the cleanup cannot run, so 10d defers forever.
  - **Measured:** `azsc-p2c/seed11` claimed on 40000020 at 15:57:17. Ship failures at 15:58, 16:14,
    16:25, 18:34. 10d fired FOUR times (17:31:53, 17:42:26, 18:06:30, 18:32:56) and requeued
    **nothing**, logging `box cleanup failed rc=255 — will retry next poll` every time. After
    **2h37m** the task was still `claimed`, with `retries_used = 0` — it has not even burnt a retry,
    so no budget will ever expire it. There is no timeout on this path.
  - **The compounding half:** because `requeued == 0`, the `if requeued:` guard skips the quarantine
    too, so the box stays in the placement pool and keeps ATTRACTING tasks — 40000020 accumulated 6
    claims this way. The guard's comment says this is deliberate ("we must not also bar it from the
    pool on the strength of an ssh we could not complete"), which is right for a transient blip and
    wrong once ship AND cleanup have both failed repeatedly over an hour: that is no longer one
    inconclusive ssh, it is two independent channels agreeing the box is gone.
  - **Contrast 10b, which does not deadlock** because it `_destroy`s the box: destroying makes the
    spool copy moot, so the double-run hazard the cleanup guards against simply cannot arise. The
    spec notes 10d deliberately has "no `_destroy` backstop" — that choice is what leaves it with no
    exit when the box never recovers.
  - **★ PROVEN by a controlled contrast, 18:59:59 — the only exit is an EXTERNAL event.** Two boxes
    entered the same deadlock; they ended differently for a reason that settles the "indefinitely"
    claim:
      * **40000029 went `lost`** ("missing from `vastai show instances`"). Reconcile's lost-instance
        path needs NO ssh, so it succeeded where 10d's cleanup could not: all **8** tasks were
        `infra_failed` + requeued in the same second, at half a retry, and are now `queued` with
        `instance_id = NULL`. The box billed **$0.178 over 3h15m for zero deliveries**.
      * **40000020 stayed `live`** — and `azsc-p2c/seed11` is STILL `claimed` on it with
        `retries_used = 0` after ~3 hours.
    So the tasks that escaped did so only because Vast removed the instance from its inventory. **A
    deadlocked box that keeps answering `vastai show instances` strands its tasks with no mechanism
    able to release them** — not 10d (cleanup needs the dead ssh), not the retry budget (never
    charged), not the quarantine (skipped by `if requeued:`). That is the case the fix must cover;
    waiting for the instance to vanish is luck, not a reaper.
  - **Fix direction (NOT applied):** after N consecutive `undeliverable_defer`s for the same instance
    (or T minutes), escalate rather than repeat — (a) quarantine anyway, since by then the box has
    failed both ship and cleanup repeatedly and the "one inconclusive ssh" caveat no longer holds;
    and/or (b) fall back to `_destroy` for a `source='vast'` box exactly as 10b does, which makes the
    spool copy unreachable-and-irrelevant and permits the requeue. `_destroy`'s owned-box carve-out
    already protects owned boxes from (b). Needs a test that a SINGLE failed cleanup still defers
    (the transient case) while a sustained streak escalates.

- **★★ A box that has NEVER delivered is distinguishable from a slow one WITHOUT waiting 90 minutes —
  10d's gate is per-TASK age, but "this box has never shipped anything" is a BOX-level predicate
  (observed live 2026-07-30 16:45).**
  - **Observed:** instance 40000029 reached `live`, and in **61 minutes accepted ZERO bundles** — 4
    `ship_failed` (16:07 / 16:18 / 16:30 / 16:44) and no `ship`, `start` or `done` event of any kind.
    Meanwhile it accumulated **8 claimed tasks**. `box_measured` showed it alive and idle throughout
    (load 13-14/72 cores, 92 GB RAM free, `vram 0.0/22.0GB util 0%`) — so it answers probes while
    doing nothing, which is why no liveness check flags it.
  - **Why nothing reaps it promptly:** 10b needs state `shipped` (these never shipped) and skips boxes
    with `consecutive_fails > 0`; 10c needs a dead worker, and this one answers; 19/19h watch
    `running`/`shipped`, and it has neither. Only 10d applies, and its kill criterion is per-TASK:
    `claimed` longer than `ship_timeout_min` (90). So the first release is ~100 min after the box went
    live, and the serial drain above then releases roughly one task per pass.
  - **Why the 90-minute gate does not need to apply here.** It is justified by a TASK statistic —
    fleet-wide claim→ship p99 is 98.7 min and the longest successful ship took 301.8 min, so an
    age-only per-task rule would have requeued 70 ships that went on to land. **That reasoning does
    not transfer to a box that has never completed a single delivery in its lifetime.** There is no
    successful-but-slow case to protect: the evidence is not "this task is taking a while", it is
    "nothing has ever crossed this link".
  - **Proposed predicate (NOT applied):** quarantine an instance that is `live`, has **zero `ship`
    events since it went live**, and has **>= N consecutive `ship_failed`** — independent of any task's
    age. That catches this box in ~15 min instead of ~100, cannot fire on a box that has ever
    delivered, and reuses the existing quarantine machinery (`_fits_now` honours it; `should_teardown`
    reaps it once empty) rather than adding a new remediation. Needs a test that a box with even ONE
    successful ship is exempt, and that N is measured rather than guessed.

- **★★ 19's stall remediation REQUEUES WITHOUT QUARANTINING, so placement re-lands the same tasks on
  the same wedged box (observed live 2026-07-30 16:36).** 10b's own docstring already states the
  principle — *"without the destroy the requeued task just returns to the same broken box until its
  retry budget burns out"* — and 10b acts on it. **19 does not.**
  - **Observed:** box 40000020 wedged: it could not RECEIVE (3 `ship_failed` on `azsc-p2c/seed11`,
    15:58 / 16:14 / 16:25, zero successes) *and* its four RUNNING tasks made no checkpoint or TB
    progress for 90 min. At 16:36:30 `_reap_stalled` correctly infra-failed all four and requeued them
    at half a retry (`instance_id` cleared). **Placement then re-claimed all four onto 40000020**,
    which afterwards held **6 claimed tasks it cannot receive**, each having already burnt 0.5 of its
    retry budget and 90 min of compute.
  - **Why it does not self-correct promptly:** the box stays `live` and unquarantined, so `_fits_now`
    keeps admitting to it. The only exit is 10d, which needs a claimed task older than
    `ship_timeout_min` (90) *with* a recorded per-task `ship_failed` — roughly an hour after the
    stall — so the tasks sit on a known-dead box in the meantime, and each stall/requeue cycle costs
    another half retry against `max_retries`.
  - **The diagnostic lesson, which is the transferable part:** the ship failures were a SYMPTOM, not
    the disease. Three `ship_failed` on one box reads as a transport fault and invites probing the
    ship path (bundle size, link speed, per-group payload — all of which were checked and refuted
    here). The decisive evidence came from a DIFFERENT subsystem: the box's own running tasks had
    stopped progressing. **A box that cannot receive AND whose running work has stalled is wedged, not
    slow** — and the two signals live in different reapers, so neither alone says so.
  - **Fix direction (NOT applied):** give 19's remediation the same box-level consequence 10b and 10d
    already have — quarantine the instance (`ship_quarantine_i<id>`, which `_fits_now` honours and
    `should_teardown` reaps once empty) when a stall infra-fails EVERY running occupant of a box, as
    distinct from one task stalling on an otherwise healthy box. The all-occupants condition is what
    separates "this box is wedged" from "this task is stuck", and it must not fire on the latter.

- **A DEGRADED (slow, not dead) box drains at <=1 task per poll, because 7b deliberately bounds it to
  one attempt per pass (observed live 2026-07-30, instance 40000017). This is 7b working as designed;
  the only open question is whether the resulting hold-time on the box's other claimed tasks is worth
  reducing.**
  **⚠ CORRECTION — the first version of this entry (commit 263e20c/e54c0d8) was WRONG and is retracted
  here.** It claimed the box's "data plane [was] dead", that 10d would requeue only one task at the
  90-min mark, and proposed widening 10d's kill criterion. All three were mistaken:
  - **The box was SLOW, not dead.** `ba2ed70f-d` logged `ship_failed` at 04:43:48 (3 rsync attempts),
    was retried on the next two passes (04:47:57, 04:56:54) and **SHIPPED SUCCESSFULLY at 04:57:06** —
    ~13 min and 3 passes after the "failure". `--partial` plus a per-pass retry is exactly what
    carried it. Delivery succeeding means **10d never becomes the operative path at all** (it requires
    age > 90 min with a recorded per-task `ship_failed`, and it also lifts any quarantine on a
    successful delivery), so no change to its kill criterion is warranted on this evidence.
  - **The probe that produced the wrong answer:** `rsync --timeout=60` on a 10MB payload timed out, and
    I read that as "undeliverable". **A short-timeout probe reads DEAD on a box that is merely slow.**
    The pair of lessons is: `ssh echo` is too cheap to detect a degraded link (it passes), and a
    tightly-bounded rsync is too harsh (it fails on a link that works). To classify a link, match the
    probe to the real payload and the real patience — bundle-sized, `--partial`, and the timeout the
    ship path actually uses — or just read whether `ship` events eventually appear.
  What DOES stand, and is the real (milder) trade:
  - **7b** guarantees "a dead box costs one attempt per pass instead of one per task": on a transport
    failure it adds the box to `undeliverable` and skips its remaining claimed tasks **without
    attempting them**, logging `ship_box_deferred`.
  - **⇒ Throughput to a degraded box is capped at one task per poll**, and the loop retries the same
    earliest claimed task (stable `priority DESC, created_at ASC`) until it lands, then moves to the
    next. Observed on 40000017: 8 tasks claimed 04:38; `ba2ed70f-d` failed 04:43, landed 04:57 (3
    passes); the loop then moved to `4485c2f1-0`, which failed 05:03. So ~1 task per 1-3 polls, i.e.
    **roughly 15-90 min for 8 tasks**, not the 2.2h the retracted version claimed.
  - **Cost, revised down:** the box bills while draining (~$0.05/hr), each retry re-pays a compile
    (12-13s, a cache HIT, so cheap), and the box's remaining claimed tasks hold slots — which can
    provoke rentals for the queue behind them. No task is lost and no requeue is needed.
  - **★★ AND IT DOES NOT SELF-HEAL: a box that COMPUTES fine but cannot RECEIVE is exempt from 10d,
    the only reaper that would free its claims.** 10d skips any box with a live occupant —

        if any(t["state"] in ("running", "preempting") for t in occ):
            continue  # the box is doing real work — this is not its problem

    — which is right for a healthy box and wrong for a half-degraded one. Measured on 40000017 at
    06:40: **2 tasks RUNNING normally** (88 min and 47 min in — the GPU and worker are fine, only
    inbound bulk transport is broken), **6 tasks `claimed` and undeliverable**, and `22ddd2e2-2` had
    failed **4 times across 55 min** (05:43:40, 06:04:45, 06:18:44, 06:38:32) after the box's last
    successful delivery at 05:37:15 — **62 minutes with ZERO deliveries** while still burning ~384s of
    every pass. Every one of 10d's other conditions was satisfied (claimed ~123 min > the 90-min
    `ship_timeout_min`; a recorded per-task `ship_failed` on this instance); only the
    running-occupant guard held it off.
    **⇒ So 10d offers NO exit while the box has running work — but slow delivery still does.** Full
    delivery record for 40000017: **04:57, 05:37, 06:53 — 3 deliveries in 141 min (~1 per 47 min,
    worst gap 76 min)**, the last landing on its FIFTH attempt after four consecutive failures. So the
    claims do drain, just at ~47 min each, and the ~384s/pass persists until they are gone — hours
    either way. Do NOT read this as "resolves on its own" (an earlier version said that, wrongly) nor
    as "stuck until the running jobs finish" (also wrong, and also mine): the correct reading is that
    the only mechanism draining it is the degraded link itself, at roughly one task per pass-and-a-half,
    with no reaper able to help.
    **⇒ ★★★ CONFIRMED BY DIRECT OBSERVATION 2026-07-30 07:37 — and this un-retracts part of an earlier
    retraction.** An operator cancelled the box's three running tasks at 07:25:31-36 (`cancel_requested
... via runq`). They moved `running` -> `cancelling`, and **10d fired 12 minutes later, the instant
    no `running`/`preempting` occupant remained**:

        07:37:41  undeliverable  box=40000017  "1 task(s) claimed > 90min on instance 40000017 with
                                                a recorded ship failure — requeueing at no retry
                                                cost and quarantining the box"
        07:37:46  undeliverable  box=40000017  task ed6ac486-8
                  settings key `ship_quarantine_i40000017` now present

    **It requeued EXACTLY 1 of the box's 5 claims** — `ed6ac486-8`, the only one carrying a per-task
    `ship_failed`. Four claims remained on the now-quarantined box. That is precisely the serial-drain
    interaction first recorded here, and it also confirms the running-occupant guard was the only thing
    holding 10d off.
    **Correcting my own correction, precisely:** the retraction (cdb3f47) was RIGHT that the box was
    slow rather than undeliverable — that stands. But it bundled in a second claim, that "no change to
    [10d's] kill criterion is warranted on this evidence", and that was wrong: it conflated *is the box
    dead?* with *does 10d drain it serially?* Those are independent, and the second is now settled by
    observation. **So the proposed widening — let 10d accept a `ship_failed` for ANY task on that
    instance when the box also has `consecutive_fails > 0` — is now evidence-backed, not speculative.**
    It still needs the test that a slow-but-successful ship, and a box that failed once then recovered,
    stay unaffected.
    **Also still true at that moment:** the ~384s/pass tax was still being charged (07:26:24 pass:
    383s retired; one earlier pass at 07:06:55 hit **555s**), because quarantine stops new *placement*
    but the ship loop still attempts a `live` box's existing claims.

    **⇒ Do not track a live box's drain rate in this spec.** The numbers above are here once, as the
    evidence for the structural gap. The gap — 10d's running-occupant exemption, and 7b bounding
    attempt COUNT but not DURATION — is the durable finding; a particular box's per-hour behaviour is
    not, and chasing it produced four successive amendments to this entry in one night.
    **⇒ And no safe operator action exists:** destroying the box would kill two healthy running jobs,
    and hand-requeueing its claims races the daemon, which is the singleton owner of those transitions.
    The fix has to be in code. Narrowest form: make 10d's running-occupant guard consider whether the
    box can still RECEIVE (e.g. exempt only when `consecutive_fails == 0`), so a box that is computing
    but not accepting deliveries can have its undeliverable claims freed without touching its running
    work — plus 19h's double-run guard applies (clear the spool copy before requeueing).

  - **★ THE DOMINANT COST IS PASS LATENCY, NOT THE HOLD-TIME OR THE BILLING — 7b's credit-back bounds
    the BUDGET but not the PASS (measured 2026-07-30 06:06 and 06:24).** Two consecutive passes:

        06:06:10  ship budget 300s spent after 6 task(s) in 689s (384s of it on boxes 7b retired,
                  not charged) — 25 claimed task(s) deferred
        06:24:08  ship budget 300s spent after 6 task(s) in 706s (383s of it on boxes 7b retired,
                  not charged) — 22 claimed task(s) deferred

    **~384s per pass — MORE than the whole 300s `ship_budget_sec` — went to a retired box's failed
    rsync attempts, and the budget check subtracts it** (`time.time() - started - retired_cost >
    budget`). Crediting it back is right for fairness: healthy tasks must not be charged for a dead
    box. But the consequence is that the pass runs **~690-706s instead of ~300s**, and `poll_once` is
    SERIAL — `_ingest_and_complete`, every checkpoint pull, the whole reaper layer and placement all
    run at HALF RATE fleet-wide, every pass, for as long as the degraded box holds claims. That is
    strictly larger than this box's ~$0.07 of billing or its slow drain, and it is the same
    "one dead box starves the whole fleet" shape 7b was written to stop — surviving in a subtler form,
    because 7b bounds the *number of attempts* (one per box per pass) but not their *duration*.
    **Fix direction this implies (NOT applied):** bound the retired cost as well as credit it —
    e.g. cap total `retired_cost` per pass, or skip a box that has failed N consecutive passes for the
    next M passes (a per-box backoff), so one degraded link cannot set the coordinator's cycle time.
    Note the compile-miss cost is SECONDARY to this and better amortised than it first appears: one
    miss per distinct SHA then hits, and at 06:22 a single SHA covered 15 claimed tasks.

  - **★ STILL UNFIXED 2h50m LATER, AND THE PASS HAS GROWN (measured 2026-07-30 08:54).** The entry
    above ends "Fix direction this implies (NOT applied)". It is still not applied, the same box
    (40000017 / 203.0.113.204) is still the one failing, and the cost has gone UP, not decayed:

        06:06:10  shipped=6   pass=689s  uncharged=384s (55%)
        07:06:55  shipped=6   pass=860s  uncharged=555s (64%)
        08:22:46  shipped=7   pass=790s  uncharged=381s (48%)
        08:54:57  shipped=9   pass=825s  uncharged=382s (46%)

    **9 `ship_failed` to that box since 05:43 — a 3h13m unbroken run**, each `rsync push failed after
    3 attempts`, while its own `box_measured` reads healthy (load 12.91/72 cores, 94.6 GB free). That
    is the "COMPUTES but cannot RECEIVE" state this spec already calls self-sustaining, now with a
    duration on it. The uncharged share is the part that matters and it is structural: **a budget that
    excludes its single largest line item cannot bound anything** — `ship_budget_sec` is nominally
    300s and the pass it is meant to bound runs 689-860s, with 46-64% of that explicitly not charged.
    Per-box backoff (the fix direction above) is what closes it; crediting-back alone cannot.

    ⚠ **CORRECTION (09:50, same author).** The pass/uncharged table above is measured and stands — it
    is read straight from the `ship_budget_spent` detail strings. The DOWNSTREAM claim I first wrote
    under it does not, and I am replacing it rather than editing it quietly:

    - I wrote that `where_ladder_ramp/ladder_ramp_sf629ae` "has been claimed since 04:36 (4h20m)" and
      that a 3-cell read of mine was starved by this mechanism. **The fleet is not ship-starved: 104
      ships in the 60 min to 09:48.** The escape path also fired exactly as designed — 40000017 crossed
      into `dead_worker` at 09:13:12 → `infra_failed` → `requeue` → re-claimed on instance -1 at
      09:13:35 → shipped 09:13:49 → **running** 09:22:05. The 4h20m was real when written but it
      RESOLVED; citing it as evidence of permanent stranding was wrong.
    - **The list I generalised from was truncated by a bug in my own query** — one sqlite cursor reused
      for an outer loop and its inner lookups, which silently ends the outer iteration after the first
      row. Every "N tasks, oldest first" reading I took that way returned exactly one row and I read it
      as the whole set. *A truncated query and a genuinely small result set are indistinguishable in
      the output;* use separate cursors, and print the count next to the list so the two can be told
      apart.

  - **★ THE REAL GAP: `dead_worker_skipped` protects a box from teardown but strands its CLAIMS
    (measured 2026-07-30 09:48).** This is a different box and a different mechanism from the entry
    above. Instance 40000016 holds 2 running tasks and 4 claimed ones and has shipped NOTHING since
    08:09, emitting on every pass:

        dead_worker_skipped — last successful pull was 19min ago (>= 15), our HEARTBEAT copy is not a
                              confirmed fresh read, so st[ay] …

    The guard itself is right, and is the direct descendant of "the reaper destroyed healthy boxes —
    ssh first": do not declare a box dead on a stale read. But the two outcomes are asymmetric. A box
    that crosses into `dead_worker` (40000017) gets torn down and **its claims are freed and requeued**.
    A box that sits just under it in `dead_worker_skipped` (40000016) is neither shipped to nor torn
    down, and **nothing frees its claims** — 19c only requeues on `claimed > 90min` *with a recorded
    ship failure*, and a box that never gets a ship ATTEMPT records no failure to qualify with. So the
    protective branch has **no escape path of its own** — nothing inside it bounds the hold; it ends
    only if the box happens to recover.
    ⚠ **Bounded in practice, at least this once (10:17).** 40000016 came back: it accepted claims at
    10:10, shipped at 10:12:58/10:13:33 and its workers started 10:17:26 — so the hold ran **~2h38m
    and then cleared on its own**. An earlier draft of this entry called it "an indefinite hold with no
    timeout", which overstates the measurement: what is demonstrated is a multi-hour hold with no
    bound *in the code*, not an unbounded one. (Confound, unresolved: two of the four stranded claims
    were cancelled by hand at 09:52, so the recovery is not cleanly attributable to the box alone.)
    The gap is still worth closing — a hold whose only exit is luck is not a design — but it should be
    argued at that width.
    ⚠⚠ **AND `dead_worker_skipped` was NOT what held my tasks — the ship QUEUE was (settled from the
    code, 11:35).** I cited `m50_lang_select_sign`'s cells as the cost of this gap. They are not:
    **seed 2 sat `claimed` for 2h34m on 40000028, a box that never entered `dead_worker_skipped` at
    all**, and was released the instant its snapshot compiled (`compile` miss 158.74s at 10:43:18 →
    `ship` 10:43:28 → `done` 11:20:33). Same wait, healthy box ⇒ box state does not explain it. The
    `dead_worker_skipped` observation above stands on its own; it is simply not the cause of this
    stall, and I attributed it three times before checking. `_ship_all` is explicit:

        for r in registry_db.list_tasks(self.conn, states=["claimed"]):   # priority DESC, created_at ASC

    Ordering is **`priority DESC, created_at ASC`** and is **not compile-aware**, while 7c stops the
    pass once `ship_budget_sec` is spent — and the file's own note says "with several worktrees
    queueing at once nearly every task is a distinct snapshot", each paying a ~155s median cold miss.
    So a claimed task simply waits its turn behind every equal-priority task created before it.

  - **★★ LIVELOCK: the recovery path for an UNREACHABLE box requires REACHING that box — so it never
    completes, and the box is never quarantined either (measured 2026-07-30 20:14, instance 40000020).**
    Distinct from `dead_worker_skipped` above: that box was merely not-fresh, this one's transport is
    broken and the guard that exists for it cannot run. The full cycle, repeating every ~20 min:

        19:49:36  undeliverable         1 task(s) claimed > 90min on 40000020 with a recorded ship
                                        failure — requeueing at no retry cost
        19:49:36  cancel_signal         CANCEL marker written
        19:49:36  undeliverable_defer   box cleanup failed rc=255 — will retry next poll   ← ABORTS
        19:50:21  compile               {"mode":"hit","sec":10.44}                          ← wasted
        19:50:28  ship_failed           rsync push failed after 3 attempts to ssh0.vast.ai:10000
        19:50:28  ship_box_deferred     transport failure — deferring its remaining claimed task(s)
        …and again at 20:09:30 / 20:10:14 / 20:10:21, and again, and again.

    **10d decides correctly and then cannot execute.** Its three jobs are: free the task, quarantine
    the box, and clear the spool copy (the 19h double-run guard). The third ssh's the box, gets rc=255
    — *because broken transport is the very failure being recovered from* — and the abort takes the
    other two with it. Consequences compound:
    1. the tasks are never freed (6 claimed on this box, across **4 groups from different sessions**:
       `m50_lang_family_sign` ×3, `perc_tier_pc`, `m56_causal_vs_champ` ×2);
    2. **the box is never SHIP-QUARANTINED** — `settings` holds `ship_quarantine_i40000013`,
       `_i40000014`, `_i40000017` and NOT `_i40000020` — because `_set_ship_quarantine` lives past the
       abort. `_ship_quarantined` is consulted by `_fits`, so placement still rates the box healthy and
       **keeps packing new claims onto it**. Self-reinforcing, not merely stuck;
    3. each pass pays a compile (~10s) plus 3 rsync attempts per task, forever.
    Contrast 40000017 this morning, which escaped *because its cleanup succeeded* and so got its
    quarantine row at 08:40:31. The difference between recovering and livelocking is whether ssh
    happened to work — i.e. the guard's success is conditional on the fault not being present.
    **Fix direction (NOT applied), and the ordering is the whole point:** free the task and set the
    quarantine FIRST, then attempt cleanup as a best-effort step whose failure is logged and does not
    roll back the other two. The double-run guard's own risk is bounded here — a box we cannot rsync
    to is a box the task cannot be running on — so the ordering that 19h protects against is not the
    ordering that occurs when transport is dead. If cleanup must gate the requeue, gate it on a
    *reachability* check that distinguishes "cleanup failed" from "box unreachable", and treat the
    second as licence to quarantine rather than as a reason to retry.

  - **★ OPERATIONAL: cancel+requeue is STRICTLY HARMFUL to a task that is merely waiting to ship.**
    Learned by doing it. My seeds 0/1 were created 08:03:38/42 and shared snapshot `c9a2d84` with
    seed 2. I read their 1h43m wait as stranding and cancelled+requeued them at 09:52. **51 minutes
    later that shared snapshot took its one cold compile and seed 2 shipped 10 seconds behind it** —
    the two I cancelled would have ridden the same compile as ~10s HITS and shipped in the same pass.
    Instead the replacements got `created_at` 09:52 (**to the back of `created_at ASC`, behind every
    prio-50 task queued in the preceding 1h49m**) and a brand-new snapshot `77a548a1` shared with
    nothing, owing its own cold miss. 82 min later they had no `compile` event at all.
    **Requeueing resets BOTH keys the ship order uses.** The tell that you are about to make this
    mistake: a task `claimed` with no `ship_failed` and no `compile` event is QUEUED, not stuck — the
    only honest levers are `--probe` (priority DESC dominates created_at) or patience. Ordinary
    requeue moves it backwards.
    **Fix direction (NOT applied):** give `dead_worker_skipped` its own escape — either requeue claims
    held on a skipped box past some age REGARDLESS of whether a ship failure was recorded, or refuse to
    CLAIM onto a box whose last successful pull is already stale, so the hold cannot form. The second
    is cheaper and strictly preventative.
  - **The secondary question is whether to reduce the hold-time — and the measured rate says it MATTERS.**
    (This supersedes an earlier line in this same entry claiming the incident "did NOT establish" that;
    I wrote that before the box had been observed long enough, and it was wrong in the *other*
    direction. The measurement below is what should be trusted.)
    **Measured on 40000017:** live 04:32; **in 56 minutes it delivered exactly 1 of its 8 tasks**
    (04:57, which then started at 05:11 and ran normally). The next task failed twice more (05:03,
    05:20) and **7 remained `claimed`** — while the global queue stood at 27 and healthy boxes were
    shipping in ~12s of cache-hit compile plus a fast rsync. One delivery is too few to fit a rate,
    so do NOT quote an ETA (I quoted 2.2h, then 15-90 min, and both were wrong); quote the rate that
    was observed: **~1 delivery per ~30 min against 7 waiting.**
    So a degraded box holds a real block of work at low throughput, and its slots are counted as used
    capacity, which pushes the fleet to rent for the backlog behind them.
    **Still deliberately not designed here**, because the shape of the fix is genuinely unobvious:
    releasing a box's *unattempted* claimed tasks back to the queue trades a slow-but-certain delivery
    for a re-placement, and it interacts with 19h's double-run guard (the box may still hold a spool
    copy, and a link that recovers later must not launch a task already given to someone else — the
    same hazard 10d handles by clearing the spool copy BEFORE requeueing). Needs an owner decision plus
    a test that a recovering box cannot double-run.

- **The compile cache key is REPO-WIDE while only two packages are compiled (measured 2026-07-30;
  needs an owner decision because the safe direction is not obvious).** `_compiled_tree` keys on
  `_compile_cache_key(cache_key, abi, packages)` where `cache_key` is the snapshot's `code_hash` — a
  sha256 over the **whole-repo file manifest** (`git ls-files -co`, code-snapshot inv. 1) — or
  `git_sha` on the archive fallback. But the compiler only ever reads
  `bundle_compile_packages` (`src/native,src/shared`). **So an edit to `spec/`, `tests/`,
  a notes file, or `dispatcher.py` itself invalidates the compile cache and forces a full cold
  rebuild that could not have produced different binaries.** Measured cost of that over-invalidation:
  n=74 misses at a median **173.8s** (p90 225.6s) vs n=542 hits at 10.7s — **220 min of coordinator
  wall-clock in ~1.5 days**, each miss consuming 58% of the 300s `ship_budget_sec` and capping the
  pass at ~3 shipped tasks. It bites hardest exactly when several worktrees are active, since each
  session's own tree is a distinct key.
  **⇒ THE COST IS NOT JUST COORDINATOR SECONDS — IT IDLES OWNED HARDWARE.** Observed 2026-07-30 06:30:
  `laptop-gpu` (32 cores / 12GB, costing nothing to run) had **3 occupants, ALL `claimed`, NONE
  running** — waiting 14-32 min purely for ship budget to reach them — while **40 tasks were queued**
  and 28 were `claimed` fleet-wide. No reaper applies or should (`claim_timeout_min` governs
  `shipped`->worker-claim, not `claimed`->ship), so this state is invisible to every existing guard.
  That makes candidate **(a) — stop charging a cold compile against `ship_budget_sec`** — the
  strongest first move: scheduling-only, zero correctness risk, and it converts compile time straight
  back into delivered work on hardware the fleet already owns.
  **Why this is NOT a free win, and why it is not being changed unilaterally:** narrowing the key
  means enumerating every input that can change the compiled output — `.py` under the compiled
  packages, `.pxd`/`.pyx`, `setup.py`/`pyproject.toml`, Cython directives, the `.cybuild` recipe,
  the ABI, the package list — and **a key that misses one of those silently ships STALE BINARIES**,
  which is the worst failure class this fleet has (it looks like a working run and a wrong number).
  Candidate directions, cheapest-risk first: (a) leave the key alone and stop charging a cold
  compile against `ship_budget_sec` (scheduling-only change, zero correctness risk, does not reduce
  compile work); (b) pre-warm the cache asynchronously on merge so the miss never lands in a ship
  pass; (c) narrow the key to a manifest hash over the compiled subtrees + build recipe (biggest win,
  needs the enumeration above plus a test that a change to each enumerated input DOES bust the key).
  Owner picks; (a) and (b) are additive and could ship before (c) is designed.
  **(d) proposed 2026-07-31 — `docs/specs/ship-artifact-build.spec.md`: move the build OUT of the
  coordinator entirely.** The queuer (`runq`) compiles at `runq add` time and stores a ship-ready
  blob; the coordinator becomes blind to compilation and only rsyncs it. This SUBSUMES (a) and (b) —
  a build at queue time is never in a ship pass and is pre-warmed by construction — and it makes (c)
  optional rather than load-bearing, which matters because (c) is the direction that risks silently
  shipping stale binaries. Draft, one blocking Open question (the toolchain-unavailable fallback).

None blocking — the money knobs (spec/DECISIONS.md Blocking #1) were resolved by the owner
2026-07-08; final values are in the Input contract's Settings list above. The compile cache-key
question above is non-blocking too: today's behavior is correct, only slow.

Resolved at draft/approval time (owner may veto):
- **Devcontainer autostart** → yes, VS Code task next to TensorBoard/dashboard (it is a no-op
  loop when the queue is empty and rate is 0).
- **Per-task pip extras** → installed at task start on the box (not baked at rent) — keeps rent
  generic so heterogeneous tasks can share a box.
- **Manual lane** → `watch_and_pull.sh` remains untouched; the cwm-era `launch_sweep.sh` /
  `launch_probe.sh` were retired with the cwm track (2026-07-07; archived + in git history) —
  ad-hoc native launchers (the `launch_m11_atari.sh` pattern) are the manual lane now.
  `watch_and_pull.sh` deprecation is a separate decision after the dispatcher has survived a
  real campaign (plan 0004).
- **Slot sizing** → derived per-instance from the rented offer's hardware (invariant 4a'), not a
  flat setting; a task may override the per-lane footprint via `resource_hint` for a task class
  that doesn't match the MinAtar-derived global default (e.g. the CPU-core-bound M11
  pixel-Atari lane, `MIN_CORES=16` in `launch_m11_atari.sh`).
- **Rent patience** → replaced a flat timer with the backlog-bar rule (invariant 4c) plus
  priority preemption/bypass (invariants 4d, 17) — 2026-07-08 owner decision, see conversation
  history; this is a genuine behavior change from the original OQ6, not just a number.
- **Preemption scope** → deliberately narrow: this targets the real failure mode this codebase
  actually has (a `lost` on-demand instance, or a deliberate priority-based eviction), NOT Vast's
  interruptible/spot-bid instance type. Actually renting bid-based capacity is explicitly OUT of
  scope here — it would need its own money knob (bid price) and its own eviction-callback
  handling that Vast's interruptible offers use, neither of which is designed yet. Revisit as a
  separate spec if spend pressure justifies the ~45-47% discount (VAST-TEST.md).
- **Retry budget scope** → owner directive 2026-07-09: `max_retries` default raised 1 → 3, infra
  failures cost half a retry each (invariant 10). `task_failed` stays permanently terminal —
  explicitly confirmed, not just left alone by default — because a science failure is unlikely to
  be fixed by an identical retry, so the fastest signal to the starter is immediate, hard failure.
- **Stalled-running detection** → owner directive 2026-07-09, following the "48h cap catches
  extreme duration but not extreme idleness" utilization audit: see invariant 19.
