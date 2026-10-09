# Feature: Pause / drain an owned box (laptop) without wedging the fleet

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing.

- **Owning module:** `fleet` (fleet coordinator)
- **Module path:** `fleet/{dispatcher.py, spool_worker.py, box_pause.py}`
- **Status:** approved <!-- draft → approved → built -->
- **Spec file:** `docs/specs/box-pause.spec.md`
- **Related:** `docs/specs/task-dispatcher.spec.md` (invariants 4/8/17/19/20), `run-registry.spec.md`

## Purpose
Give the operator a one-command way to quiet CPU/GPU activity on an **owned** box (the home
`laptop-gpu`, `instances.id=-1`, `source='owned'`) without losing training progress or wedging the
rest of the fleet. Two modes matching two real situations:

- **`pause` (soft / "back soon"):** freeze the box's running trainers in place (`SIGSTOP` — GPU/CPU
  instantly idle, VRAM retained, zero lost work, resumes on `SIGCONT`) and stop packing new work
  onto it. Do **not** requeue the frozen work — unless the operator forgets: after
  `soft_pause_timeout_min` (default 30) the pause auto-escalates to a hard drain so the jobs move
  elsewhere rather than sitting frozen forever.
- **`drain` (hard / "long wait"):** gracefully stop the box's work — each task checkpoints then
  exits (the existing PREEMPT path, typically ≤5 min) and requeues on the fleet — then **hold**: no
  new work lands on the box until an explicit `resume`.

`resume` returns the box to normal (`live`, unfrozen). None of this touches Vast rentals; the rest
of the fleet keeps running.

## Input contract
- **CLI (trust boundary):** `python fleet/box_pause.py {pause|drain|resume|status} [--label L]`.
  `--label` selects the owned box; omitted → the sole `source='owned'` row (error if 0 or >1 and no
  `--label`). Validates the box exists and is `source='owned'` before acting.
- **Coordinator state (internal):** the pause is expressed entirely in the shared registry DB
  (`experiments/runs.sqlite`, `registry_db.shared_experiments_root()`):
  - `instances.state = 'paused'` — a NEW instance-state string value. Instance `state` is free-form
    TEXT (only *task* states are enum-enforced via `LEGAL_TRANSITIONS`), so this needs **no schema
    version bump**.
  - `settings['pause_i<ID>']` — JSON `{"mode": "soft"|"hard", "at": <ISO>, "escalated_at"?: <ISO>}`.
    Per-box key, mirroring the existing `overpack_cap_i<ID>` convention. No new column.
- **Box marker (internal):** `~/spool/FREEZE` on the box (a marker file, delivered/removed over the
  same ssh channel as `PREEMPT`/`CANCEL`) tells `spool_worker.py` to freeze/unfreeze its trainers.

## Output contract
- CLI prints the resulting box state, mode, elapsed time, and occupant tasks; exit 0 on success,
  non-zero on validation failure. `status` is read-only.
- All persistent effects are DB rows + `events` log rows (`pause`, `drain`, `resume`,
  `pause_timeout`, `freeze`/`unfreeze`) + the `~/spool/FREEZE` marker. No new file formats.

## Public API
Module-internal to `fleet`. New surface:
- `dispatcher.py`: `Dispatcher._signal_drain()`, `Dispatcher._reap_paused_soft_timeout()`,
  `Dispatcher._pause_meta(iid)`, `Dispatcher._set_pause_meta(iid, meta)`, `Dispatcher._clear_pause_meta(iid)`,
  new setting `soft_pause_timeout_min` (default 30).
- `spool_worker.py`: `Worker.apply_freeze()`, `ActiveTask.stopped: bool`.
- `box_pause.py`: `main(argv)` CLI.
- `Makefile`: `pause`, `drain`, `resume`, `pause-status` targets.

## Dependencies
- `registry_db` (connect, transition, log_event, now_iso, settings table).
- `dispatcher` helpers: `endpoint_for`/`ssh_run`/`ConnectionTracker` (CLI reaches the box the same
  way the daemon does), `_infra_fail`, `DEFAULT_SETTINGS`.
