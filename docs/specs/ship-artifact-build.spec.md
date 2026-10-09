# Feature: Ship-artifact build — the QUEUER builds, the coordinator is BLIND

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet`
- **Module path:** `fleet/runq.py` (gains the build), `fleet/dispatcher.py` (loses it),
  `fleet/artifact_store.py` (new)
- **Status:** built <!-- draft → approved → built --> — fully implemented 2026-07-31/08-01 on the
  owner's `implement` / `finish inv 11` instructions. Q1 resolved by the owner; inv. 1 completed
  once `backfill_ship_blobs.py` had migrated every open pre-cutover task; **inv. 11 completed
  2026-08-01** with a bundle-format v2 and a capability-gated rollout. 387 tests green.
- **Spec file:** `docs/specs/ship-artifact-build.spec.md`
- **Amends:** `docs/specs/task-dispatcher.spec.md` (resolves the standing *compile cache-key*
  Open question), `docs/specs/task-bundle.spec.md` (compile moves out of the ship path),
  `docs/specs/code-snapshot.spec.md` (snapshot becomes the first half of a build),
  `docs/specs/run-registry.spec.md` (one new task column)

## Purpose

The coordinator currently **builds what it ships**: `_ship` calls `_build_code_tar`, which snapshots
the tree, cythonizes it, caches the result, and overlays the entry module — all inside the serial
`poll_once`, all charged against `ship_budget_sec`. This makes the coordinator an expert in
compilation, and it puts a ~170s cold build on the critical path that also carries ingest, the
reapers, and every checkpoint pull.

This feature moves the build to **the process that queues the job**, and makes the coordinator
**blind**: it receives a ship-ready blob and pushes it. The coordinator gains no knowledge of Cython,
ABIs, compile caches, or entry-module overlays; it knows only "task → blob → rsync".

Correspondingly, **queueing is the only way to build, and building is mandatory to queue.** There is
exactly one funnel (`runq`'s `_finalize_task`, already shared by `add`, `add <dir>`, `submit`, and —
via `cmd_add` — `sweep`), and it does not admit a task without a ship-ready artifact.

### Why this shape, and why now

`docs/specs/task-dispatcher.spec.md`'s Open questions already record the cost and three candidate
fixes: (a) stop charging cold compiles to `ship_budget_sec`, (b) pre-warm the cache asynchronously,
(c) narrow the cache key. **This is a fourth option that subsumes (a) and (b)** — a build that
happens at queue time is never in a ship pass and is by construction pre-warmed — while leaving (c)
optional rather than load-bearing. (c) is the one the spec calls out as dangerous, because a key that
misses an input "silently ships STALE BINARIES … the worst failure class this fleet has". Moving the
build does not require narrowing the key.

Measured 2026-07-31, the state this addresses: every poll from 12:22Z onward exhausted the 300s ship
budget after 4–8 tasks, deferring 18–26 `claimed` tasks each pass; polls ran 16–32 min (median ~21),
so the fleet delivered ~14 tasks/hour against 91 `add`s in three hours. 8 cold compiles at a 168s
median consumed 22 min of coordinator wall-clock. Boxes sat holding `claimed` tasks that were merely
waiting for bytes.

**What that costs, end to end (n=880 task-placements since 2026-07-29):**

| phase | median | p90 |
|---|---|---|
| `claim` → `ship` — waiting for the coordinator to push | **57.8 min** | 197.9 min |
| `ship` → `start` — box extract + deps + launch | 7.5 min | 17.6 min |
| `start` → `done` — actually training | 94.6 min | 268.5 min |

A task spends **~1 hour holding a slot on a paid box before its code arrives**, and only ~59% of its
wall-clock training. Downstream: **59 of 104 rented boxes (57%) ran ZERO tasks** in that window,
$4.26 of pure waste — the fleet rents against a queue it cannot deliver to fast enough, then tears
the boxes down idle. This is the "boxes spend most of their time not running anything" symptom, and
`claim→ship` is where it lives; it is not idleness for want of work, since the queue was never empty.

⚠ **These numbers are a 2026-07-31 BASELINE, not a current reading, and the baseline is moving.**
Two changes landed the same day from concurrent work, both attacking the same phase from inside the
coordinator: `79719d8c` (a compile-cache HIT was 11.55s of pure re-gzip — cache the OVERLAID tree)
and `80432796` (invariant 23d — the ship pass fans out one worker per box). Both independently
reproduce the diagnosis above, which is corroboration rather than duplication. **Re-measure before
implementing this spec**: it is justified by what remains after those land, not by the numbers here.
The live instrument is the dashboard's Delivery-latency panel (`run-dashboard.spec.md` §22/§23) —
read `claim→ship` p50 and `train_frac` there rather than re-deriving them.

## Non-goals

- **Not** a change to what is compiled, how, or with which flags. The build recipe moves verbatim;
  optimising it is a separate question (see Open questions Q5).
- **Not** a change to signing, or to how a bundle is verified. ⚠ Amended by inv. 11 (2026-08-01):
  the bundle format and the box-side worker DID change — a bundle may now reference the box's shared
  code blob instead of embedding it (`bundle_version` 2). The verification model is unchanged: the
  manifest still binds the code by sha256, and the box still checks it before extracting.
- **Not** a change to run identity. `config_hash` / `arm_hash` are unaffected; a build is not an
  input to either.
- **Not** removing the source-shipping code path from the box: `code_format="snapshot"` remains
  reachable via a deliberate `bundle_compile=false` (Q4), just no longer via failure.

## Input contract

**To the build (client side), at queue time:**
- the working tree at `ROOT` (the queuer's own worktree — committed *and* uncommitted, exactly as
  `code_snapshot.make_snapshot` already captures it);
- the resolved `Entrypoint` for this task — from the task's `job_manifest_json` when it carries one,
  else the `entrypoints` table, i.e. `entrypoints.resolve()` unchanged;
- the build recipe: ABI, package list, backend, image, build interpreter (Q2 decides where these
  live).

**To the coordinator, at ship time:** the task row plus a `code_blob` reference. Nothing else. The
coordinator MUST NOT read the build recipe, the snapshot, or any compile cache.

## Output contract

**Artifact store entry** — a ship-ready `code.tar.gz`, byte-identical to what `_build_code_tar`
returns today, content-addressed by the build's full input set:

```
blob_id = sha256( STORE_VER | code_hash | abi | packages_csv | entry_source_path | code_format )
```

`entry_source_path` is in the address because `overlay_entry_source` is **per-task**, not
per-snapshot: one compiled-everything tree serves many tasks, each restoring its own entry module's
`.py`. Cells of one sweep share an entry, so a 12-cell sweep still builds once.

**Registry** — the task row gains:
- `code_blob` TEXT NOT NULL — the `blob_id` above;
- `code_format` TEXT NOT NULL — `compiled` | `snapshot` (the coordinator passes this straight to
  `bundle.build_bundle`; today it is computed at ship time and never persisted).

## Public API

`fleet/artifact_store.py` (new, home-side only — never imported on the box):

```python
def blob_id(code_hash: str, abi: str, packages: list[str],
            entry_source_path: str | None, code_format: str) -> str: ...
