"""Learned per-lane resource footprint — invariant 26 (docs/specs/task-dispatcher.spec.md).

Pure `learn_lane_footprint`/`resolve_hint` unit tests, plus the two consequences that motivate the
whole mechanism: that the learned footprint un-pins a box from 6 slots, and that a measured-zero
VRAM axis can never divide by zero inside placement.
"""

import importlib.util
import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cal = _load("calibration", "fleet/calibration.py")
res = _load("res_defaults", "fleet/res_defaults.py")


def _rows(n, ep="e", grp="g", running=6, cores=6.0, vram=0.0, ram=9.0):
    """n identical samples. Defaults mirror the live 2026-07-31 reading: 6 lanes burning 6.0 cores
    (1.00/lane — `pin_torch_threads(1)`), 0.00 GB VRAM, 9.0 GB RAM (1.5/lane)."""
    return [(ep, grp, running, cores, vram, ram)] * n


# ----------------------------------------------------------------- pure: learn_lane_footprint

def test_cold_registry_learns_nothing():
    """Below FLEET_MIN_SAMPLE nothing is learned, so a cold fleet changes no behaviour at all."""
    assert res.learn_lane_footprint(_rows(res.FLEET_MIN_SAMPLE - 1)) == {}


def test_fleet_wide_engages_at_min_sample():
    learned = res.learn_lane_footprint(_rows(res.FLEET_MIN_SAMPLE))
    assert None in learned
    # 6.0 cores / 6 lanes = 1.00; p90 = 1.00; x1.25 safety = 1.25
    assert learned[None]["cores_per_lane"] == 1.25


def test_per_key_refines_fleet_once_it_clears_min_sample():
    """A heavy key gets its own accurate value; a light key keeps its own. Each is one workload, so
    each p90 describes the workload it came from."""
    rows = _rows(40, ep="light", grp="g", cores=6.0) + _rows(
        res.LIVE_MIN_SAMPLE, ep="heavy", grp="g", running=2, cores=8.0)   # 4.0 cores/lane
    learned = res.learn_lane_footprint(rows)
    assert learned[("heavy", "g")]["cores_per_lane"] == 5.0               # 4.0 x 1.25
    assert learned[("light", "g")]["cores_per_lane"] == 1.25


def test_fleet_axis_abstains_on_a_heterogeneous_mixture():
    """THE TRAP THIS GUARD EXISTS FOR. One heavy campaign (n=12 at 4.0 cores/lane) sits at the p90 of
    a fleet otherwise running 1.0 — so an ungated fleet-wide p90 would be 5.0 and would size EVERY
    box in the fleet as if every lane were heavy, re-introducing the exact over-declaration this
    module removes, with a measurement's authority behind it. The axis must abstain instead, leaving
    the declared value untouched."""
    rows = _rows(40, ep="light", grp="g", cores=6.0) + _rows(
        res.LIVE_MIN_SAMPLE, ep="heavy", grp="g", running=2, cores=8.0)
    learned = res.learn_lane_footprint(rows)
    assert "cores_per_lane" not in learned.get(None, {})
    # ... and an unmeasured workload therefore keeps exactly what it declared.
    assert res.resolve_hint(learned, "unknown", "x", {"cores_per_lane": 2})["cores_per_lane"] == 2


def test_per_key_below_min_sample_falls_back_to_fleet():
    """A key with too few samples inherits the fleet number — here the fleet IS homogeneous, because
    the under-sampled key is only 11 of 51 samples and stays inside HOMOGENEITY_RATIO."""
    rows = _rows(40, ep="light", grp="g") + _rows(
        res.LIVE_MIN_SAMPLE - 1, ep="heavy", grp="g", running=6, cores=7.0)  # 1.17/lane, tight
    learned = res.learn_lane_footprint(rows)
    assert ("heavy", "g") not in learned
    assert res.resolve_hint(learned, "heavy", "g", None)["cores_per_lane"] == 1.46


def test_mixed_box_feeds_fleet_but_teaches_no_key():
    """`ep`/`grp` null = a box running a MIX. Its per-lane average is still real, so it must keep
    voting fleet-wide rather than being discarded."""
    learned = res.learn_lane_footprint(_rows(20, ep=None, grp=None))
    assert None in learned and len(learned) == 1


