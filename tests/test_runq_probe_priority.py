"""`runq add --probe` — the quick-hypothesis-test priority class (run-registry spec).

A probe is the run whose answer BLOCKS a decision, so its latency matters more than fleet
throughput. Getting it scheduled behind an 8-hour sweep is the failure this class exists to stop.
The number is load-bearing rather than cosmetic: `PROBE_PRIORITY` must clear `DEFAULT_PRIORITY` by
more than the dispatcher's `preempt_priority_margin`, or a probe can never displace ordinary work
and the flag silently does nothing (the repo's "a knob nothing can search" failure family).

The est-minutes bound is the matching trust-boundary check: because a probe EVICTS running work,
a sweep mislabelled as one costs real jobs hours.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import pathlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNQ = ROOT / "fleet" / "runq.py"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")
sys.path.insert(0, str(ROOT / "fleet"))
runq = _load("runq", "fleet/runq.py")



def _no_compile(db):
    """These tests exercise QUEUEING, not compilation. `runq` now builds a ship-ready artifact at
    add time (ship-artifact-build spec inv. 2), so without this every `add` would run a real ~170s
    cythonize. Seeding the deliberate off-switch (Q4) keeps them fast and still exercises the real
    build path — `build_ship_ready` returns the source tree and records `code_format="snapshot"`.
    Compilation itself is covered by tests/test_artifact_store.py."""
    import sqlite3
    pathlib.Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile', 'false')")
    conn.commit()
    conn.close()


def _runq(db, *args, actor="probe-tests"):
    env = dict(os.environ)
    env.pop("RUNQ_ACTOR", None)
    if actor is not None:
        env["RUNQ_ACTOR"] = actor
    _no_compile(db)
    return subprocess.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


TOY_TRAINER = """\
import dataclasses, sys
from shared.infra.harness import CheckpointSection, RunSection, run_trainer

@dataclasses.dataclass
class C:
    seed: int = 0
    run: RunSection = dataclasses.field(default_factory=RunSection)
    checkpoint: CheckpointSection = dataclasses.field(default_factory=CheckpointSection)

sys.exit(run_trainer(C, lambda hr: hr.save_latest({"ok": 1}, force=True)))
"""


def _job(tmp_path, name, est_minutes):
    job = tmp_path / name
    job.mkdir()
    (job / "src").symlink_to(ROOT / "tests" / "support" / "src")
    (job / "trainer.py").write_text(TOY_TRAINER)
    manifest = {
        "manifest_version": 1,
        "run": ["python", "trainer.py"],
        "completion_artifact": "results.json",
        "resources": {"est_minutes": est_minutes},
    }
    if est_minutes > 10:
        # Unrelated pre-existing guard: a >10min job with no `resume` block is refused by the
        # checkpoint+resume rule BEFORE the --probe bound is reached. Declare one so these fixtures
        # exercise the probe check rather than that one.
        manifest["resume"] = {"flag": "--init-from", "checkpoint": "ckpt_latest.pt"}
    (job / "cfg.json").write_text(json.dumps({"job": manifest}))
    return job / "cfg.json"


def _priority_of(db, task_id):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "runs.sqlite"
    _runq(p, "bootstrap")
    return p


class TestProbePriorityIsLoadBearing:
    def test_probe_priority_can_actually_preempt_default_work(self):
        """If this drifts, --probe becomes a no-op flag: it would set a higher number that still
        fails the dispatcher's eligibility test, and probes would queue behind ordinary jobs."""
        margin = disp.DEFAULT_SETTINGS["preempt_priority_margin"]
        assert runq.PROBE_PRIORITY - margin >= runq.DEFAULT_PRIORITY
        assert runq.DEFAULT_PRIORITY == disp.DEFAULT_PRIORITY      # the two files must agree

    def test_probe_priority_bypasses_the_backlog_bar(self):
        """> DEFAULT_PRIORITY is what exempts a task from invariant 4d's backlog gate, so a probe
        may rent immediately instead of waiting for a queue to accumulate."""
        assert runq.PROBE_PRIORITY > disp.DEFAULT_PRIORITY


class TestProbeFlag:
    def test_probe_sets_the_elevated_priority(self, db, tmp_path):
        job = _job(tmp_path, "quick", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "p1", "--config", str(job), "--probe")
        assert r.returncode == 0, r.stderr
        assert _priority_of(db, r.stdout.strip()) == runq.PROBE_PRIORITY

    def test_without_probe_the_default_is_unchanged(self, db, tmp_path):
        job = _job(tmp_path, "ordinary", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "p2", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert _priority_of(db, r.stdout.strip()) == runq.DEFAULT_PRIORITY

    def test_explicit_priority_still_wins_over_probe(self, db, tmp_path):
        job = _job(tmp_path, "explicit", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "p3", "--config", str(job),
                  "--probe", "--priority", "70")
        assert r.returncode == 0, r.stderr
        assert _priority_of(db, r.stdout.strip()) == 70

    def test_a_long_job_is_rejected_as_a_probe(self, db, tmp_path):
        """The bound is enforced against the RESOLVED est_minutes — here supplied by the manifest,
        never passed on the command line."""
        job = _job(tmp_path, "sweeplike", est_minutes=runq.PROBE_MAX_MINUTES + 1)
        r = _runq(db, "add", "--group", "g", "--name", "p4", "--config", str(job), "--probe")
        assert r.returncode == 2
        assert "is not a probe" in r.stderr
        con = sqlite3.connect(db)
        try:                                    # rejected BEFORE any row is written
            assert con.execute("SELECT COUNT(*) FROM tasks WHERE name='p4'").fetchone()[0] == 0
        finally:
            con.close()

    def test_the_same_long_job_queues_fine_without_probe(self, db, tmp_path):
        job = _job(tmp_path, "sweeplike2", est_minutes=runq.PROBE_MAX_MINUTES + 1)
        r = _runq(db, "add", "--group", "g", "--name", "p5", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert _priority_of(db, r.stdout.strip()) == runq.DEFAULT_PRIORITY


class TestProbeIsDiscoverableAtPointOfUse:
    """Docs only reach whoever read them. A short job queued at default priority gets told once that
    --probe exists — the failure this closes is an agent dodging the queue because it never knew a
    decision-blocking read could jump it."""

    def test_short_default_priority_job_gets_the_hint(self, db, tmp_path):
        job = _job(tmp_path, "shortish", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "h1", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert "--probe" in r.stderr
        assert r.stdout.strip()          # the task id is still the ONLY thing on stdout

    def test_no_hint_when_already_a_probe(self, db, tmp_path):
        job = _job(tmp_path, "isprobe", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "h2", "--config", str(job), "--probe")
        assert r.returncode == 0, r.stderr
        assert "hint" not in r.stderr

    def test_no_hint_when_priority_was_chosen_explicitly(self, db, tmp_path):
        """Also the guard that keeps `runq sweep` from emitting this once per cell — its cells
        always set an explicit priority."""
        job = _job(tmp_path, "explicitprio", est_minutes=5)
        r = _runq(db, "add", "--group", "g", "--name", "h3", "--config", str(job), "--priority", "50")
        assert r.returncode == 0, r.stderr
        assert "hint" not in r.stderr

    def test_no_hint_for_a_job_too_long_to_be_a_probe(self, db, tmp_path):
        job = _job(tmp_path, "toolong", est_minutes=runq.PROBE_MAX_MINUTES + 1)
        r = _runq(db, "add", "--group", "g", "--name", "h4", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert "hint" not in r.stderr
