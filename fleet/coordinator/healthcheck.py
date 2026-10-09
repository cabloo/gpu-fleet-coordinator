#!/usr/bin/env python3
"""Container healthcheck — is the POLL LOOP still turning?

Spec: docs/specs/coordinator-container.spec.md (inv. 8).

`--restart unless-stopped` only notices a process that EXITED. The failure mode that actually costs
this fleet money is the opposite one: a daemon that is alive and wedged — stuck in a serial ssh
phase behind a dead box, so nothing ships, nothing is ingested, and boxes bill while idle. That has
happened (cycle median 31 min against a 7-9 min design point, ship duty collapsed to 16%,
dispatcher.py:`poll_once`), and no process-liveness check can see it.

`poll_once` emits one `poll_cycle` event per iteration, so the freshness of the newest one IS the
loop's pulse. Report unhealthy when it goes stale.

Exit 0 = healthy, 1 = unhealthy. Docker marks the container unhealthy; nothing is restarted
automatically (a restart mid-ship is not obviously better than a slow cycle — surface it, let the
operator decide).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

DB = os.environ.get("COORD_DB", "/srv/fleet/experiments/runs.sqlite")
# Generous by design: a legitimate cycle is minutes and degrades with box count. This is a
# WEDGED-loop detector, not a latency SLO — a threshold tight enough to catch slow cycles would
# flap, and `poll_cycle`'s own phase timings are the right tool for that question.
MAX_AGE_MIN = float(os.environ.get("COORD_HEALTH_MAX_AGE_MIN", "60"))
# Grace after container start: the first cycle has not landed yet, and a fresh test-bed registry may
# have no `events` table at all.
GRACE_MIN = float(os.environ.get("COORD_HEALTH_GRACE_MIN", "20"))


def _minutes_since_start() -> float:
    """Age of THIS container, from the marker the entrypoint drops.

    Not `/proc/uptime` — that is the HOST's uptime (the PID namespace does not virtualise it), so on
    a server that has been up for weeks it reports weeks and the grace period silently never
    applies. That would make the very first healthcheck after a roll fail before any `poll_cycle`
    has been written.
    """
    try:
        with open("/tmp/coord_started_at") as f:
            return (time.time() - float(f.read().strip())) / 60.0
    except (OSError, ValueError):
        return 0.0  # no marker: assume we JUST started, i.e. stay inside the grace period


def main() -> int:
    age_since_start = _minutes_since_start()

    if not os.path.exists(DB):
        return 0 if age_since_start < GRACE_MIN else _unhealthy(f"no registry at {DB}")

    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5.0)
        row = conn.execute(
            "SELECT t FROM events WHERE event='poll_cycle' ORDER BY seq DESC LIMIT 1").fetchone()
    except sqlite3.Error as e:
        return 0 if age_since_start < GRACE_MIN else _unhealthy(f"registry unreadable: {e}")

    if row is None:
        return 0 if age_since_start < GRACE_MIN else _unhealthy("no poll_cycle event ever recorded")

    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return _unhealthy(f"unparseable poll_cycle timestamp {row[0]!r}")

    age_min = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 60.0
    if age_min > MAX_AGE_MIN:
        return _unhealthy(f"last poll_cycle {age_min:.1f} min ago (> {MAX_AGE_MIN}) — loop wedged?")
    print(f"healthy: last poll_cycle {age_min:.1f} min ago")
    return 0


def _unhealthy(msg: str) -> int:
    print(f"UNHEALTHY: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
