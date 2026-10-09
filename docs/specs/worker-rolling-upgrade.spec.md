# Feature: rolling worker upgrade — deliver new worker code WITHOUT restarting live work

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (coordinator)
- **Module path:** `fleet/dispatcher.py` (all of it; no box-side change)
- **Status:** PARTIALLY BUILT <!-- draft → approved → built -->
  - **R1 (delivery gate) — BUILT** 2026-08-02, `f065dd18`. Verified live: all 4 boxes deferred at
    20:41:18, 0 re-execs and 0 `CheckpointRegression` since.
  - **R9.3 (double-run liveness check) — BUILT** 2026-08-02, `f065dd18`.
  - **R10 (CheckpointRegression ⇒ infra) — BUILT** 2026-08-02, `dc22648b`.
  - **R3 / R4 / R6 (the stale hold, the roll, the bound) — BUILT** 2026-08-03. Open questions 1, 2
    and 3 ANSWERED by the owner (see below); the roll now terminates instead of freezing.
  - **Open question 3 (urgent escape hatch) — BUILT** 2026-08-03 as `fleet/roll_now.py`.
- **Spec file:** `docs/specs/worker-rolling-upgrade.spec.md`
- **Extends:** `docs/specs/task-dispatcher.spec.md` invariant 20i (worker self-update) as **20j**

## Purpose

Worker code delivery is currently **unconditional and fleet-wide**: `_refresh_workers` rsyncs the
four bootstrap files to every live box on a 30-minute cycle, and each worker re-execs the moment it
sees new bytes. A re-exec **under running work restarts that work COLD**, which the checkpoint guard
then refuses — so a routine push to master mass-kills in-flight training.

This spec replaces that with a **rolling upgrade**: a box due an upgrade stops taking NEW work,
finishes what it already holds, and is upgraded only once it is EMPTY — then returns to the pool (or
is reaped by the existing idle path if the fleet no longer needs it). New capacity is rented already
current. No occupant is ever preempted, requeued, or checkpoint-disturbed to make an upgrade happen.

## The failure mode this exists to remove

**MEASURED 2026-08-02.** Commit `c3f23565` (19:28) touched `spool_worker.py`. `_refresh_workers`
delivered it; five boxes re-exec'd `e610d48475701dc6 -> 9686ea59d7d59000` at 19:30:31–19:30:49; and
**six tasks across five campaigns on three boxes died inside eight minutes** — `m60_perhead_rep` ×2,
`m58_craftax_budget`, `m65_slice_default_n6`, `wrel_pc_ab2`, `m62_color_cost` — every one with
`shared.infra.checkpoint.CheckpointRegression: progress would go BACKWARDS`. There was **no
`worker_retired` event anywhere in the preceding twelve hours**, which is what proves this path
rather than the `_retire_pre_20i_worker` bootstrap fixed the day before.

**Why a re-exec restarts the work, exactly.** The replacement worker comes back up with no in-memory
state, `validate_and_prepare` re-adopts `active/<id>` (no terminal marker, `repo/` already extracted),
and `launch_ready` relaunches the task. That relaunch derives `--init-from` from ONE place —
`task.json`'s `resume_from` — which the coordinator stamped at SHIP time
(`_build_task_json`: `resume_from = "resume.pt" if task["resume_checkpoint"] and entry.resume_flag`).
A task shipped fresh has `resume_from: null`, and **`task.json` is never rewritten on the box**. So
the relaunch replays a hours-stale ship-time decision, starts at seed_index 0, and the guard refuses
to write over the valid checkpoint it has since produced.

Confirmed end-to-end on one task (`ad56b58e`, `m60_perhead_rep/rw1_s678`), from the box's own
`worker.jsonl`:

| time | event | argv |
|---|---|---|
| 15:42:49 | first launch | no `--init-from` — correct, fresh task |
| 19:30:48 | **re-exec relaunch, same box, same id** | **no `--init-from`** → `exit` 4 s later |
| 19:39:16 | dispatcher requeue + re-ship | **has `--init-from`** — ran to completion |

