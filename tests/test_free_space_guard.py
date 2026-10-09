"""Free-space guard — docs/specs/free-space-guard.spec.md, at least one test per fixture row.

THE INCIDENT THIS PINS. A site's data root reached zero free bytes while the fleet was busy, and
within one poll cycle the dispatcher:

    task_failed   x2    "artifact_missing (results.json absent after 2 pulls)"   <- finished runs
    owned_unreachable x2 "3 consecutive ssh/rsync failures (>= 3) -> quarantine" <- healthy boxes
    infra_failed + requeue x31                                                    <- their tasks
    (then nothing at all for about an hour: `database or disk is full`, restart, repeat)

None of those four things was true of a task or a box. Every one was the coordinator's own disk.

⚠ Each test plants the condition that makes the guard TRIP and runs the matching control, because a
guard that is never seen to trip pins nothing: with ample space (or the old code) the control must
show the destructive outcome, and with the planted reading it must not.
"""
from __future__ import annotations

import errno
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

# The suite's own dispatcher module, fakes and data-root isolation (tests/ is on `pythonpath`).
from test_dispatcher import (  # noqa: F401  (`_isolate_experiments_root` is an autouse fixture)
    _FakeProc, _RecordingRun, _isolate_experiments_root, _seed_instance_and_task, disp)

GIB = 1024 ** 3
# Captured at IMPORT, before conftest's autouse fixture replaces the default reading for the suite.
_REAL_FREE_BYTES = disp.data_root_free_bytes
_RECEIVER_FULL = ('rsync: [receiver] write failed on "/data/g/T/ckpt_latest.pt": '
                  "No space left on device (28)\n"
                  "rsync error: error in file IO (code 11) at receiver.c(380) [receiver=3.2.7]")
PHASES = {"reconcile", "ssh_config", "heartbeats", "provision", "ingest", "cancels", "drain",
          "over_capacity", "capacity_push", "probe", "box_requests", "measure", "book_cost",
          "place", "consolidate", "ship", "teardown", "gc_staging", "gc_temps", "gc_snapshots",
          "worker_refresh"}


class _Pushes:
    """A `Dispatcher.notify` that records every push and fails the first `fail_first` of them."""

    def __init__(self, fail_first=0):
        self.sent, self.fail_first = [], fail_first

    def __call__(self, title, message, tags, priority):
        self.sent.append(title)
        if len(self.sent) <= self.fail_first:
            return False, "channel down"
        return True, ""


def _guarded(tmp_path, free_gb=100.0, run=None, notify=None, name="runs.sqlite"):
    """A dispatcher whose reading of the data root is PLANTED. `reading["gb"]` may be changed
    between cycles: a number of GiB, None (the read failed), or an exception for the seam to raise."""
    reading = {"gb": free_gb}

    def free_bytes(_path):
        v = reading["gb"]
        if isinstance(v, BaseException):
            raise v
        return None if v is None else int(v * GIB)

    run = run or _RecordingRun()
    d = disp.Dispatcher(str(tmp_path / name), run=run, vastai_run=run, free_bytes=free_bytes,
                        notify=notify or _Pushes())
    return d, reading


def _events(d, *names):
    q = "SELECT event, detail, task_id, instance_id FROM events"
    rows = [dict(r) for r in d.conn.execute(q + " ORDER BY seq")]
    return [r for r in rows if not names or r["event"] in names]


def _state(d, tid):
    return d.conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"]


def _set(d, key, value):
    d.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))
    d.conn.commit()


def _iso_ago(minutes):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))


def _running_task(d, tid="T", minutes_ago=0, state="running"):
    """One live box (id 1) with one task in `state`, last stamped `minutes_ago`."""
    reg = _seed_instance_and_task(d.conn, task_id=tid)
    d.conn.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                   (state, _iso_ago(minutes_ago), tid))
    d.conn.commit()
    return reg


def _busy_owned_box(d, n=3):
    """Box 1, owned, with `n` running tasks — the shape that was quarantined in the incident: the
    marker listing is an ssh and succeeds, then each running task's pull fails in turn."""
    reg = _running_task(d, "B0")
    for i in range(1, n):
        reg.insert_task(
            d.conn, id=f"B{i}", created_at=reg.now_iso(), created_by="test", grp="g", name=f"B{i}",
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=f"B{i}",
            arm_hash=f"B{i}", git_sha="deadbeef", slots=1, est_minutes=1, priority=50,
            max_retries=1, state="running", instance_id=1)
    d.conn.execute("UPDATE instances SET source='owned' WHERE id=1")
    d.conn.commit()
    return [f"B{i}" for i in range(n)]


def _inst(d, iid=1):
    return dict(d.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())


