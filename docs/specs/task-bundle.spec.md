# Feature: Task bundle — packaged, integrity-checked, optionally-compiled payloads

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (the Vast coordinator)
- **Module path:** `fleet/bundle.py`
- **Status:** built <!-- draft → approved → built --> (unit + fixtures green; paid box smoke
  PASSED 2026-07-10 — a compiled `cpython-312` trainer ran on a real box to `results.json`;
  `bundle_compile` is now DEFAULT ON. See Verification status.)
- **Spec file:** `docs/specs/task-bundle.spec.md`
- **Consumes/relates:** `docs/specs/task-dispatcher.spec.md` (the ship/claim wire protocol this
  replaces the loose-files half of), `docs/operations.md` (local invariants).

## Purpose

Today the dispatcher ships a task as **loose files** into `~/spool/incoming/<id>/`
(`payload.tar.gz` = `git archive`, `task.json`, optional `resume.pt`, then a `READY` sentinel),
and the box worker extracts and runs them with no integrity check and no notion of what a
"package" is (`dispatcher.py` `_ship`, `spool_worker.py` `validate_and_prepare`). This feature
replaces that with a **single, versioned, self-describing bundle**:

1. **Formal package (Part 1).** One artifact — `bundle.tar` — carrying a `manifest.json` that
   binds every member by SHA-256, plus the code, `task.json`, and any resume checkpoint. The box
   **verifies** the manifest before extracting or running anything. This gives integrity (catches
   the truncated/corrupted transfers already observed in this repo — cf. the `rsync --append`
   corruption gotcha), reproducibility (content-addressed), and an **optional ed25519 signature**
   for authenticity (home holds the private key; the box holds only the public key — no secret on
   the box, preserving task-dispatcher invariant 13).
2. **Harder-to-read code (Part 2).** Optionally, the code member is **compiled** (Cython → `.so`)
   inside a container whose ABI matches the box image, so the shipped tree is extension modules
   instead of `.py` source. This **raises the reverse-engineering bar** for a nosy host; it is
   explicitly **not** confidentiality against a root-capable host (which can read process memory,
   weights, and CUDA traffic regardless). See **Non-goals**.

## Non-goals

- **True confidentiality from the host.** A Vast host has root and runs the code on its own GPU;
  no packaging or compilation makes code or model weights secret from it. Real code-confidentiality
  requires attested confidential compute (TEE + GPU CC mode), which Vast does not offer — that is a
  provider decision, out of scope here. Compilation is bar-raising only.
- **Dependency baking into a prebuilt image / wheelhouse.** The box still `pip install`s
  `pip_extras` at runtime. Carrying prebuilt wheels in the bundle is a natural follow-up (Open
  questions) but is out of scope for v1.
- **Full entry-module compilation.** `python -m native.<trainer>` cannot import a compiled
  extension module as `__main__` (runpy needs source/bytecode). v1 compiles everything **except**
  the entry module(s), which stay as source. See Open questions.

## Input contract

Two boundaries.

**Home-side build input** (`build_bundle`, internal to `fleet`):
- `code_tar: bytes` — a gzipped tar of the code tree (a working-tree **snapshot** per
  `docs/specs/code-snapshot.spec.md`, the legacy `git archive` output, or the compiled tree from
  `compile_tree`). `build_bundle` is agnostic to which — it hashes/packs the bytes it is given.
- `task_json: dict` — the existing task descriptor (schema owned by task-dispatcher spec;
  unchanged: keys `task_id, grp, name, argv, env, est_minutes, git_sha, pip_extras, resume_from`).
- `resume: bytes | None` — optional checkpoint bytes.
- `code_format: "snapshot" | "git-archive" | "compiled"`, `sign_key: Path | None`.

**Box-side consume input** (`unpack_bundle`, trust boundary on the box): an untrusted
`bundle.tar` file that arrived over ssh into `~/spool/active/<id>/`. It is **validated** before any
extraction: valid tar, contains `manifest.json`, manifest is well-formed, every declared member is
present and its SHA-256 matches, and — if the box was provisioned with a public key — a valid
signature over the manifest. `task.json` is still additionally validated by the worker's existing
`validate_task_json` strict-key check (passed into `unpack_bundle` as its `validate` param); it
runs **before `repo/` is extracted**, so a bad `task.json` raises `BundleError` and nothing is
extracted.