def test_zero_running_samples_are_dropped():
    """A sample with nothing running measures fixed box overhead, not a lane — and would divide by
    zero. It must not even count toward FLEET_MIN_SAMPLE."""
    assert res.learn_lane_footprint([("e", "g", 0, 4.0, 1.0, 8.0)] * 50) == {}


def test_axis_with_no_readings_is_omitted_not_defaulted():
    """A box that cannot read its GPU (vram None) must never teach the fleet that lanes need 0 VRAM
    — the axis is absent, so `resolve_hint` leaves the declared value in place."""
    learned = res.learn_lane_footprint([("e", "g", 4, 4.0, None, 8.0)] * 20)
    assert "vram_per_lane_gb" not in learned[None]
    assert res.resolve_hint(learned, "e", "g", {"vram_per_lane_gb": 2.0})["vram_per_lane_gb"] == 2.0


# ----------------------------------------------------------------------------- floors + ceilings

def test_measured_zero_vram_is_floored_never_zero():
    """THE CRASH THIS FLOOR EXISTS FOR: `slots_for_offer` divides the offer by `vram_per_lane_gb`
    with no zero-guard, and the workload genuinely measures 0.00 GB. A learned 0.0 would raise
    ZeroDivisionError inside the placement loop."""
    learned = res.learn_lane_footprint(_rows(20, vram=0.0))
    assert learned[None]["vram_per_lane_gb"] == res.FLOOR_VRAM
    assert learned[None]["vram_per_lane_gb"] > 0


def test_pathological_sample_is_clamped_to_ceiling():
    """An over-large learned footprint does not merely under-pack: `slots_for_offer` returns 0 for
    an offer that cannot host one lane, and 4e's fit filter then DROPS it — so an unclamped outlier
    could empty the rentable set and stall the queue."""
    learned = res.learn_lane_footprint([("e", "g", 1, 999.0, 999.0, 999.0)] * 20)
    assert learned[None] == {"cores_per_lane": res.CEIL_CORES,
                             "vram_per_lane_gb": res.CEIL_VRAM,
                             "ram_per_lane_gb": res.CEIL_RAM}


def test_learns_in_both_directions_not_a_ratchet():
    """`_learn_overpack_cap` only ever takes min(), so a cap learned once never recovers (it pinned
    the owned laptop at 4 slots until a human intervened). This learner must move BOTH ways — down
    on cores/VRAM (frees slots) and UP on RAM, the one axis where the declaration is too small."""
    # 6 lanes using 16.8 GB = 2.8 GB/lane, against the 2.0 GB settings default.
    learned = res.learn_lane_footprint(_rows(20, cores=6.0, ram=16.8))
    assert learned[None]["ram_per_lane_gb"] == 3.5      # 2.8 x 1.25 — RAISED above the 2.0 default
    assert learned[None]["cores_per_lane"] == 1.25      # and LOWERED below the declared 2-4


# ------------------------------------------------------------------------------- resolve_hint

def test_resolve_hint_returns_declared_when_nothing_learned():
    declared = {"vram_per_lane_gb": 2.0, "cores_per_lane": 2}
    assert res.resolve_hint({}, "e", "g", declared) == declared
    assert res.resolve_hint({}, "e", "g", None) is None


def test_resolve_hint_preserves_unmodelled_fields():
    """`max_dph` lifts a task's own price ceiling (invariant 4f). Replacing the hint dict wholesale
    would silently drop it and re-cap an expensive task at the global ceiling."""
    learned = res.learn_lane_footprint(_rows(20))
    out = res.resolve_hint(learned, "e", "g", {"max_dph": 0.9})
    assert out["max_dph"] == 0.9
    assert out["cores_per_lane"] == learned[("e", "g")]["cores_per_lane"]


# --------------------------------------- 26k: a declared axis beats the fleet-wide average

def test_fleet_average_never_overrides_a_declared_axis_for_admission():
    """THE 2026-09-27 STRANDING. One heavy campaign was most of the fleet's GPU samples, so the
    fleet-wide VRAM p90 hit the 8 GB clamp — and every NEW pclm group (no samples of its own)
    declaring 4 GB was admitted as 8, which fits no owned GPU by day and never the 8 GB desktop."""
    learned = res.learn_lane_footprint(_rows(20, running=1, vram=12.0))
    assert learned[None]["vram_per_lane_gb"] == res.CEIL_VRAM
    out = res.resolve_hint(learned, "pclm.train", "new_group", {"vram_per_lane_gb": 4.0})
    assert out["vram_per_lane_gb"] == 4.0


