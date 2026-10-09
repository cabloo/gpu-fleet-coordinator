# Feature: est_minutes feedback loop (learned per-entrypoint defaults)

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet`
- **Module path:** `fleet/est_defaults.py` (+ a change to `fleet/runq.py`)
- **Status:** built <!-- draft → approved → built -->  (2026-07-13; four design decisions locked with the user; O1–O3 non-blocking). **Extended 2026-07-31** with the IN-FLIGHT arm (`learn_group_estimates`, dispatcher invariant 24) — see "Two arms" below.
- **Spec file:** `docs/specs/est-defaults.spec.md`

## Two arms (read this first)

This feature has **two** producers of a learned `est_minutes`, with different keys and different
lifecycles. The original one below is the SIDECAR arm; the second was added 2026-07-31.

| | SIDECAR arm (original) | IN-FLIGHT arm (2026-07-31, dispatcher invariant 24) |
|---|---|---|
| key | `entrypoint` | `(entrypoint, **group**)` |
| producer | `est_defaults.py` CLI, run + committed BY HAND | `learn_group_estimates`, called by the dispatcher every cycle |
| consumer | `runq add` when `--est-minutes` is omitted | `Dispatcher._effective_est` → both view builders |
| effect | sets the value STORED on the task row | corrects only the SCHEDULING view; the row is never rewritten |
| automatic? | **no** — needs a human to regenerate and commit | **yes** — no CLI, no commit, no restart |

**Why the second arm exists.** The sidecar's key stopped being a workload. When jobs moved to
self-describing configs, ONE entrypoint (`native.training.m49_curriculum_ab`) came to span 270 groups
whose runtimes run p10 6 min → median 33 → p90 182 — so a single per-entrypoint p90 is 5.5× the
median task and 29× the p10 task, i.e. it MANUFACTURES the over-estimation this feature exists to
remove. Group is predictive and cheaply so: within-group sd 31 min vs between-group sd 81 min over
the 90 groups with n≥5. (`config_hash` is not a usable key — 1172 distinct hashes over 1182 tasks,
none recurring, because every sweep cell varies a knob.) A group is one campaign, so once a few
cells finish, the rest of that campaign re-estimates from its own siblings.

Both arms use `ceil(p90)`, never the median: over-estimation wastes budget, under-estimation causes
hard-cap eviction and stall-reap, so the loop is deliberately asymmetric.

## Purpose

Today `runq add --est-minutes` is a **required, hand-typed guess**. The live registry shows it is
systematically wrong (per-entrypoint actual/estimate median ratios of ~0.36–1.62), and the estimate
drives the rented-window sizing (`_window_minutes_needed = est_minutes × est_safety + pull_margin_min`),
so a bad guess wastes budget (over-rent) or causes hard-cap eviction / stall-reap (under-rent). This
feature closes the loop: derive a per-entrypoint `est_minutes` default from **reconstructed historical
runtime** (the same actuals `calibration.py` already computes), persist it to a committed JSON sidecar,
and have `runq add` use it when `--est-minutes` is omitted. This resolves **Q3** of
`docs/specs/calibration.spec.md` (previously deferred).

This is the **producer/consumer of learned estimates only**. It changes no dispatch decision logic,
no schema, and `est_safety` (the global 1.25 window margin) is untouched — the loop makes the *input*
`est_minutes` track reality; the existing safety margin still layers on top.

## Input contract

- **Read side (recommender):** the registry `experiments/runs.sqlite` (schema v1) — **module-internal**
  reuse of `calibration.py`'s read + reconstruction (same package), not a new boundary. Specifically it
  consumes `calibration._connect_ro`, `calibration._fetch`, `calibration.reconstruct_task_actuals`, and
  `calibration.percentile`. Read-only, exactly as `calibration.py` (`?mode=ro` + `PRAGMA query_only=ON`);
  never writes/migrates the DB.
- **Consumer side (`runq add`):** the existing CLI args, plus the persisted sidecar. `--est-minutes`
  changes from **required** to **optional**; the entrypoint name (already validated at
  `runq.cmd_add` against `entrypoints.get`) selects the learned default.
- **The sidecar** `fleet/est_defaults.json` — **internal** to this module; shape defined below.

## Output contract

- **`fleet/est_defaults.json`** (internal, this module owns the shape). Deterministic bytes for a
  given `(db, since, min_sample)` — `json.dumps(..., indent=2, sort_keys=True)`, trailing newline, **no
  timestamp or other wall-clock field** (so it is reproducible and diff-reviewable):

  ```json
  {
    "_meta": {
      "generated_from": "runs.sqlite",
      "generated_task_count": 47,
      "min_sample": 5,
      "since": null,
      "statistic": "ceil(p90) of done-task non-degenerate active_minutes"
    },
    "entrypoints": {
      "train_atari": {"est_minutes": 78, "n": 12, "actual_median": 64.0, "actual_p90": 77.3, "low_confidence": false},
      "train_new":   {"est_minutes": 40, "n": 2,  "actual_median": 38.0, "actual_p90": 39.5, "low_confidence": true}
    }
  }
  ```

- **Recommender stdout:** a human-readable table (entrypoint, n, actual_median, suggest_est=`est_minutes`,
  low-confidence marker), always printed. On a real (non-`--dry-run`) run it also writes the sidecar and
  prints `wrote <k> entrypoints to <path>`.

## Public API

Exposed OUTSIDE the module (imported by `runq.py`):

- `load_default(entrypoint: str, path: str | Path | None = None) -> int | None` — return the persisted
  `est_minutes` for `entrypoint`, or `None` if the sidecar is missing or has no entry for it. `path`
  defaults to `est_defaults.json` next to this module (`Path(__file__).parent / "est_defaults.json"`).
  Never raises on a missing file (returns `None`); a malformed/unreadable file also yields `None`.
- `resolve_est_minutes(explicit: int | None, entrypoint: str, loader=load_default) -> tuple[int, str]` —
  the resolution used by `runq add`. Returns `(value, source)` where `source ∈ {"explicit", "learned"}`.
  Raises `ValueError` if `explicit is None` and `loader` returns `None` (caller turns this into the
  `runq add` usage error). `explicit` (when not None) must be `> 0` or it is a `ValueError`.

Module-internal (unit-tested, not imported elsewhere):

- `derive_defaults(actuals: list[TaskActual], min_sample: int) -> dict` — pure. `actuals` are
  `calibration.TaskActual` objects (or equivalents exposing `.entrypoint`, `.state`, `.active_minutes`,
  `.degenerate`). Returns the `{"_meta": {...partial}, "entrypoints": {...}}` structure (the `_meta`
  fields it can compute without the DB path — `min_sample`, `generated_task_count`, `statistic`; the
  caller fills `generated_from`/`since`).
- `write_defaults(path, doc) -> None` — deterministic write (sort_keys, indent=2, trailing newline).
- `main(argv=None) -> int` — CLI.

## Dependencies

- `fleet/calibration.py` (same package) — `_connect_ro`, `_fetch`, `reconstruct_task_actuals`,
  `percentile`, `TaskActual`. Reused verbatim; this feature adds **no** new behavior to `calibration.py`
  and does not alter its report schema or fixtures.
- `fleet/registry_db.py` — `shared_experiments_root()` for the default DB path (same as
  calibration).
- `fleet/entrypoints.py` — only indirectly: `runq.cmd_add` already validates the entrypoint;
  no change to the `Entrypoint` dataclass (defaults live in the sidecar, **not** in code).

## Behavior & invariants

Numbered, testable.

1. **Population = successful, non-degenerate completions.** The recommender considers only tasks whose
   state is `done` (NOT `task_failed`) and whose reconstructed `active_minutes ≥ DEGENERATE_FLOOR_MIN`
   (2.0, reused from `calibration`). Rationale: `est_minutes` models how long a *successful* run occupies
   a box; `task_failed` runs have arbitrary, unrepresentative lengths, and degenerate (<2 min) rows are
   mis-marked crashes. (See Open question O1 for the `task_failed` alternative.)

2. **Grouping is by `entrypoint`.** Per entrypoint with `n ≥ 1` eligible tasks, compute:
   - `actual_median` = median of `active_minutes` (linear-interp median, reusing `calibration.percentile`
     at q=0.5 or `_median`), rounded to 1 decimal.
   - `actual_p90` = `percentile(sorted active_minutes, 0.90)` (numpy-linear, same helper), rounded to 1
     decimal.
   - `est_minutes` = `max(1, ceil(actual_p90))`. **p90 is the persisted default**; the median is
     informational. `ceil` because under-estimation is the asymmetric-costly failure (window too short →
     eviction), so we round the safe direction.
   - `n` = eligible count; `low_confidence` = `n < min_sample`.

3. **Low-confidence entrypoints are still emitted and still usable.** An entrypoint with `n < min_sample`
   gets a normal `est_minutes` value plus `"low_confidence": true`; `runq add` uses it exactly like any
   other (the flag is advisory — visible in the JSON diff and the recommender table, not enforced).

4. **No hard-cap clamping.** `est_minutes` reflects the true `ceil(p90)` even if the resulting window
   (`est_minutes × est_safety + pull_margin_min`) would exceed the dispatcher's `hard_cap_hours × 60`
   ceiling. That is a real capacity signal and is handled honestly by the dispatcher's existing
   infeasibility hold — the recommender does not hide it by clamping.

5. **`runq add` resolution order** (via `resolve_est_minutes`):
   1. explicit `--est-minutes N` given → use `N` (must be `> 0`, existing validation kept), `source="explicit"`.
   2. else learned default present for the entrypoint → use it, `source="learned"`; `runq` prints
      `runq add: using learned est_minutes=<N> for <entrypoint>`.
   3. else → usage error to stderr `runq add: --est-minutes required (no learned default for <entrypoint>)`,
      exit code 2 (matches the existing `--est-minutes must be > 0` error convention). No task is inserted.
   An explicit value ALWAYS overrides the learned default (never merged/averaged).

6. **Producer is idempotent & deterministic.** Same `(db, since, min_sample)` ⇒ byte-identical sidecar.
   No wall-clock, RNG, or dict-order nondeterminism (`sort_keys=True`). Re-running with no new tasks is a
   no-op diff.

7. **`--dry-run` writes nothing.** It prints the table and the would-be JSON to stdout and returns 0; the
   sidecar on disk is untouched.

8. **Empty / no eligible tasks = valid.** If no entrypoint has an eligible `done` task, the recommender
   writes `{"_meta": {...}, "entrypoints": {}}` (or prints it under `--dry-run`), exits 0. `load_default`
   then returns `None` for every entrypoint and `runq add` falls back to requiring `--est-minutes` (5.iii).

9. **Read-only on the registry.** The recommender never writes, migrates, or creates `runs.sqlite`
   (reuses `calibration._connect_ro`). The only file it writes is the sidecar (its `--out`).

10. **Missing/malformed sidecar is non-fatal for the consumer.** `load_default` returns `None` (never
    raises) when the file is absent, unparseable, or missing the entrypoint — so a fresh checkout with no
    sidecar simply behaves like the old required-flag world.

11. **Home-side only.** The sidecar is consumed exclusively at `runq add` (on the coordinator/home side).
    It is committed to git (reviewable), and while the shipped code snapshot (git archive on the legacy
    fallback path) will include it in the shipped bundle,
    **the box/worker never reads it** — by ship time `est_minutes` is already a fixed value on the task row.

12. **Validate at the boundary.** `load_default` treats the sidecar as untrusted input: non-int / non-dict
    entries yield `None` for that lookup rather than propagating a type error into `runq`.

## Fixtures

Golden input/output for the boundary — these BECOME the tests
(`tests/test_est_defaults.py`, `tests/fixtures/est_defaults/`).

- **`derive_defaults` (pure).** Input: a hand-built list of `TaskActual`-shaped rows across two
  entrypoints — `train_a` with 6 `done` runs (one degenerate <2 min, excluded) and `train_b` with 2
  `done` runs (below `min_sample=5` → `low_confidence: true`), plus one `task_failed` run (excluded by
  invariant 1). Golden output: `fixtures/est_defaults/derive_basic.json` with hand-verified
  `n`/`actual_median`/`actual_p90`/`est_minutes`/`low_confidence` for both, and `train_c` (only a
  `task_failed` run) absent entirely.
- **`load_default` + `resolve_est_minutes`.** Fixture `fixtures/est_defaults/sidecar.json` →
  `load_default("train_a")` returns its int; unknown entrypoint → `None`; missing-file path → `None`;
  a malformed fixture (`sidecar_malformed.json`, entry value a string) → `None`.
  `resolve_est_minutes(120, "train_a", loader)` → `(120, "explicit")`; `(None, "train_a", loader)` →
  `(<learned>, "learned")`; `(None, "unknown", loader)` → raises `ValueError`; `(0, "train_a", loader)`
  → raises `ValueError`.
- **End-to-end golden.** A seeded in-memory/temp `runs.sqlite` (a few `done` tasks across two entrypoints,
  one entrypoint below `min_sample`, one degenerate task, one `task_failed`) run through `main` →
  `fixtures/est_defaults/basic.est_defaults.json` golden (REGEN=1 regenerates). A determinism test asserts
  two runs produce identical bytes. A read-only test asserts the registry file mtime/size is unchanged
  after a recommender run.

## Open questions

- **O1 (population — `task_failed`).** Invariant 1 uses `done`-only. Alternative: include `task_failed`
  runs (they still occupied a box for `active_minutes`, so for *window-sizing* they are arguably
  relevant). Chosen `done`-only because failure lengths are arbitrary and would add noise to the p90;
  RESOLVE by confirming the loop should model successful-run duration, not box-occupancy duration.
  **Not blocking** — implement `done`-only; revisit if calibration shows failures carry real signal.
- **O2 (runq low-confidence surfacing).** `runq add` currently just prints the learned value (5.ii); it
  does not warn when `low_confidence` is true (the flag is visible in the JSON/recommender table only).
  Decide later whether `runq add` should also print a `(low-confidence, n=<k>)` note. **Not blocking.**
- **O3 (auto-regeneration cadence).** This spec is on-demand only (a human runs the recommender and
  commits the diff). Whether to schedule it (cron/loop) so defaults self-refresh is deferred to the
  trend-tracking followup (calibration Q4). **Not blocking.**
