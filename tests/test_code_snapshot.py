"""Code snapshot — ship the working tree, not a git ref (docs/specs/code-snapshot.spec.md).

Builds throwaway git repos in tmp_path and asserts the include/exclude set, content-addressing,
worktree behavior, and persist/load round-trip.
"""

import importlib.util
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load("code_snapshot", "fleet/code_snapshot.py")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def _members(code_tar: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(code_tar), mode="r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}


def _basic_repo(repo: Path):
    _init_repo(repo)
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "committed.py").write_text("print('a')\n")
    (repo / "modified.py").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "modified.py").write_text("EDITED on disk\n")   # uncommitted change
    (repo / "untracked.py").write_text("new file\n")        # untracked, not ignored
    (repo / "ignored.log").write_text("junk\n")             # gitignored


# --------------------------------------------------------------------------- include / exclude

def test_include_committed_uncommitted_exclude_ignored(tmp_path):
    repo = tmp_path / "r"
    _basic_repo(repo)
    snap = cs.make_snapshot(repo)
    members = _members(snap.code_tar)
    assert set(members) == {".gitignore", "committed.py", "modified.py", "untracked.py"}
    assert snap.file_count == 4
    assert members["modified.py"] == b"EDITED on disk\n"    # invariant 2: live bytes, not committed
    assert members["untracked.py"] == b"new file\n"
    assert "ignored.log" not in members                     # invariant 1: gitignored excluded


def test_no_force_include_an_ignored_config_is_a_hard_error(tmp_path):
    """v1 FORCE-INCLUDED an ignored repo-root `job.json`. That is the mechanism the mutable-singleton
    bug rode in on: an untracked file, invisible to review and to `git status`, silently supplied the
    run contract for every queued cell. v2 force-includes NOTHING — if the config you named is not in
    the snapshot, the run would ship without its own contract, so that is an error, not a rescue."""
    repo = tmp_path / "r"
    _basic_repo(repo)
    (repo / ".gitignore").write_text("*.log\nsecret.json\n")
    (repo / "secret.json").write_text('{"job": {"manifest_version": 1}}\n')
    with pytest.raises(cs.SnapshotError, match="secret.json"):
        cs.make_snapshot(repo, hoist="secret.json")
    # ...and it is genuinely absent rather than quietly added
    assert "secret.json" not in _members(cs.make_snapshot(repo).code_tar)


def test_hoist_orders_the_named_config_first_without_changing_identity(tmp_path):
    """inv. 10a is now parameterised: the caller's config goes to member 0 so it is readable from
    the gzip stream in O(config bytes). Write ORDER must not touch code_hash (inv. 4 hashes the
    sorted file manifest), or re-queueing the same tree under a different config would look like
    different code."""
    repo = tmp_path / "r"
    _basic_repo(repo)
    (repo / "cfg.json").write_text('{"job": {"manifest_version": 1}}\n')
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "cfg")

    hoisted = cs.make_snapshot(repo, hoist="cfg.json")
    plain = cs.make_snapshot(repo)
    with tarfile.open(fileobj=io.BytesIO(hoisted.code_tar), mode="r:gz") as tf:
        assert tf.next().name == "cfg.json"
    assert hoisted.code_hash == plain.code_hash              # order-independent identity
    assert set(_members(hoisted.code_tar)) == set(_members(plain.code_tar))

    (repo / "cfg.json").write_text('{"job": {"manifest_version": 2}}\n')
    assert cs.make_snapshot(repo, hoist="cfg.json").code_hash != hoisted.code_hash  # content counts


# --------------------------------------------------------------------------- content addressing

def test_determinism_and_sensitivity(tmp_path):
    repo = tmp_path / "r"
    _basic_repo(repo)
    h1 = cs.make_snapshot(repo).code_hash
    h2 = cs.make_snapshot(repo).code_hash
    assert h1 == h2                                         # invariant 4: deterministic

    (repo / "committed.py").write_text("print('b')\n")     # content change
    assert cs.make_snapshot(repo).code_hash != h1


def test_exec_bit_changes_hash(tmp_path):
    repo = tmp_path / "r"
    _basic_repo(repo)
    h1 = cs.make_snapshot(repo).code_hash
    os.chmod(repo / "committed.py", 0o755)
    assert cs.make_snapshot(repo).code_hash != h1          # invariant 4: exec bit is part of identity


# --------------------------------------------------------------------------- skipped tally

def test_symlink_skipped_not_shipped(tmp_path):
    repo = tmp_path / "r"
    _basic_repo(repo)
    os.symlink("does_not_exist", repo / "dangling")        # untracked, non-ignored symlink
    snap = cs.make_snapshot(repo)
    assert snap.skipped >= 1                                # invariant 3: counted
    assert "dangling" not in _members(snap.code_tar)        # not shipped


# --------------------------------------------------------------------------- persist / load

def test_persist_load_roundtrip(tmp_path):
    repo = tmp_path / "r"
    _basic_repo(repo)
    snap = cs.make_snapshot(repo)
    exp_root = tmp_path / "experiments"
    cs.persist(exp_root, "task-123", snap.code_tar, snap.code_hash)
    got = cs.load(exp_root, "task-123")
    assert got is not None
    assert got[0] == snap.code_tar and got[1] == snap.code_hash
    assert cs.load(exp_root, "unknown-task") is None       # invariant 10: absence-safe


# --------------------------------------------------------------------------- worktree proof

def test_snapshot_captures_worktree_uncommitted(tmp_path):
    """Invariant 12: a snapshot of a git worktree captures THAT worktree's uncommitted files."""
    repo = tmp_path / "r"
    _basic_repo(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "more")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    (wt / "only_in_worktree.py").write_text("wt-only\n")   # uncommitted, exists only in the worktree
    members = _members(cs.make_snapshot(wt).code_tar)
    assert "only_in_worktree.py" in members
    assert members["only_in_worktree.py"] == b"wt-only\n"


# --------------------------------------------------------------------------- CLI + errors

def test_cli_writes_tar(tmp_path, capsys):
    repo = tmp_path / "r"
    _basic_repo(repo)
    out = tmp_path / "code.tar.gz"
    rc = cs.main(["--root", str(repo), "--out", str(out)])
    assert rc == 0 and out.exists()
    printed = capsys.readouterr().out
    assert "code_hash=" in printed and "files=4" in printed


def test_make_snapshot_non_git_errors(tmp_path):
    with pytest.raises(cs.SnapshotError):
        cs.make_snapshot(tmp_path / "not_a_repo")