def test_fleet_average_fills_an_undeclared_axis():
    """A task that states nothing about RAM still gets the fleet's measured RAM, not the 2 GB
    settings default — the part of 26 that was always right."""
    learned = res.learn_lane_footprint(_rows(20, ram=16.8))
    out = res.resolve_hint(learned, "e", "other", {"vram_per_lane_gb": 4.0})
    assert out["ram_per_lane_gb"] == learned[None]["ram_per_lane_gb"]
    assert out["vram_per_lane_gb"] == 4.0


def test_own_measurement_can_raise_but_never_shrink_a_declaration():
    """The same workload's own samples may stop an under-declared job OOMing a box (raise), but a
    measurement never admits a job below what its author said it needs (the `9f38965d` over-
    admission onto a 12 GB card, with a 14 GB declaration re-priced to 2.3)."""
    heavy = res.learn_lane_footprint(_rows(20, running=1, vram=6.0))       # 7.5 GB/lane
    assert res.resolve_hint(heavy, "e", "g", {"vram_per_lane_gb": 2.0})["vram_per_lane_gb"] == 7.5
    light = res.learn_lane_footprint(_rows(20, running=6, vram=1.2))       # 0.25 GB/lane
    assert res.resolve_hint(light, "e", "g", {"vram_per_lane_gb": 14.0})["vram_per_lane_gb"] == 14.0
    # An axis the task left undeclared takes its own measurement outright.
    assert res.resolve_hint(heavy, "e", "g", {})["vram_per_lane_gb"] == 7.5
    assert res.resolve_hint(light, "e", "g", {})["cores_per_lane"] == light[("e", "g")]["cores_per_lane"]


def test_resolve_hint_does_not_mutate_the_declared_dict():
    declared = {"vram_per_lane_gb": 2.0}
    res.resolve_hint(res.learn_lane_footprint(_rows(20)), "e", "g", declared)
    assert declared == {"vram_per_lane_gb": 2.0}


# ------------------- 26l: a broader average fills only what it can describe (2026-09-29 incident)

LADDER = "scripts/diagnostics/sparse_pc_ladder.py"


def _live_mix():
    """The 2026-09-28 day's samples, shape-for-shape. pclm (GPU) is the MAJORITY of every GPU sample
    and most RAM samples, so the fleet homogeneity gate (c) passes and describes pclm. The ladder is
    CPU-only (the tower reads no GPU -> vram None) and names every sweep a NEW group, each
    below LIVE_MIN_SAMPLE — so no ladder group ever has a per-key footprint of its own."""
    rows = ([("pclm.train", "pclm_g1_pc", 1, 1.0, 3.9, 7.3)] * 245
            + [("pclm.train", "pclm_g1_llm", 1, 1.0, 5.2, 7.9)] * 44
            + [("pclm.train", "pclm_memprobe", 1, 1.01, 4.2, 6.8)] * 10)
    for i in range(10):                                   # 10 ladder groups x 5 samples, no GPU read
        rows += [(LADDER, "tpc_g%d" % i, 4, 4.0, None, 2.4)] * 5
    rows += [(LADDER, "tpc_hier_s1b", 1, 1.0, 1.6, 1.3)] * 2   # a ladder task on a GPU box: display baseline
    return rows


def test_entrypoint_level_fills_before_fleet():
    """A new ladder group inherits the LADDER's measured footprint, not pclm's. Before 26l it took
    the fleet's pclm-majority values: 2.21 cores / 9.7 GB RAM / 6.5 GB VRAM per lane."""
    learned = res.learn_lane_footprint(_live_mix())
    assert (LADDER, None) in learned                       # 52 samples across groups
    out = res.resolve_hint(learned, LADDER, "tpc_new_group", {"colocate": "k"})
    assert out["ram_per_lane_gb"] <= 2.0, out
    assert out["cores_per_lane"] <= 1.5, out
    # the fleet still says pclm — the entrypoint level is what shields the ladder from it
    assert learned[None]["ram_per_lane_gb"] >= 8.0 and learned[None]["vram_per_lane_gb"] >= 5.0


