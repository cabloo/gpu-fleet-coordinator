"""`runq sweep` — fixtures from docs/specs/runq-sweep.spec.md.

Expansion golden + naming rules are unit-level (sweep_expand is pure); enqueue/idempotence/abort
semantics are subprocess-level against a temp --db and a toy harness job dir, mirroring
test_runq.py (exit codes and stdout ARE the contract).
"""

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
FIXTURES = ROOT / "docs" / "specs" / "fixtures" / "runq-sweep"

_spec = importlib.util.spec_from_file_location("sweep_expand", ROOT / "fleet/sweep_expand.py")
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)



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


def _runq(db, *args, actor="sweep-tests"):
    env = dict(os.environ)
    env.pop("RUNQ_ACTOR", None)
    if actor is not None:
        env["RUNQ_ACTOR"] = actor
    _no_compile(db)
    return subprocess.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def _rows(db):
    if not Path(db).exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at, name")]
    except sqlite3.OperationalError:                     # bare file, never bootstrapped
        return []


TOY_TRAINER = """\
import dataclasses, sys
from shared.infra.harness import CheckpointSection, RunSection, run_trainer

@dataclasses.dataclass
class C:
    seed: int = 0
    run: RunSection = dataclasses.field(default_factory=RunSection)
    checkpoint: CheckpointSection = dataclasses.field(default_factory=CheckpointSection)
    plan_kappa: float = 1.0
    rounds: int = 8

sys.exit(run_trainer(C, lambda hr: hr.save_latest({"ok": 1}, force=True)))
"""


@pytest.fixture()
def toy_job(tmp_path):
    job = tmp_path / "toyjob"
    job.mkdir()
    # v2: the caller names a CONFIG file and the tree containing it is snapshotted. The identity
    # handshake runs with that tree's own `src` on PYTHONPATH, so the job's OWN code answers it.
    # This fixture's trainer imports `shared.infra.harness`, so the dir has to ship it.
    (job / "src").symlink_to(ROOT / "tests" / "support" / "src")
    (job / "trainer.py").write_text(TOY_TRAINER)
    (job / "cfg.json").write_text(json.dumps({
        "job": {
            "manifest_version": 1,
            "run": ["python", "trainer.py"],
            "completion_artifact": "results.json",
            "resources": {"est_minutes": 5},
        },
    }))
    return job / "cfg.json"


def _sweep_file(tmp_path, job, axes, **extra):
    payload = {"sweep_version": 1, "config": str(job), "group": "sw", "axes": axes, **extra}
    p = tmp_path / "sweep.json"
    p.write_text(json.dumps(payload))
    return p


# --- Fixture 1: golden expansion -----------------------------------------------------------------
def test_expand_matches_golden():
    sweep = se.parse_sweep(json.loads((FIXTURES / "basic.sweep.json").read_text()))
    got = [{"name": n, "entry_args": se.cell_args(o)} for n, o in se.expand(sweep)]
    assert got == json.loads((FIXTURES / "basic.cells.json").read_text())


# --- Fixture 2: naming rules ---------------------------------------------------------------------
def test_short_key_collision_uses_full_paths_and_objects_hash():
    sweep = se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                            "axes": {"a.rounds": [10], "b.rounds": [20]}})
    name, _ = se.expand(sweep)[0]
    assert name == "a-rounds10_b-rounds20"
    sweep2 = se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                             "axes": {"curriculum.0.cfg": [{"n_prim": 4}]}})
    name2, _ = se.expand(sweep2)[0]
    import hashlib
    h = hashlib.sha1(json.dumps({"n_prim": 4}, separators=(",", ":"), sort_keys=True)
                     .encode()).hexdigest()[:6]
    assert name2 == f"cfg{h}"
    sweep3 = se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                             "axes": {"flag": [True, False]}})
    assert [n for n, _ in se.expand(sweep3)] == ["flagT", "flagF"]


def test_name_template_and_unknown_path():
    sweep = se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                            "axes": {"plan_kappa": [0.5]},
                            "name_template": "k{plan_kappa}"})
    assert se.expand(sweep)[0][0] == "k0.5"
    bad = se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                          "axes": {"plan_kappa": [0.5]}, "name_template": "x{nope}"})
    with pytest.raises(se.SweepError):
        se.expand(bad)


