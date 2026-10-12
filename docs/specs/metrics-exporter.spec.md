# Feature: Metrics exporter — the registry's state as Prometheus text

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (fleet coordinator)
- **Module path:** `fleet/metrics_exporter.py`
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/metrics-exporter.spec.md`
- **Related:** `docs/specs/task-dispatcher.spec.md` (invariants 22, 23, 25 and 31: what a box
  reading is and where it is kept), `docs/specs/free-space-guard.spec.md` (the data root's
  reading and the hold), `docs/specs/box-pause.spec.md` (the pause rows),
  `docs/specs/run-registry.spec.md` (the schema)

## Purpose
The registry already holds what a status system needs: the queue, each box's last reading, how
long each poll cycle took, how much of the data root is free. It holds them as rows. "How deep was
the queue at three in the morning" and "tell me when the dispatcher stops cycling" are questions
about a time series, and every answer so far has been a query somebody wrote by hand after the
fact.

The exporter serves the registry's CURRENT state in the Prometheus text format. A site that runs
Prometheus scrapes it and gets history, dashboards and alert rules from tools made for that. A
site that does not run it loses nothing: the dispatcher never reads anything back.

## Where it runs
Its own process, wherever the registry file can be read:

    python fleet/metrics_exporter.py --db <data root>/runs.sqlite --host 0.0.0.0 --port 9108

It holds no credentials, makes no connection to a box or to a rental market, and needs nothing but
the standard library. Run it with a read-only view of the data root. With such a view it reads the
registry while some process holds it open for writing, which the dispatcher does for its whole
life. What it answers with no writer depends on how the writer left. After a clean close SQLite
has removed its two side files, a read-only view cannot make them, and the scrape is the error
below. After a kill (the usual way a dispatcher stops: it has no shutdown path) the side files
stay, and the exporter goes on answering with the last numbers written. So **an answering exporter
does not mean a running dispatcher.** That question has its own series: the age of
`fleet_poll_cycle_timestamp_seconds`.

The route has no authentication, like any Prometheus exporter: `--host` decides who can read it.
The default is loopback. What it serves is in the table below and in invariant 7.

## Input contract
- `--db PATH`: the registry. Default: `runs.sqlite` under the shared experiments root, as every
  other reader resolves it.
- `--host` (default `127.0.0.1`), `--port` (default `9108`).
- `--lookback N` (default 20000): how many of the newest event rows are read once, at start, to
  find the newest poll cycle.
- `--once`: print one scrape to stdout and exit (0 on success, 1 when the registry cannot be read).
- **Trust boundary:** the registry's rows. A row whose JSON does not parse, or a number that is
  not one, costs only that value (it is left out); it never fails the scrape.

## Output contract
`GET /metrics` (and `HEAD`): text format 0.0.4. Any other path: 404. A registry that cannot be
opened or read: **500 with no metrics text**, so a scraper records the source as down instead of
reading stale numbers.

Every metric is a gauge unless marked counter. A value that was not measured is ABSENT, never 0.

| metric | labels | what it is |
|---|---|---|
| `fleet_tasks` | `state` | tasks in each state; every state of the state machine is present, at 0 when empty, and a state the table holds beyond those is served under its own name |
| `fleet_queue_oldest_seconds` | | the longest any `queued` task has waited since it entered the queue (a requeued task waits from its requeue); 0 with an empty queue |
| `fleet_poll_cycle_timestamp_seconds` | | when the newest `poll_cycle` event was written |
| `fleet_poll_cycle_seconds` | | that cycle's `total_sec` |
| `fleet_poll_phase_seconds` | `phase` | that cycle's time in each phase |
| `fleet_data_root_free_bytes` | | free space the dispatcher read on that cycle |
| `fleet_data_root_holding` | | 1 while the free-space guard holds, else 0 |
| `fleet_box_info` | `label`, `instance_id`, `source`, `state`, `gpu` | 1 for each box that is not destroyed or lost; `gpu` is `yes` when the box is registered with one |
| `fleet_box_slots` | `label`, `instance_id` | the box's registered slots |
| `fleet_box_paused` | `label`, `instance_id`, `mode` | 1 for a box with a pause row |
| `fleet_box_measured_timestamp_seconds` | `label`, `instance_id` | when the box's reading was taken |
| `fleet_box_cores`, `_load1`, `_cpu_quota_cores`, `_cpu_used_cores` | same | the reading's CPU fields |
| `fleet_box_ram_total_bytes`, `_ram_avail_bytes`, `_mem_limit_bytes`, `_mem_used_bytes`, `_mem_anon_bytes` | same | its memory fields |
| `fleet_box_vram_total_bytes`, `_vram_used_bytes`, `_gpu_util_ratio`, `_gpu_mem_util_ratio`, `_gpu_count` | same | its GPU fields; the two ratios are 0 to 1 |
| `fleet_box_gpu_temperature_celsius`, `_gpu_power_watts`, `_gpu_power_limit_watts` | same | the GPU's temperature, draw and power limit |
| `fleet_box_gpu_throttled` | same + `reason` | 1 while the GPU reports that reason for running below its clocks, else 0: `power_cap`, `hw_slowdown`, `thermal`, `hw_thermal`, `power_brake` |
| `fleet_box_pressure_waiting_seconds_total` (counter) | same + `resource` | time some task on the box's HOST waited for `cpu`, `memory` or `io` (the machine's, not the container's: task-dispatcher inv. 31b says why) |
| `fleet_box_pressure_stalled_seconds_total` (counter) | same + `resource` | time every task on the host waited, for `memory` and `io` |
| `fleet_box_disk_total_bytes`, `_disk_free_bytes` | same | the filesystem the box's spool is on |
| `fleet_box_slots_effective`, `_tasks_running`, `_tasks_open` | same | the cap the packer used, and the box's tasks, at the reading |
| `fleet_events_total` (counter) | `event` | events appended since the exporter started; the names of invariant 4 are present from the first scrape, at 0 |
| `fleet_registry_last_event_seq` | | the newest event row the exporter has read |

## Behavior & invariants
1. **Read-only.** The registry is opened `mode=ro`. The exporter never changes the registry file
   and never creates one (a missing file is the 500 above), and it holds no lock between scrapes:
   each scrape opens the file, reads, and closes it. One thing can appear beside the registry, and
   it is SQLite's doing: where no other process has the registry open AND the exporter can write
   to its directory, SQLite leaves its two side files there, the log one empty. A read-only view
   rules that out.
2. **The event table is read by its key only.** Its only index beyond the key is on the task and
   the box, so a query by event name alone reads the whole table. The exporter reads `seq > N`:
   the newest `--lookback` rows once at start, then only what was appended since the last scrape,
   in batches of at most 5,000 rows and at most 40 batches (200,000 rows) a scrape. What one
   scrape does not reach, the next ones read. It keeps the newest `poll_cycle` it has seen in
   memory.
3. **Box readings come from the reading rows, not from events.** The dispatcher keeps each box's
   newest reading in one `settings` row (`box_reading_i<ID>`, task-dispatcher inv. 31). The
   exporter reads those rows and nothing else for a box's numbers. A registry written by a
   dispatcher older than inv. 31 has none: `fleet_box_info` is still served, the readings are not.
4. **Counters count from the exporter's start.** The rows the registry held when the exporter
   first looked set the newest poll cycle and are never counted in `fleet_events_total`, however
   many scrapes it takes to read them (a lookback longer than one scrape's 200,000 rows, or a
   first scrape that failed part way). A restart sets the counters to 0, which a rate over a
   counter already allows for. A registry whose newest row is BELOW the exporter's place has been
   replaced by another: the exporter finds its place again as at a first look, counts none of the
   rows that registry holds, and keeps the counts it has. Twelve names are served from the first
   scrape, at 0 until one is appended (`TROUBLE_EVENTS`: `claim_timeout`, `data_root_low`, `dead_worker`, `infra_failed`,
   `lost`, `owned_unreachable`, `rent_failed`, `requeue`, `ship_failed`, `stalled`,
   `task_failed`, `undeliverable`): a counter that first appears at 1 shows a rate no increase, so
   without the 0 the first failure after a start would be invisible to a rule over it.
5. **Every reading carries its time**, so a consumer can refuse to show an old one as current. The
   exporter itself serves a reading however old it is.
6. **A box that leaves takes its series with it.** Only instances whose state is neither
   `destroyed` nor `lost` are served. A reading row whose box is gone is ignored.
7. **Labels carry registry names and nothing else:** a box's `label`, its id, its `source`, a task
   state, an event name, a phase name. A state, event or phase name that is not
   `[a-z0-9_]{1,48}` is counted under `other`. No task name, group, entry point, argument or path
   is ever a label or a value: arguments can hold secrets. (The reading rows hold none of these:
   the dispatcher leaves the two fields that name somebody's work out of them.)
8. **One scrape is one snapshot:** its queries run in a single read transaction.
9. **No dependency** beyond the standard library, and no import of the dispatcher (which would pull
   in its whole module to read five tables).

## Fixtures
`tests/test_metrics_exporter.py` builds a small registry with the real schema and asserts the
EXACT text served for it: every state at its count, the oldest queued task's age against a fixed
clock, the newest of two poll cycles, a box with a full reading, a box with a GPU-less reading
(GPU fields absent), a paused box, a destroyed box (absent), an unparseable reading row (that box
has `fleet_box_info` and no reading), event counts after rows are appended, an odd event name
under `other`. And: the event table is never queried without a `seq` bound (the statements are
recorded and checked), and every statement of a scrape is inside its one transaction; a missing
registry is a 500 and no file is made for it, in a directory it could write to; the connection a
scrape uses refuses a write; the file is byte-identical after a scrape; from a directory it
cannot write to, it reads a registry a writer holds open (rows written since included), answers
an error after the writer closed cleanly, and goes on answering after the writer was killed (these
two are skipped when the suite runs as root, which can write anywhere); a lookback longer than
one scrape's rows counts none of them; a registry replaced by another is a first look again; the
twelve names are at 0 from the first scrape and every one is logged by the coordinator as an
event; a number too large for a float costs only itself; `HEAD` answers as `GET` does, without
the text.

## Open questions
None.
