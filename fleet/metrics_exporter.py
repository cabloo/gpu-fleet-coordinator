#!/usr/bin/env python3
"""Metrics exporter: the registry's current state as Prometheus text.

Spec: docs/specs/metrics-exporter.spec.md. Read-only, standard library only, and it does not import
the dispatcher: a scrape opens the registry, reads five small things, and closes it.

    python fleet/metrics_exporter.py --db <data root>/runs.sqlite --host 0.0.0.0 --port 9108
    python fleet/metrics_exporter.py --db <data root>/runs.sqlite --once
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DEFAULT_LOOKBACK = 20000
EVENT_BATCH = 5000
MAX_BATCHES_PER_SCRAPE = 40          # inv. 2: at most 200,000 event rows a scrape
# Inv. 7: what may become a label value. Anything else is counted under `other`.
_NAME = re.compile(r"[a-z0-9_]{1,48}\Z")
# Inv. 7 again: a core is its number as the reading row writes it, and a row's cores are capped.
_CORE = re.compile(r"(0|[1-9][0-9]{0,3})\Z")
MAX_CORES_PER_BOX = 1024
GIB = 1024.0 ** 3

# The task state machine's states (run-registry spec). Listed here so every one is served, at 0
# when empty; the test holds this list to `registry_db.LEGAL_STATES`.
TASK_STATES = ("queued", "claimed", "shipped", "running", "preempting", "cancelling",
               "done", "task_failed", "infra_failed", "cancelled")

# Inv. 4: events served at 0 from the first scrape. A counter that first appears at 1 shows a rate
# no increase, so the first failure after a start would be invisible to a rule over these. The
# test holds every name to one the coordinator logs.
TROUBLE_EVENTS = ("claim_timeout", "data_root_low", "dead_worker", "infra_failed", "lost",
                  "owned_unreachable", "rent_failed", "requeue", "ship_failed", "stalled",
                  "task_failed", "undeliverable")

# NVML's reasons for a GPU running below its clocks, by bit. Left out because they are not
# throttling: the card is idle (0x1), or a setting asked for the clocks (0x2 an application's,
# 0x10 sync boost, 0x100 the display's).
GPU_THROTTLE_BITS = (("power_cap", 0x4), ("hw_slowdown", 0x8), ("thermal", 0x20),
                     ("hw_thermal", 0x40), ("power_brake", 0x80))

# reading field -> (metric suffix, multiplier)
_BOX_FIELDS = (
    ("cores", "cores", 1.0), ("load1", "load1", 1.0),
    ("cpu_quota_cores", "cpu_quota_cores", 1.0), ("cpu_used_cores", "cpu_used_cores", 1.0),
    ("ram_total_gb", "ram_total_bytes", GIB), ("ram_avail_gb", "ram_avail_bytes", GIB),
    ("mem_limit_gb", "mem_limit_bytes", GIB), ("mem_used_gb", "mem_used_bytes", GIB),
    ("mem_anon_gb", "mem_anon_bytes", GIB),
    ("vram_total_gb", "vram_total_bytes", GIB), ("vram_used_gb", "vram_used_bytes", GIB),
    ("gpu_util", "gpu_util_ratio", 0.01), ("gpu_mem_util", "gpu_mem_util_ratio", 0.01),
    ("gpu_count", "gpu_count", 1.0),
    ("gpu_temp_c", "gpu_temperature_celsius", 1.0), ("gpu_power_w", "gpu_power_watts", 1.0),
    ("gpu_power_limit_w", "gpu_power_limit_watts", 1.0),
    ("disk_total_gb", "disk_total_bytes", GIB), ("disk_free_gb", "disk_free_bytes", GIB),
    ("slots_eff", "slots_effective", 1.0), ("running", "tasks_running", 1.0),
    ("open", "tasks_open", 1.0),
)
_PRESSURE = (("waiting", (("cpu", "cpu_some"), ("memory", "mem_some"), ("io", "io_some"))),
             ("stalled", (("memory", "mem_full"), ("io", "io_full"))))

_HELP = {
    "fleet_tasks": ("gauge", "Tasks in each state."),
    "fleet_queue_oldest_seconds": ("gauge", "The longest any queued task has waited since it entered the queue; 0 with an empty queue."),
    "fleet_poll_cycle_timestamp_seconds": ("gauge", "When the newest poll cycle was recorded."),
    "fleet_poll_cycle_seconds": ("gauge", "How long the newest poll cycle took."),
    "fleet_poll_phase_seconds": ("gauge", "The newest poll cycle's time in each phase."),
    "fleet_data_root_free_bytes": ("gauge", "Free space on the data root, as the dispatcher last read it."),
    "fleet_data_root_holding": ("gauge", "1 while the free-space guard holds."),
    "fleet_box_info": ("gauge", "1 for each box that is not destroyed or lost."),
    "fleet_box_slots": ("gauge", "The box's registered slots."),
    "fleet_box_paused": ("gauge", "1 for a box with a pause row."),
    "fleet_box_measured_timestamp_seconds": ("gauge", "When the box's reading was taken."),
    "fleet_box_gpu_throttled": ("gauge", "1 while the GPU reports this reason for running below its clocks."),
    "fleet_box_pressure_waiting_seconds_total": ("counter", "Time some task on the box's host waited for the resource."),
    "fleet_box_pressure_stalled_seconds_total": ("counter", "Time every task on the box's host waited for the resource."),
    "fleet_box_cpu_busy_seconds_total": ("counter", "CPU time the box's whole machine spent on anything but idling and waiting for I/O."),
    "fleet_box_cpu_core_busy_seconds_total": ("counter", "That time, for each core of a machine the site owns."),
    "fleet_box_network_receive_bytes_total": ("counter", "Bytes received on the one interface the box's worker sees beside loopback."),
    "fleet_box_network_transmit_bytes_total": ("counter", "Bytes sent on the one interface the box's worker sees beside loopback."),
    "fleet_events_total": ("counter", "Events appended to the registry since the exporter started."),
    "fleet_registry_last_event_seq": ("gauge", "The newest event row the exporter has read."),
}


def _number(value) -> float | None:
    """A finite number, or None. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:                # an integer too large for a float
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _count(value) -> float | None:
    """A counter's value: a finite number that is not negative, or None."""
    value = _number(value)
    return value if value is not None and value >= 0 else None


