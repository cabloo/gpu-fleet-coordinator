"""Operator escape hatch: roll the worker upgrade NOW, accepting a graceful evict.

The rolling upgrade (`docs/specs/worker-rolling-upgrade.spec.md`) waits for a box to drain
naturally, which takes as long as its longest occupant — median 1.1h, p90 4.8h, p99 13.3h. That is
right for a routine change and WRONG when the worker change is itself a correctness fix.

This sets a ONE-SHOT flag the coordinator consumes on its next poll: the box currently admitted to
the roll has its running occupants gracefully evicted — each checkpoints, exits at its next save,
and requeues WITH RESUME. Nothing is killed and no in-flight work is interrupted before a
checkpoint; the cost is the delay of a checkpoint-and-repack, not the run.

    python fleet/roll_now.py            # arm it (consumed on the next poll)
    python fleet/roll_now.py --status   # is it armed?
    python fleet/roll_now.py --cancel   # disarm before it fires

⛔ OPERATOR-ONLY BY CONSTRUCTION. Nothing in the coordinator sets this key — it is written here, by
a human, and DELETED by the coordinator the moment it acts. It cannot stay on, so it can never
quietly turn every future roll into a preempting one. Same rule as `--resume-unverified`: the hatch
exists, machinery can never reach it, and it does not persist.

⚠ It does NOT widen the roll. To upgrade more boxes at once, raise `worker_roll_max_draining` (read
live from the DB, so no coordinator restart is needed):

    python fleet/roll_now.py --width 2
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_client
import registry_db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--status", action="store_true", help="report without changing anything")
    ap.add_argument("--cancel", action="store_true", help="disarm before the coordinator acts")
    ap.add_argument("--width", type=int, default=None,
                    help="set worker_roll_max_draining (live; no coordinator restart needed)")
    a = ap.parse_args(argv)

    if not a.status and not a.cancel and a.width is None and api_client.enabled():
        return api_client.run(api_client.ApiClient().roll_now)
    db = a.db or str(registry_db.shared_experiments_root() / "runs.sqlite")
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    def get(key, default=None):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    if a.width is not None:
        if a.width < 1:
            print("roll_now: --width must be >= 1 (0 would freeze the roll entirely)")
            return 2
        conn.execute("INSERT INTO settings(key,value) VALUES('worker_roll_max_draining',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(a.width),))
        conn.commit()
        print(f"roll_now: worker_roll_max_draining = {a.width} (live — no restart needed)")

    if a.cancel:
        conn.execute("DELETE FROM settings WHERE key='worker_roll_now'")
        conn.commit()
        print("roll_now: DISARMED")

    if a.status or a.cancel or a.width is not None:
        held = [k for (k,) in conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'worker_roll_i%'")]
        print(f"roll_now: armed={bool(get('worker_roll_now'))} "
              f"width={get('worker_roll_max_draining', 1)} "
              f"holding={sorted(held) or 'none'}")
        return 0

    conn.execute("INSERT INTO settings(key,value) VALUES('worker_roll_now','true') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.commit()
    print("roll_now: ARMED — the coordinator will gracefully evict the rolling box's occupants on "
          "its next poll, then upgrade it. One shot; it disarms itself when it fires.")
    print("          Each occupant checkpoints, exits at its next save, and requeues WITH RESUME.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