def test_broad_vram_never_fills_a_cpu_task():
    """VRAM is sampled only on GPU boxes, so a per-entrypoint or fleet VRAM number describes GPU
    workloads (or a box's display baseline / running), never a CPU-only lane. A task that does not
    declare requires_gpu is left to the settings lane on that axis; a GPU task still gets the fill."""
    learned = res.learn_lane_footprint(_live_mix())
    cpu = res.resolve_hint(learned, LADDER, "tpc_new_group", {"colocate": "k"})
    assert "vram_per_lane_gb" not in cpu, cpu
    gpu = res.resolve_hint(learned, "pclm.train", "pclm_new", {"requires_gpu": True})
    assert gpu["vram_per_lane_gb"] == learned[("pclm.train", None)]["vram_per_lane_gb"]
    # ... and a CPU task's OWN (entrypoint, grp) measurement still fills when it shows GPU use
    # (same workload, 26k). At the floor it shows none, and 26m leaves the axis absent.
    own = res.learn_lane_footprint(_rows(20, ep=LADDER, grp="tpc_x", running=1, vram=2.0))
    assert res.resolve_hint(own, LADDER, "tpc_x", {})["vram_per_lane_gb"] == 2.5


def _disp_settings(disp):
    s = dict(disp.DEFAULT_SETTINGS)
    s.update(cores_per_lane=1, vram_per_lane_gb=0.6, ram_per_lane_gb=2.0)   # the live settings rows
    return s


def _occupant(disp, hint, settings, state="running"):
    cores, vram = disp.task_footprint(hint, 1, settings)
    ram = float((hint or {}).get("ram_per_lane_gb", settings["ram_per_lane_gb"]))
    return {"id": "occ", "slots": 1, "state": state, "est_minutes": 120, "priority": 50,
            "running_minutes_ago": 10.0, "cores": cores, "vram_gb": vram, "ram_gb": ram}


def _count_admitted(disp, hint, inst, settings, now, limit=16):
    """Admit ladder tasks one at a time, as successive polls would once each one is running."""
    n = 0
    while n < limit and disp._fits_now({"id": "t", "slots": 1, "est_minutes": 120, "priority": 50,
                                       "resource_hint": hint}, inst, settings, now):
        inst["occupants"].append(_occupant(disp, hint, settings))
        n += 1
    return n


# The hint the dispatcher actually used on 2026-09-28 for every new ladder group (the fleet fill).
INCIDENT_HINT = {"colocate": "k", "cores_per_lane": 2.21, "ram_per_lane_gb": 9.7, "vram_per_lane_gb": 6.5}


def test_laptop_admits_more_than_one_ladder_task():
    """THE LAPTOP INCIDENT. Owned laptop-gpu, day window (cpu 0.5 x 32 = 16 cores, vram 0.85 x 12 =
    10.2 GB), measured idle (load 0.03/32, 38 GB free, 1.6/12 GB VRAM). With the fleet-filled hint it
    admitted exactly ONE co-located sibling at a time (6.5 + 6.5 > 10.2); four held 'waiting for
    room' for 15+ min. The ladder's own footprint must admit several."""
    import time
    disp = _load("dispatcher", "fleet/dispatcher.py")
    settings, now = _disp_settings(disp), time.time()

    def laptop():
        return {"id": -1, "state": "live", "slots_total": 16, "resource_cap": {"cores": 16.0, "vram_gb": 10.2},
                "minutes_to_hard_cap": 1e9, "occupants": [],
                "measured": {"at": now, "cores": 32, "load1": 0.03, "ram_avail_gb": 38.0,
                             "vram_total_gb": 12.0, "vram_used_gb": 1.6}}

    assert _count_admitted(disp, INCIDENT_HINT, laptop(), settings, now) == 1     # the incident
    hint = res.resolve_hint(res.learn_lane_footprint(_live_mix()), LADDER, "tpc_hier_s1b", {"colocate": "k"})
    assert _count_admitted(disp, hint, laptop(), settings, now) >= 5, hint


