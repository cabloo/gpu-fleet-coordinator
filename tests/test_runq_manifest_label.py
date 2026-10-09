"""`tasks.entrypoint` names the SCRIPT, never an argument value.

The column's docstring called itself display-only ("used only for display in `runq ls`"), so its
fallback was `run[-1]` — the last argv token. For every script-style manifest that is an argument
VALUE, not the script: `--out .` labelled the task `.`, `--batch 32` labelled it `32`.

It is not display-only. `est_defaults.py` GROUPS BY this column to derive the learned per-entrypoint
`est_minutes` that `runq add` uses whenever `--est-minutes` is omitted — i.e. the number the
dispatcher packs and preempts against. Measured on the live registry when this was found: 287 of
5620 rows keyed on argument values (`0`, `0.05`, `256`, `32`, `.`), including 20 cells of
`survival_vision_coord_convergence.py` all collapsed under `.`. That fragments a real script's
timing samples across its arguments AND injects junk entrypoints into the calibration table.

Same failure family as this repo's "a knob nothing can search" rule, inverted: a field documented as
cosmetic was load-bearing somewhere else, so nobody guarded it.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "fleet"))

import runq  # noqa: E402


@pytest.mark.parametrize("run,expect", [
    # module invocations — the pre-existing behaviour, must not regress
    (["python", "-m", "native.training.m49_curriculum_ab"], "native.training.m49_curriculum_ab"),
    (["python3", "-m", "azsc.train", "--config", "x.json"], "azsc.train"),
    # script invocations — every one of these used to return the LAST token
    (["python", "scripts/diagnostics/survival_vision_coord_convergence.py",
      "--steps", "30000", "--out", "."], "scripts/diagnostics/survival_vision_coord_convergence.py"),
    (["python", "scripts/diagnostics/probe.py", "--batch", "32"],
     "scripts/diagnostics/probe.py"),
    (["python", "scripts/x.py", "--lr", "0.05"], "scripts/x.py"),
    (["bash", "scripts/run.sh", "--n", "256"], "scripts/run.sh"),
    # degenerate
    ([], "job"),
])
def test_label_is_the_script_never_an_argument(run, expect):
    assert runq._manifest_label(run) == expect


@pytest.mark.parametrize("run", [
    ["python", "scripts/diagnostics/survival_vision_coord_convergence.py",
     "--steps", "30000", "--out", "."],
    ["python", "scripts/diagnostics/probe.py", "--batch", "32"],
    ["python", "scripts/x.py", "--lr", "0.05"],
])
def test_label_is_never_a_bare_number_or_dot(run):
    """The concrete corruption signature seen in the registry."""
    label = runq._manifest_label(run)
    assert label not in (".", "32", "0.05", "256", "0")
    assert not label.replace(".", "").isdigit(), f"label {label!r} is a bare number"


def test_label_is_stable_across_differing_arguments():
    """The property calibration actually needs: same script, different args => SAME group key.

    This is what `run[-1]` broke — one script's samples were split across every value it was
    ever called with, so no entrypoint reached `min_sample` and the learned table missed it.
    """
    base = ["python", "scripts/diagnostics/probe.py"]
    labels = {
        runq._manifest_label(base + ["--batch", "32"]),
        runq._manifest_label(base + ["--batch", "256"]),
        runq._manifest_label(base + ["--out", "."]),
        runq._manifest_label(base),
    }
    assert len(labels) == 1, f"same script grouped under {len(labels)} keys: {labels}"


# --------------------------------------------------------------------- est sanity warning

class _Manifest:
    def __init__(self, run, resources):
        self.run = run
        self.resources = resources


class _Args:
    est_minutes = None
    vram_per_lane_gb = None
    cores_per_lane = None


def _add(monkeypatch, declared, learned, run=None):
    """Run the manifest est resolver with a stubbed learned table; return the resolved est."""
    monkeypatch.setattr(runq.est_defaults, "load_default", lambda label, path=None: learned)
    m = _Manifest(run or ["python", "-m", "native.training.m49_curriculum_ab"],
                  {"est_minutes": declared})
    est, _hint, err = runq._manifest_est_and_hint(_Args(), m)
    assert err is None
    return est


def test_over_declared_est_warns_and_does_not_change_the_value(monkeypatch, capsys):
    """The m99 case: 2200 declared against a 319 learned p90."""
    est = _add(monkeypatch, declared=2200, learned=319)
    err = capsys.readouterr().err
    assert "WARNING" in err and "6.9x the learned p90" in err
    assert est == 2200, "the warning must NOT silently rewrite the declared estimate"


def test_under_declared_est_warns(monkeypatch, capsys):
    est = _add(monkeypatch, declared=45, learned=104)
    err = capsys.readouterr().err
    assert "WARNING" in err and "BELOW the learned p90" in err
    assert est == 45


def test_est_within_tolerance_is_silent(monkeypatch, capsys):
    _add(monkeypatch, declared=350, learned=319)
    assert capsys.readouterr().err == ""


def test_no_learned_default_is_silent(monkeypatch, capsys):
    _add(monkeypatch, declared=2200, learned=None)
    assert capsys.readouterr().err == ""


def test_a_broken_sidecar_never_blocks_an_add(monkeypatch, capsys):
    def boom(label, path=None):
        raise RuntimeError("sidecar unreadable")
    monkeypatch.setattr(runq.est_defaults, "load_default", boom)
    m = _Manifest(["python", "-m", "native.x"], {"est_minutes": 900})
    est, _hint, err = runq._manifest_est_and_hint(_Args(), m)
    assert err is None and est == 900
