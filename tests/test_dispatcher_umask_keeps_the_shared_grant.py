"""The dispatcher must create group-WRITABLE directories, or the shared-write grant decays.

⛔ THE FAILURE IS SILENT AND ARRIVES LATER, WHICH IS WHY IT NEEDS A TEST RATHER THAN A COMMENT.
`host_setup.sh` §6 gives the queuer (the devcontainer, a different uid) write access to
`experiments/` via the group `coord` plus setgid on the directories. setgid propagates the GROUP; it
does NOT propagate the group WRITE bit, which is the umask's job. Under the default 022 every
directory the dispatcher creates after the one-time `chmod -R g+rwX` comes out `drwxr-sr-x` — right
group, no `g+w` — so the grant covers only what existed at the moment it was granted.

Measured 2026-09-17, one hour after the grant landed:

    experiments/            drwxrwsr-x  coord    <- the one-time chmod
    experiments/hier_pc_r4  drwxrwsr-x  coord    <- predates the grant, so the chmod reached it
    experiments/hier_pc_r5  drwxr-sr-x  coord    <- dispatcher-created, NOT writable by the queuer
    …/rate_s1/results.json  -rw-r--r--  coord

i.e. the queuer could not write its own campaign's `summary.md` into the directory holding that
campaign's results, one directory below a root it CAN write. §6's own comment claims setgid prevents
exactly this; it prevents half of it.

⚠ WHY THE VALUE IS 002 AND NOT 000: group write is the point, world write is not. A data root the
fleet writes to must not become world-writable to fix a group problem.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "fleet" / "dispatcher.py"


def test_the_dispatcher_sets_umask_002_at_import():
    """Pinned by SOURCE, not by importing: `dispatcher.py` pulls in the whole fleet stack, and a
    test that changed the umask of the pytest process would leak into every other test's files."""
    src = DISPATCHER.read_text()
    m = re.search(r"^os\.umask\(0o(\d+)\)", src, re.M)
    assert m, "dispatcher.py must call os.umask() at module scope"
    assert m.group(1) == "002", (
        "expected 002 — 022 strips the group write bit that the shared grant depends on, "
        "and 000 would make the fleet's data root world-writable"
    )


def test_it_runs_before_anything_creates_a_directory():
    """⛔ ORDER IS THE WHOLE POINT. A umask set inside `poll_once`, or after the singleton lock has
    already made `LOCK_PATH.parent`, leaves the first directories of a fresh root at 755."""
    src = DISPATCHER.read_text()
    umask_at = src.index("os.umask(0o002)")
    assert umask_at < src.index("def main("), "the umask must be set before main()"
    # every mkdir in the file must come from a function body, i.e. run only once main() is under way
    for m in re.finditer(r"^(\s*)\S.*\.mkdir\(", src, re.M):
        assert m.group(1), "a module-scope mkdir would run before the umask line is reached"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_umask_002_plus_setgid_actually_yields_a_writable_child(tmp_path):
    """The claim is about the FILESYSTEM, so verify it there rather than trusting the arithmetic:
    under a setgid parent and umask 002, a child directory keeps both the group AND `g+w`. This is
    the assertion that would have failed before the fix and passes after it."""
    parent = tmp_path / "experiments"
    parent.mkdir()
    parent.chmod(0o2775)                          # what host_setup.sh §6 leaves behind
    code = (
        "import os, pathlib, sys;"
        "os.umask(0o002);"
        "p = pathlib.Path(sys.argv[1]) / 'grp' / 'run';"
        "p.mkdir(parents=True);"
        "(p / 'results.json').write_text('{}')"
    )
    subprocess.run([sys.executable, "-c", code, str(parent)], check=True)
    child = parent / "grp" / "run"
    assert os.stat(child).st_mode & 0o070 == 0o070, (
        "the dispatcher-created directory must be group rwx, or the queuer cannot write into it"
    )
    assert os.stat(child / "results.json").st_mode & 0o060 == 0o060, "results must be group-writable"
    assert not os.stat(child).st_mode & 0o002, "must NOT be world-writable"