def test_gpudesktop_admits_ladder_tasks_beside_pclm():
    """THE GPUDESKTOP INCIDENT. Owned, day window (cpu 0.5 x 24 = 12 cores, vram 0.5 x 16 = 8 GB),
    a container quota of 12 cores, one pclm GPU task running (declared 3.0 GB / 2 cores, raised by
    its own measurement), 11.9 GB RAM free, 4.2/15.9 GB VRAM. With the fleet-filled hint NO ladder
    task fit (pclm + 6.5 > 8 GB budget; 2.8 GB VRAM headroom < 6.5), so 5 box-targeted tasks held
    for 17 min with 23 slots free."""
    import time
    disp = _load("dispatcher", "fleet/dispatcher.py")
    settings, now = _disp_settings(disp), time.time()
    learned = res.learn_lane_footprint(_live_mix())
    pclm = res.resolve_hint(learned, "pclm.train", "pclm_g1_pc",
                            {"vram_per_lane_gb": 3.0, "cores_per_lane": 2, "requires_gpu": True})

    def desktop():
        return {"id": -4, "state": "live", "slots_total": 24, "resource_cap": {"cores": 12.0, "vram_gb": 8.0},
                "minutes_to_hard_cap": 1e9, "occupants": [_occupant(disp, pclm, settings)],
                "measured": {"at": now, "cores": 24, "load1": 1.0, "cpu_quota_cores": 12.0,
                             "cpu_used_cores": 1.0, "ram_avail_gb": 11.9, "vram_total_gb": 15.9,
                             "vram_used_gb": 4.2}}

    assert _count_admitted(disp, INCIDENT_HINT, desktop(), settings, now) == 0    # the incident
    hint = res.resolve_hint(learned, LADDER, "tpc_hier_s1b2", {"box": "gpudesktop"})
    assert _count_admitted(disp, hint, desktop(), settings, now) >= 3, hint


# --------------------------------------------------------- the consequence this exists to produce

def test_learned_footprint_unpins_the_six_slot_box():
    """END TO END, on the live 2026-07-31 numbers. A 12 GB / 24-effective-core RTX 3060 was sized at
    6 slots by `floor(12 / 2.0)` — a VRAM reservation against a card measuring 0.00 GB used. With the
    measured footprint it reaches `max_slots_cap`."""
    disp = _load("dispatcher", "fleet/dispatcher.py")
    offer = {"gpu_ram_gb": 12.0, "cpu_cores_effective": 24.0}
    settings = dict(disp.DEFAULT_SETTINGS)

    declared = {"vram_per_lane_gb": 2.0, "cores_per_lane": 2}
    assert disp.slots_for_offer(offer, declared, settings) == 6          # the live defect

    learned = res.learn_lane_footprint(_rows(20))                        # 1.00 core, 0.00 GB / lane
    # 26k: the declaration is authoritative — the un-pinning now happens at its SOURCE (the tracked
    # config stops over-declaring), and an axis it leaves undeclared takes the measurement.
    assert disp.slots_for_offer(offer, res.resolve_hint(learned, "e", "g", declared), settings) == 6
    effective = res.resolve_hint(learned, "e", "g", {"cores_per_lane": 1})
    assert disp.slots_for_offer(offer, effective, settings) == settings["max_slots_cap"]


def test_smallest_live_box_still_gains_slots():
    """The CPU-bound box in the live set (10.0 effective cores) was sized at 5 by the cores term.
    The learned 1.25 cores/lane lifts it to 8 without the VRAM axis ever binding."""
    disp = _load("dispatcher", "fleet/dispatcher.py")
    offer = {"gpu_ram_gb": 12.0, "cpu_cores_effective": 10.0}
    settings = dict(disp.DEFAULT_SETTINGS)
    assert disp.slots_for_offer(offer, {"vram_per_lane_gb": 2.0, "cores_per_lane": 2}, settings) == 5
    effective = res.resolve_hint(res.learn_lane_footprint(_rows(20)), "e", "g", None)
    assert disp.slots_for_offer(offer, effective, settings) == 8


# ------------------------------ 26m: VRAM is charged only to a task that uses the GPU (2026-10-03)