- `spool_worker` existing PREEMPT/CANCEL marker protocol and `should_launch` gate.

## Behavior & invariants
Numbered, testable acceptance criteria.

**Admission (no new work while paused)**
1. `place()` already admits only `state=='live'` instances, so a `paused` box is never a pack/preempt
   target. No change to `place()`. (Test: a `paused` owned box is not selected even though its $0
   marginal cost would otherwise win.)

**Reaper carve-outs (a paused box must not be auto-clobbered)**
2. `_reap_unreachable_owned` quarantine (`state='live'`) and recovery (`state='unreachable'`) both
   skip `paused` — a paused box is neither quarantined nor auto-flipped back to `live`. (No change
   needed; assert it.)
3. `_reap_orphaned_tasks` treats `paused` as live-ish: a task in an on-box state on a paused box is
   **not** requeued as orphaned. (Change: add `'paused'` to the live-ish set.)
4. `_reap_stalled` skips tasks whose instance is `paused` — a frozen (soft) or draining (hard) task
   is governed by the soft-timeout watchdog / preempt path, not the 90-min stall clock.
5. `_reap_dead_workers` only reaps `state='live'` boxes; a paused box's tasks are never
   heartbeat-reaped. (No change; assert it. This is what makes a soft "closed the lid" pause NOT
   requeue at 5 min — only the 30-min watchdog decides.)
6. `_teardown_idle`/`should_teardown`/`do_reconcile` never destroy or restate an owned box (existing
   `source='owned'` carve-outs); a `paused` owned box is still `source='owned'` → untouched.

**Ingest (drain must be able to complete)**
7. The `_ingest_and_complete` pull loop pulls `state IN ('live','paused')` (not just `'live'`), so a
   draining box's PREEMPTED markers + checkpoints are ingested and its `ConnectionTracker` fail
   count stays current. Pulling a soft-frozen box is harmless (keeps HEARTBEAT fresh, no ckpt
   change).

**Soft pause**
8. `box_pause.py pause`: set `state='paused'`, `pause_i<ID> = {mode:soft, at:now}`, and `touch
   ~/spool/FREEZE`. The worker freezes within one worker poll (~2s).
9. `apply_freeze()` in the worker: while `~/spool/FREEZE` exists, every live trainer proc is
   `SIGSTOP`'d as a **process group** (trainer launched with `start_new_session=True`), EXCEPT a
   task carrying a `PREEMPT` or `CANCEL` marker — that one is kept/put running (`SIGCONT`) so it can
   still checkpoint-and-exit or be killed. When `FREEZE` is removed, all stopped procs `SIGCONT`.
   `launch_ready` does not start new tasks while `FREEZE` exists.
10. Soft watchdog `_reap_paused_soft_timeout` (each poll): a `paused` box in mode `soft` whose `at`
    is older than `soft_pause_timeout_min` is escalated to mode `hard` (log `pause_timeout`); the
    drain path then evicts its work. A soft pause under the timeout is left completely alone.

**Hard drain**
11. `box_pause.py drain`: set `state='paused'`, `pause_i<ID> = {mode:hard, at:now}`, and `rm -f
    ~/spool/FREEZE` (a drain runs to checkpoint, it does not freeze).
12. `_signal_drain` (each poll): for every `paused` box in mode `hard` — **reachable**
    (`consecutive_fails < owned_unreachable_fails`): transition each **`running`** occupant to
    `preempting` and `touch …/PREEMPT` **exactly once, at that transition** (graceful
    checkpoint-then-exit, requeue at no retry cost via the existing preempt path); also `rm -f
    ~/spool/FREEZE`. An already-`preempting` occupant is left alone — its marker must NOT be
    re-touched, or the marker mtime stays fresher than every checkpoint and the box-side kill guard
    (invariant 17b, `ckpt newer than marker`) can never fire (a slow-checkpointing trainer would run
    undrained forever — found live 2026-07-22). **Unreachable** (`>=` threshold, e.g. laptop asleep):
    `_infra_fail` each occupant (requeue at half a retry), mirroring the owned-unreachable
    quarantine's tradeoff. After eviction the box holds `paused` with no occupants until `resume`.