def put(experiments_root, blob_id: str, data: bytes) -> Path: ...   # tmp + atomic rename
def load(experiments_root, blob_id: str) -> bytes | None: ...
def gc(experiments_root, live_blob_ids: set[str], keep_max: int) -> int: ...
```

`fleet/runq.py` gains one internal build step inside `_finalize_task`. No new CLI verb: the
existing `add` / `add <dir>` / `submit` / `sweep` surfaces are unchanged from the caller's side.

## Dependencies

- `bundle.compile_tree`, `bundle.overlay_entry_source` — consumed by `runq` now instead of the
  coordinator. Public API of `docs/specs/task-bundle.spec.md`, unchanged.
- `code_snapshot.make_snapshot` — unchanged; becomes the first half of a build.
- `entrypoints.resolve` / `entry_source_path` — unchanged; called client-side, which is safe because
  a manifest job carries its contract in the task row (job-artifact-contract inv. 5/6).
- `registry_db` — one migration for the two new columns.

## Behavior & invariants

1. **The coordinator never compiles.** `_build_code_tar`, `_source_code_tar`, `_compiled_tree`,
   `_compile_cache_key`, `_prune_compile_cache`, `_build_venv_python` and `_log_compile` are deleted
   from `dispatcher.py`, along with all seven `bundle_compile*` settings defaults. `_ship` reads
   `artifact_store.load(task["code_blob"])` and passes the bytes plus the persisted `code_format`
   to `bundle.build_bundle`. **Testable:** `grep -c "compile" dispatcher.py` is 0 outside comments;
   a dispatcher unit test with no Cython installed still ships a task.
2. **Every queued task has a blob, and no task is queued without one.** `_finalize_task` builds
   before `insert_task`, and on build failure inserts nothing (Q1 governs which failures are fatal).
   **Testable:** a registry invariant test asserts no row in any non-terminal state has a null or
   dangling `code_blob`.
3. **One funnel.** All queue surfaces reach the build through `_finalize_task`. Adding a new
   surface that bypasses it is a test failure. **Testable:** a surface-hygiene test asserts every
   `registry_db.insert_task` call site in `fleet/` is inside `_finalize_task`.
4. **The build is per-snapshot, not per-task.** A `blob_id` already in the store is reused without
   rebuilding. **Testable:** queueing a 12-cell sweep from one tree invokes `compile_tree` once.
5. **Immutable publish — FIRST PUBLISHER WINS.** `put` creates the blob if absent and **never
   overwrites** an existing one; it returns the bytes actually published, and `publish` records the
   SHA-256 of *those*, never of what it happened to build. A reader still never sees a partial file.

   ⛔ **This invariant was wrong until 2026-08-14 and it cost a task.** It read "`put` writes
   `.<blob_id>.tmp` then `os.replace`; two queuers racing the same `blob_id` both succeed and the
   store holds one valid blob". That is true of the STORE and false of a BOX. `blob_id` addresses
   the BUILD INPUTS — code hash, ABI, packages, entry path, format — **not the bytes it names**, and
   `build_ship_ready`'s tar.gz is not byte-reproducible, so two builds of one tree mint the same id
   with different bytes. Inv. 6 has the box cache by id and verify by SHA-256, so overwriting mints
   a second byte-different blob under a name a box may already hold, and every later task recording
   the new digest is rejected against the cached copy.

   Live failure (`pcbed_reopen/cb_bias`): two concurrent `runq add` calls on one tree both missed
   `load()`, both built for ~8 min, and the second overwrote the first. The box had already cached
   the first and rejected the second — `sha256 3d68fe6190ff != manifest 9d465425d297`. Note the
   check-then-act window is the BUILD, i.e. minutes wide, so this is not a narrow race.

   **Testable:** two `put`s of differing payloads under one id leave the FIRST payload in the store
   and both calls return it; `publish` racing an already-published id records the *existing* digest.
6. **Validate at the trust boundary (NEW).** The coordinator now ships bytes it did not build, so
   before shipping it MUST verify: the blob exists; its SHA-256 equals its `blob_id`'s recorded
   digest; it opens as a gzipped tar. On failure → `ship_failed` with a loud alert and no push —
   never a silent fallback to source. **Testable:** a corrupted blob fails the task rather than
   shipping.
7. **GC never evicts a live blob.** `gc` is refcounted against tasks in non-terminal states, and
   only then LRU-trims to `keep_max`. This closes a race that exists *today*: the compile cache is
   LRU-capped at 24 with no reference to open tasks. **Testable:** `gc` with a live task retains its
   blob regardless of `keep_max`.
8. **Build cost is visible.** `runq` prints build mode and duration (`built | reused`, seconds), and
   records the same as an `add`-time event so the dashboard keeps its compile timing series. A
   bounded pass is never silent — the same principle `ship_budget_spent` already applies.
9. **The ship budget survives, and should shrink.** `ship_budget_sec` remains (a slow *transport*
   still starves ingest), but with build removed, per-task ship cost is rsync-bound. The setting
   should be re-derived from measurement after cutover, not assumed.
10. **The dev box is a commons.** A build is up to ~170s of 4-way-parallel cythonize
    (`nthreads=4`) on the shared machine, so `runq add` MUST report that it is building rather than
    appearing hung, and MUST NOT be issued in a loop (the existing "sweep, don't hand-loop" rule
    already covers this, and item 4 makes a sweep pay once).
11. **SHIP THE TREE ONCE PER (BLOB, BOX), NOT ONCE PER TASK.** `_ship` currently builds a full
    `bundle.tar` per task into `.ship/<task_id>/` and rsyncs the whole thing to
    `~/spool/incoming/<task_id>/`, so a box running N cells of one sweep receives N copies of an
    identical code tree. **MEASURED 2026-07-31: 496 of 905 task-placements (55%) re-sent a tree the
    box already held**, worst case 16 copies of one tree to the laptop; the compiled tree is
    **36 MB** (the in-code "~11MB" estimate is 3× stale), so this is ~18 GB of redundant rsync since
    07-29 plus N redundant extractions. The blob is already content-addressed, so the fix is
    natural: push `code.tar.gz` to `~/spool/blobs/<blob_id>/` **only if absent**, and make the
    per-task payload just `task.json` + optional resume — kilobytes. The box links or extracts from
    the shared blob. **Testable:** shipping M tasks of one blob to one box performs exactly ONE code
    push; the box-side worker resolves a task whose blob is already present without any code
    transfer.
12. **Staging is temporary and MUST be reclaimed.** `.ship/<task_id>/` is created per ship and
    never deleted — **MEASURED: 2783 dirs, 105 GB, oldest 2026-07-09**, on the coordinator's own
    disk. Delete after a confirmed delivery (or GC by age), and never let a staging leak grow
    unbounded. This is a defect in TODAY's code, not something this feature introduces; it is
    listed here because item 11 rewrites the same code path. **Testable:** after a successful ship
    the task's staging dir is gone; after a failed ship it survives for retry.
12a. **Code snapshots of FINISHED tasks are reclaimed too.** `runq add` persists one working-tree
    tar per task (`code-snapshot.spec.md`) and nothing ever deleted it — **MEASURED 2026-07-31:
    2707 files / 55.5 GB**, of which **2686 (55.0 GB) belong to tasks in a terminal state**, which
    `_source_code_tar` can never read again because a snapshot is only ever loaded to SHIP it.
    Since the queuer now builds the ship-ready blob, a snapshot is not even the shipping path for a
    new task — it is provenance. REFCOUNTED like the blob store: a snapshot whose task is still
    open is retained however old, because that is the copy the legacy ship path reads; terminal
    ones are kept 72 h so a just-failed task can still be inspected, then dropped.
    **Testable:** an open task's snapshot survives an arbitrarily old mtime; a `done` task's is
    reclaimed past the window; a task that failed an hour ago keeps its snapshot.
13. **A FAILED BUILD BLOWS THE WHOLE TASK — no source fallback, ever** (owner, 2026-07-31; Q1).
    Both `bundle.CompileFailed` and `bundle.BundleError` abort the queue operation: `runq` prints
    the compiler's error to stderr, exits non-zero, and inserts **nothing** — no task row, no
    snapshot, no blob. The error is handed back to the session that asked for the dispatch, which
    is the only place it can be fixed. `_build_code_tar`'s `except BundleError → ship source` arm is
    **deleted**, not relocated. **Testable:** with a build that raises either exception, `runq add`
    exits non-zero, the registry gains no row, and nothing is written to the artifact store.
14. **Build ONCE, BEFORE the first insert — a sweep is all-or-nothing.** Because every cell of a
    sweep shares one code snapshot (invariant 4), the build is hoisted out of the per-cell loop and
    runs before any cell is queued. A failing build therefore queues **zero** cells rather than
    stranding a partial grid that must be hand-cancelled. This is a direct consequence of 13 and is
    stated separately because getting it wrong is invisible until a sweep half-lands.
    **Testable:** a 12-cell sweep whose build fails leaves the registry unchanged and exits
    non-zero; one whose build succeeds invokes `compile_tree` exactly once.
15. **A blob that is missing or corrupt at ship time fails the task LOUDLY and names its owner.**
    The coordinator cannot rebuild — it has no toolchain and no source (invariant 1) — so the
    trust-boundary check in invariant 6 transitions the task to `task_failed` with an alert quoting
    the task's `created_by` (already required by `runq --by`). That is the coordinator-side form of
    "hand it back": it cannot fix the problem, so it must name who can. It MUST NOT silently
    re-queue, and MUST NOT fall back to source. **Testable:** deleting a queued task's blob yields
    `task_failed` + an alert containing `created_by`, and no push is attempted.

## Fixtures

- `fixtures/artifact_store_blobid.input.json` → `.output.json` — the `blob_id` of a fixed input set
  is stable across runs and processes, and changes when *any* component changes (one case per
  component: `code_hash`, `abi`, `packages`, `entry_source_path`, `code_format`).
- `fixtures/ship_blind.input.json` → `.output.json` — a coordinator ship pass over a task with a
  prepared blob produces a bundle byte-identical to today's compiled path for the same inputs.
- A migration fixture: a pre-cutover task row (no `code_blob`) is handled per Q3.
- `fixtures/build_failure.input.json` → `.output.json` — invariants 13/14. One case per exception
  class (`CompileFailed`, `BundleError`): `runq add` exits non-zero, stderr carries the compiler
  message, `SELECT count(*) FROM tasks` is unchanged, and the artifact store is untouched. Plus the
  sweep case: a 12-cell sweep whose build raises queues **zero** cells (not 0 < n < 12), and a
  succeeding 12-cell sweep calls `compile_tree` exactly once.
- `fixtures/blob_missing.input.json` → `.output.json` — invariant 15: a queued task whose blob has
  been deleted transitions to `task_failed`, the alert text contains the task's `created_by`, and
  `rsync_push` is never called.

## Implementation status (2026-07-31)

**Landed.** `fleet/artifact_store.py` (new); `runq` builds in `_finalize_task` **before**
`insert_task` and exits **5** with the compiler's error on failure; registry schema **v4** adds
`code_blob` / `code_sha256` / `code_format` (NULL on every pre-cutover row — that NULL *is* the
marker); `dispatcher._blob_code_tar` reads and **validates** the blob at the top of
`_build_code_tar`; `dispatcher._gc_ship_staging` runs as a `poll_cycle` phase.

**A third column beyond the two the Output contract named.** Invariant 6 says the coordinator
re-checks "its `blob_id`'s recorded digest", which only means anything if the digest is *recorded* —
so `code_sha256` joins `code_blob` and `code_format`. `blob_id` addresses the build INPUTS (that is
what makes reuse safe); it cannot double as a content digest, because gzip is not deterministic
across runs, so the two are genuinely different values.

**Inv. 1 LANDED (same evening).** The blocker was pre-cutover rows, and Q3's answer turned out to
need no quiet window at all: the working-tree snapshot `runq add` already persisted **is** the build
input, so `fleet/backfill_ship_blobs.py` produced their blobs home-side, exactly as the
queuer would have. 15 open tasks, 5 distinct snapshots ⇒ 5 builds. Then **286 lines were deleted**
from `dispatcher.py`: `_source_code_tar`, `_compiled_tree`, `_shipped_tree`, `_log_compile`,
`_prune_cache_dir`, `_build_venv_python`, `_resolve_build_python`, `_compile_cache_key`,
`_ship_tree_cache_key`, and both dead `except bundle.CompileFailed` arms. `_build_code_tar` is now a
blob read plus the digest check. A test asserts the surface stays gone, including that
`dispatcher.py` contains no reference to `compile_tree` at all.

Two things moved rather than died, because they are build concerns and the builder still needs them:
`resolve_build_python` / `build_venv_python` (the `uv python find 3.12` + cached Cython venv) are now
`artifact_store` functions. Losing them would have made a fresh machine unable to queue at all —
which the old source fallback used to hide and inv. 13 no longer does. The `compile` EVENT moved too,
so the dashboard's Compilation panel keeps its cold/warm series; it now measures the queuer.

A `code_blob IS NULL` row can no longer be shipped by anyone, so it is failed with an alert naming
`created_by` (Q3 option ii) rather than stranded in an open state.

**Inv. 11 LANDED (2026-08-01).** `bundle.build_bundle(..., code_ref=<blob_id>)` writes a manifest
that binds the code by sha256 under `code_ref` instead of embedding `code.tar.gz`; the box reads it
from `~/spool/blobs/<blob_id>.tar.gz` and **verifies that digest before extracting** — the shared
blob is the one thing the outer tar's own integrity check cannot cover, so it is checked explicitly.
`_ship_io` probes for the blob and pushes it only when absent, so N cells of one sweep cost ONE
transfer to a box instead of N.

**The rollout hinge, which is the whole reason this needed care.** A worker rejects any
`bundle_version` it does not know, so bumping unconditionally would have broken **every live box at
once**. The version therefore bumps *only* for the reference form (`BUNDLE_VERSION = 1` embedded,
`BUNDLE_VERSION_CODE_REF = 2`), and the dispatcher asks each box first: the worker writes
`~/spool/CAPS` containing `blobref` at startup, `_box_supports_blobs` reads it (cached on success,
re-probed on failure so a redeployed worker is picked up without a coordinator restart), and a box
without it keeps receiving self-contained v1 bundles. Rented boxes converge within
`hard_cap_hours`; an **owned box needs its worker restarted once** to gain the capability.

Box-side blobs are pruned on **LRU, not refcount** (`BLOB_MAX_AGE_SECONDS`, 3 days; `unpack_bundle`
touches the blob it reads). Safe by construction: the dispatcher checks for presence before every
ship and re-pushes when absent, so an over-eager prune costs one re-transfer, never a stranded task.
This matters only for owned boxes — a rental is destroyed inside `hard_cap_hours`.

**Test-suite note.** `runq add` now builds, so the four `test_runq*` suites seed
`bundle_compile=false` (the deliberate off-switch, Q4) in their fixtures — otherwise every `add` in
a unit test would run a real ~170s cythonize. Compilation itself is covered by
`tests/test_artifact_store.py`, and the true `BundleError` path end-to-end by
`tests/test_ship_artifact_build.py` via a forced ABI mismatch (milliseconds, no compile).

## Verification status

Built (partial) 2026-07-31 — see *Implementation status*. **448 tests green**:
`tests/test_artifact_store.py` (26 new — the `blob_id` address including one case per input,
atomic publish, refcounted GC, and both failure classes raising with no fallback),
`tests/test_ship_artifact_build.py` (14 new — blob-first read, the trust-boundary checks naming
`created_by`, staging GC, schema-v4 migration of a live v3 DB, and the CLI end-to-end: exit 5 with
zero rows, and a 12-cell sweep queuing ZERO cells), plus `test_dispatcher` (311), `test_registry_db`,
`test_dashboard`, `test_dispatch_surface_hygiene`, and the four `test_runq*` suites.

Not yet exercised on a paid box. The first real ship through the blob path is the thing to watch:
confirm a `compile` event no longer appears for a newly-queued task, and that `claim→ship` p50 on
the dashboard's Delivery-latency panel moves.

## Open questions

**Q1 — RESOLVED 2026-07-31 (owner): a failed build BLOWS THE WHOLE TASK and hands it back to
whoever asked for the dispatch. There is NO source fallback.**
`runq` exits non-zero, prints the compiler's error, and queues **nothing** — no task row, no
snapshot, no blob — for *both* failure classes: `bundle.CompileFailed` (a code bug the compiler
rejects) and `bundle.BundleError` (toolchain/ABI unavailable). See invariants 13–15.

**Why the old fallback existed and why the premise is now gone.** Today the *coordinator* compiles,
so its build toolchain is a **single point of failure for the entire fleet**: a broken build venv
there would have stopped every task from every session, which is exactly why `BundleError` degrades
to shipping source with a loud alert rather than failing. Moving the build to the queuer changes the
blast radius from fleet-wide to **session-local** — a broken toolchain now blocks only the worktree
that has it, while every other session keeps queueing normally. The fallback was buying insurance
against a risk this design deletes, and it was paying for it in the worst currency available: a
silent-ish downgrade to un-hidden, uncached source that re-pays a doomed compile on every ship.

This also makes the error land where it can actually be fixed. Today a compile error surfaces
minutes-to-hours later, on the coordinator, after the task has already claimed a slot on a paid box
— attributed to a daemon nobody is watching. Under this resolution it surfaces **synchronously, in
the terminal of the session that typed the command**, before anything is queued or rented.

**Q2 — Where does the build recipe live?**
The seven `bundle_compile*` values are in the registry `settings` table today. Leaving them there
keeps one source of truth across worktrees and `runq` already opens that DB — but the table is
nominally the coordinator's. Alternative: a committed client config. Recommend keeping them in
`settings` and simply deleting the coordinator's *reads*; that satisfies "the coordinator is blind"
without splitting the ABI across two places.

**Q3 — Cutover for tasks queued before the change.**
Options: (i) cut over only when the queue is empty; (ii) coordinator fails pre-cutover tasks loudly
with a "re-queue me" message; (iii) keep the old build path alive behind a flag until drained.
(iii) contradicts invariant 1 for as long as it lives. (i) is cleanest but needs a quiet window —
the queue was 26 deep on 2026-07-31.

**Q4 — Does the box still accept `code_format="snapshot"`? (narrowed by Q1)**
Q1 removes the only path that produced `snapshot` *by accident*, so the box's source branch is no
longer reachable through failure. It remains reachable only if the recipe deliberately sets
`bundle_compile=false` — a choice, not a degradation, and one nothing currently makes. Remaining
question is therefore just whether to keep that switch at all: keeping it costs an untested branch
on the box; dropping it makes `code_format` a constant and lets the field be retired from the task
row. Recommend keeping the switch (it is the escape hatch if compilation ever has to be turned off
fleet-wide in a hurry) and covering it with one fixture, rather than deleting a lever under time
pressure. Not blocking either way.

**Q5 — Compile flags are OUT OF SCOPE here, and separately unpromising.**
Raised 2026-07-31: could different compile params buy *inference* speed? Recorded so it is not
re-derived. The driver runs `cythonize(..., language_level=3, nthreads=4)` with **no directives**
(no `boundscheck`/`wraparound`/`cdivision`/`infer_types`) and appends `-O0 -g0` after distutils'
`-O2`, deliberately: the in-code comment states it compiles "for CODE-HIDING, not runtime speed" and
that `-O0` roughly halves C-compile time. Restoring `-O2` would therefore *worsen* the very cost this
spec exists to remove, in exchange for optimising generated C that is mostly CPython API calls —
untyped Python through Cython is typically 1.0–1.3× and ≈1.0× when time is spent inside torch
kernels. The safe directives only pay on Cython-typed memoryviews, of which this tree has none. Any
real gain needs static typing in the source, which conflicts with a build that is meant to be
transparent to the code. **Do not spend here without first profiling** what fraction of a training
step is Python-level rather than inside torch; if that fraction is small, no flag can pay.