def _marker_run(tid, marker):
    """A transport whose marker listing reports `marker` for `tid` and whose every call succeeds."""
    return lambda cmd, **kw: _FakeProc(0, f"/root/spool/active/{tid}/{marker}\n")


def _stub_cycle(d, monkeypatch, keep=()):
    """Reduce `poll_once` to the phases a test is about; everything else becomes a no-op, the way
    the suite's own whole-cycle test does it."""
    for phase in ("do_reconcile", "_refresh_heartbeats", "_advance_provisioning",
                  "_ingest_and_complete", "_signal_cancels", "_signal_drain",
                  "_reap_over_capacity", "_measure_box_resources", "_place_queue",
                  "_consolidate", "_ship_all", "_teardown_idle"):
        if phase not in keep:
            monkeypatch.setattr(d, phase, lambda *a, **k: None)


def _cycle_event(d):
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 1").fetchone()
    return json.loads(row[0])


# --------------------------------------------------------------------------------------------------
# Invariant 1 — the reading
# --------------------------------------------------------------------------------------------------

def test_reading_is_what_THIS_user_can_write_and_zero_when_no_inode_is_left(tmp_path, monkeypatch):
    assert _REAL_FREE_BYTES(tmp_path) > 0, "the real call must work on a real directory"

    def vfs(bavail, files, favail, bfree=10 ** 9):
        return lambda _p: SimpleNamespace(f_bavail=bavail, f_bfree=bfree, f_frsize=4096,
                                          f_files=files, f_favail=favail)

    monkeypatch.setattr(disp.os, "statvfs", vfs(bavail=10, files=1000, favail=5))
    assert _REAL_FREE_BYTES("/x") == 10 * 4096, "must be f_bavail x f_frsize, not the root-only f_bfree"
    monkeypatch.setattr(disp.os, "statvfs", vfs(bavail=10, files=1000, favail=0))
    assert _REAL_FREE_BYTES("/x") == 0, "a filesystem with no free inode can create nothing"
    monkeypatch.setattr(disp.os, "statvfs", vfs(bavail=10, files=0, favail=0))
    assert _REAL_FREE_BYTES("/x") == 10 * 4096, "f_files == 0 means NO inode limit, not no inodes"

    def unreadable(_p):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(disp.os, "statvfs", unreadable)
    assert _REAL_FREE_BYTES("/x") is None, "a failed read is UNKNOWN, never a number"


def test_an_unknown_reading_never_begins_a_hold_and_never_ends_one(tmp_path):
    d, reading = _guarded(tmp_path, free_gb=None)
    d._check_data_root()
    assert not d._space_hold, "an unreadable volume was treated as a full one"
    reading["gb"] = RuntimeError("the probe itself blew up")
    d._check_data_root()
    assert not d._space_hold and d._space_free_gb is None

    reading["gb"] = 1.0                     # control: the SAME dispatcher does hold on a low reading
    d._check_data_root()
    assert d._space_hold
    for unknown in (None, OSError("gone")):
        reading["gb"] = unknown
        d._check_data_root()
        assert d._space_hold, "an unreadable volume released a hold nobody confirmed was over"
    reading["gb"] = 50.0
    d._check_data_root()
    assert not d._space_hold


# --------------------------------------------------------------------------------------------------
# Invariant 2 — two marks with a gap
# --------------------------------------------------------------------------------------------------

def test_hold_below_the_first_mark_and_resume_only_at_the_second(tmp_path):
    d, reading = _guarded(tmp_path, free_gb=7.0)
    seen = []
    for gb in (7.0, 5.0, 4.99, 7.0, 9.99, 10.0, 7.0, 5.0, 4.0):
        reading["gb"] = gb
        d._check_data_root()
        seen.append(d._space_hold)
    #            7.0    5.0    4.99  7.0   9.99  10.0   7.0    5.0    4.0
    assert seen == [False, False, True, True, True, False, False, False, True], seen


def test_a_hold_mark_of_zero_turns_the_guard_off_and_releases_a_hold(tmp_path):
    d, reading = _guarded(tmp_path, free_gb=1.0)
    d._check_data_root()
    assert d._space_hold
    _set(d, "data_root_hold_free_gb", 0)    # read from the registry every cycle: no restart
    d._check_data_root()
    assert not d._space_hold, "mark 0 did not release the hold in force"
    reading["gb"] = 0.0
    d._check_data_root()
    assert not d._space_hold, "mark 0 did not switch the reading-driven hold off"


def test_a_resume_mark_below_the_hold_mark_is_read_as_the_hold_mark(tmp_path):
    d, reading = _guarded(tmp_path, free_gb=1.0)
    _set(d, "data_root_resume_free_gb", 2)  # nonsense: below the hold mark of 5
    d._check_data_root()
    assert d._space_hold
    reading["gb"] = 3.0                     # above the written resume mark, still below the hold mark
    d._check_data_root()
    assert d._space_hold, "resumed while still below the mark that begins a hold"
    reading["gb"] = 5.0
    d._check_data_root()
    assert not d._space_hold