12a. **Drain latency = the trainer's checkpoint interval.** Because the drain is graceful (never a
    hard kill before a checkpoint, invariant 17b), a task only leaves at its *next* checkpoint. A
    trainer that checkpoints every ~25 min takes ~25 min to drain — the "next checkpoint, up to 5
    minutes" expectation holds only for trainers that checkpoint that often. (Open question: an
    optional `drain_max_wait_min` force-requeue backstop that bounds drain time at the cost of the
    progress since the last *pulled* checkpoint.)
12b. **A drain evicts UNDELIVERED work too (`claimed`/`shipped`), not just `running`.** This is the
    hole that stranded work for 9h on 2026-08-12 (see below); invariant 12 above reads "occupant"
    but the implementation scoped it to `running`/`preempting`, which is the ONLY on-box pair with a
    process to PREEMPT. A task that never got a process cannot be evicted by a marker, so it is
    **requeued directly**:
      * **Reachable** — `rm -rf ~/spool/incoming/<id> ~/spool/active/<id>` FIRST (the double-run
        guard invariants 10b/10d/19h already use: a hard-paused box's worker is *not* frozen, so a
        `shipped` task it still holds would launch work we have handed to someone else), then
        transition `claimed`/`shipped` → `queued` with `instance_id=NULL` at **no retry cost** — a
        drain is the operator emptying the box, so nothing on it is the task's fault. A cleanup that
        fails leaves that task alone and retries next poll (never requeue what we could not clear).
      * **Unreachable** (`>=` threshold) — `_infra_fail` (half a retry), the same tradeoff the
        unreachable branch of invariant 12 and the owned-unreachable quarantine (20h) already make.
    The `running`/`preempting` half of invariant 12 is unchanged, and the two halves are disjoint by
    state, so a box with both kinds of occupant drains both in one poll.
12c. **Why nothing else catches it** — the reaper matrix has no other row that can fire, which is
    what made the strand permanent rather than slow. For a task `claimed` on a `paused` owned box:
    `_reap_orphaned_tasks` treats `paused` as live-ish **by invariant 3**, deliberately deferring to
    "the soft-timeout watchdog / drain path"; `_reap_unreachable_owned` (20h) scans `state='live'`
    only; `_reap_undeliverable_claims` (10d) and `_reap_unclaimed_ships` (10b) scan `state='live'`
    only; `_reap_dead_workers` (10c) needs a live box with a heartbeat; `_reap_stalled` skips paused
    **by invariant 4**. So invariant 3's carve-out is only sound *because* 12b exists to own the
    case it defers — the two must be read together, and neither may be narrowed alone.
    **Measured, 2026-08-12:** the laptop (`-1`) went unreachable at 04:49 and 20h correctly
    quarantined it; its share of the fleet then packed onto the tower (`-3`), whose spool disk
    hit 100% (98G/98G) at ~05:04, so every ship rsync failed. Three `pcbed_depth` cells claimed at
    05:04 never shipped; the box was hard-drained at 05:45; `_signal_drain` saw zero `running`
    occupants, `continue`d, and the three sat `claimed` for **9 hours** until a human looked. A
    fourth (`retbootstrap_n3/critic_s12b`) was cancelled by hand at 14:05 with the note "ORPHANED
    CLAIM … has sat in 'claimed' for 8h+ and is not re-shipped" — a human doing 12b's job manually.
12d. **A `paused` box with no (or malformed) `pause_i<ID>` meta is left to a human, and that is
    deliberate** — `_signal_drain` requires mode `hard` and `_reap_paused_soft_timeout` requires mode
    `soft`, so neither fires. Escalating an unreadable pause record would mis-evict frozen work,
    which is the exact failure invariant 3's carve-out exists to prevent. Known, bounded, and NOT
    closed by 12b. (Open question: a `pause_orphaned` log line each poll so it is at least visible.)

**Resume**
13. `box_pause.py resume`: set `state='live'`, delete `pause_i<ID>`, `rm -f ~/spool/FREEZE`. The
    worker `SIGCONT`s any still-frozen procs on its next poll; the dispatcher packs the box again.
