#!/usr/bin/env python3
"""Bound `experiments/` against a byte budget — TTL first, per-category FIFO as the backstop.

Spec: `docs/specs/experiments-retention.spec.md`.

`experiments/` had never been pruned (237 GB / 914 run dirs / +15.1 GB per day, measured
2026-08-09). The only retention anywhere in the fleet is the dispatcher's 72 h code-snapshot GC and
the box-side blob LRU — neither touches a checkpoint. This is the missing half, and it is
DESKTOP-SIDE ON DEMAND: nothing here is wired into the dispatcher loop and nothing writes to the
registry (inv. 12), so it is safe to run while the fleet is live.

Two mechanisms in series, because neither alone is enough:
  * TTL bounds AGE and matches how an artifact's value actually decays — it does the deleting in
    the normal case. It cannot bound bytes: at the burst rate observed here a 7-day TTL holds
    ~245 GB, over budget.
  * A per-category budget with oldest-first eviction bounds BYTES, which is what the operator
    asked for. It is a backstop only: on its own it evicts hours-old artifacts of a campaign being
    actively read, exactly when they are most wanted.

The escape hatch is a `KEEP` file, which protects its whole subtree. It is deliberately protection
FROM EVICTION and not a requirement to keep: the decision to probe a run is made after its result
is read (`runq add --probe --init-from experiments/<run>/<arm>/ckpt_substrate_seed0.pt`), so a
declare-at-queue-time policy gets applied defensively to everything and saves nothing.

Dry run by default. `--apply` deletes and appends a JSONL audit to `<root>/.retention/prune.log`.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_db  # noqa: E402

GB = 1024 ** 3

# The record — TB scalars, results.json, curriculum.log, filmstrips — is 4.4 GB for ALL history and
# grows ~0.1 GB/day. It is 2% of the bytes and the only irreplaceable part, so it is never governed.
RECORD = "record"
GOVERNED = ("prev", "dead_weights", "live_weights")

DEFAULT_TTL_DAYS = {"prev": 2.0, "dead_weights": 7.0, "live_weights": 90.0}
DEFAULT_CAT_BUDGET_GB = {"prev": 20.0, "dead_weights": 30.0, "live_weights": 130.0}
DEFAULT_TOTAL_BUDGET_GB = 200.0
DEFAULT_MIN_AGE_HOURS = 6.0

# A task in any of these can still be written to, shipped, or resumed — its directory is off limits
# no matter how old the files look or how far over budget we are (inv. 5). Mirrors the refcount in
# `dispatcher._gc_code_snapshots`, which retains an open task's snapshot at any age.
OPEN_STATES = frozenset({"queued", "claimed", "shipped", "running", "preempting", "cancelling"})
DEAD_STATES = frozenset({"cancelled", "task_failed", "infra_failed"})

KEEP_MARKER = "KEEP"
PREV_NAME = "ckpt_latest.pt.prev"
AUDIT_SUBDIR = ".retention"
AUDIT_LOG = "prune.log"

# Governed by their own GC (`dispatcher._gc_code_snapshots`, 72 h; `spool_worker._prune_blobs`), or
# our own bookkeeping. Never scanned, so never reported as reclaimable either.
#
# `spool` joined 2026-09-17: on an owned box the WORKER's spool can sit INSIDE the coordinator's
# data root (tower binds `/srv/coord/experiments/spool` -> `/root/spool`). Most of it is
# harmless here — `classify()` sends anything not starting with `ckpt` to RECORD, which is never
# governed — but `spool/active/<task>/ckpt_latest.pt` matches no `<grp>/<name>` task row, so it
# falls to `live_weights` and becomes FIFO-evictable under budget pressure. Deleting the checkpoint
# of a RUNNING task is exactly what inv. 5 exists to prevent, and the `<grp>/<name>` convention
# cannot see it. A worker spool is the worker's to GC (`spool_worker._prune_blobs`), never ours.
EXCLUDED_DIRS = frozenset({".dispatcher", AUDIT_SUBDIR, "spool"})


@dataclass
class Candidate:
    path: str
    category: str
    size: int
    mtime: float

    def age_days(self, now: float) -> float:
        return (now - self.mtime) / 86400.0


@dataclass
class CategoryPlan:
    category: str
    files: int = 0
    bytes: int = 0
    ttl_delete: list[Candidate] = field(default_factory=list)
    budget_delete: list[Candidate] = field(default_factory=list)
    protected_bytes: int = 0          # in-category bytes that no pass may touch

    @property
    def reclaimed(self) -> int:
        return sum(c.size for c in self.ttl_delete) + sum(c.size for c in self.budget_delete)

    @property
    def remaining(self) -> int:
        return self.bytes - self.reclaimed


# --------------------------------------------------------------------------- registry


def load_dir_states(conn: sqlite3.Connection, root: Path) -> dict[str, str]:
    """Map absolute run dir -> task state (inv. 8).

    `result_path` is only populated on `done`, so every other state resolves through the
    `<root>/<grp>/<name>` convention the dispatcher writes under. A dir that matches neither is
    left unmapped and its checkpoints fall to `live_weights` — the conservative side, since
    unmatched dirs are pre-fleet manual runs nobody's task row owns.
    """
    out: dict[str, str] = {}
    for state, grp, name, rp in conn.execute(
            "SELECT state, grp, name, result_path FROM tasks"):
        if rp:
            out[os.path.abspath(rp)] = state
        d = os.path.abspath(os.path.join(root, grp or "", name or ""))
        # Never let a `<grp>/<name>` guess overwrite an explicit `result_path` mapping.
        out.setdefault(d, state)
    return out


# --------------------------------------------------------------------------- scan


def classify(basename: str, state: str | None) -> str:
    """Exactly one category per file. Order matters: `prev` outranks `dead_weights` so a `.prev`
    inside a cancelled run is retained on the SHORTER clock, not the longer one."""
    if not basename.startswith("ckpt"):
        return RECORD
    if basename == PREV_NAME:
        return "prev"
    if state in DEAD_STATES:
        return "dead_weights"
    return "live_weights"


def scan(root: Path, dir_states: dict[str, str], now: float,
         min_age_hours: float) -> tuple[dict[str, CategoryPlan], int]:
    """Walk the tree once, classifying every regular file and marking what is off limits.

    Returns (plans keyed by category, count of dirs skipped as protected).
    """
    plans = {c: CategoryPlan(c) for c in (RECORD, *GOVERNED)}
    min_age_s = min_age_hours * 3600.0
    protected_dirs = 0
    # A `KEEP` anywhere on the path protects everything beneath it (inv. 6), so carry the flag down
    # the walk rather than re-checking ancestors per file.
    keep_prefixes: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        if dirpath == str(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            # Files sitting directly in the root are pre-fleet manual logs — out of scope.
            filenames = []
        # Never descend through a symlinked dir (inv. 7) — `experiments/` holds symlinks into
        # worktrees, and following one would prune another checkout's tree.
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]

        keep_prefixes = [p for p in keep_prefixes if dirpath.startswith(p)]
        if KEEP_MARKER in filenames:
            keep_prefixes.append(dirpath + os.sep)
        under_keep = bool(keep_prefixes) or KEEP_MARKER in filenames

        state = dir_states.get(os.path.abspath(dirpath))
        blocked = under_keep or state in OPEN_STATES
        if blocked and filenames:
            protected_dirs += 1

        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.islink(p):
                    continue
                st = os.stat(p)
            except OSError:
                continue
            if not os.path.isfile(p):
                continue
            cat = classify(fn, state)
            plan = plans[cat]
            plan.files += 1
            plan.bytes += st.st_size
            if cat == RECORD or blocked or (now - st.st_mtime) < min_age_s:
                # inv. 2 / 5 / 6 / 11 — counted toward the category total (so the budget report is
                # honest about what is actually on disk) but never a candidate.
                plan.protected_bytes += st.st_size
                continue
            plan.ttl_delete.append(Candidate(p, cat, st.st_size, st.st_mtime))

    return plans, protected_dirs


# --------------------------------------------------------------------------- plan


def plan(plans: dict[str, CategoryPlan], now: float, ttl_days: dict[str, float],
         cat_budget_gb: dict[str, float]) -> dict[str, CategoryPlan]:
    """Pass 1 TTL, then pass 2 budget FIFO over what pass 1 left (inv. 3)."""
    for cat in GOVERNED:
        p = plans[cat]
        eligible = p.ttl_delete            # scan() parked every candidate here
        ttl_s = ttl_days[cat] * 86400.0
        over_ttl, kept = [], []
        for c in eligible:
            (over_ttl if (now - c.mtime) >= ttl_s else kept).append(c)
        p.ttl_delete = over_ttl

        budget = int(cat_budget_gb[cat] * GB)
        remaining = p.bytes - sum(c.size for c in over_ttl)
        if remaining > budget:
            # Oldest first. Protected bytes cannot be evicted, so the budget may be unreachable —
            # delete what we can and let the report show the overage rather than pretending.
            for c in sorted(kept, key=lambda c: c.mtime):
                if remaining <= budget:
                    break
                p.budget_delete.append(c)
                remaining -= c.size
    return plans


# --------------------------------------------------------------------------- apply


def apply_plan(plans: dict[str, CategoryPlan], root: Path, now: float) -> tuple[int, int, list[str]]:
    """Delete, appending one audit record per removed path BEFORE unlinking it (inv. 9)."""
    audit_dir = root / AUDIT_SUBDIR
    audit_dir.mkdir(parents=True, exist_ok=True)
    deleted = freed = 0
    errors: list[str] = []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    with open(audit_dir / AUDIT_LOG, "a", encoding="utf-8") as log:
        for cat in GOVERNED:
            p = plans[cat]
            for reason, group in (("ttl", p.ttl_delete), ("budget", p.budget_delete)):
                for c in group:
                    rec = {"t": stamp, "path": os.path.relpath(c.path, root), "category": cat,
                           "bytes": c.size, "reason": reason, "age_days": round(c.age_days(now), 2)}
                    try:
                        os.unlink(c.path)
                    except OSError as e:
                        errors.append(f"{c.path}: {e}")
                        continue
                    log.write(json.dumps(rec) + "\n")
                    deleted += 1
                    freed += c.size
    return deleted, freed, errors


def remove_empty_dirs(root: Path) -> int:
    """Drop dirs left holding no regular file anywhere beneath them (inv. 10). A dir keeping any
    `record` file survives, which is why this almost never fires outside cancelled runs."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        if dirpath == str(root) or os.path.basename(dirpath) in EXCLUDED_DIRS:
            continue
        if os.path.relpath(dirpath, root).split(os.sep)[0] in EXCLUDED_DIRS:
            continue
        if filenames:
            continue
        try:
            if any(os.scandir(dirpath)):
                continue
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- report


