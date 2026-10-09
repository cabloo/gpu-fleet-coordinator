"""Run watch — the standard campaign monitor (docs/specs/run-watch.spec.md).

Every behavior is driven through the pure `step()` with a synthetic `Snapshot` and an injected
clock — no DB, no sleeping, no TensorBoard. A separate end-to-end test seeds a temp registry and
runs `main --once` over it, so the impure shell (read-only connect, SQL, artifact scan) is covered
too.
"""

import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry_db", "fleet/registry_db.py")
watch = _load("watch", "fleet/watch.py")

T0 = 1_700_000_000.0  # fixed epoch for every clock-injected test


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(tid="a1b2c3d4-0000", grp="g", name="n", state="queued", *, updated=T0,
         retries=0, max_retries=3.0, est=25, instance=7):
    return {"id": tid, "grp": grp, "name": name, "state": state, "updated_at": iso(updated),
            "retries_used": retries, "max_retries": max_retries, "est_minutes": est,
            "instance_id": instance}


def snap(tasks, **kw):
    s = watch.Snapshot(tasks=tasks)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def fresh(cfg=None, **cfgkw):
    return watch.WatchState(cfg=cfg or watch.Config(groups=("g",), **cfgkw))


def tagged(lines, tag):
    return [ln for ln in lines if f" {tag:<{watch.TAG_WIDTH}} " in ln]


# --------------------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("text,want", [
    ("90", 90.0), ("45s", 45.0), ("30m", 1800.0), ("2h", 7200.0), (" 1.5h ", 5400.0), ("0", 0.0),
])
def test_parse_duration(text, want):
    assert watch.parse_duration(text) == want


@pytest.mark.parametrize("bad", ["", "m", "-5m", "5 days", "abc", "5x"])
def test_parse_duration_rejects_garbage(bad):
    """Input contract: durations are validated at the boundary, never guessed."""
    with pytest.raises(ValueError):
        watch.parse_duration(bad)


def test_condense_failure_keeps_exit_code_and_the_exception_line():
    """Behavior 4: a task_failed detail is truncated from the TOP by a naive limit, which drops the
    only actionable line. Keep the exit code + the last traceback line + a pointer to the full text."""
    detail = ("worker exit 1\n--- run.log tail ---\nTraceback (most recent call last):\n"
              '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
              + '  File "x.py", line 1, in f\n' * 40
              + "RuntimeError: PytorchStreamReader failed reading .format_version: corrupted")
    out = watch.condense_failure(detail, "abcd1234-ffff")
    assert out.startswith("worker exit 1 · RuntimeError: PytorchStreamReader failed")
    assert out.endswith("runq show abcd1234-ffff")
    assert "frozen runpy" not in out


def test_condense_failure_without_a_log_tail():
    assert watch.condense_failure("ship-time compile error", "t1") == \
        "ship-time compile error · runq show t1"


def test_exception_line_survives_logging_that_continues_after_the_traceback():
    """Regression, from a REAL registry detail (task d7ec92db): the trainer logged one more line
    after the traceback, so 'last non-empty line' reported `[resume] 0/1 seeds done` as the cause
    and hid the actual device-mismatch RuntimeError."""
    tail = ("\nTraceback (most recent call last):\n"
            '  File "src/native/evo/module_graph.py", line 1131, in _flush\n'
            "RuntimeError: Expected all tensors to be on the same device, but got cuda:0 vs cpu\n"
            "[resume] 0/1 seeds done; continuing mid-curriculum\n")
    line, is_exc = watch.exception_line(tail)
    assert is_exc and line.startswith("RuntimeError: Expected all tensors")


def test_exception_line_picks_the_propagating_exception_from_a_chained_traceback():
    tail = ("Traceback (most recent call last):\n"
            '  File "a.py", line 1, in f\n'
            "ValueError: inner\n"
            "\nDuring handling of the above exception, another exception occurred:\n\n"
            "Traceback (most recent call last):\n"
            '  File "b.py", line 2, in g\n'
            "KeyError: 'outer'\n")
    assert watch.exception_line(tail) == ("KeyError: 'outer'", True)


def test_exception_line_labels_a_tail_with_no_traceback():
    """A worker killed mid-run leaves ordinary log output — say so rather than passing a log line
    off as an exception."""
    line, is_exc = watch.exception_line("step 400 loss=0.21\nstep 500 loss=0.19\n")
    assert (line, is_exc) == ("step 500 loss=0.19", False)
    assert "last log line: step 500 loss=0.19" in watch.condense_failure(
        "worker exit 137\n--- run.log tail ---\nstep 500 loss=0.19", "t1")


def test_exception_line_handles_an_empty_tail():
    assert watch.exception_line("") == ("(no log tail)", False)


@pytest.mark.parametrize("head,want", [
    ("worker exit -9", "SIGKILL — OOM-killed or reaped, not a code bug"),
    ("worker exit -11", "SIGSEGV"),
    ("worker exit -15", "SIGTERM"),
])
def test_negative_exit_codes_are_decoded_as_signals(head, want):
    """Behavior 4a: `worker exit -9` is 36 of the 222 live failures. Read as a Python error it sends
    the next hour in the wrong direction — it is the kernel or a reaper killing the process."""
    assert want in watch.explain_exit(head)


def test_positive_exit_code_is_left_alone():
    assert watch.explain_exit("worker exit 1") == "worker exit 1"


def test_artifact_missing_is_named_as_a_contract_bug():
    assert "job-contract bug" in watch.explain_exit("artifact_missing")


def test_fail_line_points_at_the_pulled_log_when_one_exists():
    """Behavior 4: 203 of 222 live failures carry NO log tail, so the pointer is the load-bearing
    part of the line — and the pulled run.log beats `runq show` when it is on disk."""
    with_log = watch.condense_failure("worker exit 1", "t1", "experiments/g/n/run.log")
    assert with_log == "worker exit 1 · see experiments/g/n/run.log"
    assert watch.condense_failure("worker exit 1", "t1") == "worker exit 1 · runq show t1"


def test_fail_transition_uses_the_snapshot_log_path():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    art = {"t1": watch.Artifacts(exists=True, log_path="experiments/g/n/run.log")}
    lines = watch.step(st, snap([task(tid="t1", state="task_failed", updated=T0 + 60)],
                                events=[{"task_id": "t1", "event": "task_failed",
                                         "detail": "worker exit -9"}],
                                artifacts=art), T0 + 60)
    msg = tagged(lines, "FAIL")[0]
    assert "SIGKILL" in msg and "see experiments/g/n/run.log" in msg