14. `resume` is idempotent and safe on an already-`live` box (no-op flip + best-effort `rm -f`).
14a. **⛔ `~/spool/FREEZE` is DERIVED state, and the coordinator re-asserts it** (2026-10-03).
    The marker is the registry's `paused`+`soft` projected onto the box, but it was only ever
    written or removed ONCE, by the verb that changed the state, over a best-effort ssh whose
    failure `resume` discarded. A `resume` whose ssh did not land therefore left a box the
    registry called `live` and the packer filled, that started nothing: the worker returns from
    its launch gate on `FREEZE` BEFORE it logs a gate reason, so there is no `launch_gate` line,
    no event, and — once nothing is running — no reaper that owns the `shipped` tasks. Re-pointing
    a soft-paused box by re-registration reached the same state.
    So every resource-measure probe of a `live` or `paused` box (`_measure_box_resources`, each
    `resource_measure_every_min`) carries, in the SAME ssh call and ahead of the probe, the assert
    for that box's desired state: **the marker exists iff `state='paused'` and `pause_i<ID>.mode`
    is `soft`** — `touch` it then, `rm -f` it otherwise (a `hold`, a `hard` drain, `live`). The
    command first reports whether the marker was there (`FRZ 0|1`); when that disagrees with the
    desired state the coordinator logs one **`freeze_reconciled`** event naming what it found and
    what it did. No extra connection, no per-box memory: the assert is idempotent and a box that
    could not be reached is simply asserted on the next probe. Rentals get the same assert (always
    "absent"), so the rule has no carve-out.
    **Testable:** a `live` box whose marker is present ⇒ the probe command removes it and one
    `freeze_reconciled` is logged; a soft-paused box whose marker is absent ⇒ it is created and
    logged; a `hold` or `hard` box asserts absent; a box already in its desired state logs nothing;
    the assert rides the probe's own ssh call (one call per box per cadence).

**Durability / safety**
15. Every decision is re-derivable from the DB after a dispatcher restart (no in-memory pause
    timer): the 30-min clock reads `pause_i<ID>.at` (wall-clock ISO), consistent with invariant 1.
16. Deployment order matters: the `paused` state + reaper carve-outs must be **live in the daemon
    before** the first `pause` is issued (a pre-carve-out daemon's orphan reaper would requeue frozen
    tasks). Merge → `make dispatch-restart` → then `make pause`. Documented in `docs/operations.md`.

## Fixtures
Golden DB-state tests (mirroring `TestReapUnreachableOwned` in `tests/test_dispatcher.py`):
- `paused` owned box is not a placement target (invariant 1).
- orphan reaper keeps a running task on a `paused` box (invariant 3); stall reaper skips it (4).
- soft→hard escalation after the timeout, no-op under it (invariant 10).
- `_signal_drain`: reachable → task `preempting` + a `PREEMPT` marker ssh'd; unreachable → task
  `queued` (requeued) at `retries_used==0.5` (invariant 12).
- `_signal_drain` on UNDELIVERED work (invariant 12b), the 2026-08-12 regression:
  - a `claimed` task on a reachable hard-drained box with NO running occupant → `queued`,
    `instance_id IS NULL`, `retries_used == 0` (no retry cost), and a `rm -rf ~/spool/incoming/<id>`
    issued BEFORE the requeue. The "no running occupant" part is the regression: the old code
    `continue`d on an empty `running` set and did nothing at all.
  - a `shipped` task, same box → same requeue, same pre-clear (the double-run guard).
  - cleanup failure (ssh rc != 0) → the task stays `claimed` and is retried next poll, never
    requeued behind a spool copy we could not delete.
  - unreachable + `claimed` → `_infra_fail`: `queued` at `retries_used == 0.5`, no ssh attempted.
  - a soft-paused box's `claimed` task is untouched (mode gate still governs).
  - mixed box (one `running` + one `claimed`) drains BOTH in a single poll — the two halves are
    disjoint by state, not alternatives.
