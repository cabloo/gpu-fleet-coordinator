"""Spool worker — validation trust boundary + a real local claim/run/DONE/PREEMPT cycle.

No network involved (payload is a real `git archive`, task runs as a real subprocess) — this is
the same code path the docker integration test drives over ssh, exercised here directly against
the local filesystem so it runs in plain `pytest`, no container required.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


worker_mod = _load("spool_worker", "fleet/spool_worker.py")


def _git_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def _stage_task(spool: Path, task_id: str, argv_tail: list, pip_extras: list | None = None) -> Path:
    d = spool / "incoming" / task_id
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "archive", "--format=tar.gz", _git_sha(), "-o", str(d / "payload.tar.gz")],
                    cwd=ROOT, check=True)
    task = {
        "task_id": task_id, "grp": "demo", "name": task_id,
        "argv": ["python", "fleet/smoke_entrypoint.py", *argv_tail],
        "env": {}, "est_minutes": 1, "git_sha": _git_sha(), "pip_extras": pip_extras or [],
        "resume_from": None,
    }
    (d / "task.json").write_text(json.dumps(task))
    (d / "READY").touch()
    return d


class TestValidateTaskJson:
    def test_missing_key_rejected(self):
        data = {"task_id": "t", "grp": "g", "name": "n", "argv": ["x"], "env": {},
                "est_minutes": 1, "git_sha": "x", "pip_extras": []}  # resume_from missing
        assert worker_mod.validate_task_json(data) is not None

    def test_unknown_key_rejected(self):
        data = {"task_id": "t", "grp": "g", "name": "n", "argv": ["x"], "env": {},
                "est_minutes": 1, "git_sha": "x", "pip_extras": [], "resume_from": None,
                "bogus": 1}
        assert worker_mod.validate_task_json(data) is not None

    def test_empty_argv_rejected(self):
        data = {"task_id": "t", "grp": "g", "name": "n", "argv": [], "env": {},
                "est_minutes": 1, "git_sha": "x", "pip_extras": [], "resume_from": None}
        assert worker_mod.validate_task_json(data) is not None

    def test_valid_passes(self):
        data = {"task_id": "t", "grp": "g", "name": "n", "argv": ["python", "x.py"], "env": {},
                "est_minutes": 1, "git_sha": "x", "pip_extras": [], "resume_from": None}
        assert worker_mod.validate_task_json(data) is None


class TestWorkerRejectFixture:
    """fixtures/dispatch/worker_reject.json: task.json missing argv -> reject + FAILED_validation,
    nothing executed."""

    def test_missing_argv_rejects_without_executing(self, tmp_path):
        spool = tmp_path / "spool"
        d = spool / "incoming" / "bad1"
        d.mkdir(parents=True)
        (d / "payload.tar.gz").write_bytes(b"")
        bad = {"task_id": "bad1", "grp": "g", "name": "bad1", "env": {}, "est_minutes": 1,
               "git_sha": "x", "pip_extras": [], "resume_from": None}  # argv missing
        (d / "task.json").write_text(json.dumps(bad))
        (d / "READY").touch()

        w = worker_mod.Worker(spool)
        w.tick()
        w.tick()

        active = spool / "active" / "bad1"
        assert (active / "FAILED_validation").exists()
        assert not (active / "repo").exists()  # never extracted/executed
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        kinds = [e["event"] for e in events]
        assert kinds == ["claim", "reject"]


@pytest.mark.slow
class TestPipInstallFailure:
    """Regression: found live 2026-07-09 against a real stuck Vast box — a plain `pip install`
    silently failed (PEP-668 "externally-managed-environment" on a newer pytorch/pytorch image),
    and the worker launched the training script anyway, which crashed on the very first
    `import` of whatever the missing extra was. The box then sat idle (0% GPU) for over an
    hour, still billing, until this was diagnosed by hand over ssh."""

    def test_uninstallable_extra_fails_fast_without_attempting_launch(self, tmp_path):
        spool = tmp_path / "spool"
        _stage_task(spool, "badpkg", ["--updates", "3"],
                    pip_extras=["this-package-definitely-does-not-exist-xyz123"])
        w = worker_mod.Worker(spool)
        deadline = time.time() + 30
        while time.time() < deadline:
            w.tick()
            if (spool / "active" / "badpkg" / "FAILED_pip_extras").exists():
                break
            time.sleep(0.3)
        assert (spool / "active" / "badpkg" / "FAILED_pip_extras").exists()
        assert not (spool / "active" / "badpkg" / "out" / "summary.json").exists()
        assert "badpkg" not in w.active  # never left pending for launch_ready to attempt
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        assert [e["event"] for e in events] == ["claim", "reject"]


@pytest.mark.slow
class TestClaimRunDoneCycle:
    def test_full_lifecycle_to_done(self, tmp_path):
        spool = tmp_path / "spool"
        _stage_task(spool, "ok1", ["--updates", "3", "--sleep-per-step", "0.05", "--ckpt-every", "2"])
        w = worker_mod.Worker(spool)
        deadline = time.time() + 20
        while time.time() < deadline:
            w.tick()
            if (spool / "active" / "ok1" / "DONE").exists():
                break
            time.sleep(0.3)
        assert (spool / "active" / "ok1" / "DONE").exists()
        summary = json.loads((spool / "active" / "ok1" / "out" / "summary.json").read_text())
        assert summary["updates_done"] == 3
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        assert [e["event"] for e in events] == ["claim", "start", "exit"]
        assert events[-1]["rc"] == 0

    def test_science_failure_writes_failed_marker(self, tmp_path):
        spool = tmp_path / "spool"
        _stage_task(spool, "fail1", ["--updates", "5", "--sleep-per-step", "0.05", "--fail-at", "2"])
        w = worker_mod.Worker(spool)
        deadline = time.time() + 20
        matched = None
        while time.time() < deadline:
            w.tick()
            matched = list((spool / "active" / "fail1").glob("FAILED_*"))
            if matched:
                break
            time.sleep(0.3)
        assert matched, "expected a FAILED_<rc> marker"
        assert matched[0].name == "FAILED_1"

    def test_check_exits_survives_dir_removed_by_concurrent_requeue(self, tmp_path):
        """Regression: the dispatcher can rm -rf active/<id> mid-teardown while a task is
        concurrently exiting on the box (a routine requeue race) -- check_exits used to crash
        on the now-missing dir's write_text, killing the whole worker process and stranding
        every OTHER task on the box (observed live: turned a routine requeue into a 3h dead
        box). A single task's teardown race must not take down the worker."""
        spool = tmp_path / "spool"
        spool.mkdir()
        active_dir = spool / "active"
        active_dir.mkdir()
        w = worker_mod.Worker(spool)
        task_dir = active_dir / "vanished"
        task_dir.mkdir()
        at = worker_mod.ActiveTask("vanished", task_dir)
        at.proc = subprocess.Popen(["true"])
        at.proc.wait()
        w.active["vanished"] = at
        import shutil as _shutil
        _shutil.rmtree(task_dir)  # simulate the dispatcher's concurrent rm -rf
        w.check_exits()  # must not raise
        assert "vanished" not in w.active
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        assert events[-1]["task_id"] == "vanished"
        assert events[-1]["detail"] == "done"