def test_parse_rejects_bad_files():
    for raw in (
        {"sweep_version": 2, "config": "c.json", "group": "g", "axes": {"a": [1]}},
        {"sweep_version": 1, "config": "c.json", "group": "g", "axes": {"a": [1]}, "bogus": 1},
        {"sweep_version": 1, "config": "c.json", "group": "g", "axes": {"a": []}},
        {"sweep_version": 1, "config": "c.json", "group": "g", "axes": {}},          # empty expansion
        {"sweep_version": 1, "group": "g", "axes": {"a": [1]}},              # no job
    ):
        with pytest.raises(se.SweepError):
            se.parse_sweep(raw)
    with pytest.raises(se.SweepError):                                       # duplicate cell name
        se.expand(se.parse_sweep({"sweep_version": 1, "config": "c.json", "group": "g",
                                  "axes": {"a": [1]}, "cells": [{"a": 1}]}))


# --- Fixture 3: idempotent re-run ----------------------------------------------------------------
def test_sweep_enqueues_then_skips_duplicates(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": [0.5, 2.0]})
    r1 = _runq(db, "sweep", str(sw))
    assert r1.returncode == 0, r1.stderr
    assert "queued=2 skipped=0" in r1.stdout
    r2 = _runq(db, "sweep", str(sw))
    assert r2.returncode == 0, r2.stderr
    assert "queued=0 skipped=2" in r2.stdout
    rows = _rows(db)
    assert len(rows) == 2 and {r["grp"] for r in rows} == {"sw"}
    assert sorted(r["name"] for r in rows) == ["plan_kappa0.5", "plan_kappa2.0"]
    assert all(r["job_manifest_json"] for r in rows)                 # manifest path, not named
    args = json.loads(rows[0]["args_json"])
    # v2: the named config LEADS the args, so a task row states which config it ran. Under v1 this
    # started at "--set" and the config was implicit in a mutable repo-root job.json.
    assert args[0] == "--config" and args[1].endswith("cfg.json")
    assert args[2] == "--set" and "plan_kappa=" in args[3]
    assert len({r["config_hash"] for r in rows}) == 2                # cells hash distinctly


# --- Fixture 4: invalid axis path fails closed at the handshake ----------------------------------
def test_invalid_axis_path_aborts(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"n_prims": [1, 2]})
    r = _runq(db, "sweep", str(sw))
    assert r.returncode != 0
    assert "n_prims" in r.stderr                                     # the harness's dotted-path error
    assert _rows(db) == []                                           # nothing enqueued


