# Feature: sweep supervisor (early-pruning lane scheduler)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (experiment tooling)

> **Note (2026-07-07):** the cwm track — and with it `fleet_capacity_probe.py` /
> `launch_probe.sh` / `launch_sweep.sh`, this spec's original workload and remote wrappers — was
> retired (archived + in git history). The supervisor itself is workload-generic: the lane
> protocol below (CLI passthrough, `curve.jsonl`, `DONE` marker, `--out` layout) is the normative
> contract any future workload implements.
- **Module path:** `fleet/sweep_supervisor.py`
- **Status:** built <!-- draft → approved → built; approved 2026-07-03 (owner: single-seed v1, gate defaults delegated) -->
- **Spec file:** `docs/specs/sweep-supervisor.spec.md`

## Purpose

Run a queue of capacity-probe config variants as co-tenant lanes on one GPU box, watch each
lane's eval stream, **prune bad configs early** (hard failure gates + rung-based selection), and
refill freed slots from the queue — so a fixed rental budget concentrates compute on promising
parameter choices instead of finishing doomed runs. Encodes the empirically-validated signals
from `VAST-TEST.md`: entropy death is the early kill-signal; ranking uses rolling-median
normalized return at matched env-steps; WM loss is a sanity gate only.

## Input contract

- **Sweep file** (`--sweep sweep.json`, validated at startup — trust boundary): 
  ```json
  {
    "slots": 4,
    "probe_args": ["--total-hours", "3.5", "--phase2-hours", "0.75"],
    "rungs": [
      {"env_steps": 5000,  "keep_fraction": 0.5},
      {"env_steps": 15000, "keep_fraction": 0.5}
    ],
    "gates": {"entropy_floor": 0.02, "entropy_by_env_steps": 10000, "stall_minutes": 30},
    "queue": [
      {"name": "tr1",          "args": ["--train-ratio", "1"]},
      {"name": "tr1_rw05",     "args": ["--train-ratio", "1", "--set", "world_model.dyn_reward_reweight=0.5"]},
      {"name": "tr1_rw1_s1",   "args": ["--train-ratio", "1", "--set", "world_model.dyn_reward_reweight=1.0", "--seed", "1"]}
    ]
  }
  ```
  Unknown keys → error. `slots` is an int ≥ 1 (fixed cap) **or the string `"auto"`**
  (hardware-adaptive concurrency — invariant 11 — for running the same sweep on an 8GB desktop
  card, a rented 3090, or anything else without retuning). With `"auto"`, an optional `"auto"`
  block tunes it: `{"util_ceiling": 90, "settle_minutes": 3, "max_slots": 8}` (all optional,
  defaults in code). `queue` non-empty; rung `env_steps` strictly increasing;
  `0 < keep_fraction ≤ 1`. `gates` and each of its keys are OPTIONAL; defaults live in code:
  `entropy_floor=0.02`, `entropy_by_env_steps=10000`, `stall_minutes=30` (calibrated on the
  2026-07-02 breakout campaign — see VAST-TEST.md; override per-sweep for other games).
  Every probe flag is passed through verbatim to
  `fleet_capacity_probe.py` (the probe validates its own args; unknown `--set` keys fail
  fast there).
- **Lane telemetry:** the probe's `curve.jsonl` rows (existing format; the supervisor consumes
  `env_steps`, `det_norm`, `ac_entropy`, `phase`, `mins`). The supervisor never parses TB events.

## Output contract

- **`<out>/registry.jsonl`** — one line per lifecycle event, append-only:
  `{"t": <iso8601>, "lane": <name>, "event": "start|gate_kill|rung_kill|finished|error|hold", "reason": <str>, "env_steps": <int|null>, "metric": <float|null>, "pid": <int|null>}`
  `hold` is informational and non-terminal (`lane` = `null`): emitted when auto-slot launching
  blocks, naming the identified bottleneck; at most one per contiguous hold period per reason.
- **`<out>/scoreboard.md` + `scoreboard.csv`** — rewritten after every decision: one row per lane
  (name, status, env_steps reached, rolling-median det_norm, peak det_norm, last ac_entropy,
  decision + reason), sorted by rolling-median det_norm desc.
- Each lane's own artifacts stay in `<out>/lane_<name>/` exactly as the probe writes them
  (curve.jsonl, summary.json, tb/, ckpts, DONE); killed lanes keep their partial artifacts.
