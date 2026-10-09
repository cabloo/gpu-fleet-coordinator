"""Run watch — the standard campaign monitor for coordinator tasks (docs/specs/run-watch.spec.md).

Read-only over the run registry. **stdout is an event stream**: one line per thing an agent would act
on, sized for a line-by-line monitor (each line becomes a notification). Replaces the hand-rolled
`sleep N; runq ls` loop, whose recurring defect is that it greps the happy path only — so a crash, a
wedged box, or retries burning down to a permanent `infra_failed` all look identical to "still running".

    # arm a watch over everything a sweep queued (this is the normal use)
    python fleet/watch.py --group m50_echo_center --readout-every 45m

    # one-shot "where is my campaign right now", no monitor needed
    python fleet/watch.py --group m50_echo_center --once

Exit codes: 0 all watched tasks permanently settled · 2 validation error · 3 --max-hours elapsed.

Structure mirrors `calibration.py`: a **pure core** (`step`, `classify`, `diff_states`, `render`,
`build_readout`) that takes an already-fetched `Snapshot`, an injected clock, and an injected TB
reader — unit-tested with no DB and no sleeping — under an impure shell (`main`) that opens the
read-only connection, polls, and prints.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Registry public surface: path resolution + the state vocabulary. Never its writer API.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ONE definition of the on-disk name mapping, imported rather than copied: this file and
# `Dispatcher._result_dir` disagreeing about it is precisely the bug `scan_artifacts` documents.
from dispatcher import _fs_safe_component  # noqa: E402 — needs the sys.path line above
import registry_db  # noqa: E402

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
TAG_WIDTH = 11
DETAIL_LIMIT = 220

# Spec Behavior 5. Destination state -> (tag, permanent-unconditionally). `infra_failed` is the one
# conditional case (permanent only once retries are exhausted) and is resolved in `classify`.
_STATE_TAGS = {
    "done": ("DONE", True),
    "task_failed": ("FAIL", True),
    "cancelled": ("CANCELLED", True),
    "infra_failed": ("INFRA", False),
    "queued": ("REQUEUE", False),
    "preempting": ("PREEMPT", False),
    "cancelling": ("CANCELLING", False),
    "running": ("RUNNING", False),
    "claimed": ("PROGRESS", False),
    "shipped": ("PROGRESS", False),
}
# Behavior 7: suppressed above the small-watch threshold (never failures/stalls/completions).
_LOW_SEVERITY_TAGS = frozenset({"PROGRESS", "RUNNING"})
SMALL_WATCH = 4

# Behavior 8: a task is permanently settled here (the watch may exit). NOT registry_db.TERMINAL_STATES
# alone — `infra_failed` with no retries left is just as final, and is precisely the silent death a
# `runq ls` loop misses (the task stops moving in a non-terminal state and nothing ever fires again).
_TERMINAL_STATES = frozenset(registry_db.TERMINAL_STATES)

# Behavior 16: instance-scoped events that explain a watched task's imminent infra failure.
_BOX_EVENTS = frozenset({"lost", "dead_worker", "teardown"})

# Behavior 12: preferred TB tag substrings, most interesting first.
#
# ⚠ ORDER IS LOAD-BEARING, and the old order put the WORST metric first. `durable` led the list, so a
# 9-stage curriculum readout showed `ab/durable` and nothing else useful — the one scalar this repo has
# established CANNOT arbitrate (it sums margins over every rung including the ones sitting at their
# floor, and at n=3 resolves only ~±0.9). The three that actually decide "is this run on track" now
# lead (2026-08-02):
#   max_action_share — the COLLAPSE fingerprint: ~1/n_actions healthy, near 1.0 is a latched policy.
#   mastery/<task>/gain — cold minus the PAIRED floor, i.e. the ANCHORED score. Never read a bare
#                         `cold/<task>`: unanchored, it cannot tell "0.41" (below a 0.50 floor) from
#                         a good result. This is the tag that shows a below-floor arm in flight.
#   grounded — cold minus instruction-ablated; for a `lang/*` run this is the primary.
_TB_PREFERRED = ("max_action_share", "mastery", "grounded", "durable",
                 "score", "fitness", "return", "reward", "loss", "norm")
_TB_MAX_TAGS = 4

_ARTIFACT_WALK_LIMIT = 400  # bounded stat() walk per result dir (Behavior 20)


# --------------------------------------------------------------------------- pure helpers

def parse_duration(s: str) -> float:
    """`90` (seconds) | `45s` | `30m` | `2h` -> seconds. Trust boundary: raises ValueError."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(s), re.I)
    if not m:
        raise ValueError(f"bad duration {s!r} — use N, Ns, Nm or Nh")
    v = float(m.group(1))
    return v * {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}[m.group(2).lower()]


def parse_ts(s) -> float | None:
    """Registry ISO timestamp -> epoch seconds (None if absent/unparseable)."""
    if not s:
        return None
    try:
        return datetime.strptime(s, _TS_FMT).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    # ⚠ TRUNCATE the hours, never round them. `f"{seconds/3600:.0f}h"` ROUNDS, so 5400s (1h30m)
    # printed "2h30m" — every age whose minute part was >= 30 was reported an HOUR TOO HIGH, and
    # `.0f` uses banker's rounding so it was wrong inconsistently (1.5h -> 2, 2.5h -> 2, 3.5h -> 4).
    # This function formats every age the watcher prints — task runtimes, checkpoint ages, the cold
    # -queue threshold in its own banner — so the error was invisible precisely where it mattered:
    # a cell reported "running 3h53m" was actually at 2h53m, and a reader judging it against the
    # 90-minute reaper was reading an hour of slack that did not exist.
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60}m"