# --- Fixture 5: --max-cells cap ------------------------------------------------------------------
def test_max_cells_cap(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": [1, 2], "rounds": [1, 2]})
    r = _runq(db, "sweep", str(sw), "--max-cells", "3")
    assert r.returncode == 2 and "max-cells" in r.stderr
    assert not db.exists() or _rows(db) == []


# --- Fixture 6: --dry-run touches nothing --------------------------------------------------------
def test_dry_run_prints_cells_writes_nothing(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": [1, 2], "rounds": [1, 2]})
    r = _runq(db, "sweep", str(sw), "--dry-run")
    assert r.returncode == 0
    assert r.stdout.count(": DRY") == 4 and "4 cells" in r.stdout
    assert not db.exists() or _rows(db) == []


# --- actor rule carries over ---------------------------------------------------------------------
def test_sweep_requires_actor(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": [1]})
    env = dict(os.environ)
    env.pop("RUNQ_ACTOR", None)
    r = subprocess.run([sys.executable, str(RUNQ), "--db", str(db), "sweep", str(sw),
                        "--by", "master"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 2                                         # reserved actor rejected


# --- Sibling co-location, ON BY DEFAULT (dispatcher inv. 4g) -------------------------------------
#
# Owner directive 2026-08-16: "have paired seed tests always use it". Arms 1..n of ONE seed are a
# paired measurement and must share a box (the box selects the attractor on a bistable rung — arms
# measured on different machines are not paired at all, and a collapsed control manufactures a win).
# Seeds are NOT paired with each other, so they must stay free to spread across the fleet: that is
# the difference between this and `--box`, which pairs everything with everything and serialises a
# whole campaign behind one machine.
def test_colocate_keys_group_by_seed_and_only_by_seed():
    cells = [("a_s1", {"arm": "a", "seeds": [1]}), ("b_s1", {"arm": "b", "seeds": [1]}),
             ("a_s2", {"arm": "a", "seeds": [2]}), ("b_s2", {"arm": "b", "seeds": [2]})]
    keys = se.colocate_keys("camp", cells)
    assert keys["a_s1"] == keys["b_s1"], "arms of one seed must share a box"
    assert keys["a_s2"] == keys["b_s2"]
    assert keys["a_s1"] != keys["a_s2"], "two seeds must NOT be forced onto one box"
    # readable, because a `hold` line and `runq colocate` are read by a human mid-campaign — and
    # `"seeds": [1]` is this repo's dominant form, which the generic renderer would hash to 6 hex.
    assert keys["a_s1"] == "camp:seeds1", keys


def test_a_sweep_with_no_seed_axis_is_ONE_group():
    """Its seed comes from the base config, so every cell IS the same paired seed — the 1-seed
    scout, this repo's most common paired comparison and the one that most needs pairing."""
    cells = [("ctrl", {"arm": "ctrl"}), ("armA", {"arm": "A"})]
    keys = se.colocate_keys("scout", cells)
    assert keys["ctrl"] == keys["armA"] == "scout:seed-from-config"


def test_a_cell_that_omits_the_seed_path_groups_with_the_other_such_cells():
    cells = [("s1", {"seed": 1}), ("s2", {"seed": 2}), ("base", {"arm": "x"})]
    keys = se.colocate_keys("g", cells)
    assert len({keys["s1"], keys["s2"], keys["base"]}) == 3


def test_an_explicit_colocate_by_path_no_cell_carries_is_REFUSED():
    """Silently collapsing every cell into one group is the failure most easily mistaken for the
    feature working — it looks like co-location and costs the whole sweep its parallelism."""
    with pytest.raises(se.SweepError) as e:
        se.colocate_keys("g", [("a", {"arm": "a"})], by="nosuch.path")
    assert "--no-colocate" in str(e.value)


def test_sweep_stamps_the_colocate_hint_per_seed(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"seed": [1, 2], "plan_kappa": [0.5, 2.0]})
    r = _runq(db, "sweep", str(sw))
    assert r.returncode == 0, r.stderr
    by_seed = {}
    for row in _rows(db):
        hint = json.loads(row["resource_hint_json"])
        by_seed.setdefault(hint["colocate"], set()).add(row["name"])
    assert set(by_seed) == {"sw:seed1", "sw:seed2"}, by_seed
    assert all(len(names) == 2 for names in by_seed.values()), by_seed


def test_no_colocate_opts_out_and_leaves_the_hint_absent(tmp_path, toy_job):
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"seed": [1, 2]})
    r = _runq(db, "sweep", str(sw), "--no-colocate")
    assert r.returncode == 0, r.stderr
    assert all("colocate" not in (row["resource_hint_json"] or "") for row in _rows(db))


def test_sweep_box_and_colocation_are_mutually_exclusive(tmp_path, toy_job):
    """`--box` already forces every cell onto one named machine; accepting both would mean one of
    the two silently does nothing."""
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"seed": [1]})
    r = _runq(db, "sweep", str(sw), "--box", "laptop-gpu")
    assert r.returncode == 2 and "mutually exclusive" in r.stderr, r.stderr
    r2 = _runq(db, "sweep", str(sw), "--box", "laptop-gpu", "--no-colocate")
    assert r2.returncode == 0, r2.stderr
    assert json.loads(_rows(db)[0]["resource_hint_json"])["box"] == "laptop-gpu"


def test_a_big_colocation_group_WARNS_before_the_spend(tmp_path, toy_job):
    """A group cannot run wider than the box it pins, so anything past that box's lane count
    serialises. That is a real cost, and the operator must see it at queue time rather than infer it
    from a slow queue."""
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": list(range(1, 11))})
    r = _runq(db, "sweep", str(sw), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "SERIALISE" in r.stderr and "--no-colocate" in r.stderr, r.stderr


# ─────────────────────────────────────────────────────────────────────────────────────────────
# An entrypoint that cannot parse `--set` makes every cell of a sweep run the BASE config.
_rq_spec = importlib.util.spec_from_file_location("runq_mod", RUNQ)
_rq = importlib.util.module_from_spec(_rq_spec)
sys.path.insert(0, str(ROOT / "fleet"))
_rq_spec.loader.exec_module(_rq)


def _cfg_with_entrypoint(tmp_path, script_src, name="ep.py"):
    (tmp_path / name).write_text(script_src)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"job": {"manifest_version": 1, "run": ["python", name],
                                       "completion_artifact": "results.json",
                                       "resources": {"est_minutes": 5}}}))
    return cfg