⚠ **The restore machinery is NOT broken** — the same task resumed correctly nine minutes later. It
is simply never invoked on the re-adopt path. Do not "fix" this by wiring `task.json`'s `resume_from`
into re-adoption: `../resume.pt` is the SHIP-TIME checkpoint, so that would restore hours-stale state
in place of the live `out/ckpt_latest.pt` the guard was protecting. Resuming a re-adopted task
correctly is a separate concern (see Open question 5); this spec removes the need to re-adopt at all.

**Why the box-side fix (`8d535aa7`) is necessary but not sufficient.** It makes the worker DEFER its
re-exec while any task is on the box. Two gaps remain, and both are structural:

1. **It cannot protect the boxes that need protecting.** The fleet is running the OLD worker, which
   has no defer logic. Installing the fix requires the very re-exec it prevents — so its own
   deployment is one more mass kill. A box-side guard can never protect a box that has not yet
   received it; only the coordinator can.
2. **Deferral is unbounded.** `self_update_decision` returns `"defer"` while `active` is non-empty,
   and nothing stops the placer topping the box back up. A busy box never drains, so it never
   updates — the fix trades mass-kills for a fleet that silently stops updating. The docstring's
   "the box updates when it drains" is only true if something makes it drain.

## Input contract

Internal to the coordinator. Consumes two existing surfaces, no new boundary:

- `~/spool/WORKER_VERSION` — written by `spool_worker.main` at startup with the fingerprint it
  actually LOADED. Already probed for PRESENCE by `_retire_pre_20i_worker`; this spec reads its
  CONTENT. Treated as untrusted box-supplied text: parsed at the boundary, any unreadable/malformed
  value fails SAFE (R6).
- `spool_worker.SOURCE_FILES` + `_source_fingerprint()` — the four files (`spool_worker.py`,
  `bundle.py`, `sweep_supervisor.py`, `reap_orphans.py`) and the hash the worker computes over them.

## Output contract

Two new per-instance settings rows (mirroring `drain_hold_i<id>` / `ship_quarantine_i<id>`), and
four new events:

    settings   worker_roll_i<id>       {"at": <iso8601>, "from": <fingerprint>}

    events     worker_roll_admitted    box entered the roll; holds new work
               worker_roll_upgraded    box emptied, code delivered, hold cleared
               worker_roll_expired     hold exceeded its bound; returned to pool STILL STALE
               worker_roll_deferred    delivery skipped: box occupied (edge-triggered, per box)

`worker_roll_expired` is deliberately distinct from `worker_roll_upgraded`: "returned to the pool"
and "returned to the pool having achieved nothing" must not read identically in the log, or a roll
that never completes looks exactly like one that always does.

## Public API

