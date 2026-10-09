# Feature: `experiments/` retention — TTL + per-category byte budget, FIFO eviction

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (fleet coordinator tooling — but see *Where it runs*)
- **Module path:** `fleet/prune_experiments.py`
- **Status:** built <!-- draft → approved → built -->
- **Spec file:** `docs/specs/experiments-retention.spec.md`
- **Related:** `docs/specs/run-registry.spec.md` (task states), `docs/specs/ship-artifact-build.spec.md`
  (inv. 12a — the ALREADY-EXISTING snapshot GC this feature deliberately does not duplicate)

## Purpose
`experiments/` grows without bound. Measured 2026-08-09: **237 GB across 914 run dirs**, growing
**15.1 GB/day** (14-day mean; 23–35 GB/day over the preceding three days as model widths rose).
Nothing has ever pruned it — the only retention that exists anywhere in the fleet is
`dispatcher._gc_code_snapshots` (code tarballs, 72 h) and `spool_worker._prune_blobs` (box-side
blob LRU), neither of which touches a single checkpoint.

This feature bounds the tree against an operator-set **byte budget** (default 200 GB) using two
mechanisms in series, because neither alone is sufficient:

- **TTL** matches how the value of an artifact actually decays, and is what does the deleting in
  the normal case. It bounds *age*, not *bytes* — during the observed 3-day burst a 7-day TTL
  would hold ~245 GB, over budget.
- **Per-category budget + FIFO eviction** guarantees the bound the operator asked for. It is a
  backstop, not the primary policy: on its own it evicts hours-old artifacts of a campaign that is
  being actively read, precisely when they are most wanted.