- Exit status 0 iff the queue drained and all surviving lanes finished; the final message names
  the top lane by rolling-median det_norm.

## Public API

- CLI only: `python fleet/sweep_supervisor.py --sweep sweep.json --out <dir> [--dry-run]`.
  `--dry-run` replays existing lane dirs against the rules and prints the decisions it *would*
  make (no process management) — this is also the fixture-testing entry point.
- Module-internal otherwise. Decision logic (`decide(lane, engine, lanes, terminal, gates) ->
  Decision | None`) is a pure function importable by tests — composed from `decide_gates(lane,
  gates)` (hard gates first) then `RungEngine.check(lanes, terminal)` (this lane's slice of the
  rung pass); the earlier `rows, rung_state` args no longer exist.
- **Box-worker public surface:** `should_launch`, `sample_hw`, and `AUTO_DEFAULTS` are a stable
  public surface reused **verbatim** by the dispatcher's on-box worker, not merely test-importable —
  `spool_worker.py` imports `from sweep_supervisor import AUTO_DEFAULTS, sample_hw, should_launch`
  and `dispatcher.py` ships `sweep_supervisor.py` to the box as a sibling module.

## Dependencies

- `fleet_capacity_probe.py` CLI (args passthrough, curve.jsonl format, DONE marker,
  `--out` layout) — the only coupling; no `src/cwm` imports.
- POSIX process management (`subprocess.Popen`, `terminate()`); stdlib only. Runs **on the box**
  (same host as the lanes — a rented instance or the local 8GB workstation; launch from repo
  root with `PYTHONPATH=src`); remote orchestration stays in `watch_and_pull.sh`, which needs no
  changes (it already syncs arbitrary `<out>/lane_*/` trees).
- `nvidia-smi` (optional): auto-slot GPU checks; absent → invariant 11d.

## Behavior & invariants

1. **Slots:** at most `slots` lanes run concurrently; a freed slot is refilled from the queue
   head immediately. Lanes launch with `--out <out>/lane_<name>` + `probe_args` + lane `args`.
2. **Poll loop:** every 60s the supervisor re-reads each live lane's `curve.jsonl` (tolerating
   partial last lines) and applies, in order: hard gates → rung rules.
3. **Hard gates** (kill regardless of rank, `event=gate_kill`):
   a. *Entropy death:* `phase=="learn"` and `env_steps ≥ gates.entropy_by_env_steps` and the last
      2 consecutive rows have `ac_entropy < gates.entropy_floor`.
   b. *Stall:* no new curve row for `gates.stall_minutes` while the process is alive.
   c. *Crash:* process exited without writing DONE → `event=error` (slot refilled; not ranked).
   d. NaN/inf in `det_norm` or `ac_entropy` in any row.
4. **Rungs** (`event=rung_kill`): when a lane first reaches rung `env_steps`, its **rung metric**
   = median of its last 3 `det_norm` values at or below that step. When *all* lanes that will
   ever reach the rung have reported (i.e. every live-or-finished lane launched before the rung
   check either reached it or died), rank reported lanes and kill the bottom
   `floor((1-keep_fraction) * n)`; ties keep both. Lanes launched later hit the same rung bar:
   a newcomer is killed at the rung iff its metric is below the *worst surviving* incumbent's
   metric at that rung (no re-litigation of incumbents).
5. **Matched-data fairness:** all rung/gate comparisons key on `env_steps`, never wall-clock.
6. **Kill semantics:** SIGTERM, 30s grace, SIGKILL; then write the registry line and refill. A
   killed lane's partial artifacts are never deleted.
7. **Every decision is written to `registry.jsonl` with a human-readable reason** before the
   process is signaled (crash-safe ordering).
8. **Idempotent restart:** on startup with an existing `<out>`, the supervisor reconstructs state
   from `registry.jsonl` + lane dirs (finished/killed lanes are not relaunched; queue resumes
   after the last started name). It never adopts orphan processes — a lane alive on disk but
   with no live pid is marked `error`.
9. The supervisor never modifies lane checkpoints or configs mid-flight (no PBT in v1 — see
   Open questions).