Module-internal only. Nothing outside `fleet/dispatcher.py` changes; `spool_worker.py` is
**not modified by this spec** (`8d535aa7`'s defer logic stays as the box-side backstop).

## Dependencies

- `spool_worker.SOURCE_FILES` / `_source_fingerprint` — imported, never reimplemented (R5).
- `_fits_now` (dispatcher) — the shared placement-eligibility predicate.
- `_bring_up_worker` / `_refresh_workers` — the delivery path.
- Existing idle reclamation (`warm_idle_keep`, `idle_timeout_min`, invariant 11a) — consumed as-is
  to satisfy "kill it if no longer needed"; this spec adds NO teardown rule of its own (R4.3).

## Behavior & invariants

### R1 — DELIVERY GATE: never push worker bytes to an occupied box

`_refresh_workers` skips any box holding a `claimed`, `shipped`, or `running` task, reusing verbatim
the occupancy query `_retire_pre_20i_worker` already uses:

    SELECT COUNT(*) FROM tasks WHERE instance_id=? AND state IN ('claimed','shipped','running')

- R1.1 **This is the load-bearing requirement, and it is worth landing ALONE.** If the bytes never
  arrive, the fingerprint never moves, and a worker cannot re-exec — *including a worker that predates
  `8d535aa7` and has no defer logic*. Being coordinator-side, it protects today's fleet with no
  box-side change and no chicken-and-egg.
- R1.2 The skip is logged edge-triggered per box (`worker_roll_deferred`), not per poll — same
  rationale as `_note_gate` (invariant 19h-4): a silently-never-delivering refresh is just a
  different invisible failure. Log on entering the deferred state and on leaving it.
- R1.3 `_retire_pre_20i_worker` keeps its own identical gate. Two gates, deliberately: it is called
  from inside the same function and must remain correct if ever called from elsewhere.

### R2 — STALENESS is read from what the box is RUNNING, not what it was sent

A box is **stale** iff `~/spool/WORKER_VERSION` content ≠ the fingerprint the coordinator would
deliver, computed by calling `spool_worker._source_fingerprint()` over its own checkout.

- R2.1 **Content, never mtime.** An mtime records when a delivery happened; it cannot distinguish
  "delivered and adopted" from "delivered and deferred". The worker writes WORKER_VERSION with the
  fingerprint it LOADED, so its content is the only signal that means "this is what is executing".
- R2.2 **No new ssh cost.** `_refresh_workers` already performs 1 ssh + 1 rsync per box per
  `worker_refresh_min`; the version read rides that existing connection.
- R2.3 The coordinator MUST import `_source_fingerprint` from `spool_worker` rather than recompute
  the hash. A second implementation of a comparison that gates fleet-wide delivery is exactly the
  reference-leg-sharing-the-defect trap: two implementations that agree prove nothing, and two that
  silently diverge roll the fleet forever or never.

### R3 — STALE HOLD: a box awaiting upgrade takes no new work

A box admitted to the roll gets a DB-backed `worker_roll_i<id>` hold, and joins the existing
exclusions in `_fits_now`:

    return (not inst.get("ship_quarantined")
            and not inst.get("drain_held")
            and not inst.get("worker_roll_held")      # <- new
            and not inst.get("overpack_cooldown")
            and ...)

- R3.1 **It must be excluded in `_fits_now`, not in `place()`'s pack filter.** That is the stated
  reason invariants 10d, 21h and 19h-3 all live there: `_infeasible_everywhere` and `_soonest_wait`
  have to agree with the filter, or the fleet counts a held box's free slots as incoming capacity and
  declines to rent the box that could actually run the work. A hold that is invisible to the capacity
  math starves the queue instead of rolling it.
- R3.2 **DB-backed, not in-memory** — invariant 1 forbids scheduling decisions that depend on
  in-memory state; a daemon restart must not resurrect a held box into the pool mid-roll.
- R3.3 **Without R3 the delivery gate DEADLOCKS.** R1 means an occupied box is never delivered to;
  the placer keeps it occupied; it is stale forever. R1 and R3 are a matched pair and neither is
  correct alone.
- R3.4 ⚠ **It must NOT reuse `drain_held`.** That flag makes an EMPTY box tear down
  (`_should_destroy` → `return True, "drained"`). Here an empty box must be UPGRADED AND KEPT.
  Reusing it would destroy the owned boxes (`-1`, `-2`) — which cannot be re-rented, and which, being
  the boxes that never churn, are precisely the ones most often stale.

### R4 — THE ROLL: one box at a time, and the existing idle path decides its fate

- R4.1 **`worker_roll_max_draining` caps how many boxes may hold at once (default 1).** This is what
  makes it a *roll* rather than a fleet-wide freeze: **every box goes stale simultaneously** the
  instant a worker change lands, so an uncapped R3 holds the ENTIRE fleet, drops throughput to zero,
  and triggers a rent stampede for replacement capacity — strictly worse than the bug being fixed.
- R4.2 **Admission order is deterministic: fewest occupants first**, ties broken by instance id.
  Shortest expected drain first ⇒ the roll completes soonest and the fleet spends the least
  box-time held. A random or arbitrary order can park the roll behind an 8-hour occupant.
- R4.3 **"Return it to the pool, or kill it if no longer needed" requires NO new code.** On upgrade
  the hold is cleared and the box is an ordinary pool member again; if the fleet does not need it,
  the existing idle reclamation (`warm_idle_keep` / `idle_timeout_min`, invariant 11a) reaps it on
  its own terms. Adding a teardown rule here would duplicate that policy and fight it.
- R4.4 **New boxes need nothing.** `_bring_up_worker` rsyncs current code on rental, so rented
  capacity is born current and is immediately eligible — which is exactly what absorbs the load the
  held box is not taking.

### R5 — THE CYCLE (the whole feature, per box)

1. Refresh tick: read WORKER_VERSION, compare (R2). Current ⇒ nothing to do.
2. Stale ⇒ admit to the roll if the fleet is under `worker_roll_max_draining` (R4.1/R4.2); else
   leave it working on old code until its turn.
3. Admitted ⇒ set hold; box takes no new work (R3). **Occupants are left strictly alone.**
4. Occupants finish naturally. No preempt, no requeue, no drain signal, no checkpoint disturbance.
5. Box empty ⇒ delivery gate opens (R1) ⇒ rsync current code.
6. Worker re-execs at its next loop boundary. Safe under BOTH worker generations: the old one
   re-execs unconditionally (harmless — nothing is running), the new one returns `"exec"` because
   `active` is empty.
7. WORKER_VERSION now matches ⇒ clear hold, log `worker_roll_upgraded`, box rejoins the pool — or
   the idle path reaps it (R4.3).

### R6 — FAIL SAFE, AND BOUNDED

- R6.1 **An unreadable or malformed WORKER_VERSION means NOT STALE.** Never hold a box on a bad
  read; worst case it upgrades a cycle later. Mirrors `_drain_held`'s "unreadable hold is no hold —
  never strand a box on a corrupt row". An absent file means a pre-20i worker, which is
  `_retire_pre_20i_worker`'s business, not this feature's.
- R6.2 **The hold expires after `worker_roll_hold_max_min`** and the box returns to the pool STILL
  STALE, logging `worker_roll_expired`. Same shape as `drain_hold_expired`. Without a bound, one box
  with a wedged long-running occupant removes itself from the fleet permanently and takes the roll
  slot with it, stalling every other box's upgrade behind it (R4.1 makes the slot scarce by design).
- R6.3 **A delivery/probe failure is logged, never suppressed** (`_safe_log`, per 20i's own scar:
  the recorder must not be able to raise through a connection that just failed).
- R6.4 The box-side defer (`8d535aa7`) is retained as a backstop. R1 should mean a worker never sees
  new bytes while occupied; if it somehow does, deferring is the correct second line of defence.

### R7 — What this spec does NOT do

- It does **not** preempt, requeue, or drain occupants to accelerate an upgrade. An urgent rollout is
  Open question 3, not a default.
- It does **not** change `spool_worker.py`.
- It does **not** make a re-adopted task resumable (Open question 5). It removes the re-adoption.

### R8 — DEPLOYMENT ORDER: R1 must land BEFORE the next dispatcher restart

⚠⚠ **`_last_worker_refresh` is an in-memory dict, empty at construction.** After a restart,
`now - 0.0 >= interval` holds for every box, so `_refresh_workers` fires **immediately and
fleet-wide** on the first poll — it does not inherit the 30-minute throttle. This is the same
throttle-clearing behaviour that turned 20i's `_safe_log` crash into a retire-loop.

Consequences, both load-bearing:

- **`make dispatch-restart` is currently a TRIGGER, not a fix.** Restarting the coordinator before R1
  lands delivers new worker code to every live box at once, under whatever is running — i.e. it
  brings the mass-kill forward rather than preventing it.
- **Once R1 has landed, that same restart is the correct first step of the roll**: the immediate
  fleet-wide refresh then skips every occupied box and delivers only to idle ones, which is exactly
  the intended behaviour.
- Raising `worker_refresh_min` is **not** a usable stopgap on its own: `_load_settings` runs once at
  construction (line 2129), so the new value requires a restart — and the restart is the trigger.

Therefore R1 lands first, alone if necessary, and only then is the daemon restarted.

### R10 — a `CheckpointRegression` over REAL PROGRESS is INFRA, not a task fault

The guard refuses a write that moves progress backwards. That is the signature of a task being
RESTARTED underneath — never of the task's own code. Classifying it `task_failed` was wrong twice
over: that state is terminal and never auto-requeues, so each one needed a human to notice and
hand-requeue it (**17 did, 2026-08-02**), and it spends the "your code is broken" signal on the
scheduler's mistake.

- R10.1 Route to `_infra_fail`: requeue at HALF a retry, `instance_id` cleared, `resume_checkpoint`
  left on the row so the task **warm-starts from the checkpoint the guard just protected**.
- R10.2 ⚠ **GATED ON `progressed`.** The same guard fires on a genuine self-clobbering code bug
  (m49 `162127c`). Routing that to infra converts one loud terminal failure into up to
  `2*max_retries` silent retries — fail-closed turned back into fail-silent, the exact defect this
  class keeps re-producing. Zero progress ⇒ no valid work was protected ⇒ it is the code, and it
  stays terminal. The `_alert` fires on **both** paths, so neither is silent.
- R10.3 **Requeueing the EXISTING row beats `runq add`**, and this is the non-obvious part: the row
  keeps its original `code_blob`/`git_sha`, so the requeue re-ships the ORIGINAL snapshot. A fresh
  `runq add` would snapshot current master and silently run different code from the arm's clean
  siblings — a code confound on top of the interruption one.

⚠ **What R10 does NOT fix: the interruption confound.** A resumed arm and an uninterrupted control
do not form a paired comparison — this repo's standing rule is *paired = seed + code + interruption
+ box*. `resume-integrity` stamps `resumes` / `resume_verified` into `results.json` and R4.1 refuses
silent comparison, so the requeue is safe to RUN; whether its number is comparable is a separate,
per-campaign judgement. Of the 16 requeued on 2026-08-02, four campaigns lost every arm
(`colour_dose_scout`, `m58_craftax_budget`, `m60_present_order`, `m66_maze_3arm` — symmetric, so
their arms stay comparable to each other) and four lost only some (`m60_perhead_rep`,
`m62_color_cost`, `m65_slice_default_n6`, `wrel_pc_ab2` — clean siblings already exist, so those
resumed numbers carry an interruption asymmetry their controls do not).

### R11 — 20j-3: UPGRADE THE WORKER WITHOUT TOUCHING THE TASK (supersedes the drain for capable boxes)

R1 alone is a deadlock dressed as safety: the box is held because it is occupied, and the placer
keeps it occupied, so a busy box never updates. R3/R4 solve that by forcing a drain. **This solves it
better — by making the restart harmless, so no drain is needed at all.**

**The enabling fact, PROVEN not assumed (2026-08-02):** `os.execv` replaces the process IMAGE but
**keeps the PID**. A re-exec'd worker is therefore *still the parent* of the trainers it launched —
it has only lost the in-memory `Popen` objects. Measured directly: after `execv`, the child's `PPid`
still equals our pid and `waitpid` returns its **true exit code**.

- R11.1 **`ReattachedProc` re-adopts, rather than skipping or relaunching.** On restart the worker
  maps live pids to task dirs via `/proc` cwd (`reap_orphans.find_live_task_procs`) and wraps the
  survivor in an object that quacks like `Popen` (`pid`/`poll`/`terminate`/`kill`), so `check_exits`,
  `check_preemption` and `apply_freeze` all work unchanged.
- R11.2 ⚠ **Skipping is NOT sufficient, and this is the bug the first cut of R9.3 shipped.** The
  **worker** writes the terminal marker in `check_exits`, which returns early on `proc is None`. So
  a merely-skipped survivor finishes into silence: no `DONE`, task stranded in `running` until a
  stall reaper catches it. Adoption is what closes this; the fix and the double-run fix are the same
  fix.
- R11.3 **Picking the right pid matters.** A trainer forks env-workers sharing its cwd, so a task has
  several pids. Discriminators, strongest first: our own **child** (the `execv` case — unambiguous),
  else the **session leader** (`pid == pgid`, which `start_new_session=True` made the trainer).
  Adopting a child env-worker would report the wrong exit.
- R11.4 **Two restart flavours.** `execv` self-update ⇒ we are the parent ⇒ `waitpid` gives the real
  rc, full fidelity. A genuinely new process (crash, OOM, reboot, supervisor respawn) ⇒ reparented to
  init ⇒ rc is unrecoverable *by anyone*; fall back to `/proc` liveness and, on disappearance, raise
  `DONE` and let the **coordinator** adjudicate — `_complete_done` gates on the completion artifact
  and explicitly treats its presence, not an exit code, as "the real proof of completion", failing
  `artifact_missing` otherwise. The uncertainty is resolved by the component that can see the
  evidence.
- R11.5 **The re-exec is gated on a CHECKED precondition** (`can_reattach_all`): every live trainer
  we hold a handle for must actually be visible in `/proc` under its task dir. Invisible here ⇒
  invisible to the restarted image ⇒ the task returns double-run or stranded. Unverifiable ⇒
  `defer`, which costs only latency.
- R11.6 **The coordinator's hold narrows to workers that cannot do this**, via the existing `CAPS`
  advert (same mechanism as `blobref`): a worker writing `reattach` is delivered to even while busy.
  Fail-safe — an older worker, or one that cannot be probed, gets exactly the pre-20j-3 hold. The
  capability can only ever WIDEN what is allowed.

**⚠ The bootstrap is unavoidable for exactly one hop.** A box cannot be *told* to upgrade safely by
code it does not yet run. Today's stale boxes carry `8d535aa7`'s `defer`, so they will not re-exec
under load — they take the new worker at their next natural idle gap, losing nothing. From then on,
every future worker change applies to them live, with no drain and no lost work.

**Relationship to R3/R4:** once the fleet advertises `reattach`, the roll is no longer needed to
*upgrade* anything, and R3/R4 reduce to a fallback for boxes that never gain the capability. They
stay specced but drop in priority.

## Fixtures

Pure-function tests (no ssh, no fleet), in `tests/test_worker_rolling_upgrade.py`, mirroring the
existing `self_update_decision` unit-test style in `tests/test_spool_worker.py`:

| # | input | expected |
|---|---|---|
| F1 | box occupied (`running`), fingerprint moved | no delivery; `worker_roll_deferred` logged once |
| F2 | same box, still occupied, next tick | no delivery; **no second log line** (edge-triggered, R1.2) |
| F3 | box empty, fingerprint moved | delivery proceeds |
| F4 | box stale, roll at capacity | NOT admitted; still takes new work |
| F5 | box stale, roll under capacity | admitted; `_fits_now` false for every task |
| F6 | held box empties | delivery, hold cleared, `worker_roll_upgraded` |
| F7 | WORKER_VERSION unreadable / malformed | treated NOT stale; never admitted (R6.1) |
| F8 | hold older than `worker_roll_hold_max_min` | hold cleared, `worker_roll_expired`, still stale |
| F9 | three stale boxes, `max_draining=1` | exactly one admitted; fewest-occupants-first (R4.2) |
| F10 | held box is empty | `_should_destroy` does NOT return `"drained"` (R3.4 — owned box survives) |

**Mutation checks** (a guard that cannot fail is not a guard — see
`reference-leg-sharing-the-defect-carries-no-bits`): F1 must fail if the occupancy state list
drops `'running'`; F5 must fail if the exclusion is moved from `_fits_now` into `place()`'s pack
filter (assert via `_infeasible_everywhere`/`_soonest_wait`, per R3.1); F10 must fail if
`worker_roll_held` is aliased onto `drain_held`.

## Open questions

**Open questions block implementation until cleared.**

1. ~~**`worker_roll_max_draining` default — 1 or 2?**~~ **RESOLVED 2026-08-03 (owner): default 1,
   and it must be OVERRIDABLE LIVE** — *"ideally we could override it live if we wanted to get
   something out more urgently"*. So `_worker_roll_max_draining()` reads the DB on every call rather
   than `self.settings`, which is snapshotted in `__post_init__`: a width that needs a coordinator
   restart to change is not an urgent lever, and reading the snapshot is the exact trap that made a
   `worker_refresh_min` override a silent no-op on 2026-08-02. Floored at 1 (0 would freeze the roll
   while looking like a tuning choice). `fleet/roll_now.py --width N` is the operator command.
   Measured basis for 1: task runtime median 1.1h, p90 4.8h, p99 13.3h (n=2304), and with 5 live
   boxes width 1 holds 20% of the fleet against width 2's 40%.
2. ~~**Do owned boxes roll in the same lane?**~~ **RESOLVED 2026-08-03 (owner): same lane —
   *"doesn't matter because (1)"*.** At width 1 only one box is ever held, so an owned box in the
   shared lane costs at most one box of capacity and can never hold BOTH owned boxes at once, which
   was the entire risk a separate lane existed to remove. No carve-out is implemented, and adding
   one would be dead surface. ⚠ If the width is ever raised above 1, this answer's premise is gone
   and the question reopens — owned boxes are 42% of completions (984 of 2337) from 40% of boxes.
3. ~~**Is there an URGENT mode?**~~ **RESOLVED 2026-08-03 (owner): YES, allow the escape hatch.**
   Built as `fleet/roll_now.py`, a ONE-SHOT `worker_roll_now` settings row that the
   coordinator consumes and DELETES the moment it acts, then gracefully evicts the rolling box's
   occupants (`_evict_task_graceful`: checkpoint, exit at next save, requeue WITH RESUME — a delay,
   not a kill). Operator-only by construction: nothing in the dispatcher writes the key, which
   `test_NOTHING_in_the_coordinator_writes_the_hatch_key` pins. Same rule as `--resume-unverified` —
   the hatch exists, machinery can never reach it, and it cannot persist.
9. **(superseded numbering — original Q1 text kept for provenance)** `worker_roll_max_draining` 1 or 2? 1 is safest and slowest; with ~5 live boxes and
   an 8h-tail occupant a full roll could take most of a day. 2 halves that for ~40% held capacity at
   peak. Needs an owner call, ideally against the measured distribution of box occupancy.
2. **Do owned boxes (`-1`, `-2`) roll in the same lane?** They cannot be replaced by renting, so
   holding one is pure capacity loss rather than a shift onto rented capacity. Options: their own
   lane; a longer `worker_roll_hold_max_min`; or exclude them and upgrade them by hand during a
   quiet window. They are also the boxes most likely to be stale.
3. **Is there an URGENT mode?** If a worker change is itself a correctness fix, waiting a day is
   wrong. Should there be an operator-only `--roll-now` that accepts preempting occupants? Default
   is no; the question is whether the escape hatch should exist, and it must never be reachable by
   an automatic path (same rule as `--resume-unverified` in the resume-integrity spec).
4. **Should staleness gate on a SUBSET of `SOURCE_FILES`?** A `reap_orphans.py` change cannot affect
   a running task; a `spool_worker.py` change can. Gating on the task-affecting subset would cut roll
   frequency substantially. Deliberately out of scope for v1 (it trades a simple, obviously-correct
   comparison for a judgement call about which files are "safe"), but worth deciding explicitly.
5. ~~Should a re-adopted task resume from its own `out/ckpt_latest.pt`?~~ **RESOLVED 2026-08-02 by
   `191c8fb9`** — `_launch` now falls back to `--init-from ../out/ckpt_latest.pt` when `resume_from`
   is absent. Correct fix, mutation-tested. **But see R9: it changes the failure MODE, and the new
   mode is silent.**

### R9 — ⚠ THE DOUBLE-RUN, and why `191c8fb9` made it INVISIBLE rather than worse

`_reattach_after_restart` re-adopts every non-terminal `active/<id>` with `at.proc = None` — by
design: *"we never re-attach to a possibly-stale/reused pid"*. `launch_ready` then launches a
**second trainer**. The first is untouched: trainers run under `start_new_session`, so they survive
the worker's `execv` (that is 20i's whole premise). `_launched_pids` is in-memory and does not.

