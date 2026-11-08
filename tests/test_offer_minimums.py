"""⛔ INVARIANT 4j — a task may declare the minimum driver CUDA version (`min_cuda`) and card architecture
(`min_compute_cap`) a box must have, and BOTH placement paths must honour it.

WHY IT EXISTS, measured: a large-model job built against CUDA 13 rented a card whose driver supported only
CUDA 12.x. torch found no usable GPU, the job loaded the model into host memory instead, and it was killed
for memory within minutes. Vast publishes the driver's CUDA version (`cuda_max_good`) and the card's
architecture (`compute_cap`) on every offer, so placement can refuse such a box.

The pack half: a task with minimums boards the box rented for it, or a box whose probe reads a driver and a
card that meet them (`instance_meets_minimums`). A box the probe has not read is refused, so the 4f regressions
below still hold for every unmeasured box.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, relpath):
    """The repo's canonical loader (see `test_vast_requires_gpu.py`): `sys.modules[name] = mod` is
    load-bearing, because `dataclasses` resolves `cls.__module__` through `sys.modules`."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


D = _load("dispatcher_offer_minimums", "fleet/dispatcher.py")
JM = _load("job_manifest_offer_minimums", "fleet/job_manifest.py")

SETTINGS = dict(D.DEFAULT_SETTINGS)
SETTINGS.update({
    "max_hourly_usd": 1.00, "balance_floor_usd": 3.00, "max_instance_dph": 0.08,
    "idle_timeout_min": 10, "rent_patience_min": 15, "backlog_min_tasks": 3,
    "backlog_min_task_minutes": 60, "preempt_priority_margin": 30,
    "vram_per_lane_gb": 0.6, "cores_per_lane": 1, "max_slots_cap": 8,
    "est_safety": 1.25, "pull_margin_min": 10,
    "current_rate": 0.0, "current_balance": 25.0, "deny_machine_ids": set(),
})


def _offer(oid, dph, cuda, cap, machine):
    return {"id": oid, "dph_total": dph, "gpu_name": "RTX A4000", "gpu_ram_gb": 16.0,
            "cpu_cores_effective": 12.0, "ram_gb": 48.0, "machine_id": machine, "reliability": 0.99,
            "cpu_ghz": 4.0, "cpu_name": "AMD Ryzen 9 5900X 12-Core Processor",
            "cuda_max_good": cuda, "compute_cap": cap}


OLD_DRIVER = _offer(1, 0.05, 12.4, 860, 11)     # the box that failed: cheapest, driver too old
OLD_CARD = _offer(2, 0.06, 13.0, 700, 12)       # a new driver on a card CUDA 13 no longer builds for
GOOD = _offer(3, 0.09, 13.0, 860, 13)
TASK = {"id": "t", "slots": 1, "est_minutes": 20, "priority": 90, "retries_used": 0, "max_retries": 1}


def _task(hint):
    return dict(TASK, resource_hint=hint)


def _box(label, iid=900):
    return {"id": iid, "state": "live", "slots_total": 8, "gpu_name": "RTX A4000", "dph_usd": 0.09,
            "minutes_to_hard_cap": 600, "source": "vast", "tasks": [], "label": label}


# ---- the predicates -----------------------------------------------------------------------------

def test_absent_or_malformed_minimums_set_nothing_so_every_existing_task_is_unaffected():
    for hint in (None, {}, {"vram_gb": 8}, {"min_cuda": None}, {"min_cuda": True}, {"min_cuda": "13"},
                 {"min_cuda": 0}, {"min_cuda": -1}, {"min_compute_cap": [750]}, {"min_cuda": float("nan")},
                 {"min_cuda": float("inf")}):
        assert D.offer_minimums(hint) == {}, hint
        assert D.offer_meets_minimums(OLD_DRIVER, hint) is True
        assert D.offer_meets_minimums({}, hint) is True


def test_minimums_compare_the_published_fields():
    hint = {"min_cuda": 13.0, "min_compute_cap": 750}
    assert D.offer_minimums(hint) == {"cuda_max_good": 13.0, "compute_cap": 750.0}
    assert D.offer_meets_minimums(GOOD, hint) is True
    assert D.offer_meets_minimums(OLD_DRIVER, hint) is False
    assert D.offer_meets_minimums(OLD_CARD, hint) is False
    assert D.offer_meets_minimums(dict(GOOD, cuda_max_good=13.2), hint) is True     # above the minimum
    assert D.offer_meets_minimums(OLD_CARD, {"min_cuda": 13.0}) is True             # only what is set


