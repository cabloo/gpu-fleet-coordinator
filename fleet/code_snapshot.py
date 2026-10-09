"""Code snapshot — ship the working tree, not a git ref (docs/specs/code-snapshot.spec.md).

`make_snapshot(root)` tars **whatever is in the working directory** (committed *and* uncommitted),
using `git ls-files -co --exclude-standard` only as a `.gitignore`-aware file lister — never for
content or sha. The result is content-addressed by `code_hash` (a hash of the file manifest, not the
tar bytes), so it is deterministic regardless of tar/timestamp layout. `runq add` persists one per
task under `<experiments>/.dispatcher/snapshots/<task_id>.tar.gz`; the dispatcher ships it instead of
`git archive`, falling back to `git archive` for tasks queued before this feature.

Home-side only — never imported on the box (the box just extracts the resulting `code.tar.gz`).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

SNAPSHOTS_SUBDIR = (".dispatcher", "snapshots")  # under the experiments root (parent of runs.sqlite)

# Kept in step with `bundle.GZIP_LEVEL` (task-bundle inv. 7d) but declared locally: `code_snapshot`
# is the one build-path module that must not import a sibling to tar a file.
GZIP_LEVEL = 6

# v2 (job-artifact-contract): there is NO magic manifest filename. The run contract is a `job`
# section of the trainer config the caller names, and `make_snapshot(hoist=...)` puts THAT file at
# member 0 so it can be read from a gzip stream in O(config bytes).

# Git-optional walk (job-artifact-contract spec inv. 7): when `root` is NOT a git tree, list files
# by a plain recursive walk honoring `.dispatchignore` + this fixed default denylist. In a git tree
# nothing here applies — `git ls-files` / `.gitignore` governs exactly as before.
DISPATCHIGNORE_NAME = ".dispatchignore"
_DEFAULT_DENY_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules", ".mypy_cache",
                                ".pytest_cache", ".ipynb_checkpoints"})
_DEFAULT_DENY_SUFFIXES = (".pyc", ".pyo")


class SnapshotError(Exception):
    """File listing failed — root is missing/not a directory, or (with allow_non_git=False) not a
    git tree."""


@dataclass
class SnapshotResult:
    code_tar: bytes          # gzipped tar, members at repo-relative POSIX paths (git-archive layout)
    code_hash: str           # sha256 content address over the file manifest
    file_count: int
    total_bytes: int
    skipped: int             # non-regular / deleted entries ls-files reported but we didn't ship


def _list_files_git(root: Path) -> list[str] | None:
    """Tracked + untracked-not-ignored paths, repo-relative POSIX (invariant 1). Returns None (not
    raising) when `root` is not a git tree / git errors, so `make_snapshot` can fall back to a walk."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.split(b"\0")
    # git may report the same path twice (once cached, once other) only in odd states; dedupe.
    seen, rels = set(), []
    for p in parts:
        if not p:
            continue
        rel = p.decode("utf-8", "surrogateescape")
        if rel not in seen:
            seen.add(rel)
            rels.append(rel)
    return rels


def _load_dispatchignore(root: Path) -> list[str]:
    """Newline-separated glob patterns from `<root>/.dispatchignore` (blank lines + `#` comments
    dropped). The non-git analogue of `.gitignore`."""
    f = root / DISPATCHIGNORE_NAME
    if not f.is_file():
        return []
    pats = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line.rstrip("/"))
    return pats


def _ignored(rel: str, patterns: list[str]) -> bool:
    """True if repo-relative POSIX path `rel` matches a `.dispatchignore` pattern — as a whole-path
    glob, a directory-prefix, or a match against any single path segment (so `experiments` ignores
    `experiments/...`)."""
    segs = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + "/*"):
            return True
        if any(fnmatch.fnmatch(seg, pat) for seg in segs):
            return True
    return False


def _list_files_walk(root: Path) -> list[str]:
    """Non-git file lister (job-artifact-contract inv. 7): recursive walk honoring `.dispatchignore`
    + the default denylist. Raises SnapshotError if `root` is not a directory."""
    if not root.is_dir():
        raise SnapshotError(f"not a directory: {root}")
    patterns = _load_dispatchignore(root)
    rels: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        reldir = Path(dirpath).relative_to(root)
        # prune denied/ignored dirs in place so os.walk doesn't descend into them
        dirnames[:] = [
            d for d in dirnames
            if d not in _DEFAULT_DENY_DIRS
            and not _ignored((reldir / d).as_posix(), patterns)]
        for fn in filenames:
            rel = (reldir / fn).as_posix()
            if fn.endswith(_DEFAULT_DENY_SUFFIXES):
                continue
            if _ignored(rel, patterns):
                continue
            rels.append(rel)
    return rels


