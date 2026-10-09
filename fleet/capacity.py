"""Time-of-day resource-cap schedule for an owned box.

The operator sets CPU + GPU-VRAM caps per time window; the fleet INFERS the box's usable slots from
whichever cap binds. Pure + stdlib (zoneinfo) so BOTH the coordinator (slot inference,
`dispatcher._capacity_slots`) and the host-side cgroup enforcer (`box_capacity_apply.py`, which runs
`docker update --cpus`) read one schedule and agree.

Config file `configs/capacity/<box-label>.json`:
  {
    "tz": "America/New_York",          # IANA tz the windows are in (the BOX's local time)
    "cores": 20, "vram_gb": 8,         # the box's total CPU cores + GPU VRAM (GB)
    "cores_per_lane": 4,               # OPTIONAL: this box's REAL per-lane footprint (inv. 18);
    "vram_per_lane_gb": 2.0,           #   omit to fall back to the global settings defaults
    "windows": [                        # must tile 24h; a window may wrap past midnight
      {"from": "23:00", "to": "07:00", "cpu": 0.875, "vram": 0.875},
      {"from": "07:00", "to": "23:00", "cpu": 0.5,   "vram": 0.75, "gpu_power": 0.6}
    ]
  }

`gpu_power` (OPTIONAL, per window) is a HARD GPU cap: a fraction of the card's default power limit,
applied host-side by `box_capacity_apply.py` via `nvidia-smi -pl` (spec inv. 20a). The coordinator
never reads it — packing is bounded by `vram`. Absent = the card's default limit.

Why the lane footprint is declarable (box-pause spec inv. 18, live incident 2026-07-27): a lane is
only a meaningful unit of a CPU/VRAM budget if it is the size the box's tasks actually occupy.
`desktop`'s day window budgets 10 cores / 6 GB, but dividing by the global settings lane
(1 core / 0.6 GB) inferred **10 slots** while every task in flight hinted 4 cores / 2.0 GB — i.e. up
to 40 cores demanded on a 20-core box. `window_budget` exposes the ABSOLUTE budget so the
coordinator can additionally admit against real summed footprints (inv. 18a) rather than trust a
single scalar slot count to stay honest across a heterogeneous hint mix.
"""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo


def load(text_or_dict) -> dict:
    """Parse + validate a schedule (trust boundary). Raises ValueError with a clear message."""
    d = json.loads(text_or_dict) if isinstance(text_or_dict, (str, bytes)) else text_or_dict
    for k in ("tz", "cores", "vram_gb", "windows"):
        if k not in d:
            raise ValueError(f"capacity schedule missing '{k}'")
    ZoneInfo(d["tz"])  # raises if the tz is unknown
    if not d["windows"]:
        raise ValueError("capacity schedule has no windows")
    for w in d["windows"]:
        for k in ("from", "to", "cpu", "vram"):
            if k not in w:
                raise ValueError(f"capacity window missing '{k}': {w}")
        _minute_of_day(w["from"]), _minute_of_day(w["to"])  # validate HH:MM
        if not (0.0 <= w["cpu"] <= 1.0 and 0.0 <= w["vram"] <= 1.0):
            raise ValueError(f"capacity cpu/vram must be fractions in [0,1]: {w}")
        if "gpu_power" in w and not (isinstance(w["gpu_power"], (int, float))
                                     and 0.0 < w["gpu_power"] <= 1.0):
            raise ValueError(f"capacity gpu_power must be a fraction in (0,1]: {w}")
    for k in ("cores_per_lane", "vram_per_lane_gb"):  # optional lane footprint (inv. 18)
        if k in d and not (isinstance(d[k], (int, float)) and d[k] > 0):
            raise ValueError(f"capacity {k} must be a positive number: {d[k]!r}")
    return d