def condense(detail: str | None, limit: int = DETAIL_LIMIT) -> str:
    """Collapse whitespace to one line, truncate (Behavior 4)."""
    text = " ".join((detail or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_TB_MARKER = "Traceback (most recent call last):"


def exception_line(tail: str) -> tuple[str, bool]:
    """The actionable line out of a run.log tail -> (line, is_exception).

    A Python traceback is `Traceback…:` then INDENTED `File …` frames then ONE non-indented line —
    the exception. Anchoring on that structure rather than taking the last non-empty line matters:
    real tails often keep logging after the traceback (a live case ended
    `…RuntimeError: Expected all tensors to be on the same device…` followed by
    `[resume] 0/1 seeds done; continuing mid-curriculum`, and "last line" reported the resume
    notice as the cause). Taking the LAST marker also picks the propagating exception out of a
    chained (`During handling of…`) traceback.
    """
    lines = tail.splitlines()
    idx = max((i for i, ln in enumerate(lines) if _TB_MARKER in ln), default=None)
    if idx is not None:
        for ln in lines[idx + 1:]:
            if ln.strip() and not ln[:1].isspace():
                return ln.strip(), True
    non_empty = [ln.strip() for ln in lines if ln.strip()]
    return (non_empty[-1], False) if non_empty else ("(no log tail)", False)


def explain_exit(head: str) -> str:
    """Annotate a `worker exit N` head with what N means (Behavior 4a). A negative code is a SIGNAL,
    not a Python error — `worker exit -9` (36 of the 222 live failures) is the kernel or a reaper
    killing the process, and reading it as a code bug sends the next hour in the wrong direction."""
    m = re.search(r"worker exit (-?\d+)", head)
    if m:
        code = int(m.group(1))
        if code < 0:
            try:
                import signal
                name = signal.Signals(-code).name
            except (ValueError, ImportError):
                name = f"signal {-code}"
            hint = " — OOM-killed or reaped, not a code bug" if -code == 9 else ""
            return f"{head} ({name}{hint})"
        return head
    if head.startswith("artifact_missing"):
        return (f"{head} (exited without writing its declared completion_artifact — a job-contract "
                f"bug, not a crash)")
    return head


def condense_failure(detail: str | None, task_id: str, log_path: str | None = None) -> str:
    """A `task_failed` detail is `worker exit N` optionally followed by
    `\\n--- run.log tail ---\\n<log>`. Keep the exit code plus the exception, and point at whichever
    full evidence actually exists — never truncate a traceback from the top, which yields
    `File "<frozen runpy>"…` and says nothing (Behavior 4).

    203 of the 222 live failures carry NO tail at all (just `worker exit 1`/`-9`/`artifact_missing`),
    so the pointer is the load-bearing part of this line for most failures.
    """
    raw = detail or ""
    head, sep, tail = raw.partition("--- run.log tail ---")
    head = explain_exit(condense(head, 100) or "worker failed")
    where = f"see {log_path}" if log_path else f"runq show {task_id}"
    if not sep:
        return f"{head} · {where}"
    exc, is_exc = exception_line(tail)
    prefix = "" if is_exc else "last log line: "
    return f"{head} · {prefix}{condense(exc, 160)} · {where}"


def classify(to_state: str, task: dict) -> tuple[str, bool]:
    """Destination state -> (tag, is_permanent). Behavior 5 + 6."""
    tag, permanent = _STATE_TAGS.get(to_state, ("PROGRESS", False))
    if to_state == "infra_failed":
        permanent = _num(task.get("retries_used")) >= _num(task.get("max_retries"), 1.0)
    return tag, permanent


def is_settled(task: dict) -> bool:
    """Behavior 8: permanently settled — the dispatcher will do nothing further with this task."""
    state = task.get("state")
    if state in _TERMINAL_STATES:
        return True
    return (state == "infra_failed"
            and _num(task.get("retries_used")) >= _num(task.get("max_retries"), 1.0))


def _num(v, default: float = 0.0) -> float:
    if isinstance(v, bool) or v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def render(now: float, tag: str, msg: str, task: dict | None = None) -> str:
    """The fixed line format (Output contract): `HH:MM:SS TAG  grp/name [id8] message`."""
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    if task is None:
        who, tid = "-", "--------"
    else:
        who = f"{task.get('grp')}/{task.get('name')}"
        tid = str(task.get("id", ""))[:8]
    return f"{clock} {tag:<{TAG_WIDTH}} {who} [{tid}] {msg}"


def colocation_verdicts(tasks: list[dict], boxes: dict) -> list[tuple]:
    """Behavior 22 (pure): `[(group key, verdict, sorted boxes, member count)]` for every colocation
    group among `tasks`. Verdict is `OK` (one box), `SPLIT` (two or more) or `PENDING` (none).

    WHY THE WATCHER AND NOT ONLY `runq colocate`. The project's working rules ranks a signal wired to fire AT the
    failure point above a script somebody has to remember to run — and the failure point here is
    precisely the moment a verdict gets written, which is this watcher's `END`. The repo has the
    scar: `m83_normgate` co-located 2 of its 3 pairs, was published, and the split was invisible in
    every output; then a collapsed control on the odd box turned a null into a +0.2721 "win". A
    check nobody runs is not a check.

    ⚠ `boxes` must come from the `start` EVENTS, not `tasks.instance_id` — see `Snapshot.boxes`."""
    groups: dict[str, list] = {}
    for t in tasks:
        key = registry_db.colocate_key_of(t.get("resource_hint_json"))
        if key is not None:
            groups.setdefault(key, []).append(t)
    out = []
    for key in sorted(groups):
        seen = sorted({b for t in groups[key] for b in boxes.get(t["id"], ())})
        verdict = "PENDING" if not seen else ("OK" if len(seen) == 1 else "SPLIT")
        out.append((key, verdict, seen, len(groups[key])))
    return out


def census(tasks: list[dict]) -> str:
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.get("state", "?")] = counts.get(t.get("state", "?"), 0) + 1
    order = ["running", "shipped", "claimed", "queued", "preempting", "cancelling",
             "done", "task_failed", "infra_failed", "cancelled"]
    parts = [f"{s}={counts[s]}" for s in order if s in counts]
    parts += [f"{s}={n}" for s, n in sorted(counts.items()) if s not in order]
    return " ".join(parts) or "(none)"


# --------------------------------------------------------------------------- snapshot & config

@dataclass
class Config:
    groups: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    poll_s: float = 60.0
    readout_every_s: float = 1800.0
    readout_grace_s: float = 300.0
    quiet_after_s: float = 1200.0
    queued_cold_after_s: float = 1800.0
    max_hours: float = 24.0
    tb_tags: tuple[str, ...] = ()
    verbose: bool = False


@dataclass
class Artifacts:
    """Freshness of a task's pulled result dir (Behavior 9/14)."""
    exists: bool = False
    newest_mtime: float = 0.0
    ckpt_mtime: float = 0.0
    tb_dir: str | None = None
    log_path: str | None = None   # the dispatcher pulls run.log here on failure
    ckpt_path: str | None = None  # ckpt_latest.pt — carries the COMPLETED arms (see arms_digest)


@dataclass
class Snapshot:
    tasks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)      # new rows for watched tasks
    box_events: list[dict] = field(default_factory=list)  # new rows for watched tasks' instances
    holds: dict[str, str] = field(default_factory=dict)   # task_id -> newest hold reason
    artifacts: dict[str, Artifacts] = field(default_factory=dict)
    # Behavior 18: lifetime preempt (resume-cycle) count per watched task, ALL history — not just
    # since the watermark. A campaign's comparability depends on its total, not on this poll's.
    resumes: dict[str, int] = field(default_factory=dict)
    # Behavior 22: every box each watched task actually STARTED on, ALL history. Sibling to
    # `resumes` in both shape and purpose — comparability depends on the lifetime fact, and
    # `tasks.instance_id` holds only the LATEST placement, so a requeued arm reads as though it had
    # only ever run on its second box (which is exactly the arm with `resumes > 0`).
    boxes: dict[str, tuple] = field(default_factory=dict)
    live_instances: int = 0
    max_seq: int = 0


