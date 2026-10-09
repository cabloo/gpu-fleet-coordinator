# Feature: runq sweep — declarative parameter sweeps over harness configs

> **Spec-driven.** This file is the source of truth for behavior. Implement STRICTLY to it — no
> behavior that isn't specified. If anything here is ambiguous or underspecified, STOP and record
> it under **Open questions** rather than guessing. Iterate by editing this spec, then implement
> the diff. If code and spec disagree, the spec wins (or we change the spec).

- **Owning module:** `fleet` (the coordinator CLI)
- **Module path:** `fleet/runq.py` (new `sweep` subcommand) + `fleet/sweep_expand.py`
- **Status:** built <!-- draft → approved → built --> (user approval 2026-07-19 "implement"; built +
  verified same day — 9 tests + real-m36 E2E incl. idempotent re-run; surfaced and fixed the
  harness default-list `--set` gap, trainer-harness Behavior 1)
- **Spec file:** `docs/specs/runq-sweep.spec.md`

## Purpose

Give sweeps a first-class, declarative surface: one `sweep.json` naming a base **config** plus axes
of `--set` overrides expands into N queued tasks — replacing hand-issued `runq add` loops and the
legacy bash grids. Enabled by the trainer harness (`spec/infra/trainer-harness.spec.md`): a cell is
exactly `the named config ⊕ per-cell --set overrides`, so expansion needs no generated config files, and
the existing per-cell identity handshake + `config_hash` dedupe make re-running a sweep idempotent
for free. (Distinct from the legacy `sweep_supervisor.py` co-tenant lane scheduler — that is the
pre-runq path and is untouched.)

## Input contract

CLI (trust boundary — validate everything):

```
python fleet/runq.py sweep SWEEP_JSON [--dry-run] [--group G] [--by A] [--force]
                                  [--max-cells N] [--est-minutes M] [--priority P] [--db PATH]
                                  [--box ID_OR_LABEL] [--no-colocate] [--colocate-by PATH ...]
```

`SWEEP_JSON` schema (`sweep_version` must be 1; unknown keys → exit 2 naming the key):

| field | required | meaning |
|---|---|---|
| `sweep_version` | yes | must equal 1 |
| `config` | yes | **full path to the base trainer config file** (v2, 2026-07-27; replaces `job`, which named a *directory* carrying a `job.json`). Its reserved `job` section is the run contract, parsed by the existing `job_manifest`; its `run` must invoke a harness trainer — the handshake enforces this de facto. Naming the config here is what makes a cell self-describing: under the old `"job": "."` form every sweep silently inherited whatever config the mutable repo-root `job.json` pointed at, so a sweep file could queue cells for a config it never mentions (this happened — see job-artifact-contract § "Why v2"). A directory, or a path with no `job` section, → exit 2 |
| `group` | yes* | task group (`--group` flag overrides; *required in file or flag) |
| `axes` | yes | object: dotted `--set` path → **non-empty array** of JSON values. Cartesian product over axes in sorted-key order. An empty `axes` object contributes zero cells (it does NOT enqueue the bare base run); the expansion must yield ≥1 cell overall (else exit 2) |
| `cells` | no | array of explicit extra cells, each an object of dotted path → value, appended after the product verbatim |
| `name_template` | no | template over the cell's axis paths: each `{dotted.path}` occurrence is substituted with the rendered value (Behavior 4 rendering; no format specs; unknown path in the template → exit 2); default = auto-naming (Behavior 4) |
| `est_minutes` | no | per-cell estimate (flag > file > manifest `resources.est_minutes`, the existing resolution) |
| `priority`, `slots`, `max_retries` | no | passed to every cell (defaults 50 / 1 / 10) |
| `colocate` | no | `false` disables per-seed co-location (Behavior 9); a dotted path — or an array of them — names what makes two cells the SAME PAIRED SEED instead of the auto-detected seed axis. Omit for the default |

Axis values may be any JSON value (scalars for normal knobs; objects allowed, e.g. a whole
`curriculum.N.cfg`). Flags: `--dry-run` prints the expansion and writes nothing; `--max-cells`
(default 64) is a fat-finger guard — an expansion larger than the cap exits 2 before any enqueue;
`--by`/actor resolution and `--force` behave exactly as `runq add` (runq-actor rules unchanged).

## Output contract