def test_an_entrypoint_that_ignores_set_is_DETECTED(tmp_path):
    """⛔ THE POSITIVE CONTROL FOR THE GUARD. `sweep_expand.cell_args` emits the axis as
    `--set path=value`; an argparser that does not accept it drops the override SILENTLY, so every
    cell runs the base config and identity-dedupe then reports the siblings as `skipped-duplicate` --
    the failure looks like idempotence. Measured 2026-09-12: 5 of the 10 `.py` entrypoints named in
    `configs/**` `job.run` cannot consume `--set`, `sparse_pc_ladder.py` among them.

    If this assertion cannot fail, the guard is decoration."""
    cfg = _cfg_with_entrypoint(tmp_path, "import argparse\n"
                                         "argparse.ArgumentParser().parse_known_args()\n")
    assert _rq.entrypoint_ignoring_set(str(cfg)) == "ep.py"


@pytest.mark.parametrize("src", [
    'import argparse\nargparse.ArgumentParser().add_argument("--set", action="append")\n',
    "from _probe_job import probe_argparser\nprobe_argparser()\n",
    "from shared.infra.harness import run_trainer\nrun_trainer(None, None)\n",
])
def test_the_three_legitimate_routes_to_set_are_all_cleared(tmp_path, src):
    """⚠ A FALSE REFUSAL IS WORSE THAN THE BUG. An entrypoint gets `--set` three ways: it declares the
    flag, it borrows `probe_argparser`, or it defers to `run_trainer`/`shared.infra.harness`, which
    parses argv on its behalf and names the flag NOWHERE in the calling script. The toy trainer in
    this very file takes the third route, so matching only on the literal string would have broken
    every other test here."""
    assert _rq.entrypoint_ignoring_set(str(_cfg_with_entrypoint(tmp_path, src))) is None


def test_the_guard_FAILS_OPEN_on_anything_it_cannot_judge(tmp_path):
    """A queue guard that blocked on what it cannot read would be worse than the bug it prevents."""
    mod = tmp_path / "mod.json"                      # module entrypoint: reaches the harness
    mod.write_text(json.dumps({"job": {"run": ["python", "-m", "pclm.train"]}}))
    assert _rq.entrypoint_ignoring_set(str(mod)) is None
    missing = tmp_path / "missing.json"              # names a script that is not there
    missing.write_text(json.dumps({"job": {"run": ["python", "nope.py"]}}))
    assert _rq.entrypoint_ignoring_set(str(missing)) is None
    junk = tmp_path / "junk.json"                    # not even JSON
    junk.write_text("{{{not json")
    assert _rq.entrypoint_ignoring_set(str(junk)) is None
    assert _rq.entrypoint_ignoring_set(str(tmp_path / "absent.json")) is None


def test_sweep_REFUSES_an_entrypoint_that_would_silently_unvary_the_axis(tmp_path, toy_job):
    """End to end: the refusal must reach the operator with both fixes named, and `--force` must be
    the documented escape. The toy job is itself CLEARED (it defers to `run_trainer`), so this
    rewrites its entrypoint to one that parses nothing — which is the real-world case."""
    (toy_job.parent / "trainer.py").write_text("import argparse\n"
                                               "argparse.ArgumentParser().parse_known_args()\n")
    db = tmp_path / "runs.sqlite"
    sw = _sweep_file(tmp_path, toy_job, {"plan_kappa": [1.0, 2.0, 3.0]})
    r = _runq(db, "sweep", str(sw), "--dry-run")
    assert r.returncode == 2, r.stderr
    assert "does not accept `--set`" in r.stderr, r.stderr
    assert "arms" in r.stderr and "--force" in r.stderr, r.stderr