@dataclass
class WatchState:
    cfg: Config
    armed_at: float = 0.0
    armed: bool = False
    watermark: int = 0
    last_states: dict[str, str] = field(default_factory=dict)
    state_since: dict[str, float] = field(default_factory=dict)
    last_artifact: dict[str, float] = field(default_factory=dict)
    stall_flagged: set = field(default_factory=set)
    cold_flagged: set = field(default_factory=set)
    suppressed: dict[str, int] = field(default_factory=dict)   # tag -> count since last readout
    last_readout: float = 0.0
    readout_pending_since: float | None = None
    prev_tb: dict[str, dict[str, float]] = field(default_factory=dict)  # tid -> {tag: value}
    last_error_line: float = 0.0
    finished: bool = False


def no_tb(_task: dict, _artifacts: Artifacts, _tags) -> dict[str, tuple[float, float]]:
    """Injected-TB default: no digest (tests and `--no-tb` use this)."""
    return {}


# --------------------------------------------------------------------------- pure core

def diff_states(prev: dict[str, str], tasks: list[dict]) -> list[tuple[dict, str | None]]:
    """(task, previous_state) for every task whose state changed; previous_state None = newly seen."""
    out = []
    for t in tasks:
        tid = t["id"]
        was = prev.get(tid)
        if was != t.get("state"):
            out.append((t, was))
    return out


def _detail_for(snap: Snapshot, task_id: str) -> str | None:
    """Newest new-event detail for a task — the annotation source (Behavior 4)."""
    best = None
    for e in snap.events:
        if e.get("task_id") == task_id:
            best = e
    return None if best is None else best.get("detail")


def _transition_msg(task: dict, was: str | None, tag: str, permanent: bool,
                    snap: Snapshot) -> str:
    tid = task["id"]
    detail = _detail_for(snap, tid)
    arrow = f"{was or '·'}→{task.get('state')}"
    if tag == "FAIL":
        return f"{arrow} · {condense_failure(detail, tid, snap.artifacts.get(tid, Artifacts()).log_path)}"
    if tag == "INFRA":
        used, mx = _num(task.get("retries_used")), _num(task.get("max_retries"), 1.0)
        budget = (f"retries {used:g}/{mx:g} — NO RETRIES LEFT, permanently failed"
                  if permanent else f"retries {used:g}/{mx:g}, will requeue")
        return f"{arrow} · {condense(detail) or 'infra failure'} · {budget}"
    if tag == "DONE":
        return f"{arrow} · {condense(detail) or 'complete'} · results in experiments/{_fs_safe_component(task['grp'])}/{_fs_safe_component(task['name'])}/"
    body = condense(detail)
    return f"{arrow}{' · ' + body if body else ''}"


def _artifact_advanced(state: WatchState, snap: Snapshot) -> tuple[bool, float]:
    """Did any watched task's pulled artifacts move since the last poll? (Behavior 9)"""
    advanced, newest = False, 0.0
    for tid, art in snap.artifacts.items():
        if art.newest_mtime > state.last_artifact.get(tid, 0.0):
            advanced = True
        newest = max(newest, art.ckpt_mtime or art.newest_mtime)
    return advanced, newest


_ARM_SCORE_KEYS = ("frac_of_oracle", "cold_mean", "margin_mean")


def arms_digest(ckpt_path: str, max_arms: int = 4) -> list[str]:
    """The arms a RUNNING multi-arm cell has already FINISHED, read from its pulled checkpoint.

    A READOUT asks "on track, dead, or already answered?" and until now answered it with freshness
    and percent-of-est only — so the honest reply was "go and look", and the tempting wrong reply was
    "no interim signal". There IS one: a multi-arm trainer appends each finished arm to `results` in
    the checkpoint, and the coordinator pulls that every few minutes. A 3-arm campaign is a third
    readable the moment arm 0 lands, per seed, at no cost.

    ⚠ TensorBoard emptiness proves nothing about these trainers — the event file is a ~200-byte
    header even on cells that finished successfully. That is what made this look unreadable.

    Returns [] for anything that is not a multi-arm checkpoint, so single-arm campaigns stay quiet.
    Never raises: a readout must degrade, never abort the watch (Behavior 12).
    """
    try:
        import torch                                    # local: the watch must not import torch
    except Exception:                                   # unless it actually has a checkpoint to read
        return []
    # ⚠ mmap=True IS LOAD-BEARING, not an optimisation — it is the difference between a watcher that
    # sits at ~250MB for days and one that does not. `results` is a small list of plain dicts, but a
    # plain `torch.load` MATERIALISES EVERY TENSOR IN THE CHECKPOINT to reach it, and a readout runs
    # this per running task on every `--readout-every` tick, for the life of the campaign. Measured
    # 2026-08-07 on `m49_dream_acq_n3c/fwd_lo_s1` (a 1866MB ckpt — `dream_capacity` puts a reservoir
    # replay buffer in there, so a dream/replay campaign's ckpt is ~25-80x an ordinary one's 23-78MB):
    #     plain  -> +1884MB resident per call (peak 2376MB)
    #     mmap   -> +15.7MB                                  ~120x less
    # The pages are freed on `del d`, so this is a RECURRING SPIKE rather than an unbounded leak — but
    # on a box whose RAM is already committed, each spike is what forces the swap-out, and the watcher
    # never gets the resident pages back. That box's watcher was measured at 1.4GB against 240-330MB
    # for its 8 peers, tracking checkpoint size and nothing else.
    # It is also usually pure waste: 0 of 40 live checkpoints sampled that day carried `results` at
    # all (the trainer appends an arm only as it FINISHES one), so the common case loads the whole
    # file to `return []` two lines below.
    # Fall back to a plain load rather than lose the feature: mmap needs the zipfile serialisation
    # (torch >= 1.6 default, which every checkpoint here uses) and raises on the legacy format.
    try:
        d = torch.load(ckpt_path, map_location="cpu", weights_only=False, mmap=True)
    except Exception:
        try:
            d = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        except Exception as e:
            return [f"arms: unreadable ({type(e).__name__})"]
    if not isinstance(d, dict):
        return []
    done = d.get("results")
    if not isinstance(done, list) or not done:
        return []
    out = []
    for r in done[-max_arms:]:
        if not isinstance(r, dict):
            continue
        params = r.get("params") if isinstance(r.get("params"), dict) else {}
        tag = ",".join(f"{k}={v}" for k, v in list(params.items())[:3]) or "arm"
        cells = []
        for stage, v in (r.get("per_stage") or {}).items():
            if not isinstance(v, dict):
                continue
            score = next((v[k] for k in _ARM_SCORE_KEYS if v.get(k) is not None), None)
            if score is None:
                continue
            short = stage.split("?")[0].split("/")[-1]
            for cue in ("attr", "conj"):                 # keep the cue, it IS the contrast
                if f"cue={cue}" in stage:
                    short = f"{short}/{cue}"
            cells.append(f"{short}={score:.4f}")
        out.append(f"    [{tag[:44]}] " + ("  ".join(cells[:6]) if cells else "(no scored stage)"))
    idx = d.get("arm_index")
    head = (f"  arms COMPLETE: {len(done)}"
            + (f" · now running arm {idx}" if isinstance(idx, int) else "")
            + "  ⚠ partial: compare arms WITHIN this cell (same box, blob, interruptions), not across cells")
    return [head] + out


