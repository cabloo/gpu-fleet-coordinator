"""Sweep supervisor — golden decision traces from docs/specs/sweep-supervisor.spec.md."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "sweep"
_spec = importlib.util.spec_from_file_location(
    "sweep_supervisor", ROOT / "fleet" / "sweep_supervisor.py")
sup = importlib.util.module_from_spec(_spec)
sys.modules["sweep_supervisor"] = sup  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(sup)


def _rows(path):
    return [json.loads(l) for l in (FIX / path).read_text().splitlines() if l.strip()]


GATES = dict(sup.DEFAULT_GATES)


class TestGates:
    def test_entropy_death_fires_only_after_two_subfloor_rows_past_threshold(self):
        rows = _rows("entropy_death.jsonl")
        ok = sup.LaneView("x", rows[:3])  # 10k row: prev row is 0.05 >= floor
        assert sup.decide_gates(ok, GATES) is None
        dead = sup.LaneView("x", rows)  # 12.5k: last two rows 0.010, 0.008 < 0.02
        d = sup.decide_gates(dead, GATES)
        assert d and d.event == "gate_kill" and "entropy" in d.reason
        assert d.env_steps == 12500

    def test_entropy_gate_ignores_exploit_phase(self):
        rows = _rows("entropy_death.jsonl")
        rows[-1]["phase"] = "exploit"
        assert sup.decide_gates(sup.LaneView("x", rows), GATES) is None

    def test_stall(self):
        rows = _rows("stall.jsonl")
        lane = sup.LaneView("x", rows, process_alive=True, last_row_age_min=45)
        d = sup.decide_gates(lane, GATES)
        assert d and d.event == "gate_kill" and "stall" in d.reason
        fresh = sup.LaneView("x", rows, process_alive=True, last_row_age_min=5)
        assert sup.decide_gates(fresh, GATES) is None

    def test_nan_kills(self):
        rows = [{"env_steps": 2500, "det_norm": float("nan"), "ac_entropy": 0.3,
                 "phase": "learn"}]
        d = sup.decide_gates(sup.LaneView("x", rows), GATES)
        assert d and d.event == "gate_kill" and "NaN" in d.reason


class TestRungs:
    def test_rung_prune_bottom_half(self):
        fx = json.loads((FIX / "rung_prune.json").read_text())
        engine = sup.RungEngine([fx["rung"]])
        views = {n: sup.LaneView(n, rows) for n, rows in fx["lanes"].items()}
        kills = engine.check(views, {})
        assert {n for n, _ in kills} == {"c", "d"}
        assert all(d.event == "rung_kill" for _, d in kills)

    def test_transient_not_protected_by_early_peak(self):
        fx = json.loads((FIX / "transient_trap.json").read_text())
        engine = sup.RungEngine([fx["rung"]])
        views = {n: sup.LaneView(n, rows) for n, rows in fx["lanes"].items()}
        kills = engine.check(views, {})
        assert {n for n, _ in kills} == {"transient"}

    def test_unresolved_until_all_incumbents_report(self):
        fx = json.loads((FIX / "rung_prune.json").read_text())
        engine = sup.RungEngine([fx["rung"]])
        views = {n: sup.LaneView(n, rows) for n, rows in fx["lanes"].items()}
        views["straggler"] = sup.LaneView("straggler", [
            {"env_steps": 2500, "det_norm": 0.0, "ac_entropy": 0.3, "phase": "learn"}])
        assert engine.check(views, {}) == []  # straggler hasn't reached the rung

    def test_late_joiner_vs_bar(self):
        fx = json.loads((FIX / "late_joiner.json").read_text())
        engine = sup.RungEngine([fx["rung"]])
        step = fx["rung"]["env_steps"]
        views = {n: sup.LaneView(n, [{"env_steps": step, "det_norm": m,
                                      "ac_entropy": 0.3, "phase": "learn"}])
                 for n, m in fx["incumbents"].items()}
        killed_now = {n for n, _ in engine.check(views, {})}
        terminal = {n: "rung_kill" for n in killed_now}
        for metric, expect_killed in ((fx["joiner_alive_metric"], False),
                                      (fx["joiner_killed_metric"], True)):
            name = f"joiner_{metric}"
            views[name] = sup.LaneView(name, [{"env_steps": step, "det_norm": metric,
                                               "ac_entropy": 0.3, "phase": "learn"}])
            kills = {n for n, _ in engine.check(views, terminal)}
            assert (name in kills) == expect_killed
            terminal.update({n: "rung_kill" for n in kills})

    def test_ties_keep_both(self):
        engine = sup.RungEngine([{"env_steps": 5000, "keep_fraction": 0.5}])
        views = {n: sup.LaneView(n, [{"env_steps": 5000, "det_norm": m,
                                      "ac_entropy": 0.3, "phase": "learn"}])
                 for n, m in {"a": 0.03, "b": 0.01, "c": 0.01, "d": -0.02}.items()}
        kills = {n for n, _ in engine.check(views, {})}
        assert kills == {"d"}  # c ties b at the cut -> kept


class TestAutoSlots:
    def test_should_launch_cases(self):
        for case in json.loads((FIX / "auto_slots.json").read_text())["cases"]:
            got = sup.should_launch(case["hw"], case["vram_per_lane_max"], case["n_live"],
                                    case["since_launch_min"], sup.AUTO_DEFAULTS)
            assert list(got) == case["expect"], case["name"]


class TestRegistryAndValidation:
    def test_registry_replay_no_duplicate_terminals(self, tmp_path):
        reg = sup.Registry(tmp_path / "registry.jsonl")
        reg.append("x", "start", "args")
        reg.append("x", "gate_kill", "entropy death")
        reg2 = sup.Registry(tmp_path / "registry.jsonl")  # restart
        reg2.append("x", "gate_kill", "entropy death")  # replayed decision
        events = [json.loads(l) for l in (tmp_path / "registry.jsonl").read_text().splitlines()]
        assert [e["event"] for e in events] == ["start", "gate_kill"]

    def _sweep(self, tmp_path, **over):
        base = {"slots": 2, "rungs": [], "queue": [{"name": "a", "args": []}]}
        base.update(over)
        p = tmp_path / "sweep.json"
        p.write_text(json.dumps(base))
        return str(p)

    def test_unknown_key_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            sup.load_sweep(self._sweep(tmp_path, bogus=1))

    def test_duplicate_names_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            sup.load_sweep(self._sweep(
                tmp_path, queue=[{"name": "a", "args": []}, {"name": "a", "args": []}]))

    def test_slots_auto_accepted_and_gate_defaults_filled(self, tmp_path):
        sweep = sup.load_sweep(self._sweep(tmp_path, slots="auto"))
        assert sweep["gates"] == sup.DEFAULT_GATES and sweep["auto"] == sup.AUTO_DEFAULTS

    def test_bad_rungs_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            sup.load_sweep(self._sweep(
                tmp_path, rungs=[{"env_steps": 10, "keep_fraction": 0.5},
                                 {"env_steps": 5, "keep_fraction": 0.5}]))