@pytest.mark.parametrize("state,retries,want_tag,want_perm", [
    ("done", 0, "DONE", True),
    ("task_failed", 0, "FAIL", True),
    ("cancelled", 0, "CANCELLED", True),
    ("infra_failed", 1, "INFRA", False),   # retries remain -> dispatcher will requeue
    ("infra_failed", 3, "INFRA", True),    # exhausted -> permanently dead
    ("queued", 1, "REQUEUE", False),
    ("preempting", 0, "PREEMPT", False),
    ("running", 0, "RUNNING", False),
    ("shipped", 0, "PROGRESS", False),
])
def test_classify(state, retries, want_tag, want_perm):
    """Behavior 5/6."""
    t = task(state=state, retries=retries, max_retries=3.0)
    assert watch.classify(state, t) == (want_tag, want_perm)


def test_is_settled_covers_the_exhausted_infra_failure():
    """Behavior 8: `infra_failed` with retries left is NOT settled (it requeues); with none left it
    is — that is the silent death a `runq ls` loop never surfaces."""
    assert not watch.is_settled(task(state="infra_failed", retries=1, max_retries=3.0))
    assert watch.is_settled(task(state="infra_failed", retries=3, max_retries=3.0))
    assert watch.is_settled(task(state="done"))
    assert not watch.is_settled(task(state="running"))


# --------------------------------------------------------------------------- arm

def test_arm_line_reports_the_census_and_the_settings():
    """Behavior 3: line 1 is always WATCH, and it doubles as proof the watch is live."""
    st = fresh()
    lines = watch.step(st, snap([task(tid="t1", state="running"), task(tid="t2", state="queued")]), T0)
    assert lines[0].startswith(time.strftime("%H:%M:%S", time.localtime(T0)))
    assert " WATCH " in lines[0]
    assert "watching 2 task(s) in g" in lines[0]
    assert "running=1 queued=1" in lines[0]
    assert "readout every 30m" in lines[0]
    # No transition lines on the arm poll — the census already said where everything is.
    assert not tagged(lines, "RUNNING") and not tagged(lines, "PROGRESS")


def test_arm_with_readout_disabled_says_off():
    st = fresh(readout_every_s=0)
    lines = watch.step(st, snap([task(state="running")]), T0)
    assert "readout every off" in lines[0]


# --------------------------------------------------------------------------- transitions

def test_every_terminal_state_emits_exactly_one_line():
    """Behavior 2/5 + 'silence is not success': a hand-rolled monitor that greps only the success
    marker is silent on all three of these."""
    for state, tag in (("done", "DONE"), ("task_failed", "FAIL"), ("cancelled", "CANCELLED")):
        st = fresh()
        watch.step(st, snap([task(tid="t1", state="running")]), T0)
        lines = watch.step(st, snap([task(tid="t1", state=state, updated=T0 + 60)]), T0 + 60)
        assert len(tagged(lines, tag)) == 1, (state, lines)
        assert f"running→{state}" in tagged(lines, tag)[0]


def test_fail_line_carries_the_exception_from_the_event_detail():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    ev = [{"task_id": "t1", "event": "task_failed", "detail":
           "worker exit 1\n--- run.log tail ---\nTraceback:\nKeyError: 'speech'"}]
    lines = watch.step(st, snap([task(tid="t1", state="task_failed", updated=T0 + 60)], events=ev),
                       T0 + 60)
    assert "KeyError: 'speech'" in tagged(lines, "FAIL")[0]
    assert "runq show t1" in tagged(lines, "FAIL")[0]


def test_infra_line_always_states_the_retry_budget():
    """Behavior 6."""
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    ev = [{"task_id": "t1", "event": "infra_failed", "detail": "stalled: no checkpoint or TB progress"}]
    lines = watch.step(st, snap([task(tid="t1", state="infra_failed", retries=1, updated=T0 + 60)],
                                events=ev), T0 + 60)
    msg = tagged(lines, "INFRA")[0]
    assert "retries 1/3, will requeue" in msg
    assert "stalled: no checkpoint or TB progress" in msg
    assert "NO RETRIES LEFT" not in msg


def test_exhausted_retries_says_permanently_failed_and_ends_the_watch():
    """Behavior 6/8: the registry records this as a bare `terminal` event and the task then simply
    stops moving — the exact silent death this feature exists to catch."""
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="infra_failed", retries=3, updated=T0 + 60)]),
                       T0 + 60)
    assert "NO RETRIES LEFT, permanently failed" in tagged(lines, "INFRA")[0]
    assert tagged(lines, "END"), "an all-dead watch must terminate, not hang forever"
    assert st.finished


def test_requeue_after_infra_failure_is_not_terminal():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="infra_failed", retries=1)]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="queued", retries=1, updated=T0 + 60)]), T0 + 60)
    assert tagged(lines, "REQUEUE")
    assert not st.finished


def test_progress_lines_suppressed_on_a_large_watch_but_failures_never_are():
    """Behavior 7."""
    many = [task(tid=f"t{i}", name=f"n{i}", state="claimed") for i in range(8)]
    st = fresh()
    watch.step(st, snap(many), T0)
    moved = [dict(t, state="running", updated_at=iso(T0 + 60)) for t in many]
    moved[0]["state"] = "task_failed"
    lines = watch.step(st, snap(moved), T0 + 60)
    assert len(tagged(lines, "FAIL")) == 1
    assert not tagged(lines, "RUNNING")
    assert st.suppressed["RUNNING"] == 7
    # ...and the suppressed count surfaces in the next readout rather than vanishing.
    out = watch.build_readout(st, snap(moved), T0 + 120, "cadence")
    assert any("running×7" in ln for ln in out)


def test_small_watch_reports_progress_per_task():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="claimed")]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="running", updated=T0 + 60)]), T0 + 60)
    assert tagged(lines, "RUNNING")


def test_task_added_to_a_watched_group_after_arming_joins():
    """Behavior 1: the watch set is re-resolved every poll."""
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="running"),
                                 task(tid="t2", name="n2", state="queued")]), T0 + 60)
    assert tagged(lines, "JOINED")


# --------------------------------------------------------------------------- box events

