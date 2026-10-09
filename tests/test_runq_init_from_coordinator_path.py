"""`runq --init-from` over the API transport must name the checkpoint as the COORDINATOR sees it.

The transport sends a path, not bytes. The devcontainer sees the shared experiments root at
`/workspace/project/experiments`; the coordinator's containers mount the same directory at
`/srv/fleet/experiments`. Recording the queuer's spelling made every `--init-from` fail at ship time
("resume checkpoint unreadable by the dispatcher"), including for a checkpoint the fleet itself had
pulled back — so a run on one box could never start from another box's checkpoint, which is the one
thing `--init-from` exists for.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fleet"))
sys.path.insert(0, str(ROOT / "src"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


runq = _load("runq_init_from_under_test", "fleet/runq.py")


@pytest.fixture
def shared_root(tmp_path, monkeypatch):
    root = tmp_path / "shared" / "experiments"
    (root / "grp" / "task").mkdir(parents=True)
    monkeypatch.setattr(runq.registry_db, "shared_experiments_root", lambda: root)
    monkeypatch.delenv("COORD_EXPERIMENTS_ROOT", raising=False)
    return root


def _resolve(path, *, api, monkeypatch, resume_flag="--init-from"):
    monkeypatch.setattr(runq.api_client, "enabled", lambda: api)
    return runq._resume_ckpt_or_error(argparse.Namespace(init_from=str(path)), resume_flag, "runq add")


def test_a_pulled_checkpoint_is_named_as_the_coordinator_sees_it(shared_root, monkeypatch):
    checkpoint = shared_root / "grp" / "task" / "ckpt_latest.pt"
    checkpoint.write_bytes(b"weights")
    recorded, error = _resolve(checkpoint, api=True, monkeypatch=monkeypatch)
    assert error is None
    assert recorded == "/srv/fleet/experiments/grp/task/ckpt_latest.pt"
    # The mount point is the coordinator's to say; an override is honoured.
    monkeypatch.setenv("COORD_EXPERIMENTS_ROOT", "/data/exp")
    recorded, _ = _resolve(checkpoint, api=True, monkeypatch=monkeypatch)
    assert recorded == "/data/exp/grp/task/ckpt_latest.pt"


def test_a_file_outside_the_shared_root_is_refused_at_queue_time(shared_root, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "worktree" / "experiments" / "ckpt_latest.pt"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"weights")
    recorded, error = _resolve(elsewhere, api=True, monkeypatch=monkeypatch)
    assert (recorded, error) == (None, 2)
    message = capsys.readouterr().err
    assert "outside the shared experiments root" in message and "code snapshot" in message


def test_a_symlinked_spelling_of_the_shared_root_still_resolves(shared_root, tmp_path, monkeypatch):
    checkpoint = shared_root / "grp" / "task" / "ckpt_latest.pt"
    checkpoint.write_bytes(b"weights")
    alias = tmp_path / "alias"
    alias.symlink_to(shared_root)
    recorded, error = _resolve(alias / "grp" / "task" / "ckpt_latest.pt", api=True, monkeypatch=monkeypatch)
    assert error is None and recorded == "/srv/fleet/experiments/grp/task/ckpt_latest.pt"


def test_the_direct_transport_keeps_the_queuers_own_path(shared_root, tmp_path, monkeypatch):
    """With no API in between, the queuer and the dispatcher share a filesystem: nothing to re-root."""
    checkpoint = tmp_path / "anywhere.pt"
    checkpoint.write_bytes(b"weights")
    recorded, error = _resolve(checkpoint, api=False, monkeypatch=monkeypatch)
    assert error is None and recorded == str(checkpoint.resolve())


def test_the_older_refusals_come_first(shared_root, monkeypatch, capsys):
    missing, error = _resolve(shared_root / "grp" / "task" / "nope.pt", api=True, monkeypatch=monkeypatch)
    assert (missing, error) == (None, 2) and "file not found" in capsys.readouterr().err
    checkpoint = shared_root / "grp" / "task" / "ckpt_latest.pt"
    checkpoint.write_bytes(b"weights")
    recorded, error = _resolve(checkpoint, api=True, monkeypatch=monkeypatch, resume_flag=None)
    assert (recorded, error) == (None, 2) and "no resume flag" in capsys.readouterr().err
