"""The README's numbers are the track record's numbers, and its claims about this tree hold.

A README drifts in exactly one direction: the code changes and the prose does not. These pin the
figures a reader would quote, so changing one without the other fails here.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()


def _n(value: int) -> str:
    return f"{value:,}"


def test_every_track_record_figure_appears_in_the_readme():
    record = json.loads((ROOT / "docs" / "track-record.json").read_text())
    assert record["as_of"] in README
    for key, value in record.items():
        if isinstance(value, int):
            assert _n(value) in README, f"{key} = {_n(value)} is not in the README"
    done = record["completed_on_owned_machines"] + record["completed_on_rentals"]
    assert done == record["completed"]


def test_the_stated_defaults_are_the_dispatcher_defaults():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("dispatcher_for_readme", ROOT / "fleet" / "dispatcher.py")
    disp = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = disp            # dataclasses look their module up while it is loading
    spec.loader.exec_module(disp)
    s = disp.DEFAULT_SETTINGS
    assert (s["vram_per_lane_gb"], s["cores_per_lane"]) == (0.6, 1)      # "0.6 GB ... one core each"
    assert s["idle_timeout_min"] == 10                                    # "after 10 idle minutes"
    assert s["checkpoint_pull_every_min"] == 5                            # "every 5 minutes"
    assert s["stall_timeout_min"] == 90                                   # "for 90 minutes"
    assert s["preempt_enabled"] is False                                  # "switched off by default"
    assert s["bundle_compile"] is True                                    # "Compiled shipping is on"
    assert s["bundle_compile_packages"] == ["src/native", "src/shared"]   # "two directories that exist only in ..."
    migrations = disp._SETTING_MIGRATIONS
    assert len(migrations) == 8, "the README and operations.md say EIGHT settings are reset at start"
    assert ("consolidate_enabled", True, False) in [tuple(m) for m in migrations]
    ops = (ROOT / "docs" / "operations.md").read_text()
    for key in ("max_hourly_usd", "max_instance_dph", "balance_floor_usd", "hard_cap_hours",
                "max_slots_cap", "poll_seconds"):
        value = s[key]
        shown = f"{value:.2f}" if isinstance(value, float) else str(value)
        assert re.search(rf"`{key}` \| {re.escape(shown)} \|", ops), f"{key} default {shown} not in operations.md"
    show = lambda v: str(v).lower() if isinstance(v, bool) else str(v)
    for key, old, new in migrations:
        row = rf"\| `{key}` \| {re.escape(show(old))} \| {re.escape(show(new))} \|"
        assert re.search(row, ops), f"operations.md does not list the reset of {key}: {old} -> {new}"


def test_the_dispatcher_is_as_long_as_the_readme_admits():
    lines = len((ROOT / "fleet" / "dispatcher.py").read_text().splitlines())
    assert lines > 8000, "the README says 'more than 8,000 lines'; say something else now"