# --------------------------------------------------------------------------------------------------
# Invariant 3 — one event and one alert per edge
# --------------------------------------------------------------------------------------------------

def test_one_event_and_one_push_per_edge_and_a_failed_push_is_retried(tmp_path, capsys):
    pushes = _Pushes(fail_first=2)
    d, reading = _guarded(tmp_path, free_gb=100.0, notify=pushes)
    for _ in range(3):
        d._check_data_root()
    assert not _events(d, "data_root_low", "data_root_ok", "notify") and not pushes.sent, (
        "a healthy volume produced guard events")                       # the control

    reading["gb"] = 2.0
    for _ in range(6):
        d._check_data_root()
    low = _events(d, "data_root_low")
    assert len(low) == 1, f"{len(low)} `data_root_low` events for ONE edge"
    detail = json.loads(low[0]["detail"])
    assert detail == {"free_gb": 2.0, "hold_free_gb": 5.0, "resume_free_gb": 10.0, "why": "free"}
    assert len(pushes.sent) == 3, "the push must be retried until delivered, then never again"
    assert [json.loads(e["detail"])["kind"] for e in _events(d, "notify")] == ["data_root_low"]
    assert capsys.readouterr().err.count("[ALERT]") == 1

    reading["gb"] = 40.0
    for _ in range(4):
        d._check_data_root()
    ok = _events(d, "data_root_ok")
    assert len(ok) == 1 and len(_events(d, "data_root_low")) == 1
    assert json.loads(ok[0]["detail"])["free_gb"] == 40.0
    assert "held_min" in json.loads(ok[0]["detail"])
    assert len(pushes.sent) == 4
    assert [json.loads(e["detail"])["kind"] for e in _events(d, "notify")] == [
        "data_root_low", "data_root_ok"]


def test_a_restart_during_a_hold_repeats_nothing_and_forgets_nothing(tmp_path):
    d1, _ = _guarded(tmp_path, free_gb=1.0)
    d1._check_data_root()
    assert d1._space_hold

    pushes = _Pushes()
    d2, reading = _guarded(tmp_path, free_gb=1.0, notify=pushes)      # the same registry, restarted
    assert d2._space_hold, "the restart forgot a hold that was in force"
    d2._check_data_root()
    assert len(_events(d2, "data_root_low")) == 1 and not pushes.sent, "the restart repeated the edge"
    reading["gb"] = 30.0
    d2._check_data_root()
    assert not d2._space_hold and len(_events(d2, "data_root_ok")) == 1

    d3, _ = _guarded(tmp_path, free_gb=30.0)                          # restarted again, after release
    assert not d3._space_hold
    assert d3._space_resumed_at is not None and abs(d3._space_resumed_at - time.time()) < 60, (
        "the moment the hold ended must survive a restart: the reapers' clocks restart from it")


# --------------------------------------------------------------------------------------------------
# Invariant 4 — what a hold stops, and what keeps running
# --------------------------------------------------------------------------------------------------

def _world_for_a_cycle(tmp_path, monkeypatch, free_gb):
    run = _RecordingRun()
    d, reading = _guarded(tmp_path, free_gb=free_gb, run=run)
    _running_task(d, "RUN")                                   # payload pulls are for running tasks
    reg = _seed_instance_and_task(d.conn, task_id="SHP", instance_id=2)
    reg.transition(d.conn, "SHP", "shipped", "ship", "shipped")
    d.conn.commit()
    # the box says it STARTED `SHP` — small state the loop must keep reading in a hold
    scratch = disp.EXPERIMENTS_ROOT / ".dispatcher" / "instance_2"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "worker.jsonl").write_text(
        json.dumps({"event": "start", "task_id": "SHP", "t": "2999-01-01T00:00:00Z"}) + "\n")
    called = []
    _stub_cycle(d, monkeypatch, keep=("_ingest_and_complete",))
    for phase in ("_place_queue", "_consolidate", "_ship_all", "_teardown_idle"):
        monkeypatch.setattr(d, phase, lambda p=phase: called.append(p))
    return d, run, called


