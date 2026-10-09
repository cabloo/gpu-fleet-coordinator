"""Tests for the fleet utilization monitor (docs/specs/fleet-utilization-monitor.spec.md).

The spec's fixtures ARE these tests. They exercise the pure core only — no DB, no clock, no network —
which is the whole reason `fleet_util` splits a pure core from an impure shell.

The two that matter most are the NEGATIVE ones, because this tool's failure mode is not missing a
problem, it is inventing one and proposing a config change off it:
  * `test_correctly_sized_heavy_lane_is_SILENT` guards spec invariant 1 (the two-layer model). Kill
    the CPU layer and it goes red.
  * `test_sizing_ABSTAINS_when_the_rent_did_not_log_slots` guards against the phantom defects the
    first draft produced by re-deriving a hint that is partly learned.
"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fu = _load("fleet_util", "fleet/fleet_util.py")


def _box(iid=1, gpu="RTX 3070", dph=0.074):
    return {"id": iid, "gpu_name": gpu, "dph_usd": dph}


def _sample(running=7, slots=8, quota=26.9, used=7.25, mem_limit=85.3, mem_anon=9.3, gpu=0.0):
    return {"running": running, "slots_eff": slots, "cpu_quota_cores": quota,
            "cpu_used_cores": used, "mem_limit_gb": mem_limit, "mem_anon_gb": mem_anon,
            "gpu_util": gpu}


class TestTwoLayerModel:
    """Spec invariant 1. Slot occupancy and RESOURCE utilization are different numbers, and only the
    PAIR says which fix applies. These two tests are the same slot occupancy with opposite CPU."""

    def test_underpack_fires_when_slots_full_but_cpu_idle(self):
        # The live 2026-08-03 shape: RTX 3070 at 7/8 lanes using 7.25 of 26.88 cores, GPU at 0%.
        u = fu.box_utilization(_box(), [_sample()] * 5)
        found = fu.find_packing_findings([u])
        assert [f.kind for f in found] == ["UNDERPACK"]
        assert "overpack_cap_i1" in found[0].action, "a proposal must carry the command that applies it"
        assert found[0].usd_at_stake > 0

    def test_correctly_sized_heavy_lane_is_SILENT(self):
        """SAME slot occupancy, but the lanes are actually consuming the box. Proposing more lanes
        here is how you OOM a healthy box — so the monitor must say nothing."""
        u = fu.box_utilization(_box(), [_sample(used=25.0)] * 5)   # 25.0/26.9 = 93% of quota
        assert [f.kind for f in fu.find_packing_findings([u])] == ["OVERPACK"]

    def test_the_ambiguous_band_says_nothing(self):
        """Between IDLE_CPU and BUSY_CPU we do not know which way it goes — silence beats a coin
        flip, because every finding here is a proposal to change a live config."""
        u = fu.box_utilization(_box(), [_sample(used=26.9 * 0.55)] * 5)
        assert fu.find_packing_findings([u]) == []

    def test_a_box_seen_too_few_times_is_not_proposed_on(self):
        u = fu.box_utilization(_box(), [_sample()] * (fu.MIN_SAMPLES - 1))
        assert fu.find_packing_findings([u]) == []

    def test_a_missing_cgroup_layer_ABSTAINS_rather_than_reading_as_idle(self):
        """A box that never reported cpu quota is unknown, not idle. Substituting 0 would make every
        such box an UNDERPACK proposal."""
        blind = [{"running": 8, "slots_eff": 8}] * 5
        u = fu.box_utilization(_box(), blind)
        assert u.cpu_util is None
        assert fu.find_packing_findings([u]) == []

    def test_utilization_is_averaged_over_the_window_not_the_newest_sample(self):
        """One poll can catch a box between tasks. Live, the newest sample read 7/8 lanes while the
        window averaged 0.525 — and the window is what a permanent config change must rest on."""
        u = fu.box_utilization(_box(), [_sample(running=8), _sample(running=0)])
        assert u.slot_occ == 0.5


class TestSizingDefect:
    """Spec invariant 4 — the `_adopt`-clobber detector (the defect fixed in 9cd11c97)."""

    def test_detects_the_adopt_clobber(self):
        found, abstained = fu.find_sizing_defects(
            [dict(_box(iid=40000053), slots_total=1, rent_at_slots=8)])
        assert abstained == 0 and len(found) == 1
        assert found[0].kind == "SIZING"
        assert "RENTED at 8" in found[0].measured
        assert "DEFECT" in found[0].action, "a clobber is a bug to investigate, never a knob to turn"

    def test_ABSTAINS_when_the_rent_did_not_log_slots(self):
        """No logged reference ⇒ we cannot know ⇒ we do not guess. The first draft re-derived the
        expected count with a guessed hint and produced ~20 phantom defects on real boxes, because
        the hint used at rent time is partly LEARNED (inv. 26)."""
        found, abstained = fu.find_sizing_defects(
            [dict(_box(iid=i), slots_total=5, rent_at_slots=None) for i in range(3)])
        assert found == [] and abstained == 3

    def test_a_box_at_or_above_its_rented_size_is_healthy(self):
        found, _ = fu.find_sizing_defects([dict(_box(), slots_total=8, rent_at_slots=8)])
        assert found == []


class TestRankByDollars:
    """Spec invariant 1: rank by $ at stake, never by box count. Measured 2026-08-03, 370/482 boxes
    'never ran' — worth 4-8% of spend — while idle on boxes that DID run was 58%."""

    def test_a_cheap_big_count_never_outranks_one_expensive_box(self):
        cheap = [dict(_box(iid=i, dph=0.001), slots_total=1, rent_at_slots=2) for i in range(50)]
        pricey = dict(_box(iid=999, dph=0.30), slots_total=1, rent_at_slots=8)
        found, _ = fu.find_sizing_defects(cheap + [pricey])
        assert found[0].subject.startswith("box 999"), "50 trivial boxes outranked the expensive one"


class TestCeilingAnchor:
    """Spec invariant 2: occupancy's denominator is the FULL billed lifetime, so 1.0 is unreachable
    and the ceiling must be MEASURED."""

    def test_ceiling_is_one_minus_measured_overhead(self):
        c = fu.achievable_ceiling([{"lifetime_min": 100, "boot_min": 20, "drain_min": 5}])
        assert c.ceiling == 0.75 and c.n_boxes == 1

    def test_ceiling_cannot_GO_NEGATIVE(self):
        """A first draft of this read returned -0.138 by mixing box populations. A ceiling below 0
        is not a fleet finding, it is an arithmetic bug, and it must be impossible by construction."""
        c = fu.achievable_ceiling([{"lifetime_min": 10, "boot_min": 90, "drain_min": 90}])
        assert c.ceiling is not None and 0.0 <= c.ceiling <= 1.0

    def test_empty_input_yields_no_ceiling_rather_than_a_fake_one(self):
        assert fu.achievable_ceiling([]).ceiling is None


class TestEstDrift:
    def test_reports_a_group_still_in_flight(self):
        g = {"entrypoint": "e", "grp": "g", "est_minutes": 150, "active": True,
             "actual_minutes": [50] * 10}
        found = fu.find_est_drift([g])
        assert len(found) == 1 and "--est-minutes" in found[0].action

    def test_IGNORES_a_finished_group(self):
        """A settled campaign's est_minutes is not actionable. Live, 20+ such groups drowned the two
        that were still running."""
        g = {"entrypoint": "e", "grp": "g", "est_minutes": 150, "active": False,
             "actual_minutes": [50] * 10}
        assert fu.find_est_drift([g]) == []

    def test_too_few_samples_is_not_a_proposal(self):
        g = {"entrypoint": "e", "grp": "g", "est_minutes": 150, "active": True,
             "actual_minutes": [50] * (fu.MIN_EST_SAMPLE - 1)}
        assert fu.find_est_drift([g]) == []


class TestRender:
    def test_era_split_is_LABELLED_never_pooled(self):
        """Spec invariant 3: pooling across the 2026-07-21 sizing change makes a fixed bug read as a
        chronic fleet defect (PRE 177/395 one-slot boxes vs POST 10/227)."""
        import datetime as dt
        warn = fu._era_warning(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc))
        assert warn and "not comparable" in warn.lower()
        assert fu._era_warning(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)) is None

    def test_silence_is_earned_headline_prints_with_no_findings(self):
        """Spec invariant 7: a wedged monitor must be distinguishable from a healthy fleet."""
        out = fu.render(fu.Ceiling(0.75, 25.9, 12.2, 122), 0.5, [], None, n_live=3)
        assert "OK" in out and "no under/over-pack" in out

    def test_abstained_boxes_are_NEVER_silently_dropped(self):
        """The project's working rules, no silent caps: bounded coverage must not read as full coverage."""
        out = fu.render(fu.Ceiling(0.75, 25.9, 12.2, 122), 0.5, [], None,
                        sizing_abstained=229, n_live=3)
        assert "ABSTAIN" in out and "229" in out

    def test_the_two_occupancies_are_never_divided_by_each_other(self):
        """A first draft printed '52% — 70% of the 75% ceiling', double-discounting boot+drain: the
        live read comes from `box_measured`, which only fires once a box IS live."""
        out = fu.render(fu.Ceiling(0.75, 25.9, 12.2, 122), 0.52, [], None, n_live=1)
        assert "70%" not in out
        assert "anchor 1.0" in out


