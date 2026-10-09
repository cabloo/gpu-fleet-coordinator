"""`runq` honours a manifest's `resources.slots` — the field that was validated then dropped.

`job_manifest` type-checked `resources.slots` and `runq add` then wrote `slots=a.slots` (CLI default
1), so every manifest task took exactly ONE lane however many it declared. Harmless while a task was
one process; once a job runs K workers, a one-slot claim lets K-times-oversubscribed tasks share a
box, because placement checks slots and nothing else. Same failure family as the repo's
"a knob nothing can search" rule: a field that validates and then does nothing reads as configured.
"""
from __future__ import annotations

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


def _runq(db, *args, actor="slots-tests"):
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


def _job(tmp_path, name, resources):
    """A REAL harness trainer: `runq add` runs a pre-spend `--print-run-identity` handshake, so a
    stub that cannot answer it is rejected before any task row is written.

    ⚠ MIGRATED 2026-07-30 to the artifact-agnostic v2 contract. This helper used to write a
    `job.json` beside the trainer and the tests passed `--job <dir>`; v2 deleted both — the run
    contract is now the reserved `job` section of the config named by `--config`. `runq add` no
    longer HAS a `--job` flag, so all three tests died in argparse ("unrecognized arguments: --job")
    and stayed red on master. The mechanism under test never moved: `_manifest_slots` is still called
    on the `--config` path (runq.py:404), so only the interface here needed updating."""
    job = tmp_path / name
    job.mkdir()
    # self-contained: only this dir is snapshotted, and the handshake puts only `<dir>/src` on
    # PYTHONPATH, so the job ships the harness it imports.
    (job / "src").symlink_to(ROOT / "tests" / "support" / "src")
    (job / "trainer.py").write_text(TOY_TRAINER)
    (job / "cfg.json").write_text(json.dumps({"job": {
        "manifest_version": 1,
        "run": ["python", "trainer.py"],
        "completion_artifact": "results.json",
        "resources": resources,
    }}))
    return job / "cfg.json"


def _slots_of(db, task_id):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT slots FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "runs.sqlite"
    _runq(p, "bootstrap")
    return p


def test_manifest_slots_is_honoured(db, tmp_path):
    job = _job(tmp_path, "parallel", {"est_minutes": 5, "slots": 4, "cores": 1, "vram_gb": 0.6})
    r = _runq(db, "add", "--group", "g", "--name", "n1", "--config", str(job))
    assert r.returncode == 0, r.stderr
    assert _slots_of(db, r.stdout.strip()) == 4


def test_explicit_cli_slots_still_wins(db, tmp_path):
    job = _job(tmp_path, "parallel2", {"est_minutes": 5, "slots": 4, "cores": 1, "vram_gb": 0.6})
    r = _runq(db, "add", "--group", "g", "--name", "n2", "--config", str(job), "--slots", "2")
    assert r.returncode == 0, r.stderr
    assert _slots_of(db, r.stdout.strip()) == 2


def test_a_manifest_without_slots_still_defaults_to_one(db, tmp_path):
    """Every serial job in the tree omits `slots`; none of them may change behaviour."""
    job = _job(tmp_path, "serial", {"est_minutes": 5})
    r = _runq(db, "add", "--group", "g", "--name", "n3", "--config", str(job))
    assert r.returncode == 0, r.stderr
    assert _slots_of(db, r.stdout.strip()) == 1


def _hint_of(db, task_id):
    con = sqlite3.connect(db)
    try:
        raw = con.execute("SELECT resource_hint_json FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        return json.loads(raw) if raw else None
    finally:
        con.close()


class TestPlacementHintPassthrough:
    """The same "validated then dropped" defect as `resources.slots` above, one layer down.

    `dispatcher.py` READS three placement hints and documents each as something "a task declares":
    `max_dph` (inv. 4f price ceiling), `ram_per_lane_gb` (inv. 27 RAM axis of `slots_for_offer`) and
    `cpu_name_include` (inv. 4f named-CPU targeting). `_manifest_est_and_hint` emitted only
    `vram_per_lane_gb`/`cores_per_lane`, and no other code path wrote `resource_hint_json` — so all
    three were unreachable: a config could set them, nothing would carry them, and placement would
    silently use the global defaults. That reads as configured, which is exactly the trap."""

    def test_placement_hints_reach_the_task_row(self, db, tmp_path):
        job = _job(tmp_path, "targeted", {
            "est_minutes": 5, "slots": 2, "cores": 1, "vram_gb": 0.6,
            "max_dph": 0.60, "ram_per_lane_gb": 3.0, "cpu_name_include": ["9950X", "9900X"]})
        r = _runq(db, "add", "--group", "g", "--name", "h1", "--config", str(job))
        assert r.returncode == 0, r.stderr
        hint = _hint_of(db, r.stdout.strip())
        assert hint["max_dph"] == 0.60
        assert hint["ram_per_lane_gb"] == 3.0
        assert hint["cpu_name_include"] == ["9950X", "9900X"]
        # the per-lane footprint it used to emit is untouched
        assert hint["vram_per_lane_gb"] == 0.6 and hint["cores_per_lane"] == 1

    def test_a_manifest_declaring_none_of_them_is_byte_identical(self, db, tmp_path):
        """The regression guard: every existing config omits all three, so their hint must not
        gain a key (a None/empty field would still change the stored JSON and the arm hash)."""
        job = _job(tmp_path, "plain", {"est_minutes": 5, "slots": 2, "cores": 1, "vram_gb": 0.6})
        r = _runq(db, "add", "--group", "g", "--name", "h2", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert _hint_of(db, r.stdout.strip()) == {"vram_per_lane_gb": 0.6, "cores_per_lane": 1}

    def test_placement_hints_survive_without_a_per_lane_footprint(self, db, tmp_path):
        """`max_dph` must not require declaring `cores`+`vram_gb` — the old code returned None for
        the whole hint when either was missing, which would drop the ceiling on the floor."""
        job = _job(tmp_path, "dphonly", {"est_minutes": 5, "max_dph": 0.60})
        r = _runq(db, "add", "--group", "g", "--name", "h3", "--config", str(job))
        assert r.returncode == 0, r.stderr
        assert _hint_of(db, r.stdout.strip()) == {"max_dph": 0.60}