def test_a_hold_stops_placement_shipping_and_payload_pulls_but_not_small_state(tmp_path, monkeypatch):
    d, run, called = _world_for_a_cycle(tmp_path, monkeypatch, free_gb=1.0)
    d.poll_once()
    cmds = run.joined()
    assert called == ["_teardown_idle"], f"a HOLD still ran {called}"
    assert not any("tb/**" in c or "ckpt_latest.pt" in c for c in cmds), (
        "a payload pull (TensorBoard or checkpoint) was issued while holding")
    assert any("rsync" in c and "HEARTBEAT" in c for c in cmds), "HEARTBEAT is small state"
    assert any("rsync" in c and "worker.jsonl" in c for c in cmds), "worker.jsonl is small state"
    assert any("DONE" in c and "CANCELLED" in c for c in cmds), "the marker listing must still run"
    assert _state(d, "SHP") == "running", "a start the box reported was not observed during a hold"
    ev = _cycle_event(d)
    assert set(ev["phases"]) == PHASES - {"place", "consolidate", "ship"}
    assert ev["data_root"] == {"free_gb": 1.0, "holding": True, "deferred": 0}


def test_with_ample_space_the_same_cycle_places_ships_and_pulls(tmp_path, monkeypatch):
    """The control for the test above: the SAME world, a healthy reading."""
    d, run, called = _world_for_a_cycle(tmp_path, monkeypatch, free_gb=100.0)
    d.poll_once()
    cmds = run.joined()
    assert called == ["_place_queue", "_consolidate", "_ship_all", "_teardown_idle"]
    assert any("tb/**" in c for c in cmds) and any("ckpt_latest.pt" in c for c in cmds)
    assert set(_cycle_event(d)["phases"]) == PHASES


def test_a_cancel_still_completes_during_a_hold(tmp_path, monkeypatch):
    d, _ = _guarded(tmp_path, free_gb=1.0)
    reg = _running_task(d, "CX")
    reg.transition(d.conn, "CX", "cancelling", "cancel_requested", "operator")
    d._check_data_root()
    assert d._space_hold
    d.run = _marker_run("CX", "CANCELLED")
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "CX") == "cancelled", "an operator's stop waited for disk"


# --------------------------------------------------------------------------------------------------
# Invariant 5 — nothing true only because of the hold is recorded as a failure
# --------------------------------------------------------------------------------------------------

def test_a_done_marker_is_deferred_during_a_hold_and_completes_after(tmp_path, monkeypatch):
    d, reading = _guarded(tmp_path, free_gb=1.0)
    _running_task(d, "DN")
    out = disp.EXPERIMENTS_ROOT / "g" / "DN"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    pulls = []
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: pulls.append(a) or False)  # nothing lands
    d.run = _marker_run("DN", "DONE")

    d._check_data_root()
    d._space_deferred = 0
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "DN") == "running", "a finished task was acted on while nothing could be pulled"
    assert not pulls, "a result pull was attempted while holding"
    assert not _events(d, "task_failed") and d._space_deferred == 1

    reading["gb"] = 50.0                                   # space is back
    d._check_data_root()

    def lands(*a, **k):
        (out / "summary.json").write_text("{}")            # the `smoke` entrypoint's artifact
        return True

    monkeypatch.setattr(disp, "rsync_pull", lands)
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "DN") == "done", "the SAME marker must complete the task once pulls resume"


def test_without_a_hold_the_same_failed_pulls_are_artifact_missing(tmp_path, monkeypatch):
    """The control: with space, a DONE whose artifact never arrives IS `artifact_missing`. If this
    stopped being true, the test above would pass for the wrong reason."""
    d, _ = _guarded(tmp_path, free_gb=100.0)
    _running_task(d, "DM")
    out = disp.EXPERIMENTS_ROOT / "g" / "DM"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: False)
    d.run = _marker_run("DM", "DONE")
    d._check_data_root()
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "DM") == "task_failed"
    assert "artifact_missing" in _events(d, "task_failed")[0]["detail"]


_PULLED_COPY_REAPERS = ["_reap_stalled", "_reap_dead_workers", "_reap_overpacked_boxes",
                        "_reap_unclaimed_ships", "_reap_undeliverable_claims"]
_OTHER_REAPERS = ["_reap_orphaned_tasks", "_reap_unreachable_owned", "_reap_paused_soft_timeout"]


@pytest.mark.parametrize("free_gb, expected", [
    (1.0, _OTHER_REAPERS),                                   # holding
    (100.0, _PULLED_COPY_REAPERS + _OTHER_REAPERS),          # the control
])
def test_the_reapers_that_judge_by_pulled_copies_stand_down_while_holding(
        tmp_path, monkeypatch, free_gb, expected):
    d, _ = _guarded(tmp_path, free_gb=free_gb)
    _running_task(d, "RP")
    called = []
    for name in _PULLED_COPY_REAPERS + _OTHER_REAPERS:
        monkeypatch.setattr(d, name, lambda n=name: called.append(n))
    d._check_data_root()
    d._ingest_and_complete()
    assert sorted(called) == sorted(expected)


