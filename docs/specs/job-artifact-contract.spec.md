# Feature: Job artifact contract — self-describing jobs, artifact-agnostic coordinator

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (the Vast coordinator)
- **Module path:** `fleet/job_manifest.py` (new) + wiring in `runq.py`, `dispatcher.py`,
  `entrypoints.py`, `code_snapshot.py`, `registry_db.py`
- **Status:** built <!-- draft → approved → built --> (2026-07-14; approved by owner then
  implemented. Unit + integration + a local end-to-end smoke green — see Verification status. The
  paid-box smoke, like task-bundle's, PASSED 2026-07-14 — this is the default submission path.)
- **Spec file:** `docs/specs/job-artifact-contract.spec.md`
- **Consumes/relates:** `docs/specs/task-dispatcher.spec.md` (the ship→claim→run→ingest
  lifecycle and `task.json` schema), `docs/specs/run-registry.spec.md` (the task row + CAS),
  `docs/specs/task-bundle.spec.md` (the on-the-wire `bundle.tar`), and
  `docs/specs/code-snapshot.spec.md` (how the code tar is produced).

## Purpose

The coordinator is meant to be a generic queue/retry/checkpoint/observability service that varying
agents submit **jobs** to. Today it is not generic: it can only run jobs whose contract — how to
invoke them (`argv`), what proves completion (`completion_artifact`), whether/how they resume
(`resume_flag`), and what deps they need (`pip_extras`/`apt_packages`) — is **hardcoded in the
coordinator's own source** (`fleet/entrypoints.py`) and **cached in the running daemon at
start**. Consequences:

- Adding a new kind of job requires editing coordinator code *and* `dispatch-restart` — merged ≠
  live. A task naming an entrypoint the running daemon didn't cache is rejected as
  `unknown entrypoint` and stranded (observed live: `train_curriculum`,
  `train_wake_sleep_minatar` stranded until a restart).
- `runq add` **hard-requires a git checkout** (`git rev-parse HEAD` fails the add; `git ls-files`
  lists the snapshot files), so an agent that isn't in a git working tree — or that already has a
  built artifact — cannot submit at all.

This feature makes a **job self-describing**: the job's contract travels **inside the artifact** as
a `job.json` manifest, authored alongside the code. The coordinator reads the contract off the
artifact it was handed, validates it against a schema, and schedules/ships/runs/observes it without
needing to recognize the job by name or edit any table. `entrypoints.py` stops being a *registry of
allowed jobs* and becomes (a) the manifest **schema/validator** and (b) a **backward-compat
fallback** so the ~15 existing named entrypoints and any in-flight tasks keep working unchanged.
Submission also becomes **git-optional**: an agent may point `runq` at a directory (git or not) or
hand over a pre-built code tarball.

This does **not** change the box-side trust boundary, the `bundle.tar` integrity format, the
compile pipeline, or the ship/claim wire protocol. It changes *where the run contract comes from*
(the artifact, not the coordinator's table) and *how a code tar may be produced/submitted*
(git-optional).

## Non-goals

- **Making the coordinator host itself git-free.** The coordinator resolves its shared registry /
  experiments root via `git rev-parse --git-common-dir` (`registry_db.shared_experiments_root`).
  That is *coordinator infrastructure* (where its own SQLite + spool live), not the job or the
  artifact, and stays as-is. Only the **submission** path and the **job contract** are decoupled
  from git here.
- **Sandboxing untrusted job code.** Jobs are submitted by trusted agents in this repo's
  environment; `job.json` is validated for well-formedness and path-safety, not treated as an
  adversarial payload. (The *box-side* bundle already enforces its own integrity trust boundary —
  unchanged, task-bundle spec invariant 10.)
- **Remote/multi-tenant submission API.** Submission stays a local CLI (`runq`) against the shared
  registry. A network submission endpoint is a possible follow-up, out of scope here.
- **Changing scheduling/packing/retry policy.** Packing, retries, checkpoint cadence, teardown,
  spend, and observability are unchanged — this feature only changes where the per-job *run
  contract* is sourced from.

## Input contract

Two boundaries: what an agent authors (the job manifest), and how they submit it.

### The job block — a `job` section INSIDE the trainer config (v2, 2026-07-27)

**There is no `job.json` in the repository.** The run contract lives in a reserved top-level `job`
section of the **trainer config**, and the config is named IN FULL at the call site
(`runq add --config configs/continual/x.json`, sweep `"config": "..."`). One file, one full path,
nothing inherited.

**Why v2 replaced the loose `job.json`** (user directive 2026-07-27, after a live near-miss). A
repo-root `job.json` is a MUTABLE SINGLETON: `runq add --job .` and every sweep cell with
`"job": "."` silently took whatever config that one untracked file happened to point at. A sweep
named `m50_ladder_ceiling.sweep.json` therefore queued cells running
`configs/continual/m50_echo_center.json` — the file it appeared to be about was never read. Only the
`config_hash` dedupe caught it, by refusing the cells as duplicates of an unrelated run. The failure
is silent by construction: nothing in the sweep file, the task row, or the cell name mentions the
config actually used. Per-experiment `jobs/<exp>/job.json` dirs (29 of them, each a 7-line file
whose only real content was a `--config` path) were the workaround, and they could not even be used
directly — `make_snapshot(job_dir)` snapshots the dir it is given, so a dir holding only `job.json`
shipped a one-file tree. Hence: kill the file, name the config.

The `job` section is authored by the agent that owns the job. Trust boundary: **validated at
submission** (`runq add`/`submit`) against the schema below — required keys present, correct types,
path-safe `completion_artifact`, `run[0]` a recognized interpreter form. Shape:

```json
{
  "job": {
    "manifest_version": 1,
    "run": ["python", "-m", "native.training.curriculum"],
    "completion_artifact": "results.json",
    "resume": { "flag": "--init-from", "checkpoint": "ckpt_latest.pt" },
    "setup": { "pip": ["numpy>=2,<3", "tensorboard"], "apt": [] },
    "resources": { "slots": 1, "vram_gb": 1, "cores": 2, "est_minutes": 240 }
  },
  "arm": "…", "stages": ["…"], "seeds": [0]
}
```

**`run` must NOT contain `--config`.** `runq` appends `["--config", <the path you named>]` itself,
so the config can never disagree with the file it lives in. A `run` carrying `--config` is rejected
at submission with that reason.

**`job` is a RESERVED top-level key in every harness config** — `shared.infra.harness` strips it
before typed parsing (it is coordinator-owned, not trainer-owned), so no trainer dataclass declares
it and no trainer changes when the run contract changes. It is likewise stripped from
`config_hash`/`arm_hash` (`run_identity.EXCLUDE_PATHS`) for the same reason `checkpoint` already is:
`est_minutes` and `cores` are operational, not science, and must not re-key a run's identity.

| field | required | meaning (maps to today's `Entrypoint`) |
|---|---|---|
| `manifest_version` | yes | schema version; `!= JOB_MANIFEST_VERSION` → reject at submission |
| `run` | yes | argv **prefix**; `--config <named path>` then the submission's own args are appended after it (== `Entrypoint.argv`). Must not itself contain `--config` |
| `completion_artifact` | yes | path under the run's out dir whose existence gates `done` (== `Entrypoint.completion_artifact`); must be a relative path, no `..`/absolute |
| `resume` | no | Either `{flag, checkpoint}` — `flag` is the resume CLI flag (== `Entrypoint.resume_flag`), `checkpoint` the filename the trainer writes/reads (default `ckpt_latest.pt`) — **or** `{none: "<reason>"}`, a declared opt-out for a job that genuinely cannot resume (non-empty reason; mutually exclusive with `flag`). Absent ⇒ no resume support, which invariant 14 refuses past `est_minutes` 10 |
| `setup.pip` | no | pip deps installed on the box before first run (== `Entrypoint.pip_extras`); default `[]` |
| `setup.apt` | no | apt packages installed over ssh at ship time (== `Entrypoint.apt_packages`); default `[]` |
| `resources` | no | job's **default** resource hint + `est_minutes`; each field individually overridable per submission. Absent fields fall to coordinator defaults |

#### ⛔ `resources.vram_gb` is a RESERVATION — declare what the task USES, not a round number

**`vram_gb` is PER LANE and it is spent as a *paper* number**: `_budget_fits` (invariant 18a) sums
each occupant's DECLARED value, and `_headroom_fits` (invariant 23) charges the declared footprint
against MEASURED free VRAM. Neither ever looks at what the task really uses, so an over-declaration
rations a card nobody is occupying — the box refuses work while its GPU sits empty.

**Measured fleet-wide, 2026-07-31** (1522 `box_measured` samples regressed against the number of
tasks concurrently running on that box):

    vram_used = -0.024 GB/task + 0.434 GB baseline      # slope is NEGATIVE — i.e. NO per-task rise
    median VRAM used = 0.00 GB at every occupancy from 1 to 8 tasks
    p90 of the per-task marginal = 0.283 GB

That is the expected shape, not an anomaly: **98.2% of runs (1065 of 1084) place the model graph on
the CPU** (`learn_rule="local_graph"`, and 86 of the last 100 configs pin `graph_device="cpu"`
outright). The GPU is touched mainly by the world renderer, and that is `render_device`-pinnable too.

So **1 GB is the standing default for a CPU-line job** — already ~3.5x the measured p90 marginal, with
room for the occasional `render_device="cuda"` task. Declare more only when you have MEASURED that the
job needs it (a captured-CUDA arm, a pixel-conv tower, an LM). The historical `2` was never measured;
it cost `laptop-gpu` a day pinned at 4 occupants against a 12 GB card reading 0.0 GB used, while the
fleet rented 39 paid boxes — 28 of which completed zero tasks.

**Why editing this field is always safe:** `job` is in `run_identity.EXCLUDE_PATHS`, so `config_hash`
and `arm_hash` are byte-identical before and after. Re-sizing a resource hint can never fork a run's
identity, defeat the duplicate guard, or invalidate a resume.

`run` **fully determines** the entry-source path kept as `.py` under compile (via
`entrypoints.entry_source_path`, unchanged logic: `python -m native.X` → `src/native/X.py`), so
there is no separate "keep source" field.

### Submission surfaces (both git-optional)

1. **`runq add --config <path/to/config.json>`** (v2; replaces `--job <dir>`) — point `runq` at the
   trainer config FILE, full path, no implied filename. Its `job` section is the run contract; the
   tree that CONTAINS it is snapshotted into a code tar (per code-snapshot spec) — the repo root
   when the config lives in a git tree, else the config's directory. If it is a git working tree,
   `.gitignore` is honored via `git ls-files` exactly as today; **if it is not a git tree**, files
   are listed by a plain recursive walk honoring an optional `.dispatchignore` and a fixed default
   denylist (`.git/`, `__pycache__/`, `*.pyc`, `.venv/`). git is thus an *optional* ignore-lister,
   never required. A config with no `job` section is rejected — there is no fallback to infer one.
2. **`runq submit <code.tar.gz> --config <path/inside/tar.json> ...`** — hand over a **pre-built**
   gzipped code tar (git-archive layout: sources under `src/…`). No directory, no git, nothing run
   locally. `--config` names the config's path INSIDE the tar; the coordinator streams that member
   out, reads its `job` section, content-addresses the tar (`code_hash`), and persists it as the
   task's snapshot. Same rule as surface 1 — the config is named, never discovered — so **no member
   of the tar is magic by filename** and `job.json` has no meaning anywhere in the system.

Both surfaces still accept the per-submission knobs `runq add` has today (`--group`, `--name`,
`--priority`, `--vram-per-lane-gb`, `--cores-per-lane`, `--est-minutes`, `--init-from`,
`--max-retries`, `--force`, and the trailing `-- <args…>`). Any `resources`/`est_minutes` the
manifest declares are **defaults**; an explicit CLI flag always wins (matches how `--est-minutes`
overrides `est_defaults.json` today).

### Backward-compat input (unchanged)

`runq add --entrypoint <name> ...` with **no** `job.json` continues to resolve its contract from
`entrypoints.py` exactly as today. This is how every already-queued task and every existing named
trainer keeps working.

## Output contract

- **Resolved contract persisted on the task row — the manifest is read from the artifact exactly
  once.** At submission the resolved manifest fields are stored on the task (new nullable column
  `job_manifest_json`, see registry changes). Every later stage — schedule, ship, complete — reads
  that column via `entrypoints.resolve(task_row)` and **never re-opens the tar**. A task with no
  `job_manifest_json` falls back to `entrypoints.get(entrypoint)` (today's path). So the "read a
  member out of a gzip stream" cost (below) is paid once per submission, never on a hot path.
- **`job.json` is the FIRST member of the code tar, read by a bounded stream.** Because `code.tar.gz`
  is a gzip stream (no random access) wrapping a tar (no central index), reaching a member means
  decompressing everything before it. So the snapshot/tar builder writes `job.json` as the **first**
  tar member, and `from_tar` opens the tar in streaming mode (`tarfile` `r|gz`) and stops after
  member 1 — pulling ~O(manifest bytes) (measured ≈10 KB) regardless of tree size, and never
  materializing the tree to disk. Member order does **not** affect `code_hash` (code-snapshot
  invariant 4 addresses by the sorted file manifest, not tar bytes), so this is a free constraint.
  The `runq add <dir>` path reads `job.json` as a loose file from disk (`from_dir`) and never touches
  the tar; only `runq submit <prebuilt-tar>` uses `from_tar`.
- **`task.json` on the wire is unchanged in shape.** The dispatcher still emits the same keys
  (`task_id, grp, name, argv, env, est_minutes, git_sha, pip_extras, resume_from`); the *values*
  for a manifest task are computed from `job_manifest_json` instead of `entrypoints.py`. The box
  worker is **untouched** — it never sees `job.json`; it runs the resolved `argv` as today.
- **`git_sha` becomes optional provenance.** The column stays `NOT NULL` and accepts `""`
  (best-effort provenance; no destructive column rebuild); submission records it best-effort
  (`git rev-parse HEAD` if in a git tree, else `""`). No ship path depends on it (already true since
  code-snapshot).
- **Completion / resume / setup gates read the resolved contract.** `completion_artifact`,
  `resume.flag`, `setup.pip`, `setup.apt` for a manifest task come from `job_manifest_json`; for a
  legacy task, from `entrypoints.py`. Behavior is identical given equal values.

## Public API

New module `fleet/job_manifest.py` (sibling import, like the other `fleet` modules;
**stdlib-only** at top level so the box worker's constraints are never affected — though this module
is home-side only):

```python
JOB_MANIFEST_VERSION: int = 1
JOB_MANIFEST_NAME: str = "job.json"

class JobManifestError(Exception): ...   # any validation/parse failure at the submission boundary

@dataclass(frozen=True)
class JobManifest:
    run: list                    # argv prefix
    completion_artifact: str
    resume_flag: str | None      # None ⇒ no resume
    resume_checkpoint: str       # "ckpt_latest.pt" default
    pip: list                    # setup.pip
    apt: list                    # setup.apt
    resources: dict              # {slots?, vram_gb?, cores?, est_minutes?} — defaults, may be {}

def parse(data: dict) -> JobManifest
    # validate `data` against the schema (invariants 1–4); raise JobManifestError on any violation.

def from_tar(code_tar: bytes) -> JobManifest | None
    # read job.json from a gzipped code tar via a STREAMING open (tarfile r|gz), stopping at the
    # first member (job.json is written first, invariant 10a) so cost is O(manifest bytes), not
    # O(tree). None if absent (⇒ fall back to entrypoints). Never extracts the tree to disk.

def from_dir(root: str | Path) -> JobManifest | None
    # read job.json from a directory; None if absent.

def to_entrypoint(m: JobManifest) -> "entrypoints.Entrypoint"
    # adapt a JobManifest to the existing Entrypoint dataclass so every downstream consumer
    # (dispatcher ship/complete, runq handshake) works through ONE code path.
```

`entrypoints.py` gains a schema-facing helper and keeps its table as the fallback:

```python
def resolve(task_row: dict) -> Entrypoint
    # if task_row["job_manifest_json"] is set → job_manifest.to_entrypoint(parse(json)); else
    # ENTRYPOINTS[task_row["entrypoint"]]. The ONE place the run contract is resolved.
```

`code_snapshot.py` gains a git-optional lister:

```python
def make_snapshot(root, *, allow_non_git: bool = True) -> SnapshotResult
    # unchanged when `root` is a git tree; when it is not and allow_non_git, list via a plain walk
    # honoring `.dispatchignore` + default denylist. SnapshotError only if BOTH git and walk fail.
```

`runq.py` CLI gains the `submit` subcommand and directory form of `add` (described above).

## Dependencies

- `fleet/entrypoints.py` — the `Entrypoint` dataclass (adaptation target) and the fallback
  table. `resolve()` becomes the single contract-resolution seam.
- `fleet/code_snapshot.py` — produces/loads the content-addressed code tar; extended with the
  non-git lister. `job.json` rides inside the snapshot, so it is content-addressed with the code.
  One small amendment: the tar writer currently emits members in sorted path order; it must **hoist
  `job.json` to member 0** (the sorted remainder follows). This does not touch `code_hash` — that is
  computed over the sorted `(relpath, exec_bit, content-sha)` manifest, independent of tar write
  order (code-snapshot invariant 4) — so it is a compatible change, not a hash break.
- `fleet/registry_db.py` — new nullable `job_manifest_json` column; `git_sha` stays
  `NOT NULL` and accepts `""` (best-effort provenance; no destructive column rebuild);
  `SCHEMA_VERSION` bumped with a forward migration (invariant 9).
- `docs/specs/task-dispatcher.spec.md` — the ship/complete lifecycle; its `task.json` schema is
  unchanged (values are now contract-sourced). Invariant 13 (no secrets on the box) preserved —
  `job.json` never carries secrets and `env` stays `{}`.
- `docs/specs/task-bundle.spec.md` — unchanged; the bundle packs whatever `code.tar.gz` it's
  handed, `job.json` included, and the box ignores it.

## Behavior & invariants

Numbered, testable.

1. **A valid `job.json` fully specifies the run contract.** Given a manifest with `run`,
   `completion_artifact` (and optional `resume`/`setup`/`resources`), `parse` returns a
   `JobManifest` and `to_entrypoint` yields an `Entrypoint` byte-equivalent to a hand-written table
   row: same `argv`, `completion_artifact`, `resume_flag`, `pip_extras`, `apt_packages`.
2. **Validation is strict and fail-closed at submission.** Missing `run` or `completion_artifact`,
   wrong types (`run` not a list of str, etc.), `manifest_version != JOB_MANIFEST_VERSION`, or a
   `completion_artifact` that is absolute or contains `..` → `JobManifestError`, and `runq
   add`/`submit` exits non-zero **without** inserting a task. No half-registered jobs.
3. **`run` determines invocation and kept-source identically to a table entry.** The shipped `argv`
   is `run + submission_args`; `entrypoints.entry_source_path` over `run` selects the module kept as
   `.py` under compile. A `python -m native.X` manifest and the equivalent table row produce
   identical ships.
4. **Optional fields default deterministically.** Absent `resume` ⇒ `resume_flag=None` (and
   `--init-from` is refused, as today). Absent `setup.pip`/`setup.apt` ⇒ `[]`. Absent `resources`
   ⇒ `{}` (coordinator defaults + `est_defaults.json` apply). `resume.checkpoint` defaults to
   `ckpt_latest.pt`.
5. **The coordinator resolves the contract from the artifact, not its live table.** For a task with
   `job_manifest_json` set, `entrypoints.resolve(task_row)` returns the manifest-derived
   `Entrypoint`; the dispatcher's ship (`argv`/`pip_extras`/`apt`/`resume`), compile
   (`entry_source_path`), and completion (`completion_artifact`) paths all read *that*. **No
   `dispatch-restart` is required to accept a new job type, and no `unknown entrypoint` rejection is
   possible for a manifest-carrying task** — the failure class that stranded `train_curriculum`
   cannot occur.
6. **Named entrypoints still work unchanged (fallback).** A task with no `job_manifest_json`
   resolves via `ENTRYPOINTS[entrypoint]` exactly as today; every existing trainer and every
   already-queued task is byte-for-byte unaffected. `resolve()` is the only added branch.
7. **Submission is git-optional.** `runq add <dir>` on a non-git directory produces a snapshot via
   the plain walk (honoring `.dispatchignore` + default denylist); `runq submit <tar>` needs no
   directory at all. Neither calls `git rev-parse HEAD` as a hard gate. In a git tree, listing and
   `.gitignore` behavior are **identical to today** (invariant regression guard).
8. **`git_sha` is optional provenance.** Recorded best-effort — the HEAD sha in a git tree, else the
   **empty string** `""` (`runq._git_sha` never raises). The column stays `NOT NULL` and simply
   accepts `""` (no destructive table rebuild to relax it); no add fails for lack of git, and no
   ship/complete path reads it for correctness.
9. **Registry migration is additive and forward-only.** v1→v2 adds the nullable `job_manifest_json`
   column via `ALTER TABLE … ADD COLUMN` (idempotent, guarded by a `PRAGMA table_info` check);
   `git_sha` is untouched (invariant 8). `connect()` migrates an older `user_version` forward in
   place and still refuses a *newer* one (a downgrade). An old DB opens and every existing task
   still ships via the fallback (invariant 6). No existing column is dropped or retyped.
10. **`job.json` is content-addressed with the code.** Because it lives inside the snapshot/tar, a
    change to the run contract changes `code_hash` (code-snapshot invariant 4) — two submissions
    that differ only in `job.json` are distinct artifacts and distinct compile-cache keys.
10a. **`job.json` is the first tar member and is read by a bounded stream.** The snapshot/tar builder
    emits `job.json` as member 0; `from_tar` reads it via a streaming `r|gz` open that stops after
    the first member, consuming O(manifest bytes) (empirically ≈10 KB) even for a multi-MB tree, and
    never writing the tree to disk. Placing it first does not change `code_hash` (identity is the
    sorted file manifest, not tar byte order — code-snapshot invariant 4). The coordinator reads the
    tar for the manifest **only at submission** (to populate `job_manifest_json`); no ship/schedule/
    complete path re-opens it.
11. **The box worker is untouched.** No `job.json` is shipped as a distinct bundle member and the
    worker never parses it; it runs the resolved `argv` from `task.json` as today. `job.json`
    physically present inside `repo/` on the box is inert.
12. **Config-hash dedupe still works, with a defined fallback.** For the directory-`add` path, the
    `--print-run-identity` handshake runs `run + args` in the dir as today → config hash. For the
    raw-`submit` path (code not runnable locally), dedupe falls back to `(code_hash, run, args)`
    without executing anything; `find_clash` uses whichever hash was computed. A submit task is
    never rejected merely because its handshake couldn't run.
13. **A usable actor is required for every submission path.** `runq add <dir>` and
    `runq submit <tar>` obey run-registry invariant 15 identically to named `add`: the `created_by`
    actor is resolved (`--by` → `$RUNQ_ACTOR` → current git branch) and validated before any task
    row is inserted; a non-identifying default (`master`/`main`/detached `HEAD`/`unknown`) or a
    malformed label → exit 2, no task inserted. This is orthogonal to invariant 7 (git-optional):
    a non-git submission is fine, but then `--by` or `$RUNQ_ACTOR` must supply the identity.
14. **A long job must declare how it resumes, checked at queue time** (added 2026-07-26). If a
    manifest omits `resume` and its `est_minutes` exceeds `RESUME_REQUIRED_OVER_MINUTES` (10 — the
    checkpoint+resume rule's threshold), `runq add --job` / `runq submit` exit 2 without inserting a
    row. **Opting out is DECLARED IN THE MANIFEST — `"resume": {"none": "<reason>"}`** (non-empty,
    mirroring the harness's `checkpoint.reason`); `--no-resume` on the command line is refused and
    the error names the field to add. **Revised 2026-08-25**, because the original CLI opt-out was
    checked for non-emptiness and then DISCARDED — never persisted to the task row, never visible
    to anything downstream. A config committed without a resume block was therefore
    indistinguishable from one deliberately opted out, so the repo-wide audit
    (`test_every_committed_manifest_declares_resume`) and this queue-time gate could not agree even
    in principle: the audit reads the artifact, the gate read an invocation that no longer existed.
    Two probes (`pc_colorbook_bprior_{bias,ctrl}`) carried a hand-rolled `_resume_note` key read by
    NOTHING — the author wanted the justification to be durable and had nowhere to put it — and
    when the audit went red over it, the fix went into the TEST (`33b10735` taught the test to
    honour `_resume_note`) while the coordinator carried on ignoring it. **The verdict now comes
    from one function, `job_manifest.check_submittable`, called by both.** It is deliberately NOT
    inside `parse`: `entrypoints.resolve` re-parses the manifest stored on an already-queued task
    row at dispatch, so a policy rejection in the parser would strand live work whenever the policy
    changed. Structure belongs in the parser; policy belongs where money is spent.
    **Why any of it exists:** the named-entrypoint table carried `resume_flag` and
    the coordinator honoured it; moving that declaration into each job's own `job.json` made
    omitting it silent. `native.training.m49_curriculum_ab` ran 212 tasks over 22 hours with
    `resume_flag=None` — the dispatcher never pulled a checkpoint and never re-appended
    `--init-from`, and eight tasks died with nothing to restart from. Declaring `resume` is
    necessary but NOT sufficient: the trainer must actually honour the flag (that driver's
    `--init-from` was accepted and discarded), which is what the trainer-harness gate enforces on
    the other side.

## Fixtures

Golden/behavioral tests in `tests/test_job_manifest.py` and additions to `tests/test_dispatcher.py`
/ `tests/test_registry_db.py`:

- **parse_valid / to_entrypoint** — a full manifest → `JobManifest`, and `to_entrypoint` equals a
  hand-written `Entrypoint(argv=[...], completion_artifact=..., resume_flag=..., pip_extras=...,
  apt_packages=...)`. (Invariants 1, 3.)
- **parse_invalid** — each of: missing `run`; missing `completion_artifact`; `run` not `list[str]`;
  `manifest_version=999`; `completion_artifact="/abs"`; `completion_artifact="../escape"` → each
  raises `JobManifestError`. (Invariant 2.)
- **defaults** — a minimal `{manifest_version, run, completion_artifact}` → `resume_flag=None`,
  `pip==[]`, `apt==[]`, `resources=={}`, `resume_checkpoint=="ckpt_latest.pt"`. (Invariant 4.)
- **from_tar / from_dir absence** — a tar/dir with no `job.json` → `None` (⇒ fallback path).
  (Invariant 6.)
- **from_tar_first_member_bounded** — build a `code.tar.gz` with `job.json` first followed by a large
  (≥10 MB) incompressible member; `from_tar` returns the manifest having pulled only a small bounded
  prefix of the compressed stream (assert via a byte-counting fileobj wrapper), and writes nothing to
  disk. A tar with `job.json` NOT first still parses correctly (correctness independent of the
  optimization). (Invariant 10a.)
- **resolve_manifest_vs_table** — a task row with `job_manifest_json` resolves to the manifest
  `Entrypoint`; a row without one resolves to `ENTRYPOINTS[name]`. (Invariants 5, 6.)
- **dispatcher_ships_manifest_contract** — with a stubbed ship, a manifest task's emitted `task.json`
  carries `argv = run + args`, `pip_extras = setup.pip`; the completion check looks for
  `completion_artifact`; apt install uses `setup.apt`. No `entrypoints.get` is consulted for it.
  (Invariant 5.)
- **non_git_snapshot** — `make_snapshot` on a plain (non-git) dir with a `.dispatchignore` → tar
  includes the wanted files, excludes ignored ones, and `job.json`. In a git tree, output is
  identical to the pre-change lister (regression). (Invariant 7.)
- **submit_prebuilt_tar** — `runq submit <tar-with-job.json>` inserts a task with
  `job_manifest_json` set and a persisted snapshot equal to the input tar bytes' content address;
  no git invoked. (Invariants 7, 12.)
- **migration_roundtrip** — open an old DB (pre-column), run migration, confirm existing rows ship
  via fallback and a new manifest task inserts. (Invariant 9.)
- **git_sha_optional** — `runq add`/`submit` from a non-git dir inserts a task with null/empty
  `git_sha` and ships successfully end-to-end (docker-integration or stubbed). (Invariant 8.)

## Verification status

Built + green (2026-07-14). Acceptance:

- **`tests/test_job_manifest.py` (29 cases)** — every fixture above: manifest parse/validation
  (12 invalid cases), defaults, `to_dict` round-trip, streaming reader (order-independent
  correctness), `entrypoints.resolve` manifest-vs-table, non-git walk + `.dispatchignore` +
  `job.json`-first, v1→v2 migration, and `runq submit` / `add --job` end-to-end.
- **Full vast suite green in both orders** — 149 tests (120 pre-existing + 29 new); no regression in
  `test_registry_db`/`test_code_snapshot`/`test_dispatcher`/`test_bundle`. The named `runq add`
  CLI path is unchanged.
- **Local end-to-end smoke** — a job whose `run` names an entrypoint the coordinator has never heard
  of (`native.totally_new_trainer_xyz`) was `runq submit`ted and driven through the **real**
  `Dispatcher._build_task_json`: the shipped `argv`/`pip_extras`/`apt`/`completion_artifact` all
  resolved from the artifact, while `entrypoints.get(label)` still raises (proving the old path would
  have stranded it). No box rented.
- **Passed (2026-07-14):** a **paid-box smoke** (a manifest job running to `results.json` on real
  hardware), matching task-bundle's final gate — this is now the default submission path for real
  training.

## Open questions

- **O1 (manifest location/name) — RESOLVED.** Implemented as root `job.json` (discoverable, one
  file; hoisted to tar member 0 for the bounded read). `.dispatch/job.json` was the alternative.
- **O2 (per-repo default manifest).** For this repo's 15 trainers, hand-writing a `job.json` each is
  redundant with `entrypoints.py`. Option: a generator that emits a `job.json` per entrypoint from
  the existing table (migration aid), or a repo-level default manifest keyed by the `-m` target.
  **Non-blocking** — the fallback keeps them working without any manifest.
- **O3 (raw-submit config identity).** Invariant 12 defines a content-address fallback when the
  handshake can't run. Should `submit` optionally accept a caller-supplied `config` blob (so an
  agent that *does* know its config can still get semantic dedupe)? **Non-blocking** — the
  content-address fallback is correct and safe; richer identity is additive.
- **O4 (retire `entrypoints.py` table).** Once every job carries a manifest, the table could be
  reduced to just the schema/validator and the fallback deleted. **Deliberately deferred** — keeping
  the fallback indefinitely is cheap and de-risks the transition (owner preference, this session).
- **O5 (resources in the manifest vs. purely per-submission) — RESOLVED.** Implemented
  defaults-only: `resources.{est_minutes,vram_gb,cores}` supply defaults, any explicit CLI flag
  wins (`_manifest_est_and_hint`), mirroring `est_defaults.json`. Scheduling/packing policy stays
  coordinator-owned.