def test_an_offer_that_does_not_publish_the_field_is_REFUSED():
    """FAIL-CLOSED, like `cpu_name_allowed`: the task would rather hold than run on a box it cannot use."""
    for missing in ({k: v for k, v in GOOD.items() if k != "cuda_max_good"}, dict(GOOD, cuda_max_good=None),
                    dict(GOOD, cuda_max_good="13.0"), dict(GOOD, cuda_max_good=True)):
        assert D.offer_meets_minimums(missing, {"min_cuda": 13.0}) is False


def test_eligible_offers_carries_both_fields_from_the_raw_offer():
    raw = [{"id": 5, "dph_total": 0.1, "gpu_name": "RTX 3090", "gpu_ram": 24576, "cpu_cores_effective": 8,
            "reliability2": 0.99, "cuda_max_good": 13.2, "compute_cap": 860}]
    mapped, dropped = D.eligible_offers(raw, SETTINGS)
    assert dropped == 0
    assert mapped[0]["cuda_max_good"] == 13.2 and mapped[0]["compute_cap"] == 860


# ---- the RENT path ------------------------------------------------------------------------------

def test_place_rents_a_box_that_can_run_the_task_over_a_cheaper_one_that_cannot():
    offers = [OLD_DRIVER, OLD_CARD, GOOD]
    ordinary = _task({"max_dph": 0.40})
    p1 = D.place(ordinary, [], offers, [ordinary] * 3, SETTINGS, 0)
    assert p1.action == "rent" and p1.offer["id"] == 1, "the control: the cheapest offer wins untargeted"

    driver_only = _task({"max_dph": 0.40, "min_cuda": 13.0})
    p2 = D.place(driver_only, [], offers, [driver_only] * 3, SETTINGS, 0)
    assert p2.action == "rent" and p2.offer["id"] == 2

    both = _task({"max_dph": 0.40, "min_cuda": 13.0, "min_compute_cap": 750})
    p3 = D.place(both, [], offers, [both] * 3, SETTINGS, 0)
    assert p3.action == "rent" and p3.offer["id"] == 3


def test_no_qualifying_offer_means_HOLD_never_a_rent_of_the_wrong_box():
    ordinary = _task({"max_dph": 0.40})
    assert D.place(ordinary, [], [OLD_DRIVER], [ordinary] * 3, SETTINGS, 0).action == "rent"   # the control
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    p = D.place(t, [], [OLD_DRIVER], [t] * 3, SETTINGS, 0)
    assert p.action == "hold" and "no_offer" in p.reason, f"rented a box below the task's minimums: {p}"


# ---- the PACK path ------------------------------------------------------------------------------

def test_a_task_with_minimums_refuses_a_box_it_did_not_rent():
    """4f regression 1 on this axis: another task's box that the probe has not read is unverifiable.
    The ordinary task packs onto it (the control); the one with minimums rents its own."""
    someone_elses = [_box("runq_OTHER-TASK")]
    ordinary = _task(None)
    assert D.place(ordinary, someone_elses, [GOOD], [ordinary] * 3, SETTINGS, 0).action == "pack"
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    p = D.place(t, someone_elses, [GOOD], [t] * 3, SETTINGS, 0)
    assert p.action == "rent" and p.offer["id"] == 3


def test_a_task_with_minimums_DOES_pack_onto_the_box_it_rented():
    """4f regression 2 on this axis: renting is how the box comes to exist, and the task boards it
    through the pack path on a later poll. Refusing that would rent a box every poll."""
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    p = D.place(t, [_box("runq_t")], [GOOD], [t] * 3, SETTINGS, 0)
    assert p.action == "pack" and p.target == 900