## Output contract

**`bundle.tar`** — an **uncompressed** POSIX tar (outer layer uncompressed so already-compressed
members — `code.tar.gz`, `resume.pt` — are not recompressed) whose members are exactly:

| member | required | content |
|---|---|---|
| `manifest.json` | yes | the manifest (below), UTF-8 JSON |
| `code.tar.gz` | yes | gzipped tar of the code tree |
| `task.json` | yes | the task descriptor, UTF-8 JSON |
| `resume.pt` | no | resume checkpoint bytes (present iff the task resumes) |

**`manifest.json`**:
```json
{
  "bundle_version": 1,
  "task_id": "<id>",
  "git_sha": "<sha>",
  "code_format": "snapshot" | "git-archive" | "compiled",
  "members": {
    "code.tar.gz": {"sha256": "<hex>", "bytes": <int>},
    "task.json":   {"sha256": "<hex>", "bytes": <int>},
    "resume.pt":   {"sha256": "<hex>", "bytes": <int>}   // omitted when no resume
  },
  "signature": {"alg": "ed25519", "key_id": "<hex8>", "sig": "<base64>"} | null
}
```
The signature (when present) is over the **canonical bytes of the manifest with its `signature`
field set to `null`** (`json.dumps(m, sort_keys=True, separators=(",", ":"))`), so a manifest can
carry its own signature without a chicken-and-egg hash.

**Box-side unpack layout** — `unpack_bundle(bundle, dest)` writes into `dest/`, matching the
layout the worker already expects so downstream launch is unchanged:
- `dest/task.json`
- `dest/repo/…` (the code tree, extracted from `code.tar.gz`)
- `dest/resume.pt` (iff present) — reachable by the worker's existing `--init-from ../resume.pt`.

## Public API

Module-internal to `fleet` (both `dispatcher.py` and `spool_worker.py` import it as a
sibling module, exactly as `spool_worker` imports `sweep_supervisor`). Exported surface of
`bundle.py`:

```python
BUNDLE_VERSION: int = 1
BUNDLE_NAME: str = "bundle.tar"
MANIFEST_NAME: str = "manifest.json"

class BundleError(Exception): ...   # any integrity/signature/format failure

def build_bundle(dest: Path, *, code_tar: bytes, task_json: dict,
                 git_sha: str, task_id: str, resume: bytes | None = None,
                 code_format: str = "git-archive",
                 sign_key: str | Path | None = None) -> Path
    # writes dest (a bundle.tar), returns dest.

def read_manifest(bundle: str | Path) -> dict
    # opens the tar, returns the parsed manifest. Raises BundleError if absent/malformed.

def verify_bundle(bundle: str | Path, *, public_key: str | Path | None = None,
                  require_signature: bool = False) -> dict
    # verifies every member sha256; if require_signature, a valid signature is mandatory and is
    # checked against public_key. Returns the manifest. Raises BundleError on any mismatch.

def unpack_bundle(bundle: str | Path, dest: str | Path, *,
                  public_key: str | Path | None = None,
                  require_signature: bool = False, validate=None) -> dict
    # verify_bundle, then extract task.json + repo/ + resume.pt into dest. Returns the parsed
    # task.json dict. Raises BundleError if verification fails (nothing is extracted on failure).
    # `validate`, if given (the worker passes `validate_task_json`), is called with the parsed
    # task.json BEFORE repo/ is extracted; a truthy error string it returns → BundleError, and
    # nothing is extracted (reject-without-executing a bad task.json).

# signing (optional; only used when cryptography + a key are available)
def sign_available() -> bool
def sign_manifest(manifest: dict, sign_key: str | Path) -> dict   # returns the signature block
def verify_manifest_signature(manifest: dict, public_key: str | Path) -> bool

def overlay_entry_source(compiled_tar: bytes, entry_rel: str, source_tar: bytes) -> bytes
    # return compiled_tar with the entry module restored to source: drop its .so, put its .py back
    # (from source_tar). Lets ONE compile-everything tree (cached per commit) serve every trainer by
    # swapping only the run entry. entry_rel is repo-relative, e.g. "src/native/training/lm.py".

# compilation (Part 2; bar-raising, not secrecy)
def compile_tree(code_tar: bytes, *, packages: list[str], keep_source: list[str], image: str,
                 run=subprocess.run, work_root: str | Path | None = None,
                 backend: str = "local", python_bin: str | None = None,
                 expected_abi: str | None = None, source_root: str = "src",
                 cache_dir: str | Path | None = None, cache_max: int = SO_CACHE_MAX) -> bytes
    # cythonize each *.py under `packages` (except paths in `keep_source` and every __init__.py) to
    # an ABI-tagged .so, delete the .py, return a new code.tar.gz. `cache_dir` holds the per-module
    # .so cache of invariant 7c (None disables it, which only costs speed). backend="local" (default)
    # compiles with `python_bin` on the coordinator host and refuses unless its EXT_SUFFIX contains
    # `expected_abi`; backend="docker" compiles inside a container from `image`. Raises BundleError
    # if the toolchain is unavailable or compilation fails (the dispatcher logs `ship_warn` and
    # falls back to source — never silently).
```