def _silent_task_world(tmp_path, free_gb):
    """A task `running` for three hours that never reported anything: stalled, by invariant 19."""
    d, reading = _guarded(tmp_path, free_gb=free_gb)
    _running_task(d, "ST", minutes_ago=180)
    assert d.settings["stall_timeout_min"] < 180
    return d, reading


def test_a_silent_task_IS_reaped_when_there_is_no_hold(tmp_path):
    """The control for the next test."""
    d, _ = _silent_task_world(tmp_path, free_gb=100.0)
    d._check_data_root()
    d._ingest_and_complete()
    assert _events(d, "stalled"), "the stall reaper no longer reaps a genuinely silent task"


def test_nothing_is_reaped_as_stalled_during_a_hold_or_in_the_first_cycle_after(tmp_path):
    d, reading = _silent_task_world(tmp_path, free_gb=1.0)
    for _ in range(3):
        d._check_data_root()
        d._ingest_and_complete()
    assert not _events(d, "stalled") and _state(d, "ST") == "running", (
        "a task was reaped for a silence that was OUR pulls being stopped")

    reading["gb"] = 50.0
    d._check_data_root()
    assert not d._space_hold
    d._ingest_and_complete()                               # the first cycle after the hold
    assert not _events(d, "stalled") and _state(d, "ST") == "running", (
        "reaped in the first cycle after the hold on age accumulated during it")

    # ...and the clock really does restart there, rather than being switched off for good:
    d._space_resumed_at -= (d.settings["stall_timeout_min"] + 1) * 60
    d._ingest_and_complete()
    assert _events(d, "stalled"), "after a full timeout of real silence the task must be reaped"


def _undeliverable_world(tmp_path, free_gb):
    """A task `claimed` for three hours on a live box with a recorded ship failure (inv. 10d)."""
    d, reading = _guarded(tmp_path, free_gb=free_gb)
    _running_task(d, "UD", minutes_ago=180, state="claimed")
    d.log("ship_failed", "rsync push failed", task_id="UD", instance_id=1)
    assert d.settings["ship_timeout_min"] < 180
    return d, reading


def test_an_undeliverable_claim_IS_requeued_when_there_is_no_hold(tmp_path):
    """The control for the next test."""
    d, _ = _undeliverable_world(tmp_path, free_gb=100.0)
    d._check_data_root()
    d._ingest_and_complete()
    assert _events(d, "undeliverable") and _state(d, "UD") == "queued"


def test_the_undeliverable_clock_restarts_when_a_hold_ends(tmp_path):
    d, reading = _undeliverable_world(tmp_path, free_gb=1.0)
    d._check_data_root()
    d._ingest_and_complete()
    reading["gb"] = 50.0
    d._check_data_root()
    d._ingest_and_complete()                               # the first cycle after the hold
    assert not _events(d, "undeliverable") and _state(d, "UD") == "claimed", (
        "time spent claimed while ships were STOPPED was counted as failing to deliver")
    d._space_resumed_at -= (d.settings["ship_timeout_min"] + 1) * 60
    d._ingest_and_complete()
    assert _state(d, "UD") == "queued"


# --------------------------------------------------------------------------------------------------
# Invariant 6 — a pull refused by OUR disk is not the box's failure
# --------------------------------------------------------------------------------------------------

class _FailingPulls(_RecordingRun):
    """Every rsync fails with `stderr`; every ssh succeeds and lists no markers."""

    def __init__(self, stderr, rc=11):
        super().__init__()
        self._stderr, self._rc = stderr, rc

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd and cmd[0] == "rsync":
            p = _FakeProc(self._rc, "")
            p.stderr = self._stderr
            return p
        return _FakeProc(0, "")


def test_rsync_pull_names_the_receivers_full_disk_and_only_that():
    def pull(stderr, rc=11):
        return disp.rsync_pull("h", 22, "~/x/", "/tmp/y/", ["*"], run=_FailingPulls(stderr, rc))

    before = disp._local_full_pulls()
    assert pull(_RECEIVER_FULL) is disp.LOCAL_DISK_FULL
    assert pull('rsync: write failed on "/d/x": No space left on device (28)') is disp.LOCAL_DISK_FULL
    assert disp._local_full_pulls() == before + 2
    assert not disp.LOCAL_DISK_FULL, "it must stay FALSY: every `if not ok` relies on that"
    # the same words from the SENDER are the box talking about its own disk: an ordinary failure
    assert pull("rsync: [sender] write failed: No space left on device (28)") is False
    assert pull("rsync: connection unexpectedly closed", rc=12) is False
    assert pull("", rc=0) is True, "stderr is not consulted on success"
    assert disp._local_full_pulls() == before + 2