A **`KEEP` marker** is the escape hatch. It is deliberately *protection from eviction*, never a
*requirement to keep*: the decision to investigate a run is nearly always made **after** its result
is read (the repo's own pattern is `runq add --probe --init-from experiments/<run>/<arm>/ckpt_…`),
so a declare-at-queue-time policy would be defensively applied to everything and save nothing.

### What measurement established (2026-08-09) — the basis for the tiers below

| claim | measured |
|---|---|
| checkpoints are ~all of the bytes | 218 GB of 237 GB (91%), 7,976 files |
| the scientific record is nearly free to keep forever | TB + `results.json` + logs + filmstrips = **4.4 GB for all history**, ~0.1 GB/day |
| `ckpt_latest.pt.prev` is **not** a duplicate of the head | only ~1% byte-identical; median mtime gap **746 s** — it is ~12 min of training behind |
| `ckpt_latest.pt` is **not** a copy of the substrate ckpt | 0 of 1744 pairs even match in size; where a substrate sibling exists it is a **stub** (median 0.0 MB, 88% < 1 MB) |
| cancelled/failed runs hold real bytes | **72.5 GB** across 976 dirs that produced no result |
| weights are **not** trivially recreatable | re-run costs median **180 min**, p90 520 min of fleet time |
| code snapshots **are** trivially recreatable | 1121/1123 `git_sha` still reachable; `git archive <sha>` in seconds — and they are **already** GC'd at 72 h |

## Where it runs
**On demand, or on a site's own timer. This feature is NOT wired into the dispatcher's periodic
loop** and adds no coordinator state (operator decision, 2026-08-09: "keep everything on this
desktop, not on the coordinator"). It is a standalone CLI plus a `make prune` target, run by a human
against the shared experiments root — or by a site on a timer, in a service beside the coordinator
that runs the same command with `--apply`. The second form exists because the first was not enough:
a data root nobody pruned for three weeks filled and took the fleet down with it (the incident in
`free-space-guard.spec.md`, which covers what the dispatcher does when retention has not run).
Invariant 12 below is what makes either form safe while the fleet is live.

## Input contract
- **CLI (trust boundary):**
  `python fleet/prune_experiments.py [--apply] [--root PATH] [--budget-gb N]
   [--ttl-days CATEGORY=N ...] [--cat-budget-gb CATEGORY=N ...] [--min-age-hours H] [--json]`
  - `--root` defaults to `registry_db.shared_experiments_root()`. Validated: must be an existing
    directory containing `runs.sqlite`, else exit non-zero without touching anything.
  - `--budget-gb` (default **200**) is the whole-tree target, reported against. Per-category
    budgets are what is actually enforced; they are validated to sum to ≤ `--budget-gb`.
  - `CATEGORY` must be one of the governed names in *Categories*; an unknown name is a validation
    error (exit non-zero), never a silent no-op.
  - `--min-age-hours` (default **6**) is a floor applied to every category (invariant 11).
- **Registry (read-only):** `<root>/runs.sqlite` via `registry_db.connect`. This feature **never
  writes to the registry** — no rows, no events, no schema change.

## Output contract
- **Default is a dry run.** Prints a per-category table — current files/bytes, bytes TTL would
  reclaim, bytes the budget would additionally reclaim, resulting bytes vs budget — plus the
  whole-tree total against `--budget-gb`. Deletes nothing. `--json` emits the same as one JSON
  object on stdout.
- **`--apply`** performs the deletions and appends one JSON object per deleted path to
  `<root>/.retention/prune.log` (JSONL: `t`, `path`, `category`, `bytes`, `reason` ∈
  {`ttl`, `budget`}, `age_days`). The audit log is the record of what a deletion actually removed;
  it is never pruned by this tool.
- Exit 0 on a completed run (dry or applied); non-zero only on a validation/IO failure.

## Public API
Module-internal only — a CLI. `classify()`, `plan()` and `Candidate` are importable by the test
suite but are not a cross-module contract.

## Dependencies
- `fleet/registry_db.py` — `shared_experiments_root()`, `connect()`, and the task `state`
  vocabulary. Public API of the run registry; no internals consumed.

## Categories

Every regular file under `<root>` is classified into exactly one category. A file is a
**checkpoint** iff its basename starts with `ckpt`.

| category | definition | governed | TTL default | budget default |
|---|---|---|---|---|
| `record` | any non-checkpoint file — TB events, `results.json`, `curriculum.log`, filmstrips | **no** | ∞ | — |
| `prev` | basename is exactly `ckpt_latest.pt.prev` | yes | **2 d** | **20 GB** |
| `dead_weights` | a checkpoint whose owning task state ∈ {`cancelled`, `task_failed`, `infra_failed`} | yes | **7 d** | **30 GB** |
| `live_weights` | any other checkpoint (`done`, or no task row) | yes | **90 d** | **130 GB** |

Classification order is `prev` → `dead_weights` → `live_weights`: a `.prev` inside a cancelled
run's dir is a `prev` (2 d), because it is the more redundant artifact of the two and must not be
retained on the longer clock.

**Excluded from the scan entirely** (neither governed nor reported as prunable): `<root>/.dispatcher/**`
— snapshots, blobs, staging and compile caches are governed by the dispatcher's own GC — `<root>/.retention/**`,
`runs.sqlite*`, and files directly in `<root>` (logs from pre-fleet manual runs).

**Budget arithmetic.** 20 + 30 + 130 = 180 GB governed. The ungoverned remainder is `record`
(4.4 GB today, ~0.1 GB/day) plus `.dispatcher` (~15 GB, held flat by the 72 h snapshot GC) ≈ 20 GB,
for ~200 GB total. At the measured 3.78 GB/day of surviving weight traffic, the 130 GB
`live_weights` budget binds well before its 90-day TTL and yields **~34 days** of final-weight
history. That is a real cost of the 200 GB cap and is reported, not hidden: the summary prints the
implied retention window per governed category.

## Behavior & invariants

Acceptance criteria — numbered and testable.

1. **Dry run by default.** Without `--apply` no file, directory, or DB row is modified. The
   printed plan is byte-identical in content to what `--apply` would then delete.
2. **`record` is never deleted**, by TTL or by budget, at any age or size.
3. **Two passes, in order.** Pass 1 deletes every governed file older than its category TTL.
   Pass 2 then, per governed category still over its budget, deletes **oldest mtime first** until
   the category is at or under budget. A file already deleted by pass 1 is not counted again.
4. **`mtime` is the single clock** for TTL and for FIFO order — not the registry's `updated_at`.
   A terminal task's files stop changing, so file mtime is the terminal time, and using one clock
   avoids DB/filesystem skew.
5. **Non-terminal tasks are untouchable.** If a directory's owning task is in any of
   {`queued`, `claimed`, `shipped`, `running`, `preempting`, `cancelling`}, no file in that
   directory is a candidate — regardless of age, category, or budget pressure.
6. **`KEEP` protects a subtree.** A file named `KEEP` in a directory, or in any ancestor directory
   up to and including `<root>`, makes every file at or below that directory ineligible. `KEEP` is
   itself a `record` file and so is never deleted.
7. **Symlinks are never followed and never deleted.** `experiments/` contains symlinks into
   worktrees (`native_m2 → worktrees/…`); the scan must not descend through them nor
   classify them.
8. **Directory → task resolution.** A directory maps to a task by `tasks.result_path` (exact,
   after `abspath`), else by `<root>/<grp>/<name>`. An unmatched directory has no state and its
   checkpoints are `live_weights` — the conservative side, since unmatched dirs are pre-fleet
   manual runs.
9. **Audit on apply.** Every deletion appends one JSONL record to `<root>/.retention/prune.log`
   before/at deletion time. A deletion that fails (`OSError`) is skipped and reported, never
   retried and never logged as deleted.
10. **Empty directories are removed on `--apply`** — but only a directory that contains no regular
    files anywhere beneath it after pruning. A directory holding any `record` file survives.
11. **Nothing younger than `--min-age-hours` (default 6) is ever a candidate**, in either pass.
    This is what makes the tool safe to run against a live tree: it cannot race a job that is
    mid-write but whose task row this tool has not yet seen.
12. **Read-only against the registry.** The tool opens `runs.sqlite` and issues `SELECT` only. It
    is safe to run while the dispatcher is live.
13. **Validation at the boundary.** Bad `--root` (missing, not a dir, no `runs.sqlite`), unknown
    category name, negative TTL/budget, or per-category budgets summing above `--budget-gb` all
    exit non-zero with a message and change nothing.

## Fixtures

Golden scenarios, built as a synthetic root in `tmp_path` (a real `runs.sqlite` via
`registry_db.connect` + `INSERT`), one per invariant. These BECOME `tests/test_prune_experiments.py`:

| fixture | asserts |
|---|---|
| `record_never_pruned` | a 400-day-old `results.json` and `events.out.tfevents.*` survive both passes (inv. 2) |
| `prev_ttl` | `ckpt_latest.pt.prev` at 3 d is deleted, at 1 d is kept (inv. 3) |
| `dead_vs_live_ttl` | identical ckpts at 30 d: deleted under a `task_failed` task, kept under a `done` one (inv. 3, categories) |
| `prev_beats_dead` | a `.prev` in a `cancelled` dir is classified `prev`, not `dead_weights` (classification order) |
| `running_untouchable` | a 400-day-old ckpt under a `running` task is never a candidate (inv. 5) |
| `keep_marker` | `KEEP` at the group level protects an arm dir two levels down (inv. 6) |
| `budget_fifo` | with TTL disabled and a 1-byte budget, files are deleted oldest-mtime-first (inv. 3) |
| `min_age_floor` | a 1-hour-old file over TTL and over budget is still kept (inv. 11) |
| `dry_run_is_inert` | no `--apply`: nothing removed, plan non-empty, and the applied run then removes exactly that set (inv. 1) |
| `symlink_not_followed` | a symlinked dir of ancient ckpts is neither descended nor deleted (inv. 7) |
| `dispatcher_excluded` | an ancient `.dispatcher/snapshots/x.tar.gz` is not a candidate (exclusions) |
| `audit_log_written` | `--apply` writes one JSONL line per deletion with the right `reason` (inv. 9) |
| `empty_dirs_removed` | an arm dir whose only files were pruned is removed; one with a `results.json` is not (inv. 10) |
| `bad_args_rejected` | missing root / unknown category / budgets over total each exit non-zero and delete nothing (inv. 13) |

## Open questions
- None blocking. Two deferred, both recorded rather than guessed:
  - **Champion checkpoints and FIFO.** Oldest-first eviction is backwards for a flagship checkpoint
    that later arms are compared against. The `KEEP` marker is the sanctioned answer for now; an
    automatic "protect the champion" rule would need a definition of champion this repo does not
    currently store. Revisit if a `KEEP` is ever found missing after a real loss.
  - **200 GB is the binding constraint, not the TTL.** Holding the full 90-day `live_weights` TTL
    would need ~340 GB. On the 1 TB coordinator that is affordable; the operator set 200 GB
    knowing it buys ~5 weeks. Raising `--cat-budget-gb live_weights=…` is the single knob.