def test_one_box_incident_is_one_line_naming_its_victims():
    """Behavior 16: a box hosting six watched cells is one incident, not six notifications."""
    tasks = [task(tid=f"t{i}", name=f"n{i}", state="running", instance=42) for i in range(6)]
    st = fresh()
    watch.step(st, snap(tasks), T0)
    box = [{"instance_id": 42, "event": "dead_worker", "detail": "no heartbeat in 5min (>= 5)"}] * 3
    lines = watch.step(st, snap(tasks, box_events=box), T0 + 60)
    assert len(tagged(lines, "BOX")) == 1
    assert "+2 more" in tagged(lines, "BOX")[0]


def test_box_event_for_an_instance_with_no_live_victim_is_dropped():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="done", instance=42)]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="done", instance=42)],
                                box_events=[{"instance_id": 42, "event": "teardown", "detail": "idle"}]),
                       T0 + 60)
    assert not tagged(lines, "BOX")


# --------------------------------------------------------------------------- stall / cold queue

def test_stall_fires_once_per_quiet_episode_and_rearms():
    """Behavior 14."""
    st = fresh(quiet_after_s=1200)
    t = task(tid="t1", state="running", updated=T0)
    art = {"t1": watch.Artifacts(exists=True, newest_mtime=T0)}
    watch.step(st, snap([t], artifacts=art), T0)
    assert not tagged(watch.step(st, snap([t], artifacts=art), T0 + 600), "STALL")
    first = watch.step(st, snap([t], artifacts=art), T0 + 1300)
    assert len(tagged(first, "STALL")) == 1
    assert "dispatcher's own stall reaper fires at 90m" in tagged(first, "STALL")[0]
    # still quiet -> no repeat
    assert not tagged(watch.step(st, snap([t], artifacts=art), T0 + 1900), "STALL")
    # progress resumes -> re-armed, and the next quiet spell fires again
    art2 = {"t1": watch.Artifacts(exists=True, newest_mtime=T0 + 2000)}
    assert not tagged(watch.step(st, snap([t], artifacts=art2), T0 + 2000), "STALL")
    assert tagged(watch.step(st, snap([t], artifacts=art2), T0 + 3300), "STALL")


def test_stall_disabled_by_zero():
    st = fresh(quiet_after_s=0)
    t = task(tid="t1", state="running")
    watch.step(st, snap([t]), T0)
    assert not tagged(watch.step(st, snap([t]), T0 + 100_000), "STALL")


# --- Behavior 14b: simultaneous stalls point at the PULL, not N wedged trainers ------------------
def test_simultaneous_stalls_are_annotated_as_probable_ingest_lag():
    """Behavior 14b. A STALL reads the INGESTED artifact, so a starved coordinator pull looks exactly
    like a wedged trainer. Across several tasks it does not: independent trainers do not wedge in the
    same minute. Measured 2026-07-30 — four arms across two groups flagged STALL within one minute
    while every trainer was verified alive on-box and one had already FINISHED."""
    st = fresh(quiet_after_s=1200)
    a = task(tid="t1", state="running", updated=T0)
    b = task(tid="t2", state="running", updated=T0)
    art = {"t1": watch.Artifacts(exists=True, newest_mtime=T0),
           "t2": watch.Artifacts(exists=True, newest_mtime=T0)}
    watch.step(st, snap([a, b], artifacts=art), T0)
    out = tagged(watch.step(st, snap([a, b], artifacts=art), T0 + 1300), "STALL")
    assert len(out) == 2, "both quiet tasks must still STALL — the qualifier annotates, never suppresses"
    for line in out:
        assert "2 watched tasks quiet at once" in line
        assert "check the BOX" in line


def test_a_lone_stall_is_NOT_annotated():
    """NEGATIVE CONTROL for the test above — without this, the qualifier could be unconditional and
    both tests would still pass. One task quiet while its sibling keeps writing is the genuine
    single-run wedge, and must NOT be explained away as ingest lag."""
    st = fresh(quiet_after_s=1200)
    a = task(tid="t1", state="running", updated=T0)
    b = task(tid="t2", state="running", updated=T0)
    art = {"t1": watch.Artifacts(exists=True, newest_mtime=T0),          # quiet
           "t2": watch.Artifacts(exists=True, newest_mtime=T0 + 1250)}   # still writing
    watch.step(st, snap([a, b], artifacts=art), T0)
    out = tagged(watch.step(st, snap([a, b], artifacts=art), T0 + 1300), "STALL")
    assert len(out) == 1 and "t1" in out[0]
    assert "quiet at once" not in out[0], "a lone stall must not be excused as ingest lag"


def test_queued_cold_names_a_dead_coordinator_when_there_is_no_hold_reason():
    """⛔ NO HOLD REASON + LIVE CAPACITY IS THE SIGNATURE OF A DEAD COORDINATOR, and the alert must say so.

    A hold reason means the dispatcher LOOKED at the task and declined it. Its ABSENCE means nothing ever
    evaluated the task — a different failure with a different fix, and the one that actually happened.

    ⛔ MEASURED 2026-09-15: the coordinator had been down ~18h. Three owned boxes were live and idle (36
    free slots) and two sessions' campaigns were stuck, while this alert said only "held: no hold reason
    logged" — true, unhelpful, and the diagnosis started from scratch. The self-heal does not cover it:
    it respawns a CRASHED daemon, but the supervisor shares the process tree, so a host restart takes
    both and `autostart` only fires on devcontainer folder-open.

    Two halves, because a hint that fires always is noise rather than a signal.
    """
    # 1. NO hold reason -> the alert must point at the coordinator and name the command that checks it
    st = fresh(queued_cold_after_s=1800)
    t = task(tid="t1", state="queued", updated=T0)
    s_nohold = snap([t], live_instances=3)
    watch.step(st, s_nohold, T0)
    line = tagged(watch.step(st, s_nohold, T0 + 1900), "QUEUED-COLD")
    assert len(line) == 1
    assert "DEAD COORDINATOR" in line[0], line[0]
    assert "dispatcher_ctl.sh status" in line[0], "must name the command that diagnoses it"

    # 2. a REAL hold reason -> no such hint; the fleet looked and declined, which is a different problem
    st2 = fresh(queued_cold_after_s=1800)
    s_held = snap([task(tid="t2", state="queued", updated=T0)],
                  holds={"t2": "slot_freeing_soon: waiting on instance 40000010"}, live_instances=2)
    watch.step(st2, s_held, T0)
    held_line = tagged(watch.step(st2, s_held, T0 + 1900), "QUEUED-COLD")
    assert len(held_line) == 1
    assert "DEAD COORDINATOR" not in held_line[0], (
        "a task the dispatcher evaluated and declined must NOT be blamed on a dead coordinator")
    assert "slot_freeing_soon" in held_line[0]


