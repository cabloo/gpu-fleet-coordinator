"""Task bundle — the packaged payload contract (docs/specs/task-bundle.spec.md).

Imported by BOTH the home dispatcher (`dispatcher.py`, build side) and the box-resident worker
(`spool_worker.py`, unpack/verify side) as a sibling module — exactly like `spool_worker` imports
`sweep_supervisor`. Therefore the module top-level imports **stdlib only**, so the worker runs on a
bare `pytorch/pytorch` box image; `cryptography` is imported lazily inside the signing helpers and
its absence degrades gracefully.

A bundle is an *uncompressed* tar (`bundle.tar`) whose members are:

    manifest.json   -- binds every other member by sha256 (+ optional ed25519 signature)
    code.tar.gz     -- gzipped tar of the code tree (git archive, or compiled tree)
    task.json       -- the task descriptor (schema owned by task-dispatcher.spec.md)
    resume.pt       -- optional resume checkpoint bytes

The outer tar is uncompressed on purpose: its members are already compressed (gz) or incompressible
(a torch checkpoint), so an outer gzip would only burn CPU.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

# v2 (2026-08-01, ship-artifact-build inv. 11): a bundle may carry `code_ref` INSTEAD of an embedded
# `code.tar.gz` — the code lives once per box under `~/spool/blobs/<blob_id>.tar.gz` and the manifest
# binds it by sha256 exactly as an embedded member would be. v1 bundles (embedded code) are still
# accepted and still produced for a box whose worker predates the shared blob dir.
BUNDLE_VERSION = 1              # embedded code — what a pre-inv-11 worker understands
BUNDLE_VERSION_CODE_REF = 2     # code lives in the box's shared blob dir
SUPPORTED_BUNDLE_VERSIONS = (BUNDLE_VERSION, BUNDLE_VERSION_CODE_REF)
BLOBS_DIRNAME = "blobs"

# Gzip level for every `w:gz` in the build path (inv. 7d). `tarfile` DEFAULTS TO 9, and on the real
# 12 MB compiled tree that measured 6.28s against 1.73s at level 6 — 3.6x the time for 0.8% of the
# bytes — paid twice per build (`_retar` + `overlay_entry_source`). The box only ever gunzips, and
# decompression does not depend on the level, so nothing downstream can observe this.
GZIP_LEVEL = 6

# Per-module .so cache (inv. 7c). Bump `SO_CACHE_VER` to invalidate every cached extension — a
# change in what a cached `.so` MEANS, as opposed to a change in the inputs the key already names.
SO_CACHE_VER = "so1"
SO_CACHE_MAX = 4000            # LRU cap by entry count; ~1.3 GB at the measured 336 KB median
SO_CACHE_DIRNAME = "socache"

BUNDLE_NAME = "bundle.tar"
MANIFEST_NAME = "manifest.json"
CODE_NAME = "code.tar.gz"
TASK_NAME = "task.json"
RESUME_NAME = "resume.pt"


class BundleError(Exception):
    """Any integrity/signature/format failure (a trust-boundary rejection on the box)."""


class CompileFailed(BundleError):
    """The compile step itself failed on code the compiler REJECTS (a code bug — e.g. a Cython-hostile
    construct), as opposed to a toolchain/ABI/env limitation (plain BundleError). The dispatcher fails
    such a task fast (`claimed → task_failed`) instead of shipping source, because shipping source
    would silently run uncached + un-hidden forever and retrying re-fails the same code — whereas an
    env limitation legitimately falls back to source so the fleet keeps running. Task-bundle inv. 6."""


# --------------------------------------------------------------------------------------------
# Hashing / manifest helpers (pure)
# --------------------------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(manifest: dict) -> bytes:
    """Signature domain: the manifest with `signature` set to null, canonically serialized. Both
    signer and verifier reduce to these exact bytes so a manifest can carry its own signature."""
    m = dict(manifest)
    m["signature"] = None
    return json.dumps(m, sort_keys=True, separators=(",", ":")).encode()


def _member(data: bytes) -> dict:
    return {"sha256": _sha256(data), "bytes": len(data)}


# --------------------------------------------------------------------------------------------
# Signing (optional, ed25519 — home signs with a private key, the box verifies with the public
# key; the private key never leaves home, preserving task-dispatcher invariant 13).
# --------------------------------------------------------------------------------------------

def sign_available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def _load_private_key(sign_key: str | Path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    data = Path(sign_key).expanduser().read_bytes()
    return load_pem_private_key(data, password=None)


def _load_public_key(public_key: str | Path):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    data = Path(public_key).expanduser().read_bytes()
    return load_pem_public_key(data)


def _key_id(public_bytes: bytes) -> str:
    return _sha256(public_bytes)[:8]


def sign_manifest(manifest: dict, sign_key: str | Path) -> dict:
    """Return the `signature` block for `manifest` (does not mutate it). Requires `cryptography`."""
    from cryptography.hazmat.primitives import serialization
    priv = _load_private_key(sign_key)
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    sig = priv.sign(_canonical(manifest))
    return {"alg": "ed25519", "key_id": _key_id(pub_pem), "sig": base64.b64encode(sig).decode()}


def verify_manifest_signature(manifest: dict, public_key: str | Path) -> bool:
    from cryptography.exceptions import InvalidSignature
    sig_block = manifest.get("signature")
    if not sig_block:
        return False
    try:
        pub = _load_public_key(public_key)
        pub.verify(base64.b64decode(sig_block["sig"]), _canonical(manifest))
        return True
    except (InvalidSignature, KeyError, ValueError, Exception):
        return False


# --------------------------------------------------------------------------------------------
# Build (home)
# --------------------------------------------------------------------------------------------

def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tf.addfile(info, io.BytesIO(data))


def build_bundle(dest: str | Path, *, code_tar: bytes | None, task_json: dict,
                 git_sha: str, task_id: str, resume: bytes | None = None,
                 code_format: str = "git-archive",
                 sign_key: str | Path | None = None,
                 code_ref: str | None = None) -> Path:
    """Write a bundle.tar to `dest` and return it. `code_tar` is a gzipped tar of the code tree.

    **`code_ref` (inv. 11)** names a blob already on the box (`~/spool/blobs/<code_ref>.tar.gz`)
    instead of embedding the tree. The manifest still binds the code by sha256 — under `code_ref`
    rather than under `members` — so the box verifies byte-for-byte exactly as before; the only
    change is WHERE the bytes come from. `code_tar` is still required (it is what the sha is taken
    over) but is not written into the tar.

    Why: a box running N cells of one sweep was receiving N copies of an identical ~36 MB tree.
    Measured 2026-07-31, 496 of 905 placements (55%) re-sent a tree the box already held, worst case
    16 copies to one box."""
    dest = Path(dest)
    task_bytes = json.dumps(task_json).encode()
    members = {TASK_NAME: _member(task_bytes)}
    if code_ref is None:
        members[CODE_NAME] = _member(code_tar)
    if resume is not None:
        members[RESUME_NAME] = _member(resume)
    manifest = {
        # The version bumps ONLY for the code_ref form. A pre-inv-11 worker rejects any version it
        # does not know, so emitting 2 unconditionally would break every box still running the old
        # worker — the dispatcher therefore keeps sending v1 to those (see `_box_supports_blobs`).
        "bundle_version": BUNDLE_VERSION if code_ref is None else BUNDLE_VERSION_CODE_REF,
        "task_id": task_id,
        "git_sha": git_sha,
        "code_format": code_format,
        "members": members,
        "signature": None,
    }
    if code_ref is not None:
        manifest["code_ref"] = {"blob_id": code_ref, **_member(code_tar)}
    if sign_key is not None:
        manifest["signature"] = sign_manifest(manifest, sign_key)
    manifest_bytes = json.dumps(manifest).encode()

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w") as tf:  # uncompressed outer tar (members already compressed)
        _add_bytes(tf, MANIFEST_NAME, manifest_bytes)
        if code_ref is None:
            _add_bytes(tf, CODE_NAME, code_tar)
        _add_bytes(tf, TASK_NAME, task_bytes)
        if resume is not None:
            _add_bytes(tf, RESUME_NAME, resume)
    return dest


# --------------------------------------------------------------------------------------------
# Read / verify / unpack (box — trust boundary)
# --------------------------------------------------------------------------------------------

def _read_all_members(bundle: str | Path) -> dict[str, bytes]:
    try:
        with tarfile.open(bundle, "r") as tf:
            out = {}
            for info in tf.getmembers():
                if not info.isfile():
                    raise BundleError(f"bundle member {info.name!r} is not a regular file")
                out[info.name] = tf.extractfile(info).read()
            return out
    except tarfile.TarError as e:
        raise BundleError(f"not a readable tar: {e}") from e


def read_manifest(bundle: str | Path) -> dict:
    members = _read_all_members(bundle)
    if MANIFEST_NAME not in members:
        raise BundleError(f"bundle missing {MANIFEST_NAME}")
    try:
        return json.loads(members[MANIFEST_NAME])
    except json.JSONDecodeError as e:
        raise BundleError(f"malformed manifest: {e}") from e


def verify_bundle(bundle: str | Path, *, public_key: str | Path | None = None,
                  require_signature: bool = False) -> dict:
    """Verify every member's sha256 (and the signature, if required). Return the manifest.
    Raises BundleError on any mismatch. Does not extract anything."""
    members = _read_all_members(bundle)
    if MANIFEST_NAME not in members:
        raise BundleError(f"bundle missing {MANIFEST_NAME}")
    try:
        manifest = json.loads(members[MANIFEST_NAME])
    except json.JSONDecodeError as e:
        raise BundleError(f"malformed manifest: {e}") from e

    if not isinstance(manifest, dict) or \
            manifest.get("bundle_version") not in SUPPORTED_BUNDLE_VERSIONS:
        raise BundleError(f"unsupported bundle version: {manifest.get('bundle_version')!r}")
    declared = manifest.get("members")
    # A v2 (code_ref) bundle carries no `code.tar.gz` member — the code is the box's shared blob and
    # is bound by `manifest.code_ref.sha256` instead, checked in `unpack_bundle` where it is read.
    ref = manifest.get("code_ref")
    code_required = not (isinstance(ref, dict) and ref.get("blob_id") and ref.get("sha256"))
    if not isinstance(declared, dict) or TASK_NAME not in declared or \
            (code_required and CODE_NAME not in declared):
        raise BundleError("manifest.members missing required entries")

    present = set(members) - {MANIFEST_NAME}
    if present != set(declared):
        raise BundleError(f"member set mismatch: tar has {sorted(present)}, "
                          f"manifest declares {sorted(declared)}")
    for name, meta in declared.items():
        data = members[name]
        if len(data) != meta.get("bytes") or _sha256(data) != meta.get("sha256"):
            raise BundleError(f"member {name!r} failed integrity check")

    signed = bool(manifest.get("signature"))
    if require_signature:
        if public_key is None:
            raise BundleError("signature required but no public key provided")
        if not signed or not verify_manifest_signature(manifest, public_key):
            raise BundleError("missing or invalid bundle signature")
    return manifest


def _safe_extract_targz(blob: bytes, dest: Path) -> None:
    """Extract a gzipped tar to `dest`, refusing absolute paths / `..` traversal (invariant 10)."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for info in tf.getmembers():
            target = (dest / info.name).resolve()
            if target != root and root not in target.parents:
                raise BundleError(f"unsafe path in code tar: {info.name!r}")
        tf.extractall(dest)


