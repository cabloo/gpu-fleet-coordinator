"""⛔ INVARIANT 4h — a task may declare it needs a REAL GPU, and BOTH placement paths must honour it.

WHY IT EXISTS, measured rather than anticipated (`madrona_tau1_n3`, 2026-09-04). All four cells of a
3-seed escalation were packed onto owned box `-3` (`tower`), which has no NVIDIA driver. Each
built Madrona successfully — clone 52s, cmake 14s, make 31s — and then died at the smoke import on
`ImportError: libcuda.so.1`.

⚠ AND IT WAS STRUCTURAL, NOT BAD LUCK:
  * placement packs OWNED-FIRST because owned boxes are free, and
  * BOTH live owned boxes carry `gpu_name IS NULL` (`-3 tower`, `-2 desktop`); the only
    GPU-bearing owned box (`-1 laptop-gpu`) is `unreachable`.
So every pixel-bed cell landed there whenever tower was idle. The same bed's earlier runs had
succeeded only because it was busy. `vram_gb` cannot express this: it sizes a footprint that every
owned box satisfies trivially.

⛔⛔ THE TEST THAT MATTERS MOST IS THE INERTNESS ONE. This filter sits in `_boardable`, the single
predicate every capacity branch of `place()` agrees on, so a mistake here mis-places EVERY job on the
fleet — not just the one that opted in.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DISP = ROOT / "fleet" / "dispatcher.py"


def _load(name, relpath):
    """The repo's canonical loader for these scripts (copied from
    `test_dispatcher_api_failure_is_not_emptiness.py`). ⚠ The `sys.modules[name] = mod` line is
    load-bearing, not tidiness: `dispatcher.py` defines `@dataclass`es, and `dataclasses` resolves
    `cls.__module__` through `sys.modules` — omitting it raises
    `AttributeError: 'NoneType' object has no attribute '__dict__'` at import."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


D = _load("dispatcher_requires_gpu", "fleet/dispatcher.py")

GPU_BOX = {"id": 40000056, "gpu_name": "RTX 3060", "label": "runq_x"}
OWNED_NOGPU = {"id": -3, "gpu_name": None, "label": "tower"}       # the box that failed
OWNED_NOGPU2 = {"id": -2, "gpu_name": "", "label": "desktop"}             # empty, not just None
INSTANCES = [OWNED_NOGPU, OWNED_NOGPU2, GPU_BOX]


def _task(hint=None, tid="t1"):
    return {"id": tid, "resource_hint": hint or {}}


# ---- the predicate itself ----------------------------------------------------------------------

def test_requires_gpu_defaults_off_so_every_existing_task_is_unaffected():
    assert D.requires_gpu(None) is False
    assert D.requires_gpu({}) is False
    assert D.requires_gpu({"vram_gb": 8}) is False


def test_instance_has_gpu_is_FAIL_CLOSED_on_missing_or_blank():
    assert D.instance_has_gpu(GPU_BOX) is True
    assert D.instance_has_gpu(OWNED_NOGPU) is False           # None
    assert D.instance_has_gpu(OWNED_NOGPU2) is False          # empty string
    assert D.instance_has_gpu({"gpu_name": "   "}) is False   # whitespace only
    assert D.instance_has_gpu({}) is False                    # absent entirely


# ---- the PACK path (_boardable), which is where the failure actually happened -------------------

def test_a_gpu_task_cannot_board_the_box_that_broke_madrona():
    boardable = D._boardable(_task({"requires_gpu": True}), INSTANCES)
    ids = [i["id"] for i in boardable]
    assert -3 not in ids, "the renderer-less owned box is still boardable — 4h does not hold"
    assert -2 not in ids
    assert ids == [40000056]


def test_an_ORDINARY_task_still_boards_everything_INERTNESS_CHECK():
    """⛔ THE MOST IMPORTANT TEST HERE. `_boardable` is shared by every capacity branch, so a filter
    that leaks to non-opted-in tasks would mis-place the whole fleet, not just pixel beds."""
    for hint in (None, {}, {"vram_gb": 8}, {"requires_gpu": False}):
        boardable = D._boardable(_task(hint), INSTANCES)
        assert boardable == INSTANCES, (
            f"a task with hint={hint!r} lost boardable instances — the GPU filter is not inert for "
            f"tasks that never asked for it")


def test_it_composes_with_a_box_target_rather_than_overriding_it():
    """A box target is more specific and still wins, but must not smuggle in a GPU-less box."""
    t = _task({"requires_gpu": True, "box": -3})
    assert D._boardable(t, INSTANCES) == [], (
        "a GPU-requiring task was allowed onto its named box despite that box having no GPU; the "
        "filter must apply BEFORE the box target, or `--box` becomes an escape hatch")
    t_ok = _task({"requires_gpu": True, "box": 40000056})
    assert [i["id"] for i in D._boardable(t_ok, INSTANCES)] == [40000056]


