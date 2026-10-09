"""`runq add` must REFUSE, up front and legibly, when it cannot write the data root.

⛔ THE BUG THIS REPLACES TOLD THE TRUTH ABOUT THE WRONG PROBLEM. Measured 2026-09-17, the first
queue attempt from the devcontainer after the tower cutover: `.devcontainer/.dataroot` binds
the coordinator's live root (uid 1500, mode 755) into a container running as uid 1000, so nothing
under it is writable. `runq add` did not notice. It built for ~30 s, then failed inside the Cython
build venv create with `Permission denied` wrapped in "build toolchain unavailable" and the hint
"`uv python install 3.12` usually fixes it" — advice that cannot work, pointed at a toolchain that
was never broken. The repo already handles this exact ownership mismatch one layer up (the
Dockerfile's `safe.directory` for `/srv/coord/repo.git`); nobody made the data root writable.

⚠ WARNING WAS NOT ENOUGH HERE, unlike `_warn_if_no_coordinator`. A registry nothing polls is
sometimes legitimate (a new root, an isolated test root, a coordinator briefly down). An unwritable
root has no legitimate case: the registry row, the code snapshot, the ship blob and the build venv
all live under it, so every path forward fails. Refuse.

⚠ WHAT THE ASSERTIONS PIN, and why each one is load-bearing rather than cosmetic: the message must
name the OWNER and the CALLER, or the reader cannot tell a uid mismatch from a full disk; it must
say nothing was queued, or the reader re-runs and fears duplicates; and it must NOT recommend a
Python install, which is the specific wrong turn that cost the original incident.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(ROOT / "fleet"))
runq = _load("runq", "fleet/runq.py")


def test_a_writable_root_is_not_refused(tmp_path):
    """The check must be INERT in the normal case — every existing queue path runs through it."""
    assert runq._refuse_if_dataroot_unwritable(str(tmp_path / "runs.sqlite")) is None


def test_a_writable_root_with_an_existing_db_is_not_refused(tmp_path):
    db = tmp_path / "runs.sqlite"
    db.write_bytes(b"")
    assert runq._refuse_if_dataroot_unwritable(str(db)) is None


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the mode bits this test sets")
def test_an_unwritable_root_is_refused_with_the_real_cause(tmp_path, capsys):
    root = tmp_path / "experiments"
    root.mkdir()
    root.chmod(0o555)
    try:
        rc = runq._refuse_if_dataroot_unwritable(str(root / "runs.sqlite"))
        err = capsys.readouterr().err
    finally:
        root.chmod(0o755)          # or tmp_path cleanup fails on some platforms
    assert rc == 2, "an unwritable root must REFUSE, not warn — every path forward fails"
    assert "NOT WRITABLE" in err
    assert "nothing was queued" in err.lower(), "the reader must not fear a duplicate row"
    assert "uid=%d" % os.getuid() in err, "must name the CALLER"
    assert "uid=%d" % os.stat(root).st_uid in err, "must name the OWNER of the root"
    # ⚠ the group must be the one the ROOT carries, not a hardcoded `coord`: a host provisioned with
    # a different `--user` must still be told the right group to add itself to.
    import grp as _grp
    assert _grp.getgrgid(os.stat(root).st_gid).gr_name in err, "must name the root's ACTUAL group"
    # ⛔ THE FIX MUST BE THE ONE THE REPO PROVISIONS. The first version of this guard printed an
    # invented `setfacl` line; `host_setup.sh` §6 then provisioned a shared group plus setgid and
    # said why an ACL is the wrong tool (invisible in `ls -l`, lost on `cp`, decays on a rewrite).
    # An error message recommending a rejected mechanism is worse than one naming where the answer
    # lives — so pin the pointer, and pin that the ACL recipe has not come back.
    assert "host_setup.sh" in err, "must point at the script that provisions the grant"
    assert "setgid" in err, "the load-bearing half must be named"
    assert "setfacl" not in err, "the repo rejected ACLs here; do not recommend one"
    # ⛔ the specific wrong turn the original message caused. Naming a Python install here sends the
    # next agent to reinstall a toolchain that was never broken.
    assert "uv python install" not in err, "a Python install cannot fix an ownership mismatch"
    # ⚠ the word "toolchain" is ALLOWED and wanted — but only as the correction ("not a toolchain
    # ... problem"), never as the diagnosis. Pin the correction rather than banning the word, which
    # is what a reader coming from the old message needs to see.
    assert "not a toolchain" in err


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the mode bits this test sets")
def test_a_writable_root_holding_an_unwritable_db_is_refused(tmp_path, capsys):
    """⚠ THE DIRECTORY IS NOT THE WHOLE CHECK. The live root reproduced exactly this shape — a
    644 root-owned `runs.sqlite` — and a dir-only check would have passed it and then failed on the
    INSERT, which is the late, misattributed failure this whole guard exists to remove."""
    db = tmp_path / "runs.sqlite"
    db.write_bytes(b"")
    db.chmod(0o444)
    try:
        rc = runq._refuse_if_dataroot_unwritable(str(db))
    finally:
        db.chmod(0o644)
    assert rc == 2
    assert "NOT WRITABLE" in capsys.readouterr().err


def test_the_preflight_runs_before_any_build(tmp_path):
    """⛔ ORDER IS THE POINT. The original failure was ~30 s of Cython build BEFORE the permission
    error surfaced, wearing a build-toolchain label. Pin that `cmd_add` consults the preflight ahead
    of both dispatch paths, by source order — the cheapest check that cannot drift."""
    src = (ROOT / "fleet" / "runq.py").read_text()
    body = src.split("def cmd_add(", 1)[1].split("\ndef ", 1)[0]
    assert "_refuse_if_dataroot_unwritable" in body, "cmd_add must run the preflight"
    guard = body.index("_refuse_if_dataroot_unwritable")
    for later in ("_add_config", "_add_named"):
        assert guard < body.index(later), f"the preflight must precede {later}"