#: arm names that CLAIM to be a baseline. Matching is on the name because that is what the reader sees.
_CTRL_ARM = re.compile(r"(^|_)(base|ctrl|control|baseline)(_|$)", re.I)
#: a control may carry these and still be a control
_CTRL_OK = {"arm", "seed", "seeds"}


def _pinned_sets(task: dict) -> list[str]:
    """The `--set k=v` overrides a task was QUEUED with, minus the ones a control may carry.

    Reads `args_json` — what was actually dispatched — rather than the config file, which may have
    been edited since the cell was queued."""
    raw = task.get("args_json") or "[]"
    try:
        args = json.loads(raw)
    except (ValueError, TypeError):
        # ⛔ NARROW, DELIBERATELY. A bare `except Exception` here swallowed a NameError (`json` was
        # not imported) and returned "nothing pinned" — a confound warning that silently reports
        # CLEAN is worse than no warning at all, because the reader trusts it.
        return []
    if not isinstance(args, list):
        return []
    out = []
    for i, a in enumerate(args):
        if a != "--set" or i + 1 >= len(args):
            continue
        kv = str(args[i + 1])
        k = kv.split("=", 1)[0].strip()
        if k and k not in _CTRL_OK:
            out.append(kv)
    return out


def build_readout(state: WatchState, snap: Snapshot, now: float, reason: str,
                  tb_reader=no_tb) -> list[str]:
    """The readout block: first line + two-space-indented continuation lines (Behavior 10–12)."""
    cfg = state.cfg
    lines = [render(now, "READOUT", f"{reason} · elapsed {fmt_age(now - state.armed_at)}")]
    lines.append(f"  census: {census(snap.tasks)}")
    if state.suppressed:
        rolled = " ".join(f"{k.lower()}×{v}" for k, v in sorted(state.suppressed.items()))
        lines.append(f"  since last readout (suppressed per-task lines): {rolled}")

    # ⚠ CONTROL ARMS CARRYING OVERRIDES — the other confound a readout must not hide.
    #
    # A control is the thing every treatment is measured AGAINST. When it pins knobs of its own it is
    # not a baseline, it is an unlabelled variant, and every delta reported against it is a delta
    # against a recipe nobody deliberately chose. This is NOT blocked (owner, 2026-08-25: sometimes a
    # control legitimately has to match its treatment on some axis) — it is SURFACED, so the reader
    # judges the intent instead of inheriting it silently.
    #
    # Read from the task's recorded `--set` args, i.e. what was actually queued, not what a config
    # file says today. Quiet unless a control-looking arm carries something beyond arm/seed.
    ctrl_flags: list[str] = []
    for t in sorted(snap.tasks, key=lambda x: (x["grp"], x["name"])):
        if not _CTRL_ARM.search(str(t.get("name", ""))):
            continue
        pinned = _pinned_sets(t)
        if pinned:
            ctrl_flags.append(f"{t['grp']}/{t['name']} pins {', '.join(pinned)}")
    if ctrl_flags:
        lines.append("  ⚠ CONTROL ARM(S) CARRYING OVERRIDES — judge whether each is intended; a "
                     "control that pins a knob is a variant, and deltas measured against it are "
                     "deltas against that variant:")
        for f in ctrl_flags[:8]:
            lines.append(f"      {f}")
        if len(ctrl_flags) > 8:
            lines.append(f"      … and {len(ctrl_flags) - 8} more")

    # Behavior 18: RESUME-CYCLE SPREAD — the confound a readout must not hide.
    #
    # Every preempt logs "checkpoint carried forward", which is true about WORK and silent about
    # COMPARABILITY: resume does not reproduce an uninterrupted run, so an arm interrupted more times
    # than its siblings is measuring something slightly different. Which arm gets hit is decided by
    # which box consolidation happens to drain, i.e. by luck. Measured 2026-07-29: `azsc-p1e`
    # finished with 12 resumes on one arm against 6-9 on its siblings, and nothing in any readout
    # said so — the owner would have compared them as equals.
    #
    # Reported per GROUP (arms of one campaign are what get compared), only when the spread is >= 2,
    # so a healthy campaign stays quiet and this never becomes noise to scroll past.
    by_grp: dict[str, list[tuple[int, str]]] = {}
    for t in snap.tasks:
        by_grp.setdefault(t["grp"], []).append((snap.resumes.get(t["id"], 0), t["name"]))
    for grp, arms in sorted(by_grp.items()):
        if len(arms) < 2:
            continue
        counts = [n for n, _ in arms]
        spread = max(counts) - min(counts)
        if spread < 2:
            continue
        worst = sorted(arms, reverse=True)[:3]
        detail = ", ".join(f"{name[:34]}={n}" for n, name in worst)
        lines.append(f"  ⚠ resume-cycle spread {spread} in {grp} "
                     f"(min {min(counts)}, max {max(counts)}): {detail} "
                     f"— arms were interrupted unequally; resume does not reproduce, so compare "
                     f"these with that in mind")

    open_tasks = [t for t in snap.tasks if not is_settled(t)]
    for t in sorted(open_tasks, key=lambda x: (x.get("state", ""), x.get("name", ""))):
        tid = t["id"]
        art = snap.artifacts.get(tid, Artifacts())
        since = state.state_since.get(tid) or parse_ts(t.get("updated_at")) or now
        bits = [f"{t['grp']}/{t['name']}", f"{t.get('state')} {fmt_age(now - since)}"]
        if art.ckpt_mtime:
            bits.append(f"ckpt {fmt_age(now - art.ckpt_mtime)} ago")
        elif t.get("state") == "running":
            bits.append("no ckpt yet")
        est = _num(t.get("est_minutes"))
        if est and t.get("state") == "running":
            bits.append(f"{(now - since) / 60 / est * 100:.0f}% of est {est:g}m")
        used = _num(t.get("retries_used"))
        if used:
            bits.append(f"retries {used:g}/{_num(t.get('max_retries'), 1.0):g}")
        if t.get("state") == "queued":
            bits.append(f"held: {snap.holds.get(tid, 'no hold reason logged')}")
        lines.append("  " + " · ".join(bits))
        if art.ckpt_path and t.get("state") == "running":
            lines.extend(arms_digest(art.ckpt_path))

    for t in open_tasks:
        tid = t["id"]
        art = snap.artifacts.get(tid, Artifacts())
        if not art.tb_dir:
            continue
        try:
            digest = tb_reader(t, art, cfg.tb_tags)
        except Exception as e:  # Behavior 12: a TB failure degrades, never aborts the watch
            lines.append(f"  tb {t['name']}: unavailable ({type(e).__name__}: {e})")
            continue
        if not digest:
            continue
        prev = state.prev_tb.get(tid, {})
        cells = []
        for tag, (val, tb_step) in digest.items():
            delta = val - prev[tag] if tag in prev else None
            d = f" ({delta:+.4g})" if delta is not None else ""
            cells.append(f"{tag}={val:.4g}{d}@{tb_step:g}")
        state.prev_tb[tid] = {k: v[0] for k, v in digest.items()}
        lines.append(f"  tb {t['name']}: " + "  ".join(cells))

    if open_tasks:
        lines.append("  → interim eval readout is DUE: pull/inspect the artifacts above and say "
                     "whether the campaign is on track, dead, or answered already.")
    else:
        lines.append("  → all watched tasks settled — write the group summary.md and act on the result.")
    state.suppressed.clear()
    return lines