def unpack_bundle(bundle: str | Path, dest: str | Path, *,
                  public_key: str | Path | None = None,
                  require_signature: bool = False, validate=None,
                  blob_dir: str | Path | None = None) -> dict:
    """Verify the bundle, then extract task.json + repo/ + resume.pt into `dest`. Return the parsed
    task.json dict. Nothing is extracted if verification fails.

    `validate`, if given, is called with the parsed task.json BEFORE `repo/` is extracted; if it
    returns a truthy error string, unpack raises BundleError and nothing is extracted — so the box
    worker's strict task.json key-check runs before any code lands (reject-without-executing)."""
    dest = Path(dest)
    verify_bundle(bundle, public_key=public_key, require_signature=require_signature)
    members = _read_all_members(bundle)  # already integrity-verified above

    task_bytes = members[TASK_NAME]
    try:
        task = json.loads(task_bytes)
    except json.JSONDecodeError as e:
        raise BundleError(f"malformed task.json: {e}") from e
    if validate is not None:
        err = validate(task)
        if err:
            raise BundleError(f"task.json rejected: {err}")

    dest.mkdir(parents=True, exist_ok=True)
    code_bytes = members.get(CODE_NAME)
    if code_bytes is None:                      # inv. 11: the code lives once per box
        ref = read_manifest(bundle).get("code_ref") or {}
        blob_id, want = ref.get("blob_id"), ref.get("sha256")
        if not blob_id or not want:
            raise BundleError("bundle carries neither code.tar.gz nor a usable code_ref")
        if blob_dir is None:
            raise BundleError(f"bundle references blob {blob_id} but no blob_dir was given "
                              "(this worker predates inv. 11 — redeploy it)")
        path = Path(blob_dir) / f"{blob_id}.tar.gz"
        try:
            code_bytes = path.read_bytes()
            with contextlib.suppress(OSError):
                os.utime(path, None)      # LRU touch — the box prunes blobs by last USE (inv. 11)
        except OSError as e:
            raise BundleError(f"referenced code blob {blob_id} unreadable at {path}: {e}") from e
        got = _sha256(code_bytes)
        if got != want:
            # The shared blob is the ONE thing not covered by the outer tar's own integrity check,
            # so it gets the same treatment explicitly: a mismatch means the box's copy is corrupt
            # or stale, and extracting it would run code nobody authorised.
            raise BundleError(f"code blob {blob_id} FAILED integrity: sha256 {got[:12]} != "
                              f"manifest {want[:12]}")
    _safe_extract_targz(code_bytes, dest / "repo")
    (dest / TASK_NAME).write_bytes(task_bytes)
    if RESUME_NAME in members:
        (dest / RESUME_NAME).write_bytes(members[RESUME_NAME])
    return task