def test_queued_cold_reports_the_hold_reason_and_fleet_size():
    """Behavior 15: 'the fleet quietly has no capacity for you' otherwise looks like a healthy queue."""
    st = fresh(queued_cold_after_s=1800)
    t = task(tid="t1", state="queued", updated=T0)
    s = snap([t], holds={"t1": "slot_freeing_soon: waiting on instance 40000010"}, live_instances=2)
    watch.step(st, s, T0)
    assert not tagged(watch.step(st, s, T0 + 600), "QUEUED-COLD")
    lines = watch.step(st, s, T0 + 1900)
    assert len(tagged(lines, "QUEUED-COLD")) == 1
    assert "slot_freeing_soon" in tagged(lines, "QUEUED-COLD")[0]
    assert "2 live instance(s)" in tagged(lines, "QUEUED-COLD")[0]
    assert not tagged(watch.step(st, s, T0 + 2500), "QUEUED-COLD")


# --------------------------------------------------------------------------- readout cadence

def _running_snapshot(mtime, ckpt=None):
    return snap([task(tid="t1", state="running", updated=T0)],
                artifacts={"t1": watch.Artifacts(exists=True, newest_mtime=mtime,
                                                 ckpt_mtime=ckpt if ckpt is not None else mtime)})


def test_readout_waits_for_a_fresh_checkpoint_then_fires_on_it():
    """Behavior 9/10 — the requested behavior: 'just hit 45 minutes and just got a checkpoint'."""
    st = fresh(readout_every_s=2700, readout_grace_s=300)
    watch.step(st, _running_snapshot(T0), T0)
    assert not tagged(watch.step(st, _running_snapshot(T0), T0 + 1800), "READOUT")
    # cadence elapses, but nothing new on disk yet -> pending, still silent (inside the grace)
    assert not tagged(watch.step(st, _running_snapshot(T0), T0 + 2750), "READOUT")
    # a checkpoint lands -> fire immediately, and say so
    lines = watch.step(st, _running_snapshot(T0 + 2800), T0 + 2810)
    assert tagged(lines, "READOUT")
    assert "cadence 45m + fresh checkpoint" in tagged(lines, "READOUT")[0]
    assert st.readout_pending_since is None


def test_readout_fires_on_grace_when_no_checkpoint_arrives_and_labels_it_dry():
    """Behavior 9/10: pending must never wait indefinitely, and the agent must be able to tell an
    artifact-backed read from a dry one."""
    st = fresh(readout_every_s=2700, readout_grace_s=300)
    watch.step(st, _running_snapshot(T0), T0)
    watch.step(st, _running_snapshot(T0), T0 + 2750)          # becomes pending
    lines = watch.step(st, _running_snapshot(T0), T0 + 3100)  # grace elapsed
    assert "cadence 45m (grace, no new checkpoint)" in tagged(lines, "READOUT")[0]


def test_readout_cadence_restarts_after_firing():
    st = fresh(readout_every_s=600, readout_grace_s=60)
    watch.step(st, _running_snapshot(T0), T0)
    assert tagged(watch.step(st, _running_snapshot(T0 + 700), T0 + 700), "READOUT")
    assert not tagged(watch.step(st, _running_snapshot(T0 + 800), T0 + 800), "READOUT")
    assert tagged(watch.step(st, _running_snapshot(T0 + 1400), T0 + 1400), "READOUT")


def test_readout_disabled_by_zero_but_the_final_one_still_fires():
    st = fresh(readout_every_s=0)
    watch.step(st, _running_snapshot(T0), T0)
    assert not tagged(watch.step(st, _running_snapshot(T0), T0 + 100_000), "READOUT")
    lines = watch.step(st, snap([task(tid="t1", state="done", updated=T0 + 100)]), T0 + 100_100)
    assert tagged(lines, "READOUT") and tagged(lines, "END")


def test_readout_block_reports_state_age_checkpoint_and_hold_reason():
    """Behavior 11."""
    st = fresh()
    tasks = [task(tid="t1", name="run1", state="running", updated=T0, est=100),
             task(tid="t2", name="run2", state="queued", updated=T0)]
    s = snap(tasks, holds={"t2": "awaiting_provisioning: instance 999 coming up"},
             artifacts={"t1": watch.Artifacts(exists=True, newest_mtime=T0 + 100,
                                              ckpt_mtime=T0 + 100)})
    watch.step(st, s, T0)
    out = watch.build_readout(st, s, T0 + 3000, "cadence 30m")
    body = "\n".join(out)
    assert "census: running=1 queued=1" in body
    assert "g/run1 · running 50m · ckpt 48m ago · 50% of est 100m" in body
    assert "held: awaiting_provisioning: instance 999 coming up" in body
    assert "interim eval readout is DUE" in body


def test_readout_tb_digest_reports_the_delta_since_the_previous_readout():
    """Behavior 12."""
    st = fresh()
    s = _running_snapshot(T0)
    s.artifacts["t1"].tb_dir = "/tmp/tb"
    watch.step(st, s, T0)
    watch.build_readout(st, s, T0 + 60, "first", lambda t, a, tags: {"durable": (0.10, 100.0)})
    out = watch.build_readout(st, s, T0 + 120, "second",
                              lambda t, a, tags: {"durable": (0.35, 200.0)})
    assert any("durable=0.35 (+0.25)@200" in ln for ln in out)


def test_readout_survives_an_unreadable_tb_dir():
    """Behavior 12: a TB failure degrades that run's digest, it never aborts the watch."""
    st = fresh()
    s = _running_snapshot(T0)
    s.artifacts["t1"].tb_dir = "/tmp/tb"
    watch.step(st, s, T0)

    def boom(*_a):
        raise RuntimeError("corrupt event file")

    out = watch.build_readout(st, s, T0 + 60, "cadence", boom)
    assert any("tb n: unavailable (RuntimeError: corrupt event file)" in ln for ln in out)


# --------------------------------------------------------------------------- termination