def step(state: WatchState, snap: Snapshot, now: float, tb_reader=no_tb) -> list[str]:
    """One poll's worth of decisions. Pure w.r.t. I/O: `snap` is already fetched, `now` is injected,
    TB reading is injected. Mutates and returns lines to emit (Behavior 1–17)."""
    cfg = state.cfg
    lines: list[str] = []
    by_id = {t["id"]: t for t in snap.tasks}
    small = len(snap.tasks) <= SMALL_WATCH or cfg.verbose

    # --- arm (Behavior 3) ---------------------------------------------------
    if not state.armed:
        state.armed = True
        state.armed_at = now
        state.last_readout = now
        groups = ",".join(sorted({t["grp"] for t in snap.tasks})) or ",".join(cfg.groups)
        lines.append(render(now, "WATCH", (
            f"watching {len(snap.tasks)} task(s) in {groups} · {census(snap.tasks)} · "
            f"poll {fmt_age(cfg.poll_s)} · readout every "
            f"{fmt_age(cfg.readout_every_s) if cfg.readout_every_s else 'off'}"
            f" (grace {fmt_age(cfg.readout_grace_s)}) · stall>{fmt_age(cfg.quiet_after_s)}"
            f" · cold-queue>{fmt_age(cfg.queued_cold_after_s)}")))
        for t in snap.tasks:
            state.last_states[t["id"]] = t.get("state")
            state.state_since[t["id"]] = parse_ts(t.get("updated_at")) or now

    # --- box events, before the failure they cause (Behavior 16) ------------
    # One line per (instance, event, detail) naming its victims — a box hosting six watched cells
    # is one incident, not six notifications.
    seen_box = set()
    for e in snap.box_events:
        key = (e.get("instance_id"), e.get("event"), e.get("detail"))
        if key in seen_box:
            continue
        seen_box.add(key)
        victims = [f"{t['grp']}/{t['name']}" for t in snap.tasks
                   if t.get("instance_id") == e.get("instance_id") and not is_settled(t)]
        if not victims:
            continue
        who = ", ".join(victims[:4]) + (f" (+{len(victims) - 4} more)" if len(victims) > 4 else "")
        lines.append(render(now, "BOX", f"instance {e.get('instance_id')}: {e.get('event')} · "
                                        f"{condense(e.get('detail'))} · hosting {who}"))

    # --- state transitions (Behavior 2, 4–7) --------------------------------
    for t, was in diff_states(state.last_states, snap.tasks):
        tid = t["id"]
        state.last_states[tid] = t.get("state")
        state.state_since[tid] = parse_ts(t.get("updated_at")) or now
        state.stall_flagged.discard(tid)
        state.cold_flagged.discard(tid)
        if was is None:  # Behavior 1: joined a watched group after arming
            lines.append(render(now, "JOINED", f"new task in watched group · state {t.get('state')}", t))
            continue
        tag, permanent = classify(t.get("state"), t)
        if tag in _LOW_SEVERITY_TAGS and not small:
            state.suppressed[tag] = state.suppressed.get(tag, 0) + 1
            continue
        lines.append(render(now, tag, _transition_msg(t, was, tag, permanent, snap), t))

    # --- watcher-side stall + cold queue (Behavior 14, 14b, 15) -------------
    # Behavior 14b: count the watched `running` tasks that are quiet RIGHT NOW, BEFORE emitting any
    # STALL line. A STALL reads the INGESTED artifact, so a starved coordinator pull is indistinguishable
    # from a wedged trainer on one task — but not across several: independent trainers do not wedge in the
    # same minute, a shared pull does. Never used to suppress (a dead box looks the same); only to annotate.
    quiet_now = set()
    if cfg.quiet_after_s:
        for other in snap.tasks:
            if is_settled(other) or other.get("state") != "running":
                continue
            oid = other["id"]
            o_since = state.state_since.get(oid) or parse_ts(other.get("updated_at")) or now
            o_art = snap.artifacts.get(oid, Artifacts())
            if now - max(o_art.newest_mtime, o_since) >= cfg.quiet_after_s:
                quiet_now.add(oid)

    for t in snap.tasks:
        tid = t["id"]
        if is_settled(t):
            continue
        since = state.state_since.get(tid) or parse_ts(t.get("updated_at")) or now
        if t.get("state") == "running" and cfg.quiet_after_s:
            art = snap.artifacts.get(tid, Artifacts())
            last_move = max(art.newest_mtime, since)
            if now - last_move >= cfg.quiet_after_s:
                if tid not in state.stall_flagged:
                    state.stall_flagged.add(tid)
                    shared = (
                        f" · ⚠ {len(quiet_now)} watched tasks quiet at once — more likely the "
                        f"coordinator's artifact PULL is starved than {len(quiet_now)} trainers wedging "
                        f"together; check the BOX (ssh to its spool) before believing this, and note a "
                        f"FINISHED run looks identical until its result is ingested"
                        if len(quiet_now) >= 2 else "")
                    lines.append(render(now, "STALL", (
                        f"running but no artifact progress in {fmt_age(now - last_move)} "
                        f"(dispatcher's own stall reaper fires at 90m and will burn a retry) · "
                        f"go look: experiments/{_fs_safe_component(t['grp'])}/{_fs_safe_component(t['name'])}/{shared}"), t))
            else:
                state.stall_flagged.discard(tid)
        if t.get("state") == "queued" and cfg.queued_cold_after_s:
            if now - since >= cfg.queued_cold_after_s and tid not in state.cold_flagged:
                state.cold_flagged.add(tid)
                # ⛔ NO HOLD REASON + LIVE CAPACITY IS THE SIGNATURE OF A DEAD COORDINATOR, and saying
                # so here is the whole point of this alert. A hold reason means the dispatcher LOOKED at
                # the task and declined it; its ABSENCE means nothing ever evaluated it. Measured
                # 2026-09-15: the coordinator had been down ~18h with three owned boxes live and idle
                # (36 free slots) and two sessions' campaigns stuck, and this line said only "held: no
                # hold reason logged" — true, unhelpful, and the diagnosis started from scratch.
                # ⚠ The self-heal does NOT cover this case: it respawns a daemon that CRASHED, but the
                # supervisor lives in the same process tree, so a host/devcontainer restart takes both
                # and `autostart` only fires on devcontainer folder-open.
                _hold = snap.holds.get(tid)
                _hint = ("" if _hold else
                         " · ⛔ NO hold reason + live capacity ⇒ suspect a DEAD COORDINATOR (nothing"
                         " evaluated this task at all): `bash fleet/dispatcher_ctl.sh status`,"
                         " and if it is down `… restart` — self-heal does NOT survive a host restart")
                lines.append(render(now, "QUEUED-COLD", (
                    f"queued {fmt_age(now - since)} with no box · held: "
                    f"{_hold or 'no hold reason logged'} · "
                    f"{snap.live_instances} live instance(s){_hint}"), t))

    # --- readout cadence (Behavior 9, 10) -----------------------------------
    advanced, _newest = _artifact_advanced(state, snap)
    for tid, art in snap.artifacts.items():
        state.last_artifact[tid] = max(state.last_artifact.get(tid, 0.0), art.newest_mtime)

    all_settled = bool(snap.tasks) and all(is_settled(t) for t in snap.tasks)
    if cfg.readout_every_s and not all_settled:
        if state.readout_pending_since is None and now - state.last_readout >= cfg.readout_every_s:
            state.readout_pending_since = now
        if state.readout_pending_since is not None:
            waited = now - state.readout_pending_since
            if advanced:
                ck = max((a.ckpt_mtime for a in snap.artifacts.values()), default=0.0)
                fresh = f"fresh checkpoint {fmt_age(now - ck)} ago" if ck else "fresh artifacts"
                reason = f"cadence {fmt_age(cfg.readout_every_s)} + {fresh}"
            elif waited >= cfg.readout_grace_s:
                reason = f"cadence {fmt_age(cfg.readout_every_s)} (grace, no new checkpoint)"
            else:
                reason = None
            if reason:
                lines += build_readout(state, snap, now, reason, tb_reader)
                state.last_readout = now
                state.readout_pending_since = None

    # --- termination (Behavior 8) -------------------------------------------
    if all_settled and not state.finished:
        state.finished = True
        lines += build_readout(state, snap, now, "final", tb_reader)
        for t in sorted(snap.tasks, key=lambda x: x.get("name", "")):
            lines.append(f"  {t.get('state'):<12} {t['grp']}/{t['name']}")
        # Behavior 22: the co-location verdict fires HERE, unprompted, because here is where somebody
        # writes the campaign's result. A SPLIT group is loud and names itself; an all-OK campaign
        # gets one confirming line, because "the check ran and passed" is what licenses the paired
        # claim and a silent absence cannot be told from a check that never ran.
        colo = colocation_verdicts(snap.tasks, snap.boxes)
        for key, verdict, seen, n in colo:
            if verdict == "SPLIT":
                lines.append(render(now, "COLOCATE", (
                    f"⛔ {key}: its {n} arm(s) ran on DIFFERENT boxes {seen} — a paired delta over "
                    f"them is NOT paired (the box selects the attractor; a control that collapsed "
                    f"on the other machine manufactures a win). Report the split beside the number "
                    f"or re-run the arms together; `runq colocate --key {key}`")))
        if colo and all(v == "OK" for _k, v, _b, _n in colo):
            lines.append(render(now, "COLOCATE", (
                f"✓ all {len(colo)} colocation group(s) ran each on ONE box "
                f"({', '.join(f'{k}@{b[0]}' for k, _v, b, _n in colo)})")))
        lines.append(render(now, "END", (
            f"all {len(snap.tasks)} watched task(s) settled after {fmt_age(now - state.armed_at)} · "
            f"{census(snap.tasks)}")))
    return lines


