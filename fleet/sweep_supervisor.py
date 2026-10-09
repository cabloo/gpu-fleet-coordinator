"""Sweep supervisor — early-pruning lane scheduler (docs/specs/sweep-supervisor.spec.md).

Runs a queue of capacity-probe config variants as co-tenant lanes, watches each lane's
curve.jsonl, kills bad configs early (hard gates + env-step rungs), and refills freed slots.
Ranking = rolling median of det_norm at matched env_steps; entropy death is the kill-gate;
WM loss is deliberately NOT a ranking signal (it mis-selects — see VAST-TEST.md).

    python fleet/sweep_supervisor.py --sweep sweep.json --out experiments/sweep1
    python fleet/sweep_supervisor.py --sweep sweep.json --out experiments/sweep1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1] / "vast_capacity_probe.py"
DEFAULT_GATES = {"entropy_floor": 0.02, "entropy_by_env_steps": 10_000, "stall_minutes": 30}
AUTO_DEFAULTS = {"util_ceiling": 90.0, "settle_minutes": 3.0, "max_slots": 8,
                 "settle_idle_frac": 0.5, "settle_floor_min": 0.5}
# The three numbers `should_launch` used to hard-code in its body. They are DEFAULTS now: a caller
# may override each through `auto_cfg`, which is how the fleet worker applies the coordinator's
# `launch_gate` setting (task-dispatcher inv. 8b). A sweep file that says nothing gets these.
CPU_RESERVE_CORES = 1.0     # refuse a launch once load1 is within this many cores of the box
VRAM_LANE_MULT = 1.25       # free VRAM needed = this x the largest lane seen so far...
VRAM_FREE_FRAC = 0.2        # ...or, before any lane has been measured, this share of the card
POLL_SECONDS = 60
KILL_GRACE_SECONDS = 30
TERMINAL = {"gate_kill", "rung_kill", "finished", "error"}


# --- sweep file (trust boundary: validate, never guess) ---
def load_sweep(path: str) -> dict:
    with open(path) as f:
        sweep = json.load(f)
    allowed = {"slots", "probe_args", "rungs", "gates", "queue", "auto"}
    unknown = set(sweep) - allowed
    if unknown:
        raise SystemExit(f"sweep: unknown keys {sorted(unknown)}")
    slots = sweep.get("slots")
    if slots != "auto" and (not isinstance(slots, int) or isinstance(slots, bool) or slots < 1):
        raise SystemExit("sweep: 'slots' must be an int >= 1 or \"auto\"")
    auto = dict(AUTO_DEFAULTS)
    for k, v in (sweep.get("auto") or {}).items():
        if k not in AUTO_DEFAULTS:
            raise SystemExit(f"sweep: unknown auto key {k!r}")
        auto[k] = v
    sweep["auto"] = auto
    queue = sweep.get("queue")
    if not isinstance(queue, list) or not queue:
        raise SystemExit("sweep: 'queue' must be a non-empty list")
    names = []
    for item in queue:
        if set(item) != {"name", "args"} or not isinstance(item["args"], list):
            raise SystemExit(f"sweep: queue items need exactly name+args(list): {item}")
        names.append(item["name"])
    if len(set(names)) != len(names):
        raise SystemExit("sweep: duplicate lane names")
    rungs = sweep.get("rungs")
    if not isinstance(rungs, list):
        raise SystemExit("sweep: 'rungs' must be a list (may be empty)")
    steps = []
    for r in rungs:
        if set(r) != {"env_steps", "keep_fraction"}:
            raise SystemExit(f"sweep: rung needs exactly env_steps+keep_fraction: {r}")
        if not (0 < r["keep_fraction"] <= 1):
            raise SystemExit("sweep: rung keep_fraction must be in (0, 1]")
        steps.append(r["env_steps"])
    if steps != sorted(steps) or len(set(steps)) != len(steps):
        raise SystemExit("sweep: rung env_steps must be strictly increasing")
    gates = dict(DEFAULT_GATES)
    for k, v in (sweep.get("gates") or {}).items():
        if k not in DEFAULT_GATES:
            raise SystemExit(f"sweep: unknown gate {k!r}")
        gates[k] = v
    sweep["gates"] = gates
    sweep.setdefault("probe_args", [])
    if not isinstance(sweep["probe_args"], list):
        raise SystemExit("sweep: 'probe_args' must be a list")
    return sweep


# --- pure decision core (importable by tests) ---
@dataclass
class LaneView:
    """Everything decide() needs about one lane; no process handles."""

    name: str
    rows: list  # curve.jsonl rows, chronological
    process_alive: bool = False
    done: bool = False  # DONE marker exists
    last_row_age_min: float | None = None  # age of newest row (or lane start if no rows)


@dataclass
class Decision:
    event: str  # gate_kill | rung_kill
    reason: str
    env_steps: int | None = None
    metric: float | None = None


def _bad(x) -> bool:
    return isinstance(x, float) and (math.isnan(x) or math.isinf(x))


def rung_metric(rows: list, env_steps: int) -> float | None:
    """Median of the last <=3 det_norm values at or below env_steps; None if no such row."""
    vals = [r["det_norm"] for r in rows if r["env_steps"] <= env_steps]
    if not vals:
        return None
    return float(statistics.median(vals[-3:]))


def decide_gates(lane: LaneView, gates: dict) -> Decision | None:
    """Hard gates, in spec order (3a/3b/3d; 3c crash is process-level, handled by the loop)."""
    for r in lane.rows:
        if _bad(r.get("det_norm")) or _bad(r.get("ac_entropy")):
            return Decision("gate_kill", f"NaN/inf in curve row at env_steps={r['env_steps']}",
                            r["env_steps"])
    if len(lane.rows) >= 2:
        last, prev = lane.rows[-1], lane.rows[-2]
        if (last.get("phase") == "learn"
                and last["env_steps"] >= gates["entropy_by_env_steps"]
                and last["ac_entropy"] < gates["entropy_floor"]
                and prev["ac_entropy"] < gates["entropy_floor"]):
            return Decision(
                "gate_kill",
                f"entropy death: last 2 rows < {gates['entropy_floor']} "
                f"(got {prev['ac_entropy']}, {last['ac_entropy']}) in learn phase",
                last["env_steps"], last["ac_entropy"])
    if (lane.process_alive and lane.last_row_age_min is not None
            and lane.last_row_age_min >= gates["stall_minutes"]):
        return Decision("gate_kill",
                        f"stall: no curve row for {lane.last_row_age_min:.0f} min "
                        f"(cap {gates['stall_minutes']})",
                        lane.rows[-1]["env_steps"] if lane.rows else None)
    return None


@dataclass
class RungState:
    env_steps: int
    keep_fraction: float
    resolved: bool = False
    reported: dict = field(default_factory=dict)  # name -> metric (pre-resolution reachers)
    bar: float | None = None  # worst surviving incumbent metric (post-resolution)


class RungEngine:
    """Invariant 4: incumbent ranking at each rung, then a fixed bar for late joiners."""

    def __init__(self, rungs: list):
        self.rungs = [RungState(r["env_steps"], r["keep_fraction"]) for r in rungs]

    def check(self, lanes: dict, terminal: dict) -> list[tuple[str, Decision]]:
        """lanes: name -> LaneView for every lane ever started (this poll's view).
        terminal: name -> terminal event (lanes already decided). Returns new rung kills."""
        kills: list[tuple[str, Decision]] = []
        for rung in self.rungs:
            live = {n: v for n, v in lanes.items() if n not in terminal}
            if not rung.resolved:
                for name, view in lanes.items():
                    if name in rung.reported or name in terminal and name not in rung.reported:
                        continue
                    if view.rows and view.rows[-1]["env_steps"] >= rung.env_steps:
                        m = rung_metric(view.rows, rung.env_steps)
                        if m is not None:
                            rung.reported[name] = m
                # resolve when every not-yet-terminal lane has reached this rung
                pending = [n for n in live if n not in rung.reported]
                if not pending and rung.reported:
                    ranked = sorted(rung.reported.items(), key=lambda kv: kv[1], reverse=True)
                    n = len(ranked)
                    n_kill = math.floor((1 - rung.keep_fraction) * n)
                    keep = ranked[: n - n_kill]
                    keep_names = {k for k, _ in keep}
                    cut = keep[-1][1] if keep else None
                    for name, m in ranked[n - n_kill:]:
                        if cut is not None and m == cut:  # ties keep both
                            keep_names.add(name)
                            continue
                        if name not in terminal:
                            kills.append((name, Decision(
                                "rung_kill",
                                f"rung@{rung.env_steps}: metric {m} ranked below keep cut "
                                f"(keep_fraction {rung.keep_fraction})",
                                rung.env_steps, m)))
                    survivors = [m for k, m in ranked if k in keep_names]
                    rung.bar = min(survivors) if survivors else None
                    rung.resolved = True
            else:
                for name, view in live.items():
                    if name in rung.reported:
                        continue
                    if view.rows and view.rows[-1]["env_steps"] >= rung.env_steps:
                        m = rung_metric(view.rows, rung.env_steps)
                        if m is None:
                            continue
                        rung.reported[name] = m
                        if rung.bar is not None and m < rung.bar:
                            kills.append((name, Decision(
                                "rung_kill",
                                f"rung@{rung.env_steps}: late joiner metric {m} below "
                                f"surviving bar {rung.bar}", rung.env_steps, m)))
        return kills


def decide(lane: LaneView, engine: RungEngine, lanes: dict, terminal: dict,
           gates: dict) -> Decision | None:
    """Spec's pure entry point: gates first, then this lane's slice of the rung pass."""
    d = decide_gates(lane, gates)
    if d:
        return d
    for name, dec in engine.check(lanes, terminal):
        if name == lane.name:
            return dec
    return None


# --- auto slots (invariant 11): queue until the hardware bottleneck binds, resume when clear ---
def sample_hw() -> dict:
    """One hardware snapshot. GPU fields are None without nvidia-smi (11d)."""
    hw = {"gpu_util": None, "vram_free_gb": None, "vram_total_gb": None,
          "proc_vram_gb": {}, "load1": os.getloadavg()[0], "cores": os.cpu_count() or 1}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            util, used, total = [float(x) for x in out.stdout.strip().splitlines()[0].split(",")]
            hw.update(gpu_util=util, vram_free_gb=(total - used) / 1024,
                      vram_total_gb=total / 1024)
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            for line in out.stdout.strip().splitlines():
                if "," in line:
                    pid, mem = line.split(",")[:2]
                    hw["proc_vram_gb"][int(pid)] = float(mem) / 1024
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        pass
    return hw


def should_launch(hw: dict, vram_per_lane_max: float | None, n_live: int,
                  since_launch_min: float, auto_cfg: dict) -> tuple[bool, str]:
    """Pure launch decision (11e). Order: progress guarantee, cap, settle, cpu, gpu, vram."""
    if n_live == 0:
        return True, "first lane"
    if n_live >= auto_cfg["max_slots"]:
        return False, "max_slots"
    if since_launch_min < auto_cfg["settle_minutes"]:
        # ⭐ THE SETTLE IS A PROXY FOR LOAD WE CANNOT SEE YET — AND ON A DEMONSTRABLY IDLE BOX WE CAN.
        # `load1` is a 60 s average, so a task launched seconds ago is not yet visible in it; the
        # 3-minute settle exists so we do not stack launches BLIND. But when the box is measurably
        # far from full, that blindness does not apply: we have the direct evidence the settle was
        # waiting for, and holding anyway costs latency for nothing.
        #
        # ⛔ MEASURED COST OF NOT DOING THIS (2026-09-09): a probe sweep queued 18 short tasks to one
        # 16-slot box. Each did ~22 s of compute; each waited ~880 s. `launch_ready` launches AT MOST
        # ONE task per call and this gate then blocks the next for 3 minutes, so 18 tasks is a
        # ~51-MINUTE LAUNCH RAMP before a single tick of the last one runs. The box sat near-idle
        # throughout. Nothing else in the path was slow: the dispatcher cycle was 25 s p50, the
        # queue-time compile ~8 s (already incremental, c2b696f7), and the worker polls every 2 s.
        #
        # Two conditions, both required, so this cannot degrade into "no settle at all":
        #   • `settle_floor_min` (0.5 = 30 s, half the load1 averaging window) still separates
        #     consecutive launches, so each one IS partly visible to the next decision.
        #   • `settle_idle_frac` (0.5) demands the box be under half its cores. As launches land,
        #     load1 rises and this closes ON ITS OWN — the mechanism is self-limiting, and the hard
        #     `cores - 1` guard below is untouched.
        idle_bypass = (since_launch_min >= auto_cfg.get("settle_floor_min", 0.5)
                       and hw["load1"] < auto_cfg.get("settle_idle_frac", 0.5) * hw["cores"])
        if not idle_bypass:
            return False, "settling"
    if hw["load1"] >= hw["cores"] - auto_cfg.get("cpu_reserve_cores", CPU_RESERVE_CORES):
        return False, "cpu_load"
    if hw["gpu_util"] is not None and hw["gpu_util"] >= auto_cfg["util_ceiling"]:
        return False, "gpu_util"
    if hw["vram_free_gb"] is not None:
        need = (auto_cfg.get("vram_lane_mult", VRAM_LANE_MULT) * vram_per_lane_max
                if vram_per_lane_max
                else auto_cfg.get("vram_free_frac", VRAM_FREE_FRAC) * (hw["vram_total_gb"] or 0))
        if hw["vram_free_gb"] < need:
            return False, "vram"
    return True, "headroom"


# --- artifacts / registry ---
def read_curve(lane_dir: Path) -> list:
    rows = []
    f = lane_dir / "curve.jsonl"
    if not f.exists():
        return rows
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            break  # tolerate a partially-written last line
    return rows


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.events: list[dict] = []
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    self.events.append(json.loads(line))

    def terminal(self) -> dict:
        return {e["lane"]: e["event"] for e in self.events if e["event"] in TERMINAL}

    def started(self) -> list[str]:
        return [e["lane"] for e in self.events if e["event"] == "start"]

    def append(self, lane: str, event: str, reason: str = "", env_steps=None, metric=None,
               pid=None) -> None:
        if event in TERMINAL and self.terminal().get(lane):
            return  # idempotent restart: never duplicate a terminal event
        rec = {"t": datetime.now(timezone.utc).isoformat(), "lane": lane, "event": event,
               "reason": reason, "env_steps": env_steps, "metric": metric, "pid": pid}
        self.events.append(rec)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())


def write_scoreboard(out: Path, lanes: dict, terminal: dict, reasons: dict) -> None:
    rows = []
    for name, view in lanes.items():
        rolling = rung_metric(view.rows, view.rows[-1]["env_steps"]) if view.rows else None
        peak = max((r["det_norm"] for r in view.rows), default=None)
        rows.append({
            "lane": name,
            "status": terminal.get(name, "running"),
            "env_steps": view.rows[-1]["env_steps"] if view.rows else 0,
            "rolling_med_det_norm": rolling, "peak_det_norm": peak,
            "last_ac_entropy": view.rows[-1]["ac_entropy"] if view.rows else None,
            "reason": reasons.get(name, ""),
        })
    rows.sort(key=lambda r: (r["rolling_med_det_norm"] is None,
                             -(r["rolling_med_det_norm"] or 0)))
    head = list(rows[0].keys()) if rows else []
    with open(out / "scoreboard.csv", "w") as f:
        f.write(",".join(head) + "\n")
        for r in rows:
            f.write(",".join("" if r[k] is None else str(r[k]) for k in head) + "\n")
    with open(out / "scoreboard.md", "w") as f:
        f.write("# Sweep scoreboard (rolling-median det_norm, desc)\n\n")
        f.write("| " + " | ".join(head) + " |\n|" + "|".join(["---"] * len(head)) + "|\n")
        for r in rows:
            f.write("| " + " | ".join("" if r[k] is None else str(r[k]) for k in head) + " |\n")


# --- live mode ---
class Lane:
    def __init__(self, name: str, dir: Path, proc: subprocess.Popen | None):
        self.name, self.dir, self.proc = name, dir, proc
        self.started_at = time.time()

    def view(self) -> LaneView:
        rows = read_curve(self.dir)
        curve = self.dir / "curve.jsonl"
        last_activity = curve.stat().st_mtime if curve.exists() else self.started_at
        return LaneView(
            name=self.name, rows=rows,
            process_alive=self.proc is not None and self.proc.poll() is None,
            done=(self.dir / "DONE").exists(),
            last_row_age_min=(time.time() - last_activity) / 60,
        )


def kill_lane(lane: Lane) -> None:
    if lane.proc is None or lane.proc.poll() is not None:
        return
    lane.proc.terminate()
    deadline = time.time() + KILL_GRACE_SECONDS
    while time.time() < deadline and lane.proc.poll() is None:
        time.sleep(1)
    if lane.proc.poll() is None:
        lane.proc.kill()


def run_live(sweep: dict, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    reg = Registry(out / "registry.jsonl")
    terminal = reg.terminal()
    reasons = {e["lane"]: e["reason"] for e in reg.events if e["event"] in TERMINAL}
    # restart reconstruction (invariant 8): orphans -> error; queue resumes after last start
    started = reg.started()
    for name in started:
        if name not in terminal:
            reg.append(name, "error", "orphan after supervisor restart")
    terminal = reg.terminal()
    queue = list(sweep["queue"])
    if started:
        seen = set(started)
        queue = [q for q in queue if q["name"] not in seen]
    engine = RungEngine(sweep["rungs"])
    live: dict[str, Lane] = {}
    views: dict[str, LaneView] = {  # every lane ever started, for rung bookkeeping
        name: LaneView(name, read_curve(out / f"lane_{name}"), done=True)
        for name in started
    }

    mode_auto = sweep["slots"] == "auto"
    util_hist: list[float] = []
    state = {"last_launch": 0.0, "hold": None, "vram_lane_max": None}

    def _launch(item: dict) -> None:
        lane_dir = out / f"lane_{item['name']}"
        lane_dir.mkdir(parents=True, exist_ok=True)
        log = open(lane_dir / "run.log", "a")
        proc = subprocess.Popen(
            [sys.executable, str(PROBE), "--out", str(lane_dir),
             *sweep["probe_args"], *item["args"]],
            stdout=log, stderr=subprocess.STDOUT)
        live[item["name"]] = Lane(item["name"], lane_dir, proc)
        state["last_launch"] = time.time()
        state["hold"] = None
        reg.append(item["name"], "start", " ".join(item["args"]), pid=proc.pid)
        print(f"[sweep] start {item['name']} pid={proc.pid}", flush=True)

    def start_next(cur_views: dict | None = None) -> None:
        if not queue:
            return
        if not mode_auto:
            while len(live) < sweep["slots"] and queue:
                _launch(queue.pop(0))
            return
        # auto mode (invariant 11): sample hardware, launch at most one lane per poll
        hw = sample_hw()
        if hw["gpu_util"] is not None:
            util_hist.append(hw["gpu_util"])
            del util_hist[:-3]
            hw = {**hw, "gpu_util": sum(util_hist) / len(util_hist)}
        obs = [hw["proc_vram_gb"].get(l.proc.pid, 0.0) for l in live.values() if l.proc]
        for name in live:
            v = (cur_views or {}).get(name)
            if v and v.rows:
                obs.append(max(float(r.get("vram_gb") or 0.0) for r in v.rows))
        seen = max(obs, default=0.0)
        if seen > 0:
            state["vram_lane_max"] = max(state["vram_lane_max"] or 0.0, seen)
        since = (time.time() - state["last_launch"]) / 60 if state["last_launch"] else 1e9
        ok, reason = should_launch(hw, state["vram_lane_max"], len(live), since, sweep["auto"])
        if ok:
            _launch(queue.pop(0))
        elif reason != "settling" and state["hold"] != reason:
            state["hold"] = reason
            detail = (f"bottleneck: {reason} (live={len(live)}, queued={len(queue)}, "
                      f"vram_free_gb={hw['vram_free_gb']}, gpu_util={hw['gpu_util']}, "
                      f"load1={round(hw['load1'], 1)}/{hw['cores']})")
            reg.append(None, "hold", detail)
            print(f"[sweep] hold — {detail}", flush=True)

    start_next()
    while live or queue:
        time.sleep(POLL_SECONDS)
        for name, lane in list(live.items()):
            view = lane.view()
            views[name] = view
            if not view.process_alive:  # exited on its own
                event = "finished" if view.done else "error"
                reason = "" if view.done else "process exited without DONE"
                reg.append(name, event, reason,
                           view.rows[-1]["env_steps"] if view.rows else None)
                reasons[name] = reason
                del live[name]
                continue
            d = decide_gates(view, sweep["gates"])
            if d:
                reg.append(name, d.event, d.reason, d.env_steps, d.metric, lane.proc.pid)
                reasons[name] = d.reason
                kill_lane(lane)  # registry line written first (invariant 7)
                del live[name]
                print(f"[sweep] {d.event} {name}: {d.reason}", flush=True)
        terminal = reg.terminal()
        for name, d in engine.check(views, terminal):
            if name in live:
                reg.append(name, d.event, d.reason, d.env_steps, d.metric, live[name].proc.pid)
                reasons[name] = d.reason
                kill_lane(live[name])
                del live[name]
                print(f"[sweep] {d.event} {name}: {d.reason}", flush=True)
        terminal = reg.terminal()
        write_scoreboard(out, views, terminal, reasons)
        start_next(views)

    terminal = reg.terminal()
    write_scoreboard(out, views, terminal, reasons)
    ranked = [(rung_metric(v.rows, v.rows[-1]["env_steps"]), n)
              for n, v in views.items() if v.rows]
    ranked = [(m, n) for m, n in ranked if m is not None]
    if ranked:
        best = max(ranked)
        print(f"[sweep] top lane: {best[1]} (rolling-median det_norm {best[0]})", flush=True)
    ok = all(terminal.get(n) in {"finished", "gate_kill", "rung_kill"} for n in views)
    return 0 if ok and not queue else 1


# --- dry-run: replay existing lane dirs through the rules (fixture/test entry point) ---
def run_dry(sweep: dict, out: Path) -> int:
    lane_dirs = {q["name"]: out / f"lane_{q['name']}" for q in sweep["queue"]
                 if (out / f"lane_{q['name']}").exists()}
    full = {n: read_curve(d) for n, d in lane_dirs.items()}
    thresholds = sorted({r["env_steps"] for rows in full.values() for r in rows})
    engine = RungEngine(sweep["rungs"])
    terminal: dict[str, str] = {}
    for t in thresholds:
        views = {}
        for name, rows in full.items():
            visible = [r for r in rows if r["env_steps"] <= t]
            views[name] = LaneView(name, visible, done=(lane_dirs[name] / "DONE").exists())
        for name, view in views.items():
            if name in terminal:
                continue
            d = decide_gates(view, sweep["gates"])
            if d:
                terminal[name] = d.event
                print(f"would {d.event} {name} @env_steps<={t}: {d.reason}")
        for name, d in engine.check(views, terminal):
            if name not in terminal:
                terminal[name] = d.event
                print(f"would {d.event} {name} @env_steps<={t}: {d.reason}")
    for name in full:
        if name not in terminal:
            m = rung_metric(full[name], full[name][-1]["env_steps"]) if full[name] else None
            print(f"would keep {name} (rolling-median det_norm {m})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sweep = load_sweep(a.sweep)
    out = Path(a.out)
    return run_dry(sweep, out) if a.dry_run else run_live(sweep, out)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