- Per cell, one task enqueued via the existing manifest-add path (`_add_job` internals), with
  `entry_args = ["--set", "<path>=<json.dumps(value)>", ...]` in sorted-path order appended after
  the manifest's `run`. No new task-row columns; `job_manifest_json`, `config_json`,
  `config_hash`/`arm_hash`, snapshot shipping — all exactly as a hand-issued `runq add --job`.
- stdout summary: one line per cell — `name`, verdict (`queued` / `skipped-duplicate` /
  `DRY`), and on completion a totals line `queued=X skipped=Y`. Exit 0 iff every cell either
  queued or was a duplicate; exit 2 on validation errors; first other add-failure aborts the
  remaining cells (already-queued cells stay — the re-run after a fix skips them as duplicates).

## Public API

CLI-only (`runq sweep`). `fleet/sweep_expand.py` exposes for tests:

```python
def expand(sweep: dict) -> list[Cell]     # Cell = (name: str, overrides: dict[str, Any])
def cell_args(overrides: dict) -> list[str]   # ["--set", "path=jsonvalue", ...] sorted by path
def pairing_paths(cells: list[Cell], by=None) -> list[str]        # what makes two cells one seed
def colocate_keys(group: str, cells: list[Cell], by=None) -> dict[str, str]   # name -> group key
```

Everything else module-internal.

## Dependencies

- `fleet/runq.py` add path (`_add_job`, `_finalize_task`, `_dedupe_guard` exit-3 semantics,
  actor resolution) and `job_manifest` — consumed, not modified except to register the subcommand.
- The trainer-harness `--set`/`--print-run-identity` contract (`spec/infra/trainer-harness.spec.md`)
  — each cell's handshake validates its overrides (an invalid axis path fails the cell's add with
  the harness's dotted-path error).
- `docs/specs/job-artifact-contract.spec.md` (manifest), `docs/specs/run-registry.spec.md`
  (identity/dedupe). No dispatcher changes.

## Behavior & invariants

1. **Expansion is deterministic:** cartesian product over `axes` in sorted-key order (values in
   file order), then explicit `cells` in file order. Same sweep file → identical cell list,
   names, and `entry_args` ordering (so identity hashes are stable across re-runs).
2. **Value encoding:** every override value is rendered as `json.dumps(value)` (compact) inside
   `--set path=VALUE` — the harness parses it back as a JSON literal, so strings/bools/numbers/
   objects round-trip exactly and two encodings can never alias to different hashes.
3. **Idempotent re-run:** a cell whose add is refused as a config-hash duplicate (exit-3 clash
   semantics, any non-cancelled/failed state) is reported `skipped-duplicate` and does not fail
   the sweep. A `UNIQUE(grp,name)` violation with a *different* config hash is an error (auto-name
   collision) → abort. `--force` passes through to every cell.
4. **Auto-naming** (when no `name_template`): for each axis in sorted order take the last
   non-numeric dotted segment as the short key; if two axes share a short key, both use their full
   path with `.`→`-`. Values render as: scalars via `str()` (`True`/`False` → `T`/`F`), objects/
   arrays via `sha1(canonical-json)[:6]`. Sanitize `[^A-Za-z0-9._-]`→`-`, join axis terms with
   `_`. Explicit `cells` entries are named the same way. Names must be unique within the sweep →
   duplicate generated names exit 2 before any enqueue.
5. **Fail-closed file parsing:** unknown top-level key, empty axis array, non-object cell, cap
   exceeded, missing group, or unparseable `job.json` → exit 2, nothing enqueued.
6. **`--dry-run`** prints the full cell table (name + entry_args) with no handshake, no DB access,
   no snapshot; exit 0.
7. **Ordering under failure:** cells are added strictly in expansion order; the first failure that
   is not a duplicate aborts the remainder (partial sweeps are safe by invariant 3).
8. **Seeds are just an axis** (`"seed": [0,1,2]`): the arm-level dedupe warning behaves as with
   any hand add; the POC default (single seed) is whatever the base config carries — the sweep
   file adds seeds only explicitly (directional-not-magnitude convention).