class TestGpuLost:
    """Spec invariant 10. A box registered WITH a GPU whose newest probes report none. The live
    shape is the desktop 2026-09-23: probes every ~5 min, `gpu_name` null from 02:05Z, and nothing
    in the fleet said so — the headroom gate abstains on an unmeasured GPU by design."""

    NOW = dt.datetime(2026, 9, 23, 2, 20, 0, tzinfo=dt.timezone.utc)

    def _s(self, t, gpu="NVIDIA GeForce RTX 3070 Ti"):
        return {"t": t, "gpu_name": gpu}

    def _box(self, samples, source="owned", last="2026-09-23T01:59:58Z", dph=0.0, gpu="RTX 3070 Ti"):
        return {"id": -2, "label": "desktop", "source": source, "gpu_name": gpu, "dph_usd": dph,
                "samples": samples, "last_gpu_t": last}

    def _lost(self, n):
        ts = ["2026-09-23T02:05:14Z", "2026-09-23T02:10:29Z", "2026-09-23T02:15:42Z"][:n]
        return [self._s("2026-09-23T01:59:58Z")] + [self._s(t, None) for t in ts]

    def test_fires_on_two_consecutive_gpu_less_probes(self):
        found, abst = fu.find_gpu_lost([self._box(self._lost(2))], self.NOW)
        assert [f.kind for f in found] == ["GPU-LOST"] and abst == 0
        assert "last 2 probe(s)" in found[0].measured
        assert "2026-09-23T01:59:58Z" in found[0].measured, "must say when the GPU was last seen"
        assert "owned_gpu_doctor.sh" in found[0].action, "a proposal must carry the command"
        assert found[0].usd_at_stake == 0.0

    def test_a_single_gpu_less_probe_is_SILENT(self):
        """One poll can be a one-off `nvidia-smi` timeout; the threshold is 2."""
        found, _ = fu.find_gpu_lost([self._box(self._lost(1))], self.NOW)
        assert found == []

    def test_recovery_extinguishes_the_finding(self):
        s = self._lost(2) + [self._s("2026-09-23T02:18:00Z")]
        assert fu.find_gpu_lost([self._box(s)], self.NOW)[0] == []

    def test_a_missing_KEY_is_unknown_not_lost(self):
        """Pre-inv-25 tails carry no `gpu_name` key at all — never read that as 'no GPU'."""
        s = [{"t": "2026-09-23T02:10:00Z"}, {"t": "2026-09-23T02:15:00Z"}]
        found, abst = fu.find_gpu_lost([self._box(s)], self.NOW)
        assert found == [] and abst == 1

    def test_a_STALE_box_is_abstained_not_reported(self):
        """Unreachable is not GPU-less: the newest probe is 2h old, so it says nothing about now."""
        s = [self._s("2026-09-23T00:10:00Z", None), self._s("2026-09-23T00:15:00Z", None)]
        found, abst = fu.find_gpu_lost([self._box(s)], self.NOW)
        assert found == [] and abst == 1

    def test_a_box_registered_WITHOUT_a_gpu_is_out_of_scope(self):
        """tower: no GPU, every probe null — silent, and not counted as an abstention."""
        b = self._box(self._lost(3), gpu=None)
        assert fu.find_gpu_lost([b], self.NOW) == ([], 0)

    def test_a_rental_prices_the_whole_day(self):
        found, _ = fu.find_gpu_lost([self._box(self._lost(2), source="vast", dph=0.2)], self.NOW)
        assert found[0].usd_at_stake == 4.8
        assert "runq box probe" in found[0].action

    def test_never_seen_is_stated_not_invented(self):
        found, _ = fu.find_gpu_lost([self._box(self._lost(2), last=None)], self.NOW)
        assert "never seen" in found[0].measured

    def test_render_shows_the_abstention(self):
        out = fu.render(fu.Ceiling(None, None, None, 0), None, [], None, gpu_abstained=1)
        assert "ABSTAIN" in out and "GPU box" in out
