# Feature: run registry (central task/run database + `runq` CLI)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (experiment tooling) + `src/shared/infra` (run-identity helper)
- **Module path:** `fleet/registry_db.py`, `fleet/runq.py`, `fleet/run_identity.py`
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/run-registry.spec.md`

## Purpose

One queryable source of truth for every training task: what's queued, claimed, running, done or
failed — where, on which Vast instance, produced by which exact config, at what cost. Agents
coordinate against it instead of grepping `experiments/`: duplicate A/B arms are caught at enqueue
time by config hashing, run status becomes an explicit state machine instead of file-mtime
inference, and instance/cost accounting replaces the free-text `vast_instances.log`. This is the
foundation the task dispatcher (`docs/specs/task-dispatcher.spec.md`) schedules against.

## Input contract

- **Database file** `experiments/runs.sqlite` (SQLite, WAL mode), resolved against the **MAIN
  checkout** — `git rev-parse --git-common-dir`'s parent, falling back to the invoking tree when
  git is unavailable (added 2026-07-09, dispatcher-spec retrospective bug 7): `runq`/dispatcher
  invocations from any `worktrees/*` collapse onto the ONE shared registry instead of
  silently creating a per-worktree one. **The schema is the cross-feature
  contract**: consumers (runq, dispatcher, dashboard) read/write SQL directly; there is no shared
  Python API across features. **Schema v5** (`PRAGMA user_version = 5`); an older on-disk DB is
  auto-migrated forward in place (v1→v2 adds `tasks.job_manifest_json`, v2→v3 adds
  `instances.source`, v3→v4 adds `tasks.code_blob`/`code_sha256`/`code_format`, v4→v5 adds the
  `colocations` table) — only a *newer* on-disk version is refused (invariant 10):

  ```sql
  CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,          -- uuid4
    created_at    TEXT NOT NULL,             -- iso8601 UTC, e.g. 2026-07-07T21:04:00Z
    created_by    TEXT NOT NULL,             -- required actor: --by, else $RUNQ_ACTOR, else the
                                              -- current git branch (invariant 15); never 'unknown'
    grp           TEXT NOT NULL,             -- experiments/<grp>/ destination (CLI flag: --group)
    name          TEXT NOT NULL,             -- lane name; UNIQUE(grp, name)
    entrypoint    TEXT NOT NULL,             -- key into the code-owned entrypoint table
    args_json     TEXT NOT NULL,             -- JSON list: argv passed to the entrypoint
    config_json   TEXT NOT NULL,             -- canonical config JSON (invariant 1)
    config_hash   TEXT NOT NULL,             -- invariant 2
    arm_hash      TEXT NOT NULL,             -- invariant 2
    git_sha       TEXT NOT NULL,             -- HEAD at add time if available, else `""`
                                              -- (provenance only; job-artifact-contract inv. 8)
    slots         INTEGER NOT NULL DEFAULT 1,
    est_minutes   INTEGER NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 50,   -- higher first
    state         TEXT NOT NULL DEFAULT 'queued',
    retries_used  REAL NOT NULL DEFAULT 0,     -- REAL: infra failures cost 0.5 each (dispatcher
                                              -- spec invariant 10, owner directive 2026-07-09)
    max_retries   REAL NOT NULL DEFAULT 10,
    instance_id   INTEGER,                   -- FK instances.id, set at claim
    result_path   TEXT,                      -- experiments/<grp>/<name>, set at done
    resource_hint_json TEXT,                 -- optional {"vram_per_lane_gb","cores_per_lane"}
                                              -- override (dispatcher spec invariant 4a')
    resume_checkpoint  TEXT,                 -- local path to the last-pulled checkpoint, if any
                                              -- (dispatcher spec invariants 9c/16/17)
    job_manifest_json  TEXT,                 -- self-describing job.json (job-artifact-contract)
    updated_at    TEXT NOT NULL,
    UNIQUE(grp, name)
  );
  CREATE TABLE instances (
    id            INTEGER PRIMARY KEY,       -- Vast instance id
    machine_id    INTEGER,
    label         TEXT NOT NULL,             -- always 'runq_<first_task_id>' (dispatcher spec inv. 6)
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL,             -- provisioning|live|draining|destroyed|lost
    dph_usd       REAL NOT NULL,             -- offer dph_total at rent time
    gpu_name      TEXT,
    ssh_host      TEXT, ssh_port INTEGER,
    slots_total   INTEGER NOT NULL,
    hard_cap_at   TEXT NOT NULL,             -- iso8601: destroy deadline
    destroyed_at  TEXT,
    cost_usd      REAL,                      -- dph_usd × rental hours; accrued EVERY poll cycle
                                             -- while live (inv 12b), re-stamped at destroy/lost
    source        TEXT NOT NULL DEFAULT 'vast'  -- 'vast' | 'owned' (owned-box spec)
  );
  CREATE TABLE events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    t           TEXT NOT NULL,
    task_id     TEXT, instance_id INTEGER,   -- either/both nullable
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL                -- human-readable reason, never empty
  );
  CREATE TABLE settings ( key TEXT PRIMARY KEY, value TEXT NOT NULL );
  ```

- **`runq` CLI arguments** — trust boundary; every subcommand validates its inputs (unknown
  entrypoint, malformed group/name, non-positive `--est-minutes` (which is optional — when omitted
  it resolves to a learned/manifest default; see Public API), unknown `--state` filter →
  exit 2 with a message; never a stack trace).
- **Entrypoint run-identity handshake:** each entry in the code-owned entrypoint table
  (`name → argv template + expected completion artifact`) must support a `--print-run-identity`
  flag: parse args exactly as a real run would, print `{"config": <full config tree as JSON>}`
  to stdout, exit 0 — parse-only, no GPU, no filesystem writes. Registered entrypoints: all
  `native` trainers — they adopted the `--print-run-identity`/`--out` contract 2026-07-08 (every
  row in `entrypoints.py` is now `live=True`), so this is no longer an open decision in
  `spec/DECISIONS.md`. (The cwm-era capacity probe was the original v1 candidate; it was retired
  with the cwm track, 2026-07-07.)

## Output contract

- The populated schema above (read by dispatcher and dashboard).
- **`runq` exit codes:** 0 success · 2 validation error · 3 duplicate refused · 4 illegal state
  transition. Machine-readable output: every read subcommand supports `--json`.
- **`run_identity.json`** written by `runq add` into nothing — identity lives only in the DB;
  entrypoints additionally write their existing artifacts unchanged. (The dispatcher copies
  `config_json` + hashes into `experiments/<grp>/<name>/run_identity.json` at pull time so the
  on-disk tree stays self-describing without the DB.)
- **Working-tree snapshot persisted at add.** `runq add`/`submit` also persist a content-addressed
  snapshot of the code tree to `<experiments>/.dispatcher/snapshots/<task_id>.tar.gz` at add time
  (code-snapshot spec), keyed by the new `task_id`, so the dispatcher later ships exactly what was
  on disk when the task was enqueued — no git reachability required. The snapshot carries only code
  bytes; the DB remains the only home for run identity.

## Public API

- **CLI** (`python fleet/runq.py …`, wrapped as `make`-friendly single word `runq` via a
  console alias is NOT in scope):
  - `runq add --group G --name N (--entrypoint E | --job DIR) [--est-minutes M] [--slots K]
    [--priority P] [--max-retries R] [--by WHO] [--force] [--vram-per-lane-gb X]
    [--cores-per-lane N] [--init-from CKPT] -- <entrypoint args…>` (exactly one of `--entrypoint
    <name>` — a named entry in the code-owned table — or `--job DIR` — a directory carrying a
    self-describing `job.json` (job-artifact-contract spec; git-optional, no `entrypoints.py`
    entry needed) — is required; both or neither → exit 2. `--est-minutes` is optional (default
    None): when omitted it falls back to the learned per-entrypoint default (est_defaults) for a
    named add, or `resources.est_minutes` from the manifest for a `--job`; an explicit value always
    wins. `--by` is the required actor label — invariant 15;
    it may be omitted only when `$RUNQ_ACTOR` or the current git branch supplies a usable identity.
    `--vram-per-lane-gb`/`--cores-per-lane` together
    populate `resource_hint_json`; either alone is a validation error — exit 2 — since the
    dispatcher's formula needs both dimensions. `--init-from CKPT` = a home-side checkpoint path
    to seed the task's `resume_checkpoint` (cross-task handoff, e.g. adapt from a pretrain's
    `ckpt_final.pt`); the dispatcher's existing resume path ships it as `resume.pt` and wires the
    entrypoint's resume flag. Requires the file to exist and the entrypoint to declare a
    `resume_flag` — else exit 2.
    `--probe` marks a QUICK TEST OF A HYPOTHESIS — the class of run whose answer blocks a decision,
    so latency matters more than throughput. It sets `priority = PROBE_PRIORITY (90)` when
    `--priority` is not given explicitly (explicit always wins). 90 is not arbitrary: it must exceed
    `DEFAULT_PRIORITY (50)` by more than the dispatcher's `preempt_priority_margin (30)`, or a probe
    could never displace ordinary work (invariant 17a); being `> 50` also exempts it from invariant
    4d's backlog bar, so it may rent immediately instead of waiting for a queue to build. The
    displaced victim checkpoints, requeues, and relocates on its own — renting a Vast box when the
    owned boxes are full, since a long victim clears the backlog bar by its own `est_minutes`.
    **Rejected with exit 2 when `est_minutes > PROBE_MAX_MINUTES (30)`** — a long job is not a probe,
    and because a probe EVICTS running work, mislabelling a sweep as one costs real jobs hours. The
    bound is checked after `est_minutes` resolution (it may come from est_defaults or the manifest,
    not just `--est-minutes`).)
  - `runq submit <CODE_TAR_GZ> --group G --name N [--est-minutes M] [--slots K] [--priority P]
    [--max-retries R] [--by WHO] [--force] [--vram-per-lane-gb X] [--cores-per-lane N]
    [--init-from CKPT]` — enqueue a **pre-built**, git-free artifact: a gzipped code tar
    (git-archive layout, `job.json` at the root). Nothing is run locally and no git checkout is
    required; instead of the `--print-run-identity` handshake, dedupe is content-addressed over
    `(code_hash, run, args)` (job-artifact-contract inv. 12). `--by`/actor validation and
    `resource_hint`/`--init-from`/`--est-minutes` semantics match `add`.
  - `runq ls [--state S]… [--group G] [--json]`
  - `runq show <task-id> [--json]` (task row + its events)
  - `runq cancel <task-id> --reason "..."` (**`--reason` REQUIRED**, invariant 4a')
  - `runq dupes (--of <task-id> | --hash <config_hash>) [--arm]`
  - `runq rate [--json]` — current committed spend rate (invariant 11)
  - `runq spend [--since ISO] [--json]` — realized cost from `instances.cost_usd`, INCLUDING
    live boxes clipped to the window (invariant 12b)
- **`fleet/run_identity.py`** (importable by any entrypoint, stdlib only):
  - `canonical_config(cfg: dict, exclude: frozenset[str]) -> str`
  - `config_hash(cfg: dict) -> str` and `arm_hash(cfg: dict) -> str`
  - `EXCLUDE_PATHS: frozenset[str]`, `SEED_PATHS: frozenset[str]` (code-owned defaults)
- The SQL schema (this spec) — consumed by `docs/specs/task-dispatcher.spec.md` and (read-only)
  by the dashboard.

## Dependencies

- stdlib only (`sqlite3`, `json`, `hashlib`, `uuid`, `subprocess` for `--print-run-identity` and
  `git rev-parse`). No `src/cwm` / `src/native` imports.
- Registered entrypoints' CLIs (the handshake above) — the only coupling to training code.

## Behavior & invariants

1. **Canonicalization** (`canonical_config`): deep-strip every dotted path in `exclude` from the
   config tree; recursively drop dicts left empty by stripping; serialize with
   `json.dumps(cfg, sort_keys=True, separators=(",", ":"))`. `EXCLUDE_PATHS` holds the
   non-semantic fields — output locations, display names/tags, logging sinks, device placement —
   defaults live in code; the *criterion* is: a field is excluded iff changing it cannot change
   the science of the run.
2. **Two hashes.** `config_hash` = first 16 hex chars of sha256 of the canonical JSON (seed
   INCLUDED). `arm_hash` = same but with `SEED_PATHS` also stripped — two tasks are the same A/B
   arm iff `arm_hash` matches. 64 bits is ample at this scale; collisions are not handled.
3. **Dedupe at add:** if a task with the same `config_hash` exists in any state other than
   `cancelled`, `task_failed`, `infra_failed` → refuse (exit 3) naming the clashing task, unless
   `--force`. If the `config_hash` is new but the `arm_hash` matches existing tasks → proceed but
   print a warning listing them (same arm, different seed — usually intentional).
4. **State machine** (only these transitions are legal; anything else → exit 4 / dispatcher bug):
   `queued → claimed|cancelled` · `claimed → shipped|queued|cancelled|infra_failed|task_failed` · `shipped → running|infra_failed|queued|cancelling|done|task_failed|cancelled`
   · `running → done|task_failed|infra_failed|preempting|cancelling` · `infra_failed → queued` (requeue,
   `retries_used` incremented) · `preempting → queued|cancelling|done|task_failed|infra_failed` (a requeue
   to `queued`/`cancelling` leaves `retries_used` UNCHANGED — preemption is not a failure;
   dispatcher-spec invariant 17 — but a preempted task that never checkpoints keeps running to its
   natural outcome, so it may instead reach `done`/`task_failed`/`infra_failed` directly) · `shipped → queued`
   (over-pack unschedule, `retries_used`
   UNCHANGED — the box couldn't host it, not a task failure; dispatcher-spec invariant 19h) ·
   **`shipped → done|task_failed|cancelled`** (2026-07-29 owner directive — a task may terminate
   without ever being OBSERVED to start. `_pull_markers` already queries `shipped` tasks and can
   drive all four terminal outcomes for them, but only `→ queued` was legal, so a DONE/FAILED_*/
   CANCELLED marker silently no-op'd and the task stuck in `shipped`, an OPEN state, holding its
   slot forever. Reaching `running` requires seeing a `start` row in the box's PULLED
   `worker.jsonl`, and every reason that pull can lag — the serial ship path starving
   `_ingest_and_complete`, a transient rsync failure, an on-box worker restart truncating the file
   — turned a missed OBSERVATION into permanently lost WORK. Live 2026-07-29 on owned box -1: the
   local copy ran 52 min stale, three tasks finished with DONE markers and `exit rc=0` in the
   worker's own log, and all three still read `shipped`. The marker is ground truth about what the
   box did; our bookkeeping gap must not discard a real outcome. `_complete_done` still gates on
   the completion artifact being present, so this ADMITS a completion and never invents one — a
   DONE marker with no artifact still lands in `task_failed`.) ·
   `cancelling → cancelled` (terminal; dispatcher-spec invariant 18). A box destroyed under a
   **claimed** task (before it ships) is an infra failure just like a shipped/running one, so
   `claimed → infra_failed` is legal too (then `infra_failed → queued` requeues at half a retry). A
   **claimed** task whose code won't Cython-compile because of a *compile error* (a code bug the
   compiler rejects — not a toolchain/ABI limit, which falls back to source) is failed fast at ship
   time, so `claimed → task_failed` is legal too (terminal, no auto-requeue; task-bundle invariant 6).
   `runq` may only perform `add` (→`queued`) and `cancel`; all other transitions
   belong to the dispatcher.
   **Cancel-in-flight (invariant 4a):** `runq cancel` cancels a task in ANY non-terminal state.
   `queued`/`claimed` have no running worker → straight to `cancelled`. `shipped`/`running`/
   `preempting` have a live worker → `cancelling` (a *request*); the dispatcher writes a CANCEL
   marker, the worker stops, and the `CANCELLED` marker drives `cancelling → cancelled`. Cancel is
   terminal — unlike preemption it never requeues, and it never increments `retries_used`. This is
   also the supported way to **reconfigure** a stuck/mis-scheduled task: cancel it, then `add` it
   again with the new args/resource hint under a **new lane `name`** — `UNIQUE(grp,name)` is a hard
   constraint a cancelled row still occupies, so the same name can't be reused; config-hash dedupe
   (invariant 3) is separately satisfied because the cancelled task is excluded.
   **4a' — A CANCEL MUST STATE ITS REASON (2026-07-31 owner directive).** `cancel_task(conn,
   task_id, reason)` takes a REQUIRED non-empty `reason`, recorded in the `cancel` /
   `cancel_requested` event detail; `runq cancel --reason` is likewise required, and a blank or
   non-string reason raises `ValueError` before any transition (no partial cancel on refusal).
   **Why it is mandatory rather than optional:** a bare terminal `cancelled` cannot distinguish a
   task STOPPED BECAUSE IT FINISHED ITS BUDGET from one ABANDONED, and those have opposite meanings
   for cost and calibration. Measured cost of the ambiguity, 2026-07-31: a fleet cost review read
   452 cancellations as discarded work and had to be corrected by hand, and any report scoring
   completion as `state='done'` books an entire FLOPs-budgeted workstream (`azsc-*`, whose normal
   completion path IS a cancel) at 0% completion. Forcing it at the only call site is what keeps the
   distinction in the registry instead of in an operator's head. Say WHICH: `"flops budget
   reached"`, `"superseded by <group>"`, `"misconfigured"`, `"scout read KILL"`.