**So after any worker restart under live work, two trainers share one `out/` directory.** What
changed today is only what happens next:

| | before `191c8fb9` | after `191c8fb9` |
|---|---|---|
| second trainer starts | cold, at step 0 | self-resumed from `ckpt_latest.pt` |
| checkpoint guard | **trips — `CheckpointRegression`** | does **not** trip: progress is not backwards |
| outcome | interloper dies **loudly**; original survives | both run, **both write the same checkpoint path** |

The guard was doing two jobs, and only one of them was known: it caught the cold restart, and it
*incidentally executed the double-run*. `191c8fb9` removes the cold restart, and with it the
tripwire — converting a loud crash into a silent race. That is precisely the m49/m50/m54
silent-restart class the resume-integrity spec exists to prevent, and `191c8fb9`'s own commit message
names it (*"the checkpoint regression guard is the only reason this was ever visible"*).

`191c8fb9` records the double-run as *"Pre-existing; this change only makes the relaunch resume
instead of restart."* Pre-existing is right; **unchanged is not** — it was previously mitigated by
the guard, and that mitigation is now gone.

- R9.1 **Coverage is NOT complete.** `8d535aa7`'s `defer` only guards the *self-update* restart. A
  worker that dies for any other reason — crash, OOM-kill, box reboot, supervisor respawn — restarts
  with no defer gate at all, while its trainers (separate sessions) keep running. That path is
  unguarded on both ends.