10. Validate the sweep file at the boundary (item 1 of Input contract); reject rather than guess.
11. **Auto slots** (`slots: "auto"`) — queue work until the hardware bottleneck is identified,
    hold, resume when it clears:
    a. If the queue is non-empty and no lane is live, always launch one (progress guarantee).
    b. A further launch requires ALL of: `settle_minutes` elapsed since the last launch (lets
       compile warmup + the new lane's VRAM footprint appear — settling is not a "hold");
       live lanes < `max_slots`; 1-min loadavg < cores − 1; rolling GPU util (mean of last 3
       samples) < `util_ceiling`; free VRAM ≥ 1.25 × the max per-lane GPU memory observed this
       sweep (before any observation exists: free ≥ 20% of total VRAM).
    c. When a launch is blocked by (b), record one `hold` registry event naming the binding
       bottleneck (`max_slots|cpu_load|gpu_util|vram`) — once per contiguous hold period per
       reason; launching resumes automatically when conditions clear (e.g. a lane finished and
       freed VRAM).
    d. Machines without `nvidia-smi`: GPU checks pass vacuously; `cpu_load` and `max_slots`
       still bind.
    e. The launch decision is a pure function
       `should_launch(hw, vram_per_lane_max, n_live, since_launch_min, auto_cfg) -> (bool, reason)`
       importable by tests; hardware sampling is injected, never called inside it.
    f. Per-lane VRAM observation = max(nvidia-smi per-process memory for lane pids, probe
       `vram_gb` curve values).

## Fixtures

Golden decision traces (these become the tests, driven through `--dry-run` / `decide()`):

- `fixtures/sweep/entropy_death.jsonl` → curve where `ac_entropy` = 0.31, 0.05, 0.010, 0.008 at
  env_steps 2.5k/5k/10k/12.5k, `phase=learn` → expect `gate_kill` at the 12.5k row (two
  consecutive sub-floor rows past 10k), reason mentions entropy.
- `fixtures/sweep/rung_prune.json` → 4 lanes with rung-1 (5k) metrics 0.031/0.016/−0.005/−0.031,
  `keep_fraction=0.5` → expect the bottom two `rung_kill`ed, top two alive.
- `fixtures/sweep/transient_trap.jsonl` → det_norm 0.047 at 5k then 0.005/0.000 by 15k vs a
  steady 0.021 lane → rolling-median ranking keeps the steady lane at rung 2 (the transient is
  NOT protected by its early peak).
- `fixtures/sweep/late_joiner.json` → newcomer reaches rung 1 after incumbents were pruned;
  metric above worst-survivor → alive; below → `rung_kill`.
- `fixtures/sweep/stall.jsonl` → live pid, last row older than `stall_minutes` → `gate_kill(stall)`.
- `fixtures/sweep/auto_slots.json` → `should_launch` cases: (i) 8GB card, free 1.0GB, per-lane
  max 1.2GB → `(False, "vram")`; (ii) util 95 ≥ ceiling 90 → `(False, "gpu_util")`; (iii) lane
  finished → free 6GB, util 40 → `(True, ...)`; (iv) no nvidia-smi (GPU fields null), low load,
  n_live < max_slots → `(True, ...)`; (v) n_live == max_slots → `(False, "max_slots")`;
  (vi) 1 min since launch < settle 3 → `(False, "settling")`; (vii) n_live 0 → always
  `(True, ...)` regardless of hardware.
- Registry round-trip: replaying any fixture twice (restart) yields no duplicate events.

## Open questions

None blocking. Resolved 2026-07-03 (owner):
- **Eval noise vs rung confidence** → accept single-seed noise for v1; revisit (seed-siblings /
  wider rung batteries) when the sweep goal shifts to fine-tuning.
- **Gate defaults** → defaults live in code (`entropy_floor=0.02`, `entropy_by_env_steps=10000`,
  `stall_minutes=30`; calibrated on the 2026-07-02 breakout campaign), overridable per-sweep.

Deferred to v2 (out of scope for this build):
- **PBT:** freed slots warm-starting from the leader's `ckpt_best.pt` with perturbed
  non-architecture params. Interacts with rung fairness (inherited weights break
  matched-env_steps comparison).
- **Cross-box scale-out:** one supervisor per box with a shared queue vs one supervisor over N
  boxes. Blocked on wanting >4–6 concurrent configs in practice.
