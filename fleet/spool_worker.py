"""Spool worker — box-resident process (docs/specs/task-dispatcher.spec.md invariants 8, 17).

Started on the box at rent time. Claims shipped tasks (`incoming/<id>` -> `active/<id>`),
extracts the payload, launches each one as the sweep supervisor's hardware-adaptive
`should_launch` allows (reused verbatim, not reimplemented), and marks completion with
`DONE`/`FAILED_<rc>`/`PREEMPTED` — never deletes a task dir, never executes unvalidated `argv`.

The box holds NO lane count (invariant 8a): how many tasks it carries is the coordinator's
placement decision, and the only things that hold a launch here are ones the box can measure.

    python3 spool_worker.py --spool ~/spool
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bundle  # noqa: E402  (task-bundle spec: verify + unpack the delivered bundle)
import reap_orphans  # noqa: E402  (fleet hygiene: kill runaway orphan trainers + reap zombies)
import sweep_supervisor as _sup  # noqa: E402  (the three rule defaults below, read defensively)
from sweep_supervisor import AUTO_DEFAULTS, sample_hw, should_launch  # noqa: E402  (invariant 8)

POLL_SECONDS = 2
HEARTBEAT_SECONDS = 60
KILL_GRACE_SECONDS = 30
REAP_EVERY_SECONDS = 30  # how often to sweep for orphaned/zombie trainers (fleet hygiene)
# inv. 11: a shared code blob is reused by every task at that snapshot, so it is kept on LRU rather
# than refcounted — `_unpack` touches the one it reads. A rented box is destroyed inside
# `hard_cap_hours` so this never matters there; an OWNED box lives for months and would otherwise
# accumulate a ~36 MB tree per distinct snapshot forever.
BLOB_MAX_AGE_SECONDS = 3 * 86400
# Invariant 29b: how long a FINISHED `active/<id>` is kept on the box before the worker sweeps it.
# Its results are home the moment the dispatcher completes the task, so this window buys exactly one
# thing — the ability to ssh in and read a fresh failure's `run.log`/`out/` by hand. 12h covers "look
# at it the next morning" while bounding the box's floor at ~a day of throughput. NOT a correctness
# knob: dropping it to 0 would still be safe, just hostile to forensics.
FINISHED_MAX_AGE_SECONDS = 12 * 3600
TERMINAL_MARKERS = ("DONE", "PREEMPTED", "CANCELLED")  # plus FAILED_<rc>, matched by prefix

# ---- self-update (task-dispatcher inv. 20i) ------------------------------------------------
# The dispatcher rsyncs these four files on every bring-up, so the code ON DISK is always current —
# but the RUNNING process was whatever was on disk when it started, and nothing ever restarted it.
# An owned box therefore ran months-old worker code, and a rental only picked up changes when it was
# replaced. Rather than have the coordinator kill and relaunch a worker (which needs a safe-point it
# cannot see from outside, and leaves nothing to restart a rental's worker if the new code is bad),
# the worker re-execs ITSELF at a loop boundary — where no unpack is in flight by construction.
SOURCE_FILES = ("spool_worker.py", "bundle.py", "sweep_supervisor.py", "reap_orphans.py")


def _source_fingerprint() -> str:
    """sha256 over the worker's own sources, so a re-exec fires exactly when the code the dispatcher
    delivered differs from the code this process is running. A file that cannot be read contributes
    a sentinel rather than raising — a fingerprint is a comparison, never a health check."""
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for name in SOURCE_FILES:
        try:
            h.update((here / name).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def _sources_compile() -> bool:
    """Would the new code even start? NOTHING relaunches a worker that dies on a rental (the box is
    reaped hours later by `heartbeat_stale_min`, requeueing its tasks), so a half-written rsync or a
    syntax error must never be exec'd into. Compiling is cheap and catches exactly that class."""
    here = Path(__file__).resolve().parent
    for name in SOURCE_FILES:
        p = here / name
        try:
            compile(p.read_bytes(), str(p), "exec")
        except (OSError, SyntaxError, ValueError):
            return False
    return True
REQUIRED_TASK_KEYS = frozenset({
    "task_id", "grp", "name", "argv", "env", "est_minutes", "git_sha", "pip_extras", "resume_from",
})

# The checkpoint a task writes into its OWN `out/`, used to self-resume when this worker relaunches it
# in place (see `_launch`). Mirrors `job_manifest.DEFAULT_RESUME_CHECKPOINT`; duplicated rather than
# imported because the worker ships to the box as a standalone script alongside `bundle`/`reap_orphans`.
SELF_RESUME_CHECKPOINT = "ckpt_latest.pt"

# Dispatcher invariant 4i-7: `task.json["env"]` carries this as "1" on a FORCED task. Mirrors
# `dispatcher.FORCE_BOX_ENV`; duplicated for the same standalone-script reason as the constant above.
FORCE_BOX_ENV = "RUNQ_FORCE_BOX"

# Dispatcher invariant 8c: `task.json["env"]` carries this as "1" on a task the COORDINATOR has
# decided does not use the GPU. Mirrors `dispatcher.NO_GPU_ENV`. The box holds no opinion on which
# tasks use the card; it only skips its two whole-card launch rules for one so marked.
NO_GPU_ENV = "RUNQ_NO_GPU"

# Invariant 8a: the value handed to `should_launch` as its `max_slots`, so that arm can never fire
# here. `AUTO_DEFAULTS["max_slots"]` (8) belongs to the pre-dispatcher manual sweep lane; a worker
# that let it through capped every box at 8 whatever the coordinator had placed on it.
NO_LANE_COUNT = float("inf")

# ---- launch pacing (invariant 8b) ------------------------------------------------------------
# The thresholds `should_launch` paces by belong to the COORDINATOR (its `launch_gate` setting).
# It writes them into the spool on every measure probe; that file is this box's CACHED COPY, so the
# pacing survives a worker restart and an unreachable coordinator. Until a first copy arrives the
# built-in defaults below apply — a test holds them equal to the coordinator's, so a box before its
# first probe behaves exactly like one after it.
LAUNCH_GATE_FILE = "launch_gate.json"
# key -> (built-in default, lowest value accepted, highest value accepted). The three rule defaults
# are read with `getattr` so this worker still starts beside a `sweep_supervisor.py` that predates
# them — the four source files are delivered one by one.
LAUNCH_GATE_KEYS = {
    "settle_minutes": (AUTO_DEFAULTS["settle_minutes"], 0.0, 240.0),
    "settle_floor_min": (AUTO_DEFAULTS.get("settle_floor_min", 0.5), 0.0, 240.0),
    "settle_idle_frac": (AUTO_DEFAULTS.get("settle_idle_frac", 0.5), 0.0, 1.0),
    "util_ceiling": (AUTO_DEFAULTS["util_ceiling"], 0.0, 100.0),
    "cpu_reserve_cores": (getattr(_sup, "CPU_RESERVE_CORES", 1.0), 0.0, 4096.0),
    "vram_lane_mult": (getattr(_sup, "VRAM_LANE_MULT", 1.25), 0.0, 100.0),
    "vram_free_frac": (getattr(_sup, "VRAM_FREE_FRAC", 0.2), 0.0, 1.0),
}