- CLI `pause`/`drain`/`resume` flip `instances.state` + `pause_i<ID>` as specified (mocked ssh).
- Worker `apply_freeze` against a real `start_new_session` subprocess: `SIGSTOP` → state `T`
  (stopped), `SIGCONT` → running; a `PREEMPT`-marked task is exempt.

## Capacity schedule (extension — resource caps → inferred slots, by time of day)
Same machinery as pause/drain, generalized from "0 or full" to a scheduled cap.
17. **Schedule config** `configs/capacity/<label>.json` (`capacity.py` schema): `tz` (IANA), `cores`,
    `vram_gb`, and `windows[]` of `{from,to,cpu,vram}` cap FRACTIONS tiling 24h (a window may wrap
    midnight), plus an optional per-window `gpu_power` (inv. 20a). Malformed → logged, treated as uncapped (never wedges a poll). Optional
    `cores_per_lane` / `vram_per_lane_gb` declare the box's REAL per-lane footprint for inv. 18;
    absent → the global settings defaults.
18. **Inferred slots.** `effective_slots = floor(min(cpu·cores/cores_per_lane,
    vram·vram_gb/vram_per_lane_gb))` for the current window (in `tz`). The coordinator caps a box's
    `slots_total` by this in `_instances_view` (min with `overpack_cap`), so placement tracks the cap.
    **The lane footprint comes from the SCHEDULE when it declares one**, not the global settings
    default — a lane is only a meaningful unit of a CPU/VRAM budget if it is the size the box's
    actual tasks occupy. Dividing a real budget by a fictional lane size is what invariant 18a
    exists to stop (live incident 2026-07-27: `desktop`'s day window budgets 10 cores / 6 GB, but
    the settings lane of `1 core / 0.6 GB` inferred **10 slots** while every task in flight hinted
    `4 cores / 2.0 GB` — advertising 5× the honest lane count, i.e. up to **40 cores demanded on a
    20-core box**, 4× its own declared budget and 2× the physical hardware).
18a. **Budget admission** (`_fits_now`, placement). A slot count is a single scalar and so cannot
    stay honest across a heterogeneous hint mix; it is also snapshotted once per poll
    (`_place_queue` reads `_instances_view()` before the loop), so a cap expressed only as slots
    can be spent entirely within ONE pass. So, in addition to the slot check, a box carrying a
    capacity schedule admits a task only if the window's ABSOLUTE budget still holds it:
    `Σ(occupant cores) + task cores ≤ cpu·cores` and likewise for `vram·vram_gb`, where a task's
    footprint is `resource_hint.cores_per_lane × slots` (settings default when it declares no
    hint). **The VRAM half charges GPU users only** (task-dispatcher inv. 26m, 2026-10-03): a
    task that neither declares the GPU nor has been measured using it has a VRAM footprint of 0,
    so it spends none of the window's VRAM and is not checked against it — a VRAM budget already
    spent by GPU occupants still admits CPU-only work.
    Occupants reserved earlier in the same pass count (`_apply_placement` already appends
    them to the in-memory view), so the budget binds within a pass as well as across polls. A box
    with no schedule is unaffected. This is the invariant that actually bounds CPU demand; 18 is
    the coarse cap that additionally propagates to consolidation. (It no longer reaches the box:
    the worker holds no lane count — task-dispatcher invariant 8a, 2026-10-03.)
19. **Graceful scale-down** (`_reap_over_capacity`, each poll): a live owned box with more `running`
    tasks than `effective_slots` — **or whose running tasks' summed hinted cores/VRAM exceed the
    window budget (18a)** — evicts the excess newest-first via the drain preempt path
    (checkpoint→exit→requeue). `running→preempting` is immediate, so the next poll won't over-evict.
    Pause (`state='paused'`) is the cap-0 case, handled by `_signal_drain` (inv. 12); the two never
    both act on one box (paused ≠ live).
    **A FORCED occupant (task-dispatcher inv. 4i) is outside this arithmetic**, whatever
    `preempt_enabled` says: it is never shed, and its footprint is not charged either, so its
    arrival never pushes the tasks already running over the cap. The others are kept oldest-first
    against the window exactly as if it were absent. A hard pause still evicts it (inv. 12).