def build_report(plans: dict[str, CategoryPlan], ttl_days: dict[str, float],
                 cat_budget_gb: dict[str, float], total_budget_gb: float,
                 protected_dirs: int, applied: bool) -> dict:
    cats = {}
    for cat in (RECORD, *GOVERNED):
        p = plans[cat]
        governed = cat in GOVERNED
        ttl_b = sum(c.size for c in p.ttl_delete)
        bud_b = sum(c.size for c in p.budget_delete)
        cats[cat] = {
            "files": p.files,
            "gb": round(p.bytes / GB, 2),
            "governed": governed,
            "ttl_days": ttl_days[cat] if governed else None,
            "budget_gb": cat_budget_gb[cat] if governed else None,
            "ttl_reclaim_gb": round(ttl_b / GB, 2),
            "budget_reclaim_gb": round(bud_b / GB, 2),
            "remaining_gb": round(p.remaining / GB, 2),
            "protected_gb": round(p.protected_bytes / GB, 2),
            # Exact counterparts — the GB fields round to 2 dp for humans, which is lossy well
            # below a GB. Inv. 1 (the dry plan IS what --apply removes) is only checkable on these.
            "bytes": p.bytes,
            "ttl_reclaim_bytes": ttl_b,
            "budget_reclaim_bytes": bud_b,
        }
    total = sum(p.bytes for p in plans.values())
    reclaimed = sum(p.reclaimed for p in plans.values())
    return {
        "applied": applied,
        "total_gb": round(total / GB, 2),
        "reclaim_gb": round(reclaimed / GB, 2),
        "after_gb": round((total - reclaimed) / GB, 2),
        "total_bytes": total,
        "reclaim_bytes": reclaimed,
        "total_budget_gb": total_budget_gb,
        "protected_dirs": protected_dirs,
        "categories": cats,
    }


