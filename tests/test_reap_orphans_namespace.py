"""The orphan reaper must never kill a process it does not own.

⛔ THE INCIDENT (2026-09-12, `laptop-gpu`). That box runs its worker in Docker inside WSL, and a
stale second worker was left running on the WSL host with its own, empty `/root/spool/active/`. A
cwd read out of `/proc` is spelled in the TARGET's mount namespace, so the host worker saw the
CONTAINER's trainers as `/root/spool/active/<id>/repo`, matched that against its own spool by STRING,
asked whether `<its own spool>/active/<id>` existed, correctly answered no — and killed them.

Every task ever queued to that box died 10-30s after launch: five pclm cells and round 1's ten
benchmark attempts, each recorded as `ZERO PROGRESS (likely code/config bug, not infra)`, which sent
every investigation to the trainer instead of the box.

These tests pin the guard AND its fail-closed direction, because a kill list that fails open is the
whole bug.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reap_orphans = _load("reap_orphans", "fleet/reap_orphans.py")


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    (tmp_path / "active").mkdir()
    return tmp_path


def _plant_foreign_process(monkeypatch, spool: Path, pid: int, task_id: str) -> None:
    """A process whose cwd STRING matches ours but which lives in another mount namespace."""
    monkeypatch.setattr(reap_orphans, "_all_pids", lambda: [pid])
    monkeypatch.setattr(reap_orphans, "_proc_cwd",
                        lambda _pid: f"{spool}/active/{task_id}/repo")
    monkeypatch.setattr(reap_orphans, "_mount_namespace",
                        lambda target: ("mnt:[4026531840]" if target == "self"
                                        else "mnt:[4026532225]"))


def test_a_process_in_another_mount_namespace_is_never_reaped(monkeypatch, spool):
    """The incident, in one test: same cwd string, no such dir here, different namespace."""
    _plant_foreign_process(monkeypatch, spool, pid=4242, task_id="f0e91f25")
    assert reap_orphans.find_orphans(spool) == []


def test_the_same_process_IS_reaped_when_it_really_is_ours(monkeypatch, spool):
    """⛔ The guard must not be a blanket 'never kill anything' — that would silently retire the
    runaway-orphan protection this module exists for. Identical setup, one namespace."""
    monkeypatch.setattr(reap_orphans, "_all_pids", lambda: [4242])
    monkeypatch.setattr(reap_orphans, "_proc_cwd",
                        lambda _pid: f"{spool}/active/f0e91f25/repo")
    monkeypatch.setattr(reap_orphans, "_mount_namespace", lambda _target: "mnt:[4026531840]")
    orphans = reap_orphans.find_orphans(spool)
    assert [(o["pid"], o["task_id"]) for o in orphans] == [(4242, "f0e91f25")]
    assert orphans[0]["reason"] == "active dir deleted"


def test_a_live_task_in_our_own_namespace_is_still_never_reaped(monkeypatch, spool):
    (spool / "active" / "f0e91f25").mkdir()
    monkeypatch.setattr(reap_orphans, "_all_pids", lambda: [4242])
    monkeypatch.setattr(reap_orphans, "_proc_cwd",
                        lambda _pid: f"{spool}/active/f0e91f25/repo")
    monkeypatch.setattr(reap_orphans, "_mount_namespace", lambda _target: "mnt:[4026531840]")
    assert reap_orphans.find_orphans(spool) == []


@pytest.mark.parametrize("ours,theirs", [(None, "mnt:[1]"), ("mnt:[1]", None), (None, None)])
def test_an_unreadable_namespace_fails_CLOSED(monkeypatch, spool, ours, theirs):
    """A namespace we cannot read is what a foreign container looks like from here. The cost of not
    killing is a lingering runaway; the cost of killing wrongly is somebody else's run."""
    monkeypatch.setattr(reap_orphans, "_all_pids", lambda: [4242])
    monkeypatch.setattr(reap_orphans, "_proc_cwd",
                        lambda _pid: f"{spool}/active/f0e91f25/repo")
    monkeypatch.setattr(reap_orphans, "_mount_namespace",
                        lambda target: ours if target == "self" else theirs)
    assert reap_orphans.find_orphans(spool) == []


def test_reattachment_also_refuses_a_foreign_process(monkeypatch, spool):
    """`find_live_task_procs` decides RE-ADOPTION after a worker restart. Adopting another
    container's process would have the worker report a task as running that it can never wait on."""
    _plant_foreign_process(monkeypatch, spool, pid=4242, task_id="f0e91f25")
    assert reap_orphans.find_live_task_procs(spool) == []


def test_the_namespace_of_this_very_process_is_readable_and_matches_itself():
    """The guard is only as good as the read underneath it — if `/proc/<pid>/ns/mnt` were
    unreadable on the boxes, the fail-closed branch would disable reaping everywhere and nobody
    would notice until a runaway ate a GPU."""
    if not os.path.exists("/proc/self/ns/mnt"):
        pytest.skip("no /proc/<pid>/ns on this platform")
    ours = reap_orphans._mount_namespace("self")
    assert ours and ours.startswith("mnt:[")
    assert reap_orphans.shares_our_mount_namespace(os.getpid(), ours)