def test_a_pull_refused_by_our_disk_is_not_a_box_failure(tmp_path):
    d, _ = _guarded(tmp_path, free_gb=100.0, run=_FailingPulls(_RECEIVER_FULL))   # NOT holding
    tids = _busy_owned_box(d)
    for _ in range(d.settings["owned_unreachable_fails"] + 2):
        d._check_data_root()
        d._ingest_and_complete()
    assert d.tracker.consecutive_fails(1) == 0, "our own full disk was counted against the box"
    assert _inst(d)["state"] == "live" and not _events(d, "owned_unreachable", "infra_failed")
    assert [_state(d, t) for t in tids] == ["running"] * len(tids)
    assert len(_events(d, "local_pull_no_space")) == 1, "one event per RUN of such cycles"


def test_pulls_that_fail_any_other_way_still_quarantine_the_box(tmp_path):
    """The control: the SAME world with a transport failure must end in the quarantine the
    incident produced — otherwise the test above proves only that nothing is counted at all."""
    d, _ = _guarded(tmp_path, free_gb=100.0,
                    run=_FailingPulls("rsync: connection unexpectedly closed", rc=12))
    tids = _busy_owned_box(d)
    for _ in range(d.settings["owned_unreachable_fails"] + 2):
        d._check_data_root()
        d._ingest_and_complete()
    assert _events(d, "owned_unreachable"), "the control must reproduce the incident's quarantine"
    assert len(_events(d, "infra_failed")) == len(tids), "and its requeue of every task on the box"
    assert not _events(d, "local_pull_no_space")


@pytest.mark.parametrize("marker, state", [("DONE", "running"), ("FAILED_1", "running"),
                                           ("PREEMPTED", "preempting")])
def test_a_completion_whose_pull_our_disk_refused_is_deferred_not_failed(
        tmp_path, monkeypatch, marker, state):
    d, _ = _guarded(tmp_path, free_gb=100.0)                                      # NOT holding
    _running_task(d, "CP", state=state)
    out = disp.EXPERIMENTS_ROOT / "g" / "CP"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ckpt_latest.pt").write_bytes(b"an older checkpoint we already hold")
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: disp.LOCAL_DISK_FULL)
    issued = []

    def run(cmd, **kw):
        issued.append(" ".join(cmd))
        return _FakeProc(0, f"/root/spool/active/CP/{marker}\n")

    d.run = run
    d._space_deferred = 0
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "CP") == state, f"{marker}: acted on evidence our own disk refused"
    assert not _events(d, "task_failed", "preempt_requeue", "done")
    assert d._space_deferred == 1
    assert issued and not any("rm -rf" in c for c in issued), (
        "the box's copy was deleted while ours never arrived")


def test_a_done_is_not_declared_while_our_disk_refused_part_of_its_results(tmp_path, monkeypatch):
    """The small completion artifact can land before the disk fills on the checkpoints beside it.
    `done` is terminal and nothing re-pulls a terminal task, so declaring it then strands every
    byte that did not fit: the whole completion waits, artifact present or not."""
    d, _ = _guarded(tmp_path, free_gb=100.0)                                      # NOT holding
    _running_task(d, "PD")
    out = disp.EXPERIMENTS_ROOT / "g" / "PD"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text("{}")                # the artifact made it...
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: disp.LOCAL_DISK_FULL)   # ...the rest did not
    d.run = _marker_run("PD", "DONE")
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "PD") == "running" and not _events(d, "done")

    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)                 # the control
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "PD") == "done"


def test_a_done_whose_RETRY_our_disk_refused_is_not_artifact_missing(tmp_path, monkeypatch):
    """The bulk pull fails for an ordinary reason, the artifact-only retry (inv. 9g) meets the full
    disk: the artifact is not home, which is not the same as absent."""
    d, _ = _guarded(tmp_path, free_gb=100.0)
    _running_task(d, "RT")
    out = disp.EXPERIMENTS_ROOT / "g" / "RT"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    answers = iter([False, disp.LOCAL_DISK_FULL])
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: next(answers))
    d.run = _marker_run("RT", "DONE")
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "RT") == "running" and not _events(d, "task_failed")


@pytest.mark.parametrize("marker, state, lands", [
    ("FAILED_1", "running", "task_failed"), ("PREEMPTED", "preempting", "queued")])
def test_the_same_completions_proceed_on_an_ordinary_failed_pull(
        tmp_path, monkeypatch, marker, state, lands):
    """The control: best-effort forensics and the held-checkpoint rule (17f) are unchanged for a
    pull that failed for any reason other than our disk."""
    d, _ = _guarded(tmp_path, free_gb=100.0)
    _running_task(d, "CQ", state=state)
    out = disp.EXPERIMENTS_ROOT / "g" / "CQ"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ckpt_latest.pt").write_bytes(b"an older checkpoint we already hold")
    monkeypatch.setattr(d, "_result_dir", lambda t: out)
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: False)
    d.run = _marker_run("CQ", marker)
    d._pull_markers(_inst(d), "example.com", 2222)
    assert _state(d, "CQ") == lands