`bundle.py` MUST import only the Python standard library at module top-level (`tarfile`,
`hashlib`, `json`, `io`, `base64`, `pathlib`, `subprocess`). `cryptography` is imported **lazily
inside the signing functions** and its absence degrades gracefully (`sign_available() → False`);
signing/verification is only ever *required* when a key is configured. This keeps the box worker
runnable on a bare `pytorch/pytorch` image.

## Dependencies

- `docs/specs/task-dispatcher.spec.md` — the `task.json` schema (unchanged) and the
  ship→claim→run→ingest lifecycle this slots into. Invariant 13 (no secrets on the box) is
  preserved: only a **public** key is ever shipped.
- `cryptography` (ed25519) — **optional**, lazily imported, home-side for signing and box-side
  only when a public key was provisioned.
- Docker + an ABI-matching image — **optional**, only for `compile_tree`.

## Behavior & invariants

1. **Round-trip.** `unpack_bundle(build_bundle(...))` reproduces the exact `task.json`, code tree,
   and resume bytes that went in.
2. **Integrity is always enforced on the box.** Any member whose SHA-256 does not match the
   manifest → `BundleError`, nothing extracted, task rejected (worker writes `FAILED_validation`,
   as it already does for a bad `task.json`).
3. **Manifest binds all members.** A manifest listing a member not in the tar, or a tar member not
   in the manifest (other than `manifest.json` itself), → `BundleError`.
4. **Signature is opt-in and fail-closed when required.** If the box worker was started with
   `--public-key P`, every bundle MUST carry a signature valid under `P`; a missing or invalid
   signature → `BundleError`. If no public key was provisioned, signatures are ignored (integrity
   still enforced). The dispatcher signs iff `settings["bundle_sign"]` and a private key
   (`DISPATCHER_BUNDLE_SIGN_KEY`) are both present.
5. **No secret on the box.** Only the ed25519 **public** key is ever shipped. The private key
   never leaves home; `task.json`'s `env` stays `{}` (unchanged). (task-dispatcher invariant 13.)
6. **Compilation is default-on, ABI-matched, and never a silent downgrade — but a compile ERROR
   fails fast, it does not fall back.** The dispatcher compiles iff `settings["bundle_compile"]`
   (default True). The default `backend="local"` compiles with an interpreter whose version matches
   the box (`bundle_compile_abi`, resolved via `_resolve_build_python` → explicit
   `bundle_compile_python`, else `uv python find <ver>`, else `sys.executable`); `backend="docker"`
   builds in `bundle_compile_image`. A compile failure splits by cause:
   - **Toolchain / ABI / env limitation** (no matching interpreter, ABI mismatch, no docker, build
     venv unbuildable — a plain `BundleError`): the coordinator *can't* compile at all, so failing
     would halt the whole fleet over an ops config issue. It **logs a `ship_warn`, emits a loud
     `[ALERT]`**, and ships the `git-archive` source (`code_format="git-archive"`, `compile` event
     `mode=fallback`) so the fleet keeps running — the only thing that legitimately falls back.
   - **Compile ERROR** (the compiler REJECTS the code — one file fails the whole-tree compile; a
     `bundle.CompileFailed`): this is a *code bug*, not an env issue, and shipping source would run
     it uncached + un-hidden **forever**, re-paying a doomed cold compile every ship (a fallback
     never warms the cache), all silently. So the task is **failed fast**: `claimed → task_failed`
     (terminal, no auto-requeue — retrying re-fails the same code) with a loud `[ALERT]` naming the
     offending `file:line: message` (via `_compile_error_summary`). The usually-one-line fix requeues
     it. It does not fall back, does not ship an unloadable `.so`, and does not silently pretend the
     code compiled.
   Either way it never silently downgrades: a fleet-wide compile break is impossible to miss.
