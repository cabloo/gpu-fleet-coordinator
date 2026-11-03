"""⛔ INVARIANT 4j — a task may declare the minimum driver CUDA version (`min_cuda`) and card architecture
(`min_compute_cap`) a box must have, and BOTH placement paths must honour it.

WHY IT EXISTS, measured: a large-model job built against CUDA 13 rented a card whose driver supported only
CUDA 12.x. torch found no usable GPU, the job loaded the model into host memory instead, and it was killed
for memory within minutes. Vast publishes the driver's CUDA version (`cuda_max_good`) and the card's
architecture (`compute_cap`) on every offer, so placement can refuse such a box.

The pack half reuses the CPU-targeting rule (4f): the `instances` row records no driver, so a task with
minimums boards only the box rented for it. The tests below are the 4f regressions replayed on this axis.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

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
    """4f regression 1 on this axis: the `instances` row has no driver, so another task's box is
    unverifiable. The ordinary task packs onto it (the control); the one with minimums rents its own."""
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
    """Naming a box asserts what it can run, and it is the only way a task with minimums can use an
    owned box, whose driver the registry does not record."""
    owned = dict(_box("gpudesktop", -4), source="owned")
    t = _task({"min_cuda": 13.0, "box": "gpudesktop"})
    assert [i["id"] for i in D._boardable(t, [owned, _box("runq_t")])] == [-4]


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