def make_snapshot(root: str | Path, *, allow_non_git: bool = True,
                  hoist: str | None = None) -> SnapshotResult:
    """Snapshot the working tree at `root` (invariants 1-5, 12). Reads live bytes from disk.

    In a git tree, listing + `.gitignore` behavior is identical to before. When `root` is not a git
    tree and `allow_non_git` (default), files are listed by a plain walk honoring `.dispatchignore`
    (job-artifact-contract inv. 7); with `allow_non_git=False` a non-git root raises SnapshotError.
    A missing/non-directory root always raises.

    `hoist` is a root-relative path written as tar member 0 (inv. 10a) so it is readable from the
    gzip stream in O(that file). v1 hardcoded `job.json`; v2 hoists whatever config the caller
    named, since no filename is magic any more. It must already be in the snapshot — a config that
    is gitignored would ship a tree whose run contract is missing, so that raises rather than
    force-including it (the v1 force-include is exactly how the mutable-singleton bug survived)."""
    root = Path(root).resolve()
    rels = _list_files_git(root)
    if rels is None:
        if not allow_non_git:
            raise SnapshotError(f"not a git tree (allow_non_git=False): {root}")
        rels = _list_files_walk(root)  # raises SnapshotError if root missing / not a dir
    # v2 (job-artifact-contract): NO force-include. v1 force-included an untracked repo-root
    # `job.json` here, which is precisely what let one mutable file supply the run contract to every
    # queued cell. The run contract now lives in the trainer config, which is a tracked file the
    # caller names explicitly, so it is already in `rels` by the ordinary ignore rules.
    if hoist is not None and hoist not in rels and (root / hoist).is_file():
        raise SnapshotError(f"config {hoist!r} is not in the snapshot (ignored or untracked) — a "
                            f"queued run must ship the config it names")
    entries = []  # (relpath, exec_bit, data)
    skipped = 0
    for rel in rels:
        p = root / rel
        # invariant 3: skip deleted-but-tracked, symlinks, dirs, and other non-regular files.
        if p.is_symlink() or not p.is_file():
            skipped += 1
            continue
        try:
            data = p.read_bytes()
        except OSError:
            skipped += 1
            continue
        exec_bit = 1 if os.access(p, os.X_OK) else 0
        entries.append((rel, exec_bit, data))

    entries.sort(key=lambda e: e[0])  # invariant 4: sorted, order-independent hash + tar

    h = hashlib.sha256()
    for rel, exec_bit, data in entries:
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(b"1" if exec_bit else b"0")
        h.update(b"\0")
        h.update(hashlib.sha256(data).hexdigest().encode())
        h.update(b"\0")
    code_hash = h.hexdigest()

    buf = io.BytesIO()
    # Members already at repo-relative posix paths (git-archive layout, invariant 5). mtime/uid
    # fixed so the tar is reproducible too, though identity is code_hash (the gzip header still
    # carries a wall-clock mtime — irrelevant, we address by code_hash not tar bytes).
    # WRITE ORDER: hoist the named config to member 0 (job-artifact-contract inv. 10a) so the run
    # contract is readable from the gzip stream in O(config bytes). This does NOT affect code_hash —
    # identity is the SORTED (relpath, exec_bit, content) manifest above, independent of write order.
    write_order = sorted(entries, key=lambda e: (hoist is None or e[0] != hoist, e[0]))
    # compresslevel: task-bundle inv. 7d — `tarfile` defaults to 9, which is 3.6x the time of 6 for
    # <1% of the bytes. Identity is `code_hash` (the manifest), so the level cannot affect it.
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=GZIP_LEVEL) as tf:
        for rel, exec_bit, data in write_order:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if exec_bit else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))

    return SnapshotResult(code_tar=buf.getvalue(), code_hash=code_hash,
                          file_count=len(entries), total_bytes=sum(len(d) for _, _, d in entries),
                          skipped=skipped)


# --------------------------------------------------------------------------- persist / load

def snapshots_dir(experiments_root: str | Path) -> Path:
    return Path(experiments_root).joinpath(*SNAPSHOTS_SUBDIR)


def persist(experiments_root: str | Path, task_id: str, code_tar: bytes, code_hash: str) -> Path:
    """Write <task_id>.tar.gz + <task_id>.hash atomically (invariant 9). Returns the tar path."""
    d = snapshots_dir(experiments_root)
    d.mkdir(parents=True, exist_ok=True)
    tar_path = d / f"{task_id}.tar.gz"
    hash_path = d / f"{task_id}.hash"
    tmp_tar = d / f".{task_id}.tar.gz.tmp"
    tmp_hash = d / f".{task_id}.hash.tmp"
    tmp_tar.write_bytes(code_tar)
    tmp_hash.write_text(code_hash)
    tmp_tar.replace(tar_path)
    tmp_hash.replace(hash_path)
    return tar_path


def load(experiments_root: str | Path, task_id: str) -> tuple[bytes, str] | None:
    """Return (code_tar, code_hash) if a snapshot exists for task_id, else None (invariant 10)."""
    d = snapshots_dir(experiments_root)
    tar_path = d / f"{task_id}.tar.gz"
    hash_path = d / f"{task_id}.hash"
    if not tar_path.exists():
        return None
    try:
        code_tar = tar_path.read_bytes()
        code_hash = hash_path.read_text().strip() if hash_path.exists() else ""
    except OSError:
        return None
    return code_tar, code_hash


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Snapshot a working tree (committed + uncommitted, .gitignore-aware) to a tar.")
    ap.add_argument("--root", default=".", help="working tree to snapshot (default: cwd)")
    ap.add_argument("--out", default=None, help="write code.tar.gz here (default: don't write)")
    args = ap.parse_args(argv)
    try:
        snap = make_snapshot(args.root)
    except SnapshotError as e:
        print(f"code_snapshot: {e}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_bytes(snap.code_tar)
    print(f"code_hash={snap.code_hash}")
    print(f"files={snap.file_count} bytes={snap.total_bytes} skipped={snap.skipped}"
          + (f" -> {args.out}" if args.out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
