"""Task dispatcher — golden decision traces from docs/specs/task-dispatcher.spec.md."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "dispatch"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")

SHARED_SETTINGS = dict(disp.DEFAULT_SETTINGS)
SHARED_SETTINGS.update({
    "max_hourly_usd": 1.00, "balance_floor_usd": 3.00, "max_instance_dph": 0.40,
    "idle_timeout_min": 10, "rent_patience_min": 15, "backlog_min_tasks": 3,
    "backlog_min_task_minutes": 60, "preempt_priority_margin": 30,
    "vram_per_lane_gb": 0.6, "cores_per_lane": 1, "max_slots_cap": 8,
    "est_safety": 1.25, "pull_margin_min": 10,
})


def _fx(name):
    return json.loads((FIX / name).read_text())


def _mech_settings(overrides=None):
    """Settings for tests of the consolidation/preemption MECHANICS, with the global preempt switch
    explicitly ON.

    `preempt_enabled` defaults to FALSE in production (owner directive 2026-07-30, "disable
    preempts"), which gates priority preemption, capacity scale-down and consolidation alike. The
    mechanics were not removed, only switched off, so they still need coverage — and the switch has
    its own tests. Stating the override here rather than quietly re-enabling it inside `_settings`
    keeps each test honest about the world it assumes: a reader can see this class runs in a
    configuration production currently does not."""
    o = {"preempt_enabled": True}
    o.update(overrides or {})
    return _settings(o)


def _settings(overrides=None):
    s = dict(SHARED_SETTINGS)
    s.setdefault("current_rate", 0.0)
    s.setdefault("current_balance", 25.0)
    s.setdefault("deny_machine_ids", set())
    if overrides:
        s.update(overrides)
        if "deny_machine_ids" in overrides:
            s["deny_machine_ids"] = set(overrides["deny_machine_ids"])
    return s


def _place(fx):
    return disp.place(fx["task"], fx["instances"], fx.get("offers", []), fx.get("queue", []),
                       _settings(fx.get("settings_overrides")), 0)


def _place_mech(fx):
    """`_place` with the global preempt switch ON — for the PRIORITY-PREEMPTION mechanics fixtures.
    Production ships `preempt_enabled=False` (owner directive 2026-07-30), so without this the
    preempt fixtures would all assert `hold` and stop testing the mechanism at all."""
    return disp.place(fx["task"], fx["instances"], fx.get("offers", []), fx.get("queue", []),
                       _mech_settings(fx.get("settings_overrides")), 0)


class TestPack:
    def test_pack_tightest_fit(self):
        # Tie-break when all costs are equal (both boxes $0/free here): fewest free slots wins.
        fx = _fx("pack_tightest.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == fx["expect"]["target"]
        assert fx["expect"]["reason_contains"] in p.reason

    def test_pack_prefers_free_box_over_tighter_paid(self):
        # Invariant 4b (2026-07-17): a $0 owned box wins over a tighter-fit PAID box. Tightest-fit
        # alone would pick the paid box (fewer free slots); marginal cost overrides it.
        fx = _fx("pack_prefers_free.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == -1
        assert "$0.0000" in p.reason

    def test_pack_marginal_ride_along_is_free(self):
        # Invariant 4b: riding a spare slot on a paid box kept alive by a longer occupant is $0
        # marginal, so it beats a tighter-fit paid box the task would EXTEND (a real cost).
        fx = _fx("pack_marginal_ride_along.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == 10
        assert "$0.0000" in p.reason

    def test_pack_box_preference_beats_tightest_fit(self):
        # Invariant 4b' (2026-08-09): among EQUAL-cost boxes an operator pack preference is checked
        # before tightest-fit, so a wholly idle preferred box fills FIRST. Without it the sort takes
        # `fewest free slots`, which ranks an idle box LAST among its equals — the emptier it is the
        # worse it sorts, so a newly joined box never gets packed.
        fx = _fx("pack_box_preference.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == -3
        assert "[preference 1]" in p.reason

    def test_pack_box_preference_never_outranks_cost(self):
        # Invariant 4b': the preference is a TIE-BREAK, not an override. A big preference on a PAID
        # box must not beat a $0 box, or a hand-set knob starts spending money.
        fx = _fx("pack_box_preference_never_outranks_cost.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == -1

    def test_pack_absent_preference_is_inert(self):
        # Invariant 4b': default 0 changes NOTHING. The pre-existing tightest-fit fixture carries no
        # `pack_preference` key at all, so this pins that the new term is inert for every box in the
        # fleet that has not opted in (and that a missing key never raises).
        fx = _fx("pack_tightest.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == fx["expect"]["target"]
        assert "preference" not in p.reason

    def test_pack_cheaper_dph_wins_when_both_extend(self):
        # Invariant 4b: between two equally-idle paid boxes the task extends fully, so cost is
        # dph-proportional — the cheaper box wins.
        fx = _fx("pack_cheaper_dph.json")
        p = _place(fx)
        assert p.action == "pack"
        assert p.target == 40


class TestRent:
    def test_bootstrap_rents_directly(self):
        fx = _fx("deadline_miss.json")
        p = _place(fx)
        assert p.action == "rent"
        assert p.offer["dph_total"] == fx["expect"]["offer_dph"]

    def test_offer_filters_pick_cheapest_qualifying(self):
        fx = _fx("offer_filters.json")
        p = _place(fx)
        assert p.action == "rent"
        assert p.offer["dph_total"] == fx["expect"]["offer_dph"]
        assert p.offer["machine_id"] == fx["expect"]["offer_machine"]

    def test_offer_unfittable_by_task_slots_is_skipped(self):
        # Invariant 4e fittability filter (retrospective bug 13): an offer whose
        # slots_for_offer(<hint>) < task.slots would be rented and never packed (churn).
        # Includes the 2026-07-21 fit-filter cases: a slice that can't host even ONE lane
        # under the task's hint (slots_for_offer = 0) is never bookable.
        fx = _fx("offer_unfittable.json")
        for case in fx["cases"]:
            p = _place(case)
            assert p.action == case["expect"]["action"], case["name"]
            if p.action == "rent":
                assert p.offer["dph_total"] == case["expect"]["offer_dph"]
                assert p.offer["machine_id"] == case["expect"]["offer_machine"]
            else:
                assert case["expect"]["reason_contains"] in p.reason

    def test_cores_per_dollar_ranking(self):
        # Invariant 4e cores-per-$ ranking (2026-07-21): rank by dph / lane_capacity (uncapped by
        # max_slots_cap AND demand), so a core-richer box wins even on an empty queue — the
        # degeneration the old demand-capped $/slot rule suffered. Ties break to cheaper dph.
        fx = _fx("offer_cores_per_dollar.json")
        for case in fx["cases"]:
            p = _place(case)
            assert p.action == "rent", case["name"]
            assert p.offer["machine_id"] == case["expect"]["offer_machine"], case["name"]
            assert case["expect"]["reason_contains"] in p.reason, case["name"]


class TestInfeasibleEst:
    def test_never_satisfiable_window_holds_not_rents(self):
        # Invariant 4a0 (retrospective bug 9): est=480 -> window 610 > ceiling 9h*60=540;
        # a live instance with free slots AND a qualifying offer are both present, yet the
        # only sane action is hold -- pack can't pass (a)'s window check and a fresh rent
        # couldn't either.
        fx = _fx("infeasible_est_hold.json")
        p = _place(fx)
        assert p.action == "hold"
        assert "infeasible_est" in p.reason

    def test_est_at_ceiling_boundary_falls_through(self):
        # est=424 -> window 424*1.25+10 = 540.0 <= 540: NOT infeasible; the bootstrap case
        # proceeds straight to rent exactly as deadline_miss.json specifies.
        fx = _fx("deadline_miss.json")
        fx["task"]["est_minutes"] = 424
        p = _place(fx)
        assert p.action == "rent"


class TestBudgetBalanceHold:
    def test_budget_hold_and_pack_ignores_budget(self):
        fx = _fx("budget_hold.json")
        for case in fx["cases"]:
            p = _place(case)
            assert p.action == case["expect"]["action"], case["name"]
            if p.action == "hold":
                assert case["expect"]["reason_contains"] in p.reason
            if p.action == "pack":
                assert p.target == case["expect"]["target"]

    def test_balance_hold(self):
        fx = _fx("balance_hold.json")
        p = _place(fx)
        assert p.action == "hold"
        assert fx["expect"]["reason_contains"] in p.reason


class TestSlotsForOffer:
    def test_formula_cases(self):
        for case in _fx("slots_for_offer.json")["cases"]:
            got = disp.slots_for_offer(case["offer"], case["resource_hint"], SHARED_SETTINGS)
            assert got == case["expect_slots"], case["name"]


class TestGraceExceedsFillTime:
    """The 2026-08-01 incident, as an executable invariant.

    `_reap_overpacked_boxes` calls a box over-packed when `running < advertised` after
    `ship_launch_grace_min`. But the box fills itself through `should_launch`, which starts ONE
    lane per `settle_minutes`. If the grace is shorter than `max_slots_cap x settle_minutes`, a
    perfectly healthy box that is merely still filling gets judged over-packed and has its learned
    concurrency ratcheted DOWN — permanently, because `_learn_overpack_cap` only ever takes min()
    and the cap is keyed per machine_id, so it poisons every future rental of that host.

    This fired five times in 30 minutes when `max_slots_cap` went 8 -> 16 (16 x 3 = 48 min against
    a 20 min grace) and drove the owned laptop from a manually-validated 6 down to 1.

    ⚠⚠ `cap x settle` IS A LOWER BOUND ON FILL TIME, NOT THE FILL TIME (measured 2026-08-02). This
    guard's arithmetic assumes `settle_minutes` is the only thing pacing a lane. It is not:
    `should_launch` also refuses for `cpu_load`, `gpu_util` and `vram`, each costing another poll on
    the box, and the dispatcher's own ship pacing decides when a task even ARRIVES. Measured against
    2510 real ship->start transitions in the live registry, time-in-`shipped` is median 2.5 min but
    **p90 13.5, p99 25.8 and max 143 min** — so the 8th lane of a healthy 8-slot box routinely takes
    ~12 min on its own, half of what this formula budgets for the ENTIRE box, and p99 clears the
    35-min grace with only 1.36x of margin.

    So passing this test does NOT mean the reaper is safe, and it must not be read that way — that
    reading is what made 20 -> 35 look like a fix when the false-positive rate after it was 15/18.
    The real protection is the evidence rule in `TestOverpackRatchetCannotContradictEvidence`: a cap
    may never contradict an observed peak. This stays as the cheap necessary condition it always
    was."""

    def test_grace_exceeds_the_LOWER_BOUND_on_the_time_a_full_box_needs_to_fill(self):
        import importlib.util as _il
        spec = _il.spec_from_file_location("sweep_supervisor", ROOT / "fleet/sweep_supervisor.py")
        sup = _il.module_from_spec(spec)
        sys.modules["sweep_supervisor"] = sup
        spec.loader.exec_module(sup)

        # The settle the fleet's workers pace by is the COORDINATOR's since invariant 8b; it is
        # held equal to the box-side built-in, which is what a box uses before its first probe.
        settle = disp.DEFAULT_SETTINGS["launch_gate"]["settle_minutes"]
        assert settle == sup.AUTO_DEFAULTS["settle_minutes"]
        cap = disp.DEFAULT_SETTINGS["max_slots_cap"]
        grace = disp.DEFAULT_SETTINGS["ship_launch_grace_min"]
        fill_minutes = cap * settle
        assert grace > fill_minutes, (
            f"ship_launch_grace_min={grace} <= max_slots_cap({cap}) x settle_minutes({settle}) "
            f"= {fill_minutes}: the over-pack reaper will fire on boxes that are still filling and "
            f"permanently ratchet their learned concurrency down")

    def test_the_box_is_handed_no_lane_count_to_enforce(self):
        """Invariant 8a: the number of tasks on a box is decided by PLACEMENT, and only there.

        ⚠ THIS TEST HAS NOW ENCODED THE DEFECT TWICE. It first asserted `max_slots_cap <=
        AUTO_DEFAULTS['max_slots']`, which made a fleet-wide constant of 8 look load-bearing. It
        then asserted that the worker overrides that constant with its own per-box `--max-slots` —
        i.e. that the box HAS a ceiling — and that ceiling was a copy of `slots_total` taken once at
        launch and re-exec'd verbatim by every self-update, so raising a box's slots left its worker
        refusing the extra work (four recorded occurrences). The fix is not a better copy; it is no
        copy. So what must hold is that `should_launch` is called with the count arm DISABLED and
        that the worker keeps no count of its own. (That the coordinator's launch line carries none
        is pinned on the real command in `TestReapUnreachableOwned`.)"""
        src = (ROOT / "fleet/spool_worker.py").read_text()
        assert 'auto_cfg = {**AUTO_DEFAULTS, **cfg, "max_slots": NO_LANE_COUNT}' in src, (
            "spool_worker must disable should_launch's max_slots arm; AUTO_DEFAULTS['max_slots'] "
            "(8, the manual sweep lane's) silently caps every box if it reaches should_launch")
        assert 'NO_LANE_COUNT = float("inf")' in src
        assert "should_launch(seen, self._vram_lane_max, n_live, since_min, auto_cfg)" in src
        assert "self.max_slots" not in src, "the worker must not hold a lane count at all"

    def test_with_the_count_arm_disabled_only_measured_load_holds_a_launch(self):
        """The behavioural half, on the real `should_launch`. With the manual sweep lane's constant
        a 9th lane is refused for `max_slots`; with the count arm disabled a 9th and a 30th are
        admitted, and the same call is still refused the moment the box is measurably loaded."""
        import importlib.util as _il
        spec = _il.spec_from_file_location("sweep_supervisor", ROOT / "fleet/sweep_supervisor.py")
        sup = _il.module_from_spec(spec)
        sys.modules["sweep_supervisor"] = sup
        spec.loader.exec_module(sup)

        hw = {"load1": 4.0, "cores": 32, "gpu_util": 5.0, "vram_free_gb": 10.0,
              "vram_total_gb": 12.0}
        no_count = {**sup.AUTO_DEFAULTS, "max_slots": float("inf")}
        ok_global, why_global = sup.should_launch(hw, 0.6, 8, 99.0, sup.AUTO_DEFAULTS)
        assert not ok_global and why_global == "max_slots"   # the wall, as it was
        assert sup.should_launch(hw, 0.6, 8, 99.0, no_count)[0]
        assert sup.should_launch(hw, 0.6, 30, 99.0, no_count)[0]
        loaded = {**hw, "load1": 31.5}
        assert sup.should_launch(loaded, 0.6, 30, 99.0, no_count) == (False, "cpu_load")

    def test_raising_max_slots_cap_carries_a_migration_so_it_is_not_a_silent_no_op(self):
        """`_ensure_settings` only INSERTs keys the registry LACKS, so changing a code default that
        is already seeded changes NOTHING on a live fleet — the running system keeps the old value
        while the code, spec and tests all agree on the new one. `max_slots_cap` has been seeded
        since July, so the 8 -> 11 raise needs a `_SETTING_MIGRATIONS` row to actually land. Three
        existing rows exist for exactly this bug; this pins the fourth."""
        migrated = {k: (old, new) for k, old, new in disp._SETTING_MIGRATIONS}
        assert "max_slots_cap" in migrated, (
            "max_slots_cap changed without a _SETTING_MIGRATIONS row — a live registry seeded with "
            "the old value will silently keep it")
        _old, new = migrated["max_slots_cap"]
        assert new == disp.DEFAULT_SETTINGS["max_slots_cap"], (
            f"the migration targets {new} but the code default is "
            f"{disp.DEFAULT_SETTINGS['max_slots_cap']} — they must agree or the fleet lands on "
            f"neither")

    def test_settle_minutes_is_not_pacing_a_gate_that_is_anywhere_near_binding(self):
        """Records WHY the 8 -> 11 raise did not touch `settle_minutes`, so the next person to want
        a deeper cap reaches for the right knob.

        `settle_minutes` paces the launch loop so the gates AFTER it read steady state — chiefly
        `load1 >= cores - 1`, whose signal is a 1-minute load average and therefore lagged by
        construction. Measured 2026-08-04 over ~5k `box_measured` samples, load1 rises ~1.0 per lane
        (0.7 at 0 lanes, 3.1 at 2, 6.2 at 6, 8.3 at 7) against a threshold of cores-1 = 19..55, i.e.
        10-32 cores of headroom at every depth the fleet has ever run, and the gate has never fired.
        Nor is there a throughput ramp to protect: 95% of 930 runs were at >=90% of steady-state
        steps/sec by the first logged interval. So settle is not load-bearing for SAFETY here — it
        only costs fill latency — and this test pins the arithmetic that makes that true, so it
        starts failing if a future lane footprint makes the gate reachable."""
        cores, per_lane = 24, 1.02           # measured median draw per lane
        for k in (8, 11, 16):
            projected_load1 = 0.7 + per_lane * k
            assert projected_load1 < cores - 1, (
                f"at K={k} the projected load1 {projected_load1:.1f} reaches the cpu_load gate "
                f"({cores - 1}) on a {cores}-core box — settle_minutes now paces a gate that can "
                f"actually fire, and shortening it is no longer free")


class TestOfferRamAxis:
    """Invariant 27: the rent filter admits on every axis the admission gate enforces."""

    def test_cpu_ram_is_the_SLICE_not_the_host_so_it_is_never_rescaled(self):
        """THE FACT THE WHOLE AXIS TURNS ON. `cpu_ram` already describes the rented slice: across
        400 live offers it is 2.61 GB per EFFECTIVE core (sane) but 0.38 GB per HOST core (a
        56-core host with 10.5 GB — absurd). Re-scaling by `cpu_cores_effective / cpu_cores`
        under-predicts ~3x and would make the fit filter drop nearly every offer."""
        raw = {"cpu_ram": 10752, "cpu_cores": 56, "cpu_cores_effective": 9.33}   # a real offer shape
        got = disp.offer_ram_gb(raw, {"offer_ram_derate": 1.0})
        assert got == pytest.approx(10.5, abs=0.05)          # the slice, untouched by 56/9.33
        assert got > 10.0                                     # NOT 10.5 * (9.33/56) = 1.75

    def test_derate_makes_the_advertised_figure_an_under_promise(self):
        """Offers over-promise ~20% (advertised 2.61 GB/eff-core vs measured 2.18), so the derate
        turns the number we BUY on into a conservative one."""
        raw = {"cpu_ram": 10240}                              # 10.0 GB advertised
        assert disp.offer_ram_gb(raw, {"offer_ram_derate": 0.8}) == pytest.approx(8.0)

    def test_absent_or_zero_ram_abstains(self):
        """A missing field must never silently deny every offer — same fail-open idiom as the VRAM
        gate on an unmeasured GPU."""
        for raw in ({}, {"cpu_ram": 0}, {"cpu_ram": None}):
            assert disp.offer_ram_gb(raw, dict(disp.DEFAULT_SETTINGS)) is None

    def test_eligible_offers_carries_ram_through_to_the_placer(self):
        raw = [{"id": 1, "dph_total": 0.05, "machine_id": 9, "gpu_name": "RTX 3060",
                "gpu_ram": 12288, "cpu_cores_effective": 24, "cpu_ram": 61440,
                "reliability2": 0.99}]
        mapped, dropped = disp.eligible_offers(raw, dict(disp.DEFAULT_SETTINGS))
        assert dropped == 0 and mapped[0]["ram_gb"] == pytest.approx(48.0)   # 60 GB x 0.8


class TestCpuSpeedTerm:
    """Invariant 4f: the 4e ranking prices lane COUNT; this prices lane SPEED.

    Grounded in measurement, not preference — across 4,987 `box_measured` samples GPU utilisation is
    0% at p50 AND p90 with 0.00 GB VRAM used, and GPU model does not predict realised steps/sec
    (RTX 3060 1.00, RTX 3090 1.00, RTX 3060 Ti 1.08, RTX 2080 Ti 0.96 on 608 TB-instrumented runs).
    A CPU-bound fleet that ranks only on cores-per-dollar is blind to half the throughput term."""

    S = dict(disp.DEFAULT_SETTINGS)
    HINT = {"vram_per_lane_gb": 1.0, "cores_per_lane": 1, "ram_per_lane_gb": 2.0}

    def _offer(self, **kw):
        o = {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 12.0, "cpu_cores_effective": 8.0,
             "ram_gb": 32.0}
        o.update(kw)
        return o

    def test_a_faster_clock_lowers_the_value_density_of_an_identical_box(self):
        """The whole point: two boxes identical in price, cores, RAM and VRAM must NOT tie when one
        runs its lanes faster."""
        slow = self._offer(cpu_ghz=2.2)
        fast = self._offer(cpu_ghz=3.8)
        assert disp.value_density(fast, self.HINT, self.S) \
            < disp.value_density(slow, self.HINT, self.S)

    def test_a_missing_cpu_ghz_is_NEUTRAL_not_zero(self):
        """Fail-open, the same idiom the reliability and RAM gates use. If a renamed marketplace
        field made every offer score 0 the ranking would invert; if it made every offer score
        infinity the queue would starve. Neither may happen — an unlabelled offer ranks exactly as
        it did before 4f existed."""
        for missing in ({}, {"cpu_ghz": None}, {"cpu_ghz": 0}, {"cpu_ghz": "bogus"}):
            o = self._offer(**missing)
            assert disp.offer_speed_factor(o, self.S) == 1.0
            assert disp.value_density(o, self.HINT, self.S) == pytest.approx(
                o["dph_total"] / disp.lane_capacity(o, self.HINT, self.S))

    def test_the_clamp_bounds_how_far_a_base_clock_may_move_the_ranking(self):
        """`cpu_ghz` is base clock and ignores IPC, so it is a PRIOR. The clamp is what stops it
        overriding the cores-per-$ and reliability decisions that are grounded in measurement."""
        lo, hi = self.S["cpu_speed_clamp"]
        assert disp.offer_speed_factor(self._offer(cpu_ghz=0.3), self.S) == pytest.approx(lo)
        assert disp.offer_speed_factor(self._offer(cpu_ghz=99.0), self.S) == pytest.approx(hi)

    def test_weight_zero_restores_the_pure_cores_per_dollar_ranking(self):
        """The knob must be genuinely reversible: a fleet that wants 4e alone gets 4e alone, bit for
        bit, so this can be switched off without reasoning about the clamp or the reference."""
        off = {**self.S, "cpu_speed_weight": 0.0}
        o = self._offer(cpu_ghz=3.8)
        assert disp.offer_speed_factor(o, off) == 1.0
        assert disp.value_density(o, self.HINT, off) == pytest.approx(
            o["dph_total"] / disp.lane_capacity(o, self.HINT, off))

    def test_speed_cannot_outrank_a_box_with_many_more_lanes(self):
        """Guards the failure mode this change could introduce: the clamp tops out at 1.35, so a
        fast 2-core sliver must still lose to a slower core-rich box. Lane count stays primary."""
        sliver = self._offer(id=1, cpu_cores_effective=2.0, cpu_ghz=4.5)
        wide = self._offer(id=2, cpu_cores_effective=16.0, cpu_ghz=2.4)
        assert disp.value_density(wide, self.HINT, self.S) \
            < disp.value_density(sliver, self.HINT, self.S)

    def test_eligible_offers_carries_the_cpu_identity_through(self):
        """`cpu_name` is what a later re-fit joins realised steps/sec against — recording it is the
        reason the term can ever stop being a guess."""
        raw = [{"id": 1, "dph_total": 0.05, "machine_id": 9, "gpu_name": "RTX 3060",
                "gpu_ram": 12288, "cpu_cores_effective": 24, "cpu_ram": 61440,
                "reliability2": 0.99, "cpu_ghz": 3.4, "cpu_name": "Ryzen 9 5950X ",
                "cpu_arch": "amd64"}]
        mapped, _ = disp.eligible_offers(raw, self.S)
        assert mapped[0]["cpu_ghz"] == 3.4
        assert mapped[0]["cpu_name"] == "Ryzen 9 5950X"       # trailing marketplace whitespace gone
        assert mapped[0]["cpu_arch"] == "amd64"

    def test_the_placer_actually_rents_the_faster_of_two_equal_boxes(self):
        """End to end through `place`, not just the ranking helper — the two differ only in clock."""
        task = {"id": "t1", "slots": 1, "est_minutes": 60, "priority": 50,
                "resource_hint": self.HINT}
        offers = [self._offer(id=1, machine_id=11, cpu_ghz=2.2),
                  self._offer(id=2, machine_id=22, cpu_ghz=3.8)]
        s = {**self.S, "current_balance": 100.0, "current_rate": 0.0, "max_hourly_usd": 5.0,
             "deny_machine_ids": set()}
        p = disp.place(task, [], offers, [], s, time.time())
        assert p.action == "rent" and p.offer["machine_id"] == 22

    def test_the_counterfactual_ranks_on_the_SAME_key_the_placer_used(self):
        """If the diagnostic kept the old per-lane key it would report `qualified_not_chosen` — a
        'placer bug' signal — every time the speed term legitimately decided a rent."""
        task = {"slots": 1, "resource_hint": self.HINT}
        raw = [{"id": 1, "dph_total": 0.048, "gpu_ram": 12288, "cpu_cores_effective": 8,
                "cpu_ram": 61440, "reliability2": 0.99, "gpu_name": "3060", "cpu_ghz": 2.0},
               {"id": 2, "dph_total": 0.050, "gpu_ram": 12288, "cpu_cores_effective": 8,
                "cpu_ram": 61440, "reliability2": 0.99, "gpu_name": "3060", "cpu_ghz": 3.8}]
        chosen = {"dph_total": 0.050, "gpu_name": "3060", "gpu_ram_gb": 12.0, "cpu_ghz": 3.8,
                  "cpu_name": "fast", "cpu_cores_effective": 8, "ram_gb": 48.0,
                  "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, task, self.S)
        assert cf["cheapest_alt"]["reason"] == "worse_value_density"
        assert cf["chosen"]["cpu_name"] == "fast"
        assert cf["chosen"]["speed_factor"] > 1.0

    def test_ranking_cannot_prefer_an_offer_the_fit_filter_will_drop(self):
        """`lane_capacity` (4e's value-density denominator) must count the same axes as
        `slots_for_offer`, or a RAM-starved box ranks as a cheap high-lane box and gets rented."""
        settings = dict(disp.DEFAULT_SETTINGS)
        hint = {"vram_per_lane_gb": 0.25, "cores_per_lane": 1.3, "ram_per_lane_gb": 2.34}
        starved = {"gpu_ram_gb": 8.0, "cpu_cores_effective": 16.0, "ram_gb": 5.92}
        assert disp.lane_capacity(starved, hint, settings) == 2      # not 12 (cores) or 32 (vram)
        assert disp.slots_for_offer(starved, hint, settings) == 2

    def test_the_40000042_waste_is_now_unbookable(self):
        """END TO END on the live incident: rented for an 8-slot task, came up with 7.4 GB, was
        refused on RAM by `_headroom_fits`, and was torn down 5 s after its first measurement —
        $0.019 for zero tasks. `slots_for_offer` must now report fewer lanes than the task needs,
        so 4e's fittability filter drops the offer before any money is spent."""
        settings = dict(disp.DEFAULT_SETTINGS)
        hint = {"vram_per_lane_gb": 0.25, "cores_per_lane": 1.3, "ram_per_lane_gb": 2.34}
        raw = {"cpu_ram": int(7.4 * 1024), "cpu_cores": 16, "cpu_cores_effective": 16.0}
        offer = {"gpu_ram_gb": 8.0, "cpu_cores_effective": 16.0,
                 "ram_gb": disp.offer_ram_gb(raw, settings)}
        assert disp.slots_for_offer(offer, hint, settings) < 8
        # and the box that DID run it (60.2 GB) is still bookable at full depth
        rich = {"gpu_ram_gb": 12.0, "cpu_cores_effective": 24.0,
                "ram_gb": disp.offer_ram_gb({"cpu_ram": int(62.7 * 1024)}, settings)}
        assert disp.slots_for_offer(rich, hint, settings) == settings["max_slots_cap"]


class TestSlotFreeingSoonAndBacklog:
    def test_slot_freeing_soon_cases(self):
        for case in _fx("slot_freeing_soon.json")["cases"]:
            p = _place(case)
            assert p.action == "hold", case["name"]
            assert case["expect"]["reason_contains"] in p.reason, case["name"]

    def test_overdue_occupant_does_not_block_rent(self):
        # Invariant 4c overdue-exclusion (2026-07-15): a running task overdue by MORE than
        # rent_patience_min has an expired estimate — it can't count as "freeing soon", so a full
        # fleet of chronically-overdue long-runs rents for a real backlog instead of holding forever.
        # A slightly-overdue occupant (within patience) still holds. Observed live: 8 tasks queued
        # 48min behind 21-hour runs on a full 2-box fleet with ample budget.
        for case in _fx("overdue_occupant_rent.json")["cases"]:
            p = _place(case)
            assert p.action == case["expect"]["action"], case["name"]
            if p.action == "rent":
                assert p.offer["dph_total"] == case["expect"]["offer_dph"], case["name"]
            else:
                assert case["expect"]["reason_contains"] in p.reason, case["name"]

    def test_backlog_too_small_cases(self):
        for case in _fx("backlog_too_small.json")["cases"]:
            p = _place(case)
            assert p.action == case["expect"]["action"], case["name"]
            if p.action == "rent":
                assert p.offer["dph_total"] == case["expect"]["offer_dph"]
            else:
                assert case["expect"]["reason_contains"] in p.reason

    def test_priority_bypass_when_no_preemption_candidate(self):
        fx = _fx("priority_bypass_no_candidate.json")
        p = _place(fx)
        assert p.action == "rent"
        assert p.offer["dph_total"] == fx["expect"]["offer_dph"]


class TestPreemption:
    def test_preempt_cases(self):
        for case in _fx("preempt.json")["cases"]:
            p = _place_mech(case)
            assert p.action == case["expect"]["action"], case["name"]
            if p.action == "preempt":
                assert p.target == case["expect"]["target"]
                assert p.victims == case["expect"]["victims"]
            elif p.action == "rent":
                assert p.offer["dph_total"] == case["expect"]["offer_dph"]
            else:
                assert case["expect"]["reason_contains"] in p.reason, case["name"]


class TestTeardown:
    def test_teardown_cases(self):
        for case in _fx("teardown.json")["cases"]:
            got = disp.should_teardown(case["instance"], case["queued"], 0, SHARED_SETTINGS)
            assert list(got) == case["expect"], case["name"]


class TestWarmSlotCapIsBoundedInSlotsToo:
    """Invariant 11a's SECOND bound, and the one that actually constrains. `warm_idle_max` caps warm
    capacity by BOX COUNT; the owner stated the same directive in SLOTS the same day ("58/98 slots
    in use so almost 50% of our spend is going to waste ... no more than 10 free slots kept warm"),
    and the budget is FLEET-WIDE — free slots on boxes that still hold work are spent against it
    first ("make sure max_warm_free_slots includes non-empty boxes that have free slots")."""

    def _empty(self, iid, dph=0.05, slots=6):
        return {"id": iid, "state": "live", "slots_total": slots, "occupants": [],
                "idle_minutes": 99, "minutes_to_hard_cap": 3000, "dph_usd": dph, "source": "vast"}

    def _busy(self, iid, slots=6, used=1, dph=0.05, owned=False):
        return {**self._empty(iid, dph=0.0 if owned else dph, slots=slots),
                "source": "owned" if owned else "vast",
                "occupants": [{"id": f"t{iid}_{n}", "slots": 1, "state": "running"}
                              for n in range(used)]}

    def _q(self, n=1):
        return [{"id": f"q{i}", "slots": 1, "est_minutes": 20, "priority": 50} for i in range(n)]

    # ---- the fleet-wide budget (owner directive 2026-07-30) ----

    def test_free_slots_on_BUSY_paid_boxes_are_spent_against_the_cap(self):
        """A busy box bills for its occupant regardless, so its spare lanes are free capacity the
        queue can already reach — renting a second pool of them and still calling the fleet inside
        the cap is exactly the accounting error this closes."""
        fleet = [self._busy(9, slots=8, used=1), self._empty(1)]   # 7 free on the busy box
        assert disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS) == set(), (
            "7 committed-free + a 6-slot warm box = 13 free slots against a cap of 10")
        # ...and once that box fills up, the same empty box becomes holdable again.
        fleet = [self._busy(9, slots=8, used=7), self._empty(1)]   # 1 free
        assert disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS) == {1}

    def test_free_slots_on_a_busy_OWNED_box_count_too(self):
        """Keyed on 'still holds work', not on source: an owned box's spare lanes are $0 capacity
        the queue reaches first, and an owned box is never torn down, so they are at least as
        durable as a rental's."""
        fleet = [self._busy(-2, slots=12, used=2, owned=True), self._empty(1)]  # 10 free, $0
        assert disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS) == set(), (
            "the cap is already exhausted by free home capacity — paying to keep a rental warm on "
            "top of it buys nothing")

    def test_the_live_2026_07_30_fleet_holds_nothing(self):
        """The measured fleet: 10 free slots on owned boxes + 10 on busy paid boxes = 20 committed
        free, against a cap of 10. Allowance 0."""
        fleet = [self._busy(-1, slots=6, used=2, owned=True),      # 4 free
                 self._busy(-2, slots=12, used=6, owned=True),     # 6 free
                 self._busy(10, slots=8, used=3), self._busy(11, slots=8, used=3),  # 10 free
                 self._empty(1), self._empty(2)]
        assert disp.warm_hold_grants(fleet, self._q(3), SHARED_SETTINGS) == set()

    # ---- the two ceilings ----

    def test_the_slot_cap_binds_before_the_box_cap(self):
        """Four empty 6-slot boxes, nothing else free: the box cap would keep two (12 slots), the
        slot cap keeps one. This is the 40-free-slot fleet that prompted the directive."""
        fleet = [self._empty(i) for i in (1, 2, 3, 4)]
        granted = disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS)
        assert granted == {1}, "6 + 6 = 12 exceeds the 10-slot cap, so the second box is released"
        assert sum(6 for i in fleet if i["id"] in granted) <= SHARED_SETTINGS["max_warm_free_slots"]

    def test_the_box_cap_binds_when_the_slots_fit(self):
        """`warm_idle_max = 2` is real, not dead: small boxes let both be held inside the cap."""
        fleet = [self._empty(i, slots=4) for i in (1, 2, 3)]       # 4 + 4 = 8 <= 10
        assert disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS) == {1, 2}

    def test_a_box_bigger_than_the_whole_cap_is_NOT_kept(self):
        """The cap is a ceiling, not a target — there is no first-grant carve-out. Noted in the
        spec: if `max_slots_cap` ever exceeds `max_warm_free_slots`, 11a keeps nothing at all."""
        assert disp.warm_hold_grants([self._empty(1, slots=12)], self._q(), SHARED_SETTINGS) == set()

    def test_it_keeps_the_CHEAPEST_boxes_and_releases_the_expensive_ones(self):
        fleet = [self._empty(1, dph=0.09), self._empty(2, dph=0.04), self._empty(3, dph=0.07)]
        assert disp.warm_hold_grants(fleet, self._q(), SHARED_SETTINGS) == {2}

    def test_an_empty_owned_box_is_never_a_candidate(self):
        owned = {**self._empty(-1, dph=0.0), "source": "owned"}
        assert -1 not in disp.warm_hold_grants([owned, self._empty(1)], self._q(), SHARED_SETTINGS)

    def test_the_default_slot_cap_is_TEN(self):
        assert disp.DEFAULT_SETTINGS["max_warm_free_slots"] == 10

    def test_the_premise_empty_paid_boxes_really_do_rank_last_for_placement(self):
        """WHY 11a is needed at all, pinned so a future change to invariant 4b cannot silently make
        it moot. The idle timer was not merely unreachable — placement structurally REFUSES to use
        an empty paid box: `_pack_cost` charges it the FULL `dph x window` (no occupant to ride
        along with) versus $0.0000 for a spare slot on a busy box. So placement will not use the box
        because it assumes it is about to be torn down, while teardown would not destroy it because
        it assumed the queue would use it. Live 2026-07-30 20:49: twelve tasks packed onto boxes
        with 1-4 free slots at "$0.0000 marginal" while four empty 6-slot boxes got nothing."""
        task = {"id": "t", "slots": 1, "est_minutes": 60, "priority": 50}
        empty = self._empty(1)
        busy = {**self._empty(2), "occupants": [
            {"id": "o", "slots": 1, "state": "running", "est_minutes": 600,
             "running_minutes_ago": 5}]}
        assert disp._pack_cost(task, busy, SHARED_SETTINGS) == 0.0
        assert disp._pack_cost(task, empty, SHARED_SETTINGS) > 0.0, (
            "if an empty paid box ever stops being the most expensive pack target, the deadlock "
            "11a works around is gone and the cap should be revisited")


class TestConsolidation:
    def _norm(self, drains):
        # normalize to [[instance_id, sorted task_ids, sorted targets], ...] for order-stable compare
        return [[d["instance_id"], sorted(d["task_ids"]), sorted(d["targets"])] for d in drains]

    def test_consolidation_cases(self):
        for case in _fx("consolidate.json")["cases"]:
            got = disp.consolidation_drains(case["instances"], _mech_settings(case.get("settings_overrides")), 0)
            assert self._norm(got) == case["expect"], case["name"]

    def test_a_box_the_backlog_still_needs_is_not_drained(self):
        """Invariant 21e. Draining only pays off if the box then goes IDLE and is torn down — but
        `should_teardown` refuses to destroy any box a queued task still fits, so under a backlog the
        drained box stays `live`, gets REPACKED, and the drain buys nothing while costing a preempt
        per occupant.

        Measured 2026-07-29: 10 of 10 consolidations that day ended with the box still `live` (zero
        teardowns) while 51 tasks were preempted and 6 lost their checkpoints. One box was drained at
        04:49 and had work shipped back 10 min later. The contradiction was visible inside 50s:
        `rent_created` x4 at 06:39:24-56 (26 queued, no capacity) then `consolidate` at 06:40:14."""
        case = next(c for c in _fx("consolidate.json")["cases"]
                    if c["name"] == "drain_worst_paid_onto_owned")
        settings = _mech_settings()
        # no backlog -> unchanged behaviour, the box is still worth reclaiming
        assert self._norm(disp.consolidation_drains(case["instances"], settings, 0, [])) == case["expect"]
        # a queued task that FITS the paid box -> it is needed, so do not drain it
        queued = [{"id": "q1", "slots": 1, "est_minutes": 60}]
        assert disp.consolidation_drains(case["instances"], settings, 0, queued) == [], (
            "drained a box the backlog still needs — should_teardown will then refuse to destroy it, "
            "so the preempts are pure churn")

    def _paid_box(self, dph=0.06, occ=1, est=480, ran=10, slots=8):
        """One owned target + one paid source with `occ` running tasks.

        The target is deliberately OVERSIZED (32 slots / 64GB): invariant 21a refuses a drain whose
        whole load cannot be relocated, and an undersized target would block these cases on CAPACITY
        before the 21g value test is ever consulted — making the test pass for the wrong reason.
        (It did: mutation testing showed the value test could be deleted with all 9 cases green.)"""
        return [
            {"id": -1, "state": "live", "dph_usd": 0.0, "source": "owned", "slots_total": 32,
             "vram_total_gb": 64.0, "vram_used_gb": 0.0, "occupants": []},
            {"id": 2, "state": "live", "dph_usd": dph, "source": "vast", "slots_total": slots,
             "vram_total_gb": 16.0, "vram_used_gb": 0.0,
             "occupants": [{"id": f"t{i}", "slots": 1, "state": "running",
                            "est_minutes": est, "running_minutes_ago": ran} for i in range(occ)]},
        ]

    def test_a_drain_must_earn_its_disruption_per_task(self):
        """Invariant 21g, owner directive: "saving a few cents on a repack is penny wise, pound
        foolish". MEASURED over 95 real drains: the median preempted 10 tasks to save an upper-bound
        $0.174. The bar is per TASK because that is the unit of disruption."""
        st = _mech_settings()
        # 1 task, 8h of est window left on a $0.06/hr box -> saves ~$0.50, well over $0.05
        assert disp.consolidation_drains(self._paid_box(occ=1, est=480, ran=10), st, 0) != []
        # SAME saving, but spread over 10 tasks -> needs $0.50 and the box only frees one box-hour
        cheap = self._paid_box(occ=10, est=40, ran=10, slots=16)   # ~$0.04 saved, 10 preempts
        assert disp.consolidation_drains(cheap, st, 0) == [], (
            "drained 10 running tasks to save a few cents — exactly the penny-wise case")

    def test_a_drain_may_not_interrupt_more_runs_than_a_reclaim_is_worth(self):
        """Invariant 21j. Owner, with 21g/21h/21i all live: "I still keep hearing about preempts
        that cause trouble for the task owner." Correct, and the earlier gates could not have fixed
        it — 21a forces a WHOLE-BOX drain, so a full box costs one preempt per occupant, and 21g's
        per-task price still clears a 10-task drain whenever the box has a long remaining life.

        MEASURED over 24h / 81 drains / 313 preempts: consolidation caused 93.4% of every preempt in
        the fleet, 27 lost their checkpoint outright, and it bought an upper-bound $3.51 — ~$0.011
        per preempt. A drain that SUCCEEDED never preempted more than 8 tasks (median 3); the futile
        ones ran 20-77. A cap of 4 keeps 19 of 31 reclaims and $2.10 of $3.51 while avoiding 215 of
        313 preempts."""
        st = _mech_settings()
        # a 3-occupant box with a long window: valuable AND cheap to vacate -> still drained
        assert disp.consolidation_drains(self._paid_box(occ=3, est=480, ran=10, slots=16), st, 0) \
            != [], "blocked a small drain that is exactly the good case"
        # 10 occupants: even with a window long enough to clear the per-task price, too many runs
        big = self._paid_box(occ=10, est=4800, ran=10, slots=16)
        assert disp.consolidation_drains(big, st, 0) == [], (
            "drained a box holding 10 running experiments — the case the owner kept hearing about")
        # and the ceiling is the reason, not the price: the value gate passes this fleet
        assert st["consolidate_max_preempts"] < 10
        # BOUNDARY: exactly `consolidate_max_preempts` occupants is ALLOWED, not refused. A `>=`
        # here would quietly shave one off the cap and reject the largest drain we decided is worth
        # doing — invisible to any test that only probes 3-vs-10. (Caught by mutation testing.)
        cap = st["consolidate_max_preempts"]
        assert disp.consolidation_drains(
            self._paid_box(occ=cap, est=4800, ran=10, slots=16), st, 0) != [], (
            f"refused a drain of exactly {cap} occupants — the cap is off by one")
        assert disp.consolidation_drains(
            self._paid_box(occ=cap + 1, est=4800, ran=10, slots=16), st, 0) == [], (
            f"allowed {cap + 1} occupants, one over the cap")

    def test_the_ceiling_is_above_the_typical_successful_drain(self):
        """Sized from the DISTRIBUTION, not picked: successful drains cluster at 0-8 preempts with a
        median of 3, so a ceiling at or below 3 would start refusing the ordinary good case."""
        assert disp.DEFAULT_SETTINGS["consolidate_max_preempts"] >= 4

    def test_an_overdue_box_is_still_reclaimable(self):
        """The value test must not silently override invariant 21d: past its estimate the remaining
        life is UNKNOWN, so pricing it at $0 is an artefact. A chronically-overdue paid box is what
        we most want back."""
        overdue = self._paid_box(occ=1, est=480, ran=900)          # 900min run vs a 600min window
        assert disp.consolidation_drains(overdue, _mech_settings(), 0) != []

    def test_a_box_drained_recently_is_not_drained_again(self):
        """Invariant 21g cooldown. Repack latency is MEDIAN 5 MIN — inside the 10-min idle timeout a
        drain needs to end in a teardown — so without this the same box is drained repeatedly and
        reclaims nothing. Observed: one box drained 14x in 13h for 47 preempts and zero teardowns."""
        st = _mech_settings()
        boxes = self._paid_box(occ=1, est=480, ran=10)
        now = 10_000.0
        assert disp.consolidation_drains(boxes, st, now) != []          # no history -> allowed
        recent = {2: now - 5 * 60}                                       # drained 5 min ago
        assert disp.consolidation_drains(boxes, st, now, None, recent) == [], (
            "re-drained a box that was drained 5 min ago and refilled — the repack race")
        old = {2: now - 120 * 60}                                        # drained 2 h ago
        assert disp.consolidation_drains(boxes, st, now, None, old) != [], (
            "cooldown must EXPIRE — it throttles the race, it does not disable consolidation")

    def test_a_backlog_task_too_big_for_the_box_does_not_block_reclaiming_it(self):
        """The guard mirrors `should_teardown`'s feasibility test, so a queued task that could never
        run on that box must NOT keep it alive — otherwise one oversized task pins every rental."""
        case = next(c for c in _fx("consolidate.json")["cases"]
                    if c["name"] == "drain_worst_paid_onto_owned")
        settings = _mech_settings()
        queued = [{"id": "big", "slots": 99, "est_minutes": 60}]   # cannot fit slots_total=4
        assert self._norm(disp.consolidation_drains(case["instances"], settings, 0, queued)) == case["expect"]

    def test_dph_reclaimed_is_the_vacated_box_rate(self):
        # The reclaimed $/hr reported is the SOURCE box's dph (what teardown will stop billing).
        case = next(c for c in _fx("consolidate.json")["cases"]
                    if c["name"] == "drain_worst_paid_onto_owned")
        [d] = disp.consolidation_drains(case["instances"], _mech_settings(), 0)
        assert d["dph_reclaimed"] == 0.076

    def test_greedy_pool_never_double_books_a_free_slot(self):
        # One free owned slot, two equal-priced paid boxes: exactly ONE is vacated, not both.
        owned = {"id": -1, "state": "live", "dph_usd": 0.0, "source": "owned", "slots_total": 3,
                 "vram_total_gb": 12.0, "vram_used_gb": 2.0,
                 "occupants": [{"id": "a", "slots": 1, "state": "running", "est_minutes": 360, "running_minutes_ago": 30},
                               {"id": "b", "slots": 1, "state": "running", "est_minutes": 360, "running_minutes_ago": 30}]}
        p1 = {"id": 1, "state": "live", "dph_usd": 0.06, "source": "vast", "slots_total": 1,
              "vram_total_gb": 11.0, "vram_used_gb": 3.0,
              "occupants": [{"id": "t1", "slots": 1, "state": "running", "est_minutes": 480, "running_minutes_ago": 100}]}
        p2 = {"id": 2, "state": "live", "dph_usd": 0.06, "source": "vast", "slots_total": 1,
              "vram_total_gb": 11.0, "vram_used_gb": 3.0,
              "occupants": [{"id": "t2", "slots": 1, "state": "running", "est_minutes": 480, "running_minutes_ago": 100}]}
        drains = disp.consolidation_drains([owned, p1, p2], _mech_settings(), 0)
        assert len(drains) == 1, "only one box can fit the single free owned slot"

    def test_measured_free_vram_none_when_unsampled(self):
        assert disp._measured_free_vram({"vram_total_gb": None, "vram_used_gb": None}) is None
        assert disp._measured_free_vram({"vram_total_gb": 12.0, "vram_used_gb": 5.0}) == 7.0


class TestRetryDecision:
    def test_retry_decision_cases(self):
        for case in _fx("retry.json")["cases"]:
            assert disp.retry_decision(case["task"]) == case["expect"]


class TestRunOrTimeout:
    def test_group_kill_prevents_grandchild_pipe_wedge(self):
        # Regression (2026-07-14 live fleet wedge): a backgrounded GRANDCHILD (`sleep 30`) inherits
        # the stdout pipe and outlives its parent `sh`, so plain subprocess.run(timeout=...) SIGKILLs
        # only `sh` then blocks FOREVER in communicate() on the pipe `sleep` still holds. The real
        # path must run in a new process group and group-kill on timeout: return rc 124 PROMPTLY.
        cmd = ["sh", "-c", "sleep 30 & echo started"]
        t0 = time.monotonic()
        r = disp._run_or_timeout(subprocess.run, cmd, timeout=1)
        elapsed = time.monotonic() - t0
        assert r.returncode == 124
        assert elapsed < 10, f"wedged: took {elapsed:.1f}s (old bug hung indefinitely)"

    def test_fast_command_returns_output_normally(self):
        r = disp._run_or_timeout(subprocess.run, ["sh", "-c", "echo hi"], timeout=10)
        assert r.returncode == 0 and r.stdout.strip() == "hi"


class TestStallDecision:
    def test_stall_decision_cases(self):
        for case in _fx("stall.json")["cases"]:
            got = disp.stall_decision(case["task"], case["now"], case["settings"])
            assert got == case["expect"], case["name"]


class TestReconcile:
    def test_reconcile_divergences(self):
        # Includes the foreign-registry case (invariant 3b as revised 2026-07-09, retrospective
        # bug 7): runq_zzz's task-id is unknown to this registry -> foreign, never adopted.
        fx = _fx("reconcile.json")
        got = disp.reconcile(fx["db_instances"], fx["vast_instances"],
                             frozenset(fx["db_task_ids"]))
        slim = [{"type": d["type"], "instance_id": d["instance_id"]} for d in got]
        assert slim == fx["expect"]

    def test_no_known_tasks_means_no_adoption_at_all(self):
        # A fresh/empty registry must never adopt anyone's runq boxes (the split-brain default).
        got = disp.reconcile([], [{"id": 1, "label": "runq_whatever"}])
        assert [d["type"] for d in got] == ["foreign_instance"]

    def test_stuck_provisioning_reaped_past_timeout(self):
        # Invariant 3d / bug 10: a `provisioning` row still present on Vast (not `lost`) but
        # older than provision_timeout_min is a daemon-restart zombie -- reaped regardless of
        # whether any task ever references it. A row younger than the timeout (still a
        # legitimate in-progress provision) and a `live` row are both left alone.
        fx = _fx("reconcile_stuck_provisioning.json")
        got = disp.reconcile(fx["db_instances"], fx["vast_instances"],
                             frozenset(fx["db_task_ids"]), now=fx["now"],
                             provision_timeout_min=fx["provision_timeout_min"])
        slim = [{"type": d["type"], "instance_id": d["instance_id"]} for d in got]
        assert slim == fx["expect"]

    def test_stuck_provisioning_check_is_opt_in(self):
        # Callers/fixtures that don't pass provision_timeout_min (e.g. every pre-existing test)
        # must see unchanged behavior -- an old provisioning row is never flagged by default.
        fx = _fx("reconcile_stuck_provisioning.json")
        got = disp.reconcile(fx["db_instances"], fx["vast_instances"], frozenset(fx["db_task_ids"]))
        assert [d["type"] for d in got] == []

    def test_owned_box_never_flagged_lost(self):
        # Owned-box spec: a self-owned box (source='owned') is never a Vast rental, so it will
        # never appear in `vast_instances` -- that absence alone must never be read as "lost"
        # (which would force-fail its occupant tasks and strand the box), unlike a real rental.
        fx = _fx("reconcile_owned.json")
        got = disp.reconcile(fx["db_instances"], fx["vast_instances"],
                             frozenset(fx["db_task_ids"]))
        slim = [{"type": d["type"], "instance_id": d["instance_id"]} for d in got]
        assert slim == fx["expect"]


class TestSharedRootResolution:
    """Retrospective bug 7: per-worktree registries were the split-brain vector. The shared
    root must resolve OUT of any `worktrees/*` invocation to the main checkout."""

    def test_resolves_to_main_checkout_experiments(self):
        rdb = _load("registry_sharedroot_check", "fleet/registry_db.py")
        root = rdb.shared_experiments_root()
        assert root.name == "experiments"
        # The load-bearing assertion when this suite runs from a worktree (exactly the
        # split-brain sessions' situation): never a worktree-local experiments dir.
        assert "worktrees" not in root.parts

    def test_dispatcher_and_runq_defaults_agree(self):
        runq = _load("runq_sharedroot_check", "fleet/runq.py")
        assert disp.DEFAULT_DB == runq.DEFAULT_DB
        assert str(disp.LOCK_PATH).startswith(str(Path(disp.DEFAULT_DB).parent))


class TestSshFallback:
    def test_never_switches_back(self):
        fx = _fx("ssh_fallback.json")
        settings = {"ssh_fallback_fails": fx["ssh_fallback_fails"]}
        tracker = disp.ConnectionTracker(settings)
        for step in fx["sequence"]:
            switched = tracker.record(fx["instance_id"], step["ok"])
            assert switched == step["expect_switch"]
            assert tracker.is_direct(fx["instance_id"]) == step["expect_direct_after"]


class TestTransportTimeoutIsFailureNotCrash:
    """Paid-campaign retrospective bug 6 (spec Fixtures): a hung proxy raises TimeoutExpired
    from subprocess.run — every transport helper must convert that into an ordinary failure
    (so invariant 9a's counter sees it) instead of letting it kill the poll loop."""

    @staticmethod
    def _hanging_run(cmd, capture_output=None, text=None, timeout=None):
        import subprocess
        raise subprocess.TimeoutExpired(cmd, timeout)

    def test_rsync_push_returns_false(self):
        assert disp.rsync_push("h", 22, ["f"], "r:", run=self._hanging_run) is False

    def test_rsync_pull_returns_false(self):
        assert disp.rsync_pull("h", 22, "~/x", "/tmp/y", ["*"], run=self._hanging_run) is False

    def test_ssh_run_returns_failed_process(self):
        out = disp.ssh_run("h", 22, "true", run=self._hanging_run)
        assert out.returncode != 0 and "timeout" in out.stderr

    def test_vastai_json_returns_none(self):
        assert disp.vastai_json("show", "instances", run=self._hanging_run) is None

    def test_timeouts_accumulate_to_fallback_switch(self):
        # Three consecutive hangs on pushes must flip the instance to the direct endpoint,
        # exactly like three returncode failures would (invariant 9a).
        tracker = disp.ConnectionTracker({"ssh_fallback_fails": 3})
        switched = [
            tracker.record(7, disp.rsync_push("h", 22, ["f"], "r:", run=self._hanging_run))
            for _ in range(3)
        ]
        assert switched == [False, False, True]
        assert tracker.is_direct(7)

    def test_rsync_ssh_opt_carries_connect_timeout(self):
        assert "ConnectTimeout=20" in disp._rsync_ssh_opt(22)


class TestShipInstallsAptPackages:
    """Retrospective bug 8: pip can't express system deps (dm_control's EGL import needs
    libEGL.so.1, absent from stock Vast pytorch images) — the dispatcher apt-installs an
    entrypoint's declared apt_packages over ssh at ship time, dpkg-guarded."""

    def test_ship_issues_apt_install_for_declaring_entrypoint(self, tmp_path, monkeypatch):
        # synthetic row: the legacy apt-declaring trainer rows (train_dmc) were removed with the
        # 2026-07-19 tombstoning — the ship-time apt path itself is what this covers.
        monkeypatch.setitem(
            disp.entrypoints.ENTRYPOINTS, "apt_probe",
            disp.entrypoints.Entrypoint(argv=["python", "-m", "native.training.m36"],
                                        completion_artifact="results.json", live=True,
                                        apt_packages=["libegl1", "libgles2"]))
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="TD", entrypoint="apt_probe")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='TD'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        joined = run.joined()
        apt_calls = [c for c in joined if "apt-get install" in c]
        assert apt_calls and "libegl1" in apt_calls[0] and "libgles2" in apt_calls[0]
        assert "dpkg -s" in apt_calls[0]  # idempotence guard

    def test_ship_skips_apt_for_entrypoints_without_system_deps(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="TS", entrypoint="smoke")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='TS'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        assert not any("apt-get" in c for c in run.joined())


class TestIdempotentRestart:
    def test_replaying_pack_decision_twice_is_stable(self):
        fx = _fx("pack_tightest.json")
        p1 = _place(fx)
        p2 = _place(fx)
        assert p1.action == p2.action == "pack"
        assert p1.target == p2.target


class TestAccountBalance:
    """Regression: a credit-only account (billing_creditonly=1, no card on file) reports
    balance=0 even with real spendable funds sitting in `credit` — found live while running the
    paid smoke test, where this bug would have made the dispatcher refuse to rent at all."""

    def test_credit_only_account_reads_credit_not_just_balance(self):
        assert disp.account_balance({"balance": 0, "credit": 27.68}) == pytest.approx(27.68)

    def test_card_linked_balance_alone_still_works(self):
        assert disp.account_balance({"balance": 12.5, "credit": 0}) == pytest.approx(12.5)

    def test_missing_fields_default_to_zero(self):
        assert disp.account_balance({}) == 0.0


class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class _RecordingRun:
    """Records every subprocess-shaped call `dispatcher.py` makes (ssh/rsync/git) and always
    succeeds — used to test which commands `_ship`/`_destroy` issue without touching a real
    network or a real Vast account."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return _FakeProc(0, "")

    def joined(self):
        return [" ".join(c) for c in self.calls]


class _HeartbeatRun(_RecordingRun):
    """A `_RecordingRun` that MODELS the remote HEARTBEAT file, which plain `_RecordingRun` cannot.

    `rsync -t` preserves the SOURCE's mtime, so a HEARTBEAT pull is the one call whose success has an
    observable side effect on the local copy: from a live worker (touching every `HEARTBEAT_SECONDS`)
    it lands FRESH; from a dead one it lands exactly as stale as it already was. `_RecordingRun`
    returns rc=0 and touches nothing, which silently models "the pull succeeded and the remote file
    is ancient" — i.e. a DEAD box — so using it for a healthy-box test asserts the wrong thing.

    `alive` picks which remote is being simulated; `pull_ok` simulates an unreachable box.
    """

    def __init__(self, hb_path, alive: bool, pull_ok: bool = True):
        super().__init__()
        self.hb_path = hb_path
        self.alive = alive
        self.pull_ok = pull_ok
        self.heartbeat_pulls = 0

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd and cmd[0] == "rsync" and "HEARTBEAT" in cmd:
            self.heartbeat_pulls += 1
            if not self.pull_ok:
                return _FakeProc(1, "rsync: connection unexpectedly closed")
            if self.alive and self.hb_path.exists():
                now = time.time()
                os.utime(self.hb_path, (now, now))   # the worker touched it seconds ago
            return _FakeProc(0, "")
        return _FakeProc(0, "")


_store = _load("artifact_store", "fleet/artifact_store.py")
_BLOB = b"SHIP-READY-TEST-PAYLOAD"


@pytest.fixture(autouse=True)
def _isolate_experiments_root(tmp_path, monkeypatch):
    """Per-test EXPERIMENTS_ROOT. `_ship` now reads the queuer-built blob from it and the GC
    reapers walk it, so tests must never share the real `experiments/`."""
    root = tmp_path / "experiments"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", root)


def _seed_instance_and_task(conn, task_id="T", instance_id=1, entrypoint="smoke"):
    """Seeds a shippable task. Since 2026-07-31 that means a ship-ready BLOB too: the coordinator
    no longer builds, so `_ship` reads `code_blob` from the artifact store and verifies its digest
    (ship-artifact-build inv. 1/6). A task without one is failed, not shipped — which is the point,
    but it makes every ship test need one."""
    import importlib.util
    reg_spec = importlib.util.spec_from_file_location("registry_db", ROOT / "fleet/registry_db.py")
    reg = importlib.util.module_from_spec(reg_spec)
    sys.modules["registry_db"] = reg
    reg_spec.loader.exec_module(reg)
    now = reg.now_iso()
    conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
        "slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (instance_id, f"runq_{task_id}", now, "live", 0.05, "example.com", 2222, 4, now))
    reg.insert_task(
        conn, id=task_id, created_at=now, created_by="test", grp="g", name=task_id,
        entrypoint=entrypoint, args_json="[]", config_json="{}", config_hash=task_id,
        arm_hash=task_id, git_sha="deadbeef", slots=1, est_minutes=1, priority=50,
        max_retries=1, state="claimed", instance_id=instance_id,
        code_blob=f"blob_{task_id}", code_sha256=_store.digest(_BLOB), code_format="compiled")
    _store.put(disp.EXPERIMENTS_ROOT, f"blob_{task_id}", _BLOB)
    return reg


# A REAL `vastai show instances` record (captured live from instance 40000053, the RTX 3070 of the
# 2026-08-03 slot-clobber incident; trimmed to the fields the dispatcher reads). Its whole point is
# what it does NOT contain: **there is no `slots_total` key** — that is our own concept, computed
# from the OFFER at rent time. Any adopt fixture that invents one agrees with a broken
# implementation and carries no bits (which is exactly how the clobber survived a month).
# `cpu_ram` here is the HOST's RAM, not the rented slice's: 128675 MB against the offer's
# 64337.5 MB, i.e. `cpu_ram * gpu_frac`. Do not feed it to `offer_ram_gb`.
_VAST_INSTANCE_RECORD = {
    "id": 1, "label": "runq_RA1", "machine_id": 100007, "dph_total": 0.07407407407407407,
    "gpu_name": "RTX 3070", "gpu_ram": 8192, "cpu_cores": 56, "cpu_cores_effective": 28.0,
    "cpu_ram": 128675, "gpu_frac": 0.5, "num_gpus": 1, "reliability2": 0.9671006,
    "actual_status": "running", "cur_state": "running", "intended_status": "running",
    "ssh_host": "h", "ssh_port": 22,
}


def _fake_vast_instances(records):
    """A `vastai_run` that answers `show instances` with `records` and everything else with []."""
    def run(cmd, **kwargs):
        if "instances" in cmd:
            return _FakeProc(0, json.dumps(records))
        return _FakeProc(0, "[]")
    return run


class TestReapOrphanedTasks:
    """Invariant 19g (2026-07-15): a task in an on-box state whose instance is destroyed/lost/gone
    must be requeued, not stranded. Observed live: two tasks sat `claimed` on boxes destroyed 8h
    earlier — no reaper covered claimed-on-dead-box."""

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def test_claimed_on_destroyed_box_is_requeued(self, tmp_path):
        d = self._dispatcher(tmp_path)
        _seed_instance_and_task(d.conn, task_id="ORPH1", instance_id=7)  # seeds claimed on live box
        d.conn.execute("UPDATE instances SET state='destroyed' WHERE id=7")
        d.conn.commit()
        d._reap_orphaned_tasks()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ORPH1'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 0.5  # infra loss costs half a retry (same as any infra_fail)

    def test_shipped_on_vanished_instance_is_requeued(self, tmp_path):
        d = self._dispatcher(tmp_path)
        _seed_instance_and_task(d.conn, task_id="ORPH2", instance_id=8)
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='ORPH2'")
        d.conn.execute("DELETE FROM instances WHERE id=8")  # instance row gone entirely
        d.conn.commit()
        d._reap_orphaned_tasks()
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='ORPH2'").fetchone())["state"] == "queued"

    def test_claimed_on_provisioning_box_is_left_alone(self, tmp_path):
        # a task packed onto a box still coming up is normal, NOT orphaned
        d = self._dispatcher(tmp_path)
        _seed_instance_and_task(d.conn, task_id="ORPH3", instance_id=9)
        d.conn.execute("UPDATE instances SET state='provisioning' WHERE id=9")
        d.conn.commit()
        d._reap_orphaned_tasks()
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='ORPH3'").fetchone())["state"] == "claimed"

    def test_running_on_live_box_is_left_alone(self, tmp_path):
        d = self._dispatcher(tmp_path)
        _seed_instance_and_task(d.conn, task_id="ORPH4", instance_id=10)
        d.conn.execute("UPDATE tasks SET state='running' WHERE id='ORPH4'")  # instance stays live
        d.conn.commit()
        d._reap_orphaned_tasks()
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='ORPH4'").fetchone())["state"] == "running"


class _EchoRun:
    """Fake ssh/rsync run whose every call returns a fixed returncode — drives the owned-box
    quarantine/recovery ssh probe (`echo ok`): ok=False = box still dead, ok=True = box answers."""

    def __init__(self, ok):
        self.ok = ok
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return _FakeProc(0 if self.ok else 1, "ok" if self.ok else "")


class _WorkerProbeRun:
    """ssh/rsync double for invariant 20h's recovery bring-up: the reachability + worker-liveness
    probe (`mkdir … && pgrep …`) always connects and reports whether a worker is already running
    (`worker_up`); every other call succeeds. Records commands so a test can assert whether a FRESH
    `spool_worker.py` launch was issued (reboot → yes; sleep with a surviving worker → no)."""

    def __init__(self, worker_up):
        self.worker_up = worker_up
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        remote = cmd[-1] if cmd else ""
        if "pgrep" in remote:  # the bring-up probe: reachable; report worker liveness
            return _FakeProc(0, "WORKER_UP" if self.worker_up else "WORKER_DOWN")
        return _FakeProc(0, "")

    def launched_worker(self):
        return any("nohup" in (c[-1] if c else "") and "spool_worker.py --spool" in (c[-1] if c else "")
                   for c in self.calls)


class TestReapUnreachableOwned:
    """Invariant 20h (2026-07-16): a fully-unreachable owned box (ssh/rsync failing outright) must be
    soft-quarantined (`live -> unreachable`) so it stops wedging the fleet, its stranded tasks
    requeued, and it must auto-recover (`unreachable -> live`) the moment it answers ssh again.
    Observed live: `laptop-gpu` (machine_id NULL, can't be denied; still `live`, so no reaper freed
    its claimed tasks) looped forever until a human cleared it."""

    def _owned_box(self, conn, reg, inst_id=-1, state="live", slots_total=6):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, None, "laptop-gpu", reg.now_iso(), state, 0.0, "192.168.0.9", 22,
             slots_total, reg.now_iso(), "owned"))
        conn.commit()

    def _task(self, conn, reg, tid, inst_id, state):
        reg.insert_task(
            conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid, arm_hash=tid,
            git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=3, state=state,
            instance_id=inst_id)
        conn.commit()

    def test_dead_owned_box_quarantined_and_tasks_requeued(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_EchoRun(False), vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1)
        self._task(d.conn, reg, "OB1", -1, "claimed")   # never-shipped, no heartbeat
        self._task(d.conn, reg, "OB2", -1, "running")   # was running when the box died
        for _ in range(3):
            d.tracker.record(-1, False)  # 3 consecutive ssh/rsync fails == owned_unreachable_fails
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "unreachable"
        for tid in ("OB1", "OB2"):
            row = dict(d.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            assert row["state"] == "queued", tid
            assert row["instance_id"] is None
            assert row["retries_used"] == 0.5  # infra loss = half a retry, same as any infra_fail

    def test_below_threshold_left_alone(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_EchoRun(False), vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1)
        self._task(d.conn, reg, "OB3", -1, "claimed")
        for _ in range(2):
            d.tracker.record(-1, False)  # one short of the threshold — a transient blip, not death
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='OB3'").fetchone())["state"] == "claimed"

    def test_quarantined_box_recovers_on_ssh_success(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_EchoRun(True), vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1, state="unreachable")
        for _ in range(5):
            d.tracker.record(-1, False)  # was quarantined with a high fail count
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"
        assert d.tracker.consecutive_fails(-1) == 0  # probe success reset the counter

    def test_non_owned_box_is_never_quarantined(self, tmp_path):
        # A Vast rental that goes unreachable is destroyed + re-rented by reconcile/dead-worker paths;
        # this reaper is owned-only and must not touch it (no `unreachable` state for rentals).
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_EchoRun(False), vastai_run=_RecordingRun())
        _seed_instance_and_task(d.conn, task_id="V1", instance_id=5)  # source defaults to 'vast', live
        for _ in range(9):
            d.tracker.record(5, False)
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=5").fetchone())["state"] == "live"
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='V1'").fetchone())["state"] == "claimed"

    def test_recovery_redeploys_worker_when_box_rebooted(self, tmp_path):
        # Reboot/power-cycle is the common way a laptop returns from `unreachable`: sshd answers but
        # the box-resident spool_worker.py is gone. Recovery must (re)start the worker, not just flip
        # state to `live` — else the box is a black hole (ships land, nothing claims, _destroy refused).
        run = _WorkerProbeRun(worker_up=False)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1, state="unreachable")
        for _ in range(5):
            d.tracker.record(-1, False)
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"
        assert d.tracker.consecutive_fails(-1) == 0            # bring-up success reset the counter
        assert run.launched_worker(), "recovery must (re)start spool_worker.py, not just flip state"

    def test_the_worker_is_started_with_NO_lane_count(self, tmp_path):
        """Invariant 8a, on the real command. `--max-slots {slots_total}` used to ride on this line,
        which froze the registry's number into the worker for as long as it ran — a box raised
        later kept refusing the extra work, and only a command run ON the box could change that.
        The box here is registered at 6 slots; nothing derived from that may reach its worker."""
        run = _WorkerProbeRun(worker_up=False)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1, state="unreachable", slots_total=6)
        d._reap_unreachable_owned()
        (start,) = [c[-1] for c in run.calls
                    if c and "nohup" in c[-1] and "spool_worker.py --spool" in c[-1]]
        assert "--max-slots" not in start and " 6" not in start, start
        assert "spool_worker.py --spool ~/spool >" in start, start

    def test_recovery_does_not_double_start_a_live_worker(self, tmp_path):
        # If the laptop merely slept and its worker survived, recovery re-admits it WITHOUT launching
        # a second spool_worker.py — two workers on one spool race on claim + both re-attach to the
        # same active/<id> dirs (spool_worker.py takes no singleton lock). Guarded by the pgrep probe.
        run = _WorkerProbeRun(worker_up=True)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._owned_box(d.conn, reg, inst_id=-1, state="unreachable")
        for _ in range(5):
            d.tracker.record(-1, False)
        d._reap_unreachable_owned()
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] == "live"
        assert d.tracker.consecutive_fails(-1) == 0
        assert not run.launched_worker(), "a still-running worker must not be duplicated"


class TestReapOverpackedBoxes:
    """Invariant 19h (owner directive 2026-07-15): `slots_for_offer` can advertise more slots than a
    GPU sustains; the box-side launch gate then wedges the excess task in `shipped` on a live, healthy
    box that no other reaper covers. Detect (live box, worker launching, shipped past grace) → learn
    the box's true concurrency (cap future placement) + requeue the gate-held task at no retry cost.
    Observed live: native_m32/c4_hebkv_ll_s0 sat shipped ~50min on an 8-slot box running 7 trainers."""

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _box(self, conn, reg, inst_id=1, machine_id=555, slots_total=8):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (inst_id, machine_id, f"runq_{inst_id}", reg.now_iso(), "live", 0.05, "h", 22,
             slots_total, reg.now_iso()))
        conn.commit()

    def _task(self, conn, reg, tid, inst_id, state, age_min=0.0, retries=0.0):
        import datetime
        reg.insert_task(
            conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid, arm_hash=tid,
            git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=4, state=state,
            instance_id=inst_id, code_blob=f"blob_{tid}", code_sha256=_store.digest(_BLOB),
            code_format="compiled")
        _store.put(disp.EXPERIMENTS_ROOT, f"blob_{tid}", _BLOB)   # the coordinator no longer builds
        old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=age_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE tasks SET updated_at=?, retries_used=? WHERE id=?", (old, retries, tid))
        conn.commit()

    def test_over_packed_box_learns_cap_and_requeues_at_no_retry_cost(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)  # just to load reg
        self._box(d.conn, reg, inst_id=1, machine_id=555, slots_total=8)
        # The running lanes must ALSO be old (2026-08-02): a lane that started within the grace is
        # proof the gate is still launching, which is the "still filling" case the reaper must NOT
        # judge. Seeding them at age 0 described a box that had just launched two lanes — the very
        # shape that produced the false positives — so it no longer exercises a genuine over-pack.
        grace = d.settings["ship_launch_grace_min"]
        self._task(d.conn, reg, "R1", 1, "running", age_min=grace + 20)
        self._task(d.conn, reg, "R2", 1, "running", age_min=grace + 20)
        # Age it past the CONFIGURED grace rather than a literal: the grace moved 20 -> 35 on
        # 2026-08-01 (it must exceed max_slots_cap x settle_minutes, see TestGraceExceedsFillTime)
        # and a hardcoded 30 silently stopped exercising this reaper at all.
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=grace + 10, retries=1.5)
        d._reap_overpacked_boxes()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='S1'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 1.5  # over-pack is the scheduler's fault, not the task's
        assert d._overpack_cap({"id": 1, "machine_id": 555}) == 2  # learned = running count
        # the box's spool copy was cleared so the worker can't double-launch it
        assert any("rm -rf" in " ".join(c) and "S1" in " ".join(c) for c in d.run.calls)

    def test_a_box_that_launched_a_lane_recently_is_STILL_FILLING_not_over_packed(self, tmp_path):
        """The guard for a machine with NO history, where the observed-peak floor cannot help — its
        peak is low precisely BECAUSE it is filling for the first time.

        `sweep_supervisor.should_launch` has four TRANSIENT refusal reasons (`settling`, `cpu_load`,
        `gpu_util`, `vram`) on top of the steady-state `max_slots` ceiling, and a box at 7 of 8 lanes
        is by construction the most loaded it will ever be — so the last lane is the one most likely
        to be refused for `cpu_load`. Reading that as a capacity ceiling, and then making it
        permanent, is the whole defect. A lane that started inside the grace window is direct proof
        the gate CAN still launch, so the box is not wedged."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555, slots_total=8)
        grace = d.settings["ship_launch_grace_min"]
        self._task(d.conn, reg, "R1", 1, "running", age_min=grace + 20)   # an old lane
        self._task(d.conn, reg, "R2", 1, "running", age_min=1.0)          # ...and a FRESH one
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=grace + 10)
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "shipped"
        assert d._overpack_cap({"id": 1, "machine_id": 555}) is None      # nothing learned

    def test_young_shipped_is_left_alone(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555)
        self._task(d.conn, reg, "R1", 1, "running")
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=5)  # under the 20min grace
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "shipped"
        assert d._overpack_cap({"id": 1, "machine_id": 555}) is None

    def test_no_running_occupant_is_left_to_other_reapers(self, tmp_path):
        # A shipped task with NO running sibling isn't proof of over-pack — the worker may just be
        # booting; the dead-worker/stall reapers own that case.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=30)
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "shipped"

    def test_unreachable_box_is_skipped(self, tmp_path):
        # consecutive_fails>0 means we can't currently prove the box's state — don't act on it.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555)
        self._task(d.conn, reg, "R1", 1, "running")
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=30)
        for _ in range(3):
            d.tracker.record(1, False)  # drive consecutive_fails > 0
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "shipped"

    def test_never_started_box_is_left_to_the_claim_timeout_reaper(self, tmp_path):
        # Complement of invariant 19h: no running occupant AND the box never started anything is
        # invariant 10b's case (`_reap_unclaimed_ships`), not this reaper's. Pinned so a future
        # widening of 19h can't silently swallow it and re-open the 8-hour idle-billing hole.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=30)
        d._reap_overpacked_boxes()
        assert d._overpack_cap({"id": 1, "machine_id": 555}) is None  # nothing learned

    def test_learned_cap_only_lowers_and_caps_instances_view(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1, machine_id=555, slots_total=8)
        d._learn_overpack_cap({"id": 1, "machine_id": 555}, 3)
        d._learn_overpack_cap({"id": 1, "machine_id": 555}, 5)  # higher — must NOT raise it back
        assert d._overpack_cap({"id": 1, "machine_id": 555}) == 3
        view = {v["id"]: v for v in d._instances_view()}
        assert view[1]["slots_total"] == 3  # min(8, learned 3)


class TestOverpackRatchetCannotContradictEvidence:
    """Invariant 19h-2 (2026-08-02): the ratchet may never learn a cap BELOW what the machine has
    been observed sustaining, and a stored cap that already does is repaired on read.

    THE MEASUREMENT THIS COMES FROM. Over the 18 most recent `overpack_cap` learns in the live
    registry, **15 were false positives** — the machine was later observed running MORE concurrent
    tasks than the cap learned from it. Machine m10003 was ratcheted 7 -> 6 -> 4 -> 1 while instance
    40000043 on it ran 8 at once; m100002 and m100005 also sat pinned at 1 against observed peaks of
    6 and 7. Because the cap is keyed per `machine_id` and `_learn_overpack_cap` only ever took
    min(), each false positive was PERMANENT and poisoned every future rental of that host.

    Raising `ship_launch_grace_min` does not fix this and has now failed twice (20 -> 35 on
    2026-08-01; the reaper kept firing, and the 15/18 above are all from AFTER that change). The
    grace is a threshold on a predicate that conflates two different things — see
    `_reap_overpacked_boxes` — so this class pins the evidence rule instead of the number."""

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _observe_peak(self, d, inst_id, n):
        """Drive the event stream so the box is seen running `n` tasks at once. Each `start` carries
        its own `task_id`, as every real one does — the peak is computed from per-task intervals, so
        n starts of the SAME task is one lane, not n."""
        for i in range(n):
            d.log("start", f"lane {i}", task_id=f"{inst_id}_t{i}", instance_id=inst_id)

    def test_cap_is_never_learned_below_the_observed_peak(self, tmp_path):
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 8)                      # this machine HAS run 8 at once
        d._learn_overpack_cap({"id": 1, "machine_id": 555}, 4)
        assert d._overpack_cap({"id": 1, "machine_id": 555}) is None   # refused, not clamped
        assert any(e["event"] == "overpack_cap_refused" for e in
                   [dict(r) for r in d.conn.execute("SELECT * FROM events")])

    def test_a_cap_at_or_above_the_peak_is_still_learned(self, tmp_path):
        """The reaper is not disabled — a genuinely over-packed box still gets capped."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 4)
        d._learn_overpack_cap({"id": 1, "machine_id": 555}, 4)
        assert d._overpack_cap({"id": 1, "machine_id": 555}) == 4

    def test_a_stored_cap_below_the_observed_peak_is_repaired_on_read(self, tmp_path):
        """The self-heal for caps a pre-fix daemon already wrote. Without it the pin perpetuates
        itself: a machine held at 1 slot can never again be OBSERVED sustaining more, so it can
        never earn its cap back — which is why three machines needed a MANUAL reset on 2026-08-02."""
        d = self._dispatcher(tmp_path)
        d.conn.execute("INSERT INTO settings(key, value) VALUES ('overpack_cap_m555', '1')")
        d.conn.commit()
        self._observe_peak(d, 1, 6)                      # but it has demonstrably run 6
        assert d._overpack_cap({"id": 1, "machine_id": 555}) == 6
        row = d.conn.execute("SELECT value FROM settings WHERE key='overpack_cap_m555'").fetchone()
        assert json.loads(row["value"]) == 6             # persisted, not just returned
        assert any(e["event"] == "overpack_cap_repair" for e in
                   [dict(r) for r in d.conn.execute("SELECT * FROM events")])

    def test_the_peak_is_NOT_memoised_for_the_process_lifetime(self, tmp_path, monkeypatch):
        """The peak GROWS as a box fills, and the coordinator runs for DAYS.

        A process-lifetime memo freezes a newly-rented box's floor at whatever it was on the first
        poll — often 1-2 lanes, because the box was still filling — and that stale-LOW floor then
        accepts exactly the caps the floor exists to refuse. Bounded to one `poll_seconds` window,
        the same idiom `learn_group_estimates` and the lane-footprint cache use. Cost of getting it
        right is nil: 11.2 ms cold for the whole live fleet, ~2 us warm."""
        d = self._dispatcher(tmp_path)
        inst = {"id": 1, "machine_id": 555}
        self._observe_peak(d, 1, 2)                       # box has run 2 lanes so far
        assert d._observed_peak_concurrency(inst) == 2
        for i in range(6):                                # ...it fills to 8 while the daemon runs
            d.log("start", "l", task_id=f"later{i}", instance_id=1)
        t = [time.time()]
        monkeypatch.setattr(time, "time", lambda: t[0])
        t[0] += d.settings["poll_seconds"] + 1            # one poll window later
        assert d._observed_peak_concurrency(inst) == 8, "the floor was frozen at the first poll"
        d._learn_overpack_cap(inst, 3)                    # and the stale floor no longer lets 3 in
        assert d._overpack_cap(inst) is None

    def test_a_lane_that_ends_on_an_UNENUMERATED_event_does_not_inflate_the_peak(self, tmp_path):
        """The peak is per-task INTERVALS, not a +1/-1 counter over a hand-listed exit vocabulary.

        The counter form has to name every event that ends a run, and the first draft missed
        `stalled` — 128 live cases — so the count only ever ratcheted up. Two machines resolved to a
        peak of 10 against an on-box `max_slots` of 8. An over-estimated floor silently DISABLES the
        reaper, which is the opposite failure to the one this class exists to fix, so it matters
        that the peak is not merely conservative but correct."""
        d = self._dispatcher(tmp_path)
        for i in range(3):                                # three lanes, opened then closed...
            d.log("start", "l", task_id=f"t{i}", instance_id=1)
            d.log("stalled", "l", task_id=f"t{i}", instance_id=1)   # ...via a NON-terminal event
        d.log("start", "l", task_id="t9", instance_id=1)  # then one lane alone
        assert d._observed_peak_concurrency({"id": 1, "machine_id": 555}) == 1

    def test_concurrent_rentals_of_ONE_host_do_not_sum_into_the_peak(self, tmp_path):
        """The cap is consumed PER INSTANCE (`_instances_view` caps that row's `slots_total`), so
        the floor is 'the most ONE BOX ran', not 'the most this host ran across rentals'. Live:
        machine m100002 had two overlapping rentals peaking at 6 lanes each, which a union sweep
        read as 10 — a floor no single box could ever justify, on a machine pinned at 1."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        for iid in (1, 2):
            d.conn.execute(
                "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, "
                "ssh_host, ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iid, 555, f"r{iid}", reg.now_iso(), "live", 0.05, "h", 22, 8, reg.now_iso()))
        d.conn.commit()
        for i in range(3):                                # 3 lanes on box 1...
            d.log("start", "l", task_id=f"a{i}", instance_id=1)
        for i in range(3):                                # ...and 3 CONCURRENT on box 2
            d.log("start", "l", task_id=f"b{i}", instance_id=2)
        assert d._observed_peak_concurrency({"id": 1, "machine_id": 555}) == 3   # not 6

    def test_the_peak_spans_every_rental_of_the_same_machine(self, tmp_path):
        """The cap is keyed per `machine_id` and survives teardown, so its evidence floor must be
        keyed the same way — else a re-rent starts with an empty peak and is instantly re-pinnable."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        for iid in (1, 2):
            d.conn.execute(
                "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, "
                "ssh_host, ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iid, 555, f"r{iid}", reg.now_iso(), "live", 0.05, "h", 22, 8, reg.now_iso()))
        d.conn.commit()
        self._observe_peak(d, 1, 7)                      # the peak was set on the OLD rental
        d._learn_overpack_cap({"id": 2, "machine_id": 555}, 2)   # the new one must inherit it
        assert d._overpack_cap({"id": 2, "machine_id": 555}) is None


class TestRefusedLearnCannotLoopForever:
    """Invariant 19h-3 — a refused learn must not leave the remedy firing forever.

    19h-2's floor refuses the cap; `_reap_overpacked_boxes` unschedules the gate-held task ANYWAY.
    Diagnosis denied, remedy applied, nothing changes — so the same box burns `ship_launch_grace_min`
    on the next cell, forever. Measured on the live registry in the 11 h after 19h-2 landed: **12
    unschedules against 12 refusals, a 1:1 pairing**, across 8 campaigns, one cell burned twice."""

    INST = {"id": 1, "machine_id": 555}

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _observe_peak(self, d, inst_id, n):
        for i in range(n):
            d.log("start", f"lane {i}", task_id=f"{inst_id}_t{i}", instance_id=inst_id)

    def _events(self, d, name):
        return [dict(r) for r in d.conn.execute("SELECT * FROM events WHERE event=?", (name,))]

    def test_the_first_refusal_still_protects_the_cap(self, tmp_path):
        """19h-2's whole point — a single gate-held task is the 83%-false-positive read."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 8)
        d._learn_overpack_cap(self.INST, 4)
        assert d._overpack_cap(self.INST) is None
        assert len(self._events(d, "overpack_cap_refused")) == 1
        assert d._overpack_refusals("overpack_cap_m555") == 1

    def test_the_second_refusal_COOLS_DOWN_the_box_and_leaves_the_cap_alone(self, tmp_path):
        """The floor is RIGHT that the machine can do more, so the cap must not move — and could
        not anyway: repair-on-read restores it to the peak on the next read (this test failed
        exactly that way when the first attempt tried to override the cap). What is wrong is the
        RENTAL, transiently. So stop feeding it for one grace window."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 8)
        d._learn_overpack_cap(self.INST, 4)          # strike 1
        assert not d._overpack_cooldown_active(self.INST)
        d._learn_overpack_cap(self.INST, 4)          # strike 2 -> cooldown
        assert d._overpack_cooldown_active(self.INST)
        assert d._overpack_cap(self.INST) is None    # cap untouched, peak still stands
        assert len(self._events(d, "overpack_cooldown")) == 1
        assert d._overpack_refusals("overpack_cap_m555") == 0

    def test_a_cooled_down_box_takes_NO_NEW_WORK(self, tmp_path):
        """The whole point — without this the requeued cell lands straight back on the same box."""
        inst = {"id": 1, "slots_total": 8, "occupants": [], "minutes_to_hard_cap": 600,
                "overpack_cooldown": True, "resource_cap": None}
        task = {"slots": 1, "est_minutes": 10, "resource_hint": None}
        s = {"est_safety": 1.25, "pull_margin_min": 10, "cores_per_lane": 1,
             "vram_per_lane_gb": 0.6, "ram_per_lane_gb": 2.0, "headroom_enabled": False}
        assert not disp._fits_now(task, inst, s)
        inst["overpack_cooldown"] = False
        assert disp._fits_now(task, inst, s)

    def test_a_launch_inside_the_grace_CLEARS_strikes_and_cooldown(self, tmp_path):
        """Reset on POSITIVE evidence, not elapsed time: a recovered box must not sit out the rest
        of the window."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 8)
        d._learn_overpack_cap(self.INST, 4)
        d._learn_overpack_cap(self.INST, 4)
        assert d._overpack_cooldown_active(self.INST)
        d._set_overpack_refusals("overpack_cap_m555", 0)   # what the reaper's launch branch does
        d._clear_overpack_cooldown(self.INST)
        assert not d._overpack_cooldown_active(self.INST)

    def test_the_cooldown_SELF_EXPIRES(self, tmp_path):
        """It can never wedge a box permanently — the failure mode of every other one-way mechanism
        on this path."""
        import datetime as _dt
        d = self._dispatcher(tmp_path)
        old = (_dt.datetime.utcnow() - _dt.timedelta(minutes=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute("INSERT INTO settings(key,value) VALUES (?,?)",
                       ("overpack_cooldown_i1", json.dumps({"at": old})))
        d.conn.commit()
        assert not d._overpack_cooldown_active(self.INST)   # 36 > ship_launch_grace_min 35

    def test_the_counter_is_PERSISTED_across_a_restart(self, tmp_path):
        """It must span 70+ min of grace windows, and the coordinator restarted 3x on 2026-08-02.
        An in-memory counter would reset on nearly every restart and never reach the limit — the
        same defect class as 28b."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 8)
        d._learn_overpack_cap(self.INST, 4)                      # strike 1
        d2 = self._dispatcher(tmp_path)                          # <- restart
        assert d2._overpack_refusals("overpack_cap_m555") == 1
        d2._learn_overpack_cap(self.INST, 4)                     # strike 2 survives the restart
        assert d2._overpack_cooldown_active(self.INST)

    def test_a_cap_at_or_above_the_peak_never_counts_a_strike(self, tmp_path):
        """Strikes are for CONTRADICTED learns only — a normal learn must clear the counter."""
        d = self._dispatcher(tmp_path)
        self._observe_peak(d, 1, 4)
        d._set_overpack_refusals("overpack_cap_m555", 1)
        d._learn_overpack_cap(self.INST, 4)
        assert d._overpack_cap(self.INST) == 4
        assert d._overpack_refusals("overpack_cap_m555") == 0

    def test_the_threshold_is_configurable(self, tmp_path):
        d = self._dispatcher(tmp_path)
        d.settings["overpack_refusals_before_override"] = 4
        self._observe_peak(d, 1, 8)
        for _ in range(3):
            d._learn_overpack_cap(self.INST, 4)
        assert not d._overpack_cooldown_active(self.INST)         # no cooldown at 3 of 4
        d._learn_overpack_cap(self.INST, 4)
        assert d._overpack_cooldown_active(self.INST)

    def test_the_unschedule_carries_the_BOXS_OWN_reason(self, tmp_path, monkeypatch):
        """Invariant 19h-4. An `overpack` event saying only 'gate-held > 35min' forces the reader to
        guess between over-pack, a co-tenant on the shared GPU, a settling stagger and a wedged
        worker — which is how 2026-08-02 was spent. Attach the reason and it answers itself."""
        d = self._dispatcher(tmp_path)
        local = disp.EXPERIMENTS_ROOT / ".dispatcher" / "instance_777"
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        local = tmp_path / ".dispatcher" / "instance_777"
        local.mkdir(parents=True)
        (local / "worker.jsonl").write_text(
            json.dumps({"t": "2026-08-02T15:00:00Z", "event": "start", "task_id": "a"}) + "\n" +
            json.dumps({"t": "2026-08-02T15:05:00Z", "event": "launch_gate", "task_id": "b",
                        "detail": "vram: free 0.40GB < need 0.75GB"}) + "\n")
        got = d._last_launch_gate({"id": 777})
        assert "vram" in got and "0.40" in got and "15:05" in got

    def test_a_missing_or_torn_worker_log_is_not_fatal(self, tmp_path, monkeypatch):
        """Best-effort by contract — it runs on the unschedule path and must never block recovery."""
        d = self._dispatcher(tmp_path)
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        assert d._last_launch_gate({"id": 999}) is None          # no file at all
        local = tmp_path / ".dispatcher" / "instance_998"
        local.mkdir(parents=True)
        (local / "worker.jsonl").write_text("{not json\n{\"event\": \"start\"}\n")
        assert d._last_launch_gate({"id": 998}) is None          # torn line, no launch_gate

    def test_the_counter_row_is_cleared_not_left_at_zero(self, tmp_path):
        """`settings` must not accumulate a dead row per healed machine."""
        d = self._dispatcher(tmp_path)
        d._set_overpack_refusals("overpack_cap_m555", 2)
        d._set_overpack_refusals("overpack_cap_m555", 0)
        assert d.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key='overpack_refusals_overpack_cap_m555'"
        ).fetchone()[0] == 0



class TestReapUnclaimedShips:
    """Invariant 10b — `shipped` with no worker claim within `claim_timeout_min`.

    `claim_timeout_min` was defined in settings and written into the spec but NEVER consumed, so a
    box whose worker never starts was reaped by nothing: `_reap_stalled` ignores `shipped`,
    `_reap_overpacked_boxes` bails on `running == 0`, and `_reap_dead_workers` needs a HEARTBEAT the
    worker never wrote. Live cost 2026-07-28: a Tesla V100 shipped a task at 12:44 and sat at 0% GPU
    util for 7.5h until a human cancelled it — $1.07, 25% of that day's fleet spend, for zero work.
    """

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _box(self, conn, reg, inst_id=1, machine_id=555, source="vast"):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, machine_id, f"runq_{inst_id}", reg.now_iso(), "live", 0.05, "h", 22,
             4, reg.now_iso(), source))
        conn.commit()

    def _task(self, conn, reg, tid, inst_id, state, age_min=0.0, retries=0.0):
        import datetime
        reg.insert_task(
            conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid, arm_hash=tid,
            git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=4, state=state,
            instance_id=inst_id, code_blob=f"blob_{tid}", code_sha256=_store.digest(_BLOB),
            code_format="compiled")
        _store.put(disp.EXPERIMENTS_ROOT, f"blob_{tid}", _BLOB)   # the coordinator no longer builds
        old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=age_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE tasks SET updated_at=?, retries_used=? WHERE id=?", (old, retries, tid))
        conn.commit()

    def test_unclaimed_ship_is_requeued_and_the_rental_is_destroyed(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=45, retries=1.0)  # > the 30min timeout
        d._reap_unclaimed_ships()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='S1'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        assert row["retries_used"] == 1.5  # infra failure costs HALF a retry (invariant 10)
        # the box's copy is cleared BEFORE requeue so a late worker can't double-launch it
        assert any("rm -rf" in " ".join(c) and "S1" in " ".join(c) for c in d.run.calls)
        # and the meter is stopped rather than left idle-billing forever
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone())["state"] \
            == "destroyed"

    def test_young_shipped_is_left_alone(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=5)  # far under the 30min timeout
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "shipped"
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone())["state"] \
            == "live"

    def test_box_that_started_before_is_left_to_the_dead_worker_reaper(self, tmp_path):
        # A box with a `start` in its history HAS a HEARTBEAT, so invariant 10c owns it. Acting here
        # would destroy a box whose worker is merely between tasks.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        d.log("start", "worker reported start", instance_id=1)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=45)
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "shipped"
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone())["state"] \
            == "live"

    def test_box_with_a_running_occupant_is_left_to_the_overpack_reaper(self, tmp_path):
        # 19h requeues at NO retry cost and learns the box's real concurrency. Since
        # claim_timeout_min (15) < ship_launch_grace_min (20), an unscoped 10b would fire first and
        # rob it of both — this pins the scoping that prevents that.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._task(d.conn, reg, "R1", 1, "running")
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=45)
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "shipped"
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone())["state"] \
            == "live"

    def test_unreachable_box_is_skipped(self, tmp_path):
        # Invariant 10c: a box we can't reach is a different problem — we cannot prove the worker
        # failed to claim, so abstain rather than destroy a possibly-healthy box.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=45)
        for _ in range(3):
            d.tracker.record(1, False)
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "shipped"
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone())["state"] \
            == "live"

    def test_owned_box_requeues_the_task_but_is_never_destroyed(self, tmp_path):
        # `_destroy`'s owned-box carve-out: nothing re-adopts a destroyed owned row, so it must
        # only free the slot.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=-1, machine_id=None, source="owned")
        self._task(d.conn, reg, "S1", -1, "shipped", age_min=45)
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "queued"
        assert dict(d.conn.execute("SELECT state FROM instances WHERE id=-1").fetchone())["state"] \
            == "live"

    def test_claim_timeout_min_is_actually_consumed(self, tmp_path):
        # The regression that motivated this: the setting existed and was specified, but no code
        # path read it. Assert the reaper honours a raised value rather than a hard-coded constant.
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._task(d.conn, reg, "S1", 1, "shipped", age_min=45)
        d.settings["claim_timeout_min"] = 90  # 45min old is now UNDER the timeout
        d._reap_unclaimed_ships()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] \
            == "shipped"

class _ProbeRun:
    """Fake ssh/rsync run for _provision: fails the `mkdir` ssh probe its first `fail_n` calls,
    succeeds on every other call (bootstrap rsync, worker start)."""

    def __init__(self, fail_n):
        self.fail_n = fail_n
        self.probe_calls = 0
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "mkdir -p ~/spool/incoming ~/spool/active" in " ".join(cmd):
            self.probe_calls += 1
            return _FakeProc(1 if self.probe_calls <= self.fail_n else 0, "")
        return _FakeProc(0, "")


class _FakeVastai:
    """Fake `vastai` CLI: show user -> funded, show instances -> none, create -> a fresh id,
    show instance -> running. Records created ids so a test can count rents."""

    def __init__(self, actual_status="running"):
        self.next_id = 1000
        self.created = []
        self.actual_status = actual_status

    def __call__(self, cmd, **kwargs):
        args = cmd[1:] if cmd and cmd[0] == "vastai" else cmd
        head = args[:2]
        if head == ["show", "user"]:
            return _FakeProc(0, json.dumps({"balance": 100.0, "credit": 0.0}))
        if head == ["show", "instances"]:
            return _FakeProc(0, "[]")
        if head == ["create", "instance"]:
            self.next_id += 1
            self.created.append(self.next_id)
            return _FakeProc(0, json.dumps({"new_contract": self.next_id}))
        if head == ["show", "instance"]:
            return _FakeProc(0, json.dumps({"actual_status": self.actual_status, "ssh_host": "h",
                                            "ssh_port": 22, "machine_id": 1}))
        return _FakeProc(0, "{}")


class TestAsyncProvisioning:
    """Invariant 5b/5c: provisioning is incremental (never blocks the poll), and the over-provisioning
    guard makes queued tasks HOLD for a box already being provisioned instead of each renting one."""

    def _reg(self):
        return _load("registry_db", "fleet/registry_db.py")

    def _provisioning_box(self, d, reg, iid=1, slots_total=4):
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, slots_total, "
            "hard_cap_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, 1, f"runq_{iid}", reg.now_iso(), "provisioning", 0.05, slots_total,
             disp._iso_plus_hours(48)))
        d.conn.commit()

    def test_advance_running_box_goes_live(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        self._provisioning_box(d, self._reg())
        d._advance_provisioning()
        assert d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone()[0] == "live"

    def test_advance_not_running_stays_provisioning(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_FakeVastai(actual_status="loading"))
        self._provisioning_box(d, self._reg())
        d._advance_provisioning()
        assert d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone()[0] == "provisioning"

    def test_advance_probe_fail_stays_provisioning_not_destroyed(self, tmp_path):
        # box reached running but sshd isn't up -> stay provisioning (retry next poll); _advance does
        # NOT destroy (reconcile owns the boot-ceiling reap), and the machine is NOT blacklisted.
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_ProbeRun(fail_n=99),
                            vastai_run=_FakeVastai())
        self._provisioning_box(d, self._reg())
        d._advance_provisioning()
        assert d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone()[0] == "provisioning"

    def test_rent_is_non_blocking_and_returns_id(self, tmp_path):
        vastai = _FakeVastai()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=vastai)
        iid = d._rent({"id": 7, "dph_total": 0.05, "machine_id": 1, "gpu_ram_gb": 24,
                       "cpu_cores_effective": 8}, {"id": "T", "slots": 1})
        assert iid == vastai.created[-1]
        # the box is left `provisioning` — _rent no longer blocks to bring it live
        assert d.conn.execute("SELECT state FROM instances WHERE id=?", (iid,)).fetchone()[0] == "provisioning"

    def test_boot_ceiling_blacklists_box_that_reached_running(self, tmp_path, monkeypatch):
        # Invariant 5b: a box past the boot ceiling that reached running (ssh_host set) but never
        # became usable = broken sshd -> DENY the machine (async must not lose the inline dud-denial).
        deny = tmp_path / "machines.deny"
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", deny)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        reg = self._reg()
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, slots_total, "
            "hard_cap_at, ssh_host, ssh_port) VALUES (1,1,'runq_x',?,?,0.05,1,?,'h',22)",
            (reg.now_iso(), "provisioning", disp._iso_plus_hours(48)))
        d.conn.commit()
        d._destroy_stuck_provisioning(1)
        assert d.conn.execute("SELECT state FROM instances WHERE id=1").fetchone()[0] == "destroyed"
        assert deny.exists() and deny.read_text().strip()  # machine was denied

    def test_boot_ceiling_does_not_blacklist_box_that_never_ran(self, tmp_path, monkeypatch):
        # ssh_host NULL = never reached running (maybe transient slow image pull) -> destroy, NO deny.
        deny = tmp_path / "machines.deny"
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", deny)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        reg = self._reg()
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, slots_total, "
            "hard_cap_at) VALUES (2,2,'runq_y',?,?,0.05,1,?)",
            (reg.now_iso(), "provisioning", disp._iso_plus_hours(48)))
        d.conn.commit()
        d._destroy_stuck_provisioning(2)
        assert d.conn.execute("SELECT state FROM instances WHERE id=2").fetchone()[0] == "destroyed"
        assert not (deny.exists() and deny.read_text().strip())  # NOT denied

    def _stuck_rental(self, d, iid, machine_id, created_at, *, ssh_host=None, state="destroyed"):
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, slots_total, "
            "hard_cap_at, ssh_host, ssh_port) VALUES (?,?,'runq_z',?,?,0.05,1,?,?,22)",
            (iid, machine_id, created_at, state, disp._iso_plus_hours(48), ssh_host))
        d.conn.commit()

    def test_a_machine_that_NEVER_boots_is_denied_after_N_CONSECUTIVE_STRIKES(self, tmp_path,
                                                                              monkeypatch):
        """Invariant 5c. Tolerating ONE never-reached-running failure is correct (transient image
        pull); tolerating them FOREVER is the 2026-08-16 incident — machine 100004 was rented 7
        times in 3 hours, ssh_host NULL every time, zero successes, re-rented within ~20 s of each
        teardown because the offer was still the cheapest by value density. The fleet grew by zero
        boxes while ~10 cells sat queued."""
        deny = tmp_path / "machines.deny"
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", deny)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        self._stuck_rental(d, 10, 555, "2026-08-16T17:12:00Z")
        self._stuck_rental(d, 11, 555, "2026-08-16T18:26:00Z")
        self._stuck_rental(d, 12, 555, "2026-08-16T19:11:00Z", state="provisioning")
        d._destroy_stuck_provisioning(12)
        assert d.conn.execute("SELECT state FROM instances WHERE id=12").fetchone()[0] == "destroyed"
        assert deny.exists() and "555" in deny.read_text(), \
            "the third consecutive never-booted rental of one machine must DENY it"

    def test_a_SUCCESSFUL_boot_RESETS_the_strike_count(self, tmp_path, monkeypatch):
        """It is a STREAK, not a total — otherwise a machine that had a bad hour last week is banned
        for it today. The most recent rental reached running (ssh_host set), so the two failures
        before it are history and this one is strike 1."""
        deny = tmp_path / "machines.deny"
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", deny)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        self._stuck_rental(d, 20, 777, "2026-08-16T10:00:00Z")
        self._stuck_rental(d, 21, 777, "2026-08-16T11:00:00Z")
        self._stuck_rental(d, 22, 777, "2026-08-16T12:00:00Z", ssh_host="h")   # BOOTED — resets
        self._stuck_rental(d, 23, 777, "2026-08-16T13:00:00Z", state="provisioning")
        assert d._consecutive_stuck_provisions(777) == 1
        d._destroy_stuck_provisioning(23)
        assert d.conn.execute("SELECT state FROM instances WHERE id=23").fetchone()[0] == "destroyed"
        assert not (deny.exists() and deny.read_text().strip()), "a reset streak must NOT deny"

    def test_a_box_with_NO_machine_id_is_never_denied(self, tmp_path, monkeypatch):
        """An unidentifiable box cannot be blamed — there is nothing to write to the denylist, and
        guessing would ban whichever host happened to be next."""
        deny = tmp_path / "machines.deny"
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", deny)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_FakeVastai())
        assert d._consecutive_stuck_provisions(None) == 0
        self._stuck_rental(d, 30, None, "2026-08-16T14:00:00Z", state="provisioning")
        d._destroy_stuck_provisioning(30)
        assert d.conn.execute("SELECT state FROM instances WHERE id=30").fetchone()[0] == "destroyed"
        assert not (deny.exists() and deny.read_text().strip())

    def test_over_provisioning_guard_holds_for_provisioning_box(self, monkeypatch):
        monkeypatch.setattr(disp, "load_deny_machines", lambda: set())
        fx = {
            "task": {"id": "T", "slots": 1, "est_minutes": 60, "priority": 50},
            "instances": [{"id": 1, "state": "provisioning", "slots_total": 4,
                           "minutes_to_hard_cap": 3000, "occupants": []}],
            "queue": [],
            "offers": [{"dph_total": 0.05, "machine_id": 9, "gpu_ram_gb": 24,
                        "cpu_cores_effective": 16, "reliability": 0.99}],
        }
        p = _place(fx)
        assert p.action == "hold" and "awaiting_provisioning" in p.reason and p.target == 1

    def test_no_over_provisioning_across_a_poll(self, tmp_path, monkeypatch):
        # THE guard test: 5 queued tasks, no boxes, offers give 2 slots/box -> rent ceil(5/2)=3
        # boxes, NOT 5. Without the guard the async poll would rent one box per task.
        monkeypatch.setattr(disp, "load_deny_machines", lambda: set())
        reg = self._reg()
        vastai = _FakeVastai()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=vastai)
        offer = {"id": 7, "dph_total": 0.05, "machine_id": 1, "gpu_ram_gb": 24,
                 "cpu_cores_effective": 2, "reliability": 0.99}  # slots_for_offer -> min(40,2)=2
        d._offers = lambda: [offer]
        for i in range(5):
            reg.insert_task(d.conn, id=f"Q{i}", created_at=reg.now_iso(), created_by="t", grp="g",
                            name=f"q{i}", entrypoint="smoke", args_json="[]", config_json="{}",
                            config_hash=f"h{i}", arm_hash=f"a{i}", git_sha="d", slots=1,
                            est_minutes=60, priority=50, max_retries=3, state="queued")
        d.conn.commit()
        assert disp.slots_for_offer(offer, None, d.settings) == 2  # sanity: 2 slots/box
        d._place_queue()
        assert len(vastai.created) == 3, f"expected ceil(5/2)=3 boxes, rented {len(vastai.created)}"


class TestStartRowIsAnchoredOnClaimNotTheShippedStamp:
    """Invariant 7h (2026-08-04): a box-reported `start` counts if it postdates the moment THIS
    delivery attempt BEGAN (`claim`), not the moment the `shipped` CAS finished (`updated_at`).

    THE RACE, MEASURED LIVE. The box claims and launches within SECONDS of the payload landing,
    while the dispatcher stamps `shipped` afterwards and in a per-pass BATCH. So `start` lands
    BEFORE `updated_at` and the row is rejected — and since the row never gets any newer, it is
    rejected FOREVER, leaving the task `shipped` in the registry while it trains on the box.
    Found with 11 tasks stuck simultaneously; every one carrying a `start` row had
    `start < updated_at` (gaps 8s-110s, five sharing a single batched stamp 21:14:01).

    WHY IT IS SEVERE, not cosmetic: `_reap_overpacked_boxes` treats a `shipped` task past
    `ship_launch_grace_min` as over-packed, so it requeues an ACTIVELY TRAINING task and `rm -rf`s
    its `active/<id>`, which makes `reap_orphans` kill the trainer. 60 of the last 60 over-pack
    unschedules were on tasks the box had claimed AND started — one 52 minutes into its run."""

    def _d(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="T1", instance_id=1)
        d.conn.execute("UPDATE tasks SET state='shipped', updated_at=? WHERE id='T1'",
                        ("2026-08-04T20:43:34Z",))
        d.conn.commit()
        return d

    def _worker_log(self, tmp_path, rows, iid=1):
        p = tmp_path / ".dispatcher" / f"instance_{iid}"
        p.mkdir(parents=True, exist_ok=True)
        (p / "worker.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_a_start_BEFORE_the_shipped_stamp_but_after_claim_is_ACCEPTED(self, tmp_path,
                                                                          monkeypatch):
        """The live shape: claim 20:42:55 -> box start 20:43:11 -> shipped stamped 20:43:34."""
        d = self._d(tmp_path, monkeypatch)
        d.log("claim", "placed", task_id="T1", instance_id=1)
        d.conn.execute("UPDATE events SET t=? WHERE event='claim' AND task_id='T1'",
                        ("2026-08-04T20:42:55Z",))
        d.conn.commit()
        self._worker_log(tmp_path, [{"t": "2026-08-04T20:43:11Z", "task_id": "T1",
                                     "event": "start", "rc": None, "detail": ""}])
        d._apply_worker_state({"id": 1}, True, True)
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='T1'").fetchone())["state"] == "running"

    def test_a_start_from_a_PREVIOUS_attempt_is_still_REJECTED(self, tmp_path, monkeypatch):
        """The property the guard exists for. worker.jsonl is append-only across every ship attempt
        (a requeue reuses the task_id), so a row predating THIS attempt's `claim` must not count —
        otherwise a re-shipped task instantly reads as running off a stale row."""
        d = self._d(tmp_path, monkeypatch)
        d.log("claim", "placed", task_id="T1", instance_id=1)
        d.conn.execute("UPDATE events SET t=? WHERE event='claim' AND task_id='T1'",
                        ("2026-08-04T20:42:55Z",))
        d.conn.commit()
        self._worker_log(tmp_path, [{"t": "2026-08-04T18:00:00Z", "task_id": "T1",
                                     "event": "start", "rc": None, "detail": ""}])
        d._apply_worker_state({"id": 1}, True, True)
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='T1'").fetchone())["state"] == "shipped"

    def test_with_no_claim_event_it_falls_back_to_the_old_anchor(self, tmp_path, monkeypatch):
        """A pre-event-log row must keep working, and the fallback can only be as wrong as the old
        behaviour — `updated_at` is the LATER anchor, so it never accepts a row it should reject."""
        d = self._d(tmp_path, monkeypatch)
        self._worker_log(tmp_path, [{"t": "2026-08-04T20:43:11Z", "task_id": "T1",
                                     "event": "start", "rc": None, "detail": ""}])
        d._apply_worker_state({"id": 1}, True, True)          # start < updated_at, no claim event
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='T1'").fetchone())["state"] == "shipped"
        d.conn.execute("UPDATE tasks SET updated_at=? WHERE id='T1'",
                        ("2026-08-04T20:00:00Z",))            # now the old anchor admits it
        d.conn.commit()
        d._apply_worker_state({"id": 1}, True, True)
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='T1'").fetchone())["state"] == "running"


class TestOverpackNeverUnschedulesATaskTheBoxIsRunning:
    """Invariant 7h safety valve: `_reap_overpacked_boxes` is where a registry/box divergence
    becomes IRREVERSIBLE — it `rm -rf`s `active/<id>`, which is exactly what makes `reap_orphans`
    kill the trainer. So it must consult the box's own account before requeueing, rather than
    trusting a registry state that this very defect showed can be wrong for 52 minutes."""

    def _d(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, 555, "runq_1", reg.now_iso(), "live", 0.05, "h", 22, 8, reg.now_iso()))
        d.conn.commit()
        return d, reg

    def _task(self, d, reg, tid, state, age_min):
        import datetime
        reg.insert_task(d.conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
                        entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                        arm_hash=tid, git_sha="d", slots=1, est_minutes=1, priority=50,
                        max_retries=4, state=state, instance_id=1,
                        code_blob=f"blob_{tid}", code_sha256=_store.digest(_BLOB),
                        code_format="compiled")
        _store.put(disp.EXPERIMENTS_ROOT, f"blob_{tid}", _BLOB)
        old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=age_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old, tid))
        d.conn.commit()

    def _worker_log(self, tmp_path, rows):
        p = tmp_path / ".dispatcher" / "instance_1"
        p.mkdir(parents=True, exist_ok=True)
        (p / "worker.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_a_task_the_box_STARTED_is_left_alone(self, tmp_path, monkeypatch):
        d, reg = self._d(tmp_path, monkeypatch)
        grace = d.settings["ship_launch_grace_min"]
        self._task(d, reg, "R1", "running", grace + 20)
        self._task(d, reg, "S1", "shipped", grace + 10)
        import datetime
        recent = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self._worker_log(tmp_path, [{"t": recent, "task_id": "S1", "event": "start",
                                     "rc": None, "detail": ""}])
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "shipped"
        assert any(r[0] == "overpack_skipped_running"
                   for r in d.conn.execute("SELECT event FROM events"))

    def test_a_genuinely_gate_held_task_is_STILL_requeued(self, tmp_path, monkeypatch):
        """The valve must not disarm the reaper — a task the box never started is 19h's real case,
        and no other reaper covers a gate-held `shipped` task."""
        d, reg = self._d(tmp_path, monkeypatch)
        grace = d.settings["ship_launch_grace_min"]
        self._task(d, reg, "R1", "running", grace + 20)
        self._task(d, reg, "S1", "shipped", grace + 10)
        self._worker_log(tmp_path, [{"t": "2026-08-04T00:00:00Z", "task_id": "R1",
                                     "event": "start", "rc": None, "detail": ""}])
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "queued"

    def test_a_task_that_started_then_EXITED_is_not_treated_as_running(self, tmp_path, monkeypatch):
        """`exit` clears the started flag — otherwise a box that ran and finished a task under an
        earlier attempt would shield a genuinely wedged re-ship forever."""
        d, reg = self._d(tmp_path, monkeypatch)
        grace = d.settings["ship_launch_grace_min"]
        self._task(d, reg, "R1", "running", grace + 20)
        self._task(d, reg, "S1", "shipped", grace + 10)
        import datetime
        recent = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self._worker_log(tmp_path, [
            {"t": recent, "task_id": "S1", "event": "start", "rc": None, "detail": ""},
            {"t": recent, "task_id": "S1", "event": "exit", "rc": 0, "detail": ""}])
        d._reap_overpacked_boxes()
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='S1'").fetchone())["state"] == "queued"


class TestProvisionSshProbeRetry:
    """Invariant 5 (2026-07-15): Vast flips actual_status to 'running' before sshd accepts
    connections, so the post-running ssh probe is RETRIED before a fresh box is judged dead and its
    machine PERMANENTLY denied. A single-shot probe banned 98 good machines and stalled all fleet
    growth once the rent path was un-starved (invariant 4c)."""

    def _dispatcher(self, tmp_path, run):
        def vastai(cmd, **kwargs):  # every `vastai show/destroy instance` -> running box, machine 1
            return _FakeProc(0, json.dumps({"actual_status": "running", "ssh_host": "h",
                                            "ssh_port": 22, "machine_id": 1}))
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=vastai)
        d.settings["ssh_probe_interval_s"] = 0  # no real sleeps between retries
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, 1, "runq_x", "2026-07-15T00:00:00Z", "provisioning", 0.05, "h", 22, 1,
             "2026-07-16T00:00:00Z"))
        d.conn.commit()
        return d

    def test_probe_retries_then_succeeds_no_deny(self, tmp_path):
        run = _ProbeRun(fail_n=2)  # sshd not ready for the first 2 probes, then up
        d = self._dispatcher(tmp_path, run)
        deny = tmp_path / "machines.deny"
        assert d._provision(1, deny) is True
        assert run.probe_calls == 3  # 2 transient failures + 1 success (would be 1-and-done before)
        assert not deny.exists()  # a good box is NOT denied

    def test_probe_exhausts_attempts_then_denies(self, tmp_path):
        run = _ProbeRun(fail_n=99)  # genuinely dead — every probe fails
        d = self._dispatcher(tmp_path, run)
        deny = tmp_path / "machines.deny"
        assert d._provision(1, deny) is False
        assert run.probe_calls == d.settings["ssh_probe_attempts"]  # all attempts used before giving up
        assert "1" in deny.read_text()  # only then is the machine denied


class TestReshipClearsStaleBoxState:
    """Regression: found live during the paid smoke test — a requeued (preempted or
    infra-failed) task's OLD spool dir is still on the box (the worker never deletes task
    dirs), and the first-ship idempotency check would mistake it for "already delivered",
    silently skipping the re-ship of the new resume-enabled task.json entirely."""

    def test_first_ship_uses_the_existence_check_not_a_wipe(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="T1")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='T1'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        joined = run.joined()
        assert any("test -e" in c for c in joined)
        assert not any("rm -rf" in c for c in joined)

    def test_reship_wipes_the_stale_dir_first(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="T2")
        reg.log_event(d.conn, "ship", "shipped to instance 1 (first attempt)", task_id="T2")
        d.conn.commit()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='T2'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        joined = run.joined()
        assert any("rm -rf" in c and "T2" in c for c in joined)
        assert not any("test -e" in c for c in joined)

    def test_first_ship_existence_check_requires_ready_not_just_the_directory(self, tmp_path):
        """Regression: found live 2026-07-09 against a real stuck Vast box — a prior ship
        attempt died between pushing payload+task.json and pushing READY (invariant 7's last
        step), leaving `incoming/<id>` present but never actually claimable. The OLD existence
        check (`test -e incoming/<id>`) treated that partial state as "already delivered" and
        skipped straight to the `shipped` CAS, so the worker sat forever waiting for a READY
        that had never arrived — the box billed idle for over an hour before this was caught by
        hand over ssh. The fix checks for READY specifically (or `active/<id>`, proof the
        worker's claim-rename already happened) before treating a task as already delivered."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="T4")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='T4'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        existence_checks = [c for c in run.joined() if "test -e" in c]
        assert existence_checks, "expected an existence check to be issued"
        # A bare `incoming/<id>` (no /READY) would have wrongly matched the pre-fix bug's
        # scenario (dir present, READY missing) as "already delivered".
        assert all(f"incoming/{task['id']}/READY" in c for c in existence_checks)

        # And when the (fake, always-succeeding) check reports nothing exists, `_ship` must
        # actually attempt the full delivery rather than short-circuiting.
        assert any("rsync" in c for c in run.joined())


class TestWorkerStateIngestIgnoresStaleHistoricalEvents:
    """Regression: found live — worker.jsonl is append-only across a task's ENTIRE lifetime
    (every ship attempt reuses the same task_id), so a claim/start row from an EARLIER attempt
    must not be mistaken for the current attempt actually starting."""

    def test_stale_claim_before_current_ship_is_ignored(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="T3")
        # Move the task to `shipped` NOW (so its updated_at is "later" than the stale event below).
        reg.transition(d.conn, "T3", "shipped", "ship", "re-shipped after a prior attempt")
        local = disp.ROOT / "experiments" / ".dispatcher" / "instance_1"
        local.mkdir(parents=True, exist_ok=True)
        stale_line = json.dumps({"t": "2020-01-01T00:00:00Z", "task_id": "T3", "event": "start"})
        (local / "worker.jsonl").write_text(stale_line + "\n")

        def fake_pull(host, port, remote, dest, includes, append=False, run=None):
            return True  # pretend the (empty-of-anything-new) rsync pull succeeded

        orig = disp.rsync_pull
        disp.rsync_pull = fake_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_worker_state(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='T3'").fetchone())["state"] == "shipped"


class TestHeartbeatPulledWithoutAppend:
    """Regression, live incident 2026-07-14: `HEARTBEAT` is a 0-byte file that's only ever
    re-touched, never grown. `rsync --append` decides whether to re-transfer a file by
    comparing SIZE, not mtime — a same-size file looks "already fully appended" and gets
    silently skipped forever after the first successful pull, freezing the local mtime even
    while the remote keeps advancing. `_reap_dead_workers` reads exactly that frozen mtime, so
    this alone (no real connectivity problem at all) destroyed several healthy, actively-
    training boxes ~`heartbeat_stale_min` after their first pull. `HEARTBEAT` must be pulled as
    an ordinary (non-append) transfer every cycle.

    UPDATED 2026-07-27: `worker.jsonl` must ALSO be pulled without `--append`. It is append-only
    only while the worker keeps running — a `spool_worker.py` restart RECREATES it from empty, and
    `--append` skips any file whose source is the same size or SHORTER than the destination. See
    `TestWorkerJsonlSurvivesAWorkerRestart` for the full failure chain."""

    def test_pull_worker_state_issues_both_pulls_without_append(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="THB")

        def fake_rsync_pull(host, port, remote, dest, includes, append=False, run=None):
            fake_rsync_pull.calls.append((tuple(includes), append))
            return True
        fake_rsync_pull.calls = []

        orig = disp.rsync_pull
        disp.rsync_pull = fake_rsync_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_worker_state(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        calls = dict(fake_rsync_pull.calls)
        assert calls[("HEARTBEAT",)] is False       # static size -> append would freeze the mtime
        assert calls[("worker.jsonl",)] is False    # truncated by a worker restart -> same trap


class TestWorkerJsonlSurvivesAWorkerRestart:
    """Regression, live incident 2026-07-27 on instance -2 (desktop): ONE on-box worker restart
    permanently wedged the box for every task shipped to it afterwards.

    Chain: `spool_worker.py` restarted and recreated ~/spool/worker.jsonl from empty (143345 bytes
    -> 1010). `rsync --append` refuses to transfer a source that is the same size or SHORTER than
    the destination, so the local copy froze at its pre-restart size forever. `_pull_worker_state`
    reads that frozen file for `start` rows, so tasks never left `shipped`. `_pull_markers` still
    saw their DONE markers and `_complete_done` still rsynced results.json down — but
    `("shipped", "done")` is NOT in LEGAL_TRANSITIONS, so completion silently no-op'd and the tasks
    stuck in `shipped`, an OPEN state, holding their slots. Two finished runs (rc=0, results on
    disk) still read `shipped` an hour later.

    The two halves are asserted separately: the transfer must not be append-mode (so the `start`
    row arrives), and — since 2026-07-29 — completion no longer DEPENDS on that row arriving.

    ⚠ The second assertion below was INVERTED on 2026-07-29 (owner directive). It used to pin
    `("shipped","done") not in LEGAL_TRANSITIONS` and was named `test_shipped_cannot_shortcut_to_
    done`, faithfully documenting the state machine as it stood — but what it pinned was the very
    property that made a missed `start` unrecoverable instead of merely late. Keeping the append
    fix alone was not enough: the pull can also lag because the serial ship path starves
    `_ingest_and_complete` (observed 2026-07-29 on owned box -1, local copy 52 min stale, three
    finished tasks with DONE markers and `exit rc=0` all still reading `shipped`). Completion is
    now allowed straight from `shipped`, so an unobserved start costs visibility, never the run."""

    def test_shipped_can_complete_without_an_observed_start(self):
        """A terminal marker is ground truth; failing to SEE the start must not discard the run."""
        import registry_db
        # the normal path still exists
        assert ("shipped", "running") in registry_db.LEGAL_TRANSITIONS
        assert ("running", "done") in registry_db.LEGAL_TRANSITIONS
        # ...and every terminal outcome `_pull_markers` can drive for a `shipped` task is reachable
        for target in ("done", "task_failed", "cancelled", "queued"):
            assert ("shipped", target) in registry_db.LEGAL_TRANSITIONS, (
                f"_pull_markers queries `shipped` tasks and can drive them to {target!r}; "
                "leaving that pair illegal makes the transition silently no-op and wedges the "
                "task in an OPEN state, holding its slot forever")

    def test_start_is_ingested_after_the_remote_file_shrinks(self, tmp_path, monkeypatch):
        """The end-to-end behaviour: a post-restart worker.jsonl is SMALLER than the local copy,
        and its `start` row must still reach the task."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TWR")
        reg.transition(d.conn, "TWR", "shipped", "ship", "shipped before the worker restarted")
        # `_pull_worker_state` resolves its scratch dir from EXPERIMENTS_ROOT (the SHARED
        # experiments tree), not from disp.ROOT — point it at tmp_path so this neither reads nor
        # writes the real one.
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        local = tmp_path / ".dispatcher" / "instance_1"
        local.mkdir(parents=True, exist_ok=True)
        # a big pre-restart copy, none of whose rows concern this attempt
        (local / "worker.jsonl").write_text(
            "\n".join(json.dumps({"t": "2020-01-01T00:00:00Z", "task_id": "OLD", "event": "start"})
                      for _ in range(500)) + "\n")
        big = (local / "worker.jsonl").stat().st_size

        def fake_pull(host, port, remote, dest, includes, append=False, run=None):
            # A faithful stand-in: append-mode would SKIP a shorter source, so only a non-append
            # pull may overwrite. If the code ever goes back to append=True this returns without
            # writing and the assertion below fails, exactly as it did in production.
            if includes == ["worker.jsonl"] and not append:
                (local / "worker.jsonl").write_text(json.dumps(
                    {"t": "2099-01-01T00:00:00Z", "task_id": "TWR", "event": "start"}) + "\n")
            return True

        orig = disp.rsync_pull
        disp.rsync_pull = fake_pull
        try:
            assert (local / "worker.jsonl").stat().st_size == big
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_worker_state(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        assert (local / "worker.jsonl").stat().st_size < big, "post-restart file must be shorter"
        state = dict(d.conn.execute("SELECT * FROM tasks WHERE id='TWR'").fetchone())["state"]
        assert state == "running", f"start row after a worker restart was not ingested ({state})"


    def test_heartbeat_pull_failure_does_not_discard_a_good_worker_jsonl(self, tmp_path, monkeypatch):
        """HEARTBEAT is an INDEPENDENT 0-byte transfer. A failure there used to return early and
        leave already-on-disk `start` rows unread, so the task sat in `shipped` until some later
        cycle succeeded at BOTH pulls."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="THBF")
        reg.transition(d.conn, "THBF", "shipped", "ship", "shipped")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        local = tmp_path / ".dispatcher" / "instance_1"
        local.mkdir(parents=True, exist_ok=True)
        (local / "worker.jsonl").write_text(json.dumps(
            {"t": "2099-01-01T00:00:00Z", "task_id": "THBF", "event": "start"}) + "\n")

        def fake_pull(host, port, remote, dest, includes, append=False, run=None):
            return includes != ["HEARTBEAT"]      # only the HEARTBEAT transfer fails

        orig = disp.rsync_pull
        disp.rsync_pull = fake_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_worker_state(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        state = dict(d.conn.execute("SELECT * FROM tasks WHERE id='THBF'").fetchone())["state"]
        assert state == "running", (
            f"a failed HEARTBEAT pull discarded an on-disk start row (task is {state})")


class TestShippedCompletesWithoutAnObservedStart:
    """Live incident 2026-07-29, owned box -1 (laptop). The pulled `worker.jsonl` ran 52 min stale
    (464494B/03:45 vs 466356B/04:11 on the box) because the SERIAL ship path was starving
    `_ingest_and_complete`. Three tasks finished — DONE markers on disk, `exit rc=0` in the worker's
    own log — and all three still read `shipped`, because reaching `running` requires OBSERVING a
    `start` row and `("shipped","done")` was illegal. They completed only after the pull was
    refreshed by hand.

    The marker is ground truth about what the box did. Our failure to observe the start is a gap in
    OUR bookkeeping and must not discard a real outcome."""

    def _box_with_shipped_task(self, tmp_path, tid, marker):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id=tid)
        reg.transition(d.conn, tid, "shipped", "ship", "shipped; start never observed")
        d.run = lambda cmd, **kw: _FakeProc(0, f"/root/spool/active/{tid}/{marker}\n")
        return d

    def test_done_marker_completes_a_shipped_task(self, tmp_path, monkeypatch):
        d = self._box_with_shipped_task(tmp_path, "SD1", "DONE")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        # _complete_done gates on the completion artifact actually being present (inv. 9d)
        out = tmp_path / "g" / "SD1"
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text("{}")   # the `smoke` entrypoint's artifact
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._pull_markers(inst, "example.com", 2222)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD1'").fetchone())
        assert row["state"] == "done", (
            f"a verified DONE marker did not complete a `shipped` task ({row['state']}) -- "
            "it would hold its slot forever in an OPEN state")

    def test_artifact_missing_still_fails_a_shipped_task(self, tmp_path, monkeypatch):
        """The new pair ADMITS a completion, it must never INVENT one: no artifact -> task_failed."""
        d = self._box_with_shipped_task(tmp_path, "SD2", "DONE")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        out = tmp_path / "g" / "SD2"
        out.mkdir(parents=True, exist_ok=True)          # deliberately EMPTY
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._pull_markers(inst, "example.com", 2222)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD2'").fetchone())
        assert row["state"] == "task_failed"

    def test_artifact_missing_pulls_run_log_and_says_WHICH_artifact(self, tmp_path, monkeypatch):
        """Invariant 9e — the LAST uncovered terminal path. 9d gave `task_failed` (FAILED_<rc>) a
        forensic run.log pull and 18f gave `cancelled` one, but a task the worker declared DONE whose
        completion artifact never appeared landed here with the bare string "artifact_missing" and no
        log whatsoever.

        It is the worst case to leave dark precisely BECAUSE the worker claimed success — the owner
        has no hypothesis to start from. Live 2026-07-30, `m49_phase1_eye/p1_gv0`: ran 2h07m, wrote
        ckpt_latest.pt + .prev + two substrate checkpoints + TB events, so it plainly worked, then
        died terminally (`task_failed` never auto-requeues) with nothing to distinguish "never wrote
        the artifact" from "wrote it under another name" from "the job config declares the wrong
        completion_artifact". The box is torn down minutes later with the only copy of the log."""
        d = self._box_with_shipped_task(tmp_path, "SD9", "DONE")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        out = tmp_path / "g" / "SD9"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        roots = []

        def fake_pull(host, port, remote, local, includes, **kw):
            roots.append((remote, tuple(includes)))
            if "run.log" in includes:                     # the one-level-up forensic pull
                (out / "run.log").write_text("Traceback\nRuntimeError: never wrote results\n")
            return True

        monkeypatch.setattr(disp, "rsync_pull", fake_pull)
        alerts = []
        monkeypatch.setattr(d, "_alert", lambda m: alerts.append(m))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._pull_markers(inst, "example.com", 2222)

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD9'").fetchone())
        assert row["state"] == "task_failed"
        ev = dict(d.conn.execute("SELECT detail FROM events WHERE task_id='SD9' "
                                 "AND event='task_failed' ORDER BY seq DESC LIMIT 1").fetchone())
        assert "RuntimeError: never wrote results" in ev["detail"], (
            "the run.log tail did not reach the failure reason — `runq show` still explains nothing")
        expected = disp.entrypoints.resolve(
            dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD9'").fetchone())).completion_artifact
        assert expected in ev["detail"], (
            f"must name WHICH artifact was expected ({expected}) so the owner can compare it against "
            f"what the trainer actually wrote")
        # rooted one level UP, or the include can never match (the 9d structural trap)
        assert any(r.endswith("/") and not r.endswith("/out/") and "run.log" in i
                   for r, i in roots), f"run.log was not pulled from active/<id>/: {roots}"
        assert alerts and "completion_artifact" in alerts[0], (
            "a trainer/config contract mismatch repeats for every sibling arm — it must be ONE loud "
            "alarm, not N silent red rows")

    def test_failed_marker_terminates_a_shipped_task(self, tmp_path, monkeypatch):
        d = self._box_with_shipped_task(tmp_path, "SD3", "FAILED_1")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        monkeypatch.setattr(d, "_result_dir", lambda t: tmp_path / "g" / "SD3")
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._pull_markers(inst, "example.com", 2222)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD3'").fetchone())
        assert row["state"] == "task_failed", f"FAILED_ marker left it {row['state']}"

    def test_cancelled_marker_terminates_a_shipped_task(self, tmp_path, monkeypatch):
        d = self._box_with_shipped_task(tmp_path, "SD4", "CANCELLED")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._pull_markers(inst, "example.com", 2222)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='SD4'").fetchone())
        assert row["state"] == "cancelled", f"CANCELLED marker left it {row['state']}"


class TestShipBudgetBoundsThePass:
    """Invariant 7c. 7b retires a box whose transport is BROKEN; this covers ship work that simply
    SUCCEEDS slowly, which starves ingest just as effectively because `_ingest_and_complete` runs at
    the top of a serial `poll_once`. Live 2026-07-29: 10 ships in 15 min with ZERO starts and ZERO
    dones, `box_measured` 27 min stale, owned boxes' worker.jsonl 36-59 min stale — driven by 11%
    compile MISSES at a median 155s each."""

    def _dispatcher_with_claims(self, tmp_path, n):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="C0")
        reg.transition(d.conn, "C0", "claimed", "claim", "claimed")
        for i in range(1, n):
            tid = f"C{i}"
            reg.insert_task(
                d.conn, id=tid, created_at=reg.now_iso(), created_by="t", grp="g", name=tid,
                entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                arm_hash=tid, git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=4,
                state="claimed", instance_id=1)
        d.conn.commit()
        return d

    def test_budget_stops_the_pass_and_defers_the_rest(self, tmp_path, monkeypatch):
        d = self._dispatcher_with_claims(tmp_path, 6)
        d.settings["ship_budget_sec"] = 10
        clock = {"t": 1000.0}
        monkeypatch.setattr(disp.time, "time", lambda: clock["t"])
        shipped = []

        def slow_ship(task, inst):
            clock["t"] += 60.0          # every ship blows the whole budget
            shipped.append(task["id"])
            return True
        monkeypatch.setattr(d, "_ship", slow_ship)

        d._ship_all()
        assert len(shipped) == 1, f"budget did not bound the pass (shipped {len(shipped)})"
        left = d.conn.execute("SELECT COUNT(*) c FROM tasks WHERE state='claimed'").fetchone()["c"]
        assert left == 5, "deferred tasks must stay `claimed` for the next pass, untouched"
        ev = d.conn.execute(
            "SELECT detail FROM events WHERE event='ship_budget_spent'").fetchone()
        assert ev is not None, "a bounded pass must SAY so — silence reads as 'nothing left to ship'"
        assert "5 claimed task(s) deferred" in ev["detail"]

    def test_at_least_one_task_ships_even_when_the_budget_is_already_spent(self, tmp_path, monkeypatch):
        """A single task can cost 366s (measured max), and time passes inside the pass before the
        first check. A budget already exhausted at the first task must still ship ONE — otherwise
        the queue starves permanently.

        The clock advances on every READ, so `elapsed > budget` is already true when the first task
        is considered. That is what makes this test discriminate the `attempted and ...` guard: with
        a plain `elapsed > budget` it ships ZERO. (Found by mutation testing — the earlier version of
        this test used a clock that only moved inside `_ship`, so `elapsed` was 0 on the first
        iteration and the guard was never exercised.)"""
        d = self._dispatcher_with_claims(tmp_path, 3)
        d.settings["ship_budget_sec"] = 1
        clock = {"t": 1000.0}

        def ticking_now():
            clock["t"] += 5.0            # every time.time() call consumes 5s
            return clock["t"]
        monkeypatch.setattr(disp.time, "time", ticking_now)
        shipped = []
        monkeypatch.setattr(d, "_ship", lambda task, inst: (shipped.append(task["id"]), True)[1])

        d._ship_all()
        assert len(shipped) == 1, (
            f"forward progress must be guaranteed even when the budget is already spent "
            f"(shipped {len(shipped)}) — a budget smaller than one task would starve the queue")

    def test_a_pass_within_budget_ships_everything_and_stays_silent(self, tmp_path, monkeypatch):
        d = self._dispatcher_with_claims(tmp_path, 4)
        d.settings["ship_budget_sec"] = 300
        clock = {"t": 1000.0}
        monkeypatch.setattr(disp.time, "time", lambda: clock["t"])

        def quick_ship(task, inst):
            clock["t"] += 10.0          # 4 x 10s, well inside 300s
            return True
        monkeypatch.setattr(d, "_ship", quick_ship)

        d._ship_all()
        left = d.conn.execute("SELECT COUNT(*) c FROM tasks WHERE state='claimed'").fetchone()["c"]
        assert left == 0, "an under-budget pass must still ship everything"
        assert d.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event='ship_budget_spent'").fetchone()["c"] == 0

    def test_time_burned_on_a_7b_retired_box_is_not_charged_to_healthy_tasks(self, tmp_path, monkeypatch):
        """REGRESSION (measured live 2026-07-29, introduced by the first version of 7c).

        A degraded box's tasks sort FIRST (`priority DESC, created_at ASC` — stuck longest), so its
        attempts consumed the whole budget; 7b retired the box and 7c stopped the pass in the SAME
        instant, deferring 19 healthy tasks that would each have taken ~20s. Repeated every pass;
        four tasks on the OWNED boxes sat `claimed` 33 min behind it. 7b's guarantee is that a dead
        box costs ONE attempt per pass — charging that attempt to the budget hands the cost straight
        back to the tasks queued behind it."""
        d = self._dispatcher_with_claims(tmp_path, 5)
        d.settings["ship_budget_sec"] = 100
        d.settings["ship_parallel_boxes"] = 1   # this pins the SERIAL path's budget refund
        # a second, HEALTHY box; tasks C3/C4 live there
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at) VALUES (2,777,'good','x','live',0.05,'h',22,8,'x')")
        d.conn.execute("UPDATE tasks SET instance_id=2 WHERE id IN ('C3','C4')")
        d.conn.commit()
        clock = {"t": 1000.0}
        monkeypatch.setattr(disp.time, "time", lambda: clock["t"])
        shipped = []

        def ship(task, inst):
            if inst["id"] == 1:          # the degraded box: slow AND fails
                clock["t"] += 360.0
                d.tracker.record(1, False)   # drives consecutive_fails up -> 7b retires it
                return False
            clock["t"] += 20.0           # the healthy box: fast
            shipped.append(task["id"])
            return True
        monkeypatch.setattr(d, "_ship", ship)

        d._ship_all()
        assert set(shipped) == {"C3", "C4"}, (
            f"healthy tasks were starved by a retired box's attempt (shipped {shipped}) — 7b bounds "
            "the bad box to one attempt, so 7c must not re-charge that attempt to everyone else")

    def test_deferral_cannot_be_mistaken_for_undeliverable_by_inv_10d(self, tmp_path, monkeypatch):
        """10d requires a recorded `ship_failed` for THAT task on THAT instance. A task deferred by
        the budget has none, so the two features compose instead of fighting."""
        d = self._dispatcher_with_claims(tmp_path, 4)
        d.settings["ship_budget_sec"] = 10
        clock = {"t": 1000.0}
        monkeypatch.setattr(disp.time, "time", lambda: clock["t"])
        monkeypatch.setattr(d, "_ship", lambda t, i: (clock.__setitem__("t", clock["t"] + 60), True)[1])
        d._ship_all()
        deferred = [r["id"] for r in d.conn.execute("SELECT id FROM tasks WHERE state='claimed'")]
        assert deferred
        for tid in deferred:
            assert d.conn.execute(
                "SELECT COUNT(*) c FROM events WHERE task_id=? AND event='ship_failed'",
                (tid,)).fetchone()["c"] == 0


class TestTbEventsPulledLiveWhileRunning:
    """Regression: found live 2026-07-09 — the dashboard discovers runs only via local
    `tb/`/`results.json` presence, but the poll loop only ever pulled `worker.jsonl`/
    `HEARTBEAT`/markers and (every 5min) `ckpt_latest.pt` for a `running` task. TB events were
    only rsynced on terminal completion, so an in-progress task was invisible to the dashboard
    for its entire run. Invariant 9b requires TB events (append-only) to be pulled every poll."""

    def test_pull_tb_events_rsyncs_running_tasks_with_append(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TTB")
        reg.transition(d.conn, "TTB", "shipped", "ship", "shipped")
        reg.transition(d.conn, "TTB", "running", "start", "worker reported start")

        def fake_rsync_pull(host, port, remote, dest, includes, append=False, run=None):
            fake_rsync_pull.calls.append((remote, dest, includes, append))
            return True
        fake_rsync_pull.calls = []

        orig = disp.rsync_pull
        disp.rsync_pull = fake_rsync_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_tb_events(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        assert len(fake_rsync_pull.calls) == 1
        remote, dest, includes, append = fake_rsync_pull.calls[0]
        assert remote == "~/spool/active/TTB/out/"
        assert dest.endswith("experiments/g/TTB/")
        assert "tb/**" in includes
        assert append is True

    def test_non_running_tasks_are_not_pulled(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="TQ")  # left in `claimed`, never started

        def fake_rsync_pull(*a, **k):
            fake_rsync_pull.calls += 1
            return True
        fake_rsync_pull.calls = 0

        orig = disp.rsync_pull
        disp.rsync_pull = fake_rsync_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_tb_events(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        assert fake_rsync_pull.calls == 0

    def test_ingest_and_complete_invokes_tb_pull(self, tmp_path, monkeypatch):
        """poll_once's ingest step must include the TB pull, not just worker-state/markers/ckpt.

        Asserts the PULL, not the method. Invariant 23c moved this transport out of
        `_pull_tb_events` into `_tb_io` so it can run one-thread-per-box — and a test that spied on
        the method name would have gone RED on a refactor that kept 9b perfectly intact, while
        staying GREEN if someone later dropped the `tb/**` include. The include list, the remote
        root and `append=True` are the contract; which function issues them is not.
        """
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TIC")
        reg.transition(d.conn, "TIC", "shipped", "ship", "shipped")
        reg.transition(d.conn, "TIC", "running", "start", "worker reported start")

        pulls = []

        def fake_rsync_pull(host, port, remote, dest, includes, append=False, run=None):
            pulls.append((remote, tuple(includes), append))
            return True

        monkeypatch.setattr(disp, "rsync_pull", fake_rsync_pull)
        monkeypatch.setattr(d, "_pull_worker_state", lambda *a, **k: None)
        monkeypatch.setattr(d, "_pull_markers", lambda *a, **k: None)
        monkeypatch.setattr(d, "_reap_stalled", lambda: None)

        d._ingest_and_complete()
        tb = [p for p in pulls if "tb/**" in p[1]]
        assert tb, f"ingest issued no TB pull (invariant 9b); pulls were {pulls}"
        assert tb[0][0] == "~/spool/active/TIC/out/"
        assert tb[0][2] is True, "TB events are append-only and must be pulled with --append"


class TestCheckpointPullFindsSubdirCkpt:
    """Regression (2026-07-15): trainers write ckpt_latest.pt under out/<tag>/ (run_dir =
    out/cfg.tag), but _pull_checkpoints used a bare ["ckpt_latest.pt"] include — rsync's trailing
    --exclude '*' blocks descent into out/<tag>/ without a "*/" include, so the pull fetched
    nothing AND the post-pull existence check looked at out/ top level, the wrong depth. So
    resume_checkpoint stayed None and an infra-driven requeue (_infra_fail) restarted the run from
    scratch instead of warm-starting from the latest weights — the ff_rec_linear_L2 pretrain lost
    ~1600 updates on a box loss. The pull must descend and locate the checkpoint wherever it landed."""

    def test_pull_checkpoints_sets_resume_from_tag_subdir(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TCK")
        reg.transition(d.conn, "TCK", "shipped", "ship", "shipped")
        reg.transition(d.conn, "TCK", "running", "start", "worker reported start")

        def fake_rsync_pull(host, port, remote, dest, includes, append=False, run=None):
            fake_rsync_pull.includes = includes
            # a real recursive pull lands the trainer's out/<tag>/ckpt_latest.pt one level down
            sub = Path(dest) / "m21_demo_tag"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "ckpt_latest.pt").write_bytes(b"weights")
            return True
        fake_rsync_pull.includes = None

        orig = disp.rsync_pull
        disp.rsync_pull = fake_rsync_pull
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_checkpoints(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        # (1) the pull must ask rsync to descend into subdirs, else out/<tag>/ is never fetched
        assert "*/" in fake_rsync_pull.includes
        # (2) resume_checkpoint must point at the checkpoint wherever the trainer actually wrote it
        rc = d.conn.execute("SELECT resume_checkpoint FROM tasks WHERE id='TCK'").fetchone()[0]
        assert rc is not None, "resume_checkpoint left None -> infra requeue restarts from scratch"
        assert rc.endswith("m21_demo_tag/ckpt_latest.pt")

    def test_failed_pull_leaves_resume_checkpoint_untouched(self, tmp_path):
        """A failed rsync must not clobber a previously-good resume_checkpoint with None."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TCK2")
        reg.transition(d.conn, "TCK2", "shipped", "ship", "shipped")
        reg.transition(d.conn, "TCK2", "running", "start", "worker reported start")
        d.conn.execute("UPDATE tasks SET resume_checkpoint='/prior/ckpt_latest.pt' WHERE id='TCK2'")

        orig = disp.rsync_pull
        disp.rsync_pull = lambda *a, **k: False
        try:
            inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
            d._pull_checkpoints(inst, "example.com", 2222)
        finally:
            disp.rsync_pull = orig

        rc = d.conn.execute("SELECT resume_checkpoint FROM tasks WHERE id='TCK2'").fetchone()[0]
        assert rc == "/prior/ckpt_latest.pt"


class TestFailedPullsRunLogTraceback:
    """Regression, live 2026-07-24: a task that crashes while BUILDING the model writes nothing to
    `out/`, and its stdout+stderr (the traceback) is captured by the box worker to
    `active/<id>/run.log` — a SIBLING of `out/`, one level ABOVE it (spool_worker.py). Every ingest
    pull is rooted at `active/<id>/out/`, and an rsync --include can only match a path UNDER its
    root, so the traceback was NEVER pulled home: the sole record of the crash was the bare `worker
    exit <rc>` code and `run.log` died with the box at idle-teardown (a substrate that built cleanly
    in the identical local trainer path failed on the box — the exit code was all we had).
    `_complete_failed` must pull `run.log` rooted one level up AND fold its tail into the
    task_failed reason (surfaced by `runq show`) and the [ALERT] (coordinator.log)."""

    def _seed_running(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="TF")
        reg.transition(d.conn, "TF", "shipped", "ship", "shipped")
        reg.transition(d.conn, "TF", "running", "start", "worker reported start")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='TF'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        return d, reg, task, inst

    @staticmethod
    def _failed_detail(reg, conn):
        return [dict(e)["detail"] for e in reg.get_events(conn, "TF")
                if dict(e)["event"] == "task_failed"][0]

    def test_pulls_run_log_rooted_above_out_and_folds_tail(self, tmp_path):
        d, reg, task, inst = self._seed_running(tmp_path)
        local_out = d._result_dir(task)
        tb = ("Traceback (most recent call last):\n"
              "  File \"train_m48.py\", line 42, in build_substrate\n"
              "RuntimeError: substrate build failed: module graph has 0 nodes")

        def fake_rsync_pull(host, port, remote, dest, includes, append=False, run=None):
            fake_rsync_pull.calls.append((remote, tuple(includes)))
            if tuple(includes) == ("run.log",):  # simulate the box worker's captured stdout+stderr
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "run.log").write_text("...100 lines of earlier output...\n" + tb + "\n")
            return True
        fake_rsync_pull.calls = []

        alerts = []
        d._alert = lambda m: alerts.append(m)
        orig = disp.rsync_pull
        disp.rsync_pull = fake_rsync_pull
        try:
            d._complete_failed(task, inst, "example.com", 2222, "FAILED_1")
        finally:
            disp.rsync_pull = orig
            import shutil
            shutil.rmtree(local_out, ignore_errors=True)

        # (1) a DEDICATED pull rooted at active/<id>/ (one level ABOVE out/) with the run.log include
        assert ("~/spool/active/TF/", ("run.log",)) in fake_rsync_pull.calls
        # (2) the crash's exception line reached the DB task_failed reason -> `runq show` carries it
        detail = self._failed_detail(reg, d.conn)
        assert "RuntimeError: substrate build failed" in detail
        # (3) and the loud [ALERT] line the operator sees in coordinator.log
        assert any("RuntimeError: substrate build failed" in a for a in alerts)
        # (4) the task still lands terminal task_failed (the tail is additive, not gating)
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='TF'").fetchone())["state"] == "task_failed"

    def test_missing_run_log_degrades_to_bare_exit_reason(self, tmp_path):
        """A failed/absent run.log pull must never block the task_failed CAS — the tail is simply
        empty and the reason falls back to the old bare `worker exit <rc>` (+ zero-progress label)."""
        d, reg, task, inst = self._seed_running(tmp_path)
        local_out = d._result_dir(task)
        d._alert = lambda m: None
        orig = disp.rsync_pull
        disp.rsync_pull = lambda *a, **k: False  # every pull fails -> no run.log ever lands on disk
        try:
            d._complete_failed(task, inst, "example.com", 2222, "FAILED_7")
        finally:
            disp.rsync_pull = orig
            import shutil
            shutil.rmtree(local_out, ignore_errors=True)

        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='TF'").fetchone())["state"] == "task_failed"
        detail = self._failed_detail(reg, d.conn)
        assert "worker exit 7" in detail and "run.log tail" not in detail


class TestRunLogTail:
    """`_run_log_tail` bounds what a possibly-multi-MB log contributes to the DB reason / [ALERT]:
    only the last `max_bytes`, then the last `max_lines` of that; a partial leading line from the
    byte-window cut is dropped; a missing file is '' (best-effort, never raises)."""

    def test_returns_last_lines_only(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_RecordingRun())
        p = tmp_path / "run.log"
        p.write_text("\n".join(f"line {i}" for i in range(200)) + "\n")
        tail = d._run_log_tail(p, max_lines=3)
        assert tail == "line 197\nline 198\nline 199"

    def test_byte_window_drops_partial_first_line(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_RecordingRun())
        p = tmp_path / "run.log"
        p.write_text("A" * 5000 + "\nTRACE: real tail line\n")  # first line exceeds max_bytes
        tail = d._run_log_tail(p, max_lines=40, max_bytes=4096)
        assert tail == "TRACE: real tail line"  # the giant partial-cut leading line is discarded

    def test_missing_file_is_empty(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(), vastai_run=_RecordingRun())
        assert d._run_log_tail(tmp_path / "nope.log") == ""


class TestDenyMachines:
    """Bug found live 2026-07-09: the daemon auto-appending to the git-TRACKED machines.deny
    permanently dirtied the canonical checkout, silently defeating dispatcher_ctl.sh restart's
    auto-fast-forward to master. Runtime entries now go to a separate gitignored path
    (MACHINES_DENY_RUNTIME); load_deny_machines unions both so nothing learned is lost."""

    def test_unions_static_seed_and_runtime_learned(self, tmp_path, monkeypatch):
        static = tmp_path / "machines.deny"
        static.write_text("111  # seed\n")
        runtime = tmp_path / "runtime" / "machines.deny"  # parent deliberately absent
        monkeypatch.setattr(disp, "MACHINES_DENY", static)
        monkeypatch.setattr(disp, "MACHINES_DENY_RUNTIME", runtime)
        assert disp.load_deny_machines() == {111}
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text("222  # learned\n")
        assert disp.load_deny_machines() == {111, 222}

    def test_blacklist_and_destroy_creates_missing_parent(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)

        def fake_vastai_run(cmd, **kwargs):
            if "show" in cmd:
                return _FakeProc(0, json.dumps({"machine_id": 999}))
            return _FakeProc(0, "")
        d.vastai_run = fake_vastai_run

        deny_file = tmp_path / "nested" / "not" / "yet" / "created" / "machines.deny"
        d.conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, hard_cap_at) "
            "VALUES (1,'runq_x',?,?,0.10,4,?)",
            (disp.registry_db.now_iso(), "provisioning", disp._iso_plus_hours(1)))
        d.conn.commit()
        d._blacklist_and_destroy(1, deny_file, "test reason")
        assert deny_file.exists()
        assert "999" in deny_file.read_text()


class TestCostRecordedOnDestroy:
    """Regression: found live — cost_usd was only ever stamped on the `lost` path; a normal
    idle/hard-cap teardown left it NULL, silently breaking `runq spend`."""

    def test_destroy_stamps_cost_usd(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        past = disp._iso_plus_hours(-1)  # rented "1 hour ago"
        d.conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, hard_cap_at) "
            "VALUES (1,'runq_x',?,?,0.10,4,?)", (past, "live", disp._iso_plus_hours(1)))
        d.conn.commit()
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._destroy(inst, "idle")
        row = d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone()
        assert row["state"] == "destroyed"
        assert row["cost_usd"] == pytest.approx(0.10, abs=0.01)


class TestOwnedBoxNeverDestroyed:
    """Owned-box spec: `_destroy` is the single choke point every teardown path funnels through
    (idle timeout, dead-worker reap, stuck-provisioning reap) -- a self-owned box must survive
    all of them, since it was never rented (no Vast id to destroy) and a `destroyed` row is
    never re-adopted (adopt only fires for a genuine `runq_<task>`-labeled Vast instance)."""

    def test_destroy_is_a_noop_for_owned_source(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        d.conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
            "slots_total, hard_cap_at, source) VALUES (-1,'laptop',?,?,0.0,'h',22,1,?,'owned')",
            (disp.registry_db.now_iso(), "live", disp._iso_plus_hours(1)))
        d.conn.commit()
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=-1").fetchone())
        d._destroy(inst, "idle")
        row = dict(d.conn.execute("SELECT * FROM instances WHERE id=-1").fetchone())
        assert row["state"] == "live"
        assert row["destroyed_at"] is None
        assert not any("destroy" in c for c in run.joined())

    def test_reap_dead_workers_requeues_but_keeps_owned_box_live(self, tmp_path, monkeypatch):
        # A stale heartbeat on an owned box (laptop asleep/off) must still requeue the occupant
        # task (same as any infra loss) but must NOT destroy the box -- it should just be picked
        # up again, no re-registration needed, once it's reachable and heartbeating again.
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        now = disp.registry_db.now_iso()
        d.conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
            "slots_total, hard_cap_at, source) VALUES (-1,'laptop',?,?,0.0,'h',22,1,?,'owned')",
            (now, "live", disp._iso_plus_hours(1)))
        disp.registry_db.insert_task(
            d.conn, id="OW1", created_at=now, created_by="test", grp="g", name="OW1",
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash="OW1",
            arm_hash="OW1", git_sha="deadbeef", slots=1, est_minutes=1, priority=50,
            max_retries=1, state="shipped", instance_id=-1)
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_-1"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "HEARTBEAT"
        hb.write_text("")
        old = time.time() - d.settings["heartbeat_stale_min"] * 60 - 1
        os.utime(hb, (old, old))
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='OW1'").fetchone())
        assert task["state"] == "queued"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=-1").fetchone())
        assert inst["state"] == "live"  # NOT destroyed, unlike the equivalent Vast-rental case


class TestCompletionArtifactPresenceIsAuthoritative:
    """Regression (live incident 2026-07-11): the worker raised DONE and the run's completion
    artifact was on disk, but the final rsync returned ok=False (box torn down mid-pull) — the
    task was marked task_failed/artifact_missing, silently discarding a completed run's results
    fleet-wide. Presence of the artifact must win over the transport's exit code."""

    def _pull_stub(self, artifact_appears: bool, local_dir, artifact_name):
        def stub(host, port, remote, local, includes, run=None):
            if artifact_appears:
                (local_dir).mkdir(parents=True, exist_ok=True)
                (local_dir / artifact_name).write_text("{}")
            return False  # transport reports failure regardless
        return stub

    def test_done_with_present_artifact_despite_rsync_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="AR1")  # smoke → summary.json
        d.conn.execute("UPDATE tasks SET state='running' WHERE id='AR1'")
        d.conn.commit()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='AR1'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        local = tmp_path / task["grp"] / task["name"]
        monkeypatch.setattr(disp, "rsync_pull",
                            self._pull_stub(True, local, "summary.json"))
        d._complete_done(task, inst, "h", 22)
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='AR1'").fetchone())["state"] == "done"

    def test_done_without_artifact_still_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="AR2")
        d.conn.execute("UPDATE tasks SET state='running' WHERE id='AR2'")
        d.conn.commit()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='AR2'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        local = tmp_path / task["grp"] / task["name"]
        monkeypatch.setattr(disp, "rsync_pull",
                            self._pull_stub(False, local, "summary.json"))
        d._complete_done(task, inst, "h", 22)
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='AR2'").fetchone())["state"] == "task_failed"


class TestPreemptingReachesTerminalStates:
    """Regression (live incident 2026-07-13/14): a preempted task that never wrote a checkpoint
    (so the box-side kill guard, invariant 17b, never fires) keeps running to its own natural
    outcome. `LEGAL_TRANSITIONS` was missing preempting->{done,task_failed,infra_failed}, so
    `_complete_done`/`_complete_failed`/`_infra_fail` silently no-op'd (transition() doesn't
    raise on an illegal pair) and the task was stuck `preempting` forever with its results
    stranded — two overnight pretrains sat this way for 7+ hours before manual discovery."""

    def test_done_while_still_preempting_is_not_stranded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="PD1")
        d.conn.execute("UPDATE tasks SET state='preempting' WHERE id='PD1'")
        d.conn.commit()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='PD1'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        local = tmp_path / task["grp"] / task["name"]

        def stub(host, port, remote, dest, includes, run=None):
            local.mkdir(parents=True, exist_ok=True)
            (local / "summary.json").write_text("{}")
            return True
        monkeypatch.setattr(disp, "rsync_pull", stub)
        d._complete_done(task, inst, "h", 22)
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='PD1'").fetchone())["state"] == "done"

    def test_failed_while_still_preempting_is_not_stranded(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="PF1")
        d.conn.execute("UPDATE tasks SET state='preempting' WHERE id='PF1'")
        d.conn.commit()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='PF1'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(task, inst, "h", 22, "FAILED_1")
        assert dict(d.conn.execute("SELECT * FROM tasks WHERE id='PF1'").fetchone())["state"] == "task_failed"

    def test_instance_lost_while_preempting_infra_fails_and_requeues(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="PL1")
        d.conn.execute("UPDATE tasks SET state='preempting' WHERE id='PL1'")
        d.conn.commit()
        d._mark_lost(1)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='PL1'").fetchone())
        assert row["state"] == "queued"
        assert row["retries_used"] == 0.5


class TestAdoptIsIdempotent:
    """Regression (live incident 2026-07-11): a funds-exhaustion wipe marks every live box
    `lost`; `do_reconcile` then only loads instances in ('provisioning','live','draining'), so a
    lost box that Vast still lists (with its `runq_<known-task>` label) is seen as untracked and
    reconcile re-emits `adopt` for an id that already has a `lost` row. A bare INSERT tripped
    `UNIQUE(instances.id)` and crashed the poll loop every tick, taking the whole fleet down.
    Re-adopting must also clear destroyed_at so the box stops counting against spend-since."""

    def test_reconcile_re_adopts_a_lost_box_without_crashing(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="RA1", instance_id=1)
        # Simulate the wipe: the box was marked lost, but Vast still lists it this poll.
        d.conn.execute("UPDATE instances SET state='lost', destroyed_at=? WHERE id=1",
                       (disp.registry_db.now_iso(),))
        d.conn.commit()

        d.vastai_run = _fake_vast_instances([_VAST_INSTANCE_RECORD])

        d.do_reconcile()  # must not raise sqlite3.IntegrityError
        row = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert row["state"] == "live"
        assert row["destroyed_at"] is None

    def test_re_adopt_PRESERVES_the_rent_time_slot_sizing(self, tmp_path):
        """Regression (live incident 2026-08-03): `slots_total` is OURS — computed from the OFFER
        at rent time — and NO `vastai show instances` record carries it (verified against the live
        API). `ON CONFLICT … slots_total=excluded.slots_total` therefore wrote the `.get(…, 1)`
        DEFAULT over the correct sizing, capping a re-adopted box at ONE lane permanently
        (`_effective_slots` only takes `min`; nothing recomputes it). A 7.4c/hr RTX 3070 sized at 8
        lanes by its own offer packed one task instead of eight after a transient `vastai` failure
        marked it `lost` and the next poll re-adopted it.

        ⚠ This was invisible for a month because the idempotence test above FED a `slots_total` the
        real API never sends — the fixture supplied the field whose absence is the whole bug, so it
        wrote 4 over 4 and agreed with a broken implementation. The fixture is now the real shape.
        """
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="RA2", instance_id=1)
        d.conn.execute("UPDATE instances SET slots_total=8, state='lost', destroyed_at=? WHERE id=1",
                       (disp.registry_db.now_iso(),))
        d.conn.commit()
        d.vastai_run = _fake_vast_instances([dict(_VAST_INSTANCE_RECORD, label="runq_RA2")])

        d.do_reconcile()
        row = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert row["state"] == "live" and row["destroyed_at"] is None
        assert row["slots_total"] == 8, "re-adopt clobbered the rent-time sizing"

    def test_a_fresh_adopt_with_no_prior_row_defaults_to_one_lane_and_says_so(self, tmp_path):
        """The other arm: a genuinely untracked box has no rent-time sizing to preserve, so 1 lane
        is the safe fallback (under-pack an unknown box rather than OOM it). It must be VISIBLE —
        the instance record cannot be sized like an offer, because its `cpu_ram` is the HOST's, not
        the slice's, so a future fix has to scale by `gpu_frac` rather than reuse `offer_ram_gb`."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="RA3", instance_id=1)
        # Box 2 is on Vast with a label naming a task THIS registry knows, but has no row here.
        d.vastai_run = _fake_vast_instances([
            dict(_VAST_INSTANCE_RECORD, id=2, label="runq_RA3")])

        d.do_reconcile()
        row = dict(d.conn.execute("SELECT * FROM instances WHERE id=2").fetchone())
        assert row["slots_total"] == 1
        ev = d.conn.execute(
            "SELECT detail FROM events WHERE instance_id=2 AND event='adopt'").fetchone()
        assert "1 lane" in ev["detail"]


class TestReapStalled:
    """Invariant 19: a `running` (or, per 19e, `preempting`) task's checkpoint mtime not
    advancing for `stall_timeout_min` is treated exactly like any other infra failure (same
    retry budget, same event trail) -- the box is live and billing but doing no visible work."""

    def test_stale_checkpoint_requeues_via_infra_fail_path(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST1")
        ckpt = tmp_path / "ckpt_latest.pt"
        ckpt.write_text("x")
        old = time.time() - d.settings["stall_timeout_min"] * 60 - 1
        os.utime(ckpt, (old, old))
        # updated_at must ALSO be old (invariant 19c'): a genuine stall means the task has
        # been in `running` at least as long as the checkpoint has been silent.
        import datetime
        old_iso = datetime.datetime.fromtimestamp(
            old, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute(
            "UPDATE tasks SET state='running', resume_checkpoint=?, updated_at=? WHERE id='ST1'",
            (str(ckpt), old_iso))
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST1'").fetchone())
        assert row["state"] == "queued"
        assert row["retries_used"] == 0.5
        assert row["instance_id"] is None

    @staticmethod
    def _stale_running(d, tmp_path, task_id):
        import datetime
        _seed_instance_and_task(d.conn, task_id=task_id)
        ckpt = tmp_path / f"{task_id}_ckpt_latest.pt"
        ckpt.write_text("x")
        old = time.time() - d.settings["stall_timeout_min"] * 60 - 1
        os.utime(ckpt, (old, old))
        old_iso = datetime.datetime.fromtimestamp(
            old, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute("UPDATE tasks SET state='running', resume_checkpoint=?, updated_at=? WHERE id=?",
                       (str(ckpt), old_iso, task_id))
        d.conn.commit()

    def test_a_stall_reap_clears_the_box_copy_so_the_trainer_is_not_orphaned(self, tmp_path):
        """Invariant 19j (2026-09-30): two reaped LLM cells kept an owned laptop's GPU at 100% with zero
        fleet tasks on it — the requeue freed the DB slot and left the trainer running. The box copy is
        removed (the 18e/19h act that makes `reap_orphans` collect it) for exactly that task."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        self._stale_running(d, tmp_path, "ST9")
        d._reap_stalled()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='ST9'").fetchone())["state"] == "queued"
        cleanups = [c for c in run.joined() if "rm -rf ~/spool/incoming/ST9 ~/spool/active/ST9" in c]
        assert len(cleanups) == 1
        events = [r[0] for r in d.conn.execute("SELECT event FROM events WHERE task_id='ST9'")]
        assert "stall_box_copy_cleared" in events

    def test_a_failed_box_cleanup_never_blocks_the_requeue(self, tmp_path):
        class _Failing(_RecordingRun):
            def __call__(self, cmd, **kwargs):
                self.calls.append(cmd)
                if "rm -rf" in " ".join(cmd):
                    raise OSError("box unreachable")
                return _FakeProc(0, "")

        run = _Failing()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        self._stale_running(d, tmp_path, "ST10")
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT state, retries_used FROM tasks WHERE id='ST10'").fetchone())
        assert row["state"] == "queued" and row["retries_used"] == 0.5     # the pre-19j outcome, intact

    def test_retry_with_stale_checkpoint_is_not_instantly_reaped(self, tmp_path):
        # Invariant 19c' / bug 12 (live incident 2026-07-10): after an infra-failure requeue,
        # resume_checkpoint still points at the DEAD attempt's pull (>timeout old by
        # construction). The fresh attempt's CAS into `running` stamps updated_at=now — the
        # reaper must give it a full stall_timeout_min of grace, not kill it on the next poll.
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST4")
        ckpt = tmp_path / "ckpt_latest.pt"
        ckpt.write_text("x")
        old = time.time() - d.settings["stall_timeout_min"] * 60 - 1
        os.utime(ckpt, (old, old))
        d.conn.execute(  # updated_at stays fresh: registry_db stamps it at the CAS
            "UPDATE tasks SET state='running', resume_checkpoint=? WHERE id='ST4'", (str(ckpt),))
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST4'").fetchone())
        assert row["state"] == "running"
        assert row["retries_used"] == 0

    def test_fresh_checkpoint_is_not_stalled(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST2")
        ckpt = tmp_path / "ckpt_latest.pt"
        ckpt.write_text("x")
        d.conn.execute(
            "UPDATE tasks SET state='running', resume_checkpoint=? WHERE id='ST2'", (str(ckpt),))
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST2'").fetchone())
        assert row["state"] == "running"
        assert row["retries_used"] == 0

    def test_latest_tb_mtime_finds_newest_event_and_absence_is_none(self, tmp_path):
        # Invariant 19f: the reaper's TB-liveness anchor. Newest events.out.tfevents* mtime is
        # returned; a run with no tb/ (or an empty one) yields None so it falls back to ckpt/
        # running_since (never a false "alive").
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        out = tmp_path / "run"
        assert d._latest_tb_mtime(out) is None                     # no tb/ at all
        tb = out / "tb"; tb.mkdir(parents=True)
        assert d._latest_tb_mtime(out) is None                     # tb/ present but empty
        old = tb / "events.out.tfevents.1"; old.write_text("a")
        new = tb / "sub" / "events.out.tfevents.2"; new.parent.mkdir(); new.write_text("b")
        import os
        os.utime(old, (1000.0, 1000.0)); os.utime(new, (6880.0, 6880.0))
        assert d._latest_tb_mtime(out) == 6880.0                   # newest across the tree

    def test_no_checkpoint_yet_is_not_flagged_while_fresh(self, tmp_path):
        # A task that JUST started (running_since ~= now) and hasn't pulled a checkpoint yet
        # (still warming up / provisioning) must not be reaped -- it hasn't had stall_timeout_min
        # to produce one.
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST3")
        d.conn.execute("UPDATE tasks SET state='running' WHERE id='ST3'")
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST3'").fetchone())
        assert row["state"] == "running"

    def test_no_checkpoint_ever_stalls_once_running_since_ages_out(self, tmp_path):
        # Bug 13 (2026-07-13): a task that hangs before its FIRST checkpoint (e.g. stuck in
        # provisioning/startup) used to be invisible to the reaper forever -- `resume_checkpoint`
        # stays NULL, so there was never an mtime to compare against. It must now age off
        # `running_since` (the task's CAS into `running`) exactly like a stale-checkpoint task.
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST5")
        old = time.time() - d.settings["stall_timeout_min"] * 60 - 1
        import datetime
        old_iso = datetime.datetime.fromtimestamp(
            old, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute(
            "UPDATE tasks SET state='running', updated_at=? WHERE id='ST5'", (old_iso,))
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST5'").fetchone())
        assert row["state"] == "queued"
        assert row["retries_used"] == 0.5
        assert row["instance_id"] is None

    def test_stuck_preempting_with_no_fresh_checkpoint_is_reaped(self, tmp_path):
        # Invariant 19e (retrospective bug, 2026-07-14): before this fix, `_reap_stalled` only
        # ever looked at `state='running'`, so a task the dispatcher had already CAS'd into
        # `preempting` (invariant 17b's `preempt_intent`) was invisible to it for as long as it
        # stayed `preempting` -- if the box-side kill guard never saw a fresh checkpoint (so the
        # worker never actually killed it), the task could hang there forever with NO automatic
        # recovery. Live incident: two overnight pretrains sat `preempting` for 7+ hours,
        # discovered only by manual monitoring. `updated_at` restamped at the preempt CAS must
        # age off the same way a `running` task's `running_since` does.
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST6")
        old = time.time() - d.settings["stall_timeout_min"] * 60 - 1
        import datetime
        old_iso = datetime.datetime.fromtimestamp(
            old, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute(
            "UPDATE tasks SET state='preempting', updated_at=? WHERE id='ST6'", (old_iso,))
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST6'").fetchone())
        assert row["state"] == "queued"
        assert row["retries_used"] == 0.5
        assert row["instance_id"] is None

    def test_fresh_preempting_is_not_reaped(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="ST7")
        d.conn.execute("UPDATE tasks SET state='preempting' WHERE id='ST7'")
        d.conn.commit()
        d._reap_stalled()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='ST7'").fetchone())
        assert row["state"] == "preempting"


class TestCancelInFlight:
    """Cancel-in-flight (2026-07-09): a running task -> `cancelling`; the dispatcher writes a
    CANCEL marker to its worker, then completes it terminally as `cancelled` (no requeue)."""

    def _running_task(self, d, tid):
        reg = _seed_instance_and_task(d.conn, task_id=tid)
        reg.transition(d.conn, tid, "shipped", "ship", "shipped")
        reg.transition(d.conn, tid, "running", "start", "running")
        return reg

    def test_signal_writes_cancel_marker_for_live_task(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C1")
        assert reg.cancel_task(d.conn, "C1", "test cancel").ok
        assert d.conn.execute("SELECT state FROM tasks WHERE id='C1'").fetchone()[0] == "cancelling"
        d._signal_cancels()
        assert any("touch" in c and "C1/CANCEL" in c for c in run.joined())
        # still cancelling until the worker's CANCELLED marker is ingested
        assert d.conn.execute("SELECT state FROM tasks WHERE id='C1'").fetchone()[0] == "cancelling"

    def test_complete_cancelled_terminates_and_frees_slot(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C2")
        reg.cancel_task(d.conn, "C2", "test cancel")
        d._complete_cancelled(dict(d.conn.execute("SELECT * FROM tasks WHERE id='C2'").fetchone()))
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='C2'").fetchone())
        assert row["state"] == "cancelled" and row["instance_id"] is None

    def test_cancelling_with_dead_box_completes_without_signal(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C3")
        reg.cancel_task(d.conn, "C3", "test cancel")
        d.conn.execute("UPDATE instances SET state='lost' WHERE id=1")
        d.conn.commit()
        d._signal_cancels()
        assert d.conn.execute("SELECT state FROM tasks WHERE id='C3'").fetchone()[0] == "cancelled"
        assert not any("CANCEL" in c for c in run.joined())

    def test_a_cancel_whose_spool_dir_is_GONE_terminates_instead_of_signalling_forever(self, tmp_path,
                                                                                        monkeypatch):
        """A SIGKILLed worker cannot write its own CANCELLED marker, so a `cancelling` task whose
        spool dir has been cleaned will NEVER be answered.

        Live 2026-08-14, `m55e_boxcheck2/bid_tower`: cancelled at 15:52 (`exit rc=-9`), dir
        gone, and the dispatcher re-wrote CANCEL at 18:23/18:25/18:29/18:32/18:38/18:45/18:53/19:01
        while the task held one of tower's 16 slots. The bare `touch` discarded its exit code,
        so "signalled, waiting" and "nobody will ever answer" were indistinguishable.
        """
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C9")
        reg.cancel_task(d.conn, "C9", "test cancel")

        def dir_absent(host, port, cmd, run=None):
            # `test -d ... && touch ...` exits 1 when the directory is not there
            return subprocess.CompletedProcess([], 1, "", "")

        monkeypatch.setattr(disp, "ssh_run", dir_absent)
        d._signal_cancels()

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='C9'").fetchone())
        assert row["state"] == "cancelled", (
            f"task is {row['state']!r}, not 'cancelled' — a cancel that can never be acknowledged "
            "must terminate, or it re-signals every poll and leaks its lane for the life of the "
            "daemon")
        assert row["instance_id"] is None, "the lane was not released"

    def test_a_TRANSIENT_ssh_failure_does_NOT_short_circuit_the_cancel(self, tmp_path, monkeypatch):
        """rc=255 means the SSH failed, NOT that the dir is absent. Completing then would mark the
        task cancelled while its trainer kept running — what the box-pause note forbids."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C10")
        reg.cancel_task(d.conn, "C10", "test cancel")

        monkeypatch.setattr(
            disp, "ssh_run",
            lambda h, p, c, run=None: subprocess.CompletedProcess([], 255, "", "ssh: fail"))
        d._signal_cancels()

        assert d.conn.execute("SELECT state FROM tasks WHERE id='C10'").fetchone()[0] == "cancelling", (
            "a transient ssh error was treated as proof the task dir is gone; the trainer may still "
            "be running on the box")

    def test_the_signal_checks_the_dir_before_touching(self, tmp_path, monkeypatch):
        """The exit code only distinguishes the cases if `test -d` runs FIRST — a bare `touch`
        cannot tell an absent directory from a delivered signal."""
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C11")
        reg.cancel_task(d.conn, "C11", "test cancel")
        seen = []

        def rec(host, port, cmd, run=None):
            seen.append(cmd)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(disp, "ssh_run", rec)
        d._signal_cancels()

        assert seen and "test -d" in seen[0] and "C11/CANCEL" in seen[0], (
            f"cancel signal was {seen!r} — it must probe the directory before touching, or its exit "
            "code cannot separate 'delivered' from 'dir gone'")
        assert d.conn.execute("SELECT state FROM tasks WHERE id='C11'").fetchone()[0] == "cancelling"

    def test_cancel_pulls_run_log_home_for_forensics(self, tmp_path, monkeypatch):
        """Invariant 18f. Cancel is how an operator stops a MISBEHAVING task, and the box is torn
        down right after — so before this, a hung-then-cancelled cell left no forensic trail at all
        (live 2026-07-29: the evidence for a cell that never launched died with its box). The pull
        must be rooted ONE LEVEL UP at `active/<id>/`, because `run.log` is a sibling of `out/` and
        an rsync --include only matches paths under its remote root (the same reason 9d needed it)."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C4")
        reg.cancel_task(d.conn, "C4", "test cancel")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='C4'").fetchone())
        d._complete_cancelled(task, "example.com", 2222)

        pulls = [c for c in run.joined() if c.startswith("rsync") and "run.log" in c]
        assert pulls, "a cancelled task's run.log must be pulled home — otherwise the only record " \
                      "of WHY it was cancelled dies with the box"
        assert any("active/C4/" in c and "active/C4/out" not in c for c in pulls), (
            "the run.log pull must be rooted at active/<id>/, not out/ — an --include cannot match "
            "a path above its remote root, so an out/-rooted pull can never reach run.log")
        # the forensic pull is best-effort decoration: the terminal CAS still happens
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='C4'").fetchone())
        assert row["state"] == "cancelled" and row["instance_id"] is None

    def test_cancel_without_an_endpoint_still_terminates(self, tmp_path, monkeypatch):
        """Invariant 18c: a `cancelling` task whose box was already lost has nowhere to pull from.
        The log pull must never become a precondition for completing the cancel."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = self._running_task(d, "C5")
        reg.cancel_task(d.conn, "C5", "test cancel")
        d._complete_cancelled(dict(d.conn.execute("SELECT * FROM tasks WHERE id='C5'").fetchone()))
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='C5'").fetchone())
        assert row["state"] == "cancelled" and row["instance_id"] is None
        assert not any("run.log" in c for c in run.joined())


class TestCancelRacingAnInFlightShip:
    """Invariant 18e (live 2026-07-29, `azsc-p1e`). `claimed` is not "no worker yet" from the
    DISPATCHER's side — it is the state a task occupies while `_ship` is delivering its bundle
    (compile + apt + rsync; median 30s, max 366s). Registry inv. 4a cancels a `claimed` task
    immediately, so a cancel inside that window leaves the delivery to complete anyway: `READY`
    lands, the worker claims it and launches a trainer, and the `claimed -> shipped` CAS silently
    no-ops because `transition()` never raises on an illegal pair.

    That orphan is owned by nobody: terminal in the registry, running on a paid box, holding a slot
    against the worker's `max_slots` gate, and invisible to `reap_orphans` (its `active/<id>` dir
    exists and carries no terminal marker). Measured: two such trainers ran 3h+ holding 2 of a
    6-slot box, and the sibling they starved looked exactly like a hung trainer for 85 min while
    in fact having NO PROCESS — which is also why the harness watchdog never fired on it."""

    def _claimed_task_cancelled_mid_ship(self, tmp_path, monkeypatch, ship_ok=True):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="S1")
        d.conn.commit()

        def ship_then_cancel(task, inst):
            # the operator cancels WHILE the bundle is being delivered; `claimed` -> `cancelled`
            # is immediate (registry inv. 4a), and the delivery below still completes.
            reg.cancel_task(d.conn, "S1", "test cancel")
            return ship_ok
        monkeypatch.setattr(d, "_ship", ship_then_cancel)
        return d, run, reg

    def test_payload_delivered_after_a_cancel_is_removed_from_the_box(self, tmp_path, monkeypatch):
        d, run, _ = self._claimed_task_cancelled_mid_ship(tmp_path, monkeypatch)
        d._ship_all()

        assert d.conn.execute("SELECT state FROM tasks WHERE id='S1'").fetchone()[0] == "cancelled"
        cleanups = [c for c in run.joined() if "rm -rf" in c and "S1" in c]
        assert cleanups, (
            "a payload delivered after the task went terminal is owned by NOBODY — left on the box "
            "the worker claims it and runs an untracked trainer forever")
        joined = " ".join(cleanups)
        assert "incoming/S1" in joined, "the undelivered-but-present READY must go, or the worker claims it"
        assert "active/S1" in joined, (
            "active/<id> must go too: that is the ONLY handle on an already-launched trainer — "
            "reap_orphans classifies a process whose owning task dir is gone as an orphan")

    def test_the_unowned_delivery_is_logged_never_silent(self, tmp_path, monkeypatch):
        d, _, _ = self._claimed_task_cancelled_mid_ship(tmp_path, monkeypatch)
        d._ship_all()
        ev = d.conn.execute("SELECT detail FROM events WHERE event='ship_unowned'").fetchone()
        assert ev is not None, "a delivery we then had to retract must be visible, not silent"
        assert "illegal" in ev["detail"], "say WHY the CAS did not apply"

    def test_an_ordinary_ship_never_triggers_cleanup(self, tmp_path, monkeypatch):
        """The guard must key on the CAS result, not fire on every ship — deleting a healthy task's
        spool dir would destroy the run it just delivered."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="S2")
        d.conn.commit()
        monkeypatch.setattr(d, "_ship", lambda task, inst: True)

        d._ship_all()
        assert d.conn.execute("SELECT state FROM tasks WHERE id='S2'").fetchone()[0] == "shipped"
        assert not any("rm -rf" in c and "S2" in c for c in run.joined()), \
            "a successful ship must leave the box copy alone"
        assert d.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event='ship_unowned'").fetchone()["c"] == 0


class TestNoOverpackWithinOnePoll:
    """Bug fix 2026-07-09: `_place_queue` built the instances view ONCE at poll start and never
    decremented free slots as it claimed tasks, so every queued task packed onto the same box —
    over-packing a slots_total=1 box with N CPU-bound lanes (the M14 MinAtar contention)."""

    def test_place_queue_respects_slots_total(self, tmp_path):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="OCCUPANT", instance_id=1)
        reg.cancel_task(d.conn, "OCCUPANT", "test cancel")  # free the box (claimed -> cancelled)
        # one live box with a SINGLE slot and a hard cap far enough out that tasks fit time-wise
        d.conn.execute("UPDATE instances SET slots_total=1, hard_cap_at=? WHERE id=1",
                       (disp._iso_plus_hours(9),))
        now = reg.now_iso()
        for tid in ("Q1", "Q2", "Q3"):
            reg.insert_task(d.conn, id=tid, created_at=now, created_by="t", grp="g", name=tid,
                            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                            arm_hash=tid, git_sha="x", slots=1, est_minutes=1, priority=50,
                            max_retries=1, state="queued", instance_id=None)
        d.conn.commit()
        d._place_queue()
        on_box = d.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE instance_id=1 AND state='claimed'").fetchone()[0]
        assert on_box == 1  # exactly ONE fits; the other two must not over-pack the single slot


class TestEligibleOffers:
    """Invariant 4e: the offer-candidate quality gates — reliability floor (2026-07-10 spend
    audit) + gpu_deny substrings + effective-cores floor (2026-07-21 hardware-quality directive)."""

    def test_drops_below_floor_keeps_missing(self):
        raw = [
            {"id": 1, "dph_total": 0.05, "gpu_name": "A", "reliability2": 0.85},   # drop
            {"id": 2, "dph_total": 0.06, "gpu_name": "B", "reliability2": 0.97},   # keep
            {"id": 3, "dph_total": 0.07, "gpu_name": "C"},                          # keep (fail-open)
            {"id": 4, "dph_total": 0.04, "gpu_name": "D", "reliability": 0.80},     # drop (fallback field)
            {"id": 5, "dph_total": 0.08, "gpu_name": "E", "reliability2": 0.90},   # keep (== floor)
        ]
        offers, dropped = disp.eligible_offers(raw, {"min_reliability": 0.90})
        assert [o["id"] for o in offers] == [2, 3, 5]
        assert dropped == 2

    def test_maps_placer_fields(self):
        raw = [{"id": 9, "dph_total": 0.05, "machine_id": 42, "gpu_name": "RTX 3060",
                "gpu_ram": 12288, "cpu_cores_effective": 8, "reliability2": 0.99}]
        offers, dropped = disp.eligible_offers(raw, {"min_reliability": 0.90})
        assert dropped == 0
        o = offers[0]
        assert o["machine_id"] == 42 and o["gpu_ram_gb"] == 12.0 and o["cpu_cores_effective"] == 8
        assert o["reliability"] == 0.99

    def test_floor_zero_keeps_everything(self):
        raw = [{"id": 1, "dph_total": 0.05, "reliability2": 0.10}]
        offers, dropped = disp.eligible_offers(raw, {"min_reliability": 0.0})
        assert len(offers) == 1 and dropped == 0

    def test_gpu_deny_drops_matching_names(self):
        # substring, case-insensitive; a good card and a missing name are both kept.
        raw = [
            {"id": 1, "dph_total": 0.05, "gpu_name": "GTX 1080", "cpu_cores_effective": 9},   # deny
            {"id": 2, "dph_total": 0.06, "gpu_name": "RTX 3060 Ti", "cpu_cores_effective": 24},  # keep
            {"id": 3, "dph_total": 0.05, "gpu_name": "Titan Xp", "cpu_cores_effective": 12},   # deny
            {"id": 4, "dph_total": 0.09, "cpu_cores_effective": 8},                             # keep (no name)
        ]
        S = {"min_reliability": 0.0, "gpu_deny": ["GTX", "Titan Xp"], "min_cpu_cores_effective": 0.0}
        offers, dropped = disp.eligible_offers(raw, S)
        assert [o["id"] for o in offers] == [2, 4]
        assert dropped == 2

    def test_cores_floor_drops_slivers(self):
        raw = [
            {"id": 1, "dph_total": 0.05, "gpu_name": "RTX 2060", "cpu_cores_effective": 1.71},  # drop
            {"id": 2, "dph_total": 0.06, "gpu_name": "RTX 4070S", "cpu_cores_effective": 4},    # keep (small modern card, above floor)
            {"id": 3, "dph_total": 0.07, "gpu_name": "RTX 3060 Ti", "cpu_cores_effective": 24},  # keep
        ]
        S = {"min_reliability": 0.0, "gpu_deny": [], "min_cpu_cores_effective": 2.0}
        offers, dropped = disp.eligible_offers(raw, S)
        assert [o["id"] for o in offers] == [2, 3]
        assert dropped == 1


class TestOfferCounterfactual:
    """Invariant 5d: the offer-set counterfactual logged per rent (observability only)."""

    S = {"min_reliability": 0.90, "vram_per_lane_gb": 3.0, "cores_per_lane": 1.0,
         "max_slots_cap": 8}
    TASK = {"slots": 1, "resource_hint": None}   # a big GPU below yields many slots

    def _big(self, **o):
        # 12 GB / 8 cores -> 4 slots; plenty for a 1-slot task
        return {"gpu_ram": 12288, "cpu_cores_effective": 8, **o}

    def test_cheapest_dropped_for_reliability(self):
        raw = [self._big(id=1, dph_total=0.04, reliability2=0.80, gpu_name="dud"),   # cheapest, sub-floor
               self._big(id=2, dph_total=0.06, reliability2=0.95, gpu_name="ok")]    # rented
        chosen = {"dph_total": 0.06, "gpu_name": "ok", "reliability": 0.95}
        cf = disp.offer_counterfactual(raw, chosen, self.TASK, self.S)
        assert cf["n_considered"] == 2 and cf["n_qualifying"] == 1
        assert cf["cheapest_alt"]["reason"] == "reliability_below_floor"
        assert cf["premium_dph"] == 0.02

    def test_cheapest_too_small_for_task(self):
        # a 2-slot GPU task: cheapest box (4 GB -> 1 lane) can't host it, the big box (4 lanes) can.
        # It must CLAIM the GPU: since invariant 26m the card does not size a task that never
        # touches it, so a hint-less task fits the 4 GB box on its 8 cores.
        task2 = {"slots": 2, "resource_hint": {"requires_gpu": True}}
        raw = [{"id": 1, "dph_total": 0.03, "gpu_ram": 4096, "cpu_cores_effective": 8,
                "reliability2": 0.99, "gpu_name": "tiny"},                                # 1 lane < 2
               self._big(id=2, dph_total=0.06, reliability2=0.99, gpu_name="ok")]         # 4 lanes >= 2
        chosen = {"dph_total": 0.06, "gpu_name": "ok", "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, task2, self.S)
        assert cf["cheapest_alt"]["reason"] == "too_few_slots"
        assert cf["premium_dph"] == 0.03

    def test_global_cheapest_chosen_no_premium(self):
        raw = [self._big(id=1, dph_total=0.05, reliability2=0.99, gpu_name="ok"),    # rented (cheapest)
               self._big(id=2, dph_total=0.09, reliability2=0.99, gpu_name="pricey")]
        chosen = {"dph_total": 0.05, "gpu_name": "ok", "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, self.TASK, self.S)
        assert cf["cheapest_alt"] is None and cf["premium_dph"] == 0.0

    def test_fail_open_on_bad_offer_field(self):
        raw = [{"id": 1, "dph_total": "not-a-number", "gpu_ram": 12288,
                "cpu_cores_effective": 8, "reliability2": 0.80, "gpu_name": "bad"},   # skipped
               self._big(id=2, dph_total=0.06, reliability2=0.95, gpu_name="ok")]
        chosen = {"dph_total": 0.06, "gpu_name": "ok", "reliability": 0.95}
        cf = disp.offer_counterfactual(raw, chosen, self.TASK, self.S)   # must not raise
        assert cf["n_considered"] == 1 and cf["cheapest_alt"] is None    # bad one dropped, no cheaper alt

    def test_empty_offer_set(self):
        cf = disp.offer_counterfactual([], {"dph_total": 0.06}, self.TASK, self.S)
        assert cf["n_considered"] == 0 and cf["cheapest_alt"] is None and cf["premium_dph"] == 0.0

    def test_worse_value_density_alt(self):
        # 4e cores-per-$ ranking (2026-07-21): the cheaper-per-BOX 6-core slice loses per LANE to
        # the 20-core box (uncapped lane capacity, no demand term) — the diagnostic classifies it
        # as the expected ranking outcome, not a placer bug. Mirrors the GTX-1080-vs-3060-Ti case.
        task = {"slots": 1, "resource_hint": {"vram_per_lane_gb": 2.0, "cores_per_lane": 4}}
        raw = [{"id": 1, "dph_total": 0.0412, "gpu_ram": 8192, "cpu_cores_effective": 6,
                "reliability2": 0.99, "gpu_name": "ti"},        # 1 lane -> 4.12c/lane
               {"id": 2, "dph_total": 0.0481, "gpu_ram": 12288, "cpu_cores_effective": 20,
                "reliability2": 0.99, "gpu_name": "3060"}]      # 5 lanes -> 0.96c/lane, rented
        chosen = {"dph_total": 0.0481, "gpu_name": "3060", "gpu_ram_gb": 12.0,
                  "cpu_cores_effective": 20, "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, task, self.S, demand=5)
        assert cf["cheapest_alt"]["reason"] == "worse_value_density"
        assert cf["chosen"]["lanes"] == 5 and cf["chosen"]["usable_now"] == 5
        assert cf["chosen"]["dph_per_lane"] == pytest.approx(0.0481 / 5, abs=1e-6)
        assert cf["cheapest_alt"]["dph_per_lane"] == pytest.approx(0.0412, abs=1e-6)

    def test_cheapest_gpu_denied(self):
        S = {**self.S, "gpu_deny": ["GTX"], "min_cpu_cores_effective": 0.0}
        raw = [self._big(id=1, dph_total=0.04, reliability2=0.99, gpu_name="GTX 1080"),  # cheapest, denied
               self._big(id=2, dph_total=0.06, reliability2=0.99, gpu_name="RTX 3060 Ti")]  # rented
        chosen = {"dph_total": 0.06, "gpu_name": "RTX 3060 Ti", "gpu_ram_gb": 12.0,
                  "cpu_cores_effective": 8, "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, self.TASK, S)
        assert cf["cheapest_alt"]["reason"] == "gpu_denied"
        assert cf["premium_dph"] == 0.02 and cf["n_qualifying"] == 1

    def test_cheapest_below_cores_floor(self):
        S = {**self.S, "gpu_deny": [], "min_cpu_cores_effective": 2.0}
        raw = [{"id": 1, "dph_total": 0.03, "gpu_ram": 12288, "cpu_cores_effective": 1.5,
                "reliability2": 0.99, "gpu_name": "sliver"},                                  # cheapest, sub-floor
               self._big(id=2, dph_total=0.06, reliability2=0.99, gpu_name="ok")]             # rented
        chosen = {"dph_total": 0.06, "gpu_name": "ok", "gpu_ram_gb": 12.0,
                  "cpu_cores_effective": 8, "reliability": 0.99}
        cf = disp.offer_counterfactual(raw, chosen, self.TASK, S)
        assert cf["cheapest_alt"]["reason"] == "below_cores_floor"
        assert cf["premium_dph"] == 0.03 and cf["n_qualifying"] == 1


class TestReapDeadWorkers:
    """`heartbeat_stale_min`: a worker process that dies mid-life (not just a stalled task)
    leaves its occupant task stuck (`claimed`/`shipped`/`running`) and the box `feasible_task_
    waiting` forever, so it idle-bills with no automatic recovery. A stale pulled HEARTBEAT
    file is the signal; the fix requeues the occupant(s) and destroys the box."""

    def test_stale_heartbeat_requeues_task_and_destroys_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HB1")
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='HB1'")
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_1"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "HEARTBEAT"
        hb.write_text("")
        old = time.time() - d.settings["heartbeat_stale_min"] * 60 - 1
        os.utime(hb, (old, old))
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB1'").fetchone())
        assert task["state"] == "queued"
        assert task["instance_id"] is None
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "destroyed"
        assert any("destroy" in c for c in run.joined())

    def test_fresh_heartbeat_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HB2")
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='HB2'")
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_1"
        hb_dir.mkdir(parents=True)
        (hb_dir / "HEARTBEAT").write_text("")
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB2'").fetchone())
        assert task["state"] == "shipped"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "live"

    def test_no_heartbeat_yet_is_not_flagged(self, tmp_path, monkeypatch):
        """A box that has never produced a HEARTBEAT yet is still provisioning/booting --
        covered by provision_timeout_min/rent_patience_min, not this reaper."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HB3")
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='HB3'")
        d.conn.commit()
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB3'").fetchone())
        assert task["state"] == "shipped"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "live"

    def test_stale_heartbeat_from_failing_pulls_is_not_reaped(self, tmp_path, monkeypatch):
        """Invariant 10c (2026-07-14 incident): a stale local mtime is also exactly what a run
        of failed rsync pulls looks like (a slow ingest cycle over several live instances, or a
        transient ssh/rsync hiccup) -- indistinguishable from a genuinely dead worker by mtime
        alone. Live incident: multiple healthy, still-training boxes got destroyed this way.
        `ConnectionTracker.consecutive_fails > 0` means the most recent pull for this instance
        did NOT succeed, so the mtime we're reading isn't a confirmed fresh read of the remote
        file -- the reaper must not fire until a pull actually succeeds again."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HB4")
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='HB4'")
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_1"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "HEARTBEAT"
        hb.write_text("")
        old = time.time() - d.settings["heartbeat_stale_min"] * 60 - 1
        os.utime(hb, (old, old))
        d.tracker.record(1, False)
        d.tracker.record(1, False)
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB4'").fetchone())
        assert task["state"] == "shipped"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "live"

    def _stale_pull_box(self, tmp_path, monkeypatch, run, task_id):
        """A live box whose local HEARTBEAT copy AND last heartbeat pull are both stale — the state a
        poll cycle longer than `heartbeat_stale_min` puts EVERY box into (invariant 10c(g))."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id=task_id)
        d.conn.execute(f"UPDATE tasks SET state='running' WHERE id='{task_id}'")
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_1"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "HEARTBEAT"
        hb.write_text("")
        old = time.time() - d.settings["heartbeat_stale_min"] * 60 - 1
        os.utime(hb, (old, old))
        # The box is REACHABLE and busy — every ship/ingest/compile records a fresh success, which
        # is exactly the state a reap/re-ship churn produces. What is stale is the HEARTBEAT PULL.
        d.tracker.record(1, True)
        d.tracker.record_heartbeat_pull(1, True)
        d.tracker._hb_pull_ok[1] = old                 # last heartbeat pull is as old as the mtime
        assert d.tracker.consecutive_fails(1) == 0     # so the connectivity guard passes it through
        return d, hb

    def test_stale_pull_on_a_LIVE_worker_is_confirmed_fresh_not_reaped(self, tmp_path, monkeypatch):
        """Invariant 10c, second half (live recurrence 2026-07-26, owned box -2), as re-established
        by 10c(g): `consecutive_fails == 0` says the last pull ATTEMPT succeeded, never WHEN. A poll
        cycle busy re-shipping a previous reap's tasks never ATTEMPTS a pull for this instance, so
        the counter sits at 0 from an old success while our local HEARTBEAT copy ages past the
        threshold — and the reaper must not destroy this HEALTHY, actively-training box. Verified
        live: worker alive and touching HEARTBEAT on the box at 22:51, our pulled copy frozen at
        22:42, reaper fired at 22:50 and requeued 8 tasks across three campaigns.

        10c(g) keeps that protection but obtains the confirmation instead of giving up on it: the
        reaper pulls HEARTBEAT for this box and re-stats. A LIVE worker touches the remote file every
        60 s and `rsync -t` preserves that mtime, so the refreshed copy reads fresh and the box is
        left alone — modelled here by a run that touches the file on a HEARTBEAT pull."""
        run = _HeartbeatRun(tmp_path / ".dispatcher" / "instance_1" / "HEARTBEAT", alive=True)
        d, hb = self._stale_pull_box(tmp_path, monkeypatch, run, "HB6")
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB6'").fetchone())
        assert task["state"] == "running", "a healthy box was reaped on an unconfirmed stale read"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "live"
        assert run.heartbeat_pulls == 1, "the reaper must ATTEMPT the confirming pull, not skip"
        ev = [r[0] for r in d.conn.execute("SELECT event FROM events WHERE instance_id=1")]
        assert "dead_worker_confirm_fresh" in ev and "dead_worker" not in ev

    def test_stale_pull_on_a_DEAD_worker_is_confirmed_stale_and_reaped(self, tmp_path, monkeypatch):
        """The branch a 31-minute poll cycle made unreachable (measured 2026-07-31: 183
        `dead_worker_skipped` vs 4 `dead_worker`, a 97.9% skip rate, four genuinely dead boxes
        surviving hours each with their occupants stranded and NO failure signal).

        Confirming pull SUCCEEDS and the mtime is STILL stale ⇒ a confirmed fresh read of a remote
        the worker has stopped touching ⇒ genuinely dead, so it reaps — regardless of how long the
        cycle took to come round. This is the whole point of 10c(g): the decision no longer depends
        on cycle timing."""
        run = _HeartbeatRun(tmp_path / ".dispatcher" / "instance_1" / "HEARTBEAT", alive=False)
        d, hb = self._stale_pull_box(tmp_path, monkeypatch, run, "HB7")
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB7'").fetchone())
        assert task["state"] == "queued", "a confirmed-dead worker was not reaped"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "destroyed"
        assert run.heartbeat_pulls == 1

    def test_stale_pull_that_cannot_be_confirmed_is_still_skipped(self, tmp_path, monkeypatch):
        """10c's original protection survives 10c(g) intact: if the confirming pull itself FAILS we
        still cannot attribute the staleness to the worker (unreachable box, transient rsync/ssh
        hiccup), so the reaper skips exactly as before and leaves the box to the next pass."""
        run = _HeartbeatRun(tmp_path / ".dispatcher" / "instance_1" / "HEARTBEAT",
                            alive=False, pull_ok=False)
        d, hb = self._stale_pull_box(tmp_path, monkeypatch, run, "HB8")
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB8'").fetchone())
        assert task["state"] == "running", "reaped a box we could not confirm"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "live"
        ev = [r[0] for r in d.conn.execute("SELECT event FROM events WHERE instance_id=1")]
        assert "dead_worker_skipped" in ev and "dead_worker" not in ev

    def test_stale_heartbeat_reaped_once_pulls_recover(self, tmp_path, monkeypatch):
        """Once a pull for this instance succeeds again (`consecutive_fails` resets to 0), a
        still-stale mtime is now a confirmed fresh read of the remote file -- genuine death --
        and the reaper fires normally."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HB5")
        d.conn.execute("UPDATE tasks SET state='shipped' WHERE id='HB5'")
        d.conn.commit()
        hb_dir = tmp_path / ".dispatcher" / "instance_1"
        hb_dir.mkdir(parents=True)
        hb = hb_dir / "HEARTBEAT"
        hb.write_text("")
        old = time.time() - d.settings["heartbeat_stale_min"] * 60 - 1
        os.utime(hb, (old, old))
        d.tracker.record(1, False)
        d.tracker.record(1, True)  # the pull itself succeeded; mtime is still genuinely old
        d._reap_dead_workers()
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='HB5'").fetchone())
        assert task["state"] == "queued"
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        assert inst["state"] == "destroyed"


class TestSettingsMigration:
    """Guarded default migration: a superseded default moves forward, a customized value stays."""

    def _seed(self, path, **rows):
        conn = disp.registry_db.connect(str(path))
        for k, v in rows.items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
        conn.commit(); conn.close()

    def test_provision_timeout_old_default_migrates(self, tmp_path):
        db = tmp_path / "runs.sqlite"
        self._seed(db, provision_timeout_min=45)          # the superseded default
        d = disp.Dispatcher(str(db))
        assert d.settings["provision_timeout_min"] == 15  # moved forward
        assert d.settings["min_reliability"] == 0.90      # new key seeded alongside

    def test_customized_provision_timeout_untouched(self, tmp_path):
        db = tmp_path / "runs.sqlite"
        self._seed(db, provision_timeout_min=30)          # operator-tuned, not the old default
        d = disp.Dispatcher(str(db))
        assert d.settings["provision_timeout_min"] == 30  # left alone

    def test_fresh_registry_gets_new_defaults(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"))
        assert d.settings["provision_timeout_min"] == 15
        assert d.settings["min_reliability"] == 0.90
        # 2026-07-21 hardware-quality gate keys seed on a fresh registry too.
        assert d.settings["offer_search_limit"] == 1000
        assert "GTX" in d.settings["gpu_deny"]
        assert d.settings["min_cpu_cores_effective"] == 2.0

    def test_quality_gate_keys_seed_into_existing_registry(self, tmp_path):
        # New keys reach an ALREADY-seeded registry via _ensure_settings' missing-key seeding, so
        # `dispatch-restart` is all it takes to make the live coordinator honor them — no migration.
        db = tmp_path / "runs.sqlite"
        self._seed(db, min_reliability=0.90)  # pre-existing registry without the new keys
        d = disp.Dispatcher(str(db))
        assert d.settings["offer_search_limit"] == 1000
        assert d.settings["min_cpu_cores_effective"] == 2.0
        assert "Titan Xp" in d.settings["gpu_deny"]


class TestHeartbeatRefreshOrdering:
    """The dead-worker reaper's input must not sit behind the cycle's expensive I/O."""

    def test_heartbeats_refresh_before_the_expensive_ingest(self, tmp_path, monkeypatch):
        """Root cause of the 2026-07-26 owned-box incident: HEARTBEAT was pulled only inside
        `_ingest_and_complete` (~4 rsyncs/instance) and before `_ship_all`'s compiles, so a "30s"
        cycle ran minutes and the 0-byte file the reaper judges by went 5-8 min stale on a live
        worker. Assert it is refreshed FIRST, and that the pull is stamped so the reaper's guard
        can tell a fresh confirmation from a stale one."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="HBR")
        d.conn.commit()
        order = []
        monkeypatch.setattr(disp, "rsync_pull",
                            lambda *a, **k: (order.append(tuple(k.get("includes") or a[4])), True)[1])
        monkeypatch.setattr(d, "_ingest_and_complete", lambda: order.append("INGEST"))
        for m in ("_advance_provisioning", "_signal_cancels", "_signal_drain", "_reap_over_capacity",
                  "_measure_box_resources", "_place_queue", "_consolidate", "_ship_all",
                  "_teardown_idle", "_reconcile_instances", "_reap_dead_workers"):
            if hasattr(d, m):
                monkeypatch.setattr(d, m, lambda *a, **k: None)
        d._refresh_heartbeats()
        d._ingest_and_complete()
        assert order and order[0] == ("HEARTBEAT",), f"heartbeat not pulled first: {order}"
        assert order.index(("HEARTBEAT",)) < order.index("INGEST")
        assert d.tracker.seconds_since_heartbeat_pull(1) is not None, "the pull was not stamped"


class TestUndeliverableBox:
    """Invariants 7 (resumable push), 7b (one bad box may not starve the pass) and 10d
    (undeliverable claims) — the 2026-07-29 fleet incident.

    A rented box degraded to ~70 KB/s while its siblings ran ~550-615 KB/s from the same uplink at
    the same moment. A 37.6 MB bundle cannot cross that link inside `rsync_push`'s 120s budget, and
    because the push carried no `--partial`, every timeout SIGKILLed the process group and the
    receiver discarded the temp file — three attempts, ~6m07s, zero bytes delivered, forever. Six
    tasks were claimed onto it and ship is SERIAL, so 36.5 min of every poll went to that one box
    while healthy boxes' tasks (~23s each once reached) waited behind it. Nothing reaped it: 10b
    needs state `shipped` and skips boxes with `consecutive_fails > 0`, 10c needs a dead worker and
    this worker was alive, 19/19h only watch `running`/`shipped`.
    """

    def _dispatcher(self, tmp_path, run=None):
        run = run or _RecordingRun()
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)

    def _box(self, conn, reg, inst_id=1, source="vast"):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, 555, f"runq_{inst_id}", reg.now_iso(), "live", 0.05, "h", 22, 4,
             reg.now_iso(), source))
        conn.commit()

    def _task(self, conn, reg, tid, inst_id, state="claimed", age_min=0.0, retries=0.0,
              created_at=None):
        import datetime
        reg.insert_task(
            conn, id=tid, created_at=created_at or reg.now_iso(), created_by="t", grp="g", name=tid,
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid, arm_hash=tid,
            git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=4, state=state,
            instance_id=inst_id, code_blob=f"blob_{tid}", code_sha256=_store.digest(_BLOB),
            code_format="compiled")
        _store.put(disp.EXPERIMENTS_ROOT, f"blob_{tid}", _BLOB)   # the coordinator no longer builds
        old = (datetime.datetime.utcnow() - datetime.timedelta(minutes=age_min)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conn.execute("UPDATE tasks SET updated_at=?, retries_used=? WHERE id=?", (old, retries, tid))
        conn.commit()

    # ---- invariant 7: the push must be able to resume ----

    def test_push_is_resumable_so_a_slow_link_converges(self):
        """THE root cause. Without `--partial`, a timed-out push leaves nothing behind and all three
        attempts restart at byte 0, so a payload the link cannot move in 120s is undeliverable no
        matter how many polls try. `--inplace` writes straight to the destination so there is no
        temp file for the receiver to discard."""
        calls = []
        disp.rsync_push("h", 22, ["/tmp/bundle.tar"], "~/spool/incoming/x/",
                        run=lambda cmd, **kw: (calls.append(cmd), _FakeProc(0, ""))[1])
        assert "--partial" in calls[0], f"push is not resumable: {calls[0]}"
        assert "--inplace" in calls[0], f"push still uses a discardable temp file: {calls[0]}"

    # ---- invariant 7f: the PULL must never leave a truncated destination ----

    def _pull_flags(self, append):
        calls = []
        disp.rsync_pull("h", 22, "~/spool/active/x/out/", "/tmp/out/", ["ckpt_latest.pt"],
                        append=append, run=lambda cmd, **kw: (calls.append(cmd), _FakeProc(0, ""))[1])
        return calls[0]

    def test_a_checkpoint_pull_does_not_write_in_place(self):
        """THE opposite requirement to the push above, and the reason this is its own test.

        `--inplace` writes straight into the destination, so the 60s SIGKILL leaves a TRUNCATED file
        wearing the name of a valid checkpoint. It is not a tail risk: for any payload the link cannot
        move in 60s it happens on EVERY pull, which is how it corrupts `ckpt_latest.pt` and its
        `.prev` spare in turn — defeating the one mechanism (`save_atomic`) that exists to survive a
        torn transfer. Measured on m49_pc_dream: local `.prev` 283 MB short of the box's, five cells
        resumed onto `PytorchStreamReader ... corrupted` and restarted from scratch.

        Without it rsync reconstructs into a temp file and renames, so a killed pull degrades to a
        STALE checkpoint rather than a corrupt one."""
        assert "--inplace" not in self._pull_flags(append=False), (
            "a non-append pull writes in place; a timed-out transfer will leave a truncated file "
            "that torch.load rejects, and it will destroy the .prev spare on the following pull")

    def test_the_append_pull_keeps_inplace(self):
        """`--append` is `--inplace` by nature and is used only for `tb/**`, which only ever grows.
        Pinned so the fix above cannot be over-applied into breaking the TB stream."""
        flags = self._pull_flags(append=True)
        assert "--append" in flags and "--inplace" in flags, (
            f"the append pull lost its in-place semantics: {flags}")

    # ---- invariant 7b: one bad box may not consume the pass ----

    def test_a_transport_failure_defers_only_that_box_not_the_fleet(self, tmp_path, monkeypatch):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.settings["ship_parallel_boxes"] = 1   # 7b on the SERIAL path; the parallel twin is below
        self._box(d.conn, reg, inst_id=1)   # the degraded box
        self._box(d.conn, reg, inst_id=2)   # a healthy sibling
        # The broken box's tasks sort FIRST (older created_at) — exactly as live, since they are the
        # ones that have been stuck longest.
        for i, tid in enumerate(["B1", "B2", "B3"]):
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        self._task(d.conn, reg, "H1", 2, created_at="2026-07-29T04:00:00Z")
        attempts = []

        def fake_ship(task, inst):
            attempts.append((task["id"], inst["id"]))
            if inst["id"] == 1:
                d.tracker.record(1, False)  # what the real `_ship` does on an rsync/apt failure
                return False
            return True

        monkeypatch.setattr(d, "_ship", fake_ship)
        d._ship_all()
        on_broken = [t for t, i in attempts if i == 1]
        assert on_broken == ["B1"], f"burned the pass on a dead box: {attempts}"
        assert ("H1", 2) in attempts, f"healthy box starved behind the dead one: {attempts}"
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='H1'").fetchone())["state"] \
            == "shipped"
        # the deferred siblings keep their claim and are simply retried next poll
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='B2'").fetchone())["state"] \
            == "claimed"

    def test_a_task_level_failure_does_not_defer_the_box(self, tmp_path, monkeypatch):
        """`_ship` also returns False for faults that say nothing about the box (a failed `git
        archive`, an entrypoint whose apt packages don't exist). Deferring the box on those would
        stall its healthy siblings for a fault they don't share — so the trigger is the tracker's
        transport counter moving, not `ok` alone."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.settings["ship_parallel_boxes"] = 1   # SERIAL path; the parallel twin is below
        self._box(d.conn, reg, inst_id=1)
        for i, tid in enumerate(["B1", "B2", "B3"]):
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        attempts = []
        monkeypatch.setattr(d, "_ship",
                            lambda task, inst: (attempts.append((task["id"], inst["id"])), False)[1])
        d._ship_all()
        on_box = [t for t, i in attempts if i == 1]
        assert on_box == ["B1", "B2", "B3"], f"task-level fault wrongly deferred the box: {on_box}"

    # ---- invariant 10d(fast): arming on the SHIP path, not only in the reaper ----

    def _fastfail_box(self, d, monkeypatch, n_tasks=4, inst_id=1):
        """A box whose every ship fails FAST — the poisoned-blob-cache shape. `_ship` returns False
        and the transport counter moves, exactly as the real path does on an rsync/blob failure."""
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.settings["ship_parallel_boxes"] = 1
        self._box(d.conn, reg, inst_id=inst_id)
        for i in range(n_tasks):
            self._task(d.conn, reg, f"F{i}", inst_id, created_at=f"2026-07-29T03:0{i}:00Z")

        def fake_ship(task, inst):
            d.tracker.record(inst["id"], False)
            return False
        monkeypatch.setattr(d, "_ship", fake_ship)
        return reg

    def test_a_FAST_failing_box_is_quarantined_without_any_claim_ageing(self, tmp_path, monkeypatch):
        """⛔⛔ THE GAP THIS CLOSES. Arming used to live ONLY in `_reap_undeliverable_claims`, which
        needs a task to sit CLAIMED past `undeliverable_after_min` and then be requeued. A box that
        fails delivery FAST never produces that: the task goes straight to `task_failed`, `requeued`
        stays 0, and the arming line never runs.

        MEASURED 2026-08-30, instance 40000055 — a poisoned blob cache failed every ship in ~11s;
        the box stayed `live`, kept winning placements, and destroyed 39 tasks across two unrelated
        sessions. NOTHING here ages: the quarantine must arm on repeated ship failure alone."""
        d = self._dispatcher(tmp_path)
        self._fastfail_box(d, monkeypatch)
        assert d._ship_quarantined({"id": 1}) is False, "precondition: not quarantined yet"
        for _ in range(d.settings["ship_quarantine_after_fails"]):
            d._ship_all()
        assert d._ship_quarantined({"id": 1}) is True, (
            "a box that failed every ship is STILL in the placement pool — this is the fast-fail "
            "gap that let one poisoned box eat 39 tasks across two sessions")

    def test_one_ship_failure_does_NOT_quarantine(self, tmp_path, monkeypatch):
        """⚠ The other half. Barring a box on a single blip is how a healthy box gets stranded, so
        the threshold must be > 1 and this pins it."""
        d = self._dispatcher(tmp_path)
        self._fastfail_box(d, monkeypatch)
        d._ship_all()                      # ONE pass -> one failure on the box
        assert d.settings["ship_quarantine_after_fails"] > 1, "a 1-failure threshold strands blips"
        assert d._ship_quarantined({"id": 1}) is False, (
            "quarantined after a single ship failure — a transient blip now bars a healthy box")

    def test_a_recovered_box_lifts_its_own_quarantine(self, tmp_path, monkeypatch):
        """Symmetry: arming is new, lifting already existed. A box that starts delivering again must
        rejoin the pool with no operator action, or this fix trades one stranded box for another."""
        d = self._dispatcher(tmp_path)
        self._fastfail_box(d, monkeypatch)
        for _ in range(d.settings["ship_quarantine_after_fails"]):
            d._ship_all()
        assert d._ship_quarantined({"id": 1}) is True, "precondition: quarantined"
        monkeypatch.setattr(d, "_ship", lambda task, inst: True)
        d.conn.execute("UPDATE tasks SET state='claimed', instance_id=1 WHERE id='F0'")
        d.conn.commit()
        d._ship_all()
        assert d._ship_quarantined({"id": 1}) is False, (
            "a box that delivered successfully is still quarantined — recovery must be automatic")

    def test_a_TASK_level_fault_still_does_not_quarantine_the_box(self, tmp_path, monkeypatch):
        """⛔ The discriminator, mirroring `test_a_task_level_failure_does_not_defer_the_box`. A
        failed `git archive` or a bad apt package makes `_ship` return False while saying NOTHING
        about the box. The trigger is the transport counter moving, not `ok` being False — otherwise
        one broken entrypoint quarantines a healthy box for every campaign on it."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.settings["ship_parallel_boxes"] = 1
        self._box(d.conn, reg, inst_id=1)
        for i in range(4):
            self._task(d.conn, reg, f"T{i}", 1, created_at=f"2026-07-29T03:0{i}:00Z")
        # NOTE: no `tracker.record(..., False)` — a task-level fault, not a transport one
        monkeypatch.setattr(d, "_ship", lambda task, inst: False)
        for _ in range(d.settings["ship_quarantine_after_fails"] + 2):
            d._ship_all()
        assert d._ship_quarantined({"id": 1}) is False, (
            "a task-level fault quarantined the BOX — one bad entrypoint now bars a healthy box "
            "for every other campaign on it")

    # ---- invariant 10d: the reaper ----

    def _stick(self, d, reg, inst_id=1, tid="U1", age_min=120.0, evidence=True):
        self._box(d.conn, reg, inst_id=inst_id)
        self._task(d.conn, reg, tid, inst_id, "claimed", age_min=age_min, retries=1.0)
        if evidence:
            d.log("ship_failed", "rsync push failed after 3 attempts", task_id=tid,
                  instance_id=inst_id)

    def test_undeliverable_claim_is_requeued_free_and_the_box_is_quarantined(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._stick(d, reg)
        d._reap_undeliverable_claims()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='U1'").fetchone())
        assert row["state"] == "queued"
        assert row["instance_id"] is None
        # NOT an infra failure charged to the task — the scheduler failed to deliver it (19h's rule)
        assert row["retries_used"] == 1.0, "requeue must be free"
        # the box's copy is cleared BEFORE the requeue so a link that recovers can't double-launch
        assert any("rm -rf" in " ".join(c) and "U1" in " ".join(c) for c in d.run.calls)
        assert d._ship_quarantined({"id": 1}) is True

    def test_a_claim_with_no_recorded_ship_failure_is_left_alone(self, tmp_path):
        """The discriminator. Age alone cannot separate "undeliverable" from "queued behind a slow
        pass": measured fleet-wide, claim->ship p99 is 98.7 min and the max successful ship took
        301.8 min, so an age-only rule would requeue work that was about to land."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._stick(d, reg, age_min=600.0, evidence=False)
        d._reap_undeliverable_claims()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='U1'").fetchone())["state"] \
            == "claimed"
        assert d._ship_quarantined({"id": 1}) is False

    def test_a_young_claim_is_left_alone(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._stick(d, reg, age_min=10.0)  # far under the 90min timeout
        d._reap_undeliverable_claims()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='U1'").fetchone())["state"] \
            == "claimed"

    def test_a_box_doing_real_work_is_never_touched(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._stick(d, reg)
        self._task(d.conn, reg, "R1", 1, "running")
        d._reap_undeliverable_claims()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='U1'").fetchone())["state"] \
            == "claimed"
        assert d._ship_quarantined({"id": 1}) is False

    def test_failed_cleanup_defers_without_requeueing_or_quarantining(self, tmp_path):
        """On a box we cannot reach, this ssh is exactly what fails. Requeueing anyway would risk a
        double-run if the link later recovers and the worker finds the payload; quarantining anyway
        would bar a box on the strength of a check we never completed."""
        class _FailingSsh(_RecordingRun):
            def __call__(self, cmd, **kwargs):
                self.calls.append(cmd)
                return _FakeProc(255, "")

        d = self._dispatcher(tmp_path, run=_FailingSsh())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._stick(d, reg)
        d._reap_undeliverable_claims()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='U1'").fetchone())["state"] \
            == "claimed"
        assert d._ship_quarantined({"id": 1}) is False

    # ---- invariant 10d: what the quarantine does ----

    def test_a_quarantined_box_takes_no_new_work(self, tmp_path):
        task = {"id": "N", "slots": 1, "est_minutes": 10, "priority": 50, "resource_hint": None}
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast"}
        assert disp._fits_now(task, inst, SHARED_SETTINGS) is True
        assert disp._fits_now(task, {**inst, "ship_quarantined": True}, SHARED_SETTINGS) is False

    def test_an_empty_quarantined_rental_is_torn_down_not_left_idle_billing(self, tmp_path):
        """`should_teardown` reports `feasible_task_waiting` for as long as ANY queued task fits, so
        without this a quarantined box idle-bills forever against a queue it can never serve."""
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast",
                "ship_quarantined": True}
        queued = [{"id": "Q", "slots": 1, "est_minutes": 10, "priority": 50}]
        down, reason = disp.should_teardown(inst, queued, time.time(), SHARED_SETTINGS)
        assert (down, reason) == (True, "undeliverable")
        # but not while it still holds work shipped before the quarantine
        busy = {**inst, "occupants": [{"id": "X", "slots": 1, "state": "running",
                                       "est_minutes": 10, "running_minutes_ago": 1}]}
        assert disp.should_teardown(busy, queued, time.time(), SHARED_SETTINGS)[0] is False

    def test_an_owned_box_is_quarantined_but_never_destroyed(self, tmp_path):
        """Nothing re-adopts a destroyed owned row, and an owned box genuinely recovers — box -1
        came back from streaks of 311 and 284 consecutive ship failures."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=-1, source="owned")
        self._task(d.conn, reg, "U1", -1, "claimed", age_min=120.0)
        d.log("ship_failed", "rsync push failed", task_id="U1", instance_id=-1)
        d._reap_undeliverable_claims()
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='U1'").fetchone())["state"] \
            == "queued"
        assert d._ship_quarantined({"id": -1}) is True
        owned = {"id": -1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                 "occupants": [], "idle_minutes": 999.0, "dph_usd": 0.0, "source": "owned",
                 "ship_quarantined": True}
        assert disp.should_teardown(owned, [], time.time(), SHARED_SETTINGS) \
            == (False, "owned_box_never_torn_down")

    def test_a_successful_delivery_lifts_the_quarantine(self, tmp_path, monkeypatch):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._set = d._set_ship_quarantine({"id": 1}, "test")
        self._task(d.conn, reg, "G1", 1)
        monkeypatch.setattr(d, "_ship", lambda task, inst: True)
        d._ship_all()
        assert d._ship_quarantined({"id": 1}) is False
        assert dict(d.conn.execute("SELECT state FROM tasks WHERE id='G1'").fetchone())["state"] \
            == "shipped"

    def test_the_quarantine_survives_a_dispatcher_restart(self, tmp_path):
        """Invariant 1: every scheduling decision must be reconstructible from the DB. An in-memory
        flag would hand the box a fresh backlog on each respawn — and the supervisor respawns."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        d._set_ship_quarantine({"id": 1}, "test")
        d2 = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                             vastai_run=_RecordingRun())
        assert d2._ship_quarantined({"id": 1}) is True
        assert any(i["id"] == 1 and i["ship_quarantined"] for i in d2._instances_view())


class TestDrainHold:
    """Invariant 21h — THE DRAIN-REPACK RACE, and the fix the owner actually asked for.

    21g made drains RARER (a value bar + a cooldown). It did nothing about a drain undoing itself:
    a drain requeues its occupants, those tasks become ordinary `queued` work, and the box they just
    left is by construction a perfect fit — so the next `_place_queue` ships them straight back.

    MEASURED 2026-07-29, box 40000015: drained 16:51 with 5 preempts, and 3 of those 5 tasks were
    running on it again by 16:57 — WITH THE QUEUE AT ZERO, so this is not contention, it is the
    drain feeding itself. Fleet-wide, 79 of 111 drains ever (71%) were repacked before any teardown.

    The cost is scientific, not $/hr: one victim (`azsc-p1e/…_seed3`) reached 11 resume cycles
    against siblings at 7-9, and resume does not reproduce an uninterrupted run — so the arms of a
    live campaign stopped measuring the same thing, decided by which box consolidation drained next.
    """

    def _dispatcher(self, tmp_path, run=None):
        run = run or _RecordingRun()
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)

    def _box(self, conn, reg, inst_id=1, source="vast"):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, 555, f"runq_{inst_id}", reg.now_iso(), "live", 0.05, "h", 22, 4,
             reg.now_iso(), source))
        conn.commit()

    def test_a_draining_box_takes_no_new_work(self):
        """The whole race in one assertion: the box that was just drained must not be a placement
        candidate, or the tasks it evicted come straight back to it."""
        task = {"id": "N", "slots": 1, "est_minutes": 10, "priority": 50, "resource_hint": None}
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast"}
        assert disp._fits_now(task, inst, SHARED_SETTINGS) is True
        assert disp._fits_now(task, {**inst, "drain_held": True}, SHARED_SETTINGS) is False

    def test_an_emptied_drained_box_is_reclaimed_against_a_feasible_queue(self):
        """`feasible_task_waiting` holds a box alive for as long as ANY queued task fits — and after
        a drain the queue is FULL of tasks that fit, because we just put them there. That is the
        mechanism behind 79-of-111 drains ending with the box still live."""
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast",
                "drain_held": True}
        queued = [{"id": "Q", "slots": 1, "est_minutes": 10, "priority": 50}]
        assert disp.should_teardown(inst, queued, time.time(), SHARED_SETTINGS) == (True, "drained")
        # ...but never while the drain is still in flight — occupants are checked first.
        busy = {**inst, "occupants": [{"id": "X", "slots": 1, "state": "preempting",
                                       "est_minutes": 10, "running_minutes_ago": 1}]}
        assert disp.should_teardown(busy, queued, time.time(), SHARED_SETTINGS)[0] is False

    def test_a_draining_box_is_never_a_relocation_target(self):
        """Consolidation moves work onto surviving boxes. A box we are emptying is not surviving —
        relocating onto it would undo its own drain and strand both boxes."""
        held = [
            {"id": -1, "state": "live", "dph_usd": 0.0, "source": "owned", "slots_total": 32,
             "vram_total_gb": 64.0, "vram_used_gb": 0.0, "occupants": [], "drain_held": True},
            {"id": 2, "state": "live", "dph_usd": 0.06, "source": "vast", "slots_total": 8,
             "vram_total_gb": 16.0, "vram_used_gb": 0.0,
             "occupants": [{"id": "t0", "slots": 1, "state": "running",
                            "est_minutes": 480, "running_minutes_ago": 10}]},
        ]
        assert disp.consolidation_drains(held, _mech_settings(), 0) == [], (
            "relocated onto a box that is itself draining")
        # sanity: identical fleet with the hold lifted DOES drain — else this passes for free
        free_target = [{**held[0], "drain_held": False}, held[1]]
        assert disp.consolidation_drains(free_target, _mech_settings(), 0) != []

    def test_a_draining_box_is_not_drained_again(self):
        """A second drain on a box already draining just re-preempts whichever occupants have not
        finished checkpointing yet — pure disruption, zero additional reclaim."""
        boxes = [
            {"id": -1, "state": "live", "dph_usd": 0.0, "source": "owned", "slots_total": 32,
             "vram_total_gb": 64.0, "vram_used_gb": 0.0, "occupants": []},
            {"id": 2, "state": "live", "dph_usd": 0.06, "source": "vast", "slots_total": 8,
             "vram_total_gb": 16.0, "vram_used_gb": 0.0, "drain_held": True,
             "occupants": [{"id": "t0", "slots": 1, "state": "running",
                            "est_minutes": 480, "running_minutes_ago": 10}]},
        ]
        assert disp.consolidation_drains(boxes, _mech_settings(), 0) == []
        lifted = [boxes[0], {**boxes[1], "drain_held": False}]
        assert disp.consolidation_drains(lifted, _mech_settings(), 0) != []

    def test_the_hold_expires_so_a_box_that_cannot_empty_returns_to_service(self, tmp_path):
        """Time-bounded on purpose. A box whose occupants never checkpoint would otherwise sit held
        AND billing forever — the exact idle-bill failure every reaper in this file exists to stop.
        40 min is MEASURED: over the 31 clean drains (never repacked, ended in teardown),
        drain->teardown is median 13.2 / p90 33.5 / p95 42.2 min."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        import datetime
        d._set_drain_hold({"id": 1})
        assert d._drain_held({"id": 1}) is True
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(minutes=d.settings["consolidate_drain_hold_min"] + 1)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute("UPDATE settings SET value=? WHERE key='drain_hold_i1'",
                       (json.dumps({"at": stale}),))
        d.conn.commit()
        assert d._drain_held({"id": 1}) is False, "a box that cannot empty must return to service"
        assert d.conn.execute("SELECT 1 FROM settings WHERE key='drain_hold_i1'").fetchone() is None

    def test_the_hold_survives_a_dispatcher_restart(self, tmp_path):
        """Invariant 1. The supervisor respawns the daemon; an in-memory hold would hand the
        draining box a fresh backlog on every respawn, which is the race again with extra steps."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        d._set_drain_hold({"id": 1})
        d.conn.commit()
        d2 = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                             vastai_run=_RecordingRun())
        assert d2._drain_held({"id": 1}) is True
        assert any(i["id"] == 1 and i["drain_held"] for i in d2._instances_view())

    def test_the_hold_is_shorter_than_the_cooldown_so_the_guards_compose(self):
        """21h releases a stuck box at 40 min; 21g still refuses to re-drain it until 60. If the
        hold outlived the cooldown a box could be re-drained while still held — drained, held,
        drained again, never placed on — which is a strictly worse race than the one being fixed."""
        assert (disp.DEFAULT_SETTINGS["consolidate_drain_hold_min"]
                < disp.DEFAULT_SETTINGS["consolidate_cooldown_min"])

    def test_consolidate_actually_arms_the_hold(self, tmp_path, monkeypatch):
        """THE WIRING, and the one mutation that escaped a first pass of this suite: every guard
        above can be perfectly tested while the single line in `_consolidate` that ARMS them is
        deleted, leaving 21h dead code and all tests green. A guard nothing switches on is not a
        guard. (Third time today mutation testing caught a test passing for the wrong reason — the
        others were a clock that never advanced and a fixture blocked by an earlier gate.)"""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        reg.insert_task(d.conn, id="R1", created_at=reg.now_iso(), created_by="t", grp="g",
                        name="R1", entrypoint="smoke", args_json="[]", config_json="{}",
                        config_hash="R1", arm_hash="R1", git_sha="d", slots=1, est_minutes=60,
                        priority=50, max_retries=4, state="running", instance_id=1)
        d.conn.commit()
        monkeypatch.setattr(disp, "endpoint_for", lambda inst, tracker, run: ("h", 22))
        monkeypatch.setattr(d, "_evict_task_graceful", lambda *a, **k: None)
        monkeypatch.setattr(disp, "consolidation_drains",
                            lambda *a, **k: [{"instance_id": 1, "task_ids": ["R1"],
                                              "targets": [-1], "dph_reclaimed": 0.05}])
        assert d._drain_held({"id": 1}) is False
        d._consolidate()
        assert d._drain_held({"id": 1}) is True, (
            "consolidation drained a box without holding it out of placement — the next "
            "_place_queue ships the evicted tasks straight back and the drain is pure loss")

    # ---- invariant 21i: the hold may never CAUSE a rental ----

    def _held_fleet(self, tmp_path):
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        d._set_drain_hold({"id": 1})
        d.conn.commit()
        return d

    def test_a_drain_is_abandoned_rather_than_funded_with_a_new_box(self, tmp_path, monkeypatch):
        """MEASURED 2026-07-29, the 2nd drain under 21h: box 40000015 drained 17:51:39 at
        $0.0496/hr and a new box was rented **58 seconds later at $0.0523/hr** — DEARER than the one
        being reclaimed — taking 4 of the 5 preempted tasks. Without the hold those tasks would have
        gone back to the source and nothing would have been rented. So 21h had traded "repack the
        same box" for "rent another one", which is strictly worse.

        A `rent` decision is proof the drain's premise (its load fits on surviving capacity) was
        false, so the drain must yield."""
        d = self._held_fleet(tmp_path)
        task = {"id": "T", "slots": 1, "est_minutes": 10, "priority": 50, "resource_hint": None}
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast",
                "drain_held": True}
        monkeypatch.setattr(d, "_offers", lambda: [])
        # The REAL `place()` runs here: with the hold lifted the box is an ordinary pack candidate,
        # so this asserts the whole path, not a stub of it.
        assert disp.place(task, [inst], [], [], SHARED_SETTINGS, time.time()).action != "pack", (
            "precondition: while HELD the box must not be packable, else this proves nothing")
        got = d._abandon_drain_rather_than_rent(task, [inst], [], SHARED_SETTINGS)
        assert got is not None and got.action == "pack", "kept the hold and rented anyway"
        assert d._drain_held({"id": 1}) is False, "hold was not lifted"
        assert inst["drain_held"] is False, "the in-memory view still hides the box from placement"

    def test_place_queue_actually_consults_the_abandon_path_before_renting(self, tmp_path,
                                                                            monkeypatch):
        """THE WIRING — and the SECOND time in one session that a fully-tested guard could be
        orphaned with every test green (the first was `_consolidate` not arming the hold at all).
        The helper below can be perfect while `_place_queue` never calls it, which leaves 21i inert
        and the fleet renting boxes to replace ones it is destroying. Mutate the CALL SITE, always.
        """
        d = self._held_fleet(tmp_path)
        reg = disp.registry_db
        reg.insert_task(d.conn, id="Q1", created_at=reg.now_iso(), created_by="t", grp="g",
                        name="Q1", entrypoint="smoke", args_json="[]", config_json="{}",
                        config_hash="Q1", arm_hash="Q1", git_sha="d", slots=1, est_minutes=10,
                        priority=50, max_retries=4, state="queued")
        d.conn.commit()
        held = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast",
                "drain_held": True}
        monkeypatch.setattr(d, "_instances_view", lambda: [held])
        monkeypatch.setattr(d, "_offers", lambda: [])
        monkeypatch.setattr(d, "_apply_placement", lambda *a, **k: None)
        monkeypatch.setattr(disp, "account_balance", lambda *a, **k: 100.0)
        monkeypatch.setattr(disp, "vastai_json", lambda *a, **k: {})
        monkeypatch.setattr(disp, "place",
                            lambda *a, **k: disp.Placement("rent", None, None, "rent"))
        d._place_queue()
        assert d._drain_held({"id": 1}) is False, (
            "_place_queue decided to RENT without ever consulting the drained box that could have "
            "taken the work — 21i is wired up wrong and the fleet buys a box to replace one it is "
            "in the middle of destroying")

    def test_a_drain_is_NOT_abandoned_when_the_box_could_not_take_the_task_anyway(
            self, tmp_path, monkeypatch):
        """The hold only yields when lifting it actually AVOIDS the rental. A box that cannot fit
        the task is not the reason we are renting, so destroying its drain would be pure loss —
        we would pay the preempts, keep the box, AND still rent."""
        d = self._held_fleet(tmp_path)
        task = {"id": "T", "slots": 8, "est_minutes": 10, "priority": 50, "resource_hint": None}
        inst = {"id": 1, "state": "live", "slots_total": 4, "minutes_to_hard_cap": 600,
                "occupants": [], "idle_minutes": 0.0, "dph_usd": 0.05, "source": "vast",
                "drain_held": True}
        monkeypatch.setattr(d, "_offers", lambda: [])
        assert d._abandon_drain_rather_than_rent(task, [inst], [], SHARED_SETTINGS) is None
        assert d._drain_held({"id": 1}) is True, "lifted a hold that could not have helped"

    def test_the_cheapest_held_box_is_the_one_kept_alive(self, tmp_path, monkeypatch):
        """With several boxes draining, yielding the CHEAPEST one keeps the least costly capacity
        and leaves the dearer drains free to complete."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        self._box(d.conn, reg, inst_id=1)
        self._box(d.conn, reg, inst_id=2)
        d._set_drain_hold({"id": 1}); d._set_drain_hold({"id": 2}); d.conn.commit()
        task = {"id": "T", "slots": 1, "est_minutes": 10, "priority": 50, "resource_hint": None}
        base = {"state": "live", "slots_total": 4, "minutes_to_hard_cap": 600, "occupants": [],
                "idle_minutes": 0.0, "source": "vast", "drain_held": True}
        dear = {**base, "id": 1, "dph_usd": 0.20}
        cheap = {**base, "id": 2, "dph_usd": 0.02}
        monkeypatch.setattr(d, "_offers", lambda: [])
        monkeypatch.setattr(disp, "place", lambda *a, **k: disp.Placement("pack", 2, None, "p"))
        d._abandon_drain_rather_than_rent(task, [dear, cheap], [], SHARED_SETTINGS)
        assert d._drain_held({"id": 2}) is False, "did not keep the cheapest box"
        assert d._drain_held({"id": 1}) is True, "needlessly abandoned the expensive box's drain"




class TestLongTaskNamesCannotWedgeTheFleet:
    """Invariant 9e. `runq sweep` builds an arm name by concatenating every axis value, so a wide
    sweep produces names past the 255-byte single-component limit ext4/xfs enforce.

    That is not cosmetic. `_result_dir` is called from `_ingest_and_complete`, so the `OSError`
    propagated out of `poll_once` and killed the DAEMON; the self-heal supervisor respawned it
    straight back into the same task. Live 2026-07-29: 2 crash-respawns at 15:07-15:08 on a 264-char
    `m50_nav_module` arm, every task in the fleet stalled, and it stopped only because a human
    cancelled the task. ONE over-long name can wedge the whole fleet indefinitely.
    """

    def test_a_name_within_the_limit_is_untouched(self):
        """No existing result directory may move — the fix must be a no-op for every name on disk."""
        assert disp._fs_safe_component("armd2_ctrl_batch_size32") == "armd2_ctrl_batch_size32"

    def test_an_over_long_name_is_shortened_below_the_filesystem_limit(self, tmp_path):
        name = "arm" + "_edge_vision_to_interior037" * 20          # 543 chars
        safe = disp._fs_safe_component(name)
        assert len(safe.encode()) <= 255
        (tmp_path / safe).mkdir()                                   # the real assertion: it works

    def test_two_arms_differing_only_in_their_TAIL_do_not_collide(self):
        """A plain truncation would map every arm of one sweep onto ONE directory and silently
        overwrite their results — sweep names share a long prefix and differ at the end."""
        stem = "arm_" + "x" * 300
        assert disp._fs_safe_component(stem + "_seed1") != disp._fs_safe_component(stem + "_seed2")

    def test_the_mapping_is_stable_across_calls(self):
        """`_result_dir` is called from many places across many polls and restarts; an unstable name
        would scatter one task's results across directories."""
        name = "arm_" + "y" * 400
        assert disp._fs_safe_component(name) == disp._fs_safe_component(name)

    def test_ingest_survives_a_name_no_directory_can_hold(self, tmp_path, monkeypatch):
        """End-to-end: the crash was `_result_dir` raising INSIDE the poll loop, so the regression
        test has to be that `_result_dir` itself returns rather than raising."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        out = d._result_dir({"grp": "m50_nav_module", "name": "armnav_module_" + "z" * 250})
        assert out.is_dir()


class TestAtMostOneBoxIsKeptWarmAndIdle:
    """Invariant 11a — owner directive 2026-07-30: "no more than 1 box kept warm and idle".

    `feasible_task_waiting` keeps an EMPTY box alive whenever ANY queued task fits, and it is
    evaluated PER BOX — so one 1-slot task retained every idle box in the fleet and
    `idle_timeout_min` never fired. Measured live: four empty boxes idle 24/42/48/80 minutes against
    a 10-minute timeout, costing $0.2162/hr for zero work.
    """

    QUEUED = [{"id": "q1", "slots": 1, "est_minutes": 10, "priority": 50}]

    def _empty(self, iid, dph=0.05, slots=6, idle=99):
        return {"id": iid, "state": "live", "slots_total": slots, "occupants": [],
                "idle_minutes": idle, "minutes_to_hard_cap": 3000, "dph_usd": dph,
                "source": "vast"}

    def test_an_undesignated_empty_box_is_torn_down_even_though_a_task_fits(self):
        inst = {**self._empty(1), "warm_idle_keep": False}
        assert disp.should_teardown(inst, self.QUEUED, 0, SHARED_SETTINGS) == (
            True, "idle_over_warm_cap"), (
            "an empty box over the warm cap must fall through to the idle timer; holding it is how "
            "four boxes idled 24-80 min against a 10-min timeout")

    def test_the_designated_box_is_still_held_for_pending_work(self):
        inst = {**self._empty(1), "warm_idle_keep": True}
        assert disp.should_teardown(inst, self.QUEUED, 0, SHARED_SETTINGS) == (
            False, "feasible_task_waiting"), "the ONE warm box must still absorb incoming work"

    def test_a_box_with_occupants_is_never_affected_by_the_cap(self):
        busy = {**self._empty(1), "warm_idle_keep": False,
                "occupants": [{"id": "t", "slots": 1, "state": "running"}]}
        assert disp.should_teardown(busy, self.QUEUED, 0, SHARED_SETTINGS)[0] is False, (
            "the cap bounds EMPTY capacity only — it must never destroy a box doing work")

    def test_an_undesignated_box_under_the_idle_timer_still_waits(self):
        """The cap removes the OVERRIDE, it does not skip the timer."""
        inst = {**self._empty(1, idle=3), "warm_idle_keep": False}
        assert disp.should_teardown(inst, self.QUEUED, 0, SHARED_SETTINGS) == (False, "idle_not_yet")

    # ---- the call site: this is what actually caps the fleet ----

    def _fleet(self, tmp_path, dphs):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        view = [self._empty(i + 1, dph=dph) for i, dph in enumerate(dphs)]
        d._instances_view = lambda: [dict(v) for v in view]
        return d

    def test_teardown_idle_ACTUALLY_KEEPS_ONLY_ONE_and_it_is_the_CHEAPEST(self, tmp_path,
                                                                          monkeypatch):
        """Pins the wiring, not just the rule. A pure predicate nobody feeds `warm_idle_keep` to
        would leave every box held exactly as before."""
        d = self._fleet(tmp_path, [0.09, 0.04, 0.07, 0.05])   # cheapest is id=2 @ $0.04
        monkeypatch.setattr(d, "_task_view", lambda t: t)
        monkeypatch.setattr(disp.registry_db, "list_tasks",
                            lambda *a, **k: [dict(self.QUEUED[0])])
        killed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: killed.append((inst["id"], reason)))
        d._teardown_idle()
        assert len(killed) == 3, f"expected 3 of 4 empty boxes culled, got {killed}"
        assert 2 not in [k for k, _ in killed], (
            "the CHEAPEST empty box should be the one kept warm")
        assert all(r == "idle_over_warm_cap" for _, r in killed), killed

    def test_the_warm_budget_is_not_spent_on_an_OWNED_box(self, tmp_path, monkeypatch):
        """An owned box costs nothing idle and is never torn down anyway — letting it absorb the
        single warm slot would cull a PAID box we are actually paying to keep."""
        d = self._fleet(tmp_path, [0.06, 0.08])
        view = d._instances_view()
        view.append({**self._empty(-1, dph=0.0), "source": "owned"})
        d._instances_view = lambda: [dict(v) for v in view]
        monkeypatch.setattr(d, "_task_view", lambda t: t)
        monkeypatch.setattr(disp.registry_db, "list_tasks",
                            lambda *a, **k: [dict(self.QUEUED[0])])
        killed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: killed.append((inst["id"], reason)))
        d._teardown_idle()
        assert [k for k, _ in killed] == [2], (
            f"the $0.06 paid box should have been kept warm and only the $0.08 culled: {killed}")

    def test_warm_idle_max_is_the_knob(self, tmp_path, monkeypatch):
        d = self._fleet(tmp_path, [0.09, 0.04, 0.07, 0.05])
        d.settings["warm_idle_max"] = 2
        # 11a's SECOND bound (`max_warm_free_slots`) would otherwise cap these two 6-slot boxes at
        # 10 free slots and keep only one — see TestWarmSlotCapIsBoundedInSlotsToo. Raised here so
        # this test measures the box-count knob it is named for, not the slot ceiling.
        d.settings["max_warm_free_slots"] = 12
        monkeypatch.setattr(d, "_task_view", lambda t: t)
        monkeypatch.setattr(disp.registry_db, "list_tasks",
                            lambda *a, **k: [dict(self.QUEUED[0])])
        killed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: killed.append((inst["id"], reason)))
        d._teardown_idle()
        assert len(killed) == 2, f"warm_idle_max=2 should keep two: {killed}"

    def test_the_default_box_count_is_TWO(self):
        """Was 1 — owner directive 2026-07-30, "that was just a hack to close the gap". The gap is
        now held by `max_warm_free_slots`, which counts free slots FLEET-WIDE, so the box count no
        longer has to stand in for a slot bound it was never expressing."""
        assert disp.DEFAULT_SETTINGS["warm_idle_max"] == 2


class TestTheCompletionRetryMustNotRepeatTheBulkPull:
    """Invariant 9g — a finished run must not be discarded because a sibling file is too big.

    Live 2026-07-30, `m49_dreamfix/fleetcheck`: the run COMPLETED (11m23s, DONE marker, final summary
    in run.log) and `results.json` was verified present ON THE BOX at 6,082 bytes by ssh — next to
    `ckpt_substrate_seed0.pt` at 235,420,985 bytes. The out/-rooted ingest is ONE rsync whose includes
    carry `ckpt_*.pt`, so the 235 MB file blows the 60s budget and NOTHING lands, including the 6 KB
    artifact that proves success. Comparable runs with 13 MB substrate checkpoints pulled fine. The
    task went `task_failed` — terminal, never auto-requeues — and the alert blamed the owner's
    `completion_artifact` declaration.
    """

    def _done_box(self, tmp_path, monkeypatch):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="CD1")
        reg.transition(d.conn, "CD1", "shipped", "ship", "shipped")
        reg.transition(d.conn, "CD1", "running", "start", "started")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        out = tmp_path / "g" / "CD1"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        monkeypatch.setattr(d, "_alert", lambda m: None)
        return d, out

    def test_the_retry_is_NARROW_and_recovers_a_run_the_bulk_pull_could_not(self, tmp_path,
                                                                            monkeypatch):
        d, out = self._done_box(tmp_path, monkeypatch)
        entry = disp.entrypoints.resolve(
            dict(d.conn.execute("SELECT * FROM tasks WHERE id='CD1'").fetchone()))
        calls = []

        def fake_pull(host, port, remote, local, includes, **kw):
            calls.append(tuple(includes))
            # the bulk pull TIMES OUT on the 235 MB sibling and lands nothing
            if any("ckpt_" in i for i in includes):
                return False
            # a narrow pull of just the small artifact succeeds
            if entry.completion_artifact in includes:
                (out / entry.completion_artifact).write_text("{}")
                return True
            return False

        monkeypatch.setattr(disp, "rsync_pull", fake_pull)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_done(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CD1'").fetchone()),
                          inst, "example.com", 2222)

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='CD1'").fetchone())
        assert row["state"] == "done", (
            f"a COMPLETED run was discarded as {row['state']} because a sibling checkpoint was too "
            "big to move inside one timeout")
        assert len(calls) == 2, calls
        assert any("ckpt_" in i for i in calls[0]), "first pull should still be the bulk ingest"
        assert not any("ckpt_" in i for i in calls[1]), (
            f"the RETRY repeated the bulk includes {calls[1]} — that re-attempts the transfer that "
            "just failed, so when the cause is SIZE the retry can never succeed")
        assert entry.completion_artifact in calls[1]

    def test_a_genuinely_missing_artifact_still_fails(self, tmp_path, monkeypatch):
        """The narrow retry must not INVENT a completion: no artifact anywhere -> task_failed."""
        d, out = self._done_box(tmp_path, monkeypatch)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)   # pulls nothing
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_done(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CD1'").fetchone()),
                          inst, "example.com", 2222)
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='CD1'").fetchone())
        assert row["state"] == "task_failed", row["state"]


class TestABoxDirIsNotDeletedWhileItsEvidenceIsStillOnIt:
    """Invariant 29a precondition — `done` may be declared on the completion artifact alone, but the
    box's copy may only be destroyed once EVERY evidence file is home.

    Live 2026-08-13, `m55e_bid_scout2`: three arms of one sweep, each writing nine ~42 MB per-stage
    substrate checkpoints. `ctrl` landed 9/9; `bid` landed 2 of 9 and `moe` 4 of 9, each with a
    `.rsync-partial` remnant and a `ckpt_latest.pt` truncated to 10,775 bytes against a 40 MB
    predecessor. All three read `done` / "completion artifact verified" and their box dirs were
    already `rm -rf`d, so the campaign's pre-registered per-stage collapse gate was uncomputable AND
    unrecoverable. The 9g comment asserted "the bulk artifacts are not lost — the next ingest pass
    keeps pulling them"; the same branch deleted the source, so there was no next pass.
    """

    def _box(self, tmp_path, monkeypatch, listing):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="EV1")
        reg.transition(d.conn, "EV1", "shipped", "ship", "shipped")
        reg.transition(d.conn, "EV1", "running", "start", "started")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        out = tmp_path / "g" / "EV1"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        monkeypatch.setattr(d, "_alert", lambda m: None)
        rm = []

        def fake_ssh(host, port, cmd, run=None):
            if cmd.startswith("find "):
                return subprocess.CompletedProcess([], 0, listing, "")
            rm.append(cmd)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(disp, "ssh_run", fake_ssh)
        entry = disp.entrypoints.resolve(
            dict(d.conn.execute("SELECT * FROM tasks WHERE id='EV1'").fetchone()))

        def fake_pull(host, port, remote, local, includes, **kw):
            (out / entry.completion_artifact).write_text("{}")
            return True

        monkeypatch.setattr(disp, "rsync_pull", fake_pull)
        return d, out, rm, entry

    def _complete(self, d):
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_done(dict(d.conn.execute("SELECT * FROM tasks WHERE id='EV1'").fetchone()),
                          inst, "example.com", 2222)

    def test_the_m55e_truncation_does_NOT_delete_the_box_copy(self, tmp_path, monkeypatch):
        """The exact shape that lost the campaign: 9 checkpoints on the box, 2 pulled."""
        listing = "".join(f"44000000 ckpt_substrate_seed0_stage0{i}.pt\n" for i in range(9))
        listing += "6082 results.json\n"
        d, out, rm, entry = self._box(tmp_path, monkeypatch, listing)
        for i in range(2):                       # only 2 of the 9 landed
            (out / f"ckpt_substrate_seed0_stage0{i}.pt").write_bytes(b"x" * 44000000)

        self._complete(d)

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='EV1'").fetchone())
        assert row["state"] == "done", (
            "the run FINISHED — a partial artifact pull must not un-complete it")
        assert not any("rm -rf" in c for c in rm), (
            f"the box dir was deleted with 7 of 9 checkpoints still only on the box: {rm}. This is "
            "the m55e_bid_scout2 data loss — `done` proves the run finished, not that its bytes are "
            "home.")

    def test_a_TRUNCATED_file_counts_as_absent(self, tmp_path, monkeypatch):
        """`ckpt_latest.pt` came home at 10,775 bytes against 40 MB — present is not sufficient."""
        d, out, rm, entry = self._box(tmp_path, monkeypatch,
                                      "40160150 ckpt_latest.pt\n6082 results.json\n")
        (out / "ckpt_latest.pt").write_bytes(b"x" * 10775)

        self._complete(d)

        assert not any("rm -rf" in c for c in rm), (
            f"a file present at 10,775 of 40,160,150 bytes was treated as home: {rm}")

    def test_a_COMPLETE_pull_still_reclaims_the_box_dir(self, tmp_path, monkeypatch):
        """The fix must not strand disk — the incident this repo already had is a FULL box."""
        d, out, rm, entry = self._box(tmp_path, monkeypatch,
                                      "44000000 ckpt_substrate_seed0.pt\n6082 results.json\n")
        (out / "ckpt_substrate_seed0.pt").write_bytes(b"x" * 44000000)
        (out / "results.json").write_text("x" * 6082)

        self._complete(d)

        assert any("rm -rf" in c and "EV1" in c for c in rm), (
            f"everything was home and the box dir was NOT reclaimed: {rm}. Deferring every cleanup "
            "refills the box — tower hit 98G/98G and Errno 28 killed a campaign.")

    def test_an_unreachable_box_defers_rather_than_deleting(self, tmp_path, monkeypatch):
        d, out, rm, entry = self._box(tmp_path, monkeypatch, "")

        def failing_ssh(host, port, cmd, run=None):
            if cmd.startswith("find "):
                return subprocess.CompletedProcess([], 255, "", "ssh: connect failed")
            rm.append(cmd)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(disp, "ssh_run", failing_ssh)
        self._complete(d)

        assert not any("rm -rf" in c for c in rm), (
            f"an unverifiable box was cleaned anyway: {rm}. A false negative costs a directory until "
            "the 12h sweep; a false positive costs an experiment.")


class TestTheTrainerWrittenCrashRecordReachesTheFailureReason:
    """Invariant 9d(g) — prefer the trainer's own `out/crash.json` over the run.log tail.

    Live 2026-07-30: `m50_stage_reset/armnostagereset…` had BOTH `run.log` (9190 bytes) and
    `crash.json` (8635 bytes) in its result dir, and its `task_failed` reason was still the bare
    string "worker exit 1" — the tail read empty at reason-construction time even though the log had
    arrived. Two arms of that campaign failed identically within 60 seconds, each recording nothing,
    while the traceback (an unexpected-keyword error listing every valid param — a knob-reachability
    bug) sat unread on disk. crash.json also comes home on the FIRST, `out/`-rooted pull, which is
    the reliable one; run.log needs the second one-level-up pull, which is the one that missed.
    """

    TB = ("Traceback (most recent call last):\n"
          '  File "train_m49_curriculum_ab.py", line 998, in _run_ab\n'
          "TypeError: evaluate_modulegraph_config() got an unexpected keyword argument 'stage_reset'\n")

    def _failed_box(self, tmp_path, monkeypatch, *, crash=None, runlog=None):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="CJ1")
        reg.transition(d.conn, "CJ1", "shipped", "ship", "shipped")
        reg.transition(d.conn, "CJ1", "running", "start", "started")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        out = tmp_path / "g" / "CJ1"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        if crash is not None:
            (out / "crash.json").write_text(crash)
        if runlog is not None:
            (out / "run.log").write_text(runlog)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
        self.alerts = []
        monkeypatch.setattr(d, "_alert", lambda m: self.alerts.append(m))
        return d, out

    def _reason(self, d):
        return dict(d.conn.execute("SELECT detail FROM events WHERE task_id='CJ1' AND "
                                   "event='task_failed' ORDER BY seq DESC LIMIT 1").fetchone())["detail"]

    def test_crash_json_traceback_reaches_the_reason_when_run_log_is_MISSING(self, tmp_path, monkeypatch):
        """The observed case: the tail was empty, so crash.json is the only source that can explain it."""
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=json.dumps({"traceback": self.TB}))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "unexpected keyword argument 'stage_reset'" in reason, (
            f"the trainer wrote its own traceback and the reason still explains nothing: {reason!r}")
        assert self.alerts and "stage_reset" in self.alerts[0], (
            "a campaign-wide code bug must be ONE loud alarm, not N silent red rows")

    def test_the_exception_LINE_survives_the_bound_because_the_traceback_is_TAILED(
            self, tmp_path, monkeypatch):
        """A traceback's LAST line names the bug; the head is only the call chain. A head-clipped
        bound would drop exactly the useful part on a long traceback (the live one was 8635 bytes)."""
        long_tb = ("filler line that is here only to overflow the byte bound\n" * 400) + self.TB
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=json.dumps({"traceback": long_tb}))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "unexpected keyword argument 'stage_reset'" in reason, (
            "the exception line was clipped away — the bound must keep the TAIL")
        assert len(reason) < 6000, f"reason unbounded ({len(reason)} chars) — it goes into the DB"

    def test_both_sources_appear_and_crash_json_comes_FIRST(self, tmp_path, monkeypatch):
        d, _ = self._failed_box(tmp_path, monkeypatch,
                                 crash=json.dumps({"traceback": self.TB}),
                                 runlog="some progress line\nanother line\n")
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "crash.json traceback" in reason and "run.log tail" in reason
        assert reason.index("crash.json traceback") < reason.index("run.log tail"), (
            "the deliberate structured record must lead; the log tail is the fallback")

    def test_a_HUGE_single_line_exception_message_still_yields_the_exception_NAME(
            self, tmp_path, monkeypatch):
        """The live regression that the first version of 9d(g) got wrong.

        `TypeError: … unexpected keyword argument 'stage_reset'` is followed by a `dict_keys([…])`
        dump of every valid parameter — ~8 KB on ONE line. A byte-tail (`tb[-max_bytes:]`) lands
        mid-dump and keeps the USELESS end while dropping the exception name and the offending
        argument. Measured on `m50_navdefault/ctl_s1184f5`: the stored tail was 4120 chars over 2
        lines (longest 4095) and contained no exception name at all."""
        giant = ("TypeError: evaluate_modulegraph_config() got an unexpected keyword argument "
                 "'stage_reset'. Valid: dict_keys([" + ", ".join(f"'p{i}'" for i in range(1200)) + "])")
        tb = ("Traceback (most recent call last):\n"
              '  File "train_m49_curriculum_ab.py", line 998, in _run_ab\n' + giant + "\n")
        assert len(giant) > 8000, "fixture must reproduce the real >8KB single line"
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=json.dumps({"traceback": tb}))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "TypeError" in reason, (
            "the exception NAME was clipped away — a byte-tail keeps the parameter dump instead")
        assert "unexpected keyword argument 'stage_reset'" in reason, (
            "the offending ARGUMENT was clipped away, which is the one thing the owner needs")
        assert "more chars" in reason, "the dump should be elided with a visible marker, not silently"
        assert len(reason) < 6000, f"reason unbounded ({len(reason)} chars)"

    def test_when_the_byte_bound_BITES_lines_are_dropped_from_the_FRONT(self, tmp_path, monkeypatch):
        """Pins the pop DIRECTION. A mutant that popped from the END passed every other test here,
        because no other fixture exceeds max_bytes once each line is clipped — so the trimming loop
        never ran. The exception lives on the LAST line, so trimming must sacrifice the OLDEST frames.
        """
        frames = [f'  File "mod{i}.py", line {i}, in fn{i}' + "x" * 150 for i in range(60)]
        tb = "Traceback (most recent call last):\n" + "\n".join(frames) + "\nValueError: THE-REAL-CAUSE\n"
        clipped_total = sum(min(len(l), 300) + 1 for l in tb.splitlines()[-40:])
        assert clipped_total > 4096, f"fixture must force the trimming loop (got {clipped_total})"
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=json.dumps({"traceback": tb}))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "ValueError: THE-REAL-CAUSE" in reason, (
            "the byte bound sacrificed the EXCEPTION instead of the oldest frames — trimming must "
            "pop from the front")
        assert "mod0.py" not in reason, "the oldest frame should have been the one dropped"

    def test_frame_lines_survive_whole_so_the_call_site_is_still_readable(self, tmp_path, monkeypatch):
        tb = ("Traceback (most recent call last):\n"
              '  File "/root/spool/active/X/repo/src/native/training/m49_curriculum_ab.py", line 998, '
              "in _run_ab\n"
              "ValueError: boom\n")
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=json.dumps({"traceback": tb}))
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        reason = self._reason(d)
        assert "m49_curriculum_ab.py" in reason and "_run_ab" in reason, (
            "a short frame line must not be clipped — it names WHERE the bug is")
        assert "ValueError: boom" in reason

    @pytest.mark.parametrize("bad", ['not json at all', '[]', '{}', '{"traceback": ""}',
                                      '{"traceback": 42}'])
    def test_malformed_crash_json_never_blocks_the_terminal_transition(self, bad, tmp_path,
                                                                       monkeypatch):
        """A task stuck non-terminal is far worse than a reason without a traceback."""
        d, _ = self._failed_box(tmp_path, monkeypatch, crash=bad)
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._complete_failed(dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone()),
                            inst, "example.com", 2222, "FAILED_1")
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='CJ1'").fetchone())
        assert row["state"] == "task_failed", f"malformed crash.json blocked the CAS ({row['state']})"
        assert "crash.json traceback" not in self._reason(d), (
            "an empty section falsely reads as 'we looked and the crash record said nothing'")


class TestTheDeadWorkerPathMustLeaveAForensicTrail:
    """Invariant 10c(f) — the FOURTH place this identical omission had to be fixed.

    9d gave `task_failed` (FAILED_<rc>) a forensic run.log pull, 18f gave `cancelled` one, 9e(f) gave
    `artifact_missing` one — and `_reap_dead_workers` still called `_destroy` immediately, taking the
    only copy of the log with it. So the entire record of every dead worker was the bare string
    "dead_worker: heartbeat stale", while the method's own docstring names three DIFFERENT causes
    (OOM-killed, box hiccup, uncaught exception) it cannot tell apart. Measured 2026-07-30: four boxes
    reaped in 56 minutes taking ~14 tasks, dozens more on prior days, none diagnosable.
    """

    def _dead_worker_box(self, tmp_path, monkeypatch, task_state="running"):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = _seed_instance_and_task(d.conn, task_id="DW1")
        if task_state != "claimed":
            reg.transition(d.conn, "DW1", "shipped", "ship", "shipped")
            if task_state == "running":
                reg.transition(d.conn, "DW1", "running", "start", "started")
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        # a HEARTBEAT that is provably stale
        hb = tmp_path / ".dispatcher" / "instance_1" / "HEARTBEAT"
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("x")
        old = time.time() - 99 * 60
        os.utime(hb, (old, old))
        # inv. 10c's two guards must both PASS so the reap is legitimate
        monkeypatch.setattr(d.tracker, "consecutive_fails", lambda i: 0)
        monkeypatch.setattr(d.tracker, "seconds_since_heartbeat_pull", lambda i: 1.0)
        return d

    def test_dead_worker_pulls_run_log_from_active_root_and_folds_it_into_the_reason(
            self, tmp_path, monkeypatch):
        d = self._dead_worker_box(tmp_path, monkeypatch)
        out = tmp_path / "g" / "DW1"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        roots = []

        def fake_pull(host, port, remote, local, includes, **kw):
            roots.append((remote, tuple(includes)))
            if "run.log" in includes:
                (out / "run.log").write_text("Traceback\nMemoryError: killed by OOM\n")
            return True

        monkeypatch.setattr(disp, "rsync_pull", fake_pull)
        alerts = []
        monkeypatch.setattr(d, "_alert", lambda m: alerts.append(m))
        destroyed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: destroyed.append(inst["id"]))

        d._reap_dead_workers()

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='DW1'").fetchone())
        # `_infra_fail` requeues at HALF a retry (inv. 10), so the resting state is `queued`, not
        # `infra_failed` — verified live 2026-07-30, where ~14 dead-worker casualties all recovered.
        # The forensics therefore have to ride on the `infra_failed` EVENT, which is what `runq show`
        # reads; a tail attached only to the task row would vanish on requeue.
        assert row["state"] == "queued", row["state"]
        assert row["retries_used"] == 0.5, row["retries_used"]
        ev = dict(d.conn.execute("SELECT detail FROM events WHERE task_id='DW1' "
                                 "AND event='infra_failed' ORDER BY seq DESC LIMIT 1").fetchone())
        assert "MemoryError: killed by OOM" in ev["detail"], (
            "the run.log tail did not reach the infra_failed reason — `runq show` still explains "
            "nothing, which is the whole defect 10c(f) closes")
        # THE STRUCTURAL TRAP: rooted at active/<id>/, NOT active/<id>/out/. An rsync --include only
        # matches under its own root, so an out/-rooted pull can never see run.log (a SIBLING of out/).
        assert any("run.log" in i and r.rstrip("/").endswith("DW1") for r, i in roots), (
            f"run.log was not pulled from active/<id>/ — re-rooting it silently restores the bug: {roots}")
        assert destroyed == [1], "the box must still be destroyed; forensics may not cancel the reap"
        assert alerts and "dead_worker" in alerts[0]

    def test_a_failing_forensic_pull_still_infra_fails_and_still_destroys(self, tmp_path, monkeypatch):
        """Best-effort means best-effort: a raising pull must not block the transitions or the reap."""
        d = self._dead_worker_box(tmp_path, monkeypatch)
        monkeypatch.setattr(d, "_result_dir", lambda t: tmp_path / "g" / "DW1")

        def boom(*a, **k):
            raise OSError("ssh exploded")

        monkeypatch.setattr(disp, "rsync_pull", boom)
        monkeypatch.setattr(d, "_alert", lambda m: None)
        destroyed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: destroyed.append(inst["id"]))

        d._reap_dead_workers()

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='DW1'").fetchone())
        assert row["state"] == "queued", (
            f"a failed forensic pull swallowed the reap itself ({row['state']}) — the box would "
            "idle-bill forever and the task would be stranded, both strictly worse than having no log")
        assert destroyed == [1]

    def test_a_claimed_task_that_never_shipped_yields_an_empty_tail_not_a_crash(
            self, tmp_path, monkeypatch):
        """A `claimed` task has no run.log on the box. That empty tail IS the signal (the worker died
        before starting work) — it must not raise, and must not fabricate a tail."""
        d = self._dead_worker_box(tmp_path, monkeypatch, task_state="claimed")
        out = tmp_path / "g" / "DW1"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(d, "_result_dir", lambda t: out)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)   # pulls nothing
        monkeypatch.setattr(d, "_alert", lambda m: None)
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: None)

        d._reap_dead_workers()

        ev = dict(d.conn.execute("SELECT detail FROM events WHERE task_id='DW1' "
                                 "AND event='infra_failed' ORDER BY seq DESC LIMIT 1").fetchone())
        assert "run.log tail" not in ev["detail"], (
            "no log existed, so no tail section may appear — an empty section reads as 'we looked and "
            "the run said nothing', which is a different and false claim")
        assert "dead_worker" in ev["detail"]

    def test_10c_guards_still_block_the_reap_so_forensics_never_run_on_a_healthy_box(
            self, tmp_path, monkeypatch):
        """10c(f) must not weaken 10c. An unconfirmed read still skips — the 2026-07-14/07-26
        incidents destroyed healthy, still-training boxes exactly here."""
        d = self._dead_worker_box(tmp_path, monkeypatch)
        monkeypatch.setattr(d.tracker, "consecutive_fails", lambda i: 3)   # cannot prove staleness
        pulls = []
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: pulls.append(a) or True)
        destroyed = []
        monkeypatch.setattr(d, "_destroy", lambda inst, reason: destroyed.append(inst["id"]))

        d._reap_dead_workers()

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='DW1'").fetchone())
        assert row["state"] == "running", f"a healthy box was reaped anyway ({row['state']})"
        assert destroyed == [] and pulls == []


class TestAFailedFinalPullMustNotDiscardAHeldCheckpoint:
    """Invariant 17f. The owner kept hearing about preempts hurting task owners even after 21g/21h/21i
    and 21j; this is the sharpest form of that harm — work DESTROYED, not merely delayed.

    `_complete_preempted` required `ok and ckpt.exists()`, so ONE transient rsync failure at preempt
    time discarded the copy the 5-minutely `_pull_checkpoints` had already fetched, and the task
    restarted from ZERO with a good checkpoint sitting on our own disk.

    MEASURED 2026-07-29: 27 of 313 preempts in 24h (8.4%) reported "no checkpoint pulled". Their
    median run length was 14.6 min and one had run **121 min** against a 5-min pull cadence — a
    never-pulled checkpoint cannot explain that. 22 of 23 had a checkpoint on disk.

    Dropping `ok` is safe rather than optimistic: `shared.infra.checkpoint.load_checkpoint` tries the
    head, falls back to `.prev` when the head is truncated (which `--inplace` can leave behind), and
    returns None only when both are unreadable. Worst case == the old behaviour.
    """

    def _d(self, tmp_path, pull_ok):
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="P1", instance_id=1)
        self._orig = disp.rsync_pull
        # HERMETIC: `_result_dir` is rooted at the module-level EXPERIMENTS_ROOT, i.e. the SHARED
        # experiments tree. Without this the tests write into it AND read whatever a previous run
        # left there — the "no checkpoint anywhere" case passed/failed depending on leftovers from
        # a sibling case. A test whose result depends on residue is a test passing for the wrong
        # reason, which is the whole thing this suite exists to prevent.
        self._orig_root = disp.EXPERIMENTS_ROOT
        disp.EXPERIMENTS_ROOT = tmp_path / "experiments"
        disp.rsync_pull = lambda *a, **k: pull_ok
        return d

    def _restore(self):
        disp.rsync_pull = self._orig
        disp.EXPERIMENTS_ROOT = self._orig_root

    def _run(self, tmp_path, pull_ok, head=True, prev=False):
        d = self._d(tmp_path, pull_ok)
        try:
            task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='P1'").fetchone())
            d.conn.execute("UPDATE tasks SET state='preempting' WHERE id='P1'")
            d.conn.commit()
            out = d._result_dir(task)
            if head:
                (out / "ckpt_latest.pt").write_bytes(b"x")
            if prev:
                (out / "ckpt_latest.pt.prev").write_bytes(b"y")
            d._complete_preempted(task, {"id": 1}, "h", 22)
            row = dict(d.conn.execute("SELECT state,resume_checkpoint FROM tasks WHERE id='P1'").fetchone())
            ev = dict(d.conn.execute(
                "SELECT detail FROM events WHERE task_id='P1' AND event='preempt_requeue' "
                "ORDER BY seq DESC LIMIT 1").fetchone())
            return row, ev["detail"]
        finally:
            self._restore()

    def test_a_failed_pull_still_carries_forward_the_checkpoint_we_hold(self, tmp_path):
        """THE FIX. Previously this dropped the checkpoint and restarted the run from scratch."""
        row, detail = self._run(tmp_path, pull_ok=False, head=True)
        assert row["resume_checkpoint"], (
            "discarded a checkpoint that was already on our disk — the run restarts from zero")
        assert "final pull" in detail and "FAILED" in detail, (
            "must say the pull failed, so a stale/truncated resume is diagnosable")

    def test_only_the_prev_spare_present_is_still_a_resume(self, tmp_path):
        """The loader skips a missing head and reads `.prev`, so a spare alone is recoverable."""
        row, _ = self._run(tmp_path, pull_ok=False, head=False, prev=True)
        assert row["resume_checkpoint"], "ignored the .prev spare that exists for exactly this case"

    def test_no_checkpoint_anywhere_still_reports_honestly(self, tmp_path):
        """With nothing on the box and nothing held, behaviour is unchanged — and the message now
        says WHICH of the two causes it was, the distinction the old single string hid."""
        row, detail = self._run(tmp_path, pull_ok=False, head=False, prev=False)
        assert not row["resume_checkpoint"]
        assert "none held locally" in detail

    def test_a_successful_pull_is_reported_as_clean(self, tmp_path):
        """The happy path must not start claiming the pull failed."""
        row, detail = self._run(tmp_path, pull_ok=True, head=True)
        assert row["resume_checkpoint"]
        assert detail == "preempted, checkpoint carried forward"

    def test_the_loader_this_relies_on_really_falls_back_to_prev(self, tmp_path):
        """MAP THE PATH: this fix is only safe because the CONSUMER salvages a corrupt head. The
        dispatcher's comment named a function (`harness._load_resume`) that does not exist, so verify
        the real one rather than trusting the comment."""
        sys.path.insert(0, str(ROOT / "src"))
        from shared.infra.checkpoint import load_checkpoint, save_atomic
        p = tmp_path / "ckpt_latest.pt"
        save_atomic(p, {"step": 1})
        save_atomic(p, {"step": 2})                    # rotates the first into .prev
        p.write_bytes(b"truncated garbage")            # simulate an --inplace failed pull
        got = load_checkpoint(p)
        assert got == {"step": 1}, "the .prev fallback this fix depends on is not working"


class TestTheGlobalPreemptSwitch:
    """Owner directive 2026-07-30: *"let's just disable preempts for now. The checkpoints are
    introducing noise and fragility and they don't seem to be buying us much today. Our jobs tend to
    cost <$1 each so just letting the existing ones finish seems like the right move."* — then
    *"disable all of them -> wire this as a global control flag"*.

    `preempt_enabled=False` must silence ALL THREE fleet-initiated preempt sources, because
    disabling them one at a time is how one quietly comes back:
      1. priority preemption  (inv. 4d/17)
      2. capacity scale-down  (inv. 20 `_reap_over_capacity`)
      3. consolidation drains (inv. 21)

    The measured basis: preempts were buying ~$0.011 each (24h: 81 drains, 313 preempts, upper-bound
    $3.51 saved) while 8.4% of them lost their checkpoint outright and every one costs its arm some
    comparability, since resume does not reproduce an uninterrupted run.
    """

    def _dispatcher(self, tmp_path, **settings):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        d.settings.update(settings)
        return d

    def test_the_default_is_OFF(self):
        """The directive is the shipped default, not a local tweak — a DB-only change would be undone
        by any fresh registry, and `_ensure_settings` only inserts keys it lacks."""
        assert disp.DEFAULT_SETTINGS["preempt_enabled"] is False

    def test_max_hourly_usd_migration_ACTUALLY_MOVES_a_seeded_registry(self, tmp_path):
        """Owner directive 2026-07-30: raise the hourly ceiling 1.00 -> 1.50.

        Asserting the tuple is in `_SETTING_MIGRATIONS` is NOT enough — that passes even if the
        migration is never applied. `max_hourly_usd` is the ONLY brake on fleet size (inv. 11), so a
        silently-unapplied bump would leave the fleet capped at the old value while every report says
        otherwise. This drives the real path: seed a registry the way July did, then re-open it the
        way a daemon restart does, and read what `place()` would actually be handed."""
        db = str(tmp_path / "runs.sqlite")
        d1 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())
        # simulate the July-seeded registry, which `_ensure_settings` would never overwrite
        d1.conn.execute("UPDATE settings SET value=? WHERE key=?", ("1.0", "max_hourly_usd"))
        d1.conn.commit()
        d2 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())   # a restart
        assert float(d2.settings["max_hourly_usd"]) == 1.50, (
            f"the migration did not reach the live registry ({d2.settings['max_hourly_usd']}) — the "
            "fleet would stay capped at the old ceiling while the code default claims otherwise")
        row = dict(d2.conn.execute(
            "SELECT value FROM settings WHERE key='max_hourly_usd'").fetchone())
        assert float(row["value"]) == 1.50, "the DB row itself was not migrated"

    def test_consolidate_enabled_is_migrated_off_on_an_already_seeded_registry(self):
        """`consolidate_enabled` was seeded True in July, and `_ensure_settings` only INSERTs missing
        keys — so without a migration row the live registry keeps True forever and the two knobs
        disagree about what the fleet is doing. This is the exact trap `_SETTING_MIGRATIONS` exists
        for (see `heartbeat_stale_min`, `claim_timeout_min`)."""
        assert ("consolidate_enabled", True, False) in disp._SETTING_MIGRATIONS

    # ---- 1. priority preemption ----

    def test_a_high_priority_task_no_longer_evicts_anyone(self):
        """`--probe` degrades as intended: still priority 90 and first in the queue, but it WAITS or
        gets its own box instead of interrupting work that is already running."""
        fx = _fx("preempt.json")
        case = next(c for c in fx["cases"] if c["expect"]["action"] == "preempt")
        assert _place_mech(case).action == "preempt", "precondition: this fixture does preempt when on"
        assert _place(case).action != "preempt", (
            "a high-priority task still evicted a running one with the switch off")

    # ---- 2. capacity scale-down ----

    def test_an_owned_box_over_its_window_cap_is_tolerated_not_evicted(self, tmp_path, monkeypatch):
        """The day window dropping below the running load must no longer shed tasks. Placement still
        refuses to ADMIT over the cap (inv. 18a/23), so the overshoot is bounded and drains by
        itself — and it is LOGGED, because going silent about a real over-cap box would trade one
        problem for a blind spot."""
        d = self._dispatcher(tmp_path, preempt_enabled=False)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (-1, 555, "laptop", reg.now_iso(), "live", 0.0, "h", 22, 6, reg.now_iso(), "owned"))
        for i in range(4):
            reg.insert_task(d.conn, id=f"OC{i}", created_at=reg.now_iso(), created_by="t", grp="g",
                            name=f"OC{i}", entrypoint="smoke", args_json="[]", config_json="{}",
                            config_hash=f"OC{i}", arm_hash=f"OC{i}", git_sha="d", slots=1,
                            est_minutes=60, priority=50, max_retries=4, state="running",
                            instance_id=-1)
        d.conn.commit()
        monkeypatch.setattr(d, "_capacity_slots", lambda inst: 1)      # cap collapsed to 1 of 4
        monkeypatch.setattr(d, "_capacity_budget", lambda inst: None)
        evicted = []
        monkeypatch.setattr(d, "_evict_task_graceful", lambda *a, **k: evicted.append(a))
        monkeypatch.setattr(disp, "endpoint_for", lambda i, t, r: ("h", 22))
        d._reap_over_capacity()
        assert evicted == [], f"evicted {len(evicted)} task(s) with the switch off"
        ev = [dict(r) for r in d.conn.execute(
            "SELECT event, detail FROM events WHERE event='capacity_over_cap_tolerated'")]
        assert ev, "went silent about a box genuinely over its cap"
        assert "NOT evicting" in ev[0]["detail"]
        # ...and with the switch ON it still sheds, so this is a switch, not a removal.
        d.settings["preempt_enabled"] = True
        d._reap_over_capacity()
        assert evicted, "the mechanism was removed rather than gated"

    # ---- 3. consolidation ----

    def test_consolidation_drains_nothing(self):
        fx = next(c for c in _fx("consolidate.json")["cases"]
                  if c["name"] == "drain_worst_paid_onto_owned")
        assert disp.consolidation_drains(fx["instances"], _mech_settings(), 0) != [], "precondition"
        assert disp.consolidation_drains(fx["instances"], _settings(), 0) == [], (
            "consolidation still drained a box with the global switch off")

    def test_the_switch_beats_consolidate_enabled_being_left_on(self):
        """Defence in depth: someone re-enabling `consolidate_enabled` alone must not reintroduce
        churn the operator switched off globally."""
        fx = next(c for c in _fx("consolidate.json")["cases"]
                  if c["name"] == "drain_worst_paid_onto_owned")
        st = _settings({"consolidate_enabled": True})     # preempt_enabled still False
        assert disp.consolidation_drains(fx["instances"], st, 0) == []

    # ---- the carve-out ----

    def test_operator_drain_still_works(self, tmp_path, monkeypatch):
        """`make drain` / `make pause` must NOT be gated. That is the operator pressing a button to
        get their own laptop back, not the fleet choosing to churn — gating it would remove the
        ability to reclaim a personal machine on demand, the opposite of what the directive
        protects."""
        d = self._dispatcher(tmp_path, preempt_enabled=False)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (-1, 555, "laptop", reg.now_iso(), "paused", 0.0, "h", 22, 6, reg.now_iso(), "owned"))
        reg.insert_task(d.conn, id="DR1", created_at=reg.now_iso(), created_by="t", grp="g",
                        name="DR1", entrypoint="smoke", args_json="[]", config_json="{}",
                        config_hash="DR1", arm_hash="DR1", git_sha="d", slots=1, est_minutes=60,
                        priority=50, max_retries=4, state="running", instance_id=-1)
        d.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                       ("pause_i-1", json.dumps({"mode": "hard", "at": reg.now_iso()})))
        d.conn.commit()
        evicted = []
        monkeypatch.setattr(d, "_evict_task_graceful", lambda *a, **k: evicted.append(a))
        monkeypatch.setattr(disp, "endpoint_for", lambda i, t, r: ("h", 22))
        d._signal_drain()
        assert evicted, "the global switch broke `make drain` — the operator cannot reclaim their box"


class TestThePollCycleTimesItself:
    """Invariant 23 (2026-07-31): the poll cycle emits its own per-phase timing.

    The cycle's LENGTH is an emergent property of fleet size and box latency (serial rsyncs, a 300 s
    ship budget), and at least three settings are really predicates on it — `heartbeat_stale_min`
    silently disables the dead-worker reaper once the cycle exceeds it, `ship_budget_sec` makes ship
    throughput a duty cycle of it, `checkpoint_pull_every_min` cannot outpace it. It was measured
    nowhere, so the 2026-07-31 incident (median 31 min against a ~7-9 min design point ⇒ 97.9% reaper
    skip rate and ~14 ships/hr) could only be found by reverse-engineering gaps between unrelated
    event timestamps. One event per cycle turns that into one query.
    """

    def _dispatcher(self, tmp_path, monkeypatch, run):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        for phase in ("do_reconcile", "_refresh_heartbeats", "_advance_provisioning",
                      "_ingest_and_complete", "_signal_cancels", "_signal_drain",
                      "_reap_over_capacity", "_measure_box_resources", "_place_queue",
                      "_consolidate", "_ship_all", "_teardown_idle"):
            monkeypatch.setattr(d, phase, lambda *a, **k: None)
        return d

    def _cycle_event(self, d):
        row = d.conn.execute(
            "SELECT detail FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def test_a_poll_logs_every_phase_and_the_total(self, tmp_path, monkeypatch):
        d = self._dispatcher(tmp_path, monkeypatch, _RecordingRun())
        d.poll_once()
        ev = self._cycle_event(d)
        assert ev is not None, "a poll cycle left no timing record"
        assert set(ev["phases"]) == {
            "reconcile", "ssh_config", "heartbeats", "provision", "ingest", "cancels", "drain",
            "over_capacity", "capacity_push", "probe", "box_requests", "measure", "book_cost", "place", "consolidate", "ship",
            "teardown", "gc_staging", "gc_temps", "gc_snapshots", "worker_refresh"}, ev["phases"]
        assert ev["total_sec"] == pytest.approx(sum(ev["phases"].values()), abs=0.5)

    def test_a_slow_cycle_is_flagged_against_the_heartbeat_threshold(self, tmp_path, monkeypatch):
        """A cycle at or past `heartbeat_stale_min` is still worth flagging — it drives ship duty and
        checkpoint pull cadence. But it no longer DISABLES the dead-worker reaper: 10c(g) pulls the
        HEARTBEAT inline and re-stats, so the old `reaper_disabled` name asserted a consequence that
        had stopped being true, and reading it as one caused a live misdiagnosis on a healthy fleet
        (2026-07-31). Measured after 10c(g): 0 `dead_worker_skipped` vs 183 in the prior 12h."""
        d = self._dispatcher(tmp_path, monkeypatch, _RecordingRun())
        slow = d.settings["heartbeat_stale_min"] * 60 + 1
        clock = _FakeClock()
        monkeypatch.setattr(disp.time, "monotonic", clock)
        monkeypatch.setattr(d, "_ingest_and_complete", lambda: clock.advance(slow))
        d.poll_once()
        ev = self._cycle_event(d)
        assert ev["cycle_over_heartbeat_stale"] is True, ev
        assert max(ev["phases"], key=ev["phases"].get) == "ingest", "did not name the slow phase"

    def test_a_healthy_cycle_is_not_flagged(self, tmp_path, monkeypatch):
        d = self._dispatcher(tmp_path, monkeypatch, _RecordingRun())
        d.poll_once()
        ev = self._cycle_event(d)
        assert ev["cycle_over_heartbeat_stale"] is False
        assert ev["ship_duty"] == 0.0        # nothing shipped in this stubbed cycle

    def test_a_phase_that_raises_still_records_its_time(self, tmp_path, monkeypatch):
        """A crash mid-cycle is otherwise invisible — `timed` records in `finally` so the burned
        time survives the exception, and the exception still propagates to the supervisor."""
        d = self._dispatcher(tmp_path, monkeypatch, _RecordingRun())

        def boom():
            raise RuntimeError("ingest blew up")

        monkeypatch.setattr(d, "_ingest_and_complete", boom)
        with pytest.raises(RuntimeError, match="ingest blew up"):
            d.poll_once()
        assert self._cycle_event(d) is None, "a crashed cycle must not claim to have completed"

    def test_dry_run_writes_no_cycle_event(self, tmp_path, monkeypatch):
        """`--dry-run`'s contract is no ssh/vastai calls and no DB writes, and every phase this
        measures is one that mode skips — so the timing would be meaningless as well as illegal."""
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run, dry_run=True)
        monkeypatch.setattr(d, "do_reconcile", lambda *a, **k: None)
        monkeypatch.setattr(d, "_place_queue", lambda *a, **k: None)
        d.poll_once()
        assert self._cycle_event(d) is None


class _FakeClock:
    """A controllable `time.monotonic`, so a phase can 'take' minutes without the test sleeping."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestIngestReportsItsOwnBreakdown:
    """Invariant 23b: ingest is the cycle's dominant phase (measured 960s of 1475s, 65%), so it
    reports per-sub-phase seconds AND transport call COUNT — otherwise "ingest is slow" is exactly
    as unactionable as "the cycle is slow" was before invariant 23.

    The two numbers decide DIFFERENT fixes, which is why both are logged: many cheap calls means the
    transport is latency-bound and parallelisable (`_run_or_timeout` already gives each call its own
    process group, so it is thread-safe); few expensive calls means the payload is the problem and a
    budget/size cap is the lever. The per-TASK sub-phases (`tb`, `checkpoints`) scale with running
    tasks rather than boxes, so on a full fleet they, not the per-box ones, dominate.
    """

    def _dispatcher(self, tmp_path, monkeypatch, n_tasks=2):
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        # seed the box ONCE (the helper inserts the instance too, so re-calling it collides on
        # instances.id), then add the remaining tasks onto that same box.
        reg = _seed_instance_and_task(d.conn, task_id="IG0")
        d.conn.execute("UPDATE tasks SET state='running' WHERE id='IG0'")
        for i in range(1, n_tasks):
            reg.insert_task(
                d.conn, id=f"IG{i}", created_at=reg.now_iso(), created_by="test", grp="g",
                name=f"IG{i}", entrypoint="smoke", args_json="[]", config_json="{}",
                config_hash=f"IG{i}", arm_hash=f"IG{i}", git_sha="deadbeef", slots=1,
                est_minutes=1, priority=50, max_retries=1, state="claimed", instance_id=1)
            d.conn.execute(f"UPDATE tasks SET state='running' WHERE id='IG{i}'")
        d.conn.commit()
        for reaper in ("_reap_stalled", "_reap_dead_workers", "_reap_orphaned_tasks",
                       "_reap_overpacked_boxes", "_reap_unclaimed_ships",
                       "_reap_undeliverable_claims", "_reap_unreachable_owned",
                       "_reap_paused_soft_timeout"):
            monkeypatch.setattr(d, reaper, lambda *a, **k: None)
        return d, run

    def test_every_sub_phase_is_named_and_counted(self, tmp_path, monkeypatch):
        d, run = self._dispatcher(tmp_path, monkeypatch)
        d._ingest_and_complete()
        det = d._ingest_detail
        # the two SERIAL sub-phases keep their own seconds; the parallel payload phase reports
        # wall-clock here and summed per-thread work under `payload_work_sec` (they differ once the
        # fan-out is doing anything, which is the point).
        assert set(det["sec"]) == {"worker_state", "markers", "payloads_wall"}, det
        assert set(det["rsyncs"]) == set(det["sec"])
        assert set(det["payload_work_sec"]) == {"tb", "checkpoints"}, det
        assert det["total_rsyncs"] == sum(det["rsyncs"].values()) > 0, det
        assert det["payload_boxes"] == 1
        # sorted worst-first, so the phase to blame reads off the front
        assert list(det["sec"].values()) == sorted(det["sec"].values(), reverse=True)

    def test_the_per_task_pulls_scale_with_TASKS_not_boxes(self, tmp_path, monkeypatch):
        """The load-bearing asymmetry behind invariant 23c: the payload pulls (`tb`, `checkpoints`)
        issue one rsync PER RUNNING TASK, while `worker_state`/`markers` issue a fixed number PER
        BOX. A fleet with many tasks per box is therefore dominated by the per-task calls — which is
        why those two, and not the per-box ones, are the half worth parallelising."""
        d1, _ = self._dispatcher(tmp_path / "a", monkeypatch, n_tasks=1)
        d1._ingest_and_complete()
        d4, _ = self._dispatcher(tmp_path / "b", monkeypatch, n_tasks=4)
        d4._ingest_and_complete()
        assert d4._ingest_detail["rsyncs"]["payloads_wall"] > \
            d1._ingest_detail["rsyncs"]["payloads_wall"], "payload pulls did not scale with tasks"
        for per_box in ("worker_state", "markers"):
            assert d4._ingest_detail["rsyncs"][per_box] == d1._ingest_detail["rsyncs"][per_box], \
                f"{per_box} should be fixed per box, not per task"

    def test_the_count_survives_a_failing_pull(self, tmp_path, monkeypatch):
        """A transport call that FAILS is still a call that cost wall-clock — the count must include
        it, or a fleet of unreachable boxes reads as 'barely any rsyncs' while burning 60s each."""
        d, _ = self._dispatcher(tmp_path, monkeypatch)
        monkeypatch.setattr(disp, "_run_or_timeout",
                            lambda run, cmd, timeout: _FakeProc(1, "connection refused"))
        d._ingest_and_complete()
        assert d._ingest_detail["total_rsyncs"] > 0


class TestGpuClassCeiling:
    """Invariant 4f — `max_instance_dph` as a GPU-CLASS ceiling with a per-task override.

    Measured 2026-07-31: this line's workload does not use the GPU (2381 `box_measured` samples:
    GPU util median 0%, mean 3.6%, 79% exactly 0%; VRAM 0.27 GB used of 13.6 GB rented). The dear
    boxes were also the worse buy — pooled, offers > $0.07/hr cost 1.91x more AND ever-ran a task
    only 24% of the time vs 54% for cheap ones. $0.08 keeps 90% of actual rentals while blocking
    exactly that class."""

    def test_default_blocks_the_measured_bad_class(self):
        cap = disp.DEFAULT_SETTINGS["max_instance_dph"]
        assert cap == 0.08
        # the classes measured at 24% usable and ~1.9x price must not qualify by default
        for dear in (0.1747, 0.1348, 0.1169, 0.1075):  # V100, V100, Q RTX 6000, RTX 3090
            assert dear > cap
        # ...while the workhorse RTX 3060 band still does
        for cheap in (0.0507, 0.0547, 0.0645):
            assert cheap <= cap

    def test_task_hint_raises_its_own_ceiling_only(self):
        s = _settings({"max_instance_dph": 0.08})
        assert disp.task_max_dph({"resource_hint": None}, s) == 0.08
        assert disp.task_max_dph({"resource_hint": {}}, s) == 0.08
        # a 24 GB-card job lifts ITS ceiling
        assert disp.task_max_dph({"resource_hint": {"max_dph": 0.40}}, s) == 0.40

    def test_hint_never_lowers_the_global_cap(self):
        """The cap is a spend guard. A hint may only RAISE — a lower one is ignored, so a task
        cannot narrow then widen the fleet's exposure through the same field."""
        s = _settings({"max_instance_dph": 0.08})
        assert disp.task_max_dph({"resource_hint": {"max_dph": 0.01}}, s) == 0.08

    def test_malformed_hint_falls_back_to_the_global_cap(self):
        """A bad hint must never crash placement (invariant 12 style)."""
        s = _settings({"max_instance_dph": 0.08})
        for bad in ("0.4", None, True, False, -1, 0, [], {}):
            assert disp.task_max_dph({"resource_hint": {"max_dph": bad}}, s) == 0.08

    def test_place_filters_offers_by_the_per_task_ceiling(self):
        """End-to-end through `place()`: the same dear offer is refused for an ordinary task and
        accepted for one that declared it needs a big card."""
        offers = [{"id": 1, "dph_total": 0.20, "gpu_ram_gb": 24.0, "cpu_cores_effective": 8.0,
                   "machine_id": 11, "reliability": 0.99}]
        base = {"id": "t", "slots": 1, "est_minutes": 30, "priority": 50,
                "retries_used": 0, "max_retries": 1}
        s = _settings({"max_instance_dph": 0.08})

        ordinary = dict(base, resource_hint=None)
        p1 = disp.place(ordinary, [], offers, [ordinary] * 3, s, 0)
        assert p1.action == "hold" and "no_offer" in p1.reason

        big = dict(base, resource_hint={"max_dph": 0.40})
        p2 = disp.place(big, [], offers, [big] * 3, s, 0)
        assert p2.action == "rent" and p2.offer["id"] == 1


class TestCpuNameTargeting:
    """Invariant 4f — `resource_hint.cpu_name_include` lets ONE task rent a NAMED CPU class.

    The ranker can only prefer fast cores, never require them, so the fast half of the `cpu_ghz`
    axis is never sampled and a hardware bench cannot choose its own silicon. Opt-in per task;
    absent => every existing task behaves exactly as before."""

    def test_absent_or_malformed_hint_filters_nothing(self):
        offer = {"cpu_name": "AMD EPYC 7742 64-Core Processor"}
        for hint in (None, {}, {"cpu_name_include": None}, {"cpu_name_include": []},
                     {"cpu_name_include": 17}, {"cpu_name_include": ["", "  "]}):
            assert disp.cpu_name_allowed(offer, hint) is True

    def test_matches_case_insensitive_substring(self):
        offer = {"cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}
        assert disp.cpu_name_allowed(offer, {"cpu_name_include": ["9950x"]}) is True
        assert disp.cpu_name_allowed(offer, {"cpu_name_include": "9950X"}) is True   # bare string
        assert disp.cpu_name_allowed(offer, {"cpu_name_include": ["7950X", "9950X"]}) is True
        assert disp.cpu_name_allowed(offer, {"cpu_name_include": ["EPYC"]}) is False

    def test_unnamed_offer_is_refused_when_the_filter_is_set(self):
        """FAIL-CLOSED, deliberately opposite to the fail-open reliability gate: a targeted bench
        would rather HOLD than be placed on an unidentified CPU and answer a different question."""
        for offer in ({"cpu_name": None}, {"cpu_name": ""}, {}):
            assert disp.cpu_name_allowed(offer, {"cpu_name_include": ["9950X"]}) is False
            assert disp.cpu_name_allowed(offer, None) is True  # ...but only when the filter is on

    def test_place_rents_the_named_cpu_over_a_cheaper_faster_ranking_box(self):
        """End-to-end: the cheap many-core box WINS value-density and is what an untargeted task
        gets — the whole reason the filter is needed. With the hint set, the dear named box is the
        only candidate; without it, the cheap one wins."""
        cheap = {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 12.0, "cpu_cores_effective": 64.0,
                 "ram_gb": 256.0, "machine_id": 11, "reliability": 0.99, "cpu_ghz": 2.2,
                 "cpu_name": "AMD EPYC 7742 64-Core Processor"}
        dear = {"id": 2, "dph_total": 0.56, "gpu_ram_gb": 32.0, "cpu_cores_effective": 16.0,
                "ram_gb": 94.0, "machine_id": 12, "reliability": 0.99, "cpu_ghz": 5.75,
                "cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}
        base = {"id": "t", "slots": 8, "est_minutes": 20, "priority": 90,
                "retries_used": 0, "max_retries": 1}
        s = _settings({"max_instance_dph": 0.08})

        untargeted = dict(base, resource_hint={"max_dph": 0.60})
        p1 = disp.place(untargeted, [], [cheap, dear], [untargeted] * 3, s, 0)
        assert p1.action == "rent" and p1.offer["id"] == 1  # value density picks the EPYC

        targeted = dict(base, resource_hint={"max_dph": 0.60, "cpu_name_include": ["9950X"]})
        p2 = disp.place(targeted, [], [cheap, dear], [targeted] * 3, s, 0)
        assert p2.action == "rent" and p2.offer["id"] == 2

    @staticmethod
    def _box(label, iid=900):
        return {"id": iid, "state": "live", "slots_total": 11, "gpu_name": "RTX 5090",
                "dph_usd": 0.42, "minutes_to_hard_cap": 600, "source": "vast", "tasks": [],
                "label": label}

    @staticmethod
    def _offer():
        return {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 12.0, "cpu_cores_effective": 16.0,
                "ram_gb": 64.0, "machine_id": 11, "reliability": 0.99, "cpu_ghz": 5.7,
                "cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}

    def test_a_targeted_task_refuses_a_box_it_did_not_rent(self):
        """REGRESSION 1 (live, 2026-08-07): `cpu_name_allowed` guarded the RENT path only, so the
        9950X-targeted cell PACKED onto a box just rented for the 9950X3D cell and returned a curve
        labelled 9950X measured on 128 MiB of V-Cache. The `instances` row has `gpu_name` but no
        `cpu_name`, so a box this task did not rent has an unverifiable CPU and must be refused."""
        base = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 90,
                "retries_used": 0, "max_retries": 1}
        s = _settings({})
        someone_elses = [self._box("runq_OTHER-TASK")]

        # an ORDINARY task packs onto it (unchanged behaviour, and the control)
        ordinary = dict(base, resource_hint=None)
        assert disp.place(ordinary, someone_elses, [self._offer()], [ordinary] * 3, s, 0).action == "pack"

        # the TARGETED one refuses and rents its own, even though packing is free
        targeted = dict(base, resource_hint={"cpu_name_include": ["9950X 16-Core"]})
        p = disp.place(targeted, someone_elses, [self._offer()], [targeted] * 3, s, 0)
        assert p.action == "rent" and p.offer["cpu_name"].startswith("AMD Ryzen 9 9950X 16")

    def test_a_targeted_task_DOES_pack_onto_the_box_it_rented(self):
        """REGRESSION 2 (live, 2026-08-07) — and this one was CAUSED by the fix for regression 1.

        Blanket rent-only broke placement outright: renting is how the box comes to EXIST, and the
        task boards it on a later poll through the pack path. Refusing to pack meant rent a box, be
        forbidden to board it, rent another next poll — task 7c5f6460 rented THREE boxes in 16
        minutes and ran on none. The box labelled `runq_<this task>` was vetted by
        `cpu_name_allowed` at rent time, so it is the one verified CPU available and must be
        packable."""
        s = _settings({})
        targeted = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 90, "retries_used": 0,
                    "max_retries": 1, "resource_hint": {"cpu_name_include": ["9950X 16-Core"]}}
        own = [self._box("runq_t")]
        p = disp.place(targeted, own, [self._offer()], [targeted] * 3, s, 0)
        assert p.action == "pack" and p.target == 900

    def test_a_targeted_task_does_not_wait_for_capacity_it_cannot_board(self):
        """REGRESSION 3 (live, 2026-08-07), the third instance of the SAME root cause: restricting
        one branch of `place()` silently changed the meaning of the others.

        With the pack filter fixed, `_soonest_wait` (4c) and the over-provisioning guard (5c) still
        counted OTHER tasks' boxes as incoming capacity, so a targeted task deferred its own rental
        waiting for a slot it can never board. On a busy fleet something is nearly always freeing
        inside `rent_patience_min`, so that hold never clears — stuck, not slow."""
        s = _settings({})
        targeted = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 90, "retries_used": 0,
                    "max_retries": 1, "resource_hint": {"cpu_name_include": ["9950X 16-Core"]}}
        ordinary = dict(targeted, resource_hint=None)

        # someone else's FULL box with an occupant about to finish => a slot frees in 2 minutes
        other = dict(self._box("runq_SOMEONE-ELSE"), slots_total=1)
        other["occupants"] = [{"id": "victim", "state": "running", "slots": 1,
                               "est_minutes": 10, "running_minutes_ago": 8}]
        other["tasks"] = other["occupants"]

        # the ordinary task is entitled to that slot and waits for it...
        p_ord = disp.place(ordinary, [other], [self._offer()], [ordinary] * 3, s, 0)
        assert p_ord.action == "hold" and "slot_freeing_soon" in p_ord.reason

        # ...the targeted one must NOT: that slot is on a box it can never board, so it rents.
        p_tgt = disp.place(targeted, [other], [self._offer()], [targeted] * 3, s, 0)
        assert p_tgt.action == "rent", f"targeted task held on unboardable capacity: {p_tgt.reason}"

    def test_a_targeted_task_does_not_wait_for_someone_elses_provisioning_box(self):
        """The 5c twin of the above — another task's incoming box is not incoming capacity here."""
        s = _settings({})
        targeted = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 90, "retries_used": 0,
                    "max_retries": 1, "resource_hint": {"cpu_name_include": ["9950X 16-Core"]}}
        ordinary = dict(targeted, resource_hint=None)
        coming = dict(self._box("runq_SOMEONE-ELSE"), state="provisioning")

        p_ord = disp.place(ordinary, [coming], [self._offer()], [ordinary] * 3, s, 0)
        assert p_ord.action == "hold" and "awaiting_provisioning" in p_ord.reason

        p_tgt = disp.place(targeted, [coming], [self._offer()], [targeted] * 3, s, 0)
        assert p_tgt.action == "rent", f"targeted task held on someone else's box: {p_tgt.reason}"

    def test_a_targeted_task_fails_closed_on_a_box_with_no_label(self):
        s = _settings({})
        targeted = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 90, "retries_used": 0,
                    "max_retries": 1, "resource_hint": {"cpu_name_include": ["9950X 16-Core"]}}
        nameless = [self._box(None)]
        assert disp.place(targeted, nameless, [self._offer()], [targeted] * 3, s, 0).action == "rent"

    def test_the_substring_that_bit_us_does_not_match_the_x3d(self):
        """`9950X 16-Core` must NOT match `9950X3D 16-Core` — the two arms differ only by cache,
        so a substring that matched both would silently collapse the comparison."""
        plain = {"cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}
        x3d = {"cpu_name": "AMD Ryzen 9 9950X3D 16-Core Processor"}
        assert disp.cpu_name_allowed(plain, {"cpu_name_include": ["9950X 16-Core"]}) is True
        assert disp.cpu_name_allowed(x3d, {"cpu_name_include": ["9950X 16-Core"]}) is False
        assert disp.cpu_name_allowed(x3d, {"cpu_name_include": ["9950X3D"]}) is True
        assert disp.cpu_name_allowed(plain, {"cpu_name_include": ["9950X3D"]}) is False

    def test_no_matching_cpu_holds_rather_than_falling_back(self):
        """A filter that matches nothing must HOLD, never silently place on the next-best box."""
        cheap = {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 12.0, "cpu_cores_effective": 64.0,
                 "ram_gb": 256.0, "machine_id": 11, "reliability": 0.99, "cpu_ghz": 2.2,
                 "cpu_name": "AMD EPYC 7742 64-Core Processor"}
        task = {"id": "t", "slots": 1, "est_minutes": 20, "priority": 50, "retries_used": 0,
                "max_retries": 1, "resource_hint": {"cpu_name_include": ["9950X"]}}
        p = disp.place(task, [], [cheap], [task] * 3, _settings({}), 0)
        assert p.action == "hold" and "no_offer" in p.reason


class TestBoxTargeting:
    """Invariant 4f — `resource_hint.box` pins a task to ONE box the fleet ALREADY HAS.

    WHY IT EXISTS (owner, 2026-08-08: "a task should be able to specify which box it wants"). Some
    reads are about a MACHINE, not the work — does this reproduce on a real GPU, what does this box
    deliver saturated, reproduce the failure that only happens on the laptop. Measured cost of the
    gap the day it landed: a GPU parity probe was requeued four times and landed three times running
    on owned instance -2, whose GPU is blocked by the OS, answering nothing each time.

    ⚠ IT DIFFERS IN KIND FROM `cpu_name_include`, and the difference is the whole test class. A CPU
    target is satisfied BY RENTING (that is how the box comes to exist). A box target can NEVER be
    satisfied by renting, so the rent path must be closed for it — otherwise it is `_boardable`'s
    incident 2 replayed: rent a box, be forbidden to board it, rent another next poll."""

    @staticmethod
    def _box(iid, label=None, tasks=(), slots=2):
        return {"id": iid, "state": "live", "slots_total": slots, "gpu_name": "RTX 4080",
                "dph_usd": 0.0, "minutes_to_hard_cap": 600, "source": "owned",
                "tasks": list(tasks), "occupants": [], "label": label}

    @staticmethod
    def _offer():
        return {"id": 1, "dph_total": 0.05, "gpu_ram_gb": 12.0, "cpu_cores_effective": 16.0,
                "ram_gb": 64.0, "machine_id": 11, "reliability": 0.99, "cpu_ghz": 5.7,
                "cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}

    @staticmethod
    def _task(box=None, **kw):
        t = {"id": "t", "slots": 1, "est_minutes": 10, "priority": 50, "retries_used": 0,
             "max_retries": 1, "resource_hint": ({"box": box} if box is not None else None)}
        t.update(kw)
        return t

    def test_the_hint_is_parsed_by_id_or_label_and_is_absent_by_default(self):
        assert disp.box_target(None) is None and disp.box_target({}) is None
        assert disp.box_target({"box": None}) is None
        assert disp.box_target({"box": "  "}) is None          # blank is not a target
        assert disp.box_target({"box": -1}) == "-1"            # an int id normalises to str
        assert disp.box_target({"box": "laptop-gpu"}) == "laptop-gpu"
        assert disp.is_box_targeted(self._task()) is False
        assert disp.is_box_targeted(self._task(box=-1)) is True

    def test_it_packs_onto_the_named_box_by_id_and_by_label(self):
        """Both handles work, because which one you have depends on the box: rentals are known by
        id, owned boxes by label."""
        s = _settings({})
        boxes = [self._box(-2, label="desktop"), self._box(-1, label="laptop-gpu")]
        for target, want in ((-1, -1), ("laptop-gpu", -1), (-2, -2), ("desktop", -2)):
            p = disp.place(self._task(box=target), boxes, [self._offer()],
                           [self._task(box=target)] * 3, s, 0)
            assert p.action == "pack" and p.target == want, f"{target!r} -> {p}"

    def test_it_REFUSES_every_other_box_even_when_packing_there_is_free(self):
        """The point of the feature. An ordinary task packs onto the wrong box happily — that is the
        control, and it is what was happening to the GPU probe four times in a row."""
        s = _settings({})
        wrong_box_only = [self._box(-2, label="desktop")]
        ordinary = self._task()
        assert disp.place(ordinary, wrong_box_only, [self._offer()],
                          [ordinary] * 3, s, 0).action == "pack"
        targeted = self._task(box="laptop-gpu")
        p = disp.place(targeted, wrong_box_only, [self._offer()], [targeted] * 3, s, 0)
        assert p.action == "hold", p

    def test_it_NEVER_RENTS_even_with_a_backlog_and_a_qualifying_offer(self):
        """⛔ THE ONE THAT MATTERS. No rental can ever BECOME the named box, so falling through to
        4e would buy a box this task is forbidden to board, then buy another next poll — three boxes
        in 16 minutes, none used (`_boardable` incident 2). Priority 90 is used deliberately: it
        clears the backlog bar, so nothing but the 4e0 guard is standing between this task and a
        rental."""
        s = _settings({})
        targeted = self._task(box="laptop-gpu", priority=90)
        p = disp.place(targeted, [], [self._offer()], [targeted] * 5, s, 0)
        assert p.action == "hold" and "box_target" in p.reason, p

    def test_an_unknown_target_holds_and_SAYS_it_is_unknown(self):
        """A typo'd label must not look like ordinary congestion. It holds forever either way, so the
        reason line is the only thing that tells an operator which of the two it is."""
        s = _settings({})
        t = self._task(box="laptop-4O8O")                      # letter O, not zero
        p = disp.place(t, [self._box(-1, label="laptop-gpu")], [self._offer()], [t] * 3, s, 0)
        assert p.action == "hold" and "NO SUCH BOX" in p.reason, p

    def test_a_box_with_no_label_matches_nothing_rather_than_anything(self):
        """Fail-closed, same as `cpu_name_allowed`. An unlabelled box must never absorb a target."""
        s = _settings({})
        t = self._task(box="laptop-gpu")
        p = disp.place(t, [self._box(-1, label=None)], [self._offer()], [t] * 3, s, 0)
        assert p.action == "hold", p

    def test_it_does_not_inflate_the_backlog_bar_for_OTHER_tasks(self):
        """A task that can only run on one existing box is not evidence the fleet needs to RENT one.

        Counting it would make a stuck box-targeted task buy capacity that structurally cannot serve
        it — the same class as `_boardable` incident 3, where a targeted task's demand leaked into
        another branch's idea of capacity."""
        s = _settings({})
        ordinary = self._task(id="o", resource_hint=None)
        stuck_targets = [self._task(box="laptop-gpu", id=f"b{i}") for i in range(5)]
        # A FULL live box, so the backlog bar is actually reached: `any_instance_exists` gates that
        # whole block, and with zero instances the fleet rents without consulting it at all.
        full = self._box(-2, label="desktop", slots=1)
        full["occupants"] = [{"id": "x", "slots": 1, "state": "running",
                              "est_minutes": 600, "running_minutes_ago": 0}]
        p = disp.place(ordinary, [full], [self._offer()], stuck_targets, s, 0)
        assert p.action == "hold" and "backlog_too_small" in p.reason, p
        # ...and the SAME queue of ordinary tasks DOES clear the bar — otherwise this test would
        # pass on a fleet that never rents, proving nothing about the exclusion.
        ordinary_queue = [self._task(id=f"o{i}", resource_hint=None) for i in range(5)]
        p2 = disp.place(ordinary, [full], [self._offer()], ordinary_queue, s, 0)
        assert p2.action == "rent", p2

    def test_the_REAL_instance_view_carries_every_field_placement_keys_on(self, tmp_path):
        """⛔ THE ONE THAT WOULD HAVE CAUGHT THE LIVE BUG. Every other test in this file and in
        `TestCpuNameTargeting` hand-builds instance dicts, so they assert against a shape the
        PRODUCER may not actually emit — and on 2026-08-08 it did not: `_instances_view` omitted
        `label` entirely.

        The consequence was not limited to `--box`. `_boardable` decides a CPU-targeted task's own
        box by `i.get("label") == f"runq_{task_id}"`, so with the key absent that comparison was
        `None == "runq_…"` — FALSE forever. The 2026-08-07 fix for `_boardable` incident 2 ("a
        targeted task DOES pack onto the box it rented") was therefore INERT on the live fleet: the
        task refused its own box and fell through to rent another, the exact rent-loop it existed to
        prevent. Its tests passed the whole time because their fixtures supply `label`.

        Same class as the `_adopt` `slots_total` bug (docs/operations.md): the fixture fed a field the
        real producer never sends, so the test agreed with a broken implementation. So this asserts
        against the REAL view, built from a REAL row, and drives `_boardable` with it end to end."""
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (7, 555, "laptop-gpu", reg.now_iso(), "live", 0.0, "h", 22, 4, reg.now_iso()))
        d.conn.commit()

        view = {v["id"]: v for v in d._instances_view()}
        assert "label" in view[7], (
            "`_instances_view` dropped `label`, so every label-keyed placement rule is dead in "
            "production while its hand-built-fixture tests pass")
        assert view[7]["label"] == "laptop-gpu"

        # ...and the predicate that consumes it works against the REAL shape, not just a fixture.
        real = list(view.values())
        assert [i["id"] for i in disp._boardable(self._task(box="laptop-gpu"), real)] == [7]
        assert [i["id"] for i in disp._boardable(self._task(box=7), real)] == [7]
        assert disp._boardable(self._task(box="nope"), real) == []

    def test_the_REAL_instance_view_carries_gpu_name_too(self, tmp_path):
        """The test above, one axis and one month later — and it did NOT generalise (2026-09-04).

        Invariant 4h added a SECOND view-keyed placement read, `instance_has_gpu(inst)` ->
        `inst.get("gpu_name")`, and `_instances_view` never emitted it. So `_boardable` returned []
        for every `requires_gpu` task on every box, always. That empties EVERY branch of `place()`
        that could hold — pack, preempt, `_soonest_wait`, the 5c over-provisioning guard, and
        `_infeasible_everywhere` (`not any([])` is True, so the backlog bar always qualifies) — and
        leaves only RENT. Measured: 9 boxes for four cells, 3 more for one cell, $0.64/hr, budget
        cap tripped, nothing boarded, and no hold reason ever logged.

        4h's own tests passed throughout because they hand-build `{"gpu_name": ...}` dicts, and the
        incident handoff exonerated `requires_gpu` by driving `_boardable` with rows read straight
        out of sqlite — which carry `gpu_name`. Both asserted against a shape the producer does not
        emit. Hence the mechanical guard below: hand-listing the keys is what failed twice."""
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
            "ssh_host, ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (7, 555, "runq_gpu", reg.now_iso(), "live", 0.1, "RTX 3060", "h", 22, 4, reg.now_iso()))
        d.conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
            "ssh_host, ssh_port, slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (-3, 556, "tower", reg.now_iso(), "live", 0.0, None, "h", 22, 16, reg.now_iso()))
        d.conn.commit()

        view = {v["id"]: v for v in d._instances_view()}
        assert "gpu_name" in view[7], (
            "`_instances_view` dropped `gpu_name`, so invariant 4h's pack half is dead in "
            "production — every `requires_gpu` task rent-loops while its fixture tests pass")
        assert view[7]["gpu_name"] == "RTX 3060"

        # ...and the predicate that consumes it works against the REAL shape, not just a fixture.
        real = list(view.values())
        gpu_task = self._task(resource_hint={"requires_gpu": True})
        assert [i["id"] for i in disp._boardable(gpu_task, real)] == [7]
        # INERTNESS: a task that does not ask for a GPU still sees every box, unchanged.
        assert {i["id"] for i in disp._boardable(self._task(), real)} == {i["id"] for i in real}

    def test_NO_placement_key_is_missing_from_the_real_view_MECHANICAL(self):
        """⛔ THE GENERALISATION, because hand-listing the keys failed twice (`label` 2026-08-08,
        `gpu_name` 2026-09-04) and the second slipped past a test written for the first.

        Derives BOTH sides from the source instead of naming either: every string key the placement
        predicates read off an instance-shaped parameter, against every key `_instances_view`'s dict
        literal actually emits. Add a third view-keyed read without extending the view and this
        fails at test time rather than on the fleet's credit card.

        `(inst or {}).get("gpu_name")` is why the base is resolved through `BoolOp` — the omission
        this test was written for is invisible to a scan that only understands a bare `Name`."""
        import ast
        tree = ast.parse((ROOT / "fleet/dispatcher.py").read_text())

        emitted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_instances_view":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        emitted |= {k.value for k in sub.keys
                                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        assert "slots_total" in emitted, "scan found no view dict — the guard has gone blind"

        # Functions that receive `place()`'s instance dicts, and the parameter names they arrive as.
        CONSUMERS = {"_boardable", "_fits_now", "_free_slots", "_occupant_slots", "_pack_cost",
                     "_box_preference", "_box_matches", "instance_has_gpu", "box_headroom",
                     "_budget_fits", "_headroom_fits", "_soonest_wait", "_find_preemption",
                     "_relief_already_in_flight", "place", "_infeasible_everywhere",
                     "consolidation_drains", "_pending_footprint", "endpoint_for",
                     # invariant 4i (forced box placement) reads the same view
                     "_forced_fits", "admission_refusals", "_forced_hold_reason"}
        INST_PARAMS = {"inst", "i", "instance", "target", "victim", "box"}

        def bases(node):
            if isinstance(node, ast.Name):
                return {node.id}
            if isinstance(node, ast.BoolOp):        # `(inst or {}).get(...)`
                return set().union(*(bases(v) for v in node.values))
            return set()

        read = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name in CONSUMERS):
                continue
            for sub in ast.walk(node):
                key, base = None, set()
                if (isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant)
                        and isinstance(sub.slice.value, str)):
                    base, key = bases(sub.value), sub.slice.value
                elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                      and sub.func.attr == "get" and sub.args
                      and isinstance(sub.args[0], ast.Constant)
                      and isinstance(sub.args[0].value, str)):
                    base, key = bases(sub.func.value), sub.args[0].value
                if key and (base & INST_PARAMS):
                    read.setdefault(key, set()).add(node.name)

        assert "gpu_name" in read, "the scan stopped seeing `instance_has_gpu` — guard is blind"
        missing = {k: sorted(v) for k, v in read.items() if k not in emitted}
        assert not missing, (
            f"`_instances_view` does not emit {sorted(missing)}, which placement reads off an "
            f"instance: {missing}. In production that read is None for EVERY box, so the rule "
            f"depending on it is silently inert while its hand-built-fixture tests pass. This is "
            f"the `label` (2026-08-08) and `gpu_name` (2026-09-04) bug — add the key to the view.")

    def test_infeasible_everywhere_agrees_with_boardable(self):
        """The invariant `_boardable`'s docstring exists to enforce: every capacity branch answers
        about the SAME set of boxes. `_infeasible_everywhere` used to scan raw `live`, so a targeted
        task read as 'fits somewhere' on a box it may never board."""
        s = _settings({})
        elsewhere = [self._box(-2, label="desktop")]
        assert disp._infeasible_everywhere(self._task(), elsewhere, s) is False        # control
        assert disp._infeasible_everywhere(self._task(box="laptop-gpu"), elsewhere, s) is True


def _reg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("registry_db", ROOT / "fleet/registry_db.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["registry_db"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestColocation:
    """Invariant 4g — `resource_hint.colocate` co-locates SIBLINGS without naming a box.

    WHY IT EXISTS (owner, 2026-08-16). A paired comparison is only readable if its arms ran on the
    same machine: the box selects the attractor on a bistable rung (bit-identical within a box,
    ~1e-3 across), so an arm on box A against a control on box B is not a paired measurement, and a
    collapsed control manufactures a win. `runq sweep` had NO box control at all, so every multi-arm
    sweep was a box lottery by construction.

    ⚠ IT DIFFERS IN KIND FROM `--box`, and that difference is the whole class. `--box` makes the
    OPERATOR choose the machine and forces the WHOLE campaign onto it — pairing seed 1's arms with
    each other by also pairing them with seed 2's, which nothing required. This says only "these N
    tasks, together, wherever you like": arms of one seed share a key and a box; other seeds carry
    other keys and stay free to spread across the fleet."""

    @staticmethod
    def _box(iid, slots=2, free=None, dph=0.0, state="live", label=None):
        occ = [] if free is None else [{"id": f"x{iid}{n}", "slots": 1, "state": "running",
                                        "est_minutes": 600, "running_minutes_ago": 0,
                                        "cores": 1.0, "vram_gb": 0.0, "ram_gb": 1.0}
                                       for n in range(slots - free)]
        return {"id": iid, "state": state, "slots_total": slots, "dph_usd": dph,
                "minutes_to_hard_cap": 600, "occupants": occ, "label": label, "source": "owned"}

    @staticmethod
    def _task(tid="t", key="grp:seed1", slots=1, group_slots=None, box=None, **kw):
        hint = {}
        if key is not None:
            hint["colocate"] = key
        if box is not None:
            hint["box"] = str(box)
        t = {"id": tid, "slots": slots, "est_minutes": 10, "priority": 50, "retries_used": 0,
             "max_retries": 1, "resource_hint": hint or None}
        if group_slots is not None:
            t["colocate_group_slots"] = group_slots
        t.update(kw)
        return t

    @staticmethod
    def _offer(oid=1, cores=4.0, dph=0.05):
        return {"id": oid, "dph_total": dph, "gpu_ram_gb": 12.0, "cpu_cores_effective": cores,
                "ram_gb": 64.0, "machine_id": 10 + oid, "reliability": 0.99, "cpu_ghz": 5.0,
                "cpu_name": "AMD Ryzen 9 9950X 16-Core Processor"}

    # ------------------------------------------------------------------ the key, and its inertness
    def test_the_key_is_parsed_and_is_absent_by_default(self):
        assert disp.colocate_key(None) is None and disp.colocate_key({}) is None
        assert disp.colocate_key({"colocate": None}) is None
        assert disp.colocate_key({"colocate": "  "}) is None      # blank is not a group
        assert disp.colocate_key({"colocate": 7}) == "7"          # normalises to str, like `box`
        assert disp.is_colocated(self._task(key=None)) is False
        assert disp.is_colocated(self._task()) is True

    def test_group_demand_is_zero_for_an_ordinary_task_and_for_a_PINNED_member(self):
        """The group's demand may only steer the placement that is still FREE. Once the group has a
        box (the pin is stamped as `box`), inflating the demand would make the member fail to fit
        its own group's box and fall through to rent — `_boardable` incident 2 in a new costume."""
        assert disp.colocate_group_slots(self._task(key=None, group_slots=6)) == 0
        assert disp.colocate_group_slots(self._task(box=-1, group_slots=6)) == 0
        assert disp.colocate_group_slots(self._task(group_slots=6)) == 6
        # An unsupplied group size degrades to this task alone, i.e. to pre-feature behaviour.
        assert disp.colocate_group_slots(self._task()) == 1

    def test_an_uncolocated_task_places_exactly_as_before(self):
        """The control. Nothing about placement may change for the fleet's ordinary traffic."""
        s = _settings({})
        boxes = [self._box(1, slots=4, free=1), self._box(2, slots=4, free=4)]
        plain = self._task(key=None)
        assert disp.place(plain, boxes, [], [plain] * 3, s, 0).target == 1  # tightest fit, unchanged

    # ---------------------------------------------------------------------- the pinning placement
    def test_the_first_member_prefers_a_box_that_fits_the_WHOLE_group(self):
        """⛔ THE ONE THAT KEEPS CO-LOCATION FROM COSTING THE CAMPAIGN ITS PARALLELISM.

        Tightest-fit would send arm 1 to the box with ONE free lane — correct for a lone task, and
        ruinous here, because that choice binds arms 2 and 3 to a box with no room for them. They
        would still be co-located, just serialised three deep behind a decision made by a rule that
        could not see them."""
        s = _settings({})
        boxes = [self._box(1, slots=4, free=1), self._box(2, slots=4, free=3)]
        t = self._task(group_slots=3)
        p = disp.place(t, boxes, [], [], s, 0)
        assert p.action == "pack" and p.target == 2, p
        assert "all 3 group lanes fit" in p.reason, p
        # ...and the SAME queue without the group lands on the tight box, so this asserts the group
        # term and not just "box 2 was preferred anyway".
        assert disp.place(self._task(key=None), boxes, [], [], s, 0).target == 1

    def test_it_FALLS_BACK_rather_than_holding_when_no_box_fits_the_group(self):
        """A group must never be unplaceable for want of a box big enough to run it all at once —
        that would turn a 12-cell sweep into a permanently held queue on a fleet of 8-lane boxes.
        Serialised-but-co-located is the correct degradation, and the reason line says so."""
        s = _settings({})
        boxes = [self._box(1, slots=4, free=2)]
        p = disp.place(self._task(group_slots=9), boxes, [], [], s, 0)
        assert p.action == "pack" and p.target == 1, p
        assert "SERIALISE" in p.reason, p

    def test_renting_buys_a_box_sized_for_the_group_and_says_so_when_it_cannot(self):
        s = _settings({"cores_per_lane": 1, "max_slots_cap": 8})
        small, big = self._offer(1, cores=2.0, dph=0.01), self._offer(2, cores=8.0, dph=0.06)
        t = self._task(group_slots=5, priority=90)          # priority 90 clears the backlog bar
        p = disp.place(t, [], [small, big], [], s, 0)
        assert p.action == "rent" and p.offer["id"] == 2, p  # the cheap 2-lane box cannot hold it
        # ...and with only the small offer on the market it still rents, flagged as serialising.
        p2 = disp.place(t, [], [small], [], s, 0)
        assert p2.action == "rent" and p2.offer["id"] == 1, p2
        assert "SERIALISE" in p2.reason, p2

    # ------------------------------------------------------------------------- the pinned members
    def test_a_pinned_member_holds_for_its_group_box_and_NEVER_RENTS(self):
        """The pin routes through the invariant-4f box-target machinery, so it inherits the rule
        that matters: no rental can BECOME the group's box, and buying one would be a rent-loop
        (three boxes in 16 minutes, none used). Priority 90 removes the backlog bar, so nothing but
        that guard stands between this task and a rental."""
        s = _settings({})
        pinned = self._task(box=7, priority=90)
        p = disp.place(pinned, [], [self._offer()], [pinned] * 5, s, 0)
        assert p.action == "hold" and p.reason.startswith("colocate:"), p
        assert "group 'grp:seed1' is pinned to box 7" in p.reason, p

    def test_a_pinned_member_refuses_a_free_box_that_is_not_its_group_box(self):
        """The point of the feature: an ordinary task takes the free box happily — that is the
        control, and it is the lottery that split published campaigns."""
        s = _settings({})
        elsewhere = [self._box(9, slots=4, free=4)]
        assert disp.place(self._task(key=None), elsewhere, [], [], s, 0).action == "pack"
        p = disp.place(self._task(box=7), elsewhere, [], [], s, 0)
        assert p.action == "hold" and "colocate" in p.reason, p

    def test_a_pinned_member_does_not_inflate_ANOTHER_task_s_backlog_bar(self):
        """Held siblings are not evidence the fleet should rent: no rental can serve them. Same
        class as `_boardable` incident 3 — a targeted task's demand leaking into another branch's
        idea of capacity. Inherited from 4f by construction, and asserted because it is inherited."""
        s = _settings({})
        full = self._box(9, slots=1, free=0)
        ordinary = self._task(tid="o", key=None)
        stuck = [self._task(tid=f"c{i}", box=7) for i in range(5)]
        assert "backlog_too_small" in disp.place(ordinary, [full], [self._offer()], stuck, s, 0).reason
        many = [self._task(tid=f"o{i}", key=None) for i in range(5)]
        assert disp.place(ordinary, [full], [self._offer()], many, s, 0).action == "rent"

    # ------------------------------------------------------- pin resolution against the REAL views
    def test_the_pin_is_stamped_onto_members_and_group_demand_is_summed(self, tmp_path):
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _reg()
        d.conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
                       "hard_cap_at) VALUES (?,?,?,?,?,?,?)",
                       (5, "owned-a", reg.now_iso(), "live", 0.0, 8, reg.now_iso()))
        reg.pin_colocation(d.conn, "g:seed1", 5, "whoever")
        d.conn.commit()
        queued = [self._task(tid="a", key="g:seed1", slots=2),
                  self._task(tid="b", key="g:seed1", slots=3),
                  self._task(tid="c", key="g:seed2"), self._task(tid="d", key=None)]
        pins = d._resolve_colocations(queued, d._instances_view())
        assert pins == {"g:seed1": 5}
        assert disp.box_target(queued[0]["resource_hint"]) == "5"
        assert queued[0]["colocate_group_slots"] == queued[1]["colocate_group_slots"] == 5
        assert queued[2]["colocate_group_slots"] == 1        # a different seed is its own group
        assert disp.box_target(queued[2]["resource_hint"]) is None   # ...and is NOT pinned
        assert queued[3]["resource_hint"] is None            # ordinary traffic is untouched

    def test_a_pin_on_a_dead_box_is_RELEASED_and_the_break_is_logged(self, tmp_path):
        """⛔ THE ALTERNATIVE IS A WEDGED CAMPAIGN. Rented boxes are torn down on idle and at the
        hard cap, so "hold forever for the box that pinned you" turns every reclaimed rental into a
        queue only a human can unstick. Re-pinning is the right degradation — but it is also the
        exact moment co-location BREAKS, so it must be loud and it must be checkable
        (`runq colocate --verify` reads `tasks.instance_id`, not this log)."""
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _reg()
        d.conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
                       "hard_cap_at, destroyed_at) VALUES (?,?,?,?,?,?,?,?)",
                       (6, "gone", reg.now_iso(), "destroyed", 0.05, 8, reg.now_iso(),
                        reg.now_iso()))
        reg.pin_colocation(d.conn, "g:seed1", 6, "whoever")
        reg.pin_colocation(d.conn, "g:seed9", 404, "whoever")   # a box that is not even a row
        d.conn.commit()
        member = self._task(tid="a", key="g:seed1")
        assert d._resolve_colocations([member], d._instances_view()) == {}
        assert reg.colocation_pins(d.conn) == {}
        assert disp.box_target(member["resource_hint"]) is None   # free to re-pin
        events = [r["event"] for r in d.conn.execute("SELECT event FROM events")]
        assert events.count("colocate_unpinned") == 2

    @staticmethod
    def _two_box_fleet(tmp_path, dbname, key):
        """A fleet in which tightest-fit SPLITS an ordinary pair: box 11 has one free lane (so it
        wins the tie and then fills), box 12 has two. Two arms are queued; `key=None` leaves them
        ungrouped. Returns `{task id: instance id}` after one poll."""
        d = disp.Dispatcher(str(tmp_path / dbname), run=_RecordingRun(), vastai_run=_RecordingRun())
        reg = _reg()
        now = reg.now_iso()
        for iid in (11, 12):
            d.conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, "
                           "slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?)",
                           (iid, f"owned-{iid}", now, "live", 0.0, 2, _iso_in_hours(10)))
        hint = json.dumps({"colocate": key}) if key else None
        rows = [("filler", 11, "running", 50, None), ("arm1", None, "queued", 60, hint),
                ("arm2", None, "queued", 50, hint)]
        for tid, iid, state, prio, h in rows:
            reg.insert_task(d.conn, id=tid, created_at=now, created_by="test", grp="g", name=tid,
                            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid,
                            arm_hash=tid, git_sha="x", slots=1, est_minutes=5, priority=prio,
                            max_retries=1, state=state, instance_id=iid, resource_hint_json=h)
        d.conn.commit()
        d._place_queue()
        return d, reg, {r["id"]: r["instance_id"]
                        for r in d.conn.execute("SELECT id, instance_id FROM tasks")}

    def test_ONE_POLL_pins_the_group_and_diverts_the_next_sibling(self, tmp_path):
        """⛔ THE END-TO-END ONE. Two arms, one poll, a fleet whose packer would split them.

        Placing arm 1 changes the view arm 2 is placed against (that is deliberate — invariant 4),
        so tightest-fit fills box 11 with arm 1 and then sends arm 2 to box 12. Both decisions look
        perfectly reasonable in isolation, the pair is split inside a SINGLE pass, and nothing in
        the outputs says so. The control below runs the identical fleet with no group key and
        asserts the split really does happen, so this is a test of the group and not of a packer
        that happened to be sticky."""
        _d2, _r2, solo = self._two_box_fleet(tmp_path, "control.sqlite", key=None)
        assert solo["arm1"] == 11 and solo["arm2"] == 12, solo      # the lottery, reproduced

        d, reg, placed = self._two_box_fleet(tmp_path, "grouped.sqlite", key="g:seed1")
        assert placed["arm1"] == placed["arm2"] == 12, placed
        assert reg.colocation_pins(d.conn)["g:seed1"]["instance_id"] == 12
        # ...and the group went to box 12 BECAUSE it holds both — not by luck: box 11 is the one
        # tightest-fit prefers, and it is the one the group demand rules out.

    def test_a_targeted_task_cannot_PREEMPT_on_a_box_it_may_never_board(self):
        """`place()`'s preempt branch was scoped to `live`, not `boardable` — the fourth branch to
        need that lesson and the one that was missing it. A targeted task evicting a victim on a
        foreign box pays the eviction (checkpoint, requeue, relocate) and still cannot use the slot
        it cleared, then repeats next poll. Latent while `preempt_enabled` ships FALSE and box
        targeting was rare; 4g makes every pinned sibling a targeted task."""
        s = _mech_settings({})                                  # preemption explicitly ON
        foreign = self._box(9, slots=1, free=0, label="somebody-else")
        foreign["occupants"][0]["priority"] = 10                # evictable at margin 30
        hungry = self._task(tid="h", key=None, priority=90)
        assert disp.place(hungry, [foreign], [], [], s, 0).action == "preempt"   # the control
        for pinned in (self._task(tid="p", box=7, priority=90),
                       self._task(tid="q", key=None, priority=90,
                                  resource_hint={"box": "7"})):
            p = disp.place(pinned, [foreign], [], [], s, 0)
            assert p.action == "hold", p

    def test_the_pin_is_written_by_the_CLAIM_not_by_the_DECISION(self, tmp_path):
        """`_apply_placement` returns the instance a pack actually claimed, and only that pins the
        group. A pack whose CAS lost (the row moved under us) must pin nothing — otherwise a group
        is bound to a box no member of it is on."""
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                            vastai_run=_RecordingRun())
        reg = _reg()
        now = reg.now_iso()
        reg.insert_task(d.conn, id="z", created_at=now, created_by="test", grp="g", name="z",
                        entrypoint="smoke", args_json="[]", config_json="{}", config_hash="z",
                        arm_hash="z", git_sha="x", slots=1, est_minutes=5, priority=50,
                        max_retries=1, state="done")     # terminal: `queued -> claimed` is illegal
        d.conn.commit()
        task = d._task_view(dict(d.conn.execute("SELECT * FROM tasks WHERE id='z'").fetchone()))
        p = disp.Placement("pack", 11, None, "test")
        assert d._apply_placement(task, p, []) is None


def _iso_in_hours(h):
    import datetime
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestLiveCostBooking:
    """Invariant 12b — `cost_usd` accrues on LIVE boxes every cycle, not only at teardown.

    Before this, `cost_usd` was stamped once at destroy/lost, so every running box read NULL and a
    spend query saw a spike only after it ended. Measured 2026-07-31: the registry reported $5.59
    for the day while the fleet burned $0.859/hr ($20.61/day), with $11.05 accrued and invisible
    across 13 live boxes."""

    def _d(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def test_books_cost_on_a_live_paid_box(self, tmp_path, monkeypatch):
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        d.conn.execute(
            "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
            "VALUES (7,'runq_x',datetime('now','-4 hours'),'live',0.25,4,?)", (reg.now_iso(),))
        d.conn.commit()
        assert d.conn.execute("SELECT cost_usd FROM instances WHERE id=7").fetchone()[0] is None

        d._book_live_costs()
        cost = d.conn.execute("SELECT cost_usd FROM instances WHERE id=7").fetchone()[0]
        assert cost == pytest.approx(1.0, abs=0.05)  # 4h x $0.25/hr

    def test_booking_is_idempotent_not_incremental(self, tmp_path, monkeypatch):
        """Recomputed from dph x elapsed, never added to — so repeated cycles and a later teardown
        stamp all converge on the same number instead of compounding."""
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        d.conn.execute(
            "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
            "VALUES (8,'runq_y',datetime('now','-2 hours'),'live',0.10,4,?)", (reg.now_iso(),))
        d.conn.commit()
        d._book_live_costs()
        first = d.conn.execute("SELECT cost_usd FROM instances WHERE id=8").fetchone()[0]
        for _ in range(3):
            d._book_live_costs()
        again = d.conn.execute("SELECT cost_usd FROM instances WHERE id=8").fetchone()[0]
        assert again == pytest.approx(first, abs=0.01)

    def test_owned_free_boxes_and_terminal_rows_are_left_alone(self, tmp_path, monkeypatch):
        """An owned box bills $0 (invariant 20), and a destroyed row already carries its final
        stamped cost — re-deriving it from `now` would inflate it forever."""
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        d.conn.execute(
            "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at,source)"
            " VALUES (-1,'desktop',datetime('now','-9 hours'),'live',0.0,12,?,'owned')",
            (reg.now_iso(),))
        d.conn.execute(
            "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at,"
            "destroyed_at,cost_usd) VALUES (9,'runq_z',datetime('now','-9 hours'),'destroyed',"
            "0.10,4,?,?,0.42)", (reg.now_iso(), reg.now_iso()))
        d.conn.commit()
        d._book_live_costs()
        assert d.conn.execute("SELECT cost_usd FROM instances WHERE id=-1").fetchone()[0] is None
        assert d.conn.execute("SELECT cost_usd FROM instances WHERE id=9").fetchone()[0] == 0.42


class TestInFlightEstRecalibration:
    """Invariant 24 — scheduling uses the LEARNED per-group estimate once a campaign has finished
    siblings, so a mis-declared `est_minutes` self-corrects without a CLI, a commit or a restart.

    Measured 2026-07-31: fleet median actual/est was 0.37 over 1381 done tasks (26 of 48 groups
    over-estimating by >2x, the native line at 0.07-0.25). `est_minutes x est_safety` is what the
    dispatcher believes a lane stays busy for, so a 2.7x over-estimate makes it rent instead of
    wait — 56% of boxes destroyed in 3 days never ran a single task."""

    def _d(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _task(self, reg, conn, tid, grp, state, est=600):
        reg.insert_task(conn, id=tid, created_at=reg.now_iso(), created_by="test", grp=grp,
                        name=tid, entrypoint="e", args_json="[]", config_json="{}",
                        config_hash=tid, arm_hash=tid, git_sha="deadbeef", slots=1,
                        est_minutes=est, priority=50, max_retries=1, state=state)

    def _done_sibling(self, reg, conn, tid, grp, minutes):
        self._task(reg, conn, tid, grp, "done")
        conn.execute("INSERT INTO events(t,task_id,event,detail) VALUES "
                     "(datetime('now','-1 day'),?,'start','')", (tid,))
        conn.execute("INSERT INTO events(t,task_id,event,detail) VALUES "
                     f"(datetime('now','-1 day','+{minutes} minutes'),?,'done','')", (tid,))
        conn.commit()

    def test_learned_value_replaces_a_stale_declared_estimate(self, tmp_path, monkeypatch):
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        for i, m in enumerate((30, 32, 34, 36)):
            self._done_sibling(reg, d.conn, f"s{i}", "campaign", m)
        self._task(reg, d.conn, "open1", "campaign", "queued")
        d.conn.commit()

        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='open1'").fetchone())
        assert row["est_minutes"] == 600, "declared estimate unchanged in the DB"
        assert d._effective_est(row) < 60, "scheduling should use the learned ~36 min, not 600"

    def test_declared_estimate_stands_when_there_are_too_few_siblings(self, tmp_path, monkeypatch):
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        self._done_sibling(reg, d.conn, "s0", "fresh", 30)  # 1 < LIVE_MIN_SAMPLE
        self._task(reg, d.conn, "open2", "fresh", "queued")
        d.conn.commit()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='open2'").fetchone())
        assert d._effective_est(row) == 600

    def test_a_sibling_in_ANOTHER_group_does_not_leak_across(self, tmp_path, monkeypatch):
        """The whole point of keying on group: a short campaign must not re-estimate a long one."""
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        for i, m in enumerate((5, 6, 7, 8)):
            self._done_sibling(reg, d.conn, f"q{i}", "short", m)
        self._task(reg, d.conn, "open3", "long", "queued")
        d.conn.commit()
        row = dict(d.conn.execute("SELECT * FROM tasks WHERE id='open3'").fetchone())
        assert d._effective_est(row) == 600

    def test_the_row_is_never_rewritten(self, tmp_path, monkeypatch):
        """The declared `est_minutes` is part of the queue-time contract (the `--probe` bound and
        the resume contract were checked against it at `runq add`). Correcting scheduling must not
        retroactively edit a decision the operator already made."""
        reg = _reg()
        d = self._d(tmp_path, monkeypatch)
        for i, m in enumerate((10, 11, 12)):
            self._done_sibling(reg, d.conn, f"r{i}", "camp2", m)
        self._task(reg, d.conn, "open4", "camp2", "queued")
        d.conn.commit()
        d._task_view(dict(d.conn.execute("SELECT * FROM tasks WHERE id='open4'").fetchone()))
        assert d.conn.execute(
            "SELECT est_minutes FROM tasks WHERE id='open4'").fetchone()[0] == 600


class TestGpuCeilingReachesTheLiveRegistry:
    """Invariant 4f's migration — the cap is worthless if it never reaches a seeded registry.

    CAUGHT THE HARD WAY 2026-07-31: after `DEFAULT_SETTINGS["max_instance_dph"]` was changed to
    0.08, a smoke test against a COPY of the live registry still read **0.4** — `_ensure_settings`
    only INSERTs keys it lacks, so the running fleet would have kept renting $0.17 V100s forever and
    the whole cap would have been a silent no-op. Asserting the tuple is in `_SETTING_MIGRATIONS` is
    NOT enough; this drives the real restart path."""

    def test_migration_actually_moves_a_seeded_registry(self, tmp_path):
        db = str(tmp_path / "runs.sqlite")
        d1 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())
        d1.conn.execute("UPDATE settings SET value=? WHERE key=?", ("0.4", "max_instance_dph"))
        d1.conn.commit()
        d2 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())  # a restart
        assert float(d2.settings["max_instance_dph"]) == 0.08, (
            f"the GPU-class cap did not reach the live registry "
            f"({d2.settings['max_instance_dph']}) — the fleet would keep booking dear boxes it "
            "measured at 24% usable while the code default claims otherwise")

    def test_a_hand_set_value_is_NOT_stomped(self, tmp_path):
        """The migration is guarded on the OLD value, so an operator who deliberately set something
        else keeps it — the cap must not fight a human who raised it for a big-card campaign."""
        db = str(tmp_path / "runs.sqlite")
        d1 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())
        d1.conn.execute("UPDATE settings SET value=? WHERE key=?", ("0.25", "max_instance_dph"))
        d1.conn.commit()
        d2 = disp.Dispatcher(db, run=_RecordingRun(), vastai_run=_RecordingRun())
        assert float(d2.settings["max_instance_dph"]) == 0.25
class TestIngestPayloadsRunOneThreadPerBox:
    """Invariant 23c: the per-task payload pulls (`tb`, `checkpoints`) fan out ONE WORKER PER BOX.

    MEASURED 2026-07-31: they were 762.6s of a 962s ingest (79%), which was 65% of a 29.5-min poll
    cycle. Throughput is ~0.3-0.62 MB/s PER BOX against a 1 Gb/s home uplink, so the ceiling is each
    box's own network path — concurrency across boxes is ~linear, concurrency within one box buys
    nothing. Hence per-box workers, serial inside.

    The safety property that makes it legal: the threaded halves are PURE I/O. `registry_db.connect`
    omits `check_same_thread`, so any DB access off-thread raises outright — which is exactly what
    `test_the_threaded_half_never_touches_the_db` relies on to prove the split is real.
    """

    def _box(self, tmp_path, monkeypatch, n_boxes=3, n_tasks=2):
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        reg = None
        for b in range(n_boxes):
            for t in range(n_tasks):
                tid = f"P{b}_{t}"
                if reg is None:
                    reg = _seed_instance_and_task(d.conn, task_id=tid, instance_id=b + 1)
                elif t == 0:
                    reg = _seed_instance_and_task(d.conn, task_id=tid, instance_id=b + 1)
                else:
                    reg.insert_task(
                        d.conn, id=tid, created_at=reg.now_iso(), created_by="test", grp="g",
                        name=tid, entrypoint="smoke", args_json="[]", config_json="{}",
                        config_hash=tid, arm_hash=tid, git_sha="x", slots=1, est_minutes=1,
                        priority=50, max_retries=1, state="claimed", instance_id=b + 1)
                d.conn.execute("UPDATE tasks SET state='running' WHERE id=?", (tid,))
        d.conn.commit()
        # Both halves of worker_state/markers are stubbed (2026-08-02: these gained an `_io` split
        # so they could fan out too — see TestWorkerStateAndMarkersFanOut). This class measures the
        # PAYLOAD pass, so it must keep them out of the timing entirely.
        monkeypatch.setattr(d, "_pull_worker_state_io", lambda *a, **k: (True, True))
        for r in ("_pull_worker_state", "_pull_markers", "_pull_markers_io",
                  "_apply_worker_state", "_apply_markers"):
            monkeypatch.setattr(d, r, lambda *a, **k: None)
        for r in ("_reap_stalled", "_reap_dead_workers", "_reap_orphaned_tasks",
                  "_reap_overpacked_boxes", "_reap_unclaimed_ships", "_reap_undeliverable_claims",
                  "_reap_unreachable_owned", "_reap_paused_soft_timeout"):
            monkeypatch.setattr(d, r, lambda *a, **k: None)
        return d

    def test_boxes_are_pulled_concurrently_and_wall_beats_the_sum(self, tmp_path, monkeypatch):
        d = self._box(tmp_path, monkeypatch, n_boxes=4, n_tasks=2)
        threads, lock = set(), threading.Lock()

        def slow_pull(*a, **k):
            with lock:
                threads.add(threading.get_ident())
            time.sleep(0.05)
            return True

        monkeypatch.setattr(disp, "rsync_pull", slow_pull)
        d._ingest_and_complete()
        det = d._ingest_detail
        assert det["payload_boxes"] == 4
        assert len(threads) > 1, "payload pulls all ran on one thread — the fan-out is not working"
        work = sum(det["payload_work_sec"].values())
        assert det["sec"]["payloads_wall"] < work, \
            f"wall {det['sec']['payloads_wall']} not below summed work {work} — no parallelism"
        assert det["payload_speedup"] and det["payload_speedup"] > 1.0

    def test_setting_the_width_to_one_keeps_the_serial_path(self, tmp_path, monkeypatch):
        """`ingest_parallel_boxes=1` must fall back to a plain loop — the escape hatch if the
        fan-out ever misbehaves on a live fleet, and it must stay tested, not just present."""
        d = self._box(tmp_path, monkeypatch, n_boxes=3, n_tasks=1)
        d.settings["ingest_parallel_boxes"] = 1
        threads, lock = set(), threading.Lock()

        def pull(*a, **k):
            with lock:
                threads.add(threading.get_ident())
            return True

        monkeypatch.setattr(disp, "rsync_pull", pull)
        d._ingest_and_complete()
        assert threads == {threading.get_ident()}, "width=1 still spawned worker threads"
        assert d._ingest_detail["payload_boxes"] == 3

    def test_the_threaded_half_never_touches_the_db(self, tmp_path, monkeypatch):
        """THE safety property. sqlite raises `ProgrammingError` when a connection opened on one
        thread is used from another, so running the thread body off-main is a real assertion that
        the pure-I/O split holds — not a comment claiming it does. `_pull_box_payloads` catches its
        own exceptions into `error`, so a leaked DB call shows up there rather than as a crash."""
        d = self._box(tmp_path, monkeypatch, n_boxes=1, n_tasks=2)
        monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
        tasks = d._running_on(1)
        out = {}

        def body():
            out["res"] = d._pull_box_payloads("h", 22, tasks, ckpt_due=True)

        th = threading.Thread(target=body)
        th.start()
        th.join(timeout=30)
        assert out["res"]["error"] is None, f"threaded half touched the DB: {out['res']['error']}"
        assert len(out["res"]["tb"]) == 2 and len(out["res"]["ckpt"]) == 2

    def test_one_box_blowing_up_does_not_lose_the_other_boxes(self, tmp_path, monkeypatch):
        """A box whose pull raises must not take the whole ingest phase with it — the failure is
        recorded per box and every other box's payloads still apply."""
        d = self._box(tmp_path, monkeypatch, n_boxes=3, n_tasks=1)

        def flaky(host, port, *a, **k):
            if host == "example.com" and getattr(flaky, "n", 0) < 1:
                flaky.n = getattr(flaky, "n", 0) + 1
                raise OSError("box exploded")
            return True

        monkeypatch.setattr(disp, "rsync_pull", flaky)
        d._ingest_and_complete()          # must not raise
        events = [r[0] for r in d.conn.execute("SELECT event FROM events")]
        assert "ingest_box_failed" in events
        assert d._ingest_detail["payload_boxes"] == 3

    def test_resume_checkpoint_is_still_recorded_through_the_parallel_path(self, tmp_path,
                                                                           monkeypatch):
        """End-to-end behaviour preservation: the 2026-07-15 fix (descend into out/<tag>/ and record
        whatever checkpoint landed) must survive the split, since a lost `resume_checkpoint` silently
        restarts a run from scratch on the next infra requeue."""
        d = self._box(tmp_path, monkeypatch, n_boxes=1, n_tasks=1)

        def landing_pull(host, port, remote, dest, includes, append=False, run=None):
            if "ckpt_latest.pt" in includes:
                sub = Path(dest) / "some_tag"
                sub.mkdir(parents=True, exist_ok=True)
                (sub / "ckpt_latest.pt").write_bytes(b"weights")
            return True

        monkeypatch.setattr(disp, "rsync_pull", landing_pull)
        d._ingest_and_complete()
        got = d.conn.execute("SELECT resume_checkpoint FROM tasks WHERE id='P0_0'").fetchone()[0]
        assert got and got.endswith("some_tag/ckpt_latest.pt"), got


class TestWorkerStateAndMarkersFanOut:
    """Invariant 23e (2026-08-02): the `worker_state` and `markers` pulls fan out across boxes too,
    with every mutation still applied serially.

    THE MEASUREMENT. Over 400 live poll cycles the total was 96s median / 195s p90 / 415s max
    against `poll_seconds=30`, ingest was 70% of it, and ingest cost ~9-15 SECONDS PER BOX — a
    straight line in fleet size (2 boxes -> 3.5s median cycle, 11 boxes -> 355s). The payload pass
    had already been parallelised (invariant 23c); these two sub-phases had not, and were ~29% of
    ingest: three latency-bound round trips per box, run strictly one box after another.

    The safety property is the same one 23c relies on: the threaded half is PURE I/O, and
    `registry_db.connect` omits `check_same_thread`, so any DB access off-thread raises outright."""

    def _d(self, tmp_path, monkeypatch, n_boxes=4):
        monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
        run = _RecordingRun()
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        for b in range(n_boxes):
            _seed_instance_and_task(d.conn, task_id=f"W{b}", instance_id=b + 1)
            d.conn.execute("UPDATE tasks SET state='running' WHERE id=?", (f"W{b}",))
        d.conn.commit()
        for r in ("_reap_stalled", "_reap_dead_workers", "_reap_orphaned_tasks",
                  "_reap_overpacked_boxes", "_reap_unclaimed_ships", "_reap_undeliverable_claims",
                  "_reap_unreachable_owned", "_reap_paused_soft_timeout"):
            monkeypatch.setattr(d, r, lambda *a, **k: None)
        monkeypatch.setattr(d, "_pull_box_payloads", lambda *a, **k: {
            "tb": [], "ckpt": [], "error": None, "tb_sec": 0.0, "ckpt_sec": 0.0})
        return d

    def test_the_pulls_run_concurrently_so_wall_beats_the_serial_sum(self, tmp_path, monkeypatch):
        d = self._d(tmp_path, monkeypatch, n_boxes=4)
        threads, lock = set(), threading.Lock()

        def slow(inst, host, port):
            with lock:
                threads.add(threading.get_ident())
            time.sleep(0.25)
            return (True, True)

        monkeypatch.setattr(d, "_pull_worker_state_io", slow)
        monkeypatch.setattr(d, "_apply_worker_state", lambda *a, **k: None)
        monkeypatch.setattr(d, "_pull_markers_io", lambda *a, **k: None)
        monkeypatch.setattr(d, "_apply_markers", lambda *a, **k: None)
        t0 = time.monotonic()
        d._ingest_and_complete()
        wall = time.monotonic() - t0
        assert len(threads) > 1, "worker_state pulls did not fan out across boxes"
        assert wall < 4 * 0.25, f"wall {wall:.2f}s did not beat the serial sum of 1.0s"

    def test_the_threaded_half_never_touches_the_db(self, tmp_path, monkeypatch):
        """The split is only legal because the parallel half is pure I/O. sqlite's own
        cross-thread check is the enforcement — so this fails loudly if a DB read creeps in."""
        d = self._d(tmp_path, monkeypatch, n_boxes=3)
        seen = {}

        def io(inst, host, port):
            try:
                d.conn.execute("SELECT 1").fetchone()
                seen[inst["id"]] = "TOUCHED THE DB"
            except Exception as e:                       # noqa: BLE001
                seen[inst["id"]] = type(e).__name__
            return (True, True)

        monkeypatch.setattr(d, "_pull_worker_state_io", io)
        monkeypatch.setattr(d, "_apply_worker_state", lambda *a, **k: None)
        monkeypatch.setattr(d, "_pull_markers_io", lambda *a, **k: None)
        monkeypatch.setattr(d, "_apply_markers", lambda *a, **k: None)
        d._ingest_and_complete()
        assert seen and all(v == "ProgrammingError" for v in seen.values()), seen

    def test_one_box_raising_does_not_lose_the_others(self, tmp_path, monkeypatch):
        """A single unreachable box must not take the phase — and so every other box's
        `shipped -> running` transitions and terminal completions — down with it."""
        d = self._d(tmp_path, monkeypatch, n_boxes=3)
        applied = []

        def io(inst, host, port):
            if inst["id"] == 2:
                raise OSError("box exploded")
            return (True, True)

        monkeypatch.setattr(d, "_pull_worker_state_io", io)
        monkeypatch.setattr(d, "_apply_worker_state",
                            lambda inst, *a: applied.append(inst["id"]))
        monkeypatch.setattr(d, "_pull_markers_io", lambda *a, **k: None)
        monkeypatch.setattr(d, "_apply_markers", lambda *a, **k: None)
        d._ingest_and_complete()                          # must not raise
        assert applied == [1, 3], applied                 # the healthy boxes still applied
        events = [r[0] for r in d.conn.execute("SELECT event FROM events")]
        assert "ingest_box_failed" in events              # and the failure left a trace

    def test_mutations_are_applied_on_the_main_thread_in_instance_order(self, tmp_path, monkeypatch):
        """Order matters for reproducibility of the event log, and `as_completed` returns in
        COMPLETION order — so the apply loop must re-walk `insts`, not the futures."""
        d = self._d(tmp_path, monkeypatch, n_boxes=4)
        main, order = threading.get_ident(), []
        # finish in REVERSE instance order, so a futures-ordered apply would be detectable
        monkeypatch.setattr(d, "_pull_worker_state_io",
                            lambda inst, h, p: (time.sleep(0.05 * (4 - inst["id"])), (True, True))[1])
        monkeypatch.setattr(d, "_apply_worker_state",
                            lambda inst, *a: order.append((inst["id"], threading.get_ident())))
        monkeypatch.setattr(d, "_pull_markers_io", lambda *a, **k: None)
        monkeypatch.setattr(d, "_apply_markers", lambda *a, **k: None)
        d._ingest_and_complete()
        assert [i for i, _ in order] == [1, 2, 3, 4], order
        assert all(t == main for _, t in order), "a mutation ran off the main thread"


# ----------------------------------------------------------------- invariant 25: box probe parsing
# The probe is KEYED, not positional (see `BOX_PROBE_CMD`), so these fixtures deliberately include
# the stray-stdout case that broke the positional draft.

class TestParseBoxProbe:
    V2 = ("NPROC 24\n"
          "LOAD 5.22 5.45 5.88 7/494 16474\n"
          "MEM 64202 54477\n"
          "GPU 12288, 1304, 3, 0, NVIDIA GeForce RTX 3060\n"
          "CPUQ 23.0400\n"
          "CPUU 244656095664\n"
          "MEMCG 14525016784 64626884608\n"
          "MEMANON 6341234567\n")

    def test_v2_full(self):
        m = disp.parse_box_probe(self.V2)
        assert m["cores"] == 24 and m["load1"] == 5.22
        assert m["cpu_quota_cores"] == 23.04
        assert m["cpu_usage_usec"] == 244656095664
        assert round(m["mem_limit_gb"], 1) == 60.2
        assert round(m["mem_anon_gb"], 1) == 5.9
        assert m["gpu_util"] == 3.0 and m["gpu_mem_util"] == 0.0
        assert m["gpu_name"] == "NVIDIA GeForce RTX 3060"
        assert m["gpu_count"] == 1
        assert round(m["vram_total_gb"], 1) == 12.0

    def test_host_basis_fields_keep_pre_24_meaning(self):
        """These four keep their HOST meaning at the PARSER. 24 must not have moved them.

        ⚠ Since invariant 28 (2026-08-02) `box_headroom` no longer admits against these four
        directly — it prefers the cgroup pair and falls back to these (`TestContainerTrueHeadroom`).
        The parse contract below is unchanged and is what that fallback depends on."""
        m = disp.parse_box_probe(self.V2)
        assert m["cores"] == 24                        # nproc, NOT the cgroup quota
        assert m["load1"] == 5.22                      # host run queue
        assert round(m["ram_total_gb"], 2) == round(64202 / 1024, 2)
        assert round(m["ram_avail_gb"], 2) == round(54477 / 1024, 2)

    def test_nvml_failure_prose_on_stdout_does_not_shift_fields(self):
        """Regression: `nvidia-smi` prints its failure to STDOUT on the NVML-blocked owned box.

        A positional parser consumed those four lines as the cgroup fields and reported 520 GB of
        anonymous memory on a 31 GB machine (live, 2026-07-31). Keyed lines make them inert."""
        text = ("NPROC 20\n"
                "LOAD 6.15 6.02 6.02 5/2578 56121\n"
                "MEM 32041 15785\n"
                "GPU Failed to initialize NVML: GPU access blocked by the operating system\n"
                "GPU Failed to properly shut down NVML: GPU access blocked by the operating system\n"
                "GPU \n"
                "CPUU 558650769667\n"
                "MEMCG 1979158528 max\n"
                "MEMANON 1118560256\n")
        m = disp.parse_box_probe(text)
        assert m is not None
        assert round(m["mem_anon_gb"], 2) == 1.04       # NOT 520
        assert m["cpu_usage_usec"] == 558650769667
        assert m["gpu_util"] is None and m["gpu_count"] is None   # no parseable GPU reading
        assert m["mem_limit_gb"] is None                # "max" == unlimited
        assert round(m["mem_used_gb"], 2) == 1.84

    def test_v1_cgroup_layout(self):
        m = disp.parse_box_probe("NPROC 96\nLOAD 23.14 1 1 1/1 1\nMEM 96402 36352\n"
                                 "CPUQ 18.4320\nCPUU 5474313154\n"
                                 "MEMCG 4157503897 43397414912\nMEMANON 3189302067\n")
        assert m["cpu_quota_cores"] == 18.432
        assert round(m["mem_limit_gb"], 1) == 40.4

    def test_v1_unlimited_memory_sentinel_reads_as_absent(self):
        m = disp.parse_box_probe("NPROC 8\nLOAD 1 1 1 1/1 1\nMEM 1024 512\n"
                                 "MEMCG 1073741824 9223372036854771712\n")
        assert m["mem_limit_gb"] is None
        assert round(m["mem_used_gb"], 2) == 1.0

    def test_missing_cgroup_lines_degrade_to_host_only(self):
        m = disp.parse_box_probe("NPROC 8\nLOAD 2.0 1 1 1/1 1\nMEM 16384 8192\n")
        assert m["cores"] == 8 and m["load1"] == 2.0
        for k in ("cpu_quota_cores", "cpu_usage_usec", "mem_limit_gb", "mem_used_gb",
                  "mem_anon_gb", "gpu_name", "gpu_count"):
            assert m[k] is None, k

    def test_multi_gpu_reports_count_but_keeps_first_gpu_vram(self):
        """The VRAM gate is frozen on the FIRST card, so a 2-GPU box admits exactly as before."""
        m = disp.parse_box_probe("NPROC 8\nLOAD 1 1 1 1/1 1\nMEM 1024 512\n"
                                 "GPU 12288, 100, 5, 1, RTX 3060\nGPU 24576, 200, 7, 2, RTX 4090\n")
        assert m["gpu_count"] == 2
        assert round(m["vram_total_gb"], 1) == 12.0
        assert m["gpu_name"] == "RTX 3060"

    def test_unusable_cpu_ram_returns_none(self):
        assert disp.parse_box_probe("") is None
        assert disp.parse_box_probe("NPROC 8\nLOAD 1 1 1 1/1 1\n") is None   # no MEM
        assert disp.parse_box_probe("garbage\nmore garbage\n") is None


class TestContainerTrueHeadroom:
    """Invariant 28 — `box_headroom` gates on OUR container's cgroup, not the host.

    Numbers are the real fleet measurements taken at the fix (2026-08-02); `m100001` is the
    192-core shared host that motivated it."""

    M100001 = {"cores": 192, "load1": 32.45, "ram_avail_gb": 412.8,
               "cpu_quota_cores": 23.04, "cpu_used_cores": 0.0,
               "mem_limit_gb": 171.0, "mem_used_gb": 0.2, "mem_anon_gb": 0.0,
               "vram_total_gb": None, "at": 1000.0}

    @staticmethod
    def _inst(measured, **kw):
        return {"measured": measured, "occupants": [], **kw}

    def _hr(self, measured, settings=None, **kw):
        s = {"headroom_cpu_reserve": 1.0, "headroom_ram_reserve_gb": 2.0,
             "headroom_max_stale_min": 15, **(settings or {})}
        return disp.box_headroom(self._inst(measured, **kw), s, now=1000.0)

    def test_container_pair_replaces_the_host_pair(self):
        """The whole point: 192 - 32.45 = 159.6 free cores was a ~7x over-report."""
        hr = self._hr(self.M100001)
        assert hr["cores"] == pytest.approx(23.04 - 0.0 - 1.0)      # 22.04, not 158.6
        assert hr["ram_gb"] == pytest.approx(171.0 - 0.0 - 2.0)     # 169.0, not 410.8

    def test_never_mixes_container_quota_with_host_load(self):
        """⛔ The dangerous combination. quota 23.04 - host load1 32.45 = -9.4 would refuse every
        task on a box whose own container was burning 0.00 cores."""
        hr = self._hr(self.M100001)
        assert hr["cores"] > 0, "mixed denominators would make an idle box look overloaded"

    def test_first_sample_falls_back_to_the_host_pair(self):
        """`cpu_used_cores` is a RATE — None on a box's first probe and after a restart. The host
        pair stands in; the axis must NOT abstain and must NOT read used-as-zero."""
        m = {**self.M100001, "cpu_used_cores": None}
        assert self._hr(m)["cores"] == pytest.approx(192 - 32.45 - 1.0)

    def test_owned_box_with_no_cgroup_limit_is_unchanged(self):
        """Both owned boxes report quota None / limit None — bare metal, not containers. There the
        host numbers ARE the container's, so this fix must be a no-op for them."""
        m = {"cores": 20, "load1": 8.31, "ram_avail_gb": 13.9,
             "cpu_quota_cores": None, "cpu_used_cores": 4.02,
             "mem_limit_gb": None, "mem_used_gb": 2.2, "mem_anon_gb": 1.5,
             "vram_total_gb": None, "at": 1000.0}
        hr = self._hr(m)
        assert hr["cores"] == pytest.approx(20 - 8.31 - 1.0)
        assert hr["ram_gb"] == pytest.approx(13.9 - 2.0)

    def test_ram_uses_anon_not_memory_current(self):
        """40000045: `memory.current` 19.2 GB of which 10.7 GB is reclaimable page cache. Charging
        cache as used would hide 10.7 GB of genuinely free RAM."""
        m = {**self.M100001, "mem_limit_gb": 60.1, "mem_used_gb": 19.2, "mem_anon_gb": 8.5}
        assert self._hr(m)["ram_gb"] == pytest.approx(60.1 - 8.5 - 2.0)

    def test_ram_falls_back_to_current_then_host(self):
        no_anon = {**self.M100001, "mem_limit_gb": 60.1, "mem_used_gb": 19.2, "mem_anon_gb": None}
        assert self._hr(no_anon)["ram_gb"] == pytest.approx(60.1 - 19.2 - 2.0)
        unlimited = {**self.M100001, "mem_limit_gb": None, "ram_avail_gb": 55.3}
        assert self._hr(unlimited)["ram_gb"] == pytest.approx(55.3 - 2.0)

    def test_capacity_window_still_binds_over_the_container_quota(self):
        """The operator's allowance is a policy bound ON TOP of the measurement, not replaced."""
        hr = self._hr(self.M100001, resource_cap={"cores": 10.0, "vram_gb": 6.0})
        assert hr["cores"] == pytest.approx(10.0 - 0.0 - 1.0)

    def test_pending_occupants_are_still_charged(self):
        """`_pending_footprint` covers admitted-but-not-yet-measured work; 28 must not drop it."""
        inst = self._inst(self.M100001)
        inst["occupants"] = [{"state": "shipped", "cores": 4.0, "ram_gb": 8.0, "vram_gb": 0.0}]
        s = {"headroom_cpu_reserve": 1.0, "headroom_ram_reserve_gb": 2.0,
             "headroom_max_stale_min": 15}
        hr = disp.box_headroom(inst, s, now=1000.0)
        assert hr["cores"] == pytest.approx(23.04 - 0.0 - 4.0 - 1.0)
        assert hr["ram_gb"] == pytest.approx(171.0 - 0.0 - 8.0 - 2.0)

    def test_vram_stays_whole_device(self):
        """A GPU is not cgroup-namespaced and a co-tenant's allocation really does deny us memory,
        so VRAM is deliberately NOT switched to a container basis."""
        m = {**self.M100001, "vram_total_gb": 12.0, "vram_used_gb": 3.0}
        assert self._hr(m, settings={"headroom_vram_reserve_gb": 1.0})["vram_gb"] == \
            pytest.approx(12.0 - 3.0 - 1.0)

    def test_stale_measurement_still_abstains(self):
        assert disp.box_headroom(self._inst(self.M100001),
                                 {"headroom_max_stale_min": 15}, now=1000.0 + 16 * 60) is None


class TestBoxResSeeding:
    """Invariant 28b — a restart must not silently drop the fleet back to the HOST CPU basis.

    `cpu_used_cores` is a rate differenced against the prior sample, and `_box_res` is in-memory, so
    without seeding EVERY box reads `None` after a restart and `_cpu_pair` falls back. Measured live
    2026-08-02: all 6 cgroup-constrained boxes at once."""

    @staticmethod
    def _mk(tmp_path):
        """A fresh Dispatcher over the SAME db file — i.e. exactly a restart."""
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    @staticmethod
    def _measured(d, inst_id, usec, age_s, raw=None):
        """Write one `box_measured` event `age_s` ago on a live box, as the daemon would."""
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        t = (now - _dt.timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # `hard_cap_at` is NOT NULL with no default — omit it and INSERT OR IGNORE swallows the row
        # silently, which is exactly how this helper was wrong the first time.
        d.conn.execute(
            "INSERT OR IGNORE INTO instances(id,label,created_at,state,dph_usd,slots_total,"
            "hard_cap_at) VALUES (?,?,?,?,?,?,?)",
            (inst_id, f"b{inst_id}", t, "live", 0.05, 8, t))
        detail = raw if raw is not None else (
            "cpu 1.00/23.04 cores, ram 50.0GB free, no-gpu | "
            + json.dumps({"v": 1, "cores": 192, "load1": 32.45,
                          "cpu_quota_cores": 23.04, "cpu_usage_usec": usec}))
        d.conn.execute(
            "INSERT INTO events(t,task_id,instance_id,event,detail) VALUES (?,?,?,?,?)",
            (t, None, inst_id, "box_measured", detail))
        d.conn.commit()

    def test_counter_is_persisted_on_the_event(self):
        """The seed is only possible because the RAW counter rides on `box_measured`."""
        import inspect
        src = inspect.getsource(disp.Dispatcher._box_perf_payload)
        assert "cpu_usage_usec" in src, "the raw counter must be persisted, not just the rate"

    def test_seed_restores_the_previous_counter(self, tmp_path):
        d = self._mk(tmp_path)
        self._measured(d, inst_id=1, usec=1_000_000_000, age_s=60)
        d2 = self._mk(tmp_path)                      # <- the restart
        assert 1 in d2._box_res
        assert d2._box_res[1]["cpu_usage_usec"] == 1_000_000_000

    def test_seed_is_discarded_when_too_old(self, tmp_path):
        """A rate across a long outage is an AVERAGE over it — it understates a box that has since
        got busy, and understating usage OVERSTATES headroom (the over-admit direction)."""
        d = self._mk(tmp_path)
        self._measured(d, inst_id=1, usec=1_000_000_000, age_s=3 * 5 * 60)  # > 2x the 5-min cadence
        assert 1 not in self._mk(tmp_path)._box_res

    def test_seed_survives_a_payloadless_pre_25_sample(self, tmp_path):
        d = self._mk(tmp_path)
        self._measured(d, inst_id=1, usec=None, age_s=60, raw="load 1.0/8 cores, no payload")
        assert 1 not in self._mk(tmp_path)._box_res  # nothing to seed from, and no crash

    def test_only_live_boxes_are_seeded(self, tmp_path):
        d = self._mk(tmp_path)
        self._measured(d, inst_id=1, usec=1_000_000_000, age_s=60)
        d.conn.execute("UPDATE instances SET state='destroyed' WHERE id=1")
        d.conn.commit()
        assert 1 not in self._mk(tmp_path)._box_res

    def test_seeded_rate_then_yields_the_container_basis(self, tmp_path):
        """End to end: seed -> the very next sample produces a rate -> `_cpu_pair` goes container."""
        prev = {"cpu_usage_usec": 1_000_000_000, "at": 100.0}
        cur = {"cpu_usage_usec": 1_060_000_000, "at": 160.0,
               "cores": 192, "load1": 32.45, "cpu_quota_cores": 23.04}
        cur["cpu_used_cores"] = disp._cpu_used_cores(prev, cur)
        assert cur["cpu_used_cores"] == pytest.approx(1.0)
        assert disp._cpu_pair(cur) == (23.04, pytest.approx(1.0))   # container, not (192, 32.45)


class TestCpuUsedCores:
    def test_rate_from_two_samples(self):
        prev = {"cpu_usage_usec": 1_000_000_000, "at": 100.0}
        cur = {"cpu_usage_usec": 1_060_000_000, "at": 120.0}   # 60 cpu-sec over 20 wall-sec
        assert disp._cpu_used_cores(prev, cur) == pytest.approx(3.0)

    def test_no_previous_sample_is_none_not_zero(self):
        """A restart must not report every box as idle — that is a claim, not an absence."""
        assert disp._cpu_used_cores({}, {"cpu_usage_usec": 5, "at": 1.0}) is None

    def test_counter_reset_is_none(self):
        prev = {"cpu_usage_usec": 9_000_000_000, "at": 100.0}
        cur = {"cpu_usage_usec": 10_000, "at": 160.0}          # container recreated
        assert disp._cpu_used_cores(prev, cur) is None

    def test_degenerate_interval_is_none(self):
        prev = {"cpu_usage_usec": 1_000, "at": 100.0}
        assert disp._cpu_used_cores(prev, {"cpu_usage_usec": 2_000, "at": 100.5}) is None

    def test_missing_counter_is_none(self):
        prev = {"cpu_usage_usec": None, "at": 100.0}
        assert disp._cpu_used_cores(prev, {"cpu_usage_usec": 2_000, "at": 160.0}) is None


class TestShipFansOutPerBox:
    """Invariant 23d — the ship pass runs ONE WORKER PER BOX, serial within a box.

    Ship was the last serial data-movement phase: 37% of a poll cycle whose median is 20.8 min,
    with `ship_budget_spent` firing on EVERY pass (4-12 shipped, 6-31 deferred). What that cost was
    IDLE HARDWARE — median 52 tasks `running` against 115 `claimed`-but-not-started, median
    add->start 36 min (mean 82, p90 3.6 h). The axis is per-box because each box's own uplink is the
    ceiling (~0.3-0.62 MB/s), so concurrency within a box buys nothing and across boxes is ~linear.

    The serial path keeps the original 7b/7c tests above (pinned with `ship_parallel_boxes = 1`);
    these pin the same invariants on the fan-out.
    """

    def _dispatcher(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _box(self, conn, reg, inst_id):
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
            "ssh_port, slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inst_id, 555, f"runq_{inst_id}", reg.now_iso(), "live", 0.05, "h", 22, 4,
             reg.now_iso(), "vast"))
        conn.commit()

    def _task(self, conn, reg, tid, inst_id, created_at):
        reg.insert_task(
            conn, id=tid, created_at=created_at, created_by="t", grp="g", name=tid,
            entrypoint="smoke", args_json="[]", config_json="{}", config_hash=tid, arm_hash=tid,
            git_sha="d", slots=1, est_minutes=1, priority=50, max_retries=4, state="claimed",
            instance_id=inst_id)
        conn.commit()

    def _stub_prepare(self, d, monkeypatch, seen=None):
        """Bypass the real (serial, DB+compile) build — these tests are about the PUSH half."""
        def prep(task, inst=None, host=None, port=None):   # inv. 11 widened the signature
            if seen is not None:
                seen.append(task["id"])
            return {"task_id": task["id"], "reship": False, "apt_pkgs": "",
                    "bundle_path": "/tmp/b.tar", "ready_path": "/tmp/READY",
                    "remote_dir": f"~/spool/incoming/{task['id']}/"}
        monkeypatch.setattr(d, "_ship_prepare", prep)

    @staticmethod
    def _res(ok, fail=None):
        return {"ok": ok, "already": False, "ops": [(ok, True)], "fail": fail}

    def test_boxes_are_pushed_CONCURRENTLY(self, tmp_path, monkeypatch):
        """The whole point, asserted directly rather than by timing: both boxes' pushes must be in
        flight at once. The barrier is the proof — if the pass were serial the first box's worker
        would block forever waiting for a second party that never arrives, and this test would hang
        rather than quietly measure something else."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        for iid in (1, 2):
            self._box(d.conn, reg, inst_id=iid)
            self._task(d.conn, reg, f"T{iid}", iid, created_at=f"2026-07-29T03:0{iid}:00Z")
        self._stub_prepare(d, monkeypatch)
        barrier = threading.Barrier(2, timeout=10)
        overlapped = []

        def io(plan, host, port):
            overlapped.append(barrier.wait() is not None)   # raises BrokenBarrier if not concurrent
            return self._res(True)
        monkeypatch.setattr(d, "_ship_io", io)

        d._ship_all()
        assert len(overlapped) == 2, "both boxes must have been pushed concurrently"
        for tid in ("T1", "T2"):
            assert dict(d.conn.execute(
                "SELECT state FROM tasks WHERE id=?", (tid,)).fetchone())["state"] == "shipped"

    def test_within_a_box_pushes_stay_SERIAL(self, tmp_path, monkeypatch):
        """The measured half of the axis: one box's uplink is the ceiling, so its tasks must not be
        pushed concurrently with each other however wide the pool is."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        for iid in (1, 2):
            self._box(d.conn, reg, inst_id=iid)
        for i, tid in enumerate(["A1", "A2", "A3"]):
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        self._task(d.conn, reg, "B1", 2, created_at="2026-07-29T04:00:00Z")
        self._stub_prepare(d, monkeypatch)
        live = {"n": 0}
        max_concurrent_on_box1 = {"n": 0}
        lock = threading.Lock()

        def io(plan, host, port):
            if plan["task_id"].startswith("A"):
                with lock:
                    live["n"] += 1
                    max_concurrent_on_box1["n"] = max(max_concurrent_on_box1["n"], live["n"])
                time.sleep(0.02)
                with lock:
                    live["n"] -= 1
            return self._res(True)
        monkeypatch.setattr(d, "_ship_io", io)

        d._ship_all()
        assert max_concurrent_on_box1["n"] == 1, "a box's own pushes must not contend for its uplink"

    def test_a_transport_failure_defers_only_that_box_not_the_fleet(self, tmp_path, monkeypatch):
        """7b on the fan-out. It is now STRUCTURAL — a box's ships are one worker's serial loop, so
        a failure stops that box by `break` and cannot burn another box's budget on the way out."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        self._box(d.conn, reg, inst_id=1)      # degraded
        self._box(d.conn, reg, inst_id=2)      # healthy
        for i, tid in enumerate(["B1", "B2", "B3"]):
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        self._task(d.conn, reg, "H1", 2, created_at="2026-07-29T04:00:00Z")
        self._stub_prepare(d, monkeypatch)
        attempts = []

        def io(plan, host, port):
            attempts.append(plan["task_id"])
            if plan["task_id"].startswith("B"):
                return self._res(False, "rsync push failed after 3 attempts to h:22")
            return self._res(True)
        monkeypatch.setattr(d, "_ship_io", io)

        d._ship_all()
        assert [a for a in attempts if a.startswith("B")] == ["B1"], \
            f"burned the pass on a dead box: {attempts}"
        assert "H1" in attempts, f"healthy box starved behind the dead one: {attempts}"
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='H1'").fetchone())["state"] == "shipped"
        # deferred siblings keep their claim and are simply retried next poll
        assert dict(d.conn.execute(
            "SELECT state FROM tasks WHERE id='B2'").fetchone())["state"] == "claimed"
        ev = d.conn.execute(
            "SELECT detail FROM events WHERE event='ship_box_deferred'").fetchone()
        assert ev is not None and "instance 1" in ev["detail"]

    def test_a_task_level_failure_does_not_defer_the_box(self, tmp_path, monkeypatch):
        """The complement of 7b, and the reason the build had to stay on the serial side: a fault
        that says nothing about the BOX (a failed `git archive`) is resolved in `_ship_prepare`,
        yields no plan, and so can never reach the worker that would `break` its siblings."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        self._box(d.conn, reg, inst_id=1)
        self._box(d.conn, reg, inst_id=2)
        for i, tid in enumerate(["B1", "B2", "B3"]):
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        self._task(d.conn, reg, "H1", 2, created_at="2026-07-29T04:00:00Z")
        considered = []
        monkeypatch.setattr(d, "_ship_prepare",
                            lambda task, *a, **k: considered.append(task["id"]) or None)
        monkeypatch.setattr(d, "_ship_io",
                            lambda *a, **k: pytest.fail("no plan must reach the push half"))

        d._ship_all()
        assert [c for c in considered if c.startswith("B")] == ["B1", "B2", "B3"], \
            f"a task-level fault wrongly stopped its siblings: {considered}"
        assert d.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event='ship_box_deferred'").fetchone()["c"] == 0

    def test_width_one_takes_the_serial_path(self, tmp_path, monkeypatch):
        """`ship_parallel_boxes = 1` disables the fan-out outright — the serial path is kept, and
        this is the switch that selects it."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        d.settings["ship_parallel_boxes"] = 1
        for iid in (1, 2):
            self._box(d.conn, reg, inst_id=iid)
            self._task(d.conn, reg, f"T{iid}", iid, created_at=f"2026-07-29T03:0{iid}:00Z")
        used = []
        monkeypatch.setattr(d, "_ship", lambda task, inst: (used.append(task["id"]), True)[1])
        monkeypatch.setattr(d, "_ship_prepare",
                            lambda task: pytest.fail("width 1 must go through _ship"))

        d._ship_all()
        assert used == ["T1", "T2"]

    def test_the_fanout_reports_the_parallelism_it_ACHIEVED(self, tmp_path, monkeypatch):
        """A fan-out that silently stops fanning (one box monopolising the pass, a width collapsed
        to 1) is invisible from the outside — the pass still completes, just serially. So the pass
        logs summed per-thread work / wall-clock, exactly as ingest logs `payload_speedup`. This is
        the read that says whether 23d is actually doing anything on the live fleet, without
        re-deriving it by hand from event timestamps."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        for iid in (1, 2, 3):
            self._box(d.conn, reg, inst_id=iid)
            self._task(d.conn, reg, f"T{iid}", iid, created_at=f"2026-07-29T03:0{iid}:00Z")
        self._stub_prepare(d, monkeypatch)
        monkeypatch.setattr(d, "_ship_io",
                            lambda plan, host, port: (time.sleep(0.05), self._res(True))[1])

        d._ship_all()
        ev = d.conn.execute(
            "SELECT detail FROM events WHERE event='ship_fanout'").fetchone()
        assert ev is not None, "the pass must report the parallelism it achieved"
        p = json.loads(ev["detail"])
        assert p["boxes"] == 3 and p["tasks"] == 3
        # 3 boxes x ~0.05s of work each, overlapped -> work_sec ~3x wall_sec.
        assert p["speedup"] >= 2.0, f"boxes did not actually overlap: {p}"

    def test_fanout_report_DISCRIMINATES_skew_from_serialisation(self, tmp_path, monkeypatch):
        """`speedup` alone is ambiguous — the first live multi-box pass read 1.0, which is equally
        the signature of a fan-out that is not fanning and of ONE box holding nearly all the work.
        Those need opposite responses (fix a bug vs. spread the packing), so the report carries
        per-box seconds. Here box 1 does ~all the work: max(per_box) ~= wall proves SKEW, and the
        fan-out is behaving correctly despite a speedup near 1."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        for iid in (1, 2, 3):
            self._box(d.conn, reg, inst_id=iid)
        for i, tid in enumerate(["S1", "S2", "S3"]):        # the hog: 3 tasks, slow link
            self._task(d.conn, reg, tid, 1, created_at=f"2026-07-29T03:0{i}:00Z")
        self._task(d.conn, reg, "F2", 2, created_at="2026-07-29T04:00:00Z")
        self._task(d.conn, reg, "F3", 3, created_at="2026-07-29T04:01:00Z")
        self._stub_prepare(d, monkeypatch)
        monkeypatch.setattr(d, "_ship_io", lambda plan, host, port: (
            time.sleep(0.06 if plan["task_id"].startswith("S") else 0.001), self._res(True))[1])

        d._ship_all()
        p = json.loads(d.conn.execute(
            "SELECT detail FROM events WHERE event='ship_fanout'").fetchone()["detail"])
        assert p["tasks_per_box"] == {"1": 3, "2": 1, "3": 1}
        # THE discriminator: one box's own time accounts for essentially the whole wall-clock.
        assert p["slowest_box_sec"] == max(p["per_box_sec"].values())
        assert p["per_box_sec"]["1"] >= 0.9 * p["wall_sec"], (
            f"skew must be legible as 'one box ~= the whole wall', got {p}")
        assert p["per_box_sec"]["2"] < 0.5 * p["wall_sec"] and \
               p["per_box_sec"]["3"] < 0.5 * p["wall_sec"]

    def test_efficiency_makes_a_low_speedup_self_diagnosing(self, tmp_path, monkeypatch):
        """A raw `speedup` is meaningless without its ceiling: per-box parallelism can never beat
        its slowest box, so `work/slowest` is the most that was ever achievable and `efficiency` is
        the fraction of it reached. One slow box therefore reads as efficiency ~1.0 (the fan-out did
        all it could; the lever is placement) rather than as a suspiciously low speedup.

        Live 2026-07-31: 7 boxes / 20 tasks, work 338.1s -> wall 93.4s, speedup 3.6 vs a 3.62
        ceiling => efficiency ~1.0, with wall_sec == slowest_box_sec exactly."""
        d = self._dispatcher(tmp_path)
        reg = _seed_instance_and_task(d.conn, task_id="_seed", instance_id=99)
        d.conn.execute("UPDATE tasks SET state='done' WHERE id='_seed'")
        for iid in (1, 2, 3):
            self._box(d.conn, reg, inst_id=iid)
            self._task(d.conn, reg, f"T{iid}", iid, created_at=f"2026-07-29T03:0{iid}:00Z")
        self._stub_prepare(d, monkeypatch)
        # box 1 is the slow link; 2 and 3 are quick. Optimal fan-out => wall == box 1's own time.
        monkeypatch.setattr(d, "_ship_io", lambda plan, host, port: (
            time.sleep(0.12 if plan["task_id"] == "T1" else 0.01), self._res(True))[1])

        d._ship_all()
        p = json.loads(d.conn.execute(
            "SELECT detail FROM events WHERE event='ship_fanout'").fetchone()["detail"])
        assert p["ceiling"] is not None and p["efficiency"] is not None
        # wall is pinned to the slowest box — the theoretical floor for a per-box fan-out
        assert p["slowest_box_sec"] <= p["wall_sec"] <= p["slowest_box_sec"] * 1.5
        assert p["efficiency"] >= 0.7, f"an optimal fan-out must not read as inefficient: {p}"
        assert p["speedup"] <= p["ceiling"] * 1.35, f"speedup cannot exceed its ceiling: {p}"


class TestWorkerRollingUpgrade:
    """worker-rolling-upgrade.spec.md R3/R4/R6 + the three owner answers of 2026-08-03.

    R1 (delivery gate) made pushing safe by never delivering to an occupied box — and thereby froze
    the fleet, because the placer keeps every box occupied. R3/R4 are the half that makes the
    deferral TERMINATE."""

    def _d(self, tmp_path):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun())

    def _box(self, d, iid, occupied=0, machine_id=None):
        import importlib.util
        now = disp.registry_db.now_iso()
        d.conn.execute(
            "INSERT OR REPLACE INTO instances(id,machine_id,label,created_at,state,dph_usd,"
            "ssh_host,ssh_port,slots_total,hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, machine_id, f"b{iid}", now, "live", 0.05, "h", 22, 8, now))
        for k in range(occupied):
            disp.registry_db.insert_task(
                d.conn, id=f"t{iid}_{k}", created_at=now, created_by="test", grp="g",
                name=f"n{iid}_{k}", entrypoint="smoke", args_json="[]", config_json="{}",
                config_hash="h", arm_hash="a", git_sha="s", slots=1, est_minutes=10, priority=50)
            d.conn.execute("UPDATE tasks SET state='running', instance_id=? WHERE id=?",
                           (iid, f"t{iid}_{k}"))
        d.conn.commit()
        return dict(d.conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())

    # ---- R4.1 the roll width, and the owner's live-override ----
    def test_width_defaults_to_one(self, tmp_path):
        assert self._d(tmp_path)._worker_roll_max_draining() == 1

    def test_width_is_read_LIVE_from_the_db_not_the_cached_snapshot(self, tmp_path):
        """Owner 2026-08-03: 'ideally we could override it live to get something out more urgently.'
        `self.settings` is snapshotted in __post_init__, so reading from it would need a coordinator
        restart — the exact trap that made a worker_refresh_min override a no-op on 2026-08-02."""
        d = self._d(tmp_path)
        d.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_roll_max_draining','3')")
        d.conn.commit()
        assert d._worker_roll_max_draining() == 3        # no restart, no reload of self.settings
        assert d.settings.get("worker_roll_max_draining") != 3, "must NOT be read from the snapshot"

    def test_width_is_floored_at_one(self, tmp_path):
        """0 would freeze the roll entirely while looking like a tuning choice."""
        d = self._d(tmp_path)
        d.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_roll_max_draining','0')")
        d.conn.commit()
        assert d._worker_roll_max_draining() == 1

    # ---- R3 the hold ----
    def test_a_held_box_takes_NO_NEW_WORK(self, tmp_path):
        """R3.1 — in `_fits_now`, so `_infeasible_everywhere`/`_soonest_wait` agree with it. A hold
        invisible to the capacity math starves the queue instead of rolling it."""
        inst = {"id": 1, "slots_total": 8, "occupants": [], "minutes_to_hard_cap": 600,
                "worker_roll_held": True, "resource_cap": None}
        task = {"slots": 1, "est_minutes": 10, "resource_hint": None}
        s = {"est_safety": 1.25, "pull_margin_min": 10, "cores_per_lane": 1,
             "vram_per_lane_gb": 0.6, "ram_per_lane_gb": 2.0, "headroom_enabled": False}
        assert not disp._fits_now(task, inst, s)
        inst["worker_roll_held"] = False
        assert disp._fits_now(task, inst, s)

    def test_the_hold_is_DB_backed_and_survives_a_restart(self, tmp_path):
        """R3.2 — invariant 1: a restart must not resurrect a held box into the pool mid-roll."""
        d = self._d(tmp_path)
        inst = self._box(d, 1, occupied=2)
        d._admit_to_worker_roll(inst, "oldfp", 2)
        assert self._d(tmp_path)._worker_roll_held(inst) is True     # <- fresh Dispatcher

    def test_it_does_NOT_reuse_drain_held(self, tmp_path):
        """R3.4 — `drain_held` makes an EMPTY box TEAR DOWN. Here an empty box must be UPGRADED AND
        KEPT; reusing the flag would destroy the owned boxes, which cannot be re-rented and which
        (never churning) are the ones most often stale."""
        d = self._d(tmp_path)
        inst = self._box(d, -1, occupied=1)
        d._admit_to_worker_roll(inst, "oldfp", 1)
        assert d._worker_roll_held(inst) is True
        assert d._drain_held(inst) is False, "must not set the teardown flag"

    # ---- R6 fail-safe and bounded ----
    def test_an_unreadable_version_is_NOT_stale(self, tmp_path, monkeypatch):
        """R6.1 — never hold a box on a bad read; worst case it upgrades a cycle later."""
        d = self._d(tmp_path)
        inst = self._box(d, 1, occupied=1)
        monkeypatch.setattr(disp, "ssh_run", lambda *a, **k: type("R", (), {"stdout": ""})())
        assert d._box_worker_version(inst) is None
        d._consider_worker_roll(inst, 1, 1)
        assert d._worker_roll_held(inst) is False

    def test_the_hold_EXPIRES_and_says_so_distinctly(self, tmp_path):
        """R6.2 — one wedged occupant must not hold the roll's only slot forever, and 'returned to
        the pool' must not read identically to 'returned having achieved nothing'."""
        import datetime as _dt
        d = self._d(tmp_path)
        inst = self._box(d, 1, occupied=1)
        old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=721)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        d.conn.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                       ("worker_roll_i1", json.dumps({"at": old, "from": "oldfp"})))
        d.conn.commit()
        assert d._worker_roll_held(inst) is False
        evs = {r["event"] for r in d.conn.execute("SELECT event FROM events")}
        assert "worker_roll_expired" in evs and "worker_roll_upgraded" not in evs

    # ---- the escape hatch (open question 3, owner: yes) ----
    def test_the_urgent_hatch_is_ONE_SHOT(self, tmp_path):
        """It must not stay on and quietly turn every future roll into a preempting one."""
        d = self._d(tmp_path)
        d.conn.execute("INSERT INTO settings(key,value) VALUES('worker_roll_now','true')")
        d.conn.commit()
        assert d._worker_roll_urgent() is True
        assert d._worker_roll_urgent() is False, "consumed and deleted on first read"

    def test_the_hatch_is_OFF_by_default_and_on_a_bad_value(self, tmp_path):
        d = self._d(tmp_path)
        assert d._worker_roll_urgent() is False
        d.conn.execute("INSERT INTO settings(key,value) VALUES('worker_roll_now','banana')")
        d.conn.commit()
        assert d._worker_roll_urgent() is False

    def test_NOTHING_in_the_coordinator_writes_the_hatch_key(self):
        """Operator-only BY CONSTRUCTION — the key is written by `roll_now.py` (a human) and only
        ever DELETED by the dispatcher. If an automatic path could set it, the guarantee is void."""
        src = (ROOT / "fleet/dispatcher.py").read_text()
        for line in src.splitlines():
            if "worker_roll_now" in line:
                assert ("SELECT" in line or "DELETE" in line or line.strip().startswith("#")
                        or '"""' in line or line.strip().startswith("f\"")
                        or "self.log" in line or "worker_roll_now:" in line), \
                    f"dispatcher must never SET the hatch: {line.strip()}"


class TestRsyncFailureReasonIsNamed:
    """A ship failure must say WHAT failed — a full disk is not a transport fault.

    ⛔ THE INCIDENT (2026-08-12). Owned box `tower` filled its disk (98G/98G, 0 free). Every
    push failed with ENOSPC, and the dispatcher reported `rsync push failed after 3 attempts` plus
    `transport failure on instance -3`. The box was up, sshd answered, the worker was alive and
    heartbeating — so the message named a cause the evidence did not support, which is this repo's
    own "a check must be able to fail for the reason it names" turned on a diagnostic. It re-claimed
    and re-failed every ~68s for 34 minutes, holding SIX tasks from FOUR unrelated campaigns, and
    the only route to the truth was `ssh` + `df` by hand.
    """

    @staticmethod
    def _run(rc, stderr):
        def _r(cmd, **kw):
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)
        return _r

    def test_a_FULL_DISK_is_named_and_explicitly_not_called_transport(self):
        ok, why = disp.rsync_push_detail(
            "h", 22, ["f"], "r:",
            run=self._run(11, "rsync: write failed on \"/root/spool/incoming/x/bundle.tar\": "
                              "No space left on device (28)\nrsync error: error in file IO (11)"))
        assert ok is False
        assert "REMOTE DISK FULL" in why and "NOT a transport fault" in why
        assert "df -h" in why, "the message must carry the command that confirms it"

    def test_an_ordinary_failure_still_carries_the_exit_code_and_stderr(self):
        ok, why = disp.rsync_push_detail(
            "h", 22, ["f"], "r:", run=self._run(255, "ssh: connect to host h port 22: No route"))
        assert ok is False and "255" in why and "ssh error" in why and "No route" in why

    def test_success_reports_no_reason(self):
        assert disp.rsync_push_detail("h", 22, ["f"], "r:", run=self._run(0, "")) == (True, "")

    def test_the_bool_wrapper_is_unchanged_for_every_other_caller(self):
        assert disp.rsync_push("h", 22, ["f"], "r:", run=self._run(0, "")) is True
        assert disp.rsync_push("h", 22, ["f"], "r:", run=self._run(11, "x")) is False


class _BoxWithATruncatedBlob(_RecordingRun):
    """A box whose cached code blob EXISTS but is SHORT — the exact state `--partial` LEAVES.

    `rsync_push` runs `--partial --inplace` on purpose (inv. 7): an interrupted push keeps the
    truncated destination so the next attempt sends only the missing tail. This models the remote
    side of that: the file is there, and it is the wrong size.

    The probe is answered the way a real shell would. A SIZE-AWARE probe carries `-eq <bytes>` and
    can tell short from complete; an EXISTENCE-ONLY probe (`test -f`) cannot, and answers PRESENT
    for a 6 MB fragment of a 44 MB blob just as readily as for the real thing.
    """

    def __init__(self, remote_size: int):
        super().__init__()
        self.remote_size = remote_size

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "spool/CAPS" in joined:
            return _FakeProc(0, "blobref")      # a box new enough to take a code_ref bundle
        if "PRESENT" in joined and "blobs/" in joined:
            import re as _re
            want = _re.search(r"-eq (\d+)", joined)
            if want:
                return _FakeProc(0, "PRESENT" if int(want.group(1)) == self.remote_size else "ABSENT")
            return _FakeProc(0, "PRESENT")      # existence alone: a short file passes
        return _FakeProc(0, "")


class TestATruncatedBlobIsReshippedNotTrusted:
    """⛔⛔ REGRESSION, measured live 2026-08-30 on instance 40000055 — 39 TASKS DESTROYED ACROSS
    TWO UNRELATED SESSIONS, silently, and reported as a code/config bug.

    `rsync_push` uses `--partial --inplace` DELIBERATELY so an interrupted blob push resumes. The
    blob delivery path guarded that push with `test -f` — EXISTENCE. The two cancel out exactly:
    `--partial` guarantees a truncated file will exist, and `test -f` then reports PRESENT forever,
    so the resume never runs. `unpack_bundle` rejects the short blob on sha256 and does NOT evict
    it, so every task routed to that box dies at `FAILED_validation` until a human intervenes.

    The box held four blobs at 5.5-6.2 MB against a real 43.8 MB, each returning the SAME wrong
    sha256 on every attempt (10/10, 9/9, 10/10, 3/3) — which is what proved it was a stale FILE
    and not transit corruption, since corruption would vary per attempt.
    """

    def test_a_short_remote_blob_is_pushed_again(self, tmp_path):
        run = _BoxWithATruncatedBlob(remote_size=len(_BLOB) - 1)     # one byte short is still short
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="TRUNC")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='TRUNC'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        pushed = [c for c in run.joined() if "rsync" in c and "blob_TRUNC" in c]
        assert pushed, (
            "a TRUNCATED remote blob was accepted as delivered — the `--partial` resume can never "
            f"run and this box is now poisoned for blob_TRUNC forever. calls: {run.joined()}")

    def test_a_complete_remote_blob_is_NOT_pushed_again(self, tmp_path):
        """The other half: the skip must still work, or every cell of a sweep re-ships the blob."""
        run = _BoxWithATruncatedBlob(remote_size=len(_BLOB))          # byte-for-byte complete
        d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)
        _seed_instance_and_task(d.conn, task_id="WHOLE")
        task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='WHOLE'").fetchone())
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
        d._ship(task, inst)
        pushed = [c for c in run.joined() if "rsync" in c and "blob_WHOLE" in c]
        assert not pushed, f"re-shipped a blob the box already had in full: {pushed}"


class TestGpuAlertTransition:
    """Invariant 30b, the pure edge. One push per drop and one per recovery — never one per poll."""

    G = "NVIDIA GeForce RTX 3070 Ti"

    def test_two_gpu_less_probes_is_lost(self):
        assert disp.gpu_alert_transition(self.G, [self.G, None, None], alerted=False) == "lost"

    def test_one_gpu_less_probe_is_not_yet(self):
        assert disp.gpu_alert_transition(self.G, [self.G, None], alerted=False) is None

    def test_an_open_alert_is_not_resent(self):
        assert disp.gpu_alert_transition(self.G, [None, None], alerted=True) is None

    def test_recovery_after_a_loss_is_restored(self):
        assert disp.gpu_alert_transition(self.G, [None, self.G], alerted=True) == "restored"

    def test_healthy_box_is_silent(self):
        assert disp.gpu_alert_transition(self.G, [self.G, self.G], alerted=False) is None

    def test_a_box_registered_without_a_gpu_is_out_of_scope(self):
        assert disp.gpu_alert_transition(None, [None, None], alerted=False) is None


class TestGpuAlert:
    """Invariant 30 end to end over a real registry: the event log holds the alert state, a failed
    push retries on the next probe, and nothing goes out when no channel is configured."""

    G = "NVIDIA GeForce RTX 3070 Ti"

    @staticmethod
    def _mk(tmp_path, notify=None):
        return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_RecordingRun(),
                               vastai_run=_RecordingRun(), notify=notify)

    def _box(self, d, gpu=G):
        d.conn.execute(
            "INSERT OR IGNORE INTO instances(id,label,created_at,state,dph_usd,slots_total,"
            "hard_cap_at,gpu_name,source) VALUES (-2,'desktop','2026-09-23T00:00:00Z','live',0,4,"
            "'2036-01-01T00:00:00Z',?,'owned')", (gpu,))
        d.conn.commit()
        return dict(d.conn.execute("SELECT * FROM instances WHERE id=-2").fetchone())

    @staticmethod
    def _probe(d, gpu_name, key=True):
        tail = {"v": 1, "cores": 20}
        if key:
            tail["gpu_name"] = gpu_name
        d.log("box_measured", "load 0.1/20 cores | " + json.dumps(tail), instance_id=-2)

    @staticmethod
    def _events(d, name):
        return [r["event"] for r in d.conn.execute(
            "SELECT event FROM events WHERE instance_id=-2 AND event=? ORDER BY seq", (name,))]

    def test_one_push_per_drop_and_one_per_recovery(self, tmp_path):
        sent = []
        d = self._mk(tmp_path, notify=lambda *a: (sent.append(a[0]) or (True, "")))
        inst = self._box(d)
        for g in (self.G, None, None, None, None, self.G, self.G):
            self._probe(d, g)
            d._check_gpu_alert(inst)
        assert sent == ["desktop: GPU lost", "desktop: GPU back"]
        assert len(self._events(d, "gpu_lost")) == 1 and len(self._events(d, "gpu_restored")) == 1

    def test_a_restart_does_not_resend_an_open_alert(self, tmp_path):
        sent = []
        note = lambda *a: (sent.append(a[0]) or (True, ""))    # noqa: E731
        d = self._mk(tmp_path, notify=note)
        inst = self._box(d)
        for g in (None, None):
            self._probe(d, g)
            d._check_gpu_alert(inst)
        d2 = self._mk(tmp_path, notify=note)                   # <- the restart
        self._probe(d2, None)
        d2._check_gpu_alert(inst)
        assert sent == ["desktop: GPU lost"]

    def test_a_FAILED_push_is_retried_on_the_next_probe(self, tmp_path):
        results = [(False, "URLError: offline"), (True, "")]
        d = self._mk(tmp_path, notify=lambda *a: results.pop(0))
        inst = self._box(d)
        for g in (None, None):
            self._probe(d, g)
            d._check_gpu_alert(inst)
        assert self._events(d, "gpu_lost") == [], "a failed push must not record the transition"
        self._probe(d, None)
        d._check_gpu_alert(inst)
        assert len(self._events(d, "gpu_lost")) == 1 and results == []

    def test_a_missing_gpu_KEY_never_alerts(self, tmp_path):
        sent = []
        d = self._mk(tmp_path, notify=lambda *a: (sent.append(a) or (True, "")))
        inst = self._box(d)
        for _ in range(3):
            self._probe(d, None, key=False)
            d._check_gpu_alert(inst)
        assert sent == []

    def test_no_channel_still_records_the_transition(self, tmp_path, monkeypatch):
        monkeypatch.delenv(disp.NTFY_TOPIC_ENV, raising=False)
        d = self._mk(tmp_path)
        inst = self._box(d)
        for g in (None, None):
            self._probe(d, g)
            d._check_gpu_alert(inst)
        assert len(self._events(d, "gpu_lost")) == 1

    def test_env_topic_routes_to_ntfy(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setenv(disp.NTFY_TOPIC_ENV, "t-secret")
        monkeypatch.setattr(disp, "ntfy_post",
                            lambda topic, title, msg, **kw: (calls.append((topic, title)) or (True, "")))
        d = self._mk(tmp_path)
        inst = self._box(d)
        for g in (None, None):
            self._probe(d, g)
            d._check_gpu_alert(inst)
        assert calls == [("t-secret", "desktop: GPU lost")]

    def test_ntfy_post_never_raises(self):
        ok, err = disp.ntfy_post("t", "x", "y", server="http://127.0.0.1:9", timeout=1.0)
        assert ok is False and err