# --------------------------------------------------------------------------------------------
# Entry-source overlay — lets ONE compile-everything tree (cached per commit) serve every trainer.
# --------------------------------------------------------------------------------------------

def _read_member(tar_gz: bytes, name: str) -> bytes | None:
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tf:
        for cand in (name, "./" + name):
            try:
                f = tf.extractfile(cand)
            except KeyError:
                continue
            if f is not None:
                return f.read()
    return None


def overlay_entry_source(compiled_tar: bytes, entry_rel: str, source_tar: bytes) -> bytes:
    """Return `compiled_tar` with the entry module restored to source: its compiled `.so` removed
    and its original `.py` (from `source_tar`) put back — so `python -m <entry>` works while every
    other module stays compiled. This lets a single compile-everything tree (cached once per commit)
    serve every trainer by swapping only the run entry's file. If the entry module wasn't compiled
    (e.g. a script outside the compiled packages), its `.py` is already present and this just
    re-affirms it. `entry_rel` is repo-relative, e.g. "src/native/training/m36.py"."""
    py_bytes = _read_member(source_tar, entry_rel)
    if py_bytes is None:
        raise BundleError(f"entry source {entry_rel!r} not found in the source tree")
    stem = entry_rel[:-3] if entry_rel.endswith(".py") else entry_rel  # "src/native/training/m36"
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(compiled_tar), mode="r:gz") as src, \
            tarfile.open(fileobj=out, mode="w:gz", compresslevel=GZIP_LEVEL) as dst:
        for m in src.getmembers():
            norm = m.name.lstrip("./")
            # drop the entry's compiled .so (e.g. src/native/training/m36.cpython-…so) and any stale .py
            if norm == entry_rel or (norm.startswith(stem + ".") and norm.endswith(".so")):
                continue
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        info = tarfile.TarInfo(entry_rel)
        info.size, info.mode = len(py_bytes), 0o644
        dst.addfile(info, io.BytesIO(py_bytes))
    return out.getvalue()