class TestThreadCapsInLaunchEnv:
    """Calibration (2026-07-15): the launch env pinned no BLAS/OMP thread count, so every process
    defaulted to nproc threads. An env-worker RL task (main + env_workers subprocesses) then spawned
    ~nproc threads PER process — two tasks saturated a 12-core box (load ~26) and tripped
    pthread_create/RLIMIT_NPROC. should_launch's load guard caps task COUNT, not per-task thread
    blow-up, so the launch env must cap BLAS/OMP threads (overridable by a task's own spec env)."""

    class _Dummy:
        pid = 4321
        def poll(self): return None      # looks alive so the worker doesn't finalize it
        def wait(self, timeout=None): return None
        def terminate(self): pass
        def kill(self): pass

    def _run_until_launch(self, w, captured, monkeypatch):
        real_popen = worker_mod.subprocess.Popen
        def _fake_popen(*args, **kwargs):
            # Only intercept the task launch (cwd=.../repo); delegate other Popen uses
            # (e.g. subprocess.run's tar extraction) to the real implementation.
            if str(kwargs.get("cwd") or "").endswith("repo"):
                captured["env"] = kwargs.get("env")
                return self._Dummy()
            return real_popen(*args, **kwargs)
        monkeypatch.setattr(worker_mod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(worker_mod, "should_launch", lambda *a, **k: (True, "test"))
        for _ in range(6):
            w.tick()
            if "env" in captured:
                break
        assert "env" in captured, "launch never occurred"

    def test_launch_env_caps_blas_and_omp_threads(self, tmp_path, monkeypatch):
        spool = tmp_path / "spool"
        _stage_task(spool, "capt", ["--updates", "1", "--sleep-per-step", "0.01"])
        w = worker_mod.Worker(spool)
        captured = {}
        self._run_until_launch(w, captured, monkeypatch)
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            assert captured["env"].get(var) == "1", f"{var} not capped in launch env"

    def test_task_spec_env_can_override_thread_cap(self, tmp_path, monkeypatch):
        spool = tmp_path / "spool"
        d = _stage_task(spool, "capo", ["--updates", "1", "--sleep-per-step", "0.01"])
        task = json.loads((d / "task.json").read_text())
        task["env"] = {"OMP_NUM_THREADS": "4"}  # a task that genuinely wants more can override
        (d / "task.json").write_text(json.dumps(task))
        w = worker_mod.Worker(spool)
        captured = {}
        self._run_until_launch(w, captured, monkeypatch)
        assert captured["env"].get("OMP_NUM_THREADS") == "4"       # spec wins over the cap
        assert captured["env"].get("OPENBLAS_NUM_THREADS") == "1"  # the rest stay capped


@pytest.mark.slow
class TestPreemption:
    def test_preempt_waits_for_fresh_checkpoint_then_kills(self, tmp_path):
        spool = tmp_path / "spool"
        _stage_task(spool, "victim", ["--updates", "50", "--sleep-per-step", "0.3", "--ckpt-every", "1"])
        w = worker_mod.Worker(spool)
        # get it running first
        deadline = time.time() + 10
        while time.time() < deadline and (spool / "active" / "victim" / "repo").exists() is False:
            w.tick()
            time.sleep(0.2)
        for _ in range(5):
            w.tick()
            time.sleep(0.2)

        preempt_marker = spool / "active" / "victim" / "PREEMPT"
        preempt_marker.touch()
        deadline = time.time() + 15
        while time.time() < deadline:
            w.tick()
            if (spool / "active" / "victim" / "PREEMPTED").exists():
                break
            time.sleep(0.3)
        assert (spool / "active" / "victim" / "PREEMPTED").exists()
        assert not (spool / "active" / "victim" / "DONE").exists()
        ckpt = spool / "active" / "victim" / "out" / "ckpt_latest.pt"
        assert ckpt.exists()
        assert ckpt.stat().st_mtime > preempt_marker.stat().st_mtime
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        assert events[-1]["detail"] == "preempted"


@pytest.mark.slow
class TestCancellation:
    def test_cancel_kills_immediately_and_writes_cancelled(self, tmp_path):
        # Cancel-in-flight: unlike preempt, cancel does NOT wait for a checkpoint (a cancelled run
        # is discarded) — the CANCEL marker terminates the run and yields a CANCELLED marker.
        spool = tmp_path / "spool"
        _stage_task(spool, "doomed", ["--updates", "500", "--sleep-per-step", "0.3", "--ckpt-every", "999"])
        w = worker_mod.Worker(spool)
        deadline = time.time() + 10
        while time.time() < deadline and not (spool / "active" / "doomed" / "repo").exists():
            w.tick()
            time.sleep(0.2)
        for _ in range(3):
            w.tick()
            time.sleep(0.2)
        (spool / "active" / "doomed" / "CANCEL").touch()  # no checkpoint has been written
        deadline = time.time() + 15
        while time.time() < deadline:
            w.tick()
            if (spool / "active" / "doomed" / "CANCELLED").exists():
                break
            time.sleep(0.3)
        assert (spool / "active" / "doomed" / "CANCELLED").exists()
        assert not (spool / "active" / "doomed" / "DONE").exists()
        events = [json.loads(l) for l in (spool / "worker.jsonl").read_text().splitlines()]
        assert events[-1]["detail"] == "cancelled"

    def test_cancel_before_launch_writes_cancelled_without_running(self, tmp_path):
        spool = tmp_path / "spool"
        _stage_task(spool, "early", ["--updates", "5", "--sleep-per-step", "0.05"])
        w = worker_mod.Worker(spool)
        w.claim_ready()
        w.validate_and_prepare()
        (spool / "active" / "early" / "CANCEL").touch()
        w.check_cancellation()
        assert (spool / "active" / "early" / "CANCELLED").exists()
        assert not (spool / "active" / "early" / "run.log").exists()  # never launched


class TestLaunchGateIsReported:
    """Invariant 19h-4 — the launch gate must SAY why it refused.

    `should_launch` returned `(ok, reason)` and the worker wrote `ok, _reason = ...`, discarding it.
    So a gate-held task produced no evidence at all, and every over-pack incident on 2026-08-02 was
    diagnosed by inference — the box knew and never said. Cheapest, highest-leverage observability
    there is: a signal at the SOURCE."""

    HW = {"load1": 2.0, "cores": 8, "gpu_util": 5.0, "vram_free_gb": 10.0, "vram_total_gb": 12.0}

    def test_every_reason_carries_the_numbers_that_decided_it(self):
        """A bare reason names the branch; the numbers say whether to cap the box or leave the host.
        Anchored the same way every diagnostic here is: measured value AND the threshold."""
        g = worker_mod._gate_detail
        assert "min since last launch <" in g("settling", self.HW, 1, 0.5, None)
        cpu = g("cpu_load", {**self.HW, "load1": 7.5}, 1, 99.0, None)
        assert "7.50" in cpu and "cores-1" in cpu
        gpu = g("gpu_util", {**self.HW, "gpu_util": 97.0}, 1, 99.0, None)
        assert "97" in gpu and "co-tenants" in gpu.lower()

    def test_the_vram_line_names_the_need_AND_that_the_card_is_shared(self):
        """VRAM is not cgroup-isolated, so 'free < need' may mean a co-tenant took the card rather
        than that we are full — a different remedy (move host) from capping the box."""
        d = worker_mod._gate_detail("vram", {**self.HW, "vram_free_gb": 0.4}, 1, 99.0, 0.6)
        assert "0.40" in d and "0.75" in d          # need = 1.25 x 0.6
        assert "co-tenants" in d.lower()

    def test_an_unknown_reason_still_produces_a_line(self):
        """A new `should_launch` branch must not silently vanish from the record."""
        assert "n_live=3" in worker_mod._gate_detail("something_new", self.HW, 3, 99.0, None)

    def test_it_is_EDGE_TRIGGERED_not_per_tick(self, tmp_path):
        """`launch_ready` runs every tick and `worker.jsonl` is rsynced home every ingest pass —
        logging unconditionally would flood the file and the wire."""
        w = worker_mod.Worker.__new__(worker_mod.Worker)
        w.worker_log = tmp_path / "worker.jsonl"
        w._gate_reason = None
        for _ in range(5):
            w._note_gate("t1", "vram: free 0.4GB < need 0.75GB")
        assert w.worker_log.read_text().count("launch_gate") == 1
        w._note_gate("t1", "cpu_load: load1 7.50 >= cores-1 7")   # reason CHANGED -> one more
        assert w.worker_log.read_text().count("launch_gate") == 2

    def test_launching_clears_the_hold_without_logging(self, tmp_path):
        w = worker_mod.Worker.__new__(worker_mod.Worker)
        w.worker_log = tmp_path / "worker.jsonl"
        w._gate_reason = "vram: free 0.4GB < need 0.75GB"
        w._note_gate(None, None)
        assert w._gate_reason is None
        assert not w.worker_log.exists() or "launch_gate" not in w.worker_log.read_text()


class _LiveTrainer:
    pid = 4321

    def poll(self):
        return None


class _StopAfterBuild(Exception):
    """Raised from the first `tick` so `main` can be driven up to, and not into, its loop."""


class TestTheBoxHoldsNoLaneCount:
    """Invariant 8a — how many tasks a box carries is decided by PLACEMENT, and only there.

    The worker used to be started with `--max-slots {slots_total}` and refuse to launch past it.
    That number was a copy taken ONCE: bring-up no-ops on a live worker, and the self-update
    re-execs with the same argv, so it outlived every change the coordinator made to the box. A
    box whose slots were raised kept refusing the extra work, the over-pack reaper read that as a
    capacity ceiling, and the only fix was a command run on the box itself. A copy that agrees with
    placement while fresh can only ever bind when it is wrong — so there is no copy."""

    IDLE = {"gpu_util": None, "vram_free_gb": None, "vram_total_gb": None, "proc_vram_gb": {},
            "load1": 1.0, "cores": 64}

    def _worker(self, tmp_path, monkeypatch, n_live, hw):
        """A worker with `n_live` trainers already running and ONE prepared task waiting, on a box
        measuring `hw`. `should_launch` is the REAL one: only its input is controlled."""
        w = worker_mod.Worker(tmp_path / "spool")
        d = tmp_path / "spool" / "active" / "new"
        (d / "repo").mkdir(parents=True)
        spec = {"task_id": "new", "grp": "g", "name": "new", "argv": ["python", "x.py"],
                "env": {}, "est_minutes": 1, "git_sha": "d", "pip_extras": [], "resume_from": None}
        assert worker_mod.validate_task_json(spec) is None
        w.active = {f"busy{i}": worker_mod.ActiveTask(f"busy{i}", tmp_path / f"busy{i}",
                                                       proc=_LiveTrainer())
                    for i in range(n_live)}
        w.active["new"] = worker_mod.ActiveTask("new", d, spec=spec)
        launched = []
        monkeypatch.setattr(worker_mod.subprocess, "Popen",
                            lambda argv, **kw: launched.append(argv) or _LiveTrainer())
        monkeypatch.setattr(worker_mod, "sample_hw", lambda: dict(hw))
        return w, launched

    def _gate_lines(self, w):
        if not w.worker_log.exists():
            return []
        rows = [json.loads(line) for line in w.worker_log.read_text().splitlines()]
        return [r["detail"] for r in rows if r["event"] == "launch_gate"]

    def test_a_box_far_past_any_old_cap_still_launches_when_it_measures_idle(self, tmp_path,
                                                                           monkeypatch):
        """30 live trainers: more than any box in this fleet is registered for, and far past
        `AUTO_DEFAULTS['max_slots']` (8), which is what `should_launch` falls back to if the worker
        stops overriding it."""
        assert 30 > worker_mod.AUTO_DEFAULTS["max_slots"]
        w, launched = self._worker(tmp_path, monkeypatch, n_live=30, hw=self.IDLE)
        w.launch_ready()
        assert len(launched) == 1 and w.active["new"].proc is not None
        assert self._gate_lines(w) == []

    def test_a_MEASURED_refusal_still_holds_the_launch_and_names_no_count(self, tmp_path,
                                                                         monkeypatch):
        """The control that keeps the test above honest: the gate is still there, it just no longer
        counts. Same box, same 30 trainers, now measurably loaded."""
        w, launched = self._worker(tmp_path, monkeypatch, n_live=30,
                                   hw={**self.IDLE, "load1": 63.5})
        w.launch_ready()
        assert launched == [] and w.active["new"].proc is None
        (line,) = self._gate_lines(w)
        assert line.startswith("cpu_load:")
        assert "max_slots" not in line and "max-slots" not in line

    def test_the_worker_has_nowhere_to_keep_a_lane_count(self, tmp_path):
        import inspect
        assert "max_slots" not in inspect.signature(worker_mod.Worker.__init__).parameters
        assert not hasattr(worker_mod.Worker(tmp_path / "spool"), "max_slots")

    @pytest.mark.parametrize("argv_tail", [[], ["--max-slots", "4"]])
    def test_a_worker_started_before_8a_survives_its_own_re_exec(self, tmp_path, monkeypatch,
                                                                 argv_tail):
        """The self-update re-execs with `sys.argv`, and every worker started before 8a carries
        `--max-slots N`. If the new code REJECTED the flag, that worker would die parsing its own
        arguments on the way in (`_sources_compile` cannot see an argparse error) — and nothing
        relaunches a worker on a rental. So the flag is accepted, and ignored."""
        built = []

        def _tick(self):
            built.append(self)
            raise _StopAfterBuild

        monkeypatch.setattr(worker_mod.Worker, "tick", _tick)
        with pytest.raises(_StopAfterBuild):
            worker_mod.main(["--spool", str(tmp_path / "spool"), *argv_tail])
        assert len(built) == 1 and not hasattr(built[0], "max_slots")


class TestSelfUpdateMustNotRestartUnderRunningWork:
    """★ inv 20i's self-update `os.execv`s the worker when its sources change. It used to do so
    REGARDLESS of occupants, on the reasoning that "trainers are launched with start_new_session, so
    replacing this process does not touch them".

    That premise is FALSE IN THE FIELD. Measured 2026-08-02: six tasks across five campaigns died
    inside 8 minutes on three boxes (`m60_perhead_rep` x2, `m58_craftax_budget`,
    `m65_slice_default_n6`, `wrel_pc_ab2`, `m62_color_cost`), every one with
    `CheckpointRegression: progress would go BACKWARDS`, and with NO `worker_retired` event anywhere —
    so it was this path, not the bootstrap gate fixed in cd93d564. The replacement worker re-adopts an
    `active/<id>` that carries no terminal marker and RE-LAUNCHES the task from scratch; its first
    checkpoint is at stage 0 and the guard refuses it.

    The trigger is any push to master, so a busy day of merges mass-kills in-flight runs.
    """

    def _d(self, cur, loaded, pending, active, compiles=True):
        return worker_mod.self_update_decision(cur, loaded, pending, active, lambda: compiles)

    def test_an_IDLE_box_updates(self):
        assert self._d("new", "old", "new", {}) == "exec"

    def test_a_box_with_RUNNING_WORK_defers(self):
        assert self._d("new", "old", "new", {"t1": object()}) == "defer"

    def test_the_deferral_LIFTS_once_the_box_drains(self):
        """Deferring must cost LATENCY, not the rollout — otherwise a busy box never updates and
        20i silently becomes a no-op again, which is the bootstrap gap it exists to close."""
        assert self._d("new", "old", "new", {"t1": object()}) == "defer"
        assert self._d("new", "old", "new", {}) == "exec"

    def test_an_unchanged_fingerprint_does_nothing(self):
        assert self._d("same", "same", None, {}) == "none"

    def test_a_fingerprint_seen_ONCE_still_settles(self):
        """The four bootstrap files rsync individually, so one sighting can be a set caught
        mid-delivery. This guard predates the occupancy fix and must survive it."""
        assert self._d("new", "old", None, {}) == "settle"

    def test_sources_that_do_not_COMPILE_are_never_exec_into(self):
        assert self._d("new", "old", "new", {}, compiles=False) == "settle"


class TestSelfResumeAfterWorkerRestart:
    """A worker restart must relaunch a task FROM ITS OWN CHECKPOINT, not from scratch.

    REGRESSION (m58 `budget50x_s0`, 2026-08-02): a 15h craftax run died at 5h37m with
    `CheckpointRegression: refusing to write ../out/ckpt_latest.pt: progress would go BACKWARDS —
    step 173277 -> 2418`. The task events showed one `start` and then `task_failed` — no preempt, no
    requeue — i.e. the WORKER relaunched the process in place. `resume_from` is set only by the
    coordinator's requeue path, so the relaunch carried no `--init-from` and began at step 0 while a
    fully resumable checkpoint (seed_index 0, sched stage_done 173277) sat in the task's own `out/`.
    Only the regression guard stopped it from clobbering 5.5M transitions of training.
    """

    class _Dummy:
        pid = 4321

        def poll(self):
            return None

    def _prepare_then_launch(self, w, monkeypatch, plant_checkpoint: bool):
        """Tick to PREPARE the task (launch gated off), optionally plant a prior lifetime's
        checkpoint, then open the gate and capture the relaunch argv."""
        captured = {}
        real_popen = worker_mod.subprocess.Popen

        def _fake_popen(*args, **kwargs):
            if str(kwargs.get("cwd") or "").endswith("repo"):
                captured["argv"] = list(args[0])
                return self._Dummy()
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(worker_mod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(worker_mod, "should_launch", lambda *a, **k: (False, "held"))
        for _ in range(4):
            w.tick()
        out = w.active_dir / "selfres" / "out"
        assert out.exists(), "validate_and_prepare should have created out/"
        if plant_checkpoint:
            (out / worker_mod.SELF_RESUME_CHECKPOINT).write_bytes(b"prior-lifetime")
        monkeypatch.setattr(worker_mod, "should_launch", lambda *a, **k: (True, "test"))
        for _ in range(4):
            w.tick()
            if "argv" in captured:
                break
        assert "argv" in captured, "task never launched"
        return captured["argv"]

    @pytest.mark.slow
    def test_relaunch_passes_init_from_when_its_own_checkpoint_exists(self, tmp_path, monkeypatch):
        spool = tmp_path / "spool"
        _stage_task(spool, "selfres", ["--updates", "1", "--sleep-per-step", "0.01"])
        w = worker_mod.Worker(spool)
        argv = self._prepare_then_launch(w, monkeypatch, plant_checkpoint=True)
        assert "--init-from" in argv, f"relaunch did not self-resume: {argv}"
        assert argv[argv.index("--init-from") + 1] == f"../out/{worker_mod.SELF_RESUME_CHECKPOINT}"

    @pytest.mark.slow
    def test_first_launch_without_a_checkpoint_does_NOT_pass_init_from(self, tmp_path, monkeypatch):
        """The negative half — without it the test above passes vacuously on a worker that always
        appends the flag, which would point every FRESH task at a nonexistent file."""
        spool = tmp_path / "spool"
        _stage_task(spool, "selfres", ["--updates", "1", "--sleep-per-step", "0.01"])
        w = worker_mod.Worker(spool)
        argv = self._prepare_then_launch(w, monkeypatch, plant_checkpoint=False)
        assert "--init-from" not in argv, f"fresh task should not resume: {argv}"


class TestNoDoubleRunAfterWorkerRestart:
    """★ A worker restart must RE-ADOPT a surviving trainer — never relaunch it, never lose it.

    Trainers are started with `start_new_session`, so they SURVIVE the worker's `os.execv` — that is
    invariant 20i's founding premise. `os.execv` also KEEPS THE PID, so the restarted image is still
    their PARENT and can still `waitpid` them (PROVEN 2026-08-02: child PPid still equals our pid,
    waitpid returns the true exit code). Re-adoption is therefore possible, and it is what makes a
    worker upgrade a no-op for running work.

    Two failure modes it prevents, both measured or found this repo:
    1. DOUBLE-RUN — re-adopting with `proc=None` let `launch_ready` start a SECOND trainer on one
       `out/`. Survivable by ACCIDENT until 2026-08-02: the second used to start COLD, so the
       checkpoint guard refused its stage-0 write and killed the interloper loudly. Once `191c8fb9`
       made an in-place relaunch self-resume, it stopped tripping the guard and became a silent race.
    2. NO TERMINAL MARKER — the WORKER writes DONE/FAILED_<rc> in `check_exits`, which returns early
       on `proc is None`. So merely SKIPPING a surviving trainer strands its task in `running` with
       no marker until a stall reaper catches it. Adoption is what closes this; skipping does not.
    """

    def _restarted_worker(self, spool, monkeypatch, tid, child):
        """Simulate a worker RESTART over a task whose trainer is still alive.

        `child` is a REAL process (not a fake pid): `_leader_pid` reads /proc for PPid and pgid, so a
        synthetic pid would simply fail to adopt and the test would pass for the wrong reason."""
        d = _stage_task(spool, tid, ["--updates", "1", "--sleep-per-step", "0.01"])
        active = spool / "active"
        active.mkdir(parents=True, exist_ok=True)
        d.rename(active / tid)                     # the prior lifetime's `claim_ready` did this
        tdir = active / tid
        # ...and its `validate_and_prepare` had already extracted repo/ + made out/. Load-bearing:
        # with no repo/, `launch_ready`'s pending filter excludes the task for THAT reason and the
        # guard under test is never reached — a mutation test caught exactly that false pass.
        (tdir / "repo").mkdir(exist_ok=True)
        with tarfile.open(tdir / "payload.tar.gz") as tf:
            tf.extractall(tdir / "repo")
        (tdir / "out").mkdir(exist_ok=True)
        procs = [{"pid": child.pid, "task_id": tid, "cwd": str(tdir / "repo")}] if child else []
        monkeypatch.setattr(worker_mod.reap_orphans, "find_live_task_procs", lambda _s: list(procs))
        return worker_mod.Worker(spool)

    def _launches(self, w, monkeypatch):
        launched = []
        real_popen = worker_mod.subprocess.Popen

        def _fake_popen(*args, **kwargs):
            if str(kwargs.get("cwd") or "").endswith("repo"):
                launched.append(list(args[0]))

                class _D:
                    pid = 4321

                    def poll(self):
                        return None
                return _D()
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(worker_mod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(worker_mod, "should_launch", lambda *a, **k: (True, "test"))
        for _ in range(6):
            w.tick()
        return launched

    @pytest.mark.slow
    def test_a_surviving_trainer_is_RE_ADOPTED_not_relaunched(self, tmp_path, monkeypatch):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  start_new_session=True)
        try:
            w = self._restarted_worker(tmp_path / "spool", monkeypatch, "dbl", child)
            at = w.active["dbl"]
            assert at.proc is not None, "a surviving trainer must be RE-ADOPTED"
            assert at.proc.pid == child.pid
            assert self._launches(w, monkeypatch) == [], "must not launch a SECOND trainer"
        finally:
            child.kill(); child.wait()

    @pytest.mark.slow
    def test_a_re_adopted_trainer_STILL_GETS_ITS_TERMINAL_MARKER(self, tmp_path, monkeypatch):
        """★ The gap that adoption exists to close. The WORKER writes the marker, so a trainer the
        worker cannot poll finishes into silence and the task strands in `running`. Exit code 7 is
        asserted specifically: it proves the re-adopted handle recovered the REAL status via
        `waitpid`, not a guess — which is only possible because execv preserved the PID."""
        child = subprocess.Popen([sys.executable, "-c", "import sys,time; time.sleep(0.5); sys.exit(7)"],
                                  start_new_session=True)
        try:
            w = self._restarted_worker(tmp_path / "spool", monkeypatch, "dbl", child)
            assert w.active["dbl"].proc is not None
            deadline = time.time() + 20
            while time.time() < deadline and "dbl" in w.active:
                w.check_exits()
                time.sleep(0.2)
            assert not (tmp_path / "spool" / "active" / "dbl" / "DONE").exists()
            assert (tmp_path / "spool" / "active" / "dbl" / "FAILED_7").exists(), \
                "a re-adopted trainer's exit must still produce a terminal marker, with its REAL rc"
        finally:
            if child.poll() is None:
                child.kill(); child.wait()

    @pytest.mark.slow
    def test_a_task_whose_trainer_DIED_is_still_relaunched(self, tmp_path, monkeypatch):
        """The negative half. Without it the tests above pass vacuously on a worker that never
        relaunches anything — which would strand every task a real crash interrupted."""
        w = self._restarted_worker(tmp_path / "spool", monkeypatch, "dbl", None)
        assert w.active["dbl"].proc is None, "no live trainer => nothing to re-adopt"
        assert self._launches(w, monkeypatch), "a task with no live trainer MUST be relaunched"

    @pytest.mark.slow
    def test_a_re_adopted_trainer_OCCUPIES_a_slot(self, tmp_path, monkeypatch):
        """It burns the same CPU/VRAM whether or not we hold a real Popen. Counting only our own
        procs would under-report the box after a restart: `should_launch` reads zero live lanes as
        "first lane" and launches past its settle and load checks."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  start_new_session=True)
        try:
            w = self._restarted_worker(tmp_path / "spool", monkeypatch, "dbl", child)
            n_live = sum(1 for at in w.active.values()
                         if at.proc is not None and at.proc.poll() is None)
            assert n_live == 1, "a re-adopted trainer must count against the live-slot total"
        finally:
            child.kill(); child.wait()

    def test_an_unreadable_proc_table_does_not_wedge_the_worker(self, tmp_path, monkeypatch):
        """Fail-safe: if liveness cannot be determined we fall back to the old behaviour rather than
        refusing to boot. A worker that will not start is worse than one that may double-run, and
        the checkpoint guard remains the backstop for the cold-restart case."""
        spool = tmp_path / "spool"
        d = _stage_task(spool, "boom", ["--updates", "1"])
        (spool / "active").mkdir(parents=True, exist_ok=True)
        d.rename(spool / "active" / "boom")

        def _raise(_spool):
            raise OSError("/proc unreadable")

        monkeypatch.setattr(worker_mod.reap_orphans, "find_live_task_procs", _raise)
        w = worker_mod.Worker(spool)
        assert "boom" in w.active and w.active["boom"].proc is None


class TestSelfUpdateUnderRunningWork:
    """Invariant 20j-3: a re-exec under live work is allowed ONLY when re-adoption is verifiable."""

    def test_it_EXECS_under_running_work_when_every_trainer_is_reattachable(self):
        """This is the whole point — otherwise a permanently-busy box NEVER updates."""
        assert worker_mod.self_update_decision(
            "new", "old", "new", {"t": object()}, lambda: True, lambda: True) == "exec"

    def test_it_DEFERS_when_a_live_trainer_cannot_be_SEEN(self):
        """An invisible trainer would be invisible to the restarted image too, so the task would
        come back double-run or stranded. Unverifiable ⇒ defer, which costs only latency."""
        assert worker_mod.self_update_decision(
            "new", "old", "new", {"t": object()}, lambda: True, lambda: False) == "defer"

    def test_an_IDLE_box_still_execs_regardless(self):
        assert worker_mod.self_update_decision(
            "new", "old", "new", {}, lambda: True, lambda: False) == "exec"

    def test_sources_that_do_not_compile_are_never_exec_into(self):
        """Pre-existing guard that must survive 20j-3: nothing relaunches a worker that dies."""
        assert worker_mod.self_update_decision(
            "new", "old", "new", {}, lambda: False, lambda: True) == "settle"


class TestCanReattachAll:
    """The 20j-3 precondition itself: could a restart of THIS process find every live trainer again?

    It gates whether the worker may re-exec under running work, so a version that always answers
    "yes" would hand back exactly the double-run/stranding this invariant exists to prevent.
    """

    def _worker_with_live_task(self, tmp_path, monkeypatch, *, visible):
        spool = tmp_path / "spool"
        (spool / "active").mkdir(parents=True)
        monkeypatch.setattr(worker_mod.reap_orphans, "find_live_task_procs", lambda _s: [])
        w = worker_mod.Worker(spool)

        class _LiveProc:
            pid = 12345

            def poll(self):
                return None

        at = worker_mod.ActiveTask("t1", spool / "active" / "t1")
        at.proc = _LiveProc()
        w.active["t1"] = at
        found = [{"pid": 12345, "task_id": "t1", "cwd": "x"}] if visible else []
        monkeypatch.setattr(worker_mod.reap_orphans, "find_live_task_procs", lambda _s: list(found))
        return w

    def test_a_live_trainer_that_proc_CANNOT_SEE_blocks_the_re_exec(self, tmp_path, monkeypatch):
        """★ Kills the always-True mutant. If /proc does not show a process under this task's dir,
        the restarted image has no way to find it either — so re-exec'ing would double-run or strand
        it. The honest answer is 'no', and the cost of being wrong here is a lost run."""
        w = self._worker_with_live_task(tmp_path, monkeypatch, visible=False)
        assert w.can_reattach_all() is False

    def test_a_visible_live_trainer_permits_the_re_exec(self, tmp_path, monkeypatch):
        """The negative half — otherwise 'always defer' would pass, and a busy box never updates."""
        w = self._worker_with_live_task(tmp_path, monkeypatch, visible=True)
        assert w.can_reattach_all() is True

    def test_an_unreadable_proc_table_blocks_the_re_exec(self, tmp_path, monkeypatch):
        """Unverifiable ⇒ defer. Any error reading /proc must fail toward the conservative side."""
        w = self._worker_with_live_task(tmp_path, monkeypatch, visible=True)

        def _raise(_s):
            raise OSError("nope")

        monkeypatch.setattr(worker_mod.reap_orphans, "find_live_task_procs", _raise)
        assert w.can_reattach_all() is False



# --------------------------------------------------------------------------------------------
# Spool GC (task-dispatcher inv. 29b/29c) — the HAPPY PATH leaked the box's disk.
#
# A task that completed NORMALLY left its whole `active/<id>` behind forever: every `rm -rf` in the
# dispatcher sat on an exceptional path, so the leak scaled with SUCCESS. Measured 2026-08-12 after
# the tower hit 100% disk: laptop 248G/1375 dirs, desktop 47G/169, tower 69G/291 —
# and 1083 of the laptop's 1375 markers were `DONE`. This sweep is the self-healing half: it fires
# without the coordinator, so it also covers a box that was unreachable when its task finished.
# --------------------------------------------------------------------------------------------
class TestPruneFinished:
    def _finished(self, spool: Path, tid: str, marker: str, age_h: float) -> Path:
        d = spool / "active" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "repo").mkdir(exist_ok=True)
        (d / "repo" / "big.bin").write_bytes(b"x" * 1024)
        m = d / marker
        m.write_text("")
        old = time.time() - age_h * 3600
        os.utime(m, (old, old))
        return d

    def test_an_old_finished_dir_is_swept(self, tmp_path):
        w = worker_mod.Worker(tmp_path / "spool")
        d = self._finished(tmp_path / "spool", "OLD", "DONE", 24)
        w._prune_finished(time.time())
        assert not d.exists(), "a DONE dir older than the retention window must be swept"

    def test_a_recent_finished_dir_is_KEPT(self, tmp_path):
        """The forensics grace period — the whole point of the window being non-zero."""
        w = worker_mod.Worker(tmp_path / "spool")
        d = self._finished(tmp_path / "spool", "FRESH", "DONE", 1)
        w._prune_finished(time.time())
        assert d.exists(), "a just-finished dir must survive so a human can still read its run.log"

    @pytest.mark.parametrize("marker", ["DONE", "PREEMPTED", "CANCELLED", "FAILED_1"])
    def test_every_terminal_marker_is_swept(self, tmp_path, marker):
        w = worker_mod.Worker(tmp_path / "spool")
        d = self._finished(tmp_path / "spool", f"T_{marker}", marker, 24)
        w._prune_finished(time.time())
        assert not d.exists(), f"{marker} is terminal and must be collected"

    def test_a_RUNNING_dir_is_NEVER_swept_however_old(self, tmp_path):
        """⛔ THE ONE THAT MATTERS. The gate is the MARKER, never age — a long trainer is
        indistinguishable from an abandoned dir by mtime, so an age-only rule deletes live work."""
        w = worker_mod.Worker(tmp_path / "spool")
        d = (tmp_path / "spool" / "active" / "RUNNING")
        (d / "repo").mkdir(parents=True, exist_ok=True)
        old = time.time() - 30 * 86400          # a month old, and still running
        os.utime(d, (old, old))
        w._prune_finished(time.time())
        assert d.exists(), "no terminal marker => the task is live => must never be swept"

    def test_the_sweep_is_throttled_like_the_blob_prune(self, tmp_path):
        w = worker_mod.Worker(tmp_path / "spool")
        d = self._finished(tmp_path / "spool", "THROTTLED", "DONE", 24)
        w._last_reap = time.time()              # just reaped => inside the throttle
        w._prune_finished(time.time())
        assert d.exists(), "must respect REAP_EVERY_SECONDS like _prune_blobs"


class TestBundleTarDroppedAfterUnpack:
    """Inv. 29c: ~33 MB of a measured ~112 MB task dir is a tarball already extracted beside it."""

    def test_bundle_tar_is_gone_after_a_successful_claim(self, tmp_path):
        spool = tmp_path / "spool"
        tid = "bundlegc-0000"
        _stage_task(spool, tid, ["--seconds", "0"])
        w = worker_mod.Worker(spool)
        w.tick()
        at = spool / "active" / tid
        if not (at / "repo").exists():
            pytest.skip("staging used the legacy loose-file path; 29c covers bundle.tar only")
        assert not (at / "bundle.tar").exists(), "an unpacked bundle.tar is dead weight"