9. **PER-SEED CO-LOCATION, ON BY DEFAULT** (2026-08-16 owner directive: *"have paired seed tests
   always use it"*; dispatcher invariant 4g owns the placement half). Every cell is stamped with
   `resource_hint.colocate = "<group>:<rendered seed terms>"`, so **all arms of one seed run on one
   box and different seeds are free to land anywhere**. This is the only place that KNOWS which
   cells are the same seed, which is why the default lives here rather than in `add`.
   - *Why it must be the default.* A paired comparison measured across two boxes is not paired: the
     box selects the attractor on a bistable rung, and a control that collapsed on the other machine
     manufactures a win. Before this, `runq sweep` had no box control of any kind, so every
     multi-arm sweep was a box lottery **by construction** — and the resulting split was invisible
     in the outputs. A default that must be remembered is the same defect with extra steps.
   - *Seed detection:* every override path whose last non-numeric dotted segment is `seed`/`seeds`
     (case-insensitive; `"seeds": [1]` is the repo's dominant form and a one-element list renders as
     its element, so keys stay readable). `--colocate-by PATH` (repeatable) or the file's `colocate`
     field overrides the detection; a named path that NO cell overrides is exit 2, because it would
     silently collapse the whole sweep into one group — the failure most easily mistaken for the
     feature working.
   - *A sweep with no seed path is ONE group*: its seed comes from the base config, so every cell IS
     the same paired seed. That is the 1-seed scout, this repo's most common paired comparison.
     A cell that omits an otherwise-present seed path likewise groups with the other such cells.
   - *Opt out with `--no-colocate`* (or `"colocate": false`) when the cells are an independent grid
     rather than a paired comparison — there, forcing a seed's cells onto one box only costs
     parallelism.
   - *Group sizes are printed* (`[sweep] colocate <key>: N cell(s) share one box`) and a group over
     8 cells WARNS on stderr: a group cannot run wider than the box it pins, so anything past that
     box's lane count serialises, and the operator must see that before the spend rather than infer
     it from a slow queue. It is a warning, never a refusal — the dispatcher's 4g3 fallback means an
     oversized group still runs.
   - *`--box` and co-location are mutually exclusive* (exit 2). `--box` already forces every cell
     onto one named machine, pairing seeds with each other that nothing required; accepting both
     would make one of the two silently inert. `--box` exists on `sweep` at all only because the
     gap was real (pin an owned box for a hardware question); per-seed co-location is the right tool
     for arm-vs-arm comparability.
   - *Verification is a separate command*, `runq colocate [--verify]` — see dispatcher 4g5. The
     dispatcher co-locates BEST-EFFORT (a torn-down box releases the pin and the group re-pins), so
     "the sweep asked for pairing" never licenses a paired verdict on its own.

## Fixtures

`docs/specs/fixtures/runq-sweep/` — these become the tests:

1. `basic.sweep.json` (axes `plan_kappa: [0.5, 2.0]` × `curriculum.0.rounds: [10, 20]` + one
   explicit cell `{plan_kappa: 4.0}`) → `basic.cells.json` golden: 5 cells with exact names per
   Behavior 4 in expansion order (`rounds10_plan_kappa0.5`, `rounds10_plan_kappa2.0`,
   `rounds20_plan_kappa0.5`, `rounds20_plan_kappa2.0`, `plan_kappa4.0`) and exact `entry_args`.
2. Name collision: two axes with the same last segment (`a.rounds`, `b.rounds`) → golden shows
   full-path names (`a-rounds10_b-rounds20`); an object-valued axis renders as its 6-char hash.
3. Idempotence (temp `--db`, toy harness job dir): run the sweep twice — first run `queued=N
   skipped=0`, second `queued=0 skipped=N`, both exit 0.
4. Invalid axis path (`n_prims`): the first cell's handshake fails with the harness's dotted-path
   error → sweep exits nonzero, zero *further* cells enqueued.
5. `--max-cells 3` against a 4-cell sweep → exit 2, temp DB has zero rows.
6. `--dry-run` against the same → exit 0, prints 4 cells, temp DB untouched.

## Resolved decisions

- **Rides the manifest path only.** No named-entrypoint sweeps (the table is just `smoke` since
  the 2026-07-19 tombstoning) and no generated config files — cells are `--set` overrides.
- **No scheduler semantics in v1**: no early-pruning/cancel-losers automation, no seed escalation,
  no status view — `runq ls --group` + `runq cancel` already cover the directional workflow
  (actively cancelling foregone losers stays a human/agent call). Revisit only if the manual loop
  proves painful.
- **`--max-cells` default 64** is a fat-finger guard on accidental cartesian blowups, not a cost
  control (queue-blindly still stands).
- **Resume lineage stays out of the hash** (carried decision — `spec/DECISIONS.md` entry stands;
  warm-start sweeps use `--init-from` per cell manually if ever needed, not in v1).

## Open questions

None. (Approval flips Status to `approved`; implementation is the subcommand + expander +
fixtures above.)