def _epoch(text) -> float | None:
    """The registry's timestamps are `YYYY-MM-DDTHH:MM:SSZ` (UTC)."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _json(text) -> dict | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) and abs(value) < 1e15 else repr(value)


class Exporter:
    """One per process. Holds only what a scrape cannot read back cheaply: where it is in the
    event table, the counts since it started, and the newest poll cycle it has seen."""

    def __init__(self, db: str, lookback: int = DEFAULT_LOOKBACK, clock=time.time, connect=None):
        self.db = str(db)
        self.lookback = max(0, int(lookback))
        self.clock = clock
        self._connect = connect or self._open
        self._lock = threading.Lock()
        self._last_seq: int | None = None        # None until the first scrape has found its place
        self._count_from = 0                     # rows after this one are counted (inv. 4)
        self._counts: dict[str, int] = dict.fromkeys(TROUBLE_EVENTS, 0)
        self._cycle: tuple[float | None, dict] | None = None

    def _open(self) -> sqlite3.Connection:
        # Inv. 1: `mode=ro` never creates the file; a missing registry raises here. The path is
        # quoted: in a URI a `?`, a `#` or a `%` in it would be read as syntax.
        uri = "file:" + urllib.parse.quote(str(Path(self.db))) + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)

    # -- events (inv. 2, 4) --
    def _follow_events(self, conn) -> None:
        (newest,) = conn.execute("SELECT MAX(seq) FROM events").fetchone()
        newest = int(newest or 0)
        if self._last_seq is None or newest < self._last_seq:
            # The first scrape, or the registry was replaced by a shorter one. Rows up to `newest`
            # were there before the exporter looked: they place the newest poll cycle and are never
            # counted, however many scrapes it takes to read them.
            self._last_seq = max(0, newest - self.lookback)
            self._count_from = newest
        for _ in range(MAX_BATCHES_PER_SCRAPE):
            rows = conn.execute(
                "SELECT seq, t, event, CASE WHEN event='poll_cycle' THEN detail END "
                "FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (self._last_seq, EVENT_BATCH)).fetchall()
            for seq, t, event, detail in rows:
                self._last_seq = int(seq)
                if event == "poll_cycle":
                    parsed = _json(detail)
                    if parsed is not None:
                        self._cycle = (_epoch(t), parsed)
                if self._last_seq > self._count_from:
                    name = event if isinstance(event, str) and _NAME.match(event) else "other"
                    self._counts[name] = self._counts.get(name, 0) + 1
            if len(rows) < EVENT_BATCH:
                break

    # -- one scrape --
    def collect(self) -> str:
        """The metrics text. Raises (sqlite3.Error, OSError) when the registry cannot be read."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")            # inv. 8: one snapshot
                self._follow_events(conn)
                states = dict(conn.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall())
                # `updated_at` is written at insert and at each change of state and by nothing else,
                # so for a queued task it is when it entered the queue (again, after a requeue).
                (oldest,) = conn.execute("SELECT MIN(updated_at) FROM tasks WHERE state='queued'").fetchone()
                boxes = conn.execute(
                    "SELECT id, label, state, source, gpu_name, slots_total FROM instances "
                    "WHERE state NOT IN ('destroyed','lost') ORDER BY id").fetchall()
                readings = dict(conn.execute(
                    "SELECT key, value FROM settings WHERE key >= 'box_reading_i' AND key < 'box_reading_j'").fetchall())
                pauses = dict(conn.execute(
                    "SELECT key, value FROM settings WHERE key >= 'pause_i' AND key < 'pause_j'").fetchall())
                guard = conn.execute("SELECT value FROM settings WHERE key='data_root_guard'").fetchone()
                conn.execute("ROLLBACK")
            finally:
                conn.close()
            return self._render(states, oldest, boxes, readings, pauses, guard[0] if guard else None)

    def _render(self, states, oldest, boxes, readings, pauses, guard) -> str:
        out: list[str] = []
        seen: set[str] = set()

        def emit(name: str, value, labels: tuple = ()) -> None:
            value = _number(value)
            if value is None:
                return
            if name not in seen:
                seen.add(name)
                kind, text = _HELP.get(name, ("gauge", "From the box's newest reading."))
                out.append(f"# HELP {name} {text}")
                out.append(f"# TYPE {name} {kind}")
            body = ",".join(f'{k}="{_label(str(v))}"' for k, v in labels)
            out.append(f"{name}{{{body}}} {_fmt(value)}" if body else f"{name} {_fmt(value)}")

        def named(pairs) -> dict:
            """Inv. 7: sum values by name, a name that may not be a label under `other`."""
            total: dict[str, float] = {}
            for name, value in pairs:
                value = _number(value)
                if value is not None:
                    name = name if isinstance(name, str) and _NAME.match(name) else "other"
                    total[name] = total.get(name, 0.0) + value
            return total

        # Every state of the state machine, at 0 when empty; then whatever else the table holds.
        extra = named((state, n) for state, n in states.items() if state not in TASK_STATES)
        for state in TASK_STATES:
            emit("fleet_tasks", states.get(state, 0), (("state", state),))
        for state in sorted(extra):
            emit("fleet_tasks", extra[state], (("state", state),))
        born = _epoch(oldest)
        emit("fleet_queue_oldest_seconds", max(0.0, self.clock() - born) if born is not None else 0)

        if self._cycle is not None:
            at, cycle = self._cycle
            emit("fleet_poll_cycle_timestamp_seconds", at)
            emit("fleet_poll_cycle_seconds", cycle.get("total_sec"))
            phases = cycle.get("phases")
            phases = named(phases.items()) if isinstance(phases, dict) else {}
            for phase in sorted(phases):
                emit("fleet_poll_phase_seconds", phases[phase], (("phase", phase),))
            root = cycle.get("data_root")
            free_gb = _number(root.get("free_gb")) if isinstance(root, dict) else None
            if free_gb is not None:
                emit("fleet_data_root_free_bytes", free_gb * GIB)
        holding = (_json(guard) or {}).get("holding")
        emit("fleet_data_root_holding", 1 if holding is True else 0)

        present = [(int(i), str(label), str(state), str(source), gpu, slots)
                   for i, label, state, source, gpu, slots in boxes]
        for i, label, state, source, gpu, slots in present:
            emit("fleet_box_info", 1, (("label", label), ("instance_id", i), ("source", source),
                                       ("state", state), ("gpu", "yes" if gpu else "no")))
        for i, label, _state, _source, _gpu, slots in present:
            emit("fleet_box_slots", slots, (("label", label), ("instance_id", i)))
        for i, label, *_ in present:
            mode = (_json(pauses.get(f"pause_i{i}")) or {}).get("mode")
            if isinstance(mode, str) and _NAME.match(mode):
                emit("fleet_box_paused", 1, (("label", label), ("instance_id", i), ("mode", mode)))

        # Inv. 3 and 6: a reading row counts only for a box that is still here.
        got = [((("label", label), ("instance_id", i)), _json(readings.get(f"box_reading_i{i}")))
               for i, label, *_ in present]
        got = [(labels, reading) for labels, reading in got if reading is not None]
        for labels, reading in got:
            emit("fleet_box_measured_timestamp_seconds", reading.get("at"), labels)
        for field, suffix, scale in _BOX_FIELDS:
            for labels, reading in got:
                value = _number(reading.get(field))
                if value is not None:
                    emit(f"fleet_box_{suffix}", value * scale, labels)
        for labels, reading in got:
            mask = reading.get("gpu_throttle")       # an integer of bits: never through a float
            if isinstance(mask, int) and not isinstance(mask, bool) and 0 <= mask < 1 << 64:
                for reason, bit in GPU_THROTTLE_BITS:
                    emit("fleet_box_gpu_throttled", 1 if mask & bit else 0, labels + (("reason", reason),))
        for kind, pairs in _PRESSURE:
            for labels, reading in got:
                psi = reading.get("psi")
                for resource, key in pairs if isinstance(psi, dict) else ():
                    usec = _number(psi.get(key))
                    if usec is not None:
                        emit(f"fleet_box_pressure_{kind}_seconds_total", usec / 1e6, labels + (("resource", resource),))
        for labels, reading in got:
            usec = _count(reading.get("host_cpu_busy_usec"))
            if usec is not None:
                emit("fleet_box_cpu_busy_seconds_total", usec / 1e6, labels)
        # A rented machine's cores are its host's, shared with other renters, and every rental is a
        # new box: its cores would be hundreds of series nobody reads. The machine's whole time,
        # just above, is served for every box.
        owned = {i for i, _label, _state, source, _gpu, _slots in present if source == "owned"}
        for labels, reading in got:
            per_core = reading.get("host_cpu_core_busy_usec") if labels[1][1] in owned else None
            names = [c for c in per_core if isinstance(c, str) and _CORE.match(c)] if isinstance(per_core, dict) else []
            for core in sorted(names, key=int)[:MAX_CORES_PER_BOX]:
                usec = _count(per_core[core])
                if usec is not None:
                    emit("fleet_box_cpu_core_busy_seconds_total", usec / 1e6, labels + (("core", core),))
        for field, way in (("net_rx_bytes", "receive"), ("net_tx_bytes", "transmit")):
            for labels, reading in got:
                emit(f"fleet_box_network_{way}_bytes_total", _count(reading.get(field)), labels)

        for name in sorted(self._counts):
            emit("fleet_events_total", self._counts[name], (("event", name),))
        emit("fleet_registry_last_event_seq", self._last_seq or 0)
        return "\n".join(out) + "\n"