def _minute_of_day(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def active_window(sched: dict, now_utc) -> dict | None:
    """The window covering `now_utc` (a tz-aware UTC datetime), evaluated in the schedule's tz.
    Handles a window that wraps past midnight (`from` > `to`)."""
    local = now_utc.astimezone(ZoneInfo(sched["tz"]))
    cur = local.hour * 60 + local.minute
    for w in sched["windows"]:
        a, b = _minute_of_day(w["from"]), _minute_of_day(w["to"])
        inside = (a <= cur < b) if a < b else (cur >= a or cur < b)
        if inside:
            return w
    return None


def lane_footprint(sched: dict, cores_per_lane: float, vram_per_lane_gb: float) -> tuple[float, float]:
    """The per-lane footprint to size this box's slots by: the SCHEDULE's declared values when it
    declares them, else the passed global-settings defaults (inv. 18)."""
    return (float(sched.get("cores_per_lane", cores_per_lane)),
            float(sched.get("vram_per_lane_gb", vram_per_lane_gb)))


def window_budget(sched: dict, now_utc) -> dict | None:
    """The current window's ABSOLUTE resource budget — `{"cores", "vram_gb"}` — which invariant 18a
    admits against directly, instead of trusting a single slot scalar to stay honest across a mix of
    per-task hints. None if no window matches (box uncapped)."""
    w = active_window(sched, now_utc)
    if w is None:
        return None
    return {"cores": w["cpu"] * sched["cores"], "vram_gb": w["vram"] * sched["vram_gb"]}


def effective_slots(sched: dict, now_utc, cores_per_lane: float, vram_per_lane_gb: float) -> int | None:
    """Slots the box may run in the current window = floor of the tighter of the CPU- and
    VRAM-derived lane counts. None if no window matches (leave the box uncapped).

    `cores_per_lane`/`vram_per_lane_gb` are the global-settings DEFAULTS; a schedule that declares
    its own lane footprint overrides them (inv. 18) — dividing a real budget by a lane size the
    box's tasks don't actually occupy is what over-advertised `desktop` 5x on 2026-07-27."""
    w = active_window(sched, now_utc)
    if w is None:
        return None
    cores_per_lane, vram_per_lane_gb = lane_footprint(sched, cores_per_lane, vram_per_lane_gb)
    cpu_lanes = int(w["cpu"] * sched["cores"] / max(1e-9, cores_per_lane))
    vram_lanes = int(w["vram"] * sched["vram_gb"] / max(1e-9, vram_per_lane_gb))
    return max(0, min(cpu_lanes, vram_lanes))


def uncapped(sched: dict) -> dict:
    """The schedule to PUSH TO THE HOST while a forced task occupies the box (box-pause inv. 20e):
    the same box, every hour of the day at full CPU and VRAM, with no `gpu_power`.

    An ordinary schedule on purpose, so the enforcer already installed on a host understands it and
    nothing there is re-installed. Two windows rather than one `00:00`→`00:00`: that tiles the day
    using only the plain and the wrap-past-midnight cases every shipped schedule already exercises,
    instead of leaning on how a zero-length window happens to evaluate. The coordinator never
    admits against this — its own gates keep reading the configured schedule."""
    out = {k: sched[k] for k in ("tz", "cores", "vram_gb")}
    out["windows"] = [{"from": "00:00", "to": "12:00", "cpu": 1.0, "vram": 1.0},
                      {"from": "12:00", "to": "00:00", "cpu": 1.0, "vram": 1.0}]
    return out


def fully_open(cores: int) -> dict:
    """The schedule to PUSH TO THE HOST of a box that has NO configured schedule (box-pause 20b-1):
    every hour of the day at full CPU, no `gpu_power`, for a box with `cores` cores.

    An explicit instruction instead of a missing file, because the enforcer could not act on
    absence: it lifted the CPU cap with `docker update --cpus 0`, which Docker reads as "field not
    supplied", so a box whose schedule was removed kept its last cap forever. `vram_gb` is 0
    because nothing on the host reads it — the enforcer applies a CPU cap and a GPU power limit —
    and the coordinator never admits against this schedule."""
    return uncapped({"tz": "UTC", "cores": int(cores), "vram_gb": 0})


def cpu_cores_cap(sched: dict, now_utc) -> float | None:
    """The hard CPU-core cap for the current window, for `docker update --cpus`. None if no match."""
    w = active_window(sched, now_utc)
    return None if w is None else round(w["cpu"] * sched["cores"], 2)


def gpu_power_fraction(sched: dict, now_utc) -> float | None:
    """The current window's GPU power cap as a fraction of the card's default limit (inv. 20a).
    None = no cap (no window matches, or the window declares no `gpu_power`)."""
    w = active_window(sched, now_utc)
    return None if w is None else w.get("gpu_power")