5. **Compare-and-swap transitions:** every state change is a single
   `UPDATE tasks SET state=?, updated_at=? WHERE id=? AND state=?` and the writer checks
   `changes() == 1`; on 0 it re-reads and backs off. This is the claim-atomicity mechanism —
   no table locks, no advisory files.
6. **Event with every transition, atomically:** the `events` insert (human-readable `detail`)
   commits in the same transaction as the state change. `events` is append-only; rows are never
   updated or deleted. Task rows are never deleted either — `cancelled` is a state, history is
   the point.
7. **Concurrency:** the DB is opened with WAL mode and `busy_timeout ≥ 5000 ms` by every consumer;
   all timestamps are UTC iso8601 (`…Z`); writers keep transactions short (single row + event).
8. **Derived, never stored:** an instance's used slots =
   `SELECT COALESCE(SUM(slots),0) FROM tasks WHERE instance_id=? AND state IN
   ('claimed','shipped','running','preempting','cancelling')` — a `preempting` or `cancelling` task
   still physically occupies its slot until the worker's kill sequence completes, so it counts as
   used. No `slots_used`
   column exists, so it cannot drift.
9. **Identity at add:** `runq add` invokes the entrypoint with `--print-run-identity` + the given
   args, canonicalizes/hashes the printed config, and records `git_sha` from `git rev-parse HEAD`
   on a **best-effort** basis — provenance only (job-artifact-contract inv. 8): when the invoking
   tree is not a git checkout it stores `""` (empty, not NULL) and this is **not** an error, since
   ship uses the content-addressed snapshot rather than the sha. A non-zero exit or unparseable
   stdout from the handshake → exit 2 (the entrypoint's own arg validation is thereby enforced at
   enqueue time, before any money is spent).