23. **Measured headroom is the real admission gate** (`box_headroom` / `_headroom_fits`, owner
    directive 2026-07-28: *"how much headroom do we have on CPU / RAM / GPU / VRAM? if we have it we
    have space to schedule more work — slots is pretty naive"*).
    a. *Why.* Slots, lane footprints and `resource_hint`s are all DECLARED numbers, and this repo has
       now been burned by each of them: hints ran ~3x over on a GPU workload and ~1000x over on a
       CPU-bound one (inv. 22), and a hand-set `cores_per_lane` calibrated from the heaviest job
       class throttled the desktop to 2 slots at ~20% CPU / 10% GPU with work queued (07-27→28).
       Declared numbers cannot track a change of job class; measurement can.
    b. *Probe* (`BOX_PROBE_CMD`, one ssh per box per `resource_measure_every_min`, extending inv.
       22's nvidia-smi call): `nproc`, `/proc/loadavg`, `free -m`, and `nvidia-smi`. `parse_box_probe`
       is pure and returns `{cores, load1, ram_total_gb, ram_avail_gb, vram_total_gb, vram_used_gb,
       gpu_util}`. **GPU is independent of CPU/RAM**: a `NOGPU`/blocked/unparseable GPU line still
       yields a usable CPU/RAM measurement, because an absent GPU must not cost CPU admission (the
       desktop's GPU was NVML-blocked for a day while its CPUs were perfectly schedulable).
    c. *Headroom* = `min(hardware, operator allowance) − measured usage − pending − reserve`, per
       axis. The allowance is the capacity window's `cpu`/`vram` fraction (inv. 17), so **TOTAL**
       observed load counts against it, the owner's own processes included — when the human is
       working the fleet's headroom genuinely shrinks, which no slot count can express. `pending` is
       the declared footprint of occupants a sample cannot yet see (`claimed`/`shipped`/`reserved`,
       incl. same-pass reservations); a `running` occupant is already inside the sample and must not
       be double-charged. Reserves (`headroom_cpu_reserve` 1.0 core, `headroom_ram_reserve_gb` 2.0,
       `headroom_vram_reserve_gb` 1.0) keep a poll from scheduling a box to its exact edge.
    d. *Gate.* `_fits_now` admits only if headroom holds the task's declared footprint on every
       measured axis (RAM via `ram_per_lane_gb`, the axis hints almost never declare). **Abstains
       rather than blocks on absent data**: no measurement, a sample older than
       `headroom_max_stale_min` (15), or an unmeasured axis → that check passes, exactly the
       best-effort convention of inv. 21c/22. `headroom_enabled=false` disables it entirely.
       **The VRAM axis applies only to a task with a VRAM footprint** (task-dispatcher inv. 26m):
       c's "the owner's own processes included" is what a GPU user must fit beside, and it is no
       reason to refuse a task that never touches the card. Until 2026-10-03 a card the owner had
       filled refused every task on the box, CPU-only ones included.
    e. *Observability.* Every measurement logs a `box_measured` event (load/cores, free RAM, VRAM +
       GPU util). Under-packing is otherwise invisible — that is precisely how the 07-27 throttle
       went unnoticed until the owner looked at the box. Since **task-dispatcher invariant 25** the
       event also carries a JSON tail with the CONTAINER-true axes (cgroup CPU quota + measured
       cores burned, memory limit/current/anon) and the box's occupancy at sample time, which the
       dashboard's fleet-performance panel reads (run-dashboard spec §20).
       ⚠ **The four axes THIS invariant gates on remain HOST-basis and are deliberately unchanged**
       — on a shared rental `nproc`/`loadavg`/`free` describe the whole machine, so `cores` can
       exceed what our cgroup actually allows (measured: 96 vs 18.43) and `load1` includes other
       tenants. Inv. 24 is observability only; re-pointing these at the cgroup would re-tune live
       admission and is a separate, deliberate decision.
    f. *Relationship to the other bounds.* Headroom is the tightest, most honest constraint and is
       meant to BIND; the slot cap (18) and the declared-footprint budget (18a) remain as cheap
       ceilings that work with no measurement at all. Consequently an owned box should NOT hand-set
       `cores_per_lane`/`vram_per_lane_gb` — both shipped schedules stopped doing so on 07-28.
20. **Hard CPU cap** is enforced host-side, NOT by the coordinator: `box_capacity_apply.py` (cron on
    the box's Docker host) reads the same schedule and runs `docker update --cpus <cores·cpu>` — a
    cgroup limit training can't exceed by any thread count. GPU VRAM is bounded by the inferred slot
    count (Docker can't cap VRAM); a GPU compute-% cap requires NVIDIA MPS (out of scope here). Both
    the coordinator and the enforcer read the one schedule file, so they always agree.
    ⚠ **Not deployed on either owned box** (verified 2026-07-27): the endpoint the coordinator
    reaches on each is a container with neither `docker` nor `crontab`, so no cgroup cap is applied
    and nothing constrains a thread count host-side. Until an enforcer runs where it can actually
    call `docker update`, invariants 18/18a are the ONLY bound on an owned box's CPU demand — which
    is why the admission check may not be weakened to an advisory. **Closed for new boxes by
    20b/20c**: the coordinator now pushes the schedule to the host, where the enforcer runs.
20a. **Hard GPU cap — `gpu_power`** (owner request 2026-09-25: *"configure max GPU usage during off
    hours and on hours from this fleet coordinator"*). A window MAY carry `gpu_power`, a fraction in
    `(0, 1]` of the card's DEFAULT power limit. The host enforcer applies it with `nvidia-smi -pl`,
    clamped to the card's `[min, max]` limit (a card's floor is typically 30–50% of default, so this
    throttles; it cannot stop the GPU — `vram: 0` or `box_pause.py` does that). A window without
    `gpu_power`, or no schedule at all, RESTORES the default limit, so deleting the key is the undo.
    It is a hard cap in the same sense as inv. 20: no task can exceed it. The coordinator does not
    read it — packing is still bounded by `vram` (18/18a/23). A card that reports no settable limit
    (`[N/A]`, common on laptop GPUs) is logged and skipped, never an error.
20b. **Schedule delivery — the coordinator PUSHES it** (`_push_capacity_schedules`, each poll). The
    gap inv. 20's ⚠ records is that the host never had the schedule. So for every live/paused owned
    box the coordinator writes its schedule (keys starting `_` stripped) to
    `~/fleet_host/capacity.json` inside the worker container over the ssh it already holds —
    atomically (tmp + `mv`). It pushes on CHANGE, and
    re-pushes every `capacity_push_every_min` (30) so a recreated container is healed. A failed push
    is retried next poll and never wedges it. `~/fleet_host` is a bind mount of the host's
    `/var/lib/fleet-worker/control` on boxes built by `owned_box_setup.sh`; on older boxes it is a
    plain container dir and the push is inert.
    **20b-1. ⛔ A box with NO schedule is pushed an explicit FULLY-OPEN one — never a missing
    file** (2026-10-03). The file used to be `rm`'d, on the reading that the enforcer treats
    absence as "uncapped". It could not act on that: it lifted the CPU cap with `docker update
    --cpus 0`, and the Docker daemon reads a zero there as "field not supplied" and leaves the
    existing limit in place. So a box whose schedule was REMOVED kept its last window's cap
    forever, silently — the update exits 0 every minute. Measured on `desktop` the evening its
    schedule was deleted to make it fleet-only: eleven minutes later the container still read
    `cpu 9.82/10.00 cores`, eleven tasks sharing ten. So for a schedule-less box the coordinator
    pushes `capacity.fully_open(cores)` — the two all-day `cpu 1.0` windows of 20e, sized from the
    box's MEASURED core count — which every enforcer already installed turns into `--cpus <all
    cores>`. A box not yet measured is skipped until it is; nothing is ever removed. The
    coordinator's own gates are untouched: a box with no configured schedule still has no
    `resource_cap`. **Testable:** no schedule + a measurement ⇒ the pushed payload is all-day
    `cpu 1.0` with that core count and no `gpu_power`; no schedule + no measurement ⇒ no push at
    all, and no `rm`; a configured schedule is pushed exactly as before.
20c. **Host enforcer** — installed by `fleet/owned_box_setup.sh` as a systemd timer (1 min):
    `box_capacity_apply.py --uncapped-if-missing` against that file. It evaluates the FULL schedule
    locally, so window boundaries are honoured even while the coordinator is down. Missing file →
    uncapped: the CPU cap is set to every core the host has (`--cpus <cpu_count>` — NOT `--cpus 0`,
    which Docker ignores, 20b-1) and the GPU returns to its default power limit. Malformed file →
    exit non-zero, caps unchanged. An enforcer installed before 2026-10-03 still carries the
    `--cpus 0` no-op, which is why 20b-1 never relies on this path.
20d. **Trust.** The control file is writable from inside the container, so the host treats it as
    DATA only: parsed by `capacity.load`, every cap clamped to the hardware, and the target container
    fixed by the host's own unit file. The worst a task in the container can do is lift its OWN box's
    throttle to the hardware maximum — no code path runs anything it wrote.
20e. **Host caps LIFT while a forced task occupies the box** (task-dispatcher inv. 4i, owner
    directive 2026-10-02). Admitting a forced task past the coordinator's gates is not enough: the
    host enforcer would still hold the container at the day window's `--cpus` and the card at its
    `gpu_power`, so the job would run throttled. So:
    a. *What is pushed.* While ≥1 forced task occupies an owned box — `claimed`, `shipped`,
       `running` or `preempting`, the same occupant set `_instances_view` uses — inv. 20b pushes
       `capacity.uncapped(schedule)` instead of the configured schedule: the same `tz` / `cores` /
       `vram_gb`, and two windows tiling the day (`00:00–12:00`, `12:00–00:00`) at `cpu: 1.0`,
       `vram: 1.0` and **no** `gpu_power`. It is an ordinary schedule, so the enforcer already
       installed on a box understands it — nothing on the host is re-installed — and it deliberately
       does not rely on a missing file (a host run without `--uncapped-if-missing` treats that as an
       error and leaves the old caps in place). A box with no configured schedule has no host cap to
       lift, and nothing changes for it.
    b. *When.* The override counts as a content change, so it is pushed on the transition rather
       than at the next `capacity_push_every_min`; and `_place_queue` re-runs the push in the SAME
       poll that claims a forced task, before the ship, since the push phase precedes placement in
       the cycle. The configured schedule is pushed back in the first poll that finds no forced
       occupant. The host timer is 1 minute, so either direction lands within about a minute of
       the push.
    c. *The coordinator's own caps do not move.* `_capacity_slots` / `_capacity_budget` /
       `box_headroom` keep reading the CONFIGURED schedule, so every OTHER task is still admitted
       against the day window (4i-4). Only the host's hard caps lift, and only for as long as the
       forced task is there.
    d. *Audit, and restart-safety.* The override is recorded as `capacity_override_i<ID>` in the
       settings kv (with `pause_i<ID>`, so it survives a coordinator restart) and logged as a
       `capacity_override` event naming the forced task(s); lifting it deletes the row and logs
       `capacity_override_lifted`. Both events carry the box AND the (first) forced task's id, so
       `runq show <task>` shows the whole trail next to its `forced_placement`. Both are written
       only after the push SUCCEEDED, so a failed push is retried next poll and the record never
       claims a state the host does not hold.

## Open questions
Resolved with defaults (flip in review if wrong):
- **Q1 — soft pause holds VRAM.** `SIGSTOP` keeps the trainer resident, so the ~8–12 GB VRAM stays
  reserved while frozen. This is ideal for stepping away / calls / battery / thermals. If the goal
  is to free the GPU for *another GPU workload*, use `drain` (releases everything). **Default:
  freeze/hold for soft.**
- **Q2 — 30-min timeout escalates soft→hard** (requeue elsewhere AND keep holding), rather than
  requeue-then-reaccept, since a forgotten pause means the operator has walked away. **Default:
  escalate-to-hard.**