class TestVramIsChargedOnlyToGpuUsers:
    """`resolve_hint` decides, per task, whether its own group's VRAM measurement says "uses the
    card". `dispatcher.lane_vram_gb` then charges a task with no VRAM axis nothing at all."""

    AT_FLOOR = staticmethod(lambda: res.learn_lane_footprint(_rows(20, running=1, vram=0.1)))
    ABOVE = staticmethod(lambda: res.learn_lane_footprint(_rows(20, running=1, vram=2.0)))   # 2.5

    def test_a_group_measured_at_the_floor_leaves_a_cpu_task_with_no_vram_axis(self):
        learned = self.AT_FLOOR()
        assert learned[("e", "g")]["vram_per_lane_gb"] == res.FLOOR_VRAM
        assert "vram_per_lane_gb" not in res.resolve_hint(learned, "e", "g", {})
        assert "vram_per_lane_gb" not in res.resolve_hint(learned, "e", "g", None)
        assert "vram_per_lane_gb" not in res.resolve_hint(learned, "e", "g", {"cores_per_lane": 1})

    def test_a_cards_own_movement_is_not_gpu_use(self):
        """MEASURED on the live registry 2026-10-03: `tpc_event_cap_canon_cw`, CPU-only, learned
        0.38 GB per lane from six samples of a desktop whose display grew 0.3 GB beside its one
        lane. At the 0.25 floor that made it a GPU user."""
        noisy = res.learn_lane_footprint(_rows(20, running=1, vram=0.3))
        assert noisy[("e", "g")]["vram_per_lane_gb"] == 0.38
        assert "vram_per_lane_gb" not in res.resolve_hint(noisy, "e", "g", {})
        # ... and the smallest real GPU group measured (1.12 GB) is well clear of the line.
        real = res.learn_lane_footprint(_rows(20, running=1, vram=0.9))
        assert res.resolve_hint(real, "e", "g", {})["vram_per_lane_gb"] == 1.12
        assert res.FLOOR_VRAM < 0.38 < res.GPU_USE_MIN_VRAM < 1.12

    def test_an_explicit_zero_stays_zero(self):
        """`configs/probes/TEMPLATE.probe.json` declares `vram_gb: 0`. It used to be raised to the
        0.25 floor by its own group's measurement of nothing."""
        out = res.resolve_hint(self.AT_FLOOR(), "e", "g", {"vram_per_lane_gb": 0.0, "cores_per_lane": 1})
        assert out["vram_per_lane_gb"] == 0.0

    def test_a_group_measured_ABOVE_the_floor_is_a_gpu_user_whatever_it_declared(self):
        """The safety net: a task that uses the card without saying so is gated once measured."""
        learned = self.ABOVE()
        assert res.resolve_hint(learned, "e", "g", {})["vram_per_lane_gb"] == 2.5
        assert res.resolve_hint(learned, "e", "g", {"vram_per_lane_gb": 0.0})["vram_per_lane_gb"] == 2.5

    def test_a_task_that_claims_the_gpu_is_raised_to_its_measurement_even_at_the_floor(self):
        learned = self.AT_FLOOR()
        assert res.resolve_hint(learned, "e", "g", {"requires_gpu": True})["vram_per_lane_gb"] == res.FLOOR_VRAM
        assert res.resolve_hint(learned, "e", "g", {"vram_per_lane_gb": 2.0})["vram_per_lane_gb"] == 2.0

    def test_dispatcher_charges_what_resolve_hint_decided(self):
        disp = _load("dispatcher", "fleet/dispatcher.py")
        s = _disp_settings(disp)
        cpu = res.resolve_hint(self.AT_FLOOR(), "e", "g", {})
        assert disp.lane_vram_gb(cpu, s) == 0.0 and disp.task_footprint(cpu, 3, s)[1] == 0.0
        used = res.resolve_hint(self.ABOVE(), "e", "g", {})
        assert disp.lane_vram_gb(used, s) == 2.5 and disp.task_footprint(used, 3, s)[1] == 7.5


# ------------------- 26n: the learner charges a lane only for VRAM above its box's idle reading

def _s(box, t, running, vram, ep="e", grp="g", open_n=None, cores=None, ram=None):
    """One `box_measured` sample as `net_idle_vram` takes it."""
    open_n = running if open_n is None else open_n
    return (box, t, ep if running else None, grp if running else None, running, open_n,
            float(running) if cores is None else cores, vram, 1.5 * running if ram is None else ram)


def _vram(rows):
    return [r[4] for r in rows]