def launch_gate_defaults() -> dict:
    return {k: float(v[0]) for k, v in LAUNCH_GATE_KEYS.items()}


def parse_launch_gate(text) -> tuple[dict, list]:
    """(thresholds in force, what was refused) for the pushed file's TEXT — None means no file.

    TRUST BOUNDARY: the file comes over the wire and sits in a directory every task on this box can
    write. Each key must be a finite number inside its range; one that is missing or is not falls
    back to ITS built-in default alone, so one bad value cannot disable the whole gate. Unknown keys
    are ignored rather than refused: a coordinator newer than this worker may send one, and a worker
    that rejected the file would silently pace by stale numbers."""
    cfg, problems = launch_gate_defaults(), []
    if text is None:
        return cfg, problems
    try:
        doc = json.loads(text)
    except ValueError:
        return cfg, ["not valid JSON"]
    if not isinstance(doc, dict):
        return cfg, ["not a JSON object"]
    for key, (_default, lo, hi) in LAUNCH_GATE_KEYS.items():
        if key not in doc:
            continue
        v = doc[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
            problems.append(f"{key}={v!r} is not a number in [{lo:g}, {hi:g}]")
            continue
        cfg[key] = float(v)
    return cfg, problems


def _gate_detail(reason: str, hw: dict, n_live: int, since_min: float,
                 vram_lane_max, cfg: dict | None = None) -> str:
    """Turn `should_launch`'s bare reason into a line carrying THE NUMBERS THAT DECIDED IT
    (invariant 19h-4).

    "vram" tells you which branch fired; "vram: free 0.4GB < need 0.75GB" tells you whether the box
    is genuinely full or a co-tenant took the card — and that is the difference between capping the
    box and moving off the host. Anchored the same way every diagnostic in this repo is: the
    measured value against the threshold it was compared to, never one without the other.

    `cfg` is the launch pacing IN FORCE (inv. 8b), so the line quotes the threshold the decision
    actually used rather than a constant it may no longer be."""
    cfg = cfg or launch_gate_defaults()
    f = lambda v: "?" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
    if reason == "settling":
        return (f"settling: {since_min:.1f}min since last launch < "
                f"{cfg['settle_minutes']}min")
    if reason == "cpu_load":
        reserve = cfg["cpu_reserve_cores"]
        return (f"cpu_load: load1 {f(hw.get('load1'))} >= cores-{reserve:g} "
                f"{hw.get('cores', 0) - reserve:g}")
    if reason == "gpu_util":
        return (f"gpu_util: {f(hw.get('gpu_util'))}% >= ceiling "
                f"{cfg['util_ceiling']}% (WHOLE CARD — includes co-tenants)")
    if reason == "vram":
        need = (cfg["vram_lane_mult"] * vram_lane_max if vram_lane_max
                else cfg["vram_free_frac"] * (hw.get("vram_total_gb") or 0))
        return (f"vram: free {f(hw.get('vram_free_gb'))}GB < need {need:.2f}GB "
                f"of {f(hw.get('vram_total_gb'))}GB total (WHOLE CARD — includes co-tenants)")
    return f"{reason}: n_live={n_live}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_task_json(data) -> str | None:
    """Trust boundary (Input contract): unknown/missing keys -> reject, never execute
    unvalidated argv. Returns an error string, or None if valid."""
    if not isinstance(data, dict):
        return "not a JSON object"
    missing = REQUIRED_TASK_KEYS - set(data)
    if missing:
        return f"missing keys: {sorted(missing)}"
    extra = set(data) - REQUIRED_TASK_KEYS
    if extra:
        return f"unknown keys: {sorted(extra)}"
    if not isinstance(data["argv"], list) or not data["argv"] or not all(isinstance(a, str) for a in data["argv"]):
        return "argv must be a non-empty list of strings"
    if not isinstance(data["env"], dict):
        return "env must be an object"
    return None


class ReattachedProc:
    """A trainer THIS worker launched in a prior lifetime, re-adopted after a restart.

    Quacks like the `subprocess.Popen` the rest of `Worker` expects (`pid`, `poll`, `terminate`,
    `kill`), so every caller — `check_exits`, `check_preemption`, `apply_freeze` — works unchanged.

    ⛔ WHY THIS EXISTS: it is what lets a box UPGRADE ITS WORKER WITHOUT TOUCHING ITS TASKS.
    `os.execv` replaces the process IMAGE but keeps the PID, so a re-exec'd worker is **still the
    parent** of the trainers it launched — it has merely lost the `Popen` objects that were in
    memory. PROVEN, not assumed (2026-08-02): after `execv`, the child's `PPid` still equals our pid
    and `waitpid` returns its TRUE exit code. Re-adopting therefore makes a self-update a genuine
    no-op for running work: nothing is killed, nothing is relaunched, and the exit is still reported
    with full fidelity.

    Two restart flavours, and `poll()` handles both:
    - **`execv` self-update** — we ARE the parent. `waitpid` gives the real exit code; identical
      behaviour to never having restarted.
    - **A genuinely NEW process** (worker crash, OOM-kill, box reboot, supervisor respawn) — the
      trainer was reparented to init, so `waitpid` raises `ChildProcessError` and the exit code is
      unrecoverable by anyone. We fall back to `/proc` liveness and, on disappearance, raise `DONE`
      and let the COORDINATOR adjudicate: `_complete_done` gates on the completion artifact's
      presence and explicitly treats that — not an exit code — as "the real proof of completion",
      failing the task `artifact_missing` if it is absent. So the uncertainty is resolved by the
      component that can actually see the evidence, instead of being guessed at here.
    """

    def __init__(self, pid: int, *, is_child: bool):
        self.pid = pid
        self._is_child = is_child
        self._rc: int | None = None

    def poll(self) -> int | None:
        if self._rc is not None:
            return self._rc
        if self._is_child:
            try:
                got, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._is_child = False          # reaped elsewhere; fall through to liveness
            except OSError:
                self._is_child = False
            else:
                if got == 0:
                    return None
                self._rc = os.waitstatus_to_exitcode(status)
                return self._rc
        if _pid_alive(self.pid):
            return None
        # Exit code unrecoverable (not our child). Report success and let the coordinator's
        # completion-artifact check decide — it is the authority, and it fails closed.
        self._rc = 0
        return self._rc

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def _is_our_child(pid: int, my_pid: int) -> bool:
    """True iff `pid`'s parent is us — i.e. we `execv`'d and kept the PID, so `waitpid` still works.
    False after a genuinely new process (crash/OOM/reboot), where the trainer was reparented."""
    try:
        with open(f"/proc/{pid}/status") as f:
            return next(int(l.split()[1]) for l in f if l.startswith("PPid:")) == my_pid
    except (OSError, StopIteration, ValueError):
        return False


def _leader_pid(procs: list, my_pid: int) -> int | None:
    """Pick the TRAINER among the processes running under one task dir.

    A trainer forks env-worker children that share its cwd, so `find_live_task_procs` returns
    several pids per task and adopting the wrong one would report the wrong exit. Two discriminators,
    strongest first: our own CHILD is by definition the process we launched (the `execv` case, which
    is the one that matters); otherwise the session leader (`pid == pgid`), which is what
    `start_new_session=True` made the trainer when we launched it."""
    for p in procs:
        try:
            with open(f"/proc/{p['pid']}/status") as f:
                ppid = next(int(l.split()[1]) for l in f if l.startswith("PPid:"))
        except (OSError, StopIteration, ValueError):
            continue
        if ppid == my_pid:
            return p["pid"]
    for p in procs:
        try:
            if os.getpgid(p["pid"]) == p["pid"]:
                return p["pid"]
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return None


@dataclass
class ActiveTask:
    task_id: str
    dir: Path
    proc: subprocess.Popen | None = None
    spec: dict | None = None
    preempting: bool = False
    cancelling: bool = False
    started_at: float = 0.0
    stopped: bool = False  # box-pause spec: SIGSTOP-frozen (owner paused the box); resumes on SIGCONT


class Worker:
    def __init__(self, spool: Path, public_key: str | None = None):
        self.spool = spool
        # inv. 11: the code tree is delivered ONCE per box and referenced by every task that shares
        # it, instead of being re-sent inside each task's bundle. Creating the dir + advertising the
        # capability is how the dispatcher knows this box can be sent the reference form; a box
        # still running the old worker has neither, and keeps getting self-contained v1 bundles.
        self.blob_dir = spool / bundle.BLOBS_DIRNAME
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        # `reattach` (inv 20j-3): this worker RE-ADOPTS surviving trainers across its own restart
        # instead of relaunching them, so delivering new worker code to this box is safe even while
        # it is running work — the coordinator's 20j-1 delivery gate consults this to decide whether
        # it must hold. A box without the token gets the conservative hold, which is exactly the
        # pre-20j-3 behaviour.
        (spool / "CAPS").write_text("blobref\nreattach\n")
        # If a public key was provisioned, every bundle MUST carry a valid signature (fail-closed —
        # task-bundle spec invariant 4). Without one, integrity (sha256) is still enforced.
        self.public_key = public_key
        self.require_signature = public_key is not None
        self.incoming = spool / "incoming"
        self.active_dir = spool / "active"
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.worker_log = spool / "worker.jsonl"
        self.active: dict[str, ActiveTask] = {}
        self._last_heartbeat = 0.0
        self._last_launch = 0.0
        # inv. 19h-4: last reported launch-gate reason, so `_note_gate` is edge-triggered.
        self._gate_reason = None
        # inv. 8b: the launch pacing in force, and the (mtime, size) of the pushed copy it was read
        # from — "unread" until the first look, so the first read always reports what is in force.
        self._gate_cfg = launch_gate_defaults()
        self._gate_cfg_seen = "unread"
        self._last_reap = 0.0
        self._launched_pids: set[int] = set()  # every pid we've Popen'd this lifetime (for zombie reap)
        self._vram_lane_max = None
        self._reattach_after_restart()

    def _reattach_after_restart(self) -> None:
        """Restart safety: any active/<id> dir without a terminal marker gets a fresh launch
        decision — UNLESS its trainer is still running, in which case we must not launch a second one.

        ⛔ THE DOUBLE-RUN (invariant 20j / R9). Trainers are launched with `start_new_session`, so
        they SURVIVE this worker's `os.execv` — that is 20i's founding premise. `_launched_pids` is
        in-memory and does not. So a naive re-adopt (`proc=None` → `launch_ready` starts it again)
        puts TWO trainers on one `out/` directory, both writing `ckpt_latest.pt`.

        ⚠ This was survivable by ACCIDENT until 2026-08-02, and the accident is now gone. The second
        trainer used to start COLD, so `shared.infra.checkpoint` refused its stage-0 write with
        `CheckpointRegression` — killing the interloper LOUDLY and leaving the original running. The
        guard was doing two jobs and only one was known. Once `191c8fb9` made an in-place relaunch
        self-resume from `out/ckpt_latest.pt`, the second trainer no longer writes backwards, no
        longer trips the guard, and simply runs alongside the first — a silent race in place of a
        loud crash. That is the m49/m50/m54 silent-restart class, which is exactly what this repo's
        checkpoint guard exists to make impossible.

        The pid is unrecoverable across `execv`, but the QUESTION is answerable without it:
        `reap_orphans` already maps a live pid to its `active/<id>` by reading `/proc/<pid>/cwd`.
        Reusing that is deliberate — it is the same fact ("which trainer belongs to which task dir"),
        and a second implementation of it would be free to disagree with the reaper.

        A task whose trainer is still alive is adopted as ALREADY RUNNING (`at.spec` left None so
        `validate_and_prepare` skips it, `at.proc` left None so `launch_ready`'s `pending` filter
        skips it too — `check_exits` has no pid to poll, so the task simply completes via its
        terminal marker, which is the same path a worker restart already relied on). It is NOT
        relaunched, and it is NOT killed: it is doing the work, and the whole point is to leave it
        alone."""
        by_task: dict[str, list] = {}
        try:
            for o in reap_orphans.find_live_task_procs(self.spool):
                by_task.setdefault(o["task_id"], []).append(o)
        except Exception:                      # noqa: BLE001 — /proc unreadable must not wedge boot
            by_task = {}
        me = os.getpid()
        for d in sorted(self.active_dir.iterdir()):
            if not d.is_dir():
                continue
            terminal = ((d / "DONE").exists() or (d / "PREEMPTED").exists()
                        or (d / "CANCELLED").exists() or list(d.glob("FAILED_*")))
            if terminal:
                continue
            at = ActiveTask(d.name, d)
            pid = _leader_pid(by_task.get(d.name, []), me)
            if pid is not None:
                is_child = _is_our_child(pid, me)
                at.proc = ReattachedProc(pid, is_child=is_child)
                at.started_at = time.time()
                self.log(d.name, "reattach",
                          detail=f"trainer pid {pid} survived the worker restart — RE-ADOPTED, not "
                                 f"relaunched ({'still our child: exit code recoverable' if is_child else 'orphaned: completion decided by the artifact'})")
            self.active[d.name] = at

    def can_reattach_all(self) -> bool:
        """Could a restart of THIS process re-adopt every trainer currently running under it
        (invariant 20j-3)? The precondition for re-exec'ing under live work.

        Verified, not assumed: for every task we hold a live handle for, `/proc` must actually show
        a process whose cwd is under that task's dir — because that cwd mapping is the ONLY way the
        restarted image finds it again. If a live trainer is invisible here it would be invisible
        there too, and the task would come back either double-run or stranded with no terminal
        marker. Any error reading `/proc` is a False: an unverifiable box defers, costing latency."""
        try:
            found = {o["task_id"] for o in reap_orphans.find_live_task_procs(self.spool)}
        except Exception:                      # noqa: BLE001
            return False
        for task_id, at in self.active.items():
            if at.proc is not None and at.proc.poll() is None and task_id not in found:
                return False
        return True

    def log(self, task_id: str, event: str, rc=None, detail: str = "") -> None:
        rec = {"t": _now_iso(), "task_id": task_id, "event": event, "rc": rc, "detail": detail}
        with open(self.worker_log, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            (self.spool / "HEARTBEAT").touch()
            self._last_heartbeat = now

    def claim_ready(self) -> None:
        for d in sorted(self.incoming.iterdir()):
            if not d.is_dir() or not (d / "READY").exists():
                continue
            target = self.active_dir / d.name
            if target.exists():
                continue  # already claimed -- idempotent under ship retries/restarts
            d.rename(target)
            self.active[d.name] = ActiveTask(d.name, target)
            self.log(d.name, "claim")

    def validate_and_prepare(self) -> None:
        for task_id, at in list(self.active.items()):
            if at.proc is not None or at.spec is not None:
                continue  # already running (incl. RE-ADOPTED across a restart), or already validated
            # A worker restart (or a crash between claim and start) can find `repo/` already
            # extracted from a prior attempt -- re-verifying/re-extracting/re-pip-installing is
            # wasted work, so unpack+install run only when `repo/` is absent. `spec` (task.json) is
            # ALWAYS (re)loaded below regardless, since a fresh process has no in-memory copy.
            repo_dir = at.dir / "repo"
            if not repo_dir.exists():
                # Fresh: verify + validate task.json BEFORE extracting `repo/` (reject-without-
                # executing), then materialize and pip-install. Rejection is handled inside.
                data = self._unpack(at)
                if data is None:
                    continue
                (at.dir / "out").mkdir(parents=True, exist_ok=True)
                extras = data.get("pip_extras") or []
                if extras and not self._pip_install(at, extras):
                    continue
            else:
                # Restart: task.json was materialized + validated on the prior lifetime -- reload it
                # (a fresh process has no in-memory copy) and re-check, but never re-extract/re-pip.
                try:
                    data = json.loads((at.dir / "task.json").read_text())
                except (OSError, json.JSONDecodeError) as e:
                    self._reject(at, f"unreadable task.json: {e}")
                    continue
                err = validate_task_json(data)
                if err:
                    self._reject(at, err)
                    continue
            at.spec = data

    def _unpack(self, at: ActiveTask):
        """Verify + unpack the delivered bundle into `active/<id>/` (task.json, repo/, resume.pt),
        returning the validated task.json dict (or None if rejected). Trust boundary: bundle
        integrity is always checked, the signature too if a public key was provisioned, and the
        strict task.json key-check runs before any `repo/` is extracted; any failure rejects the
        task. Tolerates a legacy loose-file delivery (payload.tar.gz + task.json) for a mixed-
        rollout box (task-bundle spec Open questions)."""
        bundle_path = at.dir / bundle.BUNDLE_NAME
        if bundle_path.exists():
            try:
                data = bundle.unpack_bundle(bundle_path, at.dir, public_key=self.public_key,
                                            require_signature=self.require_signature,
                                            validate=validate_task_json,
                                            blob_dir=self.blob_dir)
            except bundle.BundleError as e:
                self._reject(at, f"bundle rejected: {e}")
                return None
            # Invariant 29c: the tarball is now a byte-for-byte duplicate of the `repo/` beside it,
            # and `_launch` re-unpacks only when `repo/` is ABSENT, so nothing reads it again. ~33 MB
            # of a measured ~112 MB task dir. Only on a CLEAN unpack — the reject path above returns
            # first, so a bundle we refused keeps its evidence.
            try:
                bundle_path.unlink()
            except OSError:
                pass  # best-effort; the 29b sweep collects the whole dir later regardless
            return data
        legacy_payload, legacy_task = at.dir / "payload.tar.gz", at.dir / "task.json"
        if legacy_payload.exists() and legacy_task.exists():
            try:
                data = json.loads(legacy_task.read_text())
            except (OSError, json.JSONDecodeError) as e:
                self._reject(at, f"unreadable task.json: {e}")
                return None
            err = validate_task_json(data)
            if err:  # validate before extract -- reject-without-executing
                self._reject(at, err)
                return None
            (at.dir / "repo").mkdir(parents=True, exist_ok=True)
            with tarfile.open(legacy_payload) as tf:
                tf.extractall(at.dir / "repo")  # our own `git archive` output -- trusted content
            return data
        self._reject(at, "no bundle.tar or legacy payload delivered")
        return None

    def _pip_install(self, at: ActiveTask, extras: list) -> bool:
        """Newer `pytorch/pytorch` box images dropped conda -> PEP-668
        "externally-managed-environment" rejects a plain install (VAST-TEST.md's documented
        gotcha, previously only handled in the retired shell launch scripts, not here) -- retry
        with --break-system-packages before giving up. A silent failure here was indistinguishable
        from "training crashed" (ModuleNotFoundError at the first `import`, after burning however
        long pip took) until this was fixed live 2026-07-09 against a real stuck box. On failure
        this removes the task from `self.active` (mirroring `_reject`) so `launch_ready` never
        attempts the now-guaranteed-to-crash launch — `repo/` already exists by this point
        (extracted before the install attempt), so without this it would still try."""
        cmd = [sys.executable, "-m", "pip", "install", "-q", *extras]
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            out = subprocess.run([*cmd, "--break-system-packages"], capture_output=True, text=True)
        if out.returncode != 0:
            (at.dir / "FAILED_pip_extras").write_text(out.stderr[-2000:])
            self.log(at.task_id, "reject", detail=f"pip install failed for {extras}: {out.stderr[-500:]}")
            del self.active[at.task_id]
            return False
        return True

    def _reject(self, at: ActiveTask, reason: str) -> None:
        (at.dir / "FAILED_validation").write_text(reason)
        self.log(at.task_id, "reject", detail=reason)
        del self.active[at.task_id]

    def _note_gate(self, task_id, detail) -> None:
        """Emit a `launch_gate` line WHEN THE REASON CHANGES (invariant 19h-4).

        `launch_ready` runs every worker tick, so logging unconditionally would flood
        `worker.jsonl` — a file the dispatcher rsyncs home every ingest pass. Edge-triggered gives
        one line per state transition, which is exactly the diagnostic content: when the hold
        started, why, and when it cleared. `detail=None` marks the clear.

        This exists because the reason was being COMPUTED AND DISCARDED (`ok, _reason = ...`), which
        is why a whole day of over-pack incidents was diagnosed by inference — the box knew why it
        would not launch and never said. Cheapest, highest-leverage form of observability there is:
        a signal at the SOURCE, costing one line per transition."""
        if detail == self._gate_reason:
            return
        if detail is not None:
            self.log(task_id, "launch_gate", detail=detail)
        self._gate_reason = detail

    def _launch_gate(self) -> dict:
        """The launch pacing in force (invariant 8b): the coordinator's, from the copy it pushes
        into the spool, re-read only when that file changes.

        The file is this box's CACHE. Nothing here asks the coordinator anything, so a box whose
        coordinator is unreachable — or whose worker has just restarted — keeps pacing by the last
        values it was handed. One `launch_gate_config` line is logged each time a copy is read (or
        one that was there disappears), saying what is in force and anything that was refused, so
        "which numbers is this box using?" is answerable from `worker.jsonl` and never has to be
        inferred. A worker that has never been sent a copy says nothing: it is on the defaults."""
        path = self.spool / LAUNCH_GATE_FILE
        try:
            st = path.stat()
            seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            seen = None
        if seen == self._gate_cfg_seen:
            return self._gate_cfg
        first_look, self._gate_cfg_seen = self._gate_cfg_seen == "unread", seen
        text = None
        if seen is not None:
            try:
                text = path.read_text()
            except OSError:
                text = ""          # present but unreadable: refused as a whole, defaults stand
        cfg, problems = parse_launch_gate(text)
        if seen is not None or not first_look:
            source = ("from the coordinator" if seen is not None
                      else "the coordinator's copy is gone — built-in defaults")
            self.log("", "launch_gate_config",
                     detail=f"{source}: {json.dumps(cfg, sort_keys=True, separators=(',', ':'))}"
                            + (f" — REFUSED, built-in default kept: {'; '.join(problems)}"
                               if problems else ""))
        self._gate_cfg = cfg
        return cfg

    def launch_ready(self) -> None:
        # Box-pause spec inv. 9: while the box is frozen (owner paused it), start no NEW trainers —
        # the whole point is a quiet CPU/GPU. The dispatcher also stops packing a `paused` box, so
        # this is belt-and-suspenders against anything already claimed.
        if (self.spool / "FREEZE").exists():
            return
        pending = [at for at in self.active.values()
                   if at.proc is None and (at.dir / "repo").exists() and not at.preempting
                   ]
        if not pending:
            return
        # inv 20j/R9: a RE-ADOPTED trainer (`ReattachedProc`) counts here exactly like one we hold
        # a real Popen for — it is burning the same CPU/VRAM. This is automatic because re-adoption
        # sets `at.proc`; before it did, a restarted worker under-reported the box and over-packed it.
        n_live = sum(1 for at in self.active.values()
                     if at.proc is not None and at.proc.poll() is None)
        # Dispatcher invariant 4i-7: a FORCED task (the owner said "run it on this box now,
        # ignoring its capacity gates") launches FIRST and past every check below. Placement
        # admitted it past its own limits on purpose; holding it here at `should_launch`'s
        # cpu/gpu/vram arms would turn that bypass into a `shipped` task that never starts.
        # FREEZE above is NOT bypassed — a paused box stays quiet. The marker rides
        # in `env` because task.json rejects unknown top-level keys (see `validate_task_json`).
        forced = [at for at in pending
                  if ((at.spec or {}).get("env") or {}).get(FORCE_BOX_ENV) == "1"]
        if forced:
            self._note_gate(None, None)
            self.log(forced[0].task_id, "launch_forced",
                     detail=f"force_box: launch gate bypassed ({n_live} live)")
            self._start(forced[0])
            return
        hw = sample_hw()
        since_min = (time.time() - self._last_launch) / 60 if self._last_launch else 1e9
        # ⛔ INVARIANT 8a — NO LANE COUNT ON THE BOX. How many tasks this box carries was decided
        # when the coordinator placed them; a second count here could only ever agree with that
        # one or be WRONG. It was wrong four recorded times: the number arrived once, as a launch
        # argument, and the worker's self-update re-execs with the same argv, so it outlived every
        # change the coordinator made to the box's slots — the box then refused work it had been
        # sent, and the over-pack reaper read the refusal as a capacity ceiling and made it
        # permanent. So only what the box can MEASURE holds a launch: settle / cpu_load /
        # gpu_util / vram. `max_slots` is overridden rather than left at the manual sweep lane's 8.
        # The thresholds those four compare against are the coordinator's (inv. 8b), layered over
        # the built-in ones.
        cfg = self._launch_gate()
        auto_cfg = {**AUTO_DEFAULTS, **cfg, "max_slots": NO_LANE_COUNT}
        # ⛔ INVARIANT 8c — THE CARD DOES NOT HOLD A TASK THAT NEVER TOUCHES IT. `gpu_util` and
        # free VRAM are WHOLE-CARD readings, and past the first lane they held EVERY launch: a card
        # the owner had filled let this box start one CPU-only task at a time however many the
        # coordinator had placed here, and the over-pack reaper then learned that as a ceiling.
        # For a task the coordinator marked, the gate is evaluated with the card unmeasured, so
        # those two arms abstain; settle and cpu_load apply unchanged. Each waiting task is tried
        # in arrival order — still at most one launch per call — so a GPU task held on the card
        # does not hold a CPU-only one behind it.
        hold = None
        for at in pending:
            no_gpu = ((at.spec or {}).get("env") or {}).get(NO_GPU_ENV) == "1"
            seen = {**hw, "gpu_util": None, "vram_free_gb": None} if no_gpu else hw
            ok, reason = should_launch(seen, self._vram_lane_max, n_live, since_min, auto_cfg)
            if ok:
                self._note_gate(None, None)   # launching: the hold, if any, is over
                self._start(at)
                return
            if hold is None:
                hold = (at.task_id, _gate_detail(reason, seen, n_live, since_min,
                                                 self._vram_lane_max, cfg))
        self._note_gate(*hold)

    def _start(self, at: "ActiveTask") -> None:
        """Launch one prepared task. Split out of `launch_ready` so the forced path (dispatcher
        inv. 4i-7) and the gated path start a trainer through the SAME code."""
        spec = at.spec
        argv = [sys.executable if a == "python" else a for a in spec["argv"]] + ["--out", "../out"]
        if spec.get("resume_from"):
            argv += ["--init-from", f"../{spec['resume_from']}"]
        elif (at.dir / "out" / SELF_RESUME_CHECKPOINT).exists():
            # ⛔ SELF-RESUME AFTER AN IN-PLACE RESTART. `resume_from` is set ONLY by the COORDINATOR,
            # when it requeues/relocates a task and ships it a `resume.pt`. It is absent for a task
            # this worker relaunches itself (`_reattach_after_restart` -> `validate_and_prepare` ->
            # here), because that path reloads the ORIGINAL task.json. So a worker restart used to
            # relaunch a long run FROM SCRATCH while its own completed checkpoint sat in `out/`.
            #   MEASURED (m58 `budget50x_s0`, 2026-08-02): a 15h craftax run died at 5h37m with
            # `CheckpointRegression: refusing to write ../out/ckpt_latest.pt: progress would go
            # BACKWARDS — step 173277 -> 2418`. No preempt and no requeue in the task events — one
            # `start`, then `task_failed` — i.e. the worker restarted the process, it began at step 0,
            # and the regression guard refused its first write. The checkpoint it should have resumed
            # from was RIGHT THERE and was fully resumable (seed_index 0, sched stage_done 173277).
            # 5.5M transitions of training were saved only because that guard exists.
            #   SAFE UNCONDITIONALLY: the trainer treats a checkpoint it cannot use as a fresh start
            # (`train_m49_curriculum_ab` gates on `resume["sched"]`, so a boot-only payload —
            # substrate/sched None — simply starts over), so passing this can never be worse than
            # not passing it. Proven end-to-end by `scripts/diagnostics/kill_resume_probe.py`.
            #   The flag/name are HARD-CODED rather than read from task.json on purpose: task.json is
            # validated against `REQUIRED_TASK_KEYS` and REJECTS unknown keys, so adding fields here
            # would make every already-running worker reject every newly-shipped task during the
            # self-update window. All 148 committed manifests use exactly these two values, and
            # `job_manifest.DEFAULT_RESUME_CHECKPOINT` is the same constant.
            argv += ["--init-from", f"../out/{SELF_RESUME_CHECKPOINT}"]
        # Thread-cap calibration (2026-07-15): without these, OpenBLAS/OMP/MKL each default to
        # nproc threads PER PROCESS. An env-worker RL task is 1 main + env_workers subprocesses
        # (~5 procs), so on a 12-core box one task alone spawns ~60 BLAS threads; two tasks →
        # ~120 threads, load ~26, and pthread_create/RLIMIT_NPROC crashes on launch. should_launch's
        # load1>=cores-1 guard only caps task COUNT, not per-task thread blow-up, so it can't
        # prevent this. These workloads parallelize via env_workers, not BLAS (MinAtar nets are
        # tiny — CPU matmuls don't benefit from threading, they just thrash a shared box), so pin
        # each process to 1 BLAS/OMP thread. Set BEFORE spec env so a task that genuinely needs
        # more can still override via its spec["env"].
        thread_caps = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                       "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
                       # ⛔⛔ WITHOUT THIS, A KILLED TASK LEAVES NO EVIDENCE AT ALL. `stdout` below
                       # is a FILE, so Python block-buffers it at 8 KB; a task that dies on a signal
                       # never flushes, and `run.log` arrives EMPTY. Measured on `laptop-gpu`: five
                       # consecutive pclm tasks died with `worker exit -15` 10-30s in, every one of
                       # them with a 0-byte run.log — and round 1 hit the same wall TEN times and
                       # recorded the cause as never diagnosed. The registry then labels the task
                       # `ZERO PROGRESS (likely code/config bug, not infra)`, which is a guess
                       # dressed as a finding: the log that would have settled it was in a buffer.
                       # Unbuffered costs a few syscalls per line and buys every future post-mortem.
                       "PYTHONUNBUFFERED": "1"}
        # dict-literal merge (not dict(**a, **b)) so a key can appear in both thread_caps and
        # spec["env"] with the task's value winning — dict(**a, **b) raises on duplicate keys.
        env = {**os.environ, **thread_caps, "PYTHONPATH": "src", **spec.get("env", {})}
        log_f = open(at.dir / "run.log", "w")
        # start_new_session -> the trainer (and the env-worker subprocesses it forks) share their
        # OWN process group, so box-pause's SIGSTOP/SIGCONT freeze can signal the whole group by
        # pgid without touching THIS worker. terminate()/kill() below still target the leader pid,
        # so preempt/cancel behaviour is unchanged (box-pause spec inv. 9).
        at.proc = subprocess.Popen(argv, cwd=str(at.dir / "repo"), env=env, stdout=log_f,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        self._launched_pids.add(at.proc.pid)
        at.started_at = time.time()
        self._last_launch = time.time()
        self.log(at.task_id, "start", detail=" ".join(argv))

    def _signal_group(self, at: "ActiveTask", sig: int) -> None:
        """Signal the trainer's whole process group (it was launched with start_new_session, so its
        pid IS its pgid). Tolerates the proc/group having just exited — a freeze race, not a fault."""
        try:
            os.killpg(os.getpgid(at.proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def apply_freeze(self) -> None:
        """Box-pause spec inv. 9: while `~/spool/FREEZE` exists (owner soft-paused the box), SIGSTOP
        every live trainer so CPU/GPU go idle with zero lost work — EXCEPT a task carrying a PREEMPT
        or CANCEL marker, which is kept/put running (SIGCONT) so it can still checkpoint-and-exit or
        be killed even while the box is otherwise frozen. When FREEZE is removed (resume, or a drain
        that cleared it), every stopped trainer is SIGCONT'd. Idempotent per tick via `at.stopped`."""
        freeze = (self.spool / "FREEZE").exists()
        for task_id, at in list(self.active.items()):
            if at.proc is None or at.proc.poll() is not None:
                continue
            exempt = (at.dir / "PREEMPT").exists() or (at.dir / "CANCEL").exists()
            want_stopped = freeze and not exempt
            if want_stopped and not at.stopped:
                self._signal_group(at, signal.SIGSTOP)
                at.stopped = True
                self.log(task_id, "freeze", detail="SIGSTOP (box paused)")
            elif not want_stopped and at.stopped:
                self._signal_group(at, signal.SIGCONT)
                at.stopped = False
                self.log(task_id, "unfreeze", detail="SIGCONT")

    def check_preemption(self) -> None:
        for task_id, at in list(self.active.items()):
            if at.proc is None or at.proc.poll() is not None or at.preempting:
                continue
            marker = at.dir / "PREEMPT"
            ckpt = at.dir / "out" / "ckpt_latest.pt"
            if not marker.exists() or not ckpt.exists() or ckpt.stat().st_mtime <= marker.stat().st_mtime:
                continue  # no request, or no checkpoint written since the request yet
            at.preempting = True
            at.proc.terminate()
            deadline = time.time() + KILL_GRACE_SECONDS
            while time.time() < deadline and at.proc.poll() is None:
                time.sleep(1)
            if at.proc.poll() is None:
                at.proc.kill()

    def check_cancellation(self) -> None:
        """CANCEL marker -> stop the run immediately (terminal; unlike PREEMPT we don't wait for a
        checkpoint, since a cancelled task is never resumed). Also covers a task whose proc hasn't
        launched yet (cancel before start): mark cancelling so check_exits writes CANCELLED."""
        for task_id, at in list(self.active.items()):
            if at.cancelling or not (at.dir / "CANCEL").exists():
                continue
            at.cancelling = True
            if at.proc is not None and at.proc.poll() is None:
                at.proc.terminate()
                deadline = time.time() + KILL_GRACE_SECONDS
                while time.time() < deadline and at.proc.poll() is None:
                    time.sleep(1)
                if at.proc.poll() is None:
                    at.proc.kill()
            elif at.proc is None:
                # Never launched -> no exit to observe; write the terminal marker here.
                try:
                    (at.dir / "CANCELLED").write_text("")
                except FileNotFoundError:
                    pass  # dispatcher already rm -rf'd active/<id> out from under us
                self.log(task_id, "cancel", detail="cancelled before launch")
                del self.active[task_id]

    def check_exits(self) -> None:
        for task_id, at in list(self.active.items()):
            if at.proc is None:
                continue
            rc = at.proc.poll()
            if rc is None:
                continue
            if at.cancelling:
                marker, detail = at.dir / "CANCELLED", "cancelled"
            elif at.preempting:
                marker, detail = at.dir / "PREEMPTED", "preempted"
            elif rc == 0:
                marker, detail = at.dir / "DONE", "done"
            else:
                marker, detail = at.dir / f"FAILED_{rc}", f"nonzero exit {rc}"
            try:
                marker.write_text("")
            except FileNotFoundError:
                # The dispatcher rm -rf'd active/<id> out from under us (concurrent requeue/
                # teardown) -- the task is no longer ours to report on, not a worker fault.
                pass
            self.log(task_id, "exit", rc=rc, detail=detail)
            del self.active[task_id]

    def _prune_blobs(self, now: float) -> None:
        """Drop shared code blobs untouched for `BLOB_MAX_AGE_SECONDS` (inv. 11).

        Safe by construction: the dispatcher checks for a blob's presence before every ship and
        re-pushes it if absent, so deleting one costs at most a single re-transfer — never a
        stranded task. Bounded by the same throttle as the orphan reap."""
        if now - self._last_reap < REAP_EVERY_SECONDS:
            return
        try:
            entries = list(self.blob_dir.glob("*.tar.gz"))
        except OSError:
            return
        for p in entries:
            try:
                if now - p.stat().st_mtime > BLOB_MAX_AGE_SECONDS:
                    p.unlink()
            except OSError:
                continue

    @staticmethod
    def _terminal_marker(task_dir: Path):
        """The terminal marker file inside `active/<id>`, or None if the task is not finished.

        The marker is the ONLY safe gate for the sweep below: a running task has none, so keying on
        it protects live work by construction, in a way an age check never could (a long trainer is
        indistinguishable from an abandoned dir by mtime alone)."""
        for name in TERMINAL_MARKERS:
            p = task_dir / name
            if p.exists():
                return p
        try:
            for p in task_dir.glob("FAILED_*"):
                return p
        except OSError:
            pass
        return None

    def _prune_finished(self, now: float) -> None:
        """Invariant 29b: drop `active/<id>` whose terminal marker is older than
        `FINISHED_MAX_AGE_SECONDS`.

        This is the SELF-HEALING half of the spool GC, and it is the half that actually bounds disk.
        The dispatcher deletes at terminal completion (29a), but only when it reaches the box — it
        cannot clean up after a task requeued off an unreachable box, after anything that finished
        while the daemon was down, or after a historical backlog. Measured 2026-08-12: 1375 leaked
        dirs (248G) on one box, every one of them the residue of a task that SUCCEEDED.

        Safe by construction on the same argument as `_prune_blobs`: a terminal marker means the
        worker is done with the task and the dispatcher has either pulled its results or given up on
        them, so the bytes are reconstructible or worthless. Deleting also makes any surviving
        process reapable rather than hidden — `reap_orphans` treats a missing owning dir as grounds
        to kill (inv. 18e)."""
        if now - self._last_reap < REAP_EVERY_SECONDS:
            return
        try:
            entries = [p for p in self.active_dir.iterdir() if p.is_dir()]
        except OSError:
            return
        for d in entries:
            try:
                marker = self._terminal_marker(d)
                if marker is None:
                    continue  # running (or claimed-not-started) -- never ours to delete
                if now - marker.stat().st_mtime <= FINISHED_MAX_AGE_SECONDS:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                self.log(d.name, "spool_gc",
                         detail=f"finished dir swept ({marker.name} older than "
                                f"{FINISHED_MAX_AGE_SECONDS // 3600}h)")
            except OSError:
                continue

    def reap_orphans(self) -> None:
        """Fleet hygiene (found live 2026-07-22): (1) reap our own UNTRACKED zombie children — a
        dead task we stopped tracking (requeued out of `active[]`) that only the parent can
        `waitpid`; and (2) SIGKILL runaway orphan trainers — a proc under `active/` whose task is
        gone/terminal but still burning CPU/VRAM. Throttled to every `REAP_EVERY_SECONDS`. Tracked
        procs are left to `check_exits`/`Popen` — never double-reaped here."""
        now = time.time()
        self._prune_blobs(now)
        self._prune_finished(now)  # inv. 29b — same throttle, same "reconstructible bytes" argument
        if now - self._last_reap < REAP_EVERY_SECONDS:
            return
        self._last_reap = now
        tracked = frozenset(at.proc.pid for at in self.active.values()
                            if at.proc is not None and at.proc.poll() is None)
        # (1) reap our OWN dead children we've stopped tracking (untracked zombies) — only pids we
        # actually launched this lifetime, so we never waitpid an unrelated process's child (which
        # would race that owner's own bookkeeping). Silent hygiene — logging each is just noise.
        for pid in list(self._launched_pids):
            if pid in tracked:
                continue
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    self._launched_pids.discard(pid)  # was defunct, now reaped
            except ChildProcessError:
                self._launched_pids.discard(pid)  # already reaped by Popen — forget it
            except OSError:
                pass
        # (2) SIGKILL runaway orphans (proc under active/ whose task is gone/terminal) — logged only
        # when one is actually killed (rare), so it never pollutes a normal poll.
        reap_orphans.reap(self.spool, live_pids=tracked,
                          log=lambda m: self.log("-", "orphan_reap", detail=m))

    def tick(self) -> None:
        self.heartbeat()
        self.claim_ready()
        self.validate_and_prepare()
        self.apply_freeze()  # box-pause: (un)freeze before preempt/cancel so an exempt task runs
        self.check_preemption()
        self.check_cancellation()
        self.check_exits()
        self.reap_orphans()  # fleet hygiene: kill runaway orphans + reap zombies (throttled)
        self._launch_gate()  # inv. 8b: pick up (and acknowledge) a pacing copy even while idle
        self.launch_ready()


def self_update_decision(cur: str, loaded: str, pending, active, compiles,
                          reattachable=lambda: False) -> str:
    """`"exec"` | `"defer"` | `"settle"` | `"none"` — whether the worker may re-exec itself now.

    Split out of `main()`'s loop so the gate is unit-testable; the loop only logs and dispatches.

    ⚠⚠ **`"defer"` while ANY task is on this box — MEASURED 2026-08-02.** The loop used to re-exec
    regardless of occupants, on the reasoning that *"trainers are launched with start_new_session, so
    replacing this process does not touch them"*. That premise is FALSE IN THE FIELD, and it is the
    same one that made `_retire_pre_20i_worker` destroy 12 cells the day before: the replacement
    worker comes back up, `validate_and_prepare` re-adopts an `active/<id>` that still carries no
    terminal marker, and the task is RE-LAUNCHED from scratch — a second trainer whose first
    checkpoint is at stage 0, which `shared.infra.checkpoint` refuses with
    `CheckpointRegression: progress would go BACKWARDS`.

    Measured: **six tasks across five campaigns died inside 8 minutes on three boxes**
    (`m60_perhead_rep` x2, `m58_craftax_budget`, `m65_slice_default_n6`, `wrel_pc_ab2`,
    `m62_color_cost`) with **no `worker_retired` event anywhere** — so it was this path, not the
    bootstrap gate fixed in `cd93d564`. The trigger is any push to master: `_refresh_workers` rsyncs
    the bootstrap files once per `worker_refresh_min`, the fingerprint moves, and every live box
    re-execs under its running work. A busy day of merges mass-kills the fleet's in-flight runs.

    Deferring costs only LATENCY — the box updates when it drains, and the caller keeps `pending` so
    this re-fires every tick. `"settle"` is the pre-existing one-tick wait that guards against reading
    the four bootstrap files mid-rsync; it must stay, and it is why a fingerprint must be seen twice.
    """
    if cur == loaded:
        return "none"
    if cur != pending:
        return "settle"          # same new fingerprint not yet seen twice — could be a partial rsync
    if not compiles():
        return "settle"          # never exec into sources that do not import
    if not active:
        return "exec"
    # ⛔ INVARIANT 20j-3 — a re-exec UNDER RUNNING WORK is safe iff every live trainer can be
    # RE-ADOPTED on the way back up (`ReattachedProc`). `execv` keeps our PID, so those trainers
    # remain our children and `waitpid` still yields their true exit codes; the new image finds them
    # by /proc cwd, adopts them, and neither relaunches nor kills anything. That turns a worker
    # upgrade into a genuine no-op for running work — which is the point, because the alternative
    # (defer until the box drains) means a busy box NEVER updates.
    #
    # The precondition is checked, never assumed: if we cannot see a live trainer that we are
    # holding a handle for, re-adoption would silently fail — and a task that came back
    # unrecognised would either be relaunched (double-run, racing on one `out/`) or stranded with
    # no terminal marker. So an unverifiable box falls back to `defer`, which costs only latency.
    return "exec" if reattachable() else "defer"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spool", required=True)
    # ACCEPTED AND IGNORED (invariant 8a). A worker that self-updates re-execs with the argv it
    # was started with, and every worker started before 8a carries `--max-slots N`. Rejecting the
    # flag would kill such a worker on its way into this code — `_sources_compile` cannot see an
    # argparse error — and nothing relaunches a worker that dies on a rental.
    ap.add_argument("--max-slots", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--public-key", default=None,
                    help="ed25519 public key (PEM); when set, bundles must be validly signed")
    a = ap.parse_args(argv)
    w = Worker(Path(a.spool).expanduser(), public_key=a.public_key)
    loaded = _source_fingerprint()
    (w.spool / "WORKER_VERSION").write_text(loaded + "\n")
    pending = None
    deferred = None          # fingerprint whose deferral has already been logged (log once, not per tick)
    while True:
        w.tick()
        # Self-update AFTER tick() and before the sleep: `tick` has returned, so no bundle is
        # half-unpacked and no claim is half-made.
        #
        # ⚠⚠ ONLY ON AN IDLE BOX — MEASURED 2026-08-02. This block used to re-exec regardless of
        # occupants, on the reasoning that "trainers are launched with start_new_session, so
        # replacing this process does not touch them". That premise is FALSE IN THE FIELD, and it is
        # the same one that made `_retire_pre_20i_worker` destroy 12 cells the day before: the
        # replacement worker comes back up, `validate_and_prepare` re-adopts an `active/<id>` that
        # still has no terminal marker, and the task is RE-LAUNCHED from scratch — a second trainer
        # that writes a stage-0 checkpoint, which `shared.infra.checkpoint` then refuses with
        # `CheckpointRegression: progress would go BACKWARDS`.
        #
        # Measured: SIX tasks across FIVE campaigns died inside 8 minutes on THREE boxes
        # (`m60_perhead_rep` x2, `m58_craftax_budget`, `m65_slice_default_n6`, `wrel_pc_ab2`,
        # `m62_color_cost`) with NO `worker_retired` event anywhere — so it was this path, not the
        # bootstrap. The trigger is any push to master: `_refresh_workers` rsyncs the bootstrap files
        # once per `worker_refresh_min`, the fingerprint moves, and every live box re-execs under its
        # running work. A busy day of merges therefore mass-kills the fleet's in-flight runs.
        #
        # Deferring costs only latency — the box updates when it drains, and `pending` keeps the new
        # fingerprint so the check re-fires every tick. The deferral is LOGGED (once per fingerprint)
        # because a silently-never-firing self-update would just be a different invisible failure.
        cur = _source_fingerprint()
        decision = self_update_decision(cur, loaded, pending, w.active, _sources_compile,
                                         w.can_reattach_all)
        if decision == "exec":
            w.log("", "worker_update", detail=f"{loaded} -> {cur}: re-exec")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        elif decision == "defer" and deferred != cur:
            w.log("", "worker_update_deferred",
                  detail=f"{loaded} -> {cur}: {len(w.active)} task(s) still on this box; re-exec "
                         f"deferred until the box drains (restarting under running work re-launches "
                         f"it from scratch -> CheckpointRegression)")
            deferred = cur
        pending = cur if cur != loaded else None
        if cur == loaded:
            deferred = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
