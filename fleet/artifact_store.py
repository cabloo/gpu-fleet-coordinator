"""Ship-artifact store — the QUEUER builds, the coordinator is BLIND.

Spec: `docs/specs/ship-artifact-build.spec.md`.

A **ship-ready** `code.tar.gz` (compiled + entry-overlaid, i.e. exactly the bytes `_ship` puts in the
bundle) is built by whoever queues the task and stored here, content-addressed by the FULL build
input set. The coordinator then reads a blob and pushes it; it never compiles, never consults a
compile cache, and never needs an ABI or a toolchain.

Why the address includes the entry path: `overlay_entry_source` is per-TASK, not per-snapshot — one
compiled-everything tree serves many trainers, each restoring its own entry module's `.py`. Cells of
one sweep share an entry, so a 12-cell sweep still builds once (spec inv. 4).

Home-side only — never imported on the box.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import registry_db

# Bump to invalidate every stored blob (a change in what "ship-ready" means, not in its inputs).
STORE_VER = "blob1"
STORE_SUBDIR = (".dispatcher", "blobs")
SO_CACHE_SUBDIR = (".dispatcher", "socache")
SUFFIX = ".tar.gz"

# Build recipe fallbacks, used when the `settings` table does not carry a key. These MUST match
# `dispatcher.DEFAULT_SETTINGS` — `tests/test_artifact_store.py` asserts it, so a divergence is a
# test failure rather than a silently mismatched ABI shipped to a box.
RECIPE_DEFAULTS = {
    "bundle_compile": True,
    "bundle_compile_abi": "cpython-312-x86_64-linux-gnu",
    "bundle_compile_packages": ["src/native", "src/shared"],
    "bundle_compile_backend": "local",
    "bundle_compile_python": None,
    "bundle_compile_image": None,     # resolved from settings; only used by backend="docker"
}
RECIPE_KEYS = tuple(RECIPE_DEFAULTS)


class BuildFailed(Exception):
    """The queuer could not produce a ship-ready artifact (spec inv. 13).

    Carries `fatal_hint` — the one-line guidance printed to the operator. Raised for BOTH failure
    classes the owner collapsed into one outcome (2026-07-31): a compiler-rejected code bug and an
    unavailable toolchain both blow the whole task and hand it back to the requester. There is no
    source fallback: `runq` exits non-zero and inserts nothing."""

    def __init__(self, message: str, *, fatal_hint: str = ""):
        super().__init__(message)
        self.fatal_hint = fatal_hint


def store_dir(experiments_root: str | Path) -> Path:
    return Path(experiments_root).joinpath(*STORE_SUBDIR)


def so_cache_dir(experiments_root: str | Path) -> Path:
    """Where `compile_tree` keeps its per-module `.so` cache (task-bundle inv. 7c).

    Under the SHARED experiments root, beside the blob store, so every worktree warms one cache."""
    return Path(experiments_root).joinpath(*SO_CACHE_SUBDIR)


def blob_id(code_hash: str, abi: str, packages: list, entry_source_path: str | None,
            code_format: str) -> str:
    """Content address over every input that can change the shipped bytes (spec Output contract).

    `code_hash` pins the source (a working-tree content address from `code_snapshot`, so it covers
    uncommitted edits too), `abi` the compiler, `packages` the compiled set, `entry_source_path` the
    per-task overlay, `code_format` compiled-vs-source. Same inputs -> same id -> reuse without
    rebuilding; any input changing produces a different id, so a stale binary can never be served."""
    payload = "|".join((STORE_VER, code_hash or "", abi or "", ",".join(sorted(packages or [])),
                        entry_source_path or "", code_format or ""))
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def digest(data: bytes) -> str:
    """The content digest recorded on the task row and re-checked by the coordinator (inv. 6)."""
    return hashlib.sha256(data).hexdigest()


def blob_path(experiments_root: str | Path, bid: str) -> Path:
    return store_dir(experiments_root) / f"{bid}{SUFFIX}"


def put(experiments_root: str | Path, bid: str, data: bytes) -> bytes:
    """Publish IMMUTABLY and return the bytes that are ACTUALLY published (spec inv. 5).

    ⛔ FIRST PUBLISHER WINS, AND THE RETURN VALUE IS THE POINT. This used to `os.replace`, on the
    reasoning that "two queuers racing the same id both succeed and the store holds one valid blob".
    That is true of the STORE and false of a BOX, and the difference cost a task:

        `blob_id` addresses the BUILD INPUTS (code hash + ABI + packages + entry + format), NOT the
        bytes it names — and `build_ship_ready`'s tar.gz is not byte-reproducible. So two builds of
        one tree mint the SAME id with DIFFERENT bytes. A box caches the blob by id and verifies it
        by sha256 (inv. 6), so once queuer A has shipped its bytes, overwriting the store with
        queuer B's bytes makes every later task recording B's digest fail integrity against A's
        cached copy — reported as `ZERO PROGRESS (likely code/config bug, not infra)`, which is
        neither.

    Measured live 2026-08-14 (`pcbed_reopen/cb_bias`): two concurrent `runq add` calls on one tree
    both missed `load()`, both spent ~8 min building, and the second overwrote the first. The box
    had already cached the first, and rejected the second with
    `sha256 3d68fe6190ff != manifest 9d465425d297`.

    Create-if-absent via `os.link`, which is atomic on POSIX and fails rather than clobbers — so a
    loser publishes nothing and simply reads the winner's bytes. A reader still never sees a partial
    file, which was the original property and is preserved.
    """
    d = store_dir(experiments_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{bid}{SUFFIX}"
    if path.exists():
        return path.read_bytes()
    tmp = d / f".{bid}{SUFFIX}.tmp.{os.getpid()}"
    tmp.write_bytes(data)
    try:
        os.link(tmp, path)            # atomic create-if-absent; a concurrent winner makes this raise
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return path.read_bytes()


def load(experiments_root: str | Path, bid: str) -> bytes | None:
    """The blob's bytes, or None if absent. Never raises on a missing store."""
    if not bid:
        return None
    try:
        return blob_path(experiments_root, bid).read_bytes()
    except OSError:
        return None