7. **Compiled tree stays runnable.** `compile_tree` leaves the entry module(s) named in
   `keep_source` (and every `__init__.py`) as `.py` so `python -m native.<trainer>` still works, and
   compiles the rest of `packages` to `.so`. The resulting `code.tar.gz` extracts to an importable
   tree with `PYTHONPATH=src` unchanged.
7a. **One compile per commit, not per ship (cache + overlay).** The dispatcher compiles the WHOLE
   tree once (`keep_source=[]`) per `(code_hash, ABI, packages)` and caches it under
   `experiments/.dispatcher/compile-cache/` (content-addressed → never stale; LRU-pruned to
   `bundle_compile_cache_max`). `code_hash` is the content address of the working-tree snapshot
   `runq add` persisted (code-snapshot spec); `git_sha` is only the fallback cache key for the
   legacy `git archive` path (and is often `""` now). Each ship reads the cached tree and calls
   `overlay_entry_source` to restore just its own entry module to `.py`. A batch of different
   trainers at one commit therefore pays a single compile. Compilation uses `-O0 -g0` (code-hiding,
   not runtime speed), which drops a cold compile from ~171s to ~35s.
7b. **Compile timing is observable.** Each ship that runs the compile path emits one `compile`
   event into the registry `events` table, `detail` = JSON `{mode, sec, git_sha, packages, note?}`:
   `mode` ∈ {`miss` = a cold per-commit build, `hit` = a warm cache read + entry overlay,
   `fallback` = compile raised so source shipped}, `sec` = wall-clock of the build/overlay. The
   dashboard's coordinator snapshot aggregates these into cold-build p50/max, warm-ship p50, and a
   cache hit-rate (`run-dashboard.spec.md` §14). This is pure observability — it never gates
   shipping, and a registry with no such events (older code) simply shows nothing.
7c. **One compile per MODULE, not per tree — the blob-cache MISS is incremental.** 7a caches the
   whole compiled tree per `(code_hash, ABI, packages)`, so a **hit** is free and a **miss** rebuilds
   all 93 modules. But `code_hash` addresses the WHOLE working tree, so editing a spec, a config or a
   README misses the blob cache while changing **no compiled module at all** — measured over the last
   160 commits, **117 (73%) touch zero** modules under `packages`, and of the 43 that do the median
   is **1** and p90 is **4**. `compile_tree` therefore keeps a second, finer cache: one built `.so`
   per module, under `experiments/.dispatcher/socache/<key>.so`, and cythonizes only the modules
   whose key is absent.

       key = sha256( SO_CACHE_VER | EXT_SUFFIX | cython_version | py_version | language_level
                     | extra_compile_args | rel_path | source_bytes )

   **Why a per-module key is sound HERE, and the guard that keeps it sound.** The task-dispatcher
   spec calls a narrowed cache key "the worst failure class this fleet has" because a key that misses
   an input silently ships STALE BINARIES. That risk is real for a key over *some* of a module's
   inputs; this key is over *all* of them, but only because the tree has **no `.pxd` files, no
   `cimport`, and no `# cython:` directive comments** — the three ways one module's generated C can
   depend on another file. Without them each module's `.so` is a pure function of its own source
   plus the toolchain, all of which the key names. That premise is not self-evident and must not be
   allowed to rot silently, so `tests/test_bundle.py::test_no_cross_module_cython_inputs` asserts it
   over `src/`: **add a `.pxd` or a `cimport` and the suite goes red**, naming this invariant. Fixing
   it then means either keying on the whole dependency set or bumping `SO_CACHE_VER` and disabling
   the cache — never leaving it as-is.
   Corollaries: the cache is keyed by CONTENT, so reverting a file re-hits its original `.so` and a
   branch-switch costs only what genuinely differs; it is a pure accelerator, so deleting the
   directory only makes the next build cold; and it is LRU-trimmed by mtime to
   `bundle_so_cache_max` entries (default 4000, ~1.3 GB at the measured 336 KB median). A cached
   `.so` is placed by copy, never symlink, so the shipped tar carries real bytes.
   **Testable:** compiling one tree twice invokes the compiler on N modules then on 0; changing one
   module's source recompiles exactly that module; `src/` contains no `.pxd`/`cimport`/`# cython:`.