def make_handler(exporter: Exporter):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, body: bool) -> None:
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                text = exporter.collect().encode()
            except Exception as error:   # noqa: BLE001 — whatever went wrong, the answer is "down"
                # No metrics text: a scraper records the source as down, never a stale answer and
                # never a connection that closes with no answer at all.
                self.send_error(500, explain=f"the registry cannot be read: {type(error).__name__}")
                return
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            if body:
                self.wfile.write(text)

        def do_GET(self) -> None:  # noqa: N802
            self._answer(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._answer(False)

        def log_message(self, *args) -> None:
            pass

    return Handler


def default_db() -> str:
    import registry_db   # only for the default path; nothing else of the coordinator is loaded
    return str(registry_db.shared_experiments_root() / "runs.sqlite")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="the registry (default: runs.sqlite under the shared experiments root)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                        help="event rows read once at start, to find the newest poll cycle")
    parser.add_argument("--once", action="store_true", help="print one scrape and exit")
    args = parser.parse_args(argv)
    exporter = Exporter(args.db or default_db(), lookback=args.lookback)
    if args.once:
        try:
            sys.stdout.write(exporter.collect())
        except (sqlite3.Error, OSError) as error:
            print(f"the registry cannot be read: {error}", file=sys.stderr)
            return 1
        return 0
    server = ThreadingHTTPServer((args.host, args.port), make_handler(exporter))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