def test_a_task_with_minimums_does_not_wait_for_someone_elses_provisioning_box():
    """4f regression 3 on this axis: another task's incoming box is not incoming capacity for a task
    that can never board it, so it rents instead of holding behind it."""
    coming = dict(_box("runq_SOMEONE-ELSE"), state="provisioning")
    ordinary = _task({"max_dph": 0.40})
    p_ord = D.place(ordinary, [coming], [GOOD], [ordinary] * 3, SETTINGS, 0)
    assert p_ord.action == "hold" and "awaiting_provisioning" in p_ord.reason      # the control
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    p = D.place(t, [coming], [GOOD], [t] * 3, SETTINGS, 0)
    assert p.action == "rent", f"held on a box it can never board: {p.reason}"


def test_boardable_is_INERT_for_every_task_without_minimums():
    """⛔ `_boardable` is shared by every capacity branch, so a leak here mis-places the whole fleet."""
    instances = [_box("runq_OTHER-TASK", 901), _box("tower", -3), _box("runq_t", 900)]
    for hint in (None, {}, {"vram_gb": 8}, {"min_cuda": None}, {"min_cuda": False}, {"min_cuda": 0}):
        assert D._boardable(_task(hint), instances) == instances, hint


def test_a_box_target_still_wins_so_an_owned_box_can_be_named():
    """Naming a box asserts what it can run, so a named box is boarded whatever its probe reads."""
    owned = dict(_box("gpudesktop", -4), source="owned")
    t = _task({"min_cuda": 13.0, "box": "gpudesktop"})
    assert [i["id"] for i in D._boardable(t, [owned, _box("runq_t")])] == [-4]


# ---- what the box probe reads for it ----------------------------------------------------------

_PROBE_BASE = "NPROC 20\nLOAD 2.50 0.20 0.10 1/900 123\nMEM 32000 24000\n"


_ONE_CARD = "GPU 12282, 512, 40, 10, A GPU\n"
_TWO_CARDS = _ONE_CARD * 2


@pytest.mark.parametrize("lines,cuda,cap", [
    (_ONE_CARD + "CUDAV 12.7\nCCAP 8.9\n", 12.7, 890),
    (_ONE_CARD + "CUDAV 13.0\nCCAP 12.0\n", 13.0, 1200),
    (_TWO_CARDS + "CCAP 8.6\nCCAP 7.5\n", None, 750),            # several cards: the weakest
    (_TWO_CARDS + "CCAP 8.9\nCCAP [N/A]\n", None, None),          # one card unread: the field is unread
    (_TWO_CARDS + "CCAP 8.9\n", None, None),                      # a reply cut short: a card is missing
    ("CCAP 8.9\n", None, None),                                   # no card counted: nothing to match
    (_ONE_CARD + "CCAP Failed to initialize NVML: GPU access blocked by the operating system\n", None, None),
    ("CUDAV 565.57.01\n", None, None),                            # a driver version is not a CUDA version
    (_ONE_CARD + "CUDAV N/A\nCCAP 0.0\n", None, None),
    ("CUDAV 0.0\n", None, None),
    ("", None, None),                                             # an older box image: unmeasured
])
def test_the_probe_reads_the_driver_cuda_and_the_card_in_vast_units(lines, cuda, cap):
    m = D.parse_box_probe(_PROBE_BASE + lines)
    assert (m["cuda_max_good"], m["compute_cap"]) == (cuda, cap)
    assert m["cores"] == 20, "the rest of the probe is untouched"


_FAKE_SMI = """#!/bin/sh
case "$*" in
  "") {banner} ;;
  *compute_cap*) {cap} ;;
  *memory.total*) echo "12282, 512, 40, 10, A GPU" ;;
  *) echo "71, 140.25, 175.00, 0x0" ;;
esac
"""
_BANNER = ("echo '+------------------------------------------------------------------------+'; "
           "echo '| NVIDIA-SMI {v}     Driver Version: {v}     CUDA Version: {c}     |'; "
           "echo '|-------------------------------+----------------------+-----------------+'")
_BLOCKED = 'echo "Failed to initialize NVML: GPU access blocked by the operating system"; exit 255'


@pytest.mark.skipif(sys.platform != "linux" or not all(
    shutil.which(t) for t in ("sh", "nproc", "free", "df", "awk", "timeout")), reason="needs a box's shell")
