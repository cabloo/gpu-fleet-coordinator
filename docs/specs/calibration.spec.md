# Feature: coordinator calibration report (`expected vs actual`)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (experiment tooling)
- **Module path:** `fleet/calibration.py`
- **Status:** built <!-- draft → approved → built -->  (2026-07-13; Q1 resolved in-spec, Q3 built as `docs/specs/est-defaults.spec.md`; 2026-07-15 **section D packing diagnosis** — roadmap #2; 2026-07-15 **section E offer-set counterfactual** — roadmap #3, resolves Q2, pairs with task-dispatcher invariant 5d; 2026-07-15 **trend mode** (`--trend`) — roadmap #5, resolves Q4 by windowing the append-only registry rather than scheduling snapshots)
- **Spec file:** `docs/specs/calibration.spec.md`

## Purpose

A **read-only** analysis layer over the run registry (`experiments/runs.sqlite`) that reconstructs,
per finished task and per billed instance, what the coordinator **expected** (`est_minutes`,
requested slots, chosen offer price) against what **actually** happened (real runtime from the event
timeline, realized `cost_usd`, achieved slot occupancy). It exists to answer a standing question
the registry can't today: *is our modelling of cost, packing, and placement improving over time, and
are we picking cost-appropriate box configurations?* It computes calibration error, cost
attribution, packing occupancy, a **packing diagnosis** that explains *why* occupancy fell short
(section D), and an **offer-set counterfactual** (section E) that quantifies the $/hr premium we paid
for the reliability floor / sizing at rent time, and prints a report. It **changes no dispatch
behaviour and no schema** — feeding these actuals back into future `est_minutes` defaults is
explicitly out of scope (see Open questions / a future spec).

**Trend mode (`--trend`, roadmap #5).** Answers "are our estimates improving over time?" *without*
scheduling periodic snapshots. Because the registry is append-only history and `est_minutes` is
persisted **per task row** (the value actually used at that task's add-time), the trend is a pure
function of the current registry: bucket finished tasks by their `created_at` into calendar windows
(`--bucket week|day`) and compute the section-A runtime ratio (plus degenerate count, and the idle
fraction / fleet occupancy of the instances created in that window) per bucket. This is strictly
better than snapshotting: it needs no scheduler or persisted files, is deterministic, and works
**retroactively over all existing history** (a fresh snapshot regime would only start accruing now).
Ad-hoc point-in-time capture is still available via `--out` on a cron/loop if wanted — unchanged —
but is no longer required to see the trajectory.

**Section E (offer-set counterfactual, roadmap #3).** Answers the standing "are we picking the
cost-ideal box?" question that Q2 deferred: the dispatcher now logs an `offers_considered` event per
rent (task-dispatcher invariant 5d) carrying `premium_dph` — how much cheaper the cheapest *rejected*
raw offer was than the one we rented, plus WHY it was rejected (`reliability_below_floor` /
`too_few_slots`). Section E aggregates these into a realized picture: what fraction of rents paid a
premium, the median/total $/hr premium, and how it splits by reason — so a persistent reliability
premium is the concrete signal to lower `min_reliability`. Since the event is logged going forward
only, section E **states coverage** (`rents_with_offer_data` / total rents) and renders an honest
`n=0` when history predates the event — never assuming absence means "no premium."

**Why section D exists (the motivating finding, calibration roadmap #2).** Section C's raw
`occupancy_mean` is a *misleading* aggregate: on live data (2026-07-15) it conflates three unrelated
failures with very different fixes. Of 171 billed boxes, **73 never ran a single task** (rented,
provisioning/ship failed, torn down — near-zero cost each, $2.72 total, but 73 boxes at 0.0
occupancy crater the mean). Decomposing idle *slot-minutes* (capacity-weighted, not box-weighted)
splits the waste roughly **41% over-provision / 41% under-pack / 18% never-shipped**, and by realized
cost the labelled-bad boxes lead with `over_provisioned` (~$10) then `under_packed` (~$7) then
`never_ran` (~$3). So the roadmap's hypothesis — "we rent big multi-slot boxes into a thin queue" —
is **materially true (~40% of wasted slot-capacity) but only part of the story**: an equal share is
genuine **under-packing despite backlog** (backlog at rent was often *larger* than the box — 8-slot
boxes saw a median ~5–7 slots waiting — yet occupancy stayed low), and provisioning failures add the
rest. Section D decomposes idle slot-capacity into `over_provision` (no backlog existed at rent),
`never_shipped` (backlog existed but the box never ran a task), and `under_pack` (ran but sat idle
with backlog available) so the operator knows which lever to pull (offer-sizing vs provisioning
reliability vs the packer) instead of chasing a single misleading occupancy number. (The `over_provision`
share is measured *despite* a fleet-wide backlog count that structurally over-states fillable demand —
Open question 5 — so it is a lower bound.)

## Input contract

- **Database file** `experiments/runs.sqlite` — the same registry defined by
  `docs/specs/run-registry.spec.md` (schema v1). This tool is a **consumer** that reads the schema
  directly (SQL); it takes a **read-only** connection (`PRAGMA query_only=ON`) and never writes,
  transitions, or migrates. Resolution of the DB path reuses
  `registry_db.shared_experiments_root()` (the MAIN checkout's `experiments/`), overridable with
  `--db PATH`.
- The report is derived **entirely** from three existing tables — `tasks`, `instances`, `events` —
  with no new columns. The relevant fields are the registry's public schema contract: from `tasks`
  (`id, grp, name, entrypoint, slots, est_minutes, state, instance_id, resource_hint_json`), from
  `instances` (`id, dph_usd, gpu_name, slots_total, created_at, destroyed_at, cost_usd`), from
  `events` (`seq, t, task_id, instance_id, event, detail`).
- **CLI (internal surface):**
  ```
  python fleet/calibration.py [--db PATH] [--group G] [--entrypoint E]
      [--since ISO8601] [--format md|json] [--out FILE] [--trend] [--bucket week|day]
  ```
  - `--group`/`--entrypoint`/`--since` are inclusive filters over `tasks` (`--since` compared against
    `tasks.created_at`). Absent → all tasks.
  - `--format` default `md` (human report to stdout); `json` emits the machine-readable structure in
    the Output contract. `--out FILE` writes the chosen format to a file instead of stdout.
  - `--trend` switches to **trend mode** (invariant 17): instead of the single-point report, emit the
    per-window time series described below. `--bucket` (default `week`) selects calendar-week vs
    calendar-day windows. The `--group`/`--entrypoint`/`--since` filters apply to the population
    *before* bucketing.

## Output contract

Module-internal (a report + a JSON object), NOT a cross-module contract. The `json` format is a
single object:

```jsonc
{
  "generated_from": { "db": "…/experiments/runs.sqlite", "task_count": 301, "since": null },
  "runtime": {                       // section A — est_minutes vs actual
    "overall": { "n": 114, "excluded_degenerate": 9, "ratio_median": 0.67,
                 "ratio_mean": 1.04, "ratio_p10": 0.13, "ratio_p90": 3.7 },
    "by_entrypoint": [               // one row per entrypoint, most tasks first
      { "entrypoint": "train_atari", "n": 40, "est_minutes_median": 120,
        "actual_minutes_median": 88.4, "ratio_median": 0.72,
        "under_est_count": 6, "over_est_count": 34 }
    ]
  },
  "cost": {                          // section B — realized $ and idle overhead
    "realized_total_usd": 27.83,
    "attributed_usd": 21.10,         // summed over tasks (active slot-minute share)
    "unattributed_idle_usd": 6.73,   // realized − attributed = provisioning/idle/teardown waste
    "idle_fraction": 0.24,
    "cost_per_done_task_usd": 0.24,
    "by_group": [
      { "grp": "native_m24", "done": 12, "attributed_usd": 3.40,
        "usd_per_done_task": 0.28 }
    ]
  },
  "packing": {                       // section C — achieved slot occupancy
    "fleet_occupancy": 0.58,         // time-weighted used_slots / slots_total over billed lifetimes
    "boxes": 120,
    "by_offer_class": [              // grouped by (gpu_name, slots_total)
      { "gpu_name": "RTX 3060", "slots_total": 1, "boxes": 44,
        "occupancy_mean": 0.61, "dph_usd_median": 0.061 }
    ]
  },
  "packing_diagnosis": {             // section D — WHY occupancy fell short (same box population as C)
    "boxes": 171, "boxes_ran": 98, "boxes_never_ran": 73,
    "rent_time_field": "created_at", // the rent-time proxy used for backlog-at-rent
    "idle_slot_minutes": 41230.0,    // Σ (slots_total·lifetime − used) over billed+closed boxes
    "idle_partition": {              // fractions of idle slot-minutes; sum ≈ 1.0 (null if no idle)
      "over_provision": 0.06,        // rented slots with NO backlog at rent
      "never_shipped": 0.11,         // box ran zero tasks though backlog existed
      "under_pack": 0.83             // ran but idle while backlog was available at rent
    },
    "by_reason": [                   // dominant reason per box → box count + realized cost, $ desc
      { "reason": "under_packed", "boxes": 61, "cost_usd": 40.10 },
      { "reason": "healthy", "boxes": 32, "cost_usd": 4.89 },
      { "reason": "never_ran", "boxes": 73, "cost_usd": 2.72 },
      { "reason": "over_provisioned", "boxes": 5, "cost_usd": 1.20 }
    ],
    "by_offer_class": [              // mirrors C's (gpu_name, slots_total) + backlog & verdict
      { "gpu_name": "RTX 3060", "slots_total": 8, "boxes": 10, "occupancy_mean": 0.0,
        "backlog_slots_at_rent_median": 6.0, "boxes_never_ran": 8, "verdict": "never_ran" }
    ]
  },
  "offers": {                        // section E — offer-set counterfactual (from offers_considered)
    "rents_total": 171,              // rent_intent events in scope
    "rents_with_offer_data": 42,     // of those, how many carry an offers_considered event (coverage)
    "premium_paid_rents": 15,        // rents where cheapest_alt was cheaper than chosen (premium_dph>0)
    "premium_dph_median": 0.006,     // over premium-paying rents
    "premium_dph_total": 0.114,      // Σ premium_dph — extra $/hr summed across premium rents
    "by_reason": [                   // premium split by why the cheaper offer was skipped, $ desc
      { "reason": "reliability_below_floor", "rents": 11, "premium_dph_total": 0.092 },
      { "reason": "too_few_slots", "rents": 4, "premium_dph_total": 0.022 }
    ]
  }
}
```

**Trend mode (`--trend`)** emits a DIFFERENT top-level object (a time series, not the single report):

```jsonc
{
  "generated_from": { "db": "…", "bucket": "week", "task_count": 288, "since": null,
                      "group": null, "entrypoint": null, "skipped_malformed": 0 },
  "bucket": "week",                  // granularity (duplicates generated_from.bucket)
  "buckets": [                       // oldest → newest, one per calendar window that has tasks
    { "window": "2026-W28", "start": "2026-07-06",
      "runtime": { "n": 40, "excluded_degenerate": 3, "ratio_median": 0.81, "ratio_p90": 2.4,
                   "under_est_count": 9, "over_est_count": 31 },
      "cost": { "idle_fraction": 0.19 },
      "packing": { "fleet_occupancy": 0.58 } }
  ],
  "direction": { "ratio_median": "improving" }   // improving|worsening|flat|null — toward 1.0
}
```

Section E consumes the `offers_considered` event (`events.event = 'offers_considered'`, detail = the
JSON summary from task-dispatcher invariant 5d). Its `rents_total` counts `rent_intent` events in
scope (each rent logs exactly one). Malformed/absent detail is skipped and tallied like any bad row
(invariant 10), never fatal.

The `md` format renders the same five sections as headed tables, headline line first (per the
run-summary convention: first line is a one-glance summary, e.g. `calibration: runtime ratio
median 0.67 (n=114, 9 degenerate excluded) · idle overhead 24% · fleet occupancy 0.58 · 73/171
boxes never ran · idle split 83% under-pack / 11% never-shipped / 6% over-provision · offer premium
$0.011/hr over 15 rents (42/171 with data)`). Section E's line is omitted/marked when coverage is 0.

## Public API

Module-internal only. Pure reconstruction helpers are unit-testable functions taking already-fetched
rows (no live DB), mirroring `dispatcher.py`'s pure-core/impure-shell split:

- `reconstruct_task_actuals(task_row, events_for_task, now) -> TaskActual` — active runtime +
  per-instance segment list from one task's rows (invariant 2). Each segment carries
  `(instance_id, start_dt, end_dt, minutes, slots)`.
- `attribute_costs(instances, task_actuals) -> CostBreakdown` — slot-minute cost attribution.
- `occupancy(instance_row, occupied_intervals) -> float` — time-weighted slot utilisation, where
  `occupied_intervals` is the list of `(start_dt, end_dt, slots)` tuples on that instance (derived
  from the invariant-2 segments, not re-paired from raw events).
- `backlog_slots_at(rent_dt, backlog_windows) -> int` — sum of `slots` over the half-open windows
  `(enter, exit)` that contain `rent_dt` (`enter <= rent_dt < exit`); the fleet-wide queued demand
  at an instant (invariant 11).
- `packing_diagnosis(instances, task_actuals) -> dict` — the section-D object: reconstructs backlog
  windows from the task actuals, then per billed+closed box splits idle slot-minutes into
  over-provision / never-shipped / under-pack and classifies each box's dominant reason (invariants
  11–15).
- `offer_counterfactual_report(events) -> dict` — the section-E object: from the `rent_intent` and
  `offers_considered` events, computes coverage and the realized $/hr premium split by reject reason
  (invariant 16). Pure over already-fetched event rows.
- `build_report(tasks, instances, events, filters, now) -> dict` — the Output-contract object.
- `build_trend(tasks, instances, events, bucket, now) -> dict` — the trend-mode object (invariant
  17): partitions the fetched rows into calendar windows and reuses the section builders per window.

CLI `main(argv)` is the impure shell (opens the read-only connection, prints/writes).

## Dependencies

- `fleet/registry_db.py` **public** helpers only: `shared_experiments_root` and the schema
  itself — it never calls `registry_db.connect`; the read-only handle is opened directly via
  `sqlite3` in `_connect_ro` (`file:…?mode=ro` + `PRAGMA query_only=ON`, consistent with resolved
  Open question 1). No dispatcher import; no `vastai`; no network.

## Behavior & invariants

Acceptance criteria — numbered and testable. Timestamps parse the registry's `%Y-%m-%dT%H:%M:%SZ`
UTC format; all durations are minutes.

1. **Read-only.** The connection is opened `query_only`; a test asserts any write attempt raises and
   that the DB file mtime is unchanged after a full report run.

2. **Actual runtime = summed active segments, not wall clock.** For each task, walk its events in
   `seq` order and pair every `start` event with the next **running-exit transition event** for that
   task in `{done, task_failed, infra_failed, preempt_intent, cancel_requested}`; the segment is
   `(start.t, terminator.t)`. These five are exactly the events the registry logs on a transition
   OUT of the `running` state (`running`→`done`/`task_failed`/`infra_failed`/`preempting`/
   `cancelling`; the state `preempting` is logged as event `preempt_intent` and `cancelling` as
   `cancel_requested`). The `stalled` and `lost` events are NOT terminators — they are markers the
   dispatcher logs immediately before an `infra_failed` transition (a stalled task is
   `_infra_fail`ed; a lost box `_infra_fail`s each of its tasks), so the `infra_failed` event is the
   true segment close and keying on the five transition events counts each segment exactly once.
   `actual_active_minutes` is the sum of these segments' durations. A requeued/preempted task
   therefore accrues one segment per instance it ran on (its total compute), **not**
   first-start-to-final wall clock (which would double-count idle-in-queue gaps). A `start` with no
   following terminator (still `running`) contributes a segment ending at report time only when the
   task state is `running`; finished tasks always have a terminator. A second `start` arriving while
   a segment is already open (no intervening terminator — a data anomaly) is ignored, not treated as
   a nested segment.

3. **Runtime calibration (section A) covers only finished tasks** — `state IN (done, task_failed)`.
   `ratio = actual_active_minutes / est_minutes` (est_minutes ≥ 1 by schema, so no divide-by-zero).
   Reported per entrypoint and overall as median/mean/p10/p90, with `under_est_count` (ratio > 1.0)
   and `over_est_count` (ratio ≤ 1.0).

4. **Degenerate short runs are excluded from calibration aggregates and counted, never dropped
   silently.** A finished task whose `actual_active_minutes < degenerate_floor_min` (default **2.0**)
   is classified `degenerate` (a crash/first-sleep death mismarked terminal — cf. the empty-TB
   died-in-first-sleep signature) and excluded from every ratio statistic, but its count surfaces as
   `excluded_degenerate`. This is a **no-silent-truncation** invariant: the excluded count is always
   printed even when zero. `degenerate_floor_min` is a module constant (not a CLI flag in v1).

5. **Cost attribution by active slot-minutes (section B).** For each billed instance (`cost_usd` NOT
   NULL AND > 0), attribute its realized `cost_usd` across the tasks that ran on it, weighted by
   `task_active_slot_minutes = actual_active_minutes × task.slots` restricted to segments on **that**
   instance (a task's `events.instance_id` ties each segment to its box). A task's attributed cost =
   `cost_usd × task_share`. The per-box residual `cost_usd − Σ attributed` is `unattributed_idle`
   (provisioning, gaps between tasks, post-run idle before teardown). Fleet `idle_fraction =
   Σ unattributed_idle / realized_total`. An instance with realized cost but **zero** attributable
   task-minutes contributes its entire `cost_usd` to idle (a box rented and torn down without ever
   running a task — pure waste, must be visible).

6. **`cost_per_done_task` and per-group breakdown.** Overall and per `grp`: `done` count and
   `Σ attributed_usd / done_count` (groups with zero done tasks report `usd_per_done_task = null`,
   not a divide-by-zero).

7. **Packing occupancy (section C) is time-weighted, not peak.** For each billed instance, integrate
   `used_slots(t)` over its billed lifetime `[created_at, destroyed_at]` (a step function that rises
   by `task.slots` at each `start` and falls at each terminator on that instance, per invariant 2's
   segments) and divide by `slots_total × lifetime_minutes` to get `occupancy ∈ [0, 1]`. Report the
   mean grouped by `(gpu_name, slots_total)` offer class, plus a fleet mean weighted by box lifetime.
   An instance still `live` (no `destroyed_at`) is excluded from occupancy (no closed lifetime).

8. **Empty / filtered-to-nothing is a valid report, not an error.** Zero matching tasks →
   every section renders with `n=0` and null aggregates, exit code 0. A missing DB file is a clear
   error (exit non-zero) — the tool never creates the DB (contrast `registry_db.connect`, which
   would; see Open question 1).

9. **Determinism.** Same DB + same filters → byte-identical report. No wall-clock in the output
   except the segment-close-at-now for `running` tasks (invariant 2), which is excluded from section
   A (finished-only) so calibration numbers stay reproducible; section C excludes live boxes for the
   same reason.

10. **Validate at the boundary.** Malformed `resource_hint_json`/`args_json` or an unparseable
    timestamp on a row is logged to stderr and that row is skipped (counted in a `skipped_malformed`
    tally in `generated_from`), never crashing the whole report — the registry is the trust boundary
    and a single bad row must not deny the operator the rest of the report.

11. **Backlog window per task (section D).** Every task occupies the fleet backlog for a half-open
    interval `[enter, exit)` weighted by its `slots`: `enter = tasks.created_at`; `exit =` the task's
    **first `start` event timestamp** if it ever ran (i.e. `segments[0].start_dt`), else the task's
    **last event timestamp** if the task reached a backlog-terminal state
    (`{done, task_failed, cancelled, infra_failed}`) without running, else **`now`** (still queued /
    claimed / shipped, never ran). A window with `exit <= enter` (e.g. a task that started in the
    same instant it was created) is dropped. `backlog_slots_at(rent_dt, windows)` sums `slots` over
    windows with `enter <= rent_dt < exit`. This is a **fleet-wide snapshot at one instant**; it does
    **not** filter by resource hint / pinning / which box a queued task was destined for (see Open
    question 5) — an intentional first-cut approximation.

12. **Rent-time proxy = `instances.created_at`; section D covers the same population as section C.**
    Backlog is evaluated at each box's `created_at` (the `rent_intent`/`rent_created` decision is
    within seconds of it and needs event-matching; `created_at` is on the row). Section D includes
    exactly the **billed** (`cost_usd` NOT NULL AND > 0) instances with a **closed** lifetime
    (`destroyed_at` set) — a live box has no `occupancy` (invariant 7) and is excluded here too, so
    `boxes` matches section C's `boxes`.

13. **Idle slot-minutes partition is non-overlapping and sums to total idle.** For each in-scope box
    with `S = slots_total`, lifetime `L` min, capacity `C = S·L`, `used = occupancy·C`, and
    `idle = max(0, C − used)`: let `B = backlog_slots_at(created_at)`, `unbacked = max(0, S − B)`, and
    `over_provision_idle = min(idle, unbacked·L)` (capacity with no queued demand at rent). The
    remainder `idle − over_provision_idle` is `never_shipped_idle` when the box ran **zero** tasks
    (`used == 0`), otherwise `under_pack_idle`. Fleet `idle_partition` fractions are
    `Σ over_provision_idle / Σ idle`, `Σ never_shipped_idle / Σ idle`, `Σ under_pack_idle / Σ idle`
    (each `null` when `Σ idle <= 0`); by construction they sum to 1.0 (± rounding). `idle_slot_minutes
    = Σ idle`.

14. **Dominant reason per box (deterministic) + `by_reason` cost view.** Each in-scope box is
    labelled: `never_ran` if `used == 0`; else `over_provisioned` if `over_provision_idle >
    0.5·idle` (and `idle > 0`); else `under_packed` if `occupancy < 0.5`; else `healthy`. The
    thresholds `0.5` (idle-share) and `0.5` (occupancy) are module constants, not CLI flags.
    `by_reason` reports, per label, the box count and summed realized `cost_usd`, sorted by
    `cost_usd` descending then reason name (deterministic). `boxes_ran`/`boxes_never_ran` are the
    `used > 0` / `used == 0` counts.

15. **`by_offer_class` mirrors section C's `(gpu_name, slots_total)` classes** with, per class,
    `boxes`, `occupancy_mean`, `backlog_slots_at_rent_median` (median `B` over the class's boxes),
    `boxes_never_ran`, and a `verdict` = the dominant reason held by the most boxes in that class
    (tie broken by the fixed order `never_ran > over_provisioned > under_packed > healthy`). Rows
    sort by `boxes` desc, then `gpu_name`, then `slots_total` (matching section C's ordering).
    Empty / no-billed-box input yields `boxes: 0`, null partition fractions, empty lists — a valid
    report, not an error (invariant 8 extends to section D).

16. **Offer-set counterfactual (section E) is realized-premium accounting over the event log.**
    `rents_total` = count of `rent_intent` events in scope. For each `offers_considered` event (its
    detail parsed as the invariant-5d JSON), `premium_dph = max(0, detail.premium_dph or 0)` and
    `reason = detail.cheapest_alt.reason` when `cheapest_alt` is non-null. `rents_with_offer_data` =
    number of parseable `offers_considered` events; `premium_paid_rents` = those with `premium_dph >
    0`. `premium_dph_median` is over the premium-paying rents only (null when none); `premium_dph_total`
    sums premium across all rents with data. `by_reason` groups premium-paying rents by `reason` with
    per-reason rent count and summed `premium_dph`, sorted by `premium_dph_total` desc then reason. A
    malformed/absent `offers_considered` detail is skipped (counted in `skipped_malformed`, invariant
    10), never fatal. **Coverage is always reported**: zero `offers_considered` events → `rents_with_offer_data:
    0`, null medians, empty `by_reason`, a valid report (invariant 8) — the report never conflates
    "no event logged yet" with "no premium paid." Determinism holds (invariant 9): pure over event
    rows, no wall-clock.

17. **Trend mode buckets the append-only registry by task `created_at` (no snapshots).** `--trend`
    partitions finished tasks (`state IN (done, task_failed)`) into calendar windows keyed by
    `created_at`: `--bucket week` → ISO year-week `YYYY-Www`; `--bucket day` → `YYYY-MM-DD`. Each
    window's `runtime` block is exactly `_runtime_section`'s `overall` over that window's tasks (so
    the degenerate rule, finished-only rule, and ratio math are identical to section A — no
    divergent second implementation). `cost.idle_fraction` and `packing.fleet_occupancy` are computed
    over the **instances whose `created_at` falls in the same window** (instances have no task-week; a
    box is attributed to the window it was rented in), reusing `attribute_costs`/`_packing_section`;
    a window with no billed+closed boxes reports `null` for those, not an error. `window.start` is the
    calendar start date of the window (ISO Monday for weeks, the date itself for days). Buckets are
    emitted oldest → newest. `direction.ratio_median` compares the first and last **non-null** bucket
    `ratio_median`: `improving` when the newest is strictly closer to 1.0 than the oldest,
    `worsening` when strictly farther, `flat` when equal, `null` when fewer than two non-null buckets
    exist — the est-defaults feedback loop (roadmap #1) predicts convergence toward 1.0. Determinism
    and the empty/valid-report rules (invariants 8, 9) hold: zero tasks → `buckets: []`,
    `direction.ratio_median: null`, exit 0. `est_minutes` on each task row is the value used at that
    task's add-time, so windowing measures the estimate that was *actually applied*, not a recomputed
    one — which is why no periodic snapshot is needed.

## Fixtures

Golden input/output — a small hand-built SQLite fixture (or an in-memory DB seeded by the test) →
expected JSON report. These BECOME the tests:

- `fixtures/calibration/basic.sql` → `fixtures/calibration/basic.report.json` — 3 instances, ~6
  tasks exercising: one over-estimate (ratio ≈ 0.5), one under-estimate (ratio > 1), one degenerate
  (< 2 min, excluded), one task that ran on **two** boxes (preempt + requeue → two segments summed),
  one box rented-then-torn-down with zero tasks (100% idle), one group with a done task and one with
  none.
- `fixtures/calibration/empty.sql` → `fixtures/calibration/empty.report.json` — no tasks; asserts the
  n=0 valid-report path (invariant 8) and that `excluded_degenerate: 0` is present (invariant 4).
- A unit test per pure helper (`reconstruct_task_actuals`, `attribute_costs`, `occupancy`) with the
  two-segment / zero-task / live-box edge cases from invariants 2, 5, 7.
- **Section D:** a `backlog_slots_at` unit test (half-open containment, task that never ran vs ran
  vs still-queued windows) and a `packing_diagnosis` fixture seeding four deliberately-distinct
  boxes — one `healthy` (small box, backlog ≥ slots, well packed), one `over_provisioned` (big box,
  backlog < slots at rent, ran), one `under_packed` (big box, backlog ≥ slots at rent, ran but low
  occupancy), one `never_ran` (billed + torn down with zero `start` events while backlog existed) —
  asserting the idle partition, `by_reason` counts/cost, and per-class `verdict` (invariants 11–15).
  The `basic.report.json` golden is regenerated to carry the `packing_diagnosis` section.
- **Section E:** an `offer_counterfactual_report` unit test over hand-built events — several
  `rent_intent` + `offers_considered` pairs (one premium via `reliability_below_floor`, one via
  `too_few_slots`, one zero-premium `cheapest_alt=null`, one malformed detail skipped) — asserting
  coverage, `premium_paid_rents`, medians/totals, and the `by_reason` split. Plus a
  `test_offers_section_empty` asserting the zero-coverage valid-report path (invariant 16). The
  dispatcher side has its own `offer_counterfactual` unit tests in `tests/test_dispatcher.py`
  (reliability-drop / too-few-slots / global-cheapest-chosen / fail-open on a bad field).
- **Trend mode:** a `build_trend` test seeding finished tasks across two day-buckets — an early
  window with a poor ratio (est far from actual) and a later window with a near-1.0 ratio — asserting
  per-window `runtime.ratio_median`, the oldest→newest ordering, `window.start`, and
  `direction.ratio_median == "improving"`; plus a single-bucket case asserting
  `direction.ratio_median` is `null` (fewer than two non-null buckets) and an empty case
  (`buckets: []`, invariant 8).

## Open questions

- **[Q1 — RESOLVED, built]** `registry_db.connect` initialises schema on a missing file and opens
  read-write. This tool must be read-only and must NOT create a DB. Resolution (implemented in
  `_connect_ro`): open directly with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` + `PRAGMA
  query_only=ON`, and `SystemExit` if the file is absent — never routing through
  `registry_db.connect`. Test: `test_read_only_never_writes`, `test_connect_ro_missing_db_errors`.
- **[Q4 — RESOLVED, built as `--trend` windowed mode; roadmap #5]** The original resolution left
  trend-over-time to "a future scheduled wrapper." That turned out to be the wrong mechanism: because
  `est_minutes` is persisted per task row and the registry is append-only, the trend is a **pure
  function of the current registry** (bucket by `created_at`), so no scheduler, no persisted
  snapshots, and — unlike snapshots — it works retroactively over all existing history (invariant
  17). `--out` on a cron/loop still works for point-in-time capture but is no longer needed to see the
  trajectory. The `[Q4]` duplicate entry below is superseded by this.
- **[Q2 — RESOLVED, built as section E + task-dispatcher invariant 5d]** The dispatcher now logs an
  `offers_considered` event per rent (the bounded `offer_counterfactual` summary — cheapest rejected
  raw offer, why it was rejected, and the `premium_dph` we paid), and section E aggregates it into a
  realized offer-premium report split by reject reason. This closes the counterfactual gap the
  original text below described. It is answerable for **future** rents only (the event is logged going
  forward); section E reports coverage rather than assuming history. Remaining nuance moved to the new
  Q6 below (the event captures only the cheapest single alternative, not the full offer distribution).
  Original deferral rationale, kept for provenance:

  offer-selection quality is only partially answerable. The
  original motivating question
  includes *"are we picking the ideal box configuration in terms of cost?"* The event log records the
  **chosen** offer (`rent_intent`/`rent_created` detail: dph, gpu, ram, cores, reliability) but **not
  the rejected candidate set**, so this spec can report *realized* efficiency (did we occupy the
  slots / did the cheapest-that-fit rule produce good occupancy per dollar) but CANNOT compute the
  true counterfactual "a cheaper qualifying offer existed and we passed it over." Making that
  answerable needs a new `offers_considered` event logged by the dispatcher at rent time — a
  **dispatcher-spec change**, out of scope here. Decision (2026-07-13, owner): ship the
  realized-efficiency report now (this spec, built) and pursue offer-set logging as a followup spec.
- **[Q3 — RESOLVED, built as a followup spec]** Using historical per-entrypoint actuals to seed
  `est_minutes` defaults (so estimates improve automatically) is specced and built in
  `docs/specs/est-defaults.spec.md` (`fleet/est_defaults.py` — `ceil(p90)` of done-task
  runtime → committed `est_defaults.json` → `runq add` fallback). This spec deliberately stops at
  measurement; the feedback loop lives there.
- **[Q4 — SUPERSEDED]** (Original open question: should the report persist on a schedule for
  trend-over-time?) Resolved above by the `--trend` windowed mode — trend needs no scheduled
  persistence because the registry already holds the history and `est_minutes` is stored per task.
- **[Q5 — section D approximation, open]** `backlog_slots_at` is a **fleet-wide** count that ignores
  resource hints (VRAM/cores per lane), machine pinning, and which box a queued task was actually
  destined for. A task counted as backlog for box X at its rent instant might have been unshippable
  onto X (needs more VRAM/lane than X offers) or already shipped to box Y. This **over-counts**
  fillable backlog, biasing the diagnosis toward `under_pack` and away from `over_provision`. The
  live-data conclusion (backlog ≫ box size, so under-pack/never-ship dominate) is robust to this
  bias — tightening it to a hint-aware, per-offer-class-eligible backlog is a followup, gated on
  whether the coarse signal proves actionable. Also unresolved: the rent-time proxy is `created_at`,
  not the `rent_intent` event instant (seconds earlier); adopting the event instant needs
  intent→instance matching (a `rent_created` join), deferred with Q2's offer-set logging.
- **[Q6 — section E scope, open]** The `offers_considered` event captures only the single cheapest
  *rejected* offer (bounded event size), not the full offer distribution, so section E answers "did a
  cheaper box exist and why did we skip it" but NOT "how many cheaper boxes" or "what reliability
  would we have needed." It also cannot attribute a premium to a REALIZED outcome — a
  reliability-floor premium is only worth paying if the cheaper low-reliability box would actually have
  failed/preempted; correlating `premium_dph` against realized preemption/infra-failure rates by
  reliability band is the natural follow-up (needs the chosen offer's reliability persisted per
  instance — not on the row today). Ship the realized-premium report now; escalate to distribution +
  outcome-correlation only if the premium proves material.