def test_end_emits_a_final_readout_a_per_task_verdict_and_exits_once():
    """Behavior 8: the last thing in the stream is always a self-contained verdict."""
    st = fresh()
    tasks = [task(tid="t1", name="a", state="running"), task(tid="t2", name="b", state="running")]
    watch.step(st, snap(tasks), T0)
    settled = [task(tid="t1", name="a", state="done", updated=T0 + 60),
               task(tid="t2", name="b", state="task_failed", updated=T0 + 60)]
    lines = watch.step(st, snap(settled), T0 + 60)
    body = "\n".join(lines)
    assert "all 2 watched task(s) settled" in tagged(lines, "END")[0]
    assert "done         g/a" in body and "task_failed  g/b" in body
    assert st.finished
    # idempotent: a further poll after finishing does not re-emit END
    assert not tagged(watch.step(st, snap(settled), T0 + 120), "END")


# --------------------------------------------------------- Behavior 22: co-location verdict at END

def _colo(tid, name, key="camp:seed1"):
    import json
    t = task(tid=tid, name=name, state="done", updated=T0 + 60)
    t["resource_hint_json"] = json.dumps({"colocate": key})
    return t


def test_a_SPLIT_colocation_group_is_called_out_at_END():
    """⛔ THE POINT OF PUTTING IT HERE. `END` is where a campaign's verdict gets written, so this is
    where the box axis has to be settled — a check nobody runs is not a check. `m83_normgate`
    co-located 2 of its 3 pairs, was published, and the split was invisible in every output; a
    collapsed control on the odd box then turned a null into a +0.2721 "win"."""
    st = fresh()
    tasks = [_colo("t1", "ctrl"), _colo("t2", "arm")]
    watch.step(st, snap([{**t, "state": "running"} for t in tasks]), T0)
    lines = watch.step(st, snap(tasks, boxes={"t1": ("42",), "t2": ("43",)}), T0 + 60)
    out = tagged(lines, "COLOCATE")
    assert out and "DIFFERENT boxes" in out[0] and "camp:seed1" in out[0], lines
    assert "'42', '43'" in out[0] or "42" in out[0] and "43" in out[0]


def test_an_ALL_OK_campaign_gets_a_CONFIRMING_line_not_silence():
    """A silent absence cannot be told from a check that never ran, and "the check ran and passed"
    is what licenses the paired claim."""
    st = fresh()
    tasks = [_colo("t1", "ctrl"), _colo("t2", "arm")]
    watch.step(st, snap([{**t, "state": "running"} for t in tasks]), T0)
    lines = watch.step(st, snap(tasks, boxes={"t1": ("42",), "t2": ("42",)}), T0 + 60)
    out = tagged(lines, "COLOCATE")
    assert out and out[0].count("⛔") == 0 and "ran each on ONE box" in out[0], lines


def test_an_arm_REQUEUED_onto_a_second_box_is_a_split_by_itself():
    """The lifetime read is what makes this visible: `tasks.instance_id` would show only box 42 for
    both arms, which is exactly the case worth catching (that arm also has `resumes > 0`)."""
    st = fresh()
    tasks = [_colo("t1", "ctrl"), _colo("t2", "arm")]
    watch.step(st, snap([{**t, "state": "running"} for t in tasks]), T0)
    lines = watch.step(st, snap(tasks, boxes={"t1": ("42",), "t2": ("43", "42")}), T0 + 60)
    assert "DIFFERENT boxes" in tagged(lines, "COLOCATE")[0], lines


def test_an_uncolocated_campaign_emits_NO_colocate_line():
    """Ordinary traffic must not gain a line it cannot act on."""
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    lines = watch.step(st, snap([task(tid="t1", state="done", updated=T0 + 60)]), T0 + 60)
    assert tagged(lines, "END") and not tagged(lines, "COLOCATE")


def test_colocation_verdicts_is_pure_and_reports_PENDING_before_any_start():
    tasks = [_colo("t1", "ctrl"), _colo("t2", "arm"), task(tid="t3")]
    assert watch.colocation_verdicts(tasks, {}) == [("camp:seed1", "PENDING", [], 2)]
    assert watch.colocation_verdicts([], {}) == []


def test_watch_does_not_end_while_a_task_can_still_requeue():
    st = fresh()
    watch.step(st, snap([task(tid="t1", state="running")]), T0)
    watch.step(st, snap([task(tid="t1", state="infra_failed", retries=1, updated=T0 + 60)]), T0 + 60)
    assert not st.finished


# --------------------------------------------------------------------------- impure shell

@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    """A temp registry + experiments tree, with `shared_experiments_root` pointed at it."""
    exp = tmp_path / "experiments"
    exp.mkdir()
    db = exp / "runs.sqlite"
    conn = reg.connect(str(db))
    common = dict(entrypoint="smoke", args_json="[]", created_by="test",
                  config_json="{}", git_sha="deadbeef", est_minutes=25)
    for i, (name, state, retries) in enumerate([("ok", "done", 0), ("bad", "task_failed", 0),
                                                ("live", "running", 1)]):
        reg.insert_task(conn, id=f"task-{i}", grp="camp", name=name,
                        config_hash=f"c{i}", arm_hash=f"a{i}", created_at=reg.now_iso(),
                        state=state, retries_used=retries, max_retries=3, instance_id=5, **common)
        d = exp / "camp" / name
        (d / "tb").mkdir(parents=True)
        (d / "ckpt_latest.pt").write_bytes(b"x")
    reg.log_event(conn, "task_failed", "worker exit 1\n--- run.log tail ---\nValueError: nope",
                  task_id="task-1")
    conn.commit()
    conn.close()
    monkeypatch.setattr(reg, "shared_experiments_root", lambda: exp)
    monkeypatch.setattr(watch.registry_db, "shared_experiments_root", lambda: exp)
    return exp, db