# --------------------------------------------------------------------------------------------
# Compilation (Part 2 — bar-raising, NOT confidentiality; the build interpreter must match the
# box's Python version so the produced .so load there).
# --------------------------------------------------------------------------------------------

# The compile driver (runs under the build interpreter). It cythonizes each *.py under `packages`
# to an extension .so and deletes the .py, EXCEPT paths in `keep_source` (the entry modules — a
# compiled extension can't be `python -m`-run). `packages`/`keep_source` are POSIX paths relative
# to the repo root (e.g. "src/native", "src/native/training/m36.py"), matching how the tree is
# extracted on the box.
# argv: <repo_root> <packages_csv> <keep_csv> [<source_root> [<so_cache_dir> [<cache_max>]]]
#
# ⛔ THE POSITIONS OF argv[1..4] ARE FROZEN — `tests/test_bundle.py`'s stub compiler reads
# `cmd[2..4]` positionally, and so would anyone stubbing this. New arguments APPEND.
_COMPILE_DRIVER = r"""
import os, sys, pathlib, shutil, hashlib
from setuptools import setup
from Cython.Build import cythonize
import Cython
root = pathlib.Path(sys.argv[1])
packages = [p for p in sys.argv[2].split(",") if p]
keep = {p for p in sys.argv[3].split(",") if p}
source_root = sys.argv[4] if len(sys.argv) > 4 else "src"   # PYTHONPATH root, e.g. "src"
so_cache = pathlib.Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
cache_max = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else 4000
# CWD = tree root; module names come out src-stripped (e.g. "native.models.agent"). Pass the src layout
# explicitly via package_dir {"" : source_root} so build_ext --inplace places each .so back under
# `<source_root>/native/agent.<abi>.so` in the tree (WITHOUT this, distutils writes to `native/`
# relative to CWD, which doesn't exist -> "could not create ...: No such file or directory").
os.chdir(root)
targets = []  # root-relative posix paths, e.g. "src/native/models/agent.py"
for pkg in packages:
    for py in sorted((root / pkg).rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        # Keep __init__.py as source: compiling a package-init extension is finicky, and inits are
        # trivial import glue, not the model/training logic worth compiling.
        if rel in keep or py.name == "__init__.py":
            continue
        targets.append(rel)
# A generated `.c` sitting next to its `.py` is a leftover build artifact. Report it, don't block.
#
# ⚠ CORRECTION (2026-08-08). An earlier version of this block RAISED SystemExit here, on the claim
# that a sibling `.c` SHADOWS the source in the compiled build. **That claim was wrong.** We call
# `cythonize(..., build_dir=root/".cybuild")` below, so generated C is written THERE and a sibling
# `.c` is never an input to anything. Checked against the real artifact: ship blob
# `04fd9543aa217f65475fcc3f`, built while eight `.c` files were committed, contains `_body_rule_for`,
# `whiten`, `pc_Wd` and `sero_const` inside its `module_graph…so` — all added to the `.py` long after
# that `.c` was frozen (2026-07-26). The `.py` is what got compiled.
#
# So this is HYGIENE, and a hard refusal would be a self-inflicted outage: anyone with a stray `.c`
# from a local Cython run could not ship, for a condition that changes nothing. A warning keeps the
# tree clean without holding the fleet hostage to it.
_leftover = sorted(rel for rel in targets if (root / rel).with_suffix(".c").exists())
if _leftover:
    print("warning: leftover generated .c file(s) beside their .py (build artifacts; harmless here "
          "because generated C goes to .cybuild/, but they do not belong in the tree):\n  "
          + "\n  ".join(f"{r[:-3]}.c" for r in _leftover))

# -- Per-module .so cache (task-bundle inv. 7c) ------------------------------------------------
# The blob cache one level up is keyed by the WHOLE working tree, so a spec/config/README edit
# misses it while changing no compiled module at all (measured: 73% of commits touch zero). Here
# each module is addressed by its OWN source plus the toolchain, so a miss recompiles only what
# actually differs.
#
# ⛔ THIS KEY IS COMPLETE ONLY BECAUSE THE TREE HAS NO .pxd, NO cimport AND NO `# cython:`
# DIRECTIVE — the three ways one module's generated C can depend on another file. That premise is
# asserted by `test_no_cross_module_cython_inputs`; if it ever breaks, this key silently ships
# STALE BINARIES, which is the worst failure class this fleet has.
EXT = __import__("sysconfig").get_config_var("EXT_SUFFIX")
FLAGS = ["-O0", "-g0"]
# Every toolchain input that can change the produced bytes. `EXT` carries the Python ABI and the
# platform triple; `Cython.__version__` the code generator; the interpreter version the headers.
_SIG = "|".join(["__SO_CACHE_VER__", EXT or "", Cython.__version__,
                 "%d.%d" % sys.version_info[:2], "lang3", ",".join(FLAGS)])


def _so_key(rel, src_bytes):
    h = hashlib.sha256()
    # The module NAME is derived from `rel`, so the path is part of the identity, not just the body.
    h.update(("%s|%s|" % (_SIG, rel)).encode())
    h.update(src_bytes)
    return h.hexdigest()


def _so_dest(rel):
    return root / (rel[:-3] + EXT)          # "src/a/b.py" -> "src/a/b.<abi>.so", where build_ext puts it


hits, misses, keys = [], [], {}
if so_cache is not None:
    try:
        so_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        so_cache = None
for rel in targets:
    if so_cache is None:
        misses.append(rel)
        continue
    keys[rel] = k = _so_key(rel, (root / rel).read_bytes())
    src = so_cache / (k + ".so")
    try:
        # COPY, never symlink: these bytes get tarred and shipped to a box that has no such cache.
        shutil.copyfile(src, _so_dest(rel))
    except OSError:
        misses.append(rel)
        continue
    try:
        os.utime(src, None)                 # LRU touch, so the trim below keeps what we still use
    except OSError:
        pass
    hits.append(rel)

if misses:
    exts = cythonize(misses, quiet=True, language_level=3,
                     build_dir=str(root / ".cybuild"), nthreads=4)
    for e in exts:
        # We compile for CODE-HIDING, not runtime speed, so skip optimization/debug info: -O0 -g0
        # roughly halves the C-compile time and the .so run the same Python-level logic. Appended
        # after distutils' default -O2, and gcc lets the later flag win.
        e.extra_compile_args = list(getattr(e, "extra_compile_args", None) or []) + FLAGS
    setup(script_args=["build_ext", "--inplace", "-q"],
          package_dir={"": source_root}, ext_modules=exts)
    for rel in misses:                      # publish what we just built, for the next build
        built, k = _so_dest(rel), keys.get(rel)
        if so_cache is None or not k or not built.exists():
            continue
        tmp = so_cache / (".%s.%d.tmp" % (k, os.getpid()))
        try:
            shutil.copyfile(built, tmp)
            os.replace(tmp, so_cache / (k + ".so"))   # same content under the key either way
        except OSError:
            pass
        finally:
            if tmp.exists():
                tmp.unlink()

for rel in targets:  # drop the .py (and generated .c) so only the .so ships
    (root / rel).unlink(missing_ok=True)
    (root / rel).with_suffix(".c").unlink(missing_ok=True)
shutil.rmtree(root / ".cybuild", ignore_errors=True)
shutil.rmtree(root / "build", ignore_errors=True)

if so_cache is not None:                    # LRU trim by mtime; a pure accelerator, so failures pass
    try:
        entries = sorted(so_cache.glob("*.so"), key=lambda p: p.stat().st_mtime)
        for p in entries[:max(0, len(entries) - cache_max)]:
            p.unlink(missing_ok=True)
    except OSError:
        pass
print("compiled %d modules (%d from cache, %d built)" % (len(targets), len(hits), len(misses)))
"""

