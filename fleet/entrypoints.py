"""Code-owned entrypoint table (docs/specs/run-registry.spec.md + task-dispatcher.spec.md).

`runq add` uses `argv` + `--print-run-identity` to hash a task's config before any money is
spent (registry spec invariant 9). The dispatcher uses `completion_artifact` to verify a `DONE`
task actually produced its expected output (dispatcher spec invariant 9d) and `resume_flag` to
decide whether a pulled checkpoint can be wired back in via `--init-from` (invariant 16).
`argv` is relative to repo root both locally (where `runq add` runs the handshake) and remotely
(where the worker execs with `cwd=active/<id>/repo/`, the extracted `git archive` of the same
tree) — a `git archive` preserves the same relative layout, so one invocation form works both
places without translation.

All `native` trainers were adopted onto this contract 2026-07-08 (`--print-run-identity` +
`--out`, added to each trainer via `run_identity.print_run_identity`) and are now
LIVE. `train_dmc`'s resume support (optimizer + step counters, not just weights) landed
independently the same day via a parallel session (spec/native/m13-planet-h2h.spec.md Locked
decision 16); its `--print-run-identity` was reconciled onto the same shared helper as the rest
during the merge. Notes on what actually changed per trainer (none of this altered default
behavior when `--out`/`--print-run-identity` aren't passed — every trainer still respects its
old env var/hardcoded default when `--out` is omitted):
- `train_atari`/`train_minatar_multi`/`train_craftax`/`train_stream` gained a new `--out` flag
  (previously env-var-or-hardcoded only).
- `train_dmc`/`train_grid`/`train_chess` already had `--out`; only `--print-run-identity` was
  added.
- `train_minatar`/`train_continuous` had NO argparse at all before this — both gained a minimal
  CLI (`--seed`/`--out`/`--print-run-identity`, plus `--level` for `train_continuous`) without
  changing what running them with zero args does.
- `resume_flag` reflects each trainer's actual `--init-from` support, not a target: `train_atari`/
  `train_minatar_multi`/`train_stream` do weight-init-only resume (no optimizer/step state);
  `train_chess` and `train_dmc` are genuine step+optimizer resumes (M12 spec decision 18 /
  M13 Locked decision 16 respectively) — see `docs/specs/task-dispatcher.spec.md` invariant
  16's correction. The rest have no `--init-from` at all (`resume_flag=None` is accurate, not a
  gap to close later).
- Every trainer needs `numpy>=2,<3` (checkpoint-pickle compatibility, root `pyproject.toml`'s own
  rationale) and `tensorboard` (`SummaryWriter`) as baseline `pip_extras` — the box's bare
  `pytorch/pytorch:*` image ships neither. `smoke` is the one exception (stdlib-only by design).
- Queue `train_dmc` with `--tag .` in the entrypoint args so its run dir equals the worker's
  `--out` (where the completion/preemption contract looks for `results.json`/`ckpt_latest.pt`).
- **A live dispatcher caches this table at start** — restart it (`make dispatch`) after adding or
  changing an entry, or the running daemon won't see it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entrypoint:
    argv: list = field(default_factory=list)  # invocation prefix, task's own args appended after
    completion_artifact: str = ""  # relative to the task's out dir; existence gates `done`
    resume_flag: str | None = None  # e.g. "--init-from"; None = no resume support
    live: bool = False  # True iff it actually implements --print-run-identity/--out today
    pip_extras: list = field(default_factory=list)  # installed on the box before first run
    # System packages the dispatcher apt-installs over ssh AT SHIP TIME (idempotent dpkg-guard;
    # dispatcher-side so task.json's schema — and already-deployed workers — stay untouched).
    # Exists because pip can't express system deps: dm_control's EGL import needs libEGL.so.1,
    # absent from stock Vast pytorch images (dispatcher-spec retrospective bug 8).
    apt_packages: list = field(default_factory=list)


_BASE_EXTRAS = ["numpy>=2,<3", "tensorboard"]  # every real trainer needs these; smoke is stdlib-only

ENTRYPOINTS: dict[str, Entrypoint] = {
    # 2026-07-19 (trainer-harness spec, Phase 3): every legacy named trainer row was REMOVED when
    # the trainers themselves were tombstoned (import-time error; migrate to shared.infra.harness).
    # Jobs now self-describe via a `job` section in their config (`runq add --config PATH` /
    # `submit --config PATH`) — the only
    # named entrypoint left is the dispatch-canary `smoke`. Adding a new named row requires the
    # trainer to be harness-based (tests/test_trainer_checkpoint_hygiene.py enforces).
    "smoke": Entrypoint(
        argv=["python", "fleet/smoke_entrypoint.py"],
        completion_artifact="summary.json",
        resume_flag="--init-from",
        live=True,
    ),
}



def get(name: str) -> Entrypoint:
    try:
        return ENTRYPOINTS[name]
    except KeyError:
        raise SystemExit(f"unknown entrypoint {name!r}; known: {sorted(ENTRYPOINTS)}") from None


def resolve(task_row) -> Entrypoint:
    """The ONE place the run contract is resolved (job-artifact-contract spec inv. 5/6).

    If the task carries a `job_manifest_json` (a self-describing artifact), derive the Entrypoint
    from THAT — the coordinator never consults its in-memory `ENTRYPOINTS` table, so a manifest job
    can NEVER be rejected as an `unknown entrypoint` and needs no `dispatch-restart` to be accepted.
    Otherwise (a legacy named task) fall back to the table exactly as before. Accepts a dict or a
    sqlite3.Row."""
    try:
        jm = task_row["job_manifest_json"]
    except (KeyError, IndexError, TypeError):
        jm = None
    if jm:
        import job_manifest  # lazy: avoids an import cycle (job_manifest.to_entrypoint imports us)
        return job_manifest.to_entrypoint(job_manifest.parse(json.loads(jm)))
    return get(task_row["entrypoint"])


def live_entrypoints() -> dict[str, Entrypoint]:
    return {k: v for k, v in ENTRYPOINTS.items() if v.live}


def entry_source_path(entry: Entrypoint) -> str | None:
    """Repo-relative source path of the entry module, to be KEPT AS SOURCE when the bundle is
    compiled (a compiled extension module can't be `python -m`-run — task-bundle spec invariant 7).
    `python -m native.training.m36` -> `src/native/training/m36.py`; a `python <script.py>` form ->
    the script path itself. Returns None if the form isn't recognized (compile keeps nothing extra).
    """
    argv = list(entry.argv)
    if len(argv) >= 3 and argv[0] == "python" and argv[1] == "-m":
        return "src/" + argv[2].replace(".", "/") + ".py"
    if len(argv) >= 2 and argv[0] == "python" and argv[1].endswith(".py"):
        return argv[1]
    return None