# ---- the RENT path, because guarding one path only is the original 4f incident ------------------

def test_the_rent_path_refuses_a_gpuless_offer_and_is_inert_otherwise():
    gpu_offer = {"gpu_name": "RTX 4070", "dph_total": 0.1}
    bare_offer = {"dph_total": 0.02}
    assert D.gpu_allowed(gpu_offer, {"requires_gpu": True}) is True
    assert D.gpu_allowed(bare_offer, {"requires_gpu": True}) is False
    # inert when not requested — a plain task may still rent a GPU-less offer
    assert D.gpu_allowed(bare_offer, None) is True
    assert D.gpu_allowed(bare_offer, {}) is True


def test_BOTH_paths_are_actually_wired_not_merely_defined():
    """⛔ A predicate nobody calls is decoration. `cpu_name_include` shipped guarding the rent path
    alone and the targeted cell promptly packed onto the wrong box."""
    src = DISP.read_text()
    assert "gpu_allowed(o, task.get(\"resource_hint\"))" in src, (
        "gpu_allowed is defined but never called on the rent path")
    b = src[src.index("def _boardable("):]
    b = b[:b.index("\ndef ")]
    assert "requires_gpu(hint)" in b and "instance_has_gpu(i)" in b, (
        "_boardable does not apply the GPU filter, so the PACK path — the one that actually "
        "mis-placed madrona_tau1_n3 — is unguarded")


# ---- the plumbing that lets a config reach the dispatcher ---------------------------------------

JM = _load("job_manifest_requires_gpu", "fleet/job_manifest.py")

_BASE = {"manifest_version": 1, "run": ["python", "x.py"], "completion_artifact": "results.json"}


def _manifest(**resources):
    return dict(_BASE, resources=dict(resources))


def test_a_manifest_that_sets_requires_gpu_TRUE_actually_PARSES():
    """⛔⛔ THE TEST THAT SHOULD HAVE EXISTED FIRST, AND DID NOT.

    The original version of this check grepped `job_manifest.py` for the STRING "requires_gpu" and
    passed — while the field was in the NUMERIC allowlist, whose validator explicitly rejects bools
    (`not isinstance(..., bool)`). So every config setting it was refused with "resources.requires_gpu
    must be a number", four queued cells bounced, and the broken version had already reached master.

    ⇒ A check that greps for a name verifies that someone TYPED the name. Parse the thing.
    """
    m = JM.parse(_manifest(requires_gpu=True, vram_gb=8, cores=8))
    assert m.resources["requires_gpu"] is True


def test_requires_gpu_false_and_absent_both_parse_and_stay_falsey():
    assert JM.parse(_manifest(requires_gpu=False)).resources["requires_gpu"] is False
    assert "requires_gpu" not in JM.parse(_manifest(vram_gb=8)).resources
    # and the dispatcher agrees with the manifest about what those mean
    assert D.requires_gpu(JM.parse(_manifest(requires_gpu=False)).resources) is False
    assert D.requires_gpu(JM.parse(_manifest(vram_gb=8)).resources) is False


def test_a_NON_boolean_requires_gpu_is_REJECTED_with_a_useful_message():
    """It is a flag, not a count. `requires_gpu: 1` would be a typo worth catching, not a synonym."""
    for bad in (1, 0, "true", "yes", 1.0, [], {}):
        with pytest.raises(Exception) as ei:
            JM.parse(_manifest(requires_gpu=bad))
        assert "requires_gpu" in str(ei.value) and "bool" in str(ei.value).lower(), (
            f"requires_gpu={bad!r} was rejected with an unhelpful message: {ei.value}")


def test_the_numeric_resources_still_reject_a_bool_which_is_why_4h_needed_its_own_branch():
    """Pins the reason the shared loop could not simply be extended: it guards against `slots: True`.

    If this ever starts passing, the numeric guard has been loosened and `requires_gpu` could have
    ridden along after all — but so could a boolean lane count."""
    with pytest.raises(Exception):
        JM.parse(_manifest(slots=True))


def test_runq_forwards_requires_gpu_into_the_resource_hint():
    """runq copies a SUBSET of resource keys into `resource_hint`; a field missing from that list is
    silently dropped, so the task places exactly as before while the config looks correct."""
    src = (ROOT / "fleet" / "runq.py").read_text()
    i = src.index('for k in ("max_dph"')
    forwarded = src[i:src.index("\n", src.index("]", i))]
    assert "requires_gpu" in forwarded, (
        "runq does not forward requires_gpu into resource_hint, so the dispatcher never sees it")