_DOCKER_SCRIPT = (
    "set -euo pipefail\n"
    "pip install --quiet --disable-pip-version-check cython setuptools >/dev/null\n"
    "python /work/driver.py /work/repo \"$1\" \"$2\" \"$3\" \"$4\" \"$5\"\n"
    "tar -C /work/repo -czf /work/out.tar.gz .\n"
)


#: A compiler diagnostic: `path:line[:col]: message`. The canonical form for BOTH Cython
#: (`src/native/evo/world.py:72:17: undeclared name not builtin: move_of`) and gcc, which is what
#: makes one pattern enough for the whole build.
_DIAG_RE = re.compile(r"^[^\s:][^\s]*:\d+(?::\d+)?:\s+\S")


def _compiler_digest(stderr: str, *, max_diags: int = 8, tail_chars: int = 500) -> str:
    """The part of a compiler's stderr a HUMAN needs, not the part that happens to be last.

    WHY THIS EXISTS. Both raise sites below used to report `stderr[-500:]`, and for a parallel
    `cythonize` the last 500 characters are *always* `concurrent.futures` traceback boilerplate —
    the executor re-raises in the parent, so the frames that print last belong to the plumbing, not
    to the code under compilation. Measured on the real failure this was written for: 3619 bytes of
    stderr, the actual diagnostic on **line 13**, and the reported tail contained nothing but
    `_base.py` frames plus a mid-word cut (`ncel`). The message named the failing FILE and withheld
    the one line saying what was wrong with it, so `runq` reported "the compiler rejected this code"
    while hiding the rejection — a queue-blocking error that had to be reproduced by re-running the
    compiler serially by hand to read.

    So: surface the `path:line:col: message` diagnostics, deduped and capped, and fall back to the
    tail only when there are none (a linker/setup.py failure, where the tail genuinely is the story).
    """
    text = stderr or ""
    seen: set[str] = set()
    diags: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in seen or not _DIAG_RE.match(line):
            continue
        seen.add(line)
        diags.append(line)
        if len(diags) > max_diags:          # one past the cap, so we can SAY it was truncated
            break
    if not diags:
        return text[-tail_chars:].strip()
    shown, extra = diags[:max_diags], max(0, len(diags) - max_diags)
    return " ; ".join(shown) + (f" ; (+{extra} more)" if extra else "")