- R9.2 **R1 (delivery gate) closes the self-update arm** by preventing the restart rather than
  surviving it, and does so from the coordinator, covering boxes on any worker generation.
- R9.3 **The direct fix: `_reattach_after_restart` must not relaunch a task whose trainer is still
  alive.** The pid is lost across `execv`, but the seam already exists — `reap_orphans._proc_cwd` +
  `_active_id_from_cwd` map a live pid to its `active/<id>` by cwd, which is exactly this question.
  A dir with a live trainer should be adopted as RUNNING (or left alone), never relaunched.
- R9.4 Until R9.3 lands, **a double-run is undetectable from the registry**: one `start` event, no
  preempt, no requeue, a plausible `results.json`. Worth a dedicated tripwire regardless of R9.3 —
  two processes under one `active/<id>/repo` is a condition the worker can simply check for and log.

## Related

- `docs/specs/task-dispatcher.spec.md` invariant 20i — worker self-update (this is 20j).
- ⚠ **20i's spec text is now STALE and should be corrected in the same change**: it still says a
  `running` occupant *"is not at risk and does not block (separate session, survives)"*. That is the
  exact premise `8d535aa7` refuted with measurements, and the code no longer implements it —
  `_retire_pre_20i_worker`'s gate includes `'running'`. Spec and code disagree; the spec must move.
- `spec/infra/resume-integrity.spec.md` — the `CheckpointRegression` guard that made this visible at
  all. Worth restating: the crashes are the guard WORKING. Without it these runs would have completed
  and emitted plausible `results.json` files for curricula that silently restarted at stage 0.