def print_report(rep: dict) -> None:
    print(f"{'category':<14} {'files':>7} {'GB':>8} {'TTL':>6} {'budget':>8} "
          f"{'ttl→':>7} {'fifo→':>7} {'after':>8}")
    print("-" * 72)
    for cat, c in rep["categories"].items():
        ttl = "never" if c["ttl_days"] is None else f"{c['ttl_days']:g}d"
        bud = "—" if c["budget_gb"] is None else f"{c['budget_gb']:g}"
        print(f"{cat:<14} {c['files']:>7} {c['gb']:>8.1f} {ttl:>6} {bud:>8} "
              f"{c['ttl_reclaim_gb']:>7.1f} {c['budget_reclaim_gb']:>7.1f} {c['remaining_gb']:>8.1f}")
    print("-" * 72)
    verb = "reclaimed" if rep["applied"] else "would reclaim"
    print(f"total {rep['total_gb']:.1f} GB → {verb} {rep['reclaim_gb']:.1f} GB → "
          f"{rep['after_gb']:.1f} GB against a {rep['total_budget_gb']:g} GB budget")
    if rep["protected_dirs"]:
        print(f"{rep['protected_dirs']} dir(s) skipped as protected (open task or KEEP marker)")
    if not rep["applied"]:
        print("DRY RUN — nothing was deleted. Re-run with --apply.")