def _ext_suffix(python_bin: str, run) -> str:
    r = run([python_bin, "-c", "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))"],
            capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise BundleError(f"could not read build interpreter ABI: {r.stderr[-200:]}")
    return (r.stdout or "").strip()


def _retar(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=GZIP_LEVEL) as tf:
        tf.add(root, arcname=".")
    return buf.getvalue()


def compile_tree(code_tar: bytes, *, packages: list[str], keep_source: list[str],
                 image: str, run=subprocess.run, work_root: str | Path | None = None,
                 backend: str = "local", python_bin: str | None = None,
                 expected_abi: str | None = None, source_root: str = "src",
                 cache_dir: str | Path | None = None,
                 cache_max: int = SO_CACHE_MAX) -> bytes:
    """Compile the code tree to extension modules, returning a new code.tar.gz. Raises BundleError
    if the toolchain is unavailable or compilation fails (the caller decides fallback). `code_tar`
    is a gzipped tar of the repo tree (packages at `src/…`, matching `git archive`);
    `packages`/`keep_source` are paths relative to the repo root.

    backend="local" (default): compile with `python_bin` (must have cython+setuptools; defaults to
    the running interpreter) directly on the coordinator host — no container. Guards the build
    interpreter's `EXT_SUFFIX` against `expected_abi` (the box's Python ABI) and refuses on
    mismatch, so a coordinator whose Python differs from the box fails loud rather than shipping an
    unloadable `.so`. Correct here because the coordinator devcontainer and the box image share the
    conda-py3.11-x86_64 ABI.

    backend="docker": compile inside a container from `image` (an exact ABI match, but needs Docker
    on the coordinator host, and a `-runtime` box image lacks a compiler).

    `cache_dir` is the per-module `.so` cache of invariant 7c — only the modules whose source
    changed are cythonized, so the common "edited a spec, nothing under `packages`" build compiles
    NOTHING. Passing None disables it, which costs speed and never correctness."""
    import shutil
    import tempfile

    if backend == "docker":
        if not shutil.which("docker"):
            raise BundleError("docker unavailable for compile_tree (backend=docker)")
    elif backend == "local":
        py = python_bin or sys.executable
        if expected_abi:
            got = _ext_suffix(py, run)
            if expected_abi not in got:
                raise BundleError(
                    f"local compile ABI mismatch: build interpreter EXT_SUFFIX {got!r} does not "
                    f"match expected box ABI {expected_abi!r}")
    else:
        raise BundleError(f"unknown compile backend {backend!r}")

    cache = Path(cache_dir).expanduser() if cache_dir else None
    if cache is not None:
        # Make it before the build so a docker `-v` mount cannot create it root-owned.
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache = None
    tmp = Path(tempfile.mkdtemp(dir=str(work_root) if work_root else None, prefix="bundle-compile-"))
    try:
        _safe_extract_targz(code_tar, tmp / "repo")
        # Substituted, not duplicated: the driver is a string, so a literal "so1" inside it would
        # drift from SO_CACHE_VER silently — and a stale version tag is the one bug this cache
        # cannot survive (it would serve extensions built under a meaning that no longer holds).
        (tmp / "driver.py").write_text(
            _COMPILE_DRIVER.replace("__SO_CACHE_VER__", SO_CACHE_VER))
        if backend == "docker":
            (tmp / "compile.sh").write_text(_DOCKER_SCRIPT)
            cmd = ["docker", "run", "--rm", "-v", f"{tmp}:/work"]
            if cache is not None:
                cmd += ["-v", f"{cache}:/socache"]
            cmd += ["-w", "/work", image,
                    "bash", "/work/compile.sh", ",".join(packages), ",".join(keep_source),
                    source_root, "/socache" if cache is not None else "", str(cache_max)]
            out = run(cmd, capture_output=True, text=True, timeout=1800)
            if out.returncode != 0:
                raise CompileFailed(f"compile_tree failed (rc={out.returncode}): {_compiler_digest(out.stderr)}")
            result = tmp / "out.tar.gz"
            if not result.exists():
                raise CompileFailed("compile_tree produced no output tar")
            return result.read_bytes()
        # local backend
        cmd = [python_bin or sys.executable, str(tmp / "driver.py"), str(tmp / "repo"),
               ",".join(packages), ",".join(keep_source), source_root,
               str(cache) if cache is not None else "", str(cache_max)]
        out = run(cmd, capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            raise CompileFailed(f"compile_tree failed (rc={out.returncode}): {_compiler_digest(out.stderr)}")
        return _retar(tmp / "repo")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