7d. **Gzip at level 6, not 9.** Every `w:gz` in the build path (`_retar`, `overlay_entry_source`,
   `code_snapshot.make_snapshot`) pins `compresslevel=GZIP_LEVEL`. `tarfile` defaults to **9**, which
   measured **6.28 s** to produce the 12.0 MB compiled tar against **1.73 s** at level 6 for 12.1 MB
   — **3.6× the time for 0.8% of the bytes**, paid twice (retar + overlay) on every build. The blob
   is transported by rsync to a box that gunzips it; decompression is level-independent, so nothing
   downstream can observe the change. This does not weaken inv. 5 (first-publisher-wins): a
   `code.tar.gz` was never byte-reproducible across builds (tar carries mtimes), which is exactly
   why `put` is create-if-absent and `publish` records the digest of what it *published*.
8. **Wire protocol: one artifact + READY.** `_ship` writes `bundle.tar` then the `READY` sentinel
   (READY last — task-dispatcher invariant 7, unchanged). The worker claims on `READY` (unchanged),
   then unpacks `bundle.tar`. `payload.tar.gz`/loose `task.json`/loose `resume.pt` are no longer
   shipped.
9. **Idempotent re-ship / restart safety (unchanged semantics).** Re-ship clears the prior box
   dir (unchanged). On the box, `unpack_bundle` runs only when `repo/` is absent; `task.json` is
   read from `dest/task.json` on every process lifetime (a restart after unpack re-reads the
   already-materialized `task.json`, never re-verifies/re-extracts an existing `repo/`).
10. **Trust boundary.** `verify_bundle`/`unpack_bundle` treat the tar as untrusted: no member is
    extracted before its hash is verified; tar extraction is guarded against path traversal
    (members with absolute paths or `..` components → `BundleError`).