10. **Bootstrap + forward migration:** if the DB file is absent, any `runq` subcommand creates it
    at the current schema (`user_version = 5`) and continues. An OLDER on-disk `user_version` is
    auto-migrated forward in place — additive and idempotent, never dropping or retyping a column
    (v1→v2 adds `tasks.job_manifest_json`, v2→v3 adds `instances.source`, v3→v4 adds
    `tasks.code_blob`/`code_sha256`/`code_format`, v4→v5 creates `colocations`) — then stamped to 5.
    A migration step uses the SAME statement the fresh-DB path runs, so the two cannot drift. Only
    a NEWER on-disk version (a downgrade) is refused with a message (the schema is from the future).
    `.gitignore` gains `experiments/runs.sqlite*` (db + `-wal` + `-shm`).
11. **`runq rate`** = `SELECT COALESCE(SUM(dph_usd),0) FROM instances WHERE state IN
    ('provisioning','live','draining')` — the committed $/hr the dispatcher gates against.
11b. **`runq spend` COUNTS LIVE BOXES (invariant 12b, 2026-07-31).** `spend(conn, since)` sums the
    stamped `cost_usd` of boxes with `destroyed_at >= since`, PLUS every live box's accrual
    **clipped to the window** (`dph × hours(max(created_at, since) → now)`) so a long-lived box
    created before the window cannot dump its whole history into a short one. Terminal rows are not
    clipped — their cost is a single stamped scalar with no start/stop detail to apportion, and they
    are already selected by a terminal timestamp inside the window.