def test_a_box_ingest_that_raises_ENOSPC_here_counts_no_failure(tmp_path):
    d, _ = _guarded(tmp_path, free_gb=100.0)
    _running_task(d, "EN")
    d._ingest_box_failed(1, OSError(errno.ENOSPC, "No space left on device"))
    assert d.tracker.consecutive_fails(1) == 0
    assert d._copies_unconfirmed(), "such a cycle's pulled copies are unconfirmed"
    d._ingest_box_failed(1, RuntimeError("the box really blew up"))           # the control
    assert d.tracker.consecutive_fails(1) == 1


def test_the_tracker_neither_counts_nor_clears_on_a_local_full_pull(tmp_path):
    d, _ = _guarded(tmp_path)
    d.tracker.record(7, False)
    d.tracker.record(7, disp.LOCAL_DISK_FULL)
    assert d.tracker.consecutive_fails(7) == 1, "it must neither add a failure nor vouch for the box"
    d.tracker.record(7, True)
    assert d.tracker.consecutive_fails(7) == 0


# --------------------------------------------------------------------------------------------------
# Invariant 7 — the loop survives a registry that cannot be written
# --------------------------------------------------------------------------------------------------

class _FullRegistry:
    """The dispatcher's connection, with every WRITE failing the way a full volume fails it."""

    def __init__(self, real):
        self._real, self.full = real, True

    def execute(self, sql, *a):
        if self.full and sql.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("exc, why", [
    (sqlite3.OperationalError("database or disk is full"), "registry"),
    (OSError(errno.ENOSPC, "No space left on device"), "write"),
])
def test_the_loop_survives_the_data_root_being_full(tmp_path, monkeypatch, exc, why, capsys):
    pushes = _Pushes()
    d, _ = _guarded(tmp_path, free_gb=100.0, notify=pushes)

    def dies():
        raise exc

    monkeypatch.setattr(d, "poll_once", dies)
    assert d.poll_survivable() is False, "the cycle must be reported abandoned"
    assert d._space_hold and d._space_why == why
    assert len(pushes.sent) == 1 and "[ALERT]" in capsys.readouterr().err
    for _ in range(3):
        assert d.poll_survivable() is False                # still full: still up, no new alert
    assert len(pushes.sent) == 1 and capsys.readouterr().err.count("[ALERT]") == 0
    d._space_noted_at -= 601                               # ten minutes of abandoned cycles later
    for _ in range(3):
        assert d.poll_survivable() is False
    assert capsys.readouterr().err.count("poll cycle abandoned") == 1, (
        "a long outage gets ONE reminder line per ten minutes, not one per cycle and not none")
    assert len(pushes.sent) == 1


@pytest.mark.parametrize("exc", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("no such table: tasks"),
    OSError(errno.EACCES, "Permission denied"),
    RuntimeError("a bug"),
])
def test_every_other_exception_still_ends_the_process(tmp_path, monkeypatch, exc):
    """The control: only the two out-of-space shapes are survived."""
    d, _ = _guarded(tmp_path, free_gb=100.0)

    def dies():
        raise exc

    monkeypatch.setattr(d, "poll_once", dies)
    with pytest.raises(type(exc)):
        d.poll_survivable()
    assert not d._space_hold


def test_out_of_space_on_ANOTHER_filesystem_is_not_survived(tmp_path, monkeypatch):
    """`ENOSPC` naming a path outside the data root (a temp directory elsewhere) is not this
    guard's condition: the reading would say "plenty", the hold would end at once, and the same
    error would begin it again every cycle. It keeps its old behaviour. The same error naming a
    path UNDER the data root is survived."""
    d, _ = _guarded(tmp_path, free_gb=100.0)
    where = {"path": "/somewhere/else/tmpfile"}

    def dies():
        raise OSError(errno.ENOSPC, "No space left on device", where["path"])

    monkeypatch.setattr(d, "poll_once", dies)
    with pytest.raises(OSError):
        d.poll_survivable()
    assert not d._space_hold

    where["path"] = str(disp.EXPERIMENTS_ROOT / "g" / "T" / "ckpt_latest.pt")
    assert d.poll_survivable() is False and d._space_hold and d._space_why == "write"


def test_a_push_channel_that_raises_cannot_kill_the_cycle(tmp_path):
    def broken(*_a):
        raise ConnectionError("the channel itself is down")

    d, _ = _guarded(tmp_path, free_gb=1.0, notify=broken)
    d._check_data_root()                    # must not raise
    assert d._space_hold and d._space_push is not None, "the push must stay owed, to be retried"
    assert len(_events(d, "data_root_low")) == 1