11. **Bundle version.** `bundle_version != BUNDLE_VERSION` → `BundleError` ("unsupported bundle
    version"), so a future format change fails loudly rather than mis-parsing.

## Fixtures

Golden cases become `tests/test_bundle.py` (unit) and an added case in
`tests/test_docker_integration.py` (end-to-end over the real wire):

- **`roundtrip`** — build a bundle from a small code tar + task.json (+resume), unpack, assert the
  task.json dict, extracted `repo/` file contents, and `resume.pt` bytes all match. (Invariant 1.)
- **`corrupt_member`** — flip a byte in `code.tar.gz` inside the tar → `unpack_bundle` raises
  `BundleError`, `dest` has no `repo/`. (Invariant 2.)
- **`manifest_mismatch`** — manifest omits a member present in the tar (and vice-versa) →
  `BundleError`. (Invariant 3.)
- **`signature_required_ok` / `signature_required_bad`** — with a provisioned public key: a
  correctly-signed bundle unpacks; a tampered-then-re-hashed-but-unsigned bundle raises. Skipped
  if `sign_available()` is False. (Invariant 4.)
- **`no_pubkey_ignores_signature`** — no public key provisioned: an unsigned bundle unpacks fine.
  (Invariant 4.)
- **`path_traversal`** — a `code.tar.gz` containing `../evil` → `BundleError`. (Invariant 10.)
- **`bad_version`** — manifest `bundle_version: 999` → `BundleError`. (Invariant 11.)
- **`compile_tree_stub`** — `compile_tree` with a stubbed `run` that swaps one `.py` for a gcc-built
  `.so`: assert the returned tar has the `.so`, lacks the compiled `.py`, and keeps `keep_source`
  files as `.py`. (Invariants 6–7; the real Cython-in-Docker path is only exercised by a paid box
  smoke, documented below.)
- **docker-integration `bundle_lifecycle`** — the existing `TestShipRunPullDone` flow, asserting a
  `bundle.tar` (not `payload.tar.gz`) is delivered and the smoke entrypoint still completes.

## Verification status

- Unit + docker-integration fixtures above are the acceptance gate for merge.
- **The compiled path was validated on a real box** (paid smoke, below) — `bundle_compile` is
  default on. The source-bundle path remains the safe fallback whenever a matching build
  interpreter isn't available.

### Compile smoke findings (2026-07-10)

Ran the compile locally (coordinator devcontainer = conda Python 3.11 x86_64, ABI-compatible with
the box image's conda Python 3.11 x86_64) — no Docker, no box, no spend:

- **Cython compiles the whole codebase.** `compile_tree` (local backend) over a real `git archive`
  produced 36 `.so` + 5 `.py` (4 `__init__` + the kept entry) in ~80s, and the source entry ran
  `--print-run-identity` through the fully-compiled graph, exit 0. The core feasibility risk is
  retired.
- **Two placement bugs found + fixed** (the smoke's whole point):
  1. `build_ext --inplace` writes each `.so` relative to CWD from the (src-stripped) module name,
     so it tried `native/agent.so` under CWD instead of `src/native/agent.so`. Fixed by passing
     `package_dir={"": source_root}` explicitly (the working case had only *accidentally* relied on
     the repo's `pyproject.toml` src-layout config being read).
  2. Compiling a package `__init__.py` mis-resolves its output dir. Fixed by **keeping `__init__.py`
     as source** (trivial import glue; not worth compiling).
- **Docker isn't installed on the coordinator host.** `compile_tree`'s original design ran
  `docker run <box-image>` there, which can't work; and a `-runtime` box image lacks a compiler
  anyway. **Resolution (chosen):** a **local-toolchain backend** (`bundle_compile_backend="local"`,
  the default) compiles with the coordinator's own Python/gcc via a cached Cython build venv, with
  an **ABI guard** that refuses to build unless the build interpreter's `EXT_SUFFIX` matches
  `bundle_compile_abi` (the box's Python ABI) — so a mismatched coordinator fails loud instead of
  shipping an unloadable `.so`. Correct here because the devcontainer *is* the controlled,
  reproducible build environment (same conda-py3.11-x86_64 ABI as the box) — the role the nested
  Docker was playing. `backend="docker"` stays available for hosts that want the exact-image ABI
  guarantee.
- **Paid box smoke — PASSED (2026-07-10), after fixing a real ABI-version mismatch.** The first
  box run failed with `ModuleNotFoundError: No module named 'native.models.agent'` — the box image
  (`pytorch/pytorch:2.12.1-cuda12.6-cudnn9-runtime`) runs **`/usr/bin/python3` = Python 3.12.3**
  (`EXT_SUFFIX .cpython-312-…so`), but the coordinator devcontainer is conda **Python 3.11**, so
  `cpython-311` `.so` are not recognized by the box's 3.12. `.so` are Python-version-specific, and a
  3.11 interpreter cannot build 3.12 `.so`. **Fix:** build with a **matching Python 3.12
  interpreter** (`bundle_compile_abi = cpython-312`; the coordinator resolves one via
  `uv python find 3.12`, install once with `uv python install 3.12`). Re-run: the compiled
  `cpython-312` tree loaded and ran on the real box, wrote `results.json`, task **DONE**. The box
  runs the trainer under its own `/usr/bin/python3` (3.12), so the build interpreter's version — not
  the coordinator's — is what must match.
- **`bundle_compile` is now DEFAULT ON.** It engages only when a matching build interpreter is
  resolvable (else the ABI guard trips and it ships source with a `ship_warn` — safe, never a bad
  `.so`). The build interpreter is the ONE operational dependency: on this coordinator it's the
  uv-installed Python 3.12; a future box-image Python change means installing the new version and
  updating `bundle_compile_abi`.

## Open questions

*(None blocking v1. Deferred design choices, recorded per the methodology rather than guessed:)*

- **Full entry-module compilation.** To compile the entry module too, trainers would need a
  `main()` callable a compiled launcher can import (instead of a `__main__` block), or the launch
  command would change from `python -m native.<trainer>` to a source shim. Deferred; v1 keeps entry
  modules as source (invariant 7).
- **Dependency baking.** Carry a prebuilt wheelhouse (built in the same ABI-matched container) in
  the bundle so the box installs `pip_extras` offline, removing the runtime PyPI dependency.
  Deferred to a v2 bundle member.
- **Rollout across in-flight boxes.** A box provisioned before this ships runs the old worker and
  cannot read `bundle.tar`. Rollout note (not a code change): let existing boxes drain or tear them
  down after `dispatch-restart`; boxes are ephemeral and this matches how coordinator changes
  already roll out. The new worker additionally tolerates a legacy loose-file delivery as
  defense-in-depth.