class TestLearnerNetsTheIdleReading:
    def test_a_busy_sample_is_charged_only_for_what_the_card_gained_since_the_box_was_idle(self):
        rows = res.net_idle_vram([_s("b", 0, 0, 1.2), _s("b", 300, 2, 7.2), _s("b", 600, 2, 7.4)])
        assert _vram(rows)[1:] == pytest.approx([6.0, 6.2])

    def test_it_never_goes_below_zero(self):
        """The desktop after its reinstall: the idle reading (old OS) was 1.2 GB, the card now
        reads 0.3 with lanes running."""
        assert _vram(res.net_idle_vram([_s("b", 0, 0, 1.2), _s("b", 300, 3, 0.3)]))[1] == 0.0

    def test_the_reading_BEFORE_is_used_never_a_later_one(self):
        """MEASURED 2026-09-30 on laptop-gpu: with no open fleet task the card sat at a finished
        GPU run's own level for 7 h 20 min. Netting against that later reading erased the run."""
        rows = res.net_idle_vram([_s("b", 0, 0, 2.8), _s("b", 300, 1, 9.0), _s("b", 310, 1, 11.0),
                                  _s("b", 600, 0, 11.0)])
        assert _vram(rows)[1:3] == pytest.approx([6.2, 8.2])

    def test_the_MOST_RECENT_earlier_reading_is_the_one(self):
        rows = res.net_idle_vram([_s("b", 0, 0, 0.5), _s("b", 100, 0, 9.4), _s("b", 200, 1, 9.5)])
        assert _vram(rows)[2] == pytest.approx(0.1)

    def test_no_earlier_idle_reading_teaches_nothing_about_vram(self):
        """An unmeasured baseline is not a baseline of zero. Cores and RAM still pass through."""
        rows = res.net_idle_vram([_s("b", 100, 2, 9.0), _s("b", 200, 0, 1.0), _s("c", 100, 1, 5.0)])
        assert _vram(rows) == [None, 0.0, None]
        assert rows[0][3] == 2.0 and rows[0][5] == 3.0
        learned = res.learn_lane_footprint([rows[0]] * 20)
        assert "vram_per_lane_gb" not in learned[("e", "g")] and "cores_per_lane" in learned[("e", "g")]

    def test_a_box_with_a_task_shipped_or_preempting_is_not_idle(self):
        """`running == 0` is not empty: a `shipped` task may already hold memory, a `preempting`
        one still does."""
        rows = res.net_idle_vram([_s("b", 0, 0, 0.5), _s("b", 100, 0, 8.0, open_n=1),
                                  _s("b", 200, 1, 8.5)])
        assert _vram(rows)[2] == pytest.approx(8.0)          # netted against 0.5, not against 8.0

    def test_a_payload_without_open_falls_back_to_running(self):
        samples = [("b", 0, None, None, 0, None, 0.0, 1.0, 1.0), ("b", 100, "e", "g", 1, None, 1.0, 4.0, 1.5)]
        assert _vram(res.net_idle_vram(samples))[1] == pytest.approx(3.0)

    def test_boxes_do_not_share_readings(self):
        rows = res.net_idle_vram([_s("a", 0, 0, 9.4), _s("b", 0, 0, 0.5), _s("b", 100, 1, 4.5)])
        assert _vram(rows)[2] == pytest.approx(4.0)

    def test_a_box_that_cannot_read_its_gpu_stays_unmeasured(self):
        rows = res.net_idle_vram([_s("b", 0, 0, None), _s("b", 100, 1, None)])
        assert _vram(rows) == [None, None]

    def test_THE_INCIDENT_non_fleet_vram_is_not_charged_to_a_cpu_only_lane(self):
        """laptop-gpu, 2026-10-03 early morning: ~9.5 GB that no fleet task allocated, one
        CPU-only lane running beside it. Whole card / running learned the 8.0 GB clamp, which
        then fits no owned GPU by day — with or without the card freed."""
        samples = [_s("laptop", 0, 0, 9.5)] + [_s("laptop", 300 * (i + 1), 1, 9.5) for i in range(20)]
        before = res.learn_lane_footprint([(e, g, r, c, v, m) for _b, _t, e, g, r, _o, c, v, m in samples])
        assert before[("e", "g")]["vram_per_lane_gb"] == res.CEIL_VRAM            # the defect
        after = res.learn_lane_footprint(res.net_idle_vram(samples))
        assert after[("e", "g")]["vram_per_lane_gb"] == res.FLOOR_VRAM
        assert "vram_per_lane_gb" not in res.resolve_hint(after, "e", "g", {})

    def test_POSITIVE_CONTROL_a_real_gpu_lane_is_still_learned(self):
        """The netting must not erase real use: 3 GB per lane on a card with a 1.2 GB baseline."""
        samples = [_s("b", 0, 0, 1.2)] + [_s("b", 300 * (i + 1), 2, 7.2) for i in range(20)]
        learned = res.learn_lane_footprint(res.net_idle_vram(samples))
        assert learned[("e", "g")]["vram_per_lane_gb"] == 3.75                      # 3.0 x 1.25
        assert res.resolve_hint(learned, "e", "g", {})["vram_per_lane_gb"] == 3.75