def has(experiments_root: str | Path, bid: str) -> bool:
    p = blob_path(experiments_root, bid)
    if p.is_file():
        with_suppress_utime(p)
        return True
    return False


def with_suppress_utime(path: Path) -> None:
    """Bump mtime for LRU without letting a read-only/odd filesystem break a ship."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def live_blob_ids(conn) -> set:
    """Blobs referenced by a task that has not reached a terminal state. `gc` must never evict one
    of these: the compile cache this replaces was LRU-capped with NO reference to open tasks, so a
    busy period could evict the very tree a queued task was waiting to ship (spec inv. 7)."""
    rows = conn.execute(
        "SELECT DISTINCT code_blob FROM tasks WHERE code_blob IS NOT NULL AND state NOT IN "
        "('done','cancelled','task_failed','infra_failed')").fetchall()
    return {r[0] for r in rows if r[0]}


def gc(experiments_root: str | Path, live: set, keep_max: int) -> int:
    """Drop unreferenced blobs, oldest-first, until at most `keep_max` remain. Returns the number
    removed. Refcount FIRST (inv. 7): a live blob is retained regardless of `keep_max`."""
    d = store_dir(experiments_root)
    if not d.is_dir():
        return 0
    entries = []
    for p in d.glob(f"*{SUFFIX}"):
        bid = p.name[: -len(SUFFIX)]
        if bid in live:
            continue
        try:
            entries.append((p.stat().st_mtime, p))
        except OSError:
            continue
    removed = 0
    surplus = len(entries) + len(live) - max(1, int(keep_max))
    for _mtime, p in sorted(entries):
        if removed >= surplus:
            break
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def recipe(conn) -> dict:
    """The build recipe from the shared `settings` table, falling back to `RECIPE_DEFAULTS`.

    Kept in the registry (Q2) so one row governs every worktree's ABI — splitting it across a client
    config would let two sessions build incompatible `.so`s for the same fleet. The coordinator
    stops READING these; that is what "the coordinator is blind" means, not that they move."""
    out = dict(RECIPE_DEFAULTS)
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    except Exception:  # noqa: BLE001 — a partial/old schema must not stop a build
        return out
    import json
    for k, v in rows:
        if k in out:
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
    return out


def resolve_build_python(rec: dict, run) -> str:
    """Base interpreter for the compile build venv — MOVED here from the coordinator 2026-07-31
    with the build itself, because it is a build concern and the coordinator no longer has one.

    It must match the BOX's Python VERSION (`.so` are version-specific), which is not necessarily
    the queuer's own (this devcontainer is 3.11, the box image 3.12). Precedence: explicit
    `bundle_compile_python`; else resolve the ABI's version via `uv python find`; else
    `sys.executable` — which then trips `compile_tree`'s ABI guard and raises, rather than shipping
    a `.so` the box cannot import. Install the matching interpreter once with `uv python install
    3.12` to enable compilation on a new machine."""
    import re
    import shutil
    import sys as _sys
    configured = rec.get("bundle_compile_python")
    if configured:
        return configured
    m = re.search(r"cpython-3(\d+)", rec.get("bundle_compile_abi") or "")
    if m and shutil.which("uv"):
        r = run(["uv", "python", "find", f"3.{m.group(1)}"],
                capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return _sys.executable


def build_venv_python(rec: dict, experiments_root: str | Path, run, bundle_mod) -> str:
    """Cached Cython build venv for backend='local' — also moved from the coordinator.

    The caller's own env carries neither cython nor setuptools (and is usually the wrong Python
    VERSION for the box), so the build runs in a venv keyed by the build interpreter's version, so
    switching build pythons never reuses a stale env. A plain venv suffices: cythonize/build_ext
    never import the code being compiled. Raises `BundleError`, which `build_ship_ready` converts
    into a `BuildFailed` — there is no source fallback any more (inv. 13)."""
    base = resolve_build_python(rec, run)
    vr = run([base, "-c", "import sys;print('%d%d'%sys.version_info[:2])"],
             capture_output=True, text=True, timeout=30)
    if vr.returncode != 0:
        raise bundle_mod.BundleError(f"build interpreter {base!r} unusable: {vr.stderr[-200:]}")
    venv = Path(experiments_root) / ".dispatcher" / f"buildenv-py{vr.stdout.strip()}"
    py = venv / "bin" / "python"
    if py.exists():
        return str(py)
    venv.parent.mkdir(parents=True, exist_ok=True)
    r = run([base, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise bundle_mod.BundleError(f"build venv create failed: {r.stderr[-300:]}")
    r = run([str(py), "-m", "pip", "install", "-q", "cython", "setuptools"],
            capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise bundle_mod.BundleError(f"build venv pip install failed: {r.stderr[-300:]}")
    return str(py)


def build_ship_ready(code_tar: bytes, *, entry_source_path: str | None, rec: dict,
                     bundle_mod, run=None, experiments_root=None) -> tuple[bytes, str]:
    """Compile + entry-overlay `code_tar` into the exact bytes the box will receive.

    Returns `(ship_ready_tar, code_format)`. Raises `BuildFailed` on ANY failure — both a compiler
    -rejected code bug and an unavailable toolchain (spec inv. 13; owner decision 2026-07-31). The
    old `BundleError -> ship source` arm is deliberately absent: it insured against the COORDINATOR's
    toolchain being a fleet-wide single point of failure, and building here makes that failure
    session-local, so the insurance now buys nothing and costs a silent downgrade to un-hidden,
    uncached source."""
    if not rec.get("bundle_compile", True):
        return code_tar, "snapshot"      # deliberate off-switch, not a degradation (Q4)
    if run is None:
        import subprocess
        run = subprocess.run       # `compile_tree` CALLS this; None would TypeError mid-build
    backend = rec.get("bundle_compile_backend", "local")
    try:
        py_bin = (build_venv_python(rec, experiments_root, run, bundle_mod)
                  if backend == "local" and experiments_root is not None else
                  rec.get("bundle_compile_python"))
    except bundle_mod.BundleError as e:
        raise BuildFailed(
            f"build toolchain unavailable: {e}",
            fatal_hint="Could not prepare the Cython build venv. This blocks THIS session only — "
                       "other sessions queue normally. `uv python install 3.12` usually fixes it, "
                       "or set bundle_compile=false to ship source deliberately. Nothing was "
                       "queued.") from e
    try:
        compiled = bundle_mod.compile_tree(
            code_tar,
            packages=list(rec["bundle_compile_packages"]),
            keep_source=[],
            image=rec.get("bundle_compile_image"),
            run=run,
            backend=backend,
            python_bin=py_bin,
            # Per-module .so cache (task-bundle inv. 7c). Sits BESIDE the blob store and under the
            # SHARED experiments root, so every worktree's `runq` warms one cache — which is the
            # point: a branch that differs from master by three files compiles three modules.
            cache_dir=(so_cache_dir(experiments_root) if experiments_root is not None else None),
            expected_abi=rec["bundle_compile_abi"])
    except bundle_mod.CompileFailed as e:
        raise BuildFailed(
            f"compile ERROR (the compiler rejected this code): {e}",
            fatal_hint="This is a code bug, not an infrastructure problem — fix it and re-queue. "
                       "Nothing was queued.") from e
    except bundle_mod.BundleError as e:
        raise BuildFailed(
            f"compile toolchain unavailable: {e}",
            fatal_hint="Your build toolchain is broken (missing interpreter / ABI mismatch / no "
                       "docker). This blocks THIS session only — other sessions queue normally. "
                       "Fix the toolchain, or set bundle_compile=false to ship source "
                       "deliberately. Nothing was queued.") from e
    if entry_source_path:
        compiled = bundle_mod.overlay_entry_source(compiled, entry_source_path, code_tar)
    return compiled, "compiled"


def build_and_store(conn, experiments_root: str | Path, *, code_tar: bytes, code_hash: str,
                    entry_source_path: str | None, bundle_mod, run=None,
                    on_reuse=None, on_build=None) -> dict:
    """The queue-time build (spec inv. 2/4/13). Returns the columns the task row records.

    Reuses an existing blob when one addresses the same inputs, so a sweep's N cells pay ONE build.
    Raises `BuildFailed` — the caller must queue nothing."""
    rec = recipe(conn)
    fmt = "compiled" if rec.get("bundle_compile", True) else "snapshot"
    bid = blob_id(code_hash, rec["bundle_compile_abi"], rec["bundle_compile_packages"],
                  entry_source_path, fmt)
    existing = load(experiments_root, bid)
    if existing is not None:
        with_suppress_utime(blob_path(experiments_root, bid))
        if on_reuse:
            on_reuse(bid)
        return {"code_blob": bid, "code_sha256": digest(existing), "code_format": fmt}
    t0 = time.monotonic()
    data, fmt = build_ship_ready(code_tar, entry_source_path=entry_source_path, rec=rec,
                                 bundle_mod=bundle_mod, run=run,
                                 experiments_root=experiments_root)
    bid = blob_id(code_hash, rec["bundle_compile_abi"], rec["bundle_compile_packages"],
                  entry_source_path, fmt)
    # ⛔ RECORD THE DIGEST OF WHAT IS PUBLISHED, NOT OF WHAT WE BUILT. `load()` above and `put()`
    # here bracket a build that takes MINUTES, so another queuer can publish this same id in
    # between — and its bytes, not ours, are what every box will hold. Recording `digest(data)`
    # made this row assert a sha nothing would ever serve. See `put`'s note.
    published = put(experiments_root, bid, data)
    if on_build:
        on_build(bid, time.monotonic() - t0)
    return {"code_blob": bid, "code_sha256": digest(published), "code_format": fmt}