@pytest.mark.parametrize("banner,cap,cuda,ccap", [
    (_BANNER.format(v="565.57.01", c="12.7"), 'echo "8.9"', 12.7, 890),
    (_BANNER.format(v="580.65.06", c="13.0"), 'echo "12.0"', 13.0, 1200),
    (_BLOCKED, _BLOCKED, None, None),
])
def test_the_probe_command_reads_them_from_a_real_shell(tmp_path, banner, cap, cuda, ccap):
    """The probe is one string a box's shell runs: a quoting slip fails on every box at once, and the
    banner says `Version:` twice, so the word before it decides which number is read."""
    tool = tmp_path / "bin" / "nvidia-smi"
    tool.parent.mkdir()
    tool.write_text(_FAKE_SMI.format(banner=banner, cap=cap))
    tool.chmod(0o755)
    done = subprocess.run(["sh", "-c", D.BOX_PROBE_CMD], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, PATH=f"{tool.parent}{os.pathsep}{os.environ['PATH']}"))
    assert done.returncode == 0, done.stderr
    m = D.parse_box_probe(done.stdout)
    assert (m["cuda_max_good"], m["compute_cap"]) == (cuda, ccap), done.stdout
    assert m["cores"] >= 1


_DEAF_HANG = 'trap "" TERM; exec sleep 60'      # slow on every call, and ignores being asked to stop


@pytest.mark.skipif(sys.platform != "linux" or not all(
    shutil.which(t) for t in ("sh", "nproc", "free", "df", "awk", "timeout")), reason="needs a box's shell")
def test_a_driver_that_hangs_on_both_reads_costs_the_pair_one_bounded_wait(tmp_path):
    """Both reads share ONE `timeout -k 2 5`: a driver slow on every call must not cost 7 s per call,
    because the probe is what admission reads and a probe past its limit counts the box unreachable."""
    tool = tmp_path / "bin" / "nvidia-smi"
    tool.parent.mkdir()
    tool.write_text(_FAKE_SMI.format(banner=_DEAF_HANG, cap=_DEAF_HANG))
    tool.chmod(0o755)
    began = time.time()
    done = subprocess.run(["sh", "-c", D.BOX_PROBE_CMD], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, PATH=f"{tool.parent}{os.pathsep}{os.environ['PATH']}"))
    assert done.returncode == 0, done.stderr
    assert time.time() - began < 12, "the pair waited longer than one bounded call"
    assert not [ln for ln in done.stdout.splitlines() if ln.startswith(("CUDAV", "CCAP"))]
    m = D.parse_box_probe(done.stdout)
    assert (m["cuda_max_good"], m["compute_cap"]) == (None, None) and m["cores"] >= 1


# ---- the PACK path on a box the probe has read (owned boxes are never rented) -------------------

def _owned(label, iid, cuda, cap):
    return dict(_box(label, iid), source="owned", dph_usd=0.0, cuda_max_good=cuda, compute_cap=cap)


LAPTOP = _owned("laptop-gpu", -1, 12.7, 890)       # the box that failed: its driver runs CUDA 12.7
GPUDESKTOP = _owned("gpudesktop", -4, 13.0, 1200)


def test_instance_meets_minimums_reads_the_probed_fields_and_refuses_an_unread_box():
    hint = {"min_cuda": 13.0, "min_compute_cap": 750}
    assert D.instance_meets_minimums(GPUDESKTOP, hint) is True
    assert D.instance_meets_minimums(LAPTOP, hint) is False
    assert D.instance_meets_minimums(dict(GPUDESKTOP, compute_cap=700), hint) is False
    assert D.instance_meets_minimums(dict(GPUDESKTOP, compute_cap=None), {"min_cuda": 13.0}) is True
    for unread in (_box("tower", -3), dict(GPUDESKTOP, cuda_max_good=None), None):
        assert D.instance_meets_minimums(unread, hint) is False, unread
    assert D.instance_meets_minimums(_box("tower", -3), None) is True       # no minimums: inert


def test_the_box_fields_are_the_offer_fields():
    """One comparison serves a box and an offer only while the names agree."""
    import inspect
    src = inspect.getsource(D.instance_meets_minimums)
    for field in D.OFFER_MINIMUMS.values():
        assert f'.get("{field}")' in src, field


