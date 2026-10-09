# Feature: Free-space guard — hold and alert when the data root runs low, instead of failing tasks

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (fleet coordinator)
- **Module path:** `fleet/dispatcher.py`
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/free-space-guard.spec.md`
- **Related:** `docs/specs/task-dispatcher.spec.md` (the poll loop, invariants 9, 9a, 10b-10d, 19,
  19h, 20h, 23, 29, 30), `docs/specs/experiments-retention.spec.md` (what frees the space)

## Purpose
The dispatcher writes everything it knows to one filesystem, the data root: the registry
(`runs.sqlite`), every pulled result, checkpoint and TensorBoard file, and its own scratch copies
of each box's state. Nothing in it looked at how much of that filesystem was left.

**The incident this comes from.** A site's data root reached zero free bytes while the fleet was
busy. Inside one poll cycle:

- two tasks that had FINISHED on their boxes were recorded `task_failed: artifact_missing
  (results.json absent after 2 pulls)`. The results were on the boxes; the pulls failed because the
  coordinator had nowhere to put them. `task_failed` is terminal and is never retried;
- the pulls of each box's small state failed the same way, three in a row, so two healthy owned
  boxes were quarantined as unreachable and the 31 tasks running on them were recorded
  `infra_failed` and requeued;
- the next write to the registry raised `sqlite3.OperationalError: database or disk is full` in the
  completion path. The poll loop has no handler, so the process died, its supervisor restarted it,
  and it died again on the first write: a restart loop of about an hour, during which nothing was
  recorded at all. It ended only when someone moved 1.7 GB off the volume by hand.

Retention (`experiments-retention.spec.md`) bounds the tree when it is run. This guard is what
happens when it has not been: the dispatcher stops consuming space while some is left, says so,
and records nothing as failed that failed only because of the disk.

## Where it runs
Inside the dispatcher's poll loop: one `statvfs` per cycle. No new process, no new file. The state
is in memory and mirrored in one row of the registry's `settings` table.

## Settings
Rows of the `settings` table, read from the registry on EVERY cycle, so a change needs no restart.

| setting | default | meaning |
|---|---|---|
| `data_root_hold_free_gb` | 5 | a HOLD begins when less than this is free. 0 turns the reading-driven hold off |
| `data_root_resume_free_gb` | 10 | the hold ends when at least this is free. A value below the hold mark is read as the hold mark |

A gigabyte is 1024³ bytes, as in the retention tool. The marks are absolute, not a share of the
volume: a site with a small data root must set them.

Runtime state, not a knob (so not in `DEFAULT_SETTINGS`): `settings['data_root_guard']`, JSON
`{"holding": bool, "at": <ISO>, "why": "free"|"registry"|"write"}`. `at` is when the current
state began: when the hold started, or when the last one ended. `why` is present only while holding.

## Output contract
- Events: `data_root_low` (a hold begins) and `data_root_ok` (it ends), detail JSON
  `{"free_gb", "hold_free_gb", "resume_free_gb", "why"}`, plus `"held_min"` on `data_root_ok`;
  `notify` rows for the pushed alerts, as for the GPU alerts (task-dispatcher inv. 30);
  `local_pull_no_space` (invariant 6).
- Every `poll_cycle` event carries `"data_root": {"free_gb": <number|null>, "holding": <bool>,
  "deferred": <completions deferred this cycle>}` (invariant 10). Read the series with:
  `SELECT t, json_extract(detail, '$.data_root.free_gb') FROM events WHERE event='poll_cycle'`.
- Alerts: an `[ALERT]` line on stderr and one push through the dispatcher's existing channel
  (`Dispatcher.notify`, else the ntfy topic in the environment, else none) at each edge.

## Public API
Module-internal to `fleet/dispatcher.py`:
- `data_root_free_bytes(path) -> int | None`, `out_of_space(exc) -> bool`, the `LOCAL_DISK_FULL`
  pull result;
- `Dispatcher.free_bytes` (the injectable reading: `(path) -> bytes | None`),
  `Dispatcher.poll_survivable() -> bool`.

## The split: what a hold stops, and what keeps running

| held | why |
|---|---|
| placement (`_place_queue`): no claim, no rental, no preemption for a waiting task | new work is new results |
| consolidation, shipping (`_ship_all`) | a shipped task starts writing; staging and compiles write to the data root |
| per-task TensorBoard pulls and periodic checkpoint pulls | they are where the bytes go |
| the terminal completions that pull: DONE, FAILED, PREEMPTED | results, logs and checkpoints; see invariant 5 |
| the reapers that judge by pulled copies (invariant 5) | their evidence stopped arriving |

| keeps running | why |
|---|---|
| reconcile, ssh config, cost booking, box measurement, probes, operator requests | registry and network only |
| `HEARTBEAT` and `worker.jsonl` pulls (a few kilobytes per box) | a task that starts must still be seen to start; a box that dies must still be seen to die afterwards |
| the marker listing | an ssh, nothing is written here |
| cancels, including a cancelled task's completion and its best-effort `run.log` | an operator's stop must not wait for disk |
| drain of a paused box, over-capacity shedding, capacity push | they remove load, never add it |
| bringing a box that was ALREADY rented to `live` | stopping halfway gets a paid box destroyed as a stuck provision |
| teardown, the three garbage collections, worker refresh | teardown stops spend; the collections are what free space |

## Behavior & invariants

1. **The reading.** At the top of every poll cycle, before anything else, the dispatcher reads the
   space available to its OWN user on the filesystem that holds the data root
   (`statvfs(EXPERIMENTS_ROOT)`: `f_bavail × f_frsize`, not the root-only reserve). A filesystem
   that limits inodes and has none free reads as 0, because nothing can be created on it. The read
   goes through ONE seam, `Dispatcher.free_bytes`. A read that fails or raises is UNKNOWN: by
   itself it never begins a hold and never ends one (a hold mark of 0 still releases, invariant 2).
   `--dry-run` does not read.
2. **Two marks.** A hold begins when the reading is below `data_root_hold_free_gb` and ends when it
   is at or above `data_root_resume_free_gb`. Between the marks nothing changes, in either
   direction. A hold mark of 0 disables the reading-driven hold and releases one that is in force.
3. **One event and one alert per edge.** Beginning a hold logs ONE `data_root_low`; ending it logs
   ONE `data_root_ok`. Each edge prints one `[ALERT]` line and sends ONE push; a push that fails is
   retried on later cycles until it is delivered, and the event is not repeated. The state is
   mirrored in `settings['data_root_guard']`, so a restart neither repeats an edge nor forgets a
   hold. If the registry cannot record the beginning (it is the thing that is full), the hold is in
   force anyway and the row and event are written on the first cycle that can.
4. **What a hold stops** is the table above, and nothing else. A completion that is deferred
   leaves its marker on the box and its task in the state it was in; it is counted in
   `poll_cycle`, not logged per task per cycle.
5. **Nothing that is true only because of the hold is recorded as a failure.**
   - No `artifact_missing`: a DONE marker is not acted on while holding.
   - The reapers that judge a task or a box by a locally pulled copy do not run while holding:
     stalled (inv. 19: local TensorBoard and checkpoint age), dead worker (10c: the pulled
     `HEARTBEAT`), unclaimed ship (10b), over-packed box (19h), undeliverable claim (10d: ships
     are stopped). The reapers that read only the registry or real transport failures keep
     running: orphaned task, unreachable owned box, soft-pause timeout.
   - **The clocks restart when the hold ends.** For the two reapers whose clock the hold stopped
     feeding (stalled, undeliverable claim), no age accumulated before the end of the hold counts:
     a task cannot be reaped in the first cycle after on what it did not report during it.
   - A deferred completion completes normally in the first cycle after the hold.
6. **A pull refused by OUR disk is not the box's failure, in a hold or out of one.**
   `rsync_pull` returns `LOCAL_DISK_FULL` (falsy, so every caller still sees a failed pull) when
   rsync failed and its stderr says `No space left on device` on a line that is not the sender's.
   - The connection tracker ignores it: it is neither a failure toward the proxy→direct switch,
     the unreachable-box quarantine or a ship deferral, nor evidence that the box answered.
   - A DONE, FAILED or PREEMPTED completion that receives it is deferred exactly as in a hold: no
     `artifact_missing`, no failure recorded without its log, no requeue from a stale checkpoint.
   - In a cycle where one was seen, the pulled-copy reapers of invariant 5 are skipped for that
     cycle, and `local_pull_no_space` is logged once per run of such cycles (not while a hold is in
     force: its own event already says so).
   - A box whose ingest raises `ENOSPC` on this machine is treated the same way.
   This is the receiving side. The pushing side has its own, older case (`REMOTE DISK FULL`, the
   BOX is out of space) and the two are not merged: one is fixed here, the other on the box.
7. **The loop survives a registry that cannot be written.** The daemon runs each cycle through
   `poll_survivable()`. If `sqlite3.OperationalError: database or disk is full`, or an `OSError`
   with `ENOSPC` for a path under the data root (or naming no path), escapes the cycle: the open
   transaction is rolled back, the cycle is abandoned, the process stays up, and a hold begins at
   once whatever the marks say (`why` = `registry` or `write`). Every other exception propagates
   exactly as before — including `ENOSPC` that names a path on another filesystem, where the
   reading would say "plenty" and the hold would begin and end every cycle. Any hold ends by
   invariant 2's rule AND only when the `data_root_ok` event itself can be written, so a registry
   that is still full cannot flap. While a hold is in force and cycles keep being abandoned, one
   `[ALERT]` line says so at most every ten minutes; that is the only repeated output of a hold.
   `--once` is unchanged: it raises.
8. **Idle is unchanged.** With the reading at or above both marks and no hold in force, every
   phase runs, and the events, transitions and failure counts of a cycle are what they were before
   this feature, apart from the `data_root` key in `poll_cycle`.
9. **The limit this cannot remove.** A finished task's files stay on its box for 12 hours after
   its terminal marker and are then swept by the worker itself (`spool_worker._prune_finished`,
   task-dispatcher inv. 29b); a rented box is also destroyed at its hard cap. A hold that outlasts
   those loses results that were never pulled. That is why the alert exists, and why a site should
   run retention on a timer rather than wait for this guard.
10. **The reading is recorded at the source, every cycle.** Each `poll_cycle` event carries ONE
    new key, `data_root`: `{"free_gb": <the reading in GiB, two decimals; null when the read
    failed>, "holding": <bool>, "deferred": <completions deferred this cycle>}` — whether or not a
    hold is in force. The registry therefore holds a time series of the volume filling, and nobody
    has to wait for a mark to be crossed to see it coming, or log in and run `df` to learn which
    filesystem the guard reads. No other key of that event changes.

## Fixtures

Each becomes one or more tests in `tests/test_free_space_guard.py`, with a planted reading or a
planted failure that makes the guard trip, and the matching control.

| fixture | asserts |
|---|---|
| `reading_bytes_and_inodes` | `f_bavail × f_frsize`; no free inode reads 0; an unreadable path reads unknown (inv. 1) |
| `unknown_never_holds_never_releases` | a failed reading begins nothing and ends nothing (inv. 1) |
| `two_marks_with_a_gap` | below hold → hold; between the marks → no change either way; at resume → released; mark 0 → off (inv. 2) |
| `resume_mark_below_hold_mark` | a resume mark set below the hold mark is read as the hold mark (Settings) |
| `one_event_one_alert_per_edge` | many low cycles, one `data_root_low`, one push; a failed push is retried, the event is not; a restart mid-hold repeats neither (inv. 3) |
| `hold_stops_placement_shipping_payloads` | a queued task stays queued, a claimed task is not shipped, no `tb/` or checkpoint rsync is issued; `HEARTBEAT`, `worker.jsonl` and the marker listing still are; `shipped → running` is still observed (inv. 4) |
| `done_marker_is_deferred_then_completes` | DONE while holding: no pull, no `artifact_missing`, state unchanged; after release the same marker completes it `done` (inv. 5) |
| `cancel_completes_during_a_hold` | a `cancelling` task still reaches `cancelled` (inv. 4) |
| `no_reap_during_or_right_after_a_hold` | a task silent for longer than `stall_timeout_min` is not reaped while holding nor in the first cycle after; the same task IS reaped with no hold (inv. 5) |
| `undeliverable_clock_restarts` | a claim older than `ship_timeout_min` with a ship failure is not requeued in the first cycle after a hold; it is with no hold (inv. 5) |
| `local_full_pull_is_not_a_box_failure` | three pulls failing with the receiver's out-of-space message leave the failure count at 0 and the box `live`; three failing any other way quarantine it (inv. 6) |
| `local_full_pull_is_not_a_missing_artifact` | DONE whose pull fails that way: not `task_failed`; fails any other way with no artifact: `artifact_missing` as before (inv. 6) |
| `sender_side_message_is_not_local` | the same words on a `[sender]` line are an ordinary failed pull (inv. 6) |
| `loop_survives_a_full_registry` | `poll_once` raising the full-disk error: `poll_survivable` returns False, the hold is in force, nothing propagates; `database is locked`, other errors, and `ENOSPC` on another filesystem propagate (inv. 7) |
| `partly_landed_done_waits` | DONE whose bulk pull our disk refused while the small artifact landed is not declared `done`; nor is one whose artifact-only retry was refused `artifact_missing` (inv. 6) |
| `registry_hold_needs_a_writable_registry` | with the reading healthy but the event write still failing, the hold stays; when the write succeeds it ends, once (inv. 7) |
| `pulled_copy_reapers_stand_down` | while holding the five pulled-copy reapers are not called and the three others are; with no hold all eight are (inv. 5) |
| `local_enospc_in_a_box_ingest` | a box's ingest raising `ENOSPC` here counts no failure; any other exception counts one (inv. 6) |
| `tracker_neither_counts_nor_clears` | a pull refused by our disk leaves a box's failure count where it was: it is not a failure, and not proof the box answered (inv. 6) |
| `push_channel_that_raises` | an alert channel that raises does not end the cycle; the push is retried (inv. 3) |
| `healthy_cycle_is_reported_complete` | `poll_survivable` returns True for a cycle that was not abandoned (inv. 7) |
| `idle_is_unchanged` | with ample space the cycle's phases are the full set, nothing guard-related is logged, and the events of a cycle equal those of a cycle with the guard switched off (inv. 8) |
| `reading_recorded_every_cycle` | `poll_cycle.data_root.free_gb` is the planted value; null on a failed read; `holding` true in a hold; the event's other keys are exactly the ones it had (inv. 10) |

## Open questions
- **The 12-hour sweep (invariant 9).** A marker the coordinator leaves on each box while it is
  holding, and that the worker's sweep respects, would remove the limit. It needs a worker change,
  which restarts every box's worker on delivery, so it was left out of this feature.
- **A long hold is quiet.** There is one push when it begins. Nothing repeats it after six hours,
  or shortly before the first result would be swept.
- **Starting during a full registry.** The constructor writes (new settings rows, a start-up
  event). If those writes fail the process still exits and its supervisor retries; only the cycle
  is covered by invariant 7.
- **The HTTPS API** keeps accepting submissions and code blobs during a hold (they are small), and
  answers a write it cannot make with the registry's own error. It has no view of the hold.
- **Other filesystems.** Only the data root's filesystem is read. A registry kept elsewhere
  (`--db`) is covered by invariant 7 alone, and a result directory that is a mount of its own is
  covered by invariant 6 alone.
- **Quotas.** A user quota fails writes with a different message than a full device; a pull that
  hits one is an ordinary failed pull here.
- **A queued task does not say why it waits.** During a hold placement does not run, so no task
  gets a `hold` event naming the data root; the reason is the one `data_root_low` event, and the
  campaign watcher does not show it.
- **An unreadable volume keeps a hold.** By invariant 1 an unknown reading ends nothing. If the
  read itself stays broken, the way out is by hand: set `data_root_hold_free_gb` to 0.
- **The container health check.** It judges the dispatcher by the age of the newest `poll_cycle`
  event. While the registry cannot be written no such event lands, so the dispatcher reads
  unhealthy although it is up and holding. That is accurate enough to leave; whether anything
  restarts a container on that state is the site's deployment choice (Docker alone does not).
