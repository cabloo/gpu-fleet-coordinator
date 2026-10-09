"""Learned est_minutes defaults (docs/specs/est-defaults.spec.md).

Pure `derive_defaults`/`resolve_est_minutes`/`load_default` unit tests + a golden end-to-end fixture
seeded into a temp registry DB (hand-verifiable, round timings). Goldens regenerate with REGEN=1.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "est_defaults"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reg = _load("registry_db", "fleet/registry_db.py")
cal = _load("calibration", "fleet/calibration.py")
est = _load("est_defaults", "fleet/est_defaults.py")


def _ta(ep, active, state="done", degenerate=False):
    return cal.TaskActual("T", "g", ep, 1, 100, state, active, [], degenerate)


# --------------------------------------------------------------------------- pure: derive_defaults

def test_derive_basic(tmp_path):
    """Invariants 1-3: done+non-degenerate only, est=ceil(p90), low_confidence below min_sample,
    task_failed-only entrypoint absent."""
    actuals = [
        *[_ta("train_a", m) for m in (10, 20, 30, 40, 50)],  # n=5, not low-conf
        _ta("train_a", 1.0, degenerate=True),                # excluded (degenerate), not counted
        *[_ta("train_b", m) for m in (15, 25)],              # n=2 -> low_confidence
        _ta("train_c", 40, state="task_failed"),             # excluded (not done) -> absent
    ]
    doc = est.derive_defaults(actuals, min_sample=5)

    assert doc["_meta"] == {"min_sample": 5, "generated_task_count": 7, "statistic": est.STATISTIC}
    assert set(doc["entrypoints"]) == {"train_a", "train_b"}   # train_c absent (invariant 1)
    a = doc["entrypoints"]["train_a"]
    assert a == {"est_minutes": 46, "n": 5, "actual_median": 30.0, "actual_p90": 46.0,
                 "low_confidence": False}                      # p90 of [10..50] = 46, ceil -> 46
    b = doc["entrypoints"]["train_b"]
    assert b == {"est_minutes": 24, "n": 2, "actual_median": 20.0, "actual_p90": 24.0,
                 "low_confidence": True}                       # n=2 < 5

    golden = FIX / "derive_basic.json"
    if os.environ.get("REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    assert doc == json.loads(golden.read_text())


def test_derive_ceil_rounds_up_the_safe_direction():
    """est is ceil(p90), never floor -- under-estimation is the asymmetric-costly failure."""
    # single sample -> p90 == that value; a fractional minute must round UP.
    doc = est.derive_defaults([_ta("e", 12.4)], min_sample=1)
    assert doc["entrypoints"]["e"]["est_minutes"] == 13   # ceil(12.4)
    assert doc["entrypoints"]["e"]["low_confidence"] is False  # n=1 >= min_sample 1


def test_derive_est_minimum_one():
    doc = est.derive_defaults([_ta("e", 0.0)], min_sample=1)   # degenerate floor is separate; guard >=1
    assert doc["entrypoints"]["e"]["est_minutes"] == 1


def test_derive_empty_is_valid():
    doc = est.derive_defaults([], min_sample=5)
    assert doc["entrypoints"] == {} and doc["_meta"]["generated_task_count"] == 0


# --------------------------------------------------------------------------- pure: resolve/load

def test_resolve_explicit_overrides():
    assert est.resolve_est_minutes(120, "train_a", lambda e: 46) == (120, "explicit")


def test_resolve_learned_when_omitted():
    assert est.resolve_est_minutes(None, "train_a", lambda e: 46) == (46, "learned")


def test_resolve_errors_when_no_value():
    with pytest.raises(ValueError):
        est.resolve_est_minutes(None, "unknown", lambda e: None)


def test_resolve_rejects_nonpositive_explicit():
    with pytest.raises(ValueError):
        est.resolve_est_minutes(0, "train_a", lambda e: 46)


def test_load_default_reads_sidecar():
    p = FIX / "sidecar.json"
    assert est.load_default("train_p", p) == 46
    assert est.load_default("train_q", p) == 24
    assert est.load_default("unknown", p) is None            # missing entry -> None


def test_load_default_missing_file_is_none():
    assert est.load_default("train_p", FIX / "does_not_exist.json") is None


def test_load_default_malformed_is_none():
    """Invariant 12: untrusted sidecar -- a non-int entry yields None, not a type error."""
    assert est.load_default("train_p", FIX / "sidecar_malformed.json") is None


# --------------------------------------------------------------------------- golden end-to-end

def _seed(path):
    conn = reg.connect(str(path))

    def task(tid, ep, state):
        conn.execute(
            "INSERT INTO tasks(id,created_at,created_by,grp,name,entrypoint,args_json,config_json,"
            "config_hash,arm_hash,git_sha,slots,est_minutes,priority,state,retries_used,max_retries,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "2026-07-13T00:00:00Z", "t", "g", tid, ep, "[]", "{}", tid, tid, "sha",
             1, 99, 50, state, 0, 3, "2026-07-13T00:00:00Z"))

    def run(tid, ep, minutes, state="done"):
        task(tid, ep, state)
        hh, mm = divmod(minutes, 60)
        end = f"2026-07-13T{hh:02d}:{mm:02d}:00Z"
        conn.execute("INSERT INTO events(t,task_id,instance_id,event,detail) VALUES(?,?,?,?,?)",
                     ("2026-07-13T00:00:00Z", tid, 1, "start", ""))
        conn.execute("INSERT INTO events(t,task_id,instance_id,event,detail) VALUES(?,?,?,?,?)",
                     (end, tid, 1, "done" if state == "done" else "task_failed", ""))

    for i, m in enumerate((10, 20, 30, 40, 50)):
        run(f"P{i}", "train_p", m)          # n=5, not low-conf
    run("PD", "train_p", 1)                 # degenerate (<2min) -> excluded
    run("Q0", "train_q", 15); run("Q1", "train_q", 25)   # n=2 -> low-conf
    run("R0", "train_r", 40, state="task_failed")        # excluded (not done)
    conn.commit()
    conn.close()


def test_golden_end_to_end(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed(db)
    out = tmp_path / "est_defaults.json"
    rc = est.main(["--db", str(db), "--out", str(out), "--min-sample", "5"])
    assert rc == 0

    produced = out.read_text()
    golden = FIX / "basic.est_defaults.json"
    if os.environ.get("REGEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(produced)
    assert produced == golden.read_text()

    doc = json.loads(produced)
    assert doc["entrypoints"]["train_p"]["est_minutes"] == 46
    assert doc["entrypoints"]["train_q"]["low_confidence"] is True
    assert "train_r" not in doc["entrypoints"]              # task_failed-only -> absent
    assert doc["_meta"]["generated_from"] == "runs.sqlite" and doc["_meta"]["since"] is None


def test_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed(db)
    out = tmp_path / "est_defaults.json"
    rc = est.main(["--db", str(db), "--out", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists()                                 # invariant 7


def test_determinism(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed(db)
    o1, o2 = tmp_path / "a.json", tmp_path / "b.json"
    est.main(["--db", str(db), "--out", str(o1)])
    est.main(["--db", str(db), "--out", str(o2)])
    assert o1.read_bytes() == o2.read_bytes()               # invariant 6


def test_read_only_never_writes_registry(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed(db)
    mtime0 = db.stat().st_mtime_ns
    est.main(["--db", str(db), "--out", str(tmp_path / "est_defaults.json")])
    assert db.stat().st_mtime_ns == mtime0                  # invariant 9


# ---------------------------------------------------------------- invariant 13: in-flight arm

def test_learn_group_estimates_keys_on_group_not_entrypoint():
    """The measured reason this arm exists (2026-07-31): ONE entrypoint
    (`native.training.m49_curriculum_ab`) spanned 270 groups whose runtimes ran p10 6 min -> median 33
    -> p90 182, so a single per-entrypoint p90 is 5.5x the median task and 29x the p10 task. Keying
    on (entrypoint, group) keeps a short campaign's estimate short while a long one stays long."""
    ep = "native.training.m49_curriculum_ab"
    rows = ([(ep, "short_campaign", m) for m in (5, 6, 7, 8)]
            + [(ep, "long_campaign", m) for m in (180, 190, 200, 210)])
    learned = est.learn_group_estimates(rows)
    assert learned[(ep, "short_campaign")] <= 8
    assert learned[(ep, "long_campaign")] >= 200
    # the two campaigns share an entrypoint and MUST NOT collapse to one number
    assert learned[(ep, "short_campaign")] != learned[(ep, "long_campaign")]


def test_learn_group_estimates_needs_min_sample_and_uses_p90_not_median():
    ep = "e"
    # 2 samples < LIVE_MIN_SAMPLE(3): not enough to re-estimate a whole campaign off
    assert est.learn_group_estimates([(ep, "g", 10), (ep, "g", 12)]) == {}
    # p90, not median — under-estimation causes hard-cap eviction, so the loop is asymmetric
    learned = est.learn_group_estimates([(ep, "g", m) for m in (10, 10, 10, 100)])
    assert learned[(ep, "g")] > 10, "a p50 statistic would have returned 10 and evicted the tail"


def test_learn_group_estimates_ignores_nonpositive_and_missing_durations():
    """A task with no `start`/`done` pair reconstructs to None; a clock skew can yield <= 0. Neither
    may poison a campaign's estimate."""
    rows = [("e", "g", None), ("e", "g", 0), ("e", "g", -5),
            ("e", "g", 20), ("e", "g", 22), ("e", "g", 24)]
    learned = est.learn_group_estimates(rows)
    assert 20 <= learned[("e", "g")] <= 25