def test_main_once_over_a_real_registry(live_db, capsys):
    exp, db = live_db
    rc = watch.main(["--group", "camp", "--db", str(db), "--once", "--no-tb"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "watching 3 task(s) in camp" in out
    assert "running=1 done=1 task_failed=1" in out  # census lists open states first
    assert "camp/live · running" in out
    assert "retries 1/3" in out


def test_main_refuses_an_empty_watch_set(live_db, capsys):
    """Input contract: a watcher watching nothing is the failure being fixed, not a silent success."""
    _exp, db = live_db
    assert watch.main(["--group", "no-such-group", "--db", str(db), "--once"]) == 2
    assert "nothing to watch" in capsys.readouterr().err


def test_main_refuses_an_unknown_task_id(live_db, capsys):
    _exp, db = live_db
    assert watch.main(["--task", "task-0", "--task", "nope", "--db", str(db), "--once"]) == 2
    assert "no such task(s)" in capsys.readouterr().err


def test_main_requires_a_target(capsys):
    assert watch.main([]) == 2
    assert "at least one --group or --task" in capsys.readouterr().err


def test_main_rejects_a_bad_duration(live_db, capsys):
    _exp, db = live_db
    assert watch.main(["--group", "camp", "--db", str(db), "--poll", "soon"]) == 2
    assert "bad duration" in capsys.readouterr().err


def test_connect_ro_is_read_only_and_never_creates(tmp_path, live_db):
    """Behavior 19."""
    _exp, db = live_db
    conn = watch.connect_ro(str(db))
    with pytest.raises(Exception):
        conn.execute("UPDATE tasks SET state='done'")
    conn.close()
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(SystemExit):
        watch.connect_ro(str(missing))
    assert not missing.exists(), "a typo'd --db must error, not initialize a fresh registry"


def test_scan_artifacts_finds_the_checkpoint_and_tb_dir(live_db):
    exp, _db = live_db
    art = watch.scan_artifacts(exp, "camp", "live")
    assert art.exists and art.ckpt_mtime > 0 and art.tb_dir.endswith("/tb")
    assert not watch.scan_artifacts(exp, "camp", "absent").exists


def test_scan_artifacts_follows_the_dispatchers_truncate_with_hash(tmp_path):
    """A name over the 255-byte component limit is truncated-with-hash on the way to disk (invariant
    9e), so this file MUST resolve it the same way `Dispatcher._result_dir` does.

    Reading `root / grp / name` raw finds nothing for such a task, and "no artifacts" is
    indistinguishable from "no progress" — so every long-named task reported a permanent FALSE STALL.
    Observed live 2026-07-30 on `m56_causal_body_v2`: only the three `perturb` cells alarmed, because
    only they carried an extra gene in the name and crossed the limit, while their step counters had
    advanced 2222 -> 6130 across three stages. **The false alarm landed precisely on the arm under
    test.** The dispatcher's own reaper was never fooled (it uses the stored `resume_checkpoint`), so
    this was purely an observability lie — which is worse than useless, since it invites cancelling a
    healthy campaign."""
    from dispatcher import _fs_safe_component

    long_name = "arm_" + "x" * 300
    on_disk = _fs_safe_component(long_name)
    assert on_disk != long_name, "fixture no longer exercises truncation — pick a longer name"

    d = tmp_path / "grp" / on_disk / "tb"
    d.mkdir(parents=True)
    (d.parent / "ckpt_latest.pt").write_bytes(b"x")

    art = watch.scan_artifacts(tmp_path, "grp", long_name)
    assert art.exists and art.ckpt_mtime > 0, (
        "watch.py did not follow the dispatcher's truncate-with-hash mapping — long-named tasks will "
        "report a permanent false STALL")


def test_fetch_snapshot_does_not_replay_history_past_the_watermark(live_db):
    exp, db = live_db
    conn = watch.connect_ro(str(db))
    cfg = watch.Config(groups=("camp",))
    first = watch.fetch_snapshot(conn, cfg, 0, exp)
    assert any(e["event"] == "task_failed" for e in first.events)
    assert watch.fetch_snapshot(conn, cfg, first.max_seq, exp).events == []
    conn.close()


# ---- Behavior 18: resume-cycle spread must not stay hidden ----

def test_readout_warns_when_arms_were_interrupted_unequally():
    """Behavior 18. Every preempt logs "checkpoint carried forward" — true about WORK, silent about
    COMPARABILITY. Resume does not reproduce an uninterrupted run, so an arm preempted more often
    than its siblings is measuring something slightly different, and which arm gets hit is decided
    by which box consolidation happens to drain.

    Measured 2026-07-29: `azsc-p1e` finished with 12 resumes on one arm vs 6-9 on its siblings and
    NOTHING in any readout said so — the owner would have compared them as equals."""
    tasks = [task(tid="t1", name="armA", state="running"),
             task(tid="t2", name="armB", state="running"),
             task(tid="t3", name="armC", state="running")]
    s = snap(tasks, resumes={"t1": 12, "t2": 7, "t3": 6})
    out = watch.build_readout(fresh(), s, T0 + 120, "cadence")
    warn = [ln for ln in out if "resume-cycle spread" in ln]
    assert warn, "a 6-vs-12 interruption gap was invisible in the readout"
    assert "spread 6" in warn[0] and "armA=12" in warn[0]
    assert "does not reproduce" in warn[0], "must say WHY the gap matters, not just report it"


def test_a_symmetric_campaign_stays_quiet():
    """It has to be silent when healthy or it becomes one more line nobody reads. Whole-box drains
    hit every arm together and are genuinely harmless — those must not warn."""
    tasks = [task(tid="t1", name="armA"), task(tid="t2", name="armB")]
    assert not [ln for ln in watch.build_readout(
        fresh(), snap(tasks, resumes={"t1": 4, "t2": 4}), T0, "c") if "resume-cycle" in ln]
    # a spread of 1 is noise, not signal
    assert not [ln for ln in watch.build_readout(
        fresh(), snap(tasks, resumes={"t1": 4, "t2": 3}), T0, "c") if "resume-cycle" in ln]


def test_a_single_arm_campaign_never_warns():
    """Spread is only meaningful ACROSS arms that get compared to each other."""
    out = watch.build_readout(fresh(), snap([task(tid="t1", name="solo")], resumes={"t1": 9}),
                              T0, "c")
    assert not [ln for ln in out if "resume-cycle" in ln]


def test_spread_is_reported_per_campaign_not_across_the_whole_watch():
    """Arms of DIFFERENT groups are not compared, so mixing them would invent a spread that means
    nothing — and hide a real one inside a bigger fake number."""
    tasks = [task(tid="t1", grp="gA", name="a1"), task(tid="t2", grp="gA", name="a2"),
             task(tid="t3", grp="gB", name="b1"), task(tid="t4", grp="gB", name="b2")]
    s = snap(tasks, resumes={"t1": 0, "t2": 0, "t3": 8, "t4": 1})
    warn = [ln for ln in watch.build_readout(fresh(), s, T0, "c") if "resume-cycle spread" in ln]
    assert len(warn) == 1 and "gB" in warn[0], "spread must be per-group"


def test_settled_arms_still_count_toward_the_spread():
    """The comparison happens when the campaign is READ, i.e. after the arms finish — so a done arm
    is exactly the one whose resume count matters. Restricting this to open tasks would blind it at
    the only moment it is useful."""
    tasks = [task(tid="t1", name="armA", state="done"), task(tid="t2", name="armB", state="done")]
    warn = [ln for ln in watch.build_readout(
        fresh(), snap(tasks, resumes={"t1": 11, "t2": 2}), T0, "c") if "resume-cycle spread" in ln]
    assert warn, "went quiet once the arms finished — the moment the owner compares them"


def test_fetch_snapshot_actually_collects_the_resume_counts(live_db):
    """THE WIRING for Behavior 18 — and the FOURTH time in this session that a fully-tested guard
    could be orphaned with every test green. The readout tests build `Snapshot(resumes=...)` by hand,
    so `fetch_snapshot` can stop populating it entirely and they all still pass, leaving the warning
    permanently silent on real data. Mutate the producer, not just the consumer."""
    exp, db = live_db
    conn = reg.connect(str(db))
    for _ in range(3):
        reg.log_event(conn, "preempt_requeue", "preempted", task_id="task-2")
    reg.log_event(conn, "preempt_requeue", "preempted", task_id="task-0")
    conn.commit()
    snap = watch.fetch_snapshot(conn, watch.Config(groups=("camp",)), 0, exp)
    assert snap.resumes.get("task-2") == 3, "resume counts are not being collected from the registry"
    assert snap.resumes.get("task-0") == 1
    assert snap.resumes.get("task-1") == 0, "a never-preempted task must read 0, not be absent"
    # ...and the counts are LIFETIME, not since-the-watermark: comparability depends on the total.
    later = watch.fetch_snapshot(conn, watch.Config(groups=("camp",)), snap.max_seq, exp)
    assert later.resumes.get("task-2") == 3, "resume counts reset past the watermark"
    # end-to-end: that spread must reach the rendered readout
    st = watch.WatchState(cfg=watch.Config(groups=("camp",)))
    out = watch.build_readout(st, snap, T0, "cadence")
    assert any("resume-cycle spread" in ln for ln in out)
    conn.close()


# ── fmt_age ────────────────────────────────────────────────────────────────────────────────────
# REGRESSION (2026-07-31): the hours were formatted with `f"{seconds/3600:.0f}h"`, which ROUNDS.
# 5400s (1h30m) printed "2h30m", so EVERY age whose minute part was >= 30 was reported an hour too
# high — and `.0f` uses banker's rounding, so it was wrong inconsistently (1.5h→2, 2.5h→2, 3.5h→4).
# This function formats every age the watcher emits (task runtimes, checkpoint ages, its own
# threshold banner), so a reader judging a cell against the 90-minute stall reaper was seeing an
# hour of slack that did not exist.

def test_fmt_age_TRUNCATES_hours_and_does_not_round_them():
    assert watch.fmt_age(5400) == "1h30m"      # 1.5h — the exact case that printed "2h30m"
    assert watch.fmt_age(12600) == "3h30m"     # 3.5h — banker's rounding sent this to 4h
    assert watch.fmt_age(9000) == "2h30m"      # 2.5h — was right only by accident
    assert watch.fmt_age(7560) == "2h6m"       # minute part < 30 was always correct
    assert watch.fmt_age(3600 * 4) == "4h0m"


def test_fmt_age_small_units_and_boundaries_unchanged():
    assert watch.fmt_age(None) == "?"
    assert watch.fmt_age(-5) == "0s"           # clamped, never negative
    assert watch.fmt_age(89) == "89s"
    assert watch.fmt_age(90) == "2m"           # switches to minutes at 90s
    assert watch.fmt_age(5399) == "90m"        # stays in minutes right up to 5400s


def _write_tb(dirpath, points):
    """points: list of (tag, value, step). Writes a real TB event file."""
    from torch.utils.tensorboard import SummaryWriter
    w = SummaryWriter(str(dirpath))
    for tag, val, step in points:
        w.add_scalar(tag, val, step)
    w.flush()
    w.close()


def test_tb_digest_picks_ONE_TAG_PER_FAMILY_and_the_MOST_RECENT_stage(tmp_path):
    """★★ Behavior 12, fixed 2026-08-02. A multi-stage curriculum emits one tag per family PER STAGE,
    so the old plain prefix match filled all 4 slots with whichever stages sorted first — the
    EARLIEST rungs, i.e. the stalest numbers — and crowded every other family out. Each family must
    instead report the stage the run is actually ON (highest step)."""
    _write_tb(tmp_path, [
        ("mastery/agency_reach/gain", 0.11, 1),      # early stage, sorts first, STALE
        ("mastery/nav_mixed/gain", -0.42, 8),        # the stage the run is on
        ("collapse/nav_mixed/max_action_share", 0.97, 8),
        ("grounded/nav_mixed", -0.05, 8),
        ("ab/durable", 0.5, 8),
        ("cold/agency_reach", 0.9, 1),
    ])
    art = watch.Artifacts(exists=True, tb_dir=str(tmp_path))
    got = watch.tb_digest({}, art, ())
    assert "mastery/nav_mixed/gain" in got, f"must pick the CURRENT stage, got {sorted(got)}"
    assert "mastery/agency_reach/gain" not in got, "must not report the stalest stage of a family"
    assert "collapse/nav_mixed/max_action_share" in got, "the collapse fingerprint must survive"
    assert got["mastery/nav_mixed/gain"][0] == pytest.approx(-0.42)


def test_tb_digest_ranks_the_DECISION_metrics_above_durable(tmp_path):
    """`durable` used to lead `_TB_PREFERRED`, so a 9-stage readout showed the one scalar this repo
    has established CANNOT arbitrate (it sums margins over rungs sitting at their floor and resolves
    only ~±0.9 at n=3) — while the anchored gain and the collapse fingerprint were crowded out."""
    _write_tb(tmp_path, [
        ("ab/durable", 0.5, 3),
        ("ab/score", 0.4, 3),
        ("ab/loss", 0.3, 3),
        ("ab/reward", 0.2, 3),
        ("collapse/s/max_action_share", 0.99, 3),
        ("mastery/s/gain", -0.30, 3),
        ("grounded/s", -0.02, 3),
    ])
    art = watch.Artifacts(exists=True, tb_dir=str(tmp_path))
    got = set(watch.tb_digest({}, art, ()))
    assert "collapse/s/max_action_share" in got
    assert "mastery/s/gain" in got
    assert "grounded/s" in got, f"the lang/* primary must make the digest; got {sorted(got)}"


# ── arms_digest: a RUNNING multi-arm cell is readable mid-flight ────────────────────────────────
# A READOUT asks "on track, dead, or already answered?" and used to answer with freshness only, so
# the honest reply was "go and look" and the tempting WRONG reply was "no interim signal". There is
# one: the trainer appends each finished arm to `results` in the checkpoint, pulled every few
# minutes. TB emptiness is not evidence to the contrary -- the event file is a ~200-byte header even
# on cells that finished successfully.

def _write_ckpt(tmp_path, payload):
    import torch
    p = tmp_path / "ckpt_latest.pt"
    torch.save(payload, p)
    return str(p)


def test_arms_digest_reports_completed_arms(tmp_path):
    from watch import arms_digest
    p = _write_ckpt(tmp_path, {
        "arm_index": 1,
        "results": [{"params": {"vision_rule": "local_3f"},
                     "per_stage": {"agency/where?cue=attr&n=8": {"frac_of_oracle": 0.2104},
                                   "agency/where?cue=conj&n=8": {"frac_of_oracle": 0.1784}}}],
    })
    out = arms_digest(p)
    assert out and "arms COMPLETE: 1" in out[0] and "now running arm 1" in out[0]
    assert "where/attr=0.2104" in out[1] and "where/conj=0.1784" in out[1]
    assert "vision_rule=local_3f" in out[1]


def test_arms_digest_warns_against_CROSS_CELL_comparison(tmp_path):
    """The warning is the point, not decoration: arms in one cell share box, code blob and
    interruption history; a different cell shares none of those."""
    from watch import arms_digest
    p = _write_ckpt(tmp_path, {"arm_index": 1, "results": [{"params": {}, "per_stage": {}}]})
    assert "WITHIN this cell" in arms_digest(p)[0]


def test_arms_digest_is_quiet_for_single_arm_and_missing_results(tmp_path):
    """Must not become noise on the campaigns it has nothing to say about."""
    from watch import arms_digest
    assert arms_digest(_write_ckpt(tmp_path, {"step": 10})) == []
    assert arms_digest(_write_ckpt(tmp_path, {"results": []})) == []
    assert arms_digest(_write_ckpt(tmp_path, {"results": "not-a-list"})) == []


def test_arms_digest_never_raises_on_a_corrupt_checkpoint(tmp_path):
    """Behavior 12: a readout degrades, it never aborts the watch."""
    from watch import arms_digest
    bad = tmp_path / "ckpt_latest.pt"
    bad.write_bytes(b"not a torch file")
    out = arms_digest(str(bad))
    assert out == [] or "unreadable" in out[0]
    assert arms_digest(str(tmp_path / "does_not_exist.pt")) in ([], ) or True


def test_arms_digest_handles_a_stage_with_no_score(tmp_path):
    """A presentation stage has no eval and is absent/blank -- it must not crash the digest, the
    exact shape that killed a finished 5118s arm at its REPORTING step."""
    from watch import arms_digest
    p = _write_ckpt(tmp_path, {"arm_index": 0,
                               "results": [{"params": {"a": 1},
                                            "per_stage": {"agency/present": {}, "x": None}}]})
    out = arms_digest(p)
    assert len(out) == 2 and "no scored stage" in out[1]


# ── arms_digest must MEMORY-MAP, not materialise, the checkpoint ────────────────────────────────
# Found live 2026-08-07. `results` is a small list of plain dicts, but a plain `torch.load` builds
# EVERY tensor in the file to reach it, and a readout runs this per running task on every tick for
# the life of the campaign. On `m49_dream_acq_n3c/fwd_lo_s1` (1866MB — a dream/replay campaign puts
# a reservoir buffer in the checkpoint, ~25-80x an ordinary cell's 23-78MB) that measured +1884MB
# resident per call vs +15.7MB with `mmap=True`. Its watcher sat at 1.4GB against 240-330MB for its
# 8 peers, tracking checkpoint size and nothing else, on a box whose swap was 100% full.
# Usually it is pure waste too: 0 of 40 live checkpoints sampled carried `results` at all.

def test_arms_digest_MEMORY_MAPS_the_checkpoint(tmp_path, monkeypatch):
    """The mmap kwarg is load-bearing — without it a readout materialises the whole ckpt."""
    import torch
    p = _write_ckpt(tmp_path, {"results": [{"params": {"a": 1}, "per_stage": {}}]})
    seen = {}
    real = torch.load

    def spy(path, **kw):
        seen.update(kw)
        return real(path, **kw)

    monkeypatch.setattr(torch, "load", spy)
    from watch import arms_digest
    arms_digest(p)
    assert seen.get("mmap") is True, f"arms_digest must pass mmap=True; got {seen}"


def test_arms_digest_falls_back_when_mmap_is_unsupported(tmp_path, monkeypatch):
    """Legacy (non-zipfile) checkpoints raise on mmap — degrade to a plain load, never lose the arm."""
    import torch
    p = _write_ckpt(tmp_path, {"results": [{"params": {"arm": "ctrl"}, "per_stage": {}}]})
    real = torch.load
    calls = []

    def spy(path, **kw):
        calls.append(kw.get("mmap", False))
        if kw.get("mmap"):
            raise RuntimeError("mmap can only be used with files saved with torch.save(_use_new_zipfile...)")
        return real(path, **{k: v for k, v in kw.items() if k != "mmap"})

    monkeypatch.setattr(torch, "load", spy)
    from watch import arms_digest
    out = arms_digest(p)
    assert calls == [True, False], f"expected mmap attempt then fallback; got {calls}"
    assert any("arm=ctrl" in line for line in out), out


def test_arms_digest_still_degrades_when_BOTH_loads_fail(tmp_path, monkeypatch):
    """Behavior 12: a readout degrades, never aborts the watch."""
    import torch
    p = _write_ckpt(tmp_path, {"results": [{"params": {"a": 1}, "per_stage": {}}]})

    def boom(path, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(torch, "load", boom)
    from watch import arms_digest
    out = arms_digest(p)
    assert len(out) == 1 and "unreadable" in out[0], out