12. **Coexistence:** this registry does not read or replace the per-sweep `registry.jsonl`
    (sweep-supervisor spec) — it keeps its format; a whole sweep can later be one task
    (dispatcher spec).
12b. **`cost_usd` ACCRUES WHILE LIVE, not only at teardown (2026-07-31 owner directive).** The
    dispatcher's `_book_live_costs` phase re-derives `cost_usd = dph_usd × hours(created_at → now)`
    for every non-destroyed box with `dph_usd > 0`, every poll cycle. Idempotent by construction
    (recomputed, never incremented), so repeated cycles and the later destroy/lost stamp all
    converge on the same number; owned boxes (`dph_usd = 0`) book $0 and terminal rows are untouched
    (re-deriving a destroyed box's cost from `now` would inflate it forever).
    **Why:** `cost_usd` used to be written exactly once, at `destroy`/`lost`, so every running box
    read NULL and a spend query saw a spike only AFTER it ended — and a by-creation-date report
    booked a box's whole cost on the day it was CREATED. Measured 2026-07-31: the registry reported
    **$5.59** for the day while the fleet was burning **$0.859/hr ($20.61/day)**, with **$11.05
    already accrued and invisible** across 13 live boxes. A cost control you cannot see until the
    spike is over is not a control.
    ⚠ `_realized_cost` is consequently called every cycle for every live box instead of once per
    teardown, so it parses `created_at` TOLERANTLY and returns $0 (logging `cost_unpriced`) rather
    than raising — the same exception that used to cost one cost stamp would now abort the whole
    poll cycle.
13. **`resource_hint_json`** (optional, set via `runq add --vram-per-lane-gb X --cores-per-lane
    N`) declares a task's own per-lane footprint for the dispatcher's slot-sizing formula
    (dispatcher spec invariant 4a') when it differs materially from the global default — e.g. a
    CPU-core-bound task class. Absent (`NULL`) means "use the global default." This field is
    inert data as far as this spec's own invariants go; it exists here only because `tasks` is
    the one row both `runq` (writer) and the dispatcher (reader) share.
13b. **`colocations` (schema v5) + the `colocate` hint key (2026-08-16).** `resource_hint_json` may
    carry `"colocate": "<group key>"` (`runq add --colocate`, or set per paired seed automatically by
    `runq sweep`), and the `colocations(key, instance_id, pinned_at, pinned_by)` table records which
    box each group was bound to. Placement semantics belong to the dispatcher (its invariant 4g);
    what THIS spec owns is the storage shape and its two rules: the pin is **first-writer-wins**
    (`INSERT OR IGNORE` then re-read, so a race can never split a group across two boxes) and it is
    a property of the GROUP, not of a task — which is why it is its own table. A member can be
    cancelled and re-queued under a new name and must still land on the group's box, and a stale pin
    must be droppable without touching any task row. `runq colocate` is the reader, and it verifies
    against the `start` EVENTS (`boxes_started_on`), never against this table and never against
    `tasks.instance_id`: the pin records what was ASKED, the column records only the LATEST
    placement, and only the event stream records what actually HAPPENED — a task requeued onto a
    second box reads as if it had only ever run on the second, which is precisely the arm worth
    catching since it is also the one with `resumes > 0`.
13c. **The `force_box` hint key (2026-10-02).** `resource_hint_json` may carry `"force_box": true`
    beside an explicit `"box"` (`runq add --box <label> --force-box`, or `resources.force_box` in a
    config's `job` section). Placement semantics belong to the dispatcher (its invariant 4i); what
    THIS spec owns is the storage shape and the submission rule, `registry_db.force_box_error`,
    which both writers call on the FINAL hint: the value must be a JSON boolean, `true` requires a
    `box`, excludes `colocate`, and the box must be an `instances` row with `source='owned'`. A
    hint without the key is never inspected. No schema change — it is a key inside an existing
    JSON column. While a forced task occupies an owned box the dispatcher also keeps
    `capacity_override_i<ID>` in `settings` (box-pause inv. 20e), runtime state in the same family
    as `pause_i<ID>`.
14. **`resume_checkpoint`** (dispatcher-written only, never touched by `runq`) is a plain local
    filesystem path or `NULL`; this spec places no format constraint on it beyond "a path the
    dispatcher itself can read at ship time" (dispatcher spec invariant 16 owns its lifecycle).
15. **Actor is required (`created_by`).** Every task-creating command (`add`, `add --job`, `submit`)
    MUST record a usable actor — the who-launched-this for attribution, "usually a branch or agent
    name" (owner directive 2026-07-15; before it, 402/434 tasks were `unknown` so most work was
    unattributable). Resolution, first non-empty wins: (a) explicit `--by`, (b) `$RUNQ_ACTOR`,
    (c) the current git branch of the invoking tree (`git rev-parse --abbrev-ref HEAD`). The
    resolved label is then validated: after stripping, it must be non-empty, match
    `[A-Za-z0-9][\w./+-]{0,63}` (no whitespace/control chars), and NOT be a non-identifying default
    — the reserved set `{unknown, master, main, HEAD}` (case-insensitive; `HEAD` is what
    `--abbrev-ref` prints on a detached checkout). A resolved label that fails validation → **exit
    2** with a fix-it message naming which source produced it and telling the caller to pass
    `--by <branch-or-agent-name>`. There is no `$USER` fallback and no silent `unknown` — the
    pre-directive default is deliberately removed. `git` being absent is not itself an error (source
    (c) simply yields nothing); it only means (a) or (b) must supply the identity.

## Fixtures

Golden input/output — these become the tests:

- `fixtures/registry/canonical.json` → input
  `{"seed": 3, "train_ratio": 1, "world_model": {"deter": 256, "lr": 0.0003}, "run": {"out_dir": "/tmp/x", "name": "tr1"}}`
  with `exclude = {"run.out_dir", "run.name"}`, `SEED_PATHS = {"seed"}` →
  canonical `{"seed":3,"train_ratio":1,"world_model":{"deter":256,"lr":0.0003}}` (note: `run`
  dropped entirely once emptied), `config_hash = 57d6dbe210d09c26`;
  arm canonical `{"train_ratio":1,"world_model":{"deter":256,"lr":0.0003}}`,
  `arm_hash = 557b4ae023746c06`. Key order of the input MUST NOT affect either hash.
- **Dedupe:** `runq add` the same entrypoint+args twice → second exits 3 naming the first task.
  Same args but `--seed 4` appended → exit 0 with an arm-duplicate warning listing exactly the
  first task.
- **Claim race:** two connections concurrently attempt invariant-5 CAS `queued → claimed` on the
  same task → exactly one observes `changes() == 1`; the tasks table holds one `claimed` row and
  exactly one `claim` event exists.
- **Cancel-in-flight:** `runq cancel` on a `running` task → the task goes to `cancelling` (exit 0,
  "cancellation requested"), a `cancel_requested` event is logged, and a later `cancelling →
  cancelled` completes it; `runq cancel` on a terminal task (`done`/`cancelled`/…) → exit 4, no
  change.
- **Preempt requeue vs infra-failure requeue:** a `running` task CAS'd to `preempting` then to
  `queued` ends with `retries_used` unchanged from before the preemption; the same starting task
  taken `running → infra_failed → queued` instead ends with `retries_used` incremented by 0.5 —
  an infra failure costs half a retry (dispatcher spec invariant 10) — the two paths must be
  distinguishable by their effect on this column alone.
- **Rate:** instances `(live, 0.212)`, `(live, 0.298)`, `(destroyed, 9.99)` → `runq rate` = `0.510`.
- **Bootstrap:** run `runq ls` against a missing DB file → creates schema v3, exits 0, empty list;
  re-running against the created file is byte-identical output (idempotent).
- **Handshake failure:** `runq add` with args the entrypoint rejects (`--set bogus.key=1`) →
  exit 2, no task row, no event.
- **Actor required (invariant 15):** `runq add … --by master` (or `--by unknown`, or `--by ""`
  whitespace-only) → exit 2, no task row. `--by feat-x` records `created_by = "feat-x"`. With no
  `--by`, `RUNQ_ACTOR=some-agent` in the env → `created_by = "some-agent"`; explicit `--by` beats
  the env. When `--by` is absent, `$RUNQ_ACTOR` is unset, and the branch is non-identifying
  (`master`/detached) → exit 2 with a message telling the caller to pass `--by`.

## Open questions

None blocking. Resolved at draft time (owner may veto):
- **Hash truncation** → 16 hex chars (64 bits) for both hashes; full digests are recomputable
  from `config_json` at any time.
- **Who writes `run_identity.json` on disk** → the dispatcher at pull time (keeps entrypoints
  untouched beyond the `--print-run-identity` flag).
- **`cwm.evaluation.summary.config_hash` migration** → out of scope (and moot since the cwm
  track retirement, 2026-07-07).

Cross-spec (resolved): all `native` trainers adopted the `--print-run-identity`/`--out` contract
2026-07-08 (every row in `entrypoints.py` is `live=True`); no longer an open decision.