# --------------------------------------------------------------------------- impure shell

def connect_ro(path: str) -> sqlite3.Connection:
    """Behavior 19: READ-ONLY, never create. Mirrors `calibration._connect_ro` — deliberately NOT
    `registry_db.connect`, which would initialize a fresh schema at a typo'd path."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"watch: no registry db at {path}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def scan_artifacts(root: Path, grp: str, name: str) -> Artifacts:
    """Bounded stat() walk of a pulled result dir (Behavior 20).

    ⚠ The path MUST go through `_fs_safe_component`, exactly as `Dispatcher._result_dir` does.
    A task name over the 255-byte component limit is truncated-with-hash on the way to disk
    (invariant 9e), so reading `root / grp / name` raw finds NOTHING for those tasks — and "no
    artifacts" is indistinguishable here from "no progress", so every long-named task reports a
    permanent FALSE STALL.

    Observed 2026-07-30 on `m56_causal_body_v2`: the three `perturb` cells stalled-alarmed at 45m
    while their checkpoints were 18-22 min old and their step counters had advanced 2222 -> 6130
    across three stages. Only the perturb cells fired, because only they carried the extra
    `body_perturb_sigma0.05_` in the name and crossed the limit — i.e. **the false alarm lands
    precisely on the ARM UNDER TEST**, the one a new gene made longer. The dispatcher's own reaper
    is unaffected (it uses the stored `resume_checkpoint` path), so this never burned a retry; it
    was purely an observability lie, which is worse than useless because it invites cancelling a
    healthy campaign."""
    d = root / _fs_safe_component(grp) / _fs_safe_component(name)
    art = Artifacts()
    if not d.is_dir():
        return art
    art.exists = True
    seen = 0
    stack = [d]
    while stack and seen < _ARTIFACT_WALK_LIMIT:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen >= _ARTIFACT_WALK_LIMIT:
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                    if e.name == "tb":
                        art.tb_dir = e.path
                    continue
                mt = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            art.newest_mtime = max(art.newest_mtime, mt)
            if e.name in ("ckpt_latest.pt", "results.json"):
                art.ckpt_mtime = max(art.ckpt_mtime, mt)
                if e.name == "ckpt_latest.pt":
                    art.ckpt_path = e.path
            elif e.name == "run.log":
                art.log_path = e.path
    return art


def fetch_snapshot(conn: sqlite3.Connection, cfg: Config, watermark: int,
                   exp_root: Path) -> Snapshot:
    snap = Snapshot()
    where, params = [], []
    if cfg.groups:
        where.append(f"grp IN ({','.join('?' * len(cfg.groups))})")
        params += list(cfg.groups)
    if cfg.task_ids:
        where.append(f"id IN ({','.join('?' * len(cfg.task_ids))})")
        params += list(cfg.task_ids)
    rows = conn.execute(f"SELECT * FROM tasks WHERE {' OR '.join(where)}", params).fetchall()
    snap.tasks = [dict(r) for r in rows]
    ids = [t["id"] for t in snap.tasks]
    (snap.max_seq,) = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()

    if ids:
        qs = ",".join("?" * len(ids))
        # Behavior 18: lifetime resume cycles per task.
        snap.resumes = {t: 0 for t in ids}
        for r in conn.execute(
                f"SELECT task_id, COUNT(*) n FROM events WHERE event='preempt_requeue' "
                f"AND task_id IN ({qs}) GROUP BY task_id", ids):
            snap.resumes[r["task_id"]] = r["n"]
        # Behavior 22: lifetime `start` instances per task, same one-query shape as `resumes`.
        started: dict[str, list] = {}
        for r in conn.execute(
                f"SELECT DISTINCT task_id, instance_id FROM events WHERE event='start' "
                f"AND instance_id IS NOT NULL AND task_id IN ({qs}) ORDER BY seq", ids):
            started.setdefault(r["task_id"], []).append(str(r["instance_id"]))
        snap.boxes = {t["id"]: tuple(started.get(t["id"])
                                     or ([str(t["instance_id"])] if t.get("instance_id") is not None
                                         else []))
                      for t in snap.tasks}
        snap.events = [dict(r) for r in conn.execute(
            f"SELECT * FROM events WHERE seq > ? AND task_id IN ({qs}) ORDER BY seq",
            (watermark, *ids))]
        # Behavior 16: box events on instances currently hosting a watched, unsettled task.
        insts = sorted({t["instance_id"] for t in snap.tasks
                        if t.get("instance_id") is not None and not is_settled(t)})
        if insts:
            iq = ",".join("?" * len(insts))
            eq = ",".join("?" * len(_BOX_EVENTS))
            snap.box_events = [dict(r) for r in conn.execute(
                f"SELECT * FROM events WHERE seq > ? AND instance_id IN ({iq}) "
                f"AND event IN ({eq}) ORDER BY seq",
                (watermark, *insts, *sorted(_BOX_EVENTS)))]
        queued = [t["id"] for t in snap.tasks if t.get("state") == "queued"]
        if queued:
            qq = ",".join("?" * len(queued))
            for r in conn.execute(
                    f"SELECT task_id, detail FROM events WHERE event='hold' "
                    f"AND task_id IN ({qq}) AND seq IN "
                    f"(SELECT MAX(seq) FROM events WHERE event='hold' AND task_id IN ({qq}) "
                    f"GROUP BY task_id)", (*queued, *queued)):
                snap.holds[r["task_id"]] = condense(r["detail"], 90)
    (snap.live_instances,) = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE state IN ('provisioning','live')").fetchone()
    for t in snap.tasks:
        # Unsettled tasks need freshness (readout/stall); failed ones need their pulled run.log so
        # the FAIL line can point at real evidence. `done`/`cancelled` need neither — skip the walk.
        if not is_settled(t) or t.get("state") in ("task_failed", "infra_failed"):
            snap.artifacts[t["id"]] = scan_artifacts(exp_root, t["grp"], t["name"])
    return snap


def tb_digest(task: dict, art: Artifacts, want_tags) -> dict[str, tuple[float, float]]:
    """Newest scalar per selected tag (Behavior 12). Imported lazily — only readouts pay for it."""
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    acc = EventAccumulator(art.tb_dir, size_guidance={"scalars": 2000})
    acc.Reload()
    tags = list(acc.Tags().get("scalars", []))
    if not tags:
        return {}
    if want_tags:
        chosen = [t for t in tags if t in want_tags or any(w in t for w in want_tags)]
    else:
        # ONE TAG PER FAMILY, and within a family the MOST RECENTLY WRITTEN one. A multi-stage
        # curriculum emits one tag per family PER STAGE (`mastery/agency/reach/gain`,
        # `mastery/nav/mixed/gain`, …), so a plain prefix match floods the 4 slots with whichever
        # stages happen to sort first — and those are the EARLIEST rungs, i.e. the stalest numbers,
        # while every other family gets crowded out entirely. Picking the highest-step member makes
        # each family report the stage the run is actually on.
        chosen, last = [], {}
        for t in tags:
            pts = acc.Scalars(t)
            last[t] = (float(pts[-1].value), float(pts[-1].step)) if pts else None
        for pref in _TB_PREFERRED:
            fam = [t for t in tags if pref in t.lower() and t not in chosen and last[t]]
            if fam:
                chosen.append(max(fam, key=lambda t: last[t][1]))
        chosen = (chosen or tags)[:_TB_MAX_TAGS]
        return {t: last[t] for t in chosen[:_TB_MAX_TAGS] if last.get(t)}
    out = {}
    for tag in chosen[:_TB_MAX_TAGS]:
        pts = acc.Scalars(tag)
        if pts:
            out[tag] = (float(pts[-1].value), float(pts[-1].step))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watch.py", description="Standard campaign monitor for coordinator tasks "
                                     "(docs/specs/run-watch.spec.md).")
    p.add_argument("--group", action="append", default=[],
                   help="watch every task in this group (repeatable) — the normal handle, since "
                        "`runq sweep` queues a whole group")
    p.add_argument("--task", action="append", default=[], help="watch this task id (repeatable)")
    p.add_argument("--db", default=None)
    p.add_argument("--poll", default="60s")
    p.add_argument("--readout-every", default="30m",
                   help="how often you want an interim eval readout (0 = never)")
    p.add_argument("--readout-grace", default="5m",
                   help="once a readout is due, wait up to this long for a fresh checkpoint to "
                        "land so the readout has something new to read")
    p.add_argument("--quiet-after", default="20m", help="STALL a running task after this much "
                                                        "artifact silence (0 = off)")
    p.add_argument("--queued-cold-after", default="30m")
    p.add_argument("--max-hours", default="24", help="hard stop (0 = unbounded)")
    p.add_argument("--tag", action="append", default=[], dest="tb_tags",
                   help="TB scalar tag for the readout digest (repeatable; default = auto)")
    p.add_argument("--verbose", action="store_true",
                   help="per-task lines for claimed/shipped/running even on a large watch")
    p.add_argument("--no-tb", action="store_true", help="skip the TensorBoard digest entirely")
    p.add_argument("--once", action="store_true",
                   help="arm + one readout, then exit — a cheap manual status check")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if not a.group and not a.task:
        print("watch: at least one --group or --task is required", file=sys.stderr)
        return 2
    try:
        durations = {k: parse_duration(v) for k, v in (
            ("poll", a.poll), ("readout_every", a.readout_every),
            ("readout_grace", a.readout_grace), ("quiet_after", a.quiet_after),
            ("queued_cold_after", a.queued_cold_after))}
        max_hours = float(a.max_hours)  # plain hours, unlike the suffixed durations above
    except ValueError as e:
        print(f"watch: {e}", file=sys.stderr)
        return 2
    if durations["poll"] < 1:
        print("watch: --poll must be at least 1s", file=sys.stderr)
        return 2
    if max_hours < 0 or any(v < 0 for v in durations.values()):
        print("watch: durations must be >= 0", file=sys.stderr)
        return 2

    exp_root = registry_db.shared_experiments_root()
    db_path = a.db or str(exp_root / "runs.sqlite")
    cfg = Config(groups=tuple(a.group), task_ids=tuple(a.task), poll_s=durations["poll"],
                 readout_every_s=durations["readout_every"],
                 readout_grace_s=durations["readout_grace"],
                 quiet_after_s=durations["quiet_after"],
                 queued_cold_after_s=durations["queued_cold_after"],
                 max_hours=max_hours, tb_tags=tuple(a.tb_tags), verbose=a.verbose)
    reader = no_tb if a.no_tb else tb_digest

    conn = connect_ro(db_path)
    state = WatchState(cfg=cfg)
    try:
        # Behavior 2: arm at the CURRENT head of the event log, so the first poll reports live
        # state without replaying the campaign's (or the box's) whole history as fresh events.
        (head,) = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        snap = fetch_snapshot(conn, cfg, head, exp_root)
    except sqlite3.Error as e:
        print(f"watch: cannot read registry: {e}", file=sys.stderr)
        return 2
    if not snap.tasks:
        print(f"watch: nothing to watch — no tasks match groups={list(cfg.groups)} "
              f"tasks={list(cfg.task_ids)} in {db_path}", file=sys.stderr)
        return 2
    missing = set(cfg.task_ids) - {t["id"] for t in snap.tasks}
    if missing:
        print(f"watch: no such task(s): {sorted(missing)}", file=sys.stderr)
        return 2
    state.watermark = snap.max_seq  # Behavior 2: don't replay history

    def emit(lines):
        for ln in lines:
            print(ln, flush=True)

    now = time.time()
    if a.once:
        emit(step(state, snap, now, reader))
        if not state.finished:
            emit(build_readout(state, snap, now, "once", reader))
        return 0

    emit(step(state, snap, now, reader))
    deadline = now + cfg.max_hours * 3600 if cfg.max_hours else None
    while not state.finished:
        time.sleep(cfg.poll_s)
        now = time.time()
        if deadline and now >= deadline:
            emit(build_readout(state, snap, now, "max-hours", reader))
            emit([render(now, "END", f"--max-hours {cfg.max_hours:g} elapsed with work still open "
                                     f"— re-arm the watch if the campaign still matters")])
            return 3
        try:
            snap = fetch_snapshot(conn, cfg, state.watermark, exp_root)
        except sqlite3.Error as e:  # Behavior 13: transient, never fatal
            if now - state.last_error_line >= 60:
                state.last_error_line = now
                emit([render(now, "WATCH-ERROR", f"registry read failed, retrying: {e}")])
            continue
        state.watermark = max(state.watermark, snap.max_seq)
        emit(step(state, snap, now, reader))
    return 0


if __name__ == "__main__":
    sys.exit(main())