# --------------------------------------------------------------------------- cli


def _kv_days(values: list[str] | None, base: dict[str, float], what: str) -> dict[str, float]:
    out = dict(base)
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"prune: --{what} expects CATEGORY=N, got {item!r}")
        k, _, v = item.partition("=")
        if k not in GOVERNED:
            raise SystemExit(f"prune: unknown category {k!r} (governed: {', '.join(GOVERNED)})")
        try:
            n = float(v)
        except ValueError:
            raise SystemExit(f"prune: --{what} {k}: {v!r} is not a number")
        if n < 0:
            raise SystemExit(f"prune: --{what} {k}: must be >= 0")
        out[k] = n
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Bound experiments/ against a byte budget: TTL first, per-category FIFO after.")
    ap.add_argument("--root", default=None, help="experiments root (default: the shared one)")
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--budget-gb", type=float, default=DEFAULT_TOTAL_BUDGET_GB)
    ap.add_argument("--ttl-days", action="append", metavar="CAT=N")
    ap.add_argument("--cat-budget-gb", action="append", metavar="CAT=N")
    ap.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS)
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve() if a.root else registry_db.shared_experiments_root().resolve()
    if not root.is_dir():
        print(f"prune: --root {root} is not a directory", file=sys.stderr)
        return 2
    db = root / "runs.sqlite"
    if not db.is_file():
        print(f"prune: no runs.sqlite under {root} — refusing to prune an unrecognised tree",
              file=sys.stderr)
        return 2
    if a.min_age_hours < 0:
        print("prune: --min-age-hours must be >= 0", file=sys.stderr)
        return 2

    ttl_days = _kv_days(a.ttl_days, DEFAULT_TTL_DAYS, "ttl-days")
    ttl_days[RECORD] = float("inf")
    cat_budget = _kv_days(a.cat_budget_gb, DEFAULT_CAT_BUDGET_GB, "cat-budget-gb")
    cat_budget[RECORD] = float("inf")
    governed_total = sum(cat_budget[c] for c in GOVERNED)
    if governed_total > a.budget_gb:
        print(f"prune: per-category budgets sum to {governed_total:g} GB, over the "
              f"{a.budget_gb:g} GB total budget", file=sys.stderr)
        return 2

    conn = registry_db.connect(str(db))
    try:
        dir_states = load_dir_states(conn, root)
    finally:
        conn.close()

    now = time.time()
    plans, protected_dirs = scan(root, dir_states, now, a.min_age_hours)
    plans = plan(plans, now, ttl_days, cat_budget)

    errors: list[str] = []
    if a.apply:
        _, _, errors = apply_plan(plans, root, now)
        remove_empty_dirs(root)

    rep = build_report(plans, ttl_days, cat_budget, a.budget_gb, protected_dirs, a.apply)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)
    for e in errors[:20]:
        print(f"prune: failed to delete {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