def test_a_healthy_cycle_is_reported_complete(tmp_path, monkeypatch):
    d, _ = _guarded(tmp_path, free_gb=100.0)
    monkeypatch.setattr(d, "poll_once", lambda: None)
    assert d.poll_survivable() is True and not d._space_hold


def test_a_registry_hold_ends_only_when_the_registry_can_record_it(tmp_path, monkeypatch):
    pushes = _Pushes()
    d, reading = _guarded(tmp_path, free_gb=100.0, notify=pushes)   # the READING is healthy throughout
    d.conn = _FullRegistry(d.conn)
    monkeypatch.setattr(d, "poll_once", lambda: d.log("anything", "a real registry write"))
    assert d.poll_survivable() is False and d._space_hold and d._space_why == "registry"

    for _ in range(3):
        d._check_data_root()                # 100 GB free says "resume" — the registry says no
    assert d._space_hold, "a hold was released while its end could not even be recorded"
    assert not _events(d, "data_root_ok", "data_root_low")

    d.conn.full = False                     # someone freed space
    d._check_data_root()
    assert not d._space_hold
    kinds = [e["event"] for e in _events(d, "data_root_low", "data_root_ok")]
    assert kinds == ["data_root_low", "data_root_ok"], kinds
    assert json.loads(_events(d, "data_root_ok")[0]["detail"])["why"] == "registry"
    assert len(pushes.sent) == 2


# --------------------------------------------------------------------------------------------------
# Invariant 8 — idle is unchanged
# --------------------------------------------------------------------------------------------------

def _trace(d):
    """A cycle's observable record with the guard's own reading removed."""
    out = []
    for e in _events(d):
        detail = e["detail"]
        if e["event"] == "poll_cycle":
            j = json.loads(detail)
            j.pop("data_root")
            j["phases"] = sorted(j["phases"])
            detail = {k: j[k] for k in ("n_boxes", "cycle_over_heartbeat_stale", "phases")}
        out.append((e["event"], str(detail), e["task_id"], e["instance_id"]))
    return out


def test_with_ample_space_a_cycle_is_what_it_was_with_the_guard_switched_off(tmp_path, monkeypatch):
    traces, tasks = [], []
    for name, guard_on in (("on.sqlite", True), ("off.sqlite", False)):
        run = _RecordingRun()
        d, _ = _guarded(tmp_path, free_gb=100.0, run=run, name=name)
        if not guard_on:
            _set(d, "data_root_hold_free_gb", 0)
        _running_task(d, "ID")
        _stub_cycle(d, monkeypatch, keep=("_ingest_and_complete",))
        for _ in range(2):
            d.poll_once()
        assert not d._space_hold and not d._copies_unconfirmed()
        assert not _events(d, "data_root_low", "data_root_ok", "notify", "local_pull_no_space")
        assert d.conn.execute("SELECT 1 FROM settings WHERE key=?",
                              (disp.DATA_ROOT_GUARD_KEY,)).fetchone() is None
        assert set(_cycle_event(d)["phases"]) == PHASES
        traces.append(_trace(d))
        tasks.append((_state(d, "ID"), d.tracker.consecutive_fails(1), run.joined()))
    assert traces[0] == traces[1], "the guard changed what an idle cycle records"
    assert tasks[0] == tasks[1], "the guard changed what an idle cycle does"


# --------------------------------------------------------------------------------------------------
# Invariant 10 — the reading is recorded every cycle
# --------------------------------------------------------------------------------------------------

def test_every_cycle_records_the_reading_and_changes_no_other_key(tmp_path, monkeypatch):
    d, reading = _guarded(tmp_path, free_gb=123.456)
    _stub_cycle(d, monkeypatch)
    d.poll_once()
    ev = _cycle_event(d)
    assert ev["data_root"] == {"free_gb": 123.46, "holding": False, "deferred": 0}
    assert set(ev) == {"total_sec", "n_boxes", "cycle_over_heartbeat_stale", "ship_duty", "phases",
                       "ingest_detail", "data_root"}, "ONE new key; the event's others are untouched"

    reading["gb"] = None                    # the read failed
    d.poll_once()
    ev = _cycle_event(d)
    assert ev["data_root"]["free_gb"] is None and "data_root" in ev, (
        "a failed read must be recorded as null, not dropped and not as a number")
    assert ev["data_root"]["holding"] is False

    reading["gb"] = 0.5                     # and in a hold the series goes on
    d.poll_once()
    assert _cycle_event(d)["data_root"] == {"free_gb": 0.5, "holding": True, "deferred": 0}
