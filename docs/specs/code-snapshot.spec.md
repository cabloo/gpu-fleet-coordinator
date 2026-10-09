# Feature: code snapshot — ship the working tree, not a git ref

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet`
- **Module path:** `fleet/code_snapshot.py` (+ wiring in `runq.py` and `dispatcher.py`)
- **Status:** built <!-- draft → approved → built -->  (2026-07-14; approach A locked with the user)

## Purpose

Today the shipped task payload is `git archive <task.git_sha>` — it captures **only committed
files**, requires the sha to be reachable from the coordinator's checkout at ship time, and couples
the whole pipeline to git state (the source of a live-checkout incident). This feature snapshots
**whatever is in the caller's working directory at `runq add` time** (committed *and* uncommitted,
honoring `.gitignore`), persists it content-addressed, and ships that. The caller is responsible for
having their changes present in the folder; nothing needs to be committed first. Works identically
in a git worktree — the snapshot is of the directory you invoke from, with no ref/sha involvement.

This does **not** change the bundle format, the box-side trust boundary, the compile pipeline, or
the registry schema. It changes only *how the code tar is produced* and adds a persisted snapshot
keyed by task id. `git archive` remains the fallback, so already-queued tasks keep working.

## Input contract

- **`make_snapshot(root, *, allow_non_git=True)`** — `root` is a filesystem path to a directory (a
  git working tree — main checkout or worktree — or, when `allow_non_git`, any directory). Internal.
  In a git tree it reads the live bytes of every file `git ls-files -co --exclude-standard` reports
  under `root` (tracked + untracked-not-ignored), i.e. approach A: git is used **only** as a
  `.gitignore`-aware file lister, never for content or sha. When `root` is **not** a git tree and
  `allow_non_git` (default), it falls back to a recursive walk (`_list_files_walk`) that lists every
  file honoring a `.dispatchignore` file plus a fixed default denylist
  (`_DEFAULT_DENY_DIRS`/`_DEFAULT_DENY_SUFFIXES`) — see job-artifact-contract inv 7. git is thus an
  *optional* ignore-lister, never required.
- **`persist` / `load`** — keyed by `task_id` under an experiments-root dir (the parent of
  `runs.sqlite`). Internal.
- **`runq add`** (consumer) — no new CLI surface; it snapshots `runq`'s `ROOT` (the invoking
  worktree) and persists under the DB's parent dir.
- **dispatcher** (consumer) — reads the persisted snapshot for the task it is shipping.

## Output contract

- **`make_snapshot(root) -> SnapshotResult`** with fields: `code_tar` (gzipped tar bytes, members at
  repo-relative POSIX paths — same layout `git archive` produces, so the box extracts + compiles it
  unchanged), `code_hash` (hex sha256, the content address), `file_count`, `total_bytes`,
  `skipped` (count of non-regular entries skipped — symlinks/deleted/special).
- **Persisted artifact:** `<experiments>/.dispatcher/snapshots/<task_id>.tar.gz` (the code tar) and
  `<task_id>.hash` (the `code_hash` string). Written atomically (temp + rename).
- **Bundle `code_format`:** the manifest's `code_format` is `"snapshot"` (or `"compiled"` when the
  compile pipeline runs over a snapshot), vs the legacy `"git-archive"`.

## Public API

Exposed for the two consumers (same-package sibling import, like the other `fleet` modules):

- `make_snapshot(root: str | Path, *, allow_non_git: bool = True) -> SnapshotResult` — dataclass above.
- `snapshots_dir(experiments_root: str | Path) -> Path` — `<experiments_root>/.dispatcher/snapshots`.
- `persist(experiments_root, task_id: str, code_tar: bytes, code_hash: str) -> Path` — write the two
  files atomically; return the tar path.
- `load(experiments_root, task_id: str) -> tuple[bytes, str] | None` — `(code_tar, code_hash)` if a
  snapshot exists for `task_id`, else `None` (never raises on absence).
- `class SnapshotError(Exception)` — raised by `make_snapshot` only when `root` is missing / not a
  directory, or `allow_non_git=False` and `root` is not a git tree (a non-git root with
  `allow_non_git` falls back to the walk instead of raising). `main(argv)` — CLI to snapshot a folder
  and report what would ship.

## Dependencies

- `fleet/registry_db.py` — `shared_experiments_root()` (indirectly, via the callers' DB path;
  this module itself takes the root as a parameter and does no git-sha work).
- `fleet/bundle.py` — unchanged; it consumes `code_tar` bytes identically whether they came
  from `git archive` or a snapshot (compile, overlay, manifest, verify all operate on the tar).

## Behavior & invariants

Numbered, testable.

1. **File set = `git ls-files -co --exclude-standard`.** Tracked files + untracked files that are not
   `.gitignore`d. Ignored paths (`experiments/`, `__pycache__`, `.venv`, `.git`, nested
   `worktrees`, etc.) are excluded because they are gitignored — no denylist on the git path;
   the non-git walk uses a default denylist (`_DEFAULT_DENY_DIRS`/`_DEFAULT_DENY_SUFFIXES` +
   `.dispatchignore`).
   **Exception — the job manifest.** A regular file `job.json` at the snapshot root is always
   shipped, even when the lister excludes it (`.gitignore` on the git path, `.dispatchignore` on the
   walk path). The job-artifact-contract requires the manifest at the tar root (its inv. 10a), and
   the repo-root `job.json` is deliberately untracked (each worktree keeps its own loose copy;
   per-task provenance lives in the registry row + persisted snapshot, not in git). Only the root
   manifest is force-included — nested `job.json` files follow the normal listing rules.
2. **Live bytes, committed or not.** Each shipped file's content is read from disk at snapshot time,
   so uncommitted edits and new (untracked, non-ignored) files are included exactly as they are on
   disk. A staged-but-reverted-on-disk state ships the on-disk bytes.
3. **Deleted-but-tracked and non-regular files are skipped, counted.** A file `ls-files` reports that
   is absent on disk (a pending deletion), or is a symlink/dir/special, is not added to the tar; the
   `skipped` tally records how many, so nothing is silently dropped without a number.
4. **Content-addressed, deterministic `code_hash`.** `code_hash = sha256` over the sorted sequence of
   `(relpath, exec_bit, sha256(content))` for every included file. Same tree content ⇒ same
   `code_hash`, independent of tar byte layout, filesystem order, or timestamps. Any content, path,
   or exec-bit change changes `code_hash`.
5. **Repo-relative POSIX paths, `src/` layout preserved.** Member names match `git archive` (e.g.
   `src/native/training/atari.py`), so the box's extract → `PYTHONPATH=repo/src` → compile path is
   unchanged.
6. **`runq add` persists a snapshot for every new task.** At add time it calls `make_snapshot(ROOT)`
   (the invoking worktree) and `persist(...)` under the DB's parent dir, keyed by the new task id.
   `git_sha` is still recorded on the row as **provenance only** (best-effort `git rev-parse HEAD`);
   ship no longer depends on it. A snapshot failure (`SnapshotError`) fails the add (exit 2) rather
   than silently queuing an unshippable task.
7. **Dispatcher prefers the snapshot, falls back to `git archive`.** `_build_code_tar` first tries
   `load(EXPERIMENTS_ROOT, task_id)`. If present → use those bytes, `code_format="snapshot"`, and use
   `code_hash` as the compile-cache key. If absent (a task queued before this feature) → the existing
   `git archive <git_sha>` path, `code_format="git-archive"`, cache key `git_sha`. No task is
   un-shippable due to this change.
8. **Compile pipeline unchanged, re-keyed.** When `bundle_compile` is on, the compile-everything tree
   is cached by `(code_hash, ABI)` for snapshots (was `(git_sha, ABI)`). Two tasks with identical
   working-tree content share the cached compile. Compile failure still logs `ship_warn` and ships
   source (snapshot bytes) — never a silent downgrade.
9. **Atomic persist.** `persist` writes to `<name>.tmp` then renames, so a concurrent/failed write
   never leaves a torn tar the dispatcher could read.
10. **`load` is absence-safe.** Missing snapshot dir or file ⇒ `None`, no exception (enables the
    fallback in invariant 7).
11. **No registry schema change.** Keyed by `task_id` on the filesystem; `git_sha` column and all
    existing columns are untouched. `SCHEMA_VERSION` is not bumped.
12. **Worktree-correct.** `make_snapshot` operates on the directory it is given; invoked from a
    worktree it captures that worktree's files. No shared-checkout or ref reachability is consulted.

## Fixtures

Golden/behavioral tests in `tests/test_code_snapshot.py` (build a throwaway git repo in `tmp_path`):

- **Include/exclude:** a committed file, an uncommitted-modified file, a new untracked file, and a
  `.gitignore`d file → snapshot tar contains the first three (with the modified file's **new** bytes),
  excludes the ignored one; `file_count == 3`.
- **Manifest force-include:** a `.gitignore`d root `job.json` → still in the tar (as member 0, per
  job-artifact-contract inv. 10a), counted in `file_count`, and part of `code_hash`; a nested
  ignored `sub/job.json` stays excluded.
- **Determinism & sensitivity:** snapshot twice with no change → identical `code_hash`; edit one
  file → `code_hash` changes; `chmod +x` a file → `code_hash` changes.
- **Skipped tally:** a broken symlink / a `git rm --cached`-then-deleted path → counted in `skipped`,
  not in the tar.
- **persist/load round-trip:** `persist` then `load` returns the same `(bytes, hash)`; `load` of an
  unknown task id → `None`.
- **Worktree proof:** `git worktree add` a second dir, create an uncommitted file there, snapshot it
  → the file is in the tar (proving invariant 12).
- **CLI smoke:** `main(["--root", repo, "--out", tar])` writes a tar and prints the hash/counts.

## Open questions

- **O1 (snapshot GC).** Per-task snapshots accumulate under `.dispatcher/snapshots/`. This spec does
  not prune them. A followup should GC snapshots for terminal tasks (or LRU-cap the dir like the
  compile cache). **Not blocking** — snapshots are small vs the compile cache and checkpoints.
- **O2 (retain `git archive` long-term?).** The fallback keeps pre-feature tasks shippable. Once the
  queue has drained of git-archive-era tasks, the fallback could be removed. **Not blocking.**
- **O3 (huge working trees).** `make_snapshot` reads every non-ignored file into memory. Fine for
  this repo (source only; data is gitignored), but a pathological untracked large file would bloat a
  ship. A size guard/warn could be added later. **Not blocking.**