def test_a_task_with_minimums_boards_an_owned_box_whose_probe_meets_them_and_not_one_below():
    """The incident: a job built for CUDA 13 packed onto an owned laptop whose driver runs 12.7."""
    t = _task({"min_cuda": 13.0, "requires_gpu": True})
    assert [i["id"] for i in D._boardable(t, [LAPTOP, GPUDESKTOP, _box("tower", -3)])] == [-4]
    p = D.place(t, [LAPTOP, GPUDESKTOP], [], [t] * 3, SETTINGS, 0)
    assert p.action == "pack" and p.target == -4


def test_with_only_a_box_below_the_minimum_the_task_HOLDS_rather_than_packing_there():
    ordinary = _task(None)
    assert D.place(ordinary, [LAPTOP], [], [ordinary] * 3, SETTINGS, 0).action == "pack"     # the control
    t = _task({"min_cuda": 13.0})
    p = D.place(t, [LAPTOP], [], [t] * 3, SETTINGS, 0)
    assert p.action == "hold", f"placed on a box below the task's minimums: {p}"


def test_a_task_boards_its_own_rental_before_that_box_has_been_probed():
    """4f regression 2 again: refusing the unread fresh box it rented would rent a box every poll."""
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    fresh = dict(_box("runq_t"), cuda_max_good=None, compute_cap=None)
    p = D.place(t, [fresh], [GOOD], [t] * 3, SETTINGS, 0)
    assert p.action == "pack" and p.target == 900


def test_a_reading_BELOW_the_minimum_refuses_even_the_task_s_own_rental():
    """The rental was vetted on what the offer published; when the box's own driver reads lower, the
    task would fail its GPU check there and burn its retries."""
    t = _task({"max_dph": 0.40, "min_cuda": 13.0})
    below = dict(_box("runq_t"), cuda_max_good=12.4, compute_cap=860)
    assert D._boardable(t, [below]) == []
    assert D.instance_meets_minimums(below, {"min_cuda": 13.0}, unread_passes=True) is False
    assert D.instance_meets_minimums(dict(below, cuda_max_good=None), {"min_cuda": 13.0},
                                     unread_passes=True) is True


def test_a_CPU_targeted_task_with_minimums_still_boards_only_its_own_rental():
    """No box records its CPU, so a probed driver does not make another box verifiable for it; a
    reading below the minimum still refuses that rental."""
    t = _task({"min_cuda": 13.0, "cpu_name_include": ["5900X"]})
    assert [i["id"] for i in D._boardable(t, [GPUDESKTOP, _box("runq_t")])] == [900]
    below = dict(_box("runq_t"), cuda_max_good=12.4)
    assert D._boardable(t, [GPUDESKTOP, below]) == []
    cpu_only = _task({"cpu_name_include": ["5900X"]})
    assert [i["id"] for i in D._boardable(cpu_only, [GPUDESKTOP, below])] == [900], "4f unchanged"


# ---- the plumbing from a job file to the dispatcher ---------------------------------------------

_BASE = {"manifest_version": 1, "run": ["python", "x.py"], "completion_artifact": "results.json"}


def test_a_manifest_that_sets_the_minimums_PARSES():
    m = JM.parse(dict(_BASE, resources={"min_cuda": 13.0, "min_compute_cap": 750, "requires_gpu": True}))
    assert m.resources["min_cuda"] == 13.0 and m.resources["min_compute_cap"] == 750
    assert D.offer_minimums(m.resources) == {"cuda_max_good": 13.0, "compute_cap": 750.0}


@pytest.mark.parametrize("bad", [True, "13.0", [13], {}, float("nan"), float("inf"), 0, -13.0])
def test_a_non_numeric_minimum_is_REJECTED(bad):
    with pytest.raises(Exception) as ei:
        JM.parse(dict(_BASE, resources={"min_cuda": bad}))
    assert "min_cuda" in str(ei.value)


def test_runq_forwards_both_minimums_into_the_resource_hint():
    """runq copies a SUBSET of resource keys into `resource_hint`; a key missing there is silently
    dropped, and the task places exactly as before while its config looks correct."""
    src = (ROOT / "fleet" / "runq.py").read_text()
    i = src.index('for k in ("max_dph"')
    forwarded = src[i:src.index(":", src.index(")", i))]
    assert "min_cuda" in forwarded and "min_compute_cap" in forwarded
