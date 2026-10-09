"""⛔⛔ AN UNREADABLE `--init-from` MUST FAIL ONE TASK, NOT THE WHOLE DISPATCHER.

Measured 2026-09-20. `_ship_prepare` read the resume checkpoint with a bare
`Path(...).read_bytes()`. A probe was queued with `--init-from` naming a path on the QUEUER's
filesystem, which the dispatcher does not share; the `FileNotFoundError` propagated out of
`_ship` -> `_ship_all` -> `poll_once` -> `main` and exited the process. The container restarted,
re-claimed the same task, read the same missing file, and died again — a crash loop that stopped ALL
dispatch for ~40 minutes while two other sessions' tasks sat undispatched, with the cause visible
only in a traceback nobody was tailing.

⚠ The gap is REAL and cannot be asserted away: `runq add` validates the path on the queuer, which is
a different filesystem from the dispatcher's, so a path that passes at queue time can still be
absent at ship time. Hence: handle it, fail that task terminally, keep polling.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    """⚠ The loader MUST register in `sys.modules` before exec — `dataclasses` resolves a class's
    module by name at decoration time, so a spec-loaded module that skips this dies on import with
    a bare `'NoneType' object has no attribute '__dict__'`. Same helper as `test_dispatcher.py`."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")
_tmod = _load("test_dispatcher_helpers", "tests/test_dispatcher.py")


@pytest.fixture(autouse=True)
def _isolate_experiments_root(tmp_path, monkeypatch):
    """Per-test EXPERIMENTS_ROOT — `_ship_prepare` stages bytes under it, and the real
    `experiments/` is group-owned. Same reason as `test_dispatcher.py`'s own fixture."""
    root = tmp_path / "experiments"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", root)
    monkeypatch.setattr(_tmod.disp, "EXPERIMENTS_ROOT", root)


def _dispatcher(tmp_path):
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_tmod._RecordingRun(),
                           vastai_run=_tmod._RecordingRun())


def _claimed_task_with_resume(d, ckpt_path):
    _tmod._seed_instance_and_task(d.conn, task_id="RESUME1", entrypoint="smoke")
    d.conn.execute("UPDATE tasks SET resume_checkpoint=? WHERE id='RESUME1'", (str(ckpt_path),))
    d.conn.commit()
    return dict(d.conn.execute("SELECT * FROM tasks WHERE id='RESUME1'").fetchone())


def test_a_missing_resume_checkpoint_does_not_raise_out_of_ship_prepare(tmp_path):
    d = _dispatcher(tmp_path)
    task = _claimed_task_with_resume(d, tmp_path / "nowhere" / "absent.pt")
    inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
    plan = d._ship_prepare(task, inst, "host", 22)      # must NOT raise: that is the whole bug
    assert plan is None


def test_that_task_is_failed_TERMINALLY_with_the_path_in_the_reason(tmp_path):
    d = _dispatcher(tmp_path)
    missing = tmp_path / "nowhere" / "absent.pt"
    task = _claimed_task_with_resume(d, missing)
    inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
    d._ship_prepare(task, inst, "host", 22)
    row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='RESUME1'").fetchone())
    assert row["state"] == "task_failed", row["state"]
    ev = d.conn.execute("SELECT detail FROM events WHERE task_id='RESUME1' ORDER BY rowid DESC "
                        "LIMIT 1").fetchone()
    assert str(missing) in (ev["detail"] if ev else ""), ev
    # the operator must be able to act on it without reading the dispatcher's stdout
    assert "queuer" in (ev["detail"] or "").lower()


def test_a_READABLE_resume_checkpoint_still_ships(tmp_path):
    """The guard must not swallow the working path — otherwise every resume silently stops."""
    d = _dispatcher(tmp_path)
    ck = tmp_path / "present.pt"
    ck.write_bytes(b"weights")
    task = _claimed_task_with_resume(d, ck)
    inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
    plan = d._ship_prepare(task, inst, "host", 22)
    assert plan is not None
    assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='RESUME1'").fetchone())["state"] != "task_failed"
