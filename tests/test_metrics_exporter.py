"""Metrics exporter — the registry's state as Prometheus text (docs/specs/metrics-exporter.spec.md).

Each test builds a small registry with the REAL schema (via registry_db.connect) and asserts the
text served for it. The exporter never writes, so every test may also compare the file's bytes
before and after.
"""

import hashlib
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


registry_db = _load("registry_db", "fleet/registry_db.py")
exporter_mod = _load("metrics_exporter", "fleet/metrics_exporter.py")

NOW = 1_800_000_000.0                    # 2027-01-15T08:00:00Z
T0 = "2027-01-15T07:58:20Z"              # 100 s before NOW
T1 = "2027-01-15T07:59:30Z"              # 30 s before NOW
GIB = 1024 ** 3

CYCLE_OLD = {"total_sec": 99.0, "phases": {"ingest": 90.0}, "data_root": {"free_gb": 1.0}}
CYCLE_NEW = {"total_sec": 27.5, "n_boxes": 2,
             "phases": {"ingest": 10.4, "place": 5.0, "Not A Phase!": 3.0},
             "ingest_detail": {"a task name": 1}, "data_root": {"free_gb": 100.5, "holding": False}}

READING_GPU = {
    "v": 1, "at": NOW - 40, "cores": 32, "load1": 10.09, "ram_total_gb": 39.2, "ram_avail_gb": 22.8,
    "cpu_quota_cores": None, "cpu_used_cores": 10.02, "cpu_usage_usec": 3053887268638.0,
    "mem_limit_gb": None, "mem_used_gb": 4.5, "mem_anon_gb": 2.6,
    "vram_total_gb": 12.0, "vram_used_gb": 3.0, "gpu_util": 50.0, "gpu_mem_util": 25.0,
    "gpu_name": "A GPU, with a comma", "gpu_count": 1,
    "gpu_temp_c": 71, "gpu_power_w": 140, "gpu_power_limit_w": 175, "gpu_throttle": 0x4 | 0x20,
    "psi": {"cpu_some": 2_500_000, "mem_some": 1_000_000, "mem_full": 500_000,
            "io_some": 250_000, "io_full": None},
    "disk_total_gb": 1007.0, "disk_free_gb": 250.5,
    # Microseconds and bytes (task-dispatcher inv. 31f). The cores are out of order on purpose.
    "host_cpu_busy_usec": 1234560000, "host_cpu_core_busy_usec": {"10": 34560000, "0": 600000000, "2": 600000000},
    "net_rx_bytes": 5000000, "net_tx_bytes": 7000000,
    "slots_total": 16, "slots_eff": 12, "running": 10, "open": 11,
    "ep": "a/secret/entry_point.py", "grp": "a_private_group",
}
READING_NO_GPU = {
    "v": 1, "at": NOW - 300, "cores": 24, "load1": 1.5, "ram_total_gb": 61.0, "ram_avail_gb": 40.0,
    "cpu_quota_cores": 16.0, "cpu_used_cores": None, "mem_limit_gb": 32.0, "mem_used_gb": 8.0,
    "mem_anon_gb": 6.0, "vram_total_gb": None, "vram_used_gb": None, "gpu_util": None,
    "gpu_mem_util": None, "gpu_name": None, "gpu_count": None,
    "slots_total": 16, "slots_eff": 16, "running": 0, "open": 0, "ep": None, "grp": None,
}


def _task(conn, name, state, created_at, **fields):
    registry_db.insert_task(
        conn, id=f"id-{name}", created_at=created_at, created_by="tester", grp="a_private_group",
        name=name, entrypoint="a/secret/entry_point.py", args_json='["--key", "a-secret-value"]',
        config_json="{}", config_hash=f"c-{name}", arm_hash=f"a-{name}", git_sha="", est_minutes=5,
        state=state, **fields)


def _box(conn, iid, label, state, source="owned", gpu=None, slots=16):
    conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, gpu_name, slots_total, "
        "hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, label, T0, state, 0.0, gpu, slots, T0, source))


def _setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value if isinstance(value, str) else json.dumps(value)))


def _event(conn, t, event, detail="", instance_id=None):
    conn.execute("INSERT INTO events(t, instance_id, event, detail) VALUES (?,?,?,?)",
                 (t, instance_id, event, detail if isinstance(detail, str) else json.dumps(detail)))


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "runs.sqlite"
    conn = registry_db.connect(str(path))
    _task(conn, "q-old", "queued", T0)
    _task(conn, "q-new", "queued", T1)
    # Made an hour ago, ran, and went back to the queue 30 s ago: it has waited 30 s, not an hour.
    _task(conn, "q-requeued", "queued", "2027-01-15T07:00:00Z", updated_at=T1)
    for n in range(3):
        _task(conn, f"r{n}", "running", T0)
    _task(conn, "d0", "done", T0)
    _box(conn, -1, "laptop-gpu", "live", gpu="A GPU, with a comma")
    _box(conn, -3, "tower", "paused", slots=8)
    _box(conn, -5, "old-desktop", "live", gpu="Another GPU")           # its reading row does not parse
    _box(conn, 40000001, "runq_rented", "destroyed", source="vast")
    _setting(conn, "box_reading_i-1", READING_GPU)
    _setting(conn, "box_reading_i-3", READING_NO_GPU)
    _setting(conn, "box_reading_i-5", "{not json")
    _setting(conn, "box_reading_i40000001", READING_GPU)               # the box is gone (inv. 6)
    _setting(conn, "pause_i-3", {"mode": "hold", "at": T0})
    _setting(conn, "data_root_guard", {"holding": False, "at": T0})
    _event(conn, T0, "poll_cycle", CYCLE_OLD)
    _event(conn, T0, "claim", "a task name")
    _event(conn, T1, "poll_cycle", CYCLE_NEW)
    conn.commit()
    conn.close()
    return path


def _newest_seq(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    finally:
        conn.close()


def _exporter(path, **kwargs):
    return exporter_mod.Exporter(str(path), clock=lambda: NOW, **kwargs)


def _samples(text):
    return [line for line in text.splitlines() if not line.startswith("#")]


def _events(text):
    return [line for line in _samples(text) if line.startswith("fleet_events_total")]


# Inv. 4: served from the first scrape, at 0.
QUIET = [f'fleet_events_total{{event="{name}"}} 0' for name in sorted(exporter_mod.TROUBLE_EVENTS)]


def test_the_exact_text_for_a_small_registry(registry):
    newest = _newest_seq(registry)         # queueing a task logs an event of its own
    box1 = 'label="laptop-gpu",instance_id="-1"'
    box3 = 'label="tower",instance_id="-3"'
    box5 = 'label="old-desktop",instance_id="-5"'
    assert _samples(_exporter(registry).collect()) == [
        'fleet_tasks{state="queued"} 3',
        'fleet_tasks{state="claimed"} 0',
        'fleet_tasks{state="shipped"} 0',
        'fleet_tasks{state="running"} 3',
        'fleet_tasks{state="preempting"} 0',
        'fleet_tasks{state="cancelling"} 0',
        'fleet_tasks{state="done"} 1',
        'fleet_tasks{state="task_failed"} 0',
        'fleet_tasks{state="infra_failed"} 0',
        'fleet_tasks{state="cancelled"} 0',
        "fleet_queue_oldest_seconds 100",
        f"fleet_poll_cycle_timestamp_seconds {int(NOW - 30)}",        # the NEWEST of the two
        "fleet_poll_cycle_seconds 27.5",
        'fleet_poll_phase_seconds{phase="ingest"} 10.4',
        'fleet_poll_phase_seconds{phase="other"} 3',                   # "Not A Phase!" is no label
        'fleet_poll_phase_seconds{phase="place"} 5',
        f"fleet_data_root_free_bytes {int(100.5 * GIB)}",
        "fleet_data_root_holding 0",
        'fleet_box_info{label="old-desktop",instance_id="-5",source="owned",state="live",gpu="yes"} 1',
        'fleet_box_info{label="tower",instance_id="-3",source="owned",state="paused",gpu="no"} 1',
        'fleet_box_info{label="laptop-gpu",instance_id="-1",source="owned",state="live",gpu="yes"} 1',
        f"fleet_box_slots{{{box5}}} 16",
        f"fleet_box_slots{{{box3}}} 8",
        f"fleet_box_slots{{{box1}}} 16",
        f'fleet_box_paused{{{box3},mode="hold"}} 1',
        f"fleet_box_measured_timestamp_seconds{{{box3}}} {int(NOW - 300)}",
        f"fleet_box_measured_timestamp_seconds{{{box1}}} {int(NOW - 40)}",
        f"fleet_box_cores{{{box3}}} 24",
        f"fleet_box_cores{{{box1}}} 32",
        f"fleet_box_load1{{{box3}}} 1.5",
        f"fleet_box_load1{{{box1}}} 10.09",
        f"fleet_box_cpu_quota_cores{{{box3}}} 16",                      # absent where not measured
        f"fleet_box_cpu_used_cores{{{box1}}} 10.02",
        f"fleet_box_ram_total_bytes{{{box3}}} {int(61.0 * GIB)}",
        f"fleet_box_ram_total_bytes{{{box1}}} {39.2 * GIB!r}",
        f"fleet_box_ram_avail_bytes{{{box3}}} {int(40.0 * GIB)}",
        f"fleet_box_ram_avail_bytes{{{box1}}} {22.8 * GIB!r}",
        f"fleet_box_mem_limit_bytes{{{box3}}} {int(32.0 * GIB)}",
        f"fleet_box_mem_used_bytes{{{box3}}} {int(8.0 * GIB)}",
        f"fleet_box_mem_used_bytes{{{box1}}} {int(4.5 * GIB)}",
        f"fleet_box_mem_anon_bytes{{{box3}}} {int(6.0 * GIB)}",
        f"fleet_box_mem_anon_bytes{{{box1}}} {2.6 * GIB!r}",
        f"fleet_box_vram_total_bytes{{{box1}}} {int(12.0 * GIB)}",
        f"fleet_box_vram_used_bytes{{{box1}}} {int(3.0 * GIB)}",
        f"fleet_box_gpu_util_ratio{{{box1}}} 0.5",
        f"fleet_box_gpu_mem_util_ratio{{{box1}}} 0.25",
        f"fleet_box_gpu_count{{{box1}}} 1",
        f"fleet_box_gpu_temperature_celsius{{{box1}}} 71",
        f"fleet_box_gpu_power_watts{{{box1}}} 140",
        f"fleet_box_gpu_power_limit_watts{{{box1}}} 175",
        f"fleet_box_disk_total_bytes{{{box1}}} {int(1007.0 * GIB)}",
        f"fleet_box_disk_free_bytes{{{box1}}} {int(250.5 * GIB)}",
        f"fleet_box_slots_effective{{{box3}}} 16",
        f"fleet_box_slots_effective{{{box1}}} 12",
        f"fleet_box_tasks_running{{{box3}}} 0",
        f"fleet_box_tasks_running{{{box1}}} 10",
        f"fleet_box_tasks_open{{{box3}}} 0",
        f"fleet_box_tasks_open{{{box1}}} 11",
        f'fleet_box_gpu_throttled{{{box1},reason="power_cap"}} 1',
        f'fleet_box_gpu_throttled{{{box1},reason="hw_slowdown"}} 0',
        f'fleet_box_gpu_throttled{{{box1},reason="thermal"}} 1',
        f'fleet_box_gpu_throttled{{{box1},reason="hw_thermal"}} 0',
        f'fleet_box_gpu_throttled{{{box1},reason="power_brake"}} 0',
        f'fleet_box_pressure_waiting_seconds_total{{{box1},resource="cpu"}} 2.5',
        f'fleet_box_pressure_waiting_seconds_total{{{box1},resource="memory"}} 1',
        f'fleet_box_pressure_waiting_seconds_total{{{box1},resource="io"}} 0.25',
        f'fleet_box_pressure_stalled_seconds_total{{{box1},resource="memory"}} 0.5',
        f"fleet_box_cpu_busy_seconds_total{{{box1}}} 1234.56",
        f'fleet_box_cpu_core_busy_seconds_total{{{box1},core="0"}} 600',       # by number, not by text
        f'fleet_box_cpu_core_busy_seconds_total{{{box1},core="2"}} 600',
        f'fleet_box_cpu_core_busy_seconds_total{{{box1},core="10"}} 34.56',
        f"fleet_box_network_receive_bytes_total{{{box1}}} 5000000",
        f"fleet_box_network_transmit_bytes_total{{{box1}}} 7000000",
        *QUIET,
        f"fleet_registry_last_event_seq {newest}",
    ]


def test_every_metric_is_declared_once_and_its_samples_stay_together(registry):
    """The text format's rule: one HELP and one TYPE a metric, and its samples contiguous."""
    lines = _exporter(registry).collect().splitlines()
    declared = [line.split()[2] for line in lines if line.startswith("# TYPE")]
    assert len(declared) == len(set(declared))
    order = []
    for line in lines:
        if not line.startswith("#"):
            name = line.split("{")[0].split(" ")[0]
            if not order or order[-1] != name:
                order.append(name)
    assert len(order) == len(set(order)), "a metric's samples are split"
    assert set(order) <= set(declared)
    kinds = dict(line.split()[2:4] for line in lines if line.startswith("# TYPE"))
    assert {name for name, kind in kinds.items() if kind == "counter"} == {
        "fleet_box_pressure_waiting_seconds_total", "fleet_box_pressure_stalled_seconds_total",
        "fleet_box_cpu_busy_seconds_total", "fleet_box_cpu_core_busy_seconds_total",
        "fleet_box_network_receive_bytes_total", "fleet_box_network_transmit_bytes_total",
        "fleet_events_total"}
    # A gauge is not named like a counter (the convention a metrics linter holds a source to).
    assert not [name for name, kind in kinds.items() if kind == "gauge" and name.endswith("_total")]


def test_nothing_private_is_served(registry):
    """Inv. 7: arguments can hold secrets, and no task, group or entry point is a label or a value."""
    text = _exporter(registry).collect()
    for private in ("a-secret-value", "a_private_group", "entry_point", "a task name", "q-old", "tester",
                    "A GPU", "Not A Phase"):
        assert private not in text, private


def test_events_are_counted_from_the_exporters_start_and_only_by_their_key(registry):
    statements = []
    exporter = _exporter(registry)

    def connect():
        conn = sqlite3.connect(f"file:{registry}?mode=ro", uri=True, isolation_level=None)
        conn.set_trace_callback(statements.append)
        return conn

    exporter._connect = connect
    start = _newest_seq(registry)
    first = exporter.collect()
    assert _events(first) == QUIET, "rows the registry already held are not counted (inv. 4)"

    conn = registry_db.connect(str(registry))
    for name in ("requeue", "requeue", "stalled", "Weird Name; DROP", "x" * 60):
        _event(conn, T1, name)
    _event(conn, T1, "poll_cycle", {"total_sec": 3.0, "phases": {}})
    conn.commit()
    conn.close()

    second = _samples(exporter.collect())
    counted = {'fleet_events_total{event="requeue"} 0': 'fleet_events_total{event="requeue"} 2',
               'fleet_events_total{event="stalled"} 0': 'fleet_events_total{event="stalled"} 1'}
    assert _events("\n".join(second)) == sorted(
        [counted.get(line, line) for line in QUIET]
        + ['fleet_events_total{event="other"} 2', 'fleet_events_total{event="poll_cycle"} 1'])
    assert "fleet_poll_cycle_seconds 3" in second and f"fleet_registry_last_event_seq {start + 6}" in second
    assert _samples(exporter.collect()) == second, "a scrape with nothing new counts nothing"

    on_events = [s for s in statements if "FROM events" in s]
    assert on_events and all("MAX(seq)" in s or "WHERE seq >" in s for s in on_events), on_events
    assert not any("WHERE event" in s for s in on_events), "the event table was read by name (inv. 2)"
    # Inv. 8: every statement of a scrape is inside that scrape's one read transaction.
    inside = False
    for statement in statements:
        if statement in ("BEGIN", "ROLLBACK"):
            assert inside == (statement == "ROLLBACK"), statements
            inside = statement == "BEGIN"
        else:
            assert inside, f"read outside the scrape's snapshot: {statement}"
    assert not inside and statements.count("BEGIN") == 3, "three scrapes, three snapshots"


def test_the_start_lookback_bounds_what_is_read_and_still_finds_the_newest_cycle(registry):
    conn = registry_db.connect(str(registry))
    for _ in range(50):
        _event(conn, T1, "hold")
    conn.commit()
    conn.close()
    near = _samples(_exporter(registry, lookback=10).collect())
    assert f"fleet_registry_last_event_seq {_newest_seq(registry)}" in near
    assert not any(line.startswith("fleet_poll_cycle") for line in near), "the cycle is older than the lookback"
    far = _samples(_exporter(registry, lookback=60).collect())
    assert "fleet_poll_cycle_seconds 27.5" in far


def test_rows_the_registry_already_held_are_never_counted_however_many_scrapes_read_them(registry, monkeypatch):
    """Inv. 2 and 4: one scrape reads a bounded number of rows. A lookback longer than that is
    finished by the next scrapes, and none of it is an event "appended since the start"."""
    conn = registry_db.connect(str(registry))
    for _ in range(20):
        _event(conn, T1, "requeue")
    conn.commit()
    held = _newest_seq(registry)
    monkeypatch.setattr(exporter_mod, "EVENT_BATCH", 4)
    monkeypatch.setattr(exporter_mod, "MAX_BATCHES_PER_SCRAPE", 2)        # 8 rows a scrape
    exporter = _exporter(registry, lookback=1000)
    seen = []
    for _ in range(6):
        text = exporter.collect()
        assert _events(text) == QUIET, "a row from before the start was counted"
        seen.append(int(_samples(text)[-1].removeprefix("fleet_registry_last_event_seq ")))
    assert seen[0] == 8 and seen[1] == 16 and seen[-1] == held, seen
    _event(conn, T1, "requeue")                                           # appended after the start
    conn.commit()
    conn.close()
    assert 'fleet_events_total{event="requeue"} 1' in _events(exporter.collect())


def test_the_names_served_at_zero_are_names_the_coordinator_logs():
    """Inv. 4. A name nothing logs would sit at 0 for ever under a rule that can never fire."""
    logged = (ROOT / "fleet/dispatcher.py").read_text() + (ROOT / "fleet/registry_db.py").read_text()
    for name in exporter_mod.TROUBLE_EVENTS:
        # As an EVENT: the first argument of a log call, or the event of a state transition. Three
        # of the names are also states, and a state written in quotes is not an event logged.
        as_event = rf'(?:\blog\(\s*|\blog_event\(\s*[\w.]+,\s*|\btransition\(\s*[\w.]+,[^,\n]+,\s*"[a-z_]+",\s*)"{name}"'
        assert re.search(as_event, logged), name
    assert list(exporter_mod.TROUBLE_EVENTS) == sorted(set(exporter_mod.TROUBLE_EVENTS))


def test_a_value_that_is_not_a_plain_number_costs_only_itself(registry):
    """The trust boundary is the registry's rows, and the settings table is hand-editable."""
    conn = registry_db.connect(str(registry))
    odd = dict(READING_GPU, cores=10 ** 400, load1=True, gpu_temp_c="71", gpu_throttle=0x8000000000000004)
    _setting(conn, "box_reading_i-1", odd)
    _setting(conn, "box_reading_i-3", dict(READING_NO_GPU, gpu_throttle=-1))
    _setting(conn, "data_root_guard", {"holding": True})
    conn.execute("UPDATE instances SET label=? WHERE id=-5", ('a "quoted\\ name\nwith a line',))
    conn.execute("UPDATE tasks SET state='held_by_hand' WHERE name='d0'")
    conn.execute("UPDATE tasks SET state='Bad State!' WHERE name='r0'")
    conn.commit()
    conn.close()
    lines = _samples(_exporter(registry).collect())
    box1 = 'label="laptop-gpu",instance_id="-1"'
    assert not any(line.startswith((f"fleet_box_cores{{{box1}", f"fleet_box_load1{{{box1}",
                                    f"fleet_box_gpu_temperature_celsius{{{box1}")) for line in lines)
    assert f"fleet_box_gpu_power_watts{{{box1}}} 140" in lines, "the reading's other values are still served"
    # The mask is bits: the highest one set must not cost the lowest (a float would round it away).
    assert f'fleet_box_gpu_throttled{{{box1},reason="power_cap"}} 1' in lines
    assert not any("gpu_throttled" in line and "tower" in line for line in lines), "a negative mask is no mask"
    assert "fleet_data_root_holding 1" in lines
    assert 'fleet_box_info{label="a \\"quoted\\\\ name\\nwith a line",instance_id="-5",source="owned",state="live",gpu="yes"} 1' in lines
    # A state beyond the state machine is served by its name; one that cannot be a label, under `other`.
    assert 'fleet_tasks{state="held_by_hand"} 1' in lines and 'fleet_tasks{state="other"} 1' in lines
    assert 'fleet_tasks{state="done"} 0' in lines and 'fleet_tasks{state="running"} 2' in lines


def _with_reading(registry, **fields):
    conn = registry_db.connect(str(registry))
    _setting(conn, "box_reading_i-1", dict(READING_GPU, **fields))
    conn.commit()
    conn.close()
    return _samples(_exporter(registry).collect())


def _cores(lines):
    return [line for line in lines if line.startswith("fleet_box_cpu_core_busy_seconds_total")]


def test_a_cpu_or_network_count_that_is_not_one_costs_only_itself(registry):
    box1 = 'label="laptop-gpu",instance_id="-1"'
    lines = _with_reading(
        registry, host_cpu_busy_usec=-5, net_rx_bytes=-1, net_tx_bytes=float("nan"),
        host_cpu_core_busy_usec={
            "0": 1000000, "2": 2500000,
            "x": 5, "01": 5, "12345": 5, "-1": 5, "": 5, '0",evil="1': 5,     # not a core's number
            "3": -1, "4": "7", "5": True, "6": None, "7": 10 ** 400})        # not a count
    assert _cores(lines) == [
        f'fleet_box_cpu_core_busy_seconds_total{{{box1},core="0"}} 1',
        f'fleet_box_cpu_core_busy_seconds_total{{{box1},core="2"}} 2.5']
    assert not [line for line in lines if line.startswith(("fleet_box_cpu_busy_seconds_total", "fleet_box_network_"))]
    assert not [line for line in lines if "evil" in line]
    assert f"fleet_box_gpu_power_watts{{{box1}}} 140" in lines, "the reading's other values are still served"


@pytest.mark.parametrize("not_a_table", [["0", 5], "0: 5", 7, None, True])
def test_a_per_core_field_that_is_not_a_table_serves_no_core(registry, not_a_table):
    box1 = 'label="laptop-gpu",instance_id="-1"'
    lines = _with_reading(registry, host_cpu_core_busy_usec=not_a_table)
    assert _cores(lines) == []
    assert f"fleet_box_cpu_busy_seconds_total{{{box1}}} 1234.56" in lines, "the whole machine's time is its own field"
    assert f"fleet_box_network_receive_bytes_total{{{box1}}} 5000000" in lines


def test_at_most_1024_cores_a_box_are_served_the_lowest_numbers_first(registry):
    lines = _with_reading(registry, host_cpu_core_busy_usec={str(n): (n + 1) * 1000000 for n in range(1100)})
    served = _cores(lines)
    assert len(served) == exporter_mod.MAX_CORES_PER_BOX == 1024
    assert served[0].endswith('core="0"} 1') and served[-1].endswith('core="1023"} 1024')


def test_a_rented_boxs_cores_are_not_served_and_its_whole_machine_time_is(registry):
    """A rented machine's cores are its host's, shared with other renters, and every rental is a
    new box: hundreds of series nobody reads. Its whole-machine time is one series."""
    conn = registry_db.connect(str(registry))
    _box(conn, 40000002, "runq_rented", "live", source="vast")
    _setting(conn, "box_reading_i40000002", READING_GPU)
    conn.commit()
    conn.close()
    lines = _samples(_exporter(registry).collect())
    rented = 'label="runq_rented",instance_id="40000002"'
    assert f"fleet_box_cpu_busy_seconds_total{{{rented}}} 1234.56" in lines
    assert f"fleet_box_network_receive_bytes_total{{{rented}}} 5000000" in lines
    assert len(_cores(lines)) == 3 and not [line for line in _cores(lines) if "runq_rented" in line]


def test_a_reading_from_before_the_cpu_and_network_fields_serves_none_of_them(registry):
    """READING_NO_GPU has none of the four: an older dispatcher's row, or a box that could not say."""
    lines = _samples(_exporter(registry).collect())
    new = [line for line in lines if line.startswith(("fleet_box_cpu_busy", "fleet_box_cpu_core", "fleet_box_network"))]
    assert new and not [line for line in new if "tower" in line]


def test_a_scrape_changes_no_byte_of_the_registry(registry):
    """Inv. 1. The only thing that may appear is SQLite's own side files, and they hold nothing."""
    before = hashlib.sha256(registry.read_bytes()).hexdigest()
    exporter = _exporter(registry)
    for _ in range(3):
        exporter.collect()
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == before
    beside = {p.name: p for p in registry.parent.iterdir()}
    assert set(beside) <= {"runs.sqlite", "runs.sqlite-wal", "runs.sqlite-shm"}
    assert "runs.sqlite-wal" not in beside or beside["runs.sqlite-wal"].stat().st_size == 0


def _once(path):
    done = subprocess.run([sys.executable, str(ROOT / "fleet/metrics_exporter.py"), "--db", str(path), "--once"],
                          capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout


def _read_only(directory, on):
    for entry in directory.iterdir():
        entry.chmod(0o444 if on else 0o644)
    directory.chmod(0o555 if on else 0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write beside a read-only registry")
def test_a_read_only_view_reads_a_registry_its_writer_holds_open(registry):
    """How it is run beside a live dispatcher: it cannot write beside the registry, the dispatcher
    holds it open, and rows written after the view was taken are still read. With NO writer the
    same view is an error, never an answer from an old file."""
    writer = registry_db.connect(str(registry))
    try:
        _event(writer, T1, "hold")
        writer.commit()
        _read_only(registry.parent, True)
        rc, text = _once(registry)
        assert rc == 0 and f"fleet_registry_last_event_seq {_newest_seq(registry)}" in text
        _read_only(registry.parent, False)
        _event(writer, T1, "hold")
        writer.commit()
        newest = _newest_seq(registry)
        _read_only(registry.parent, True)
        rc, text = _once(registry)
        assert rc == 0 and f"fleet_registry_last_event_seq {newest}" in text
    finally:
        _read_only(registry.parent, False)
        writer.close()
    assert not (registry.parent / "runs.sqlite-wal").exists(), "the writer's close did not fold its log in"
    _read_only(registry.parent, True)
    try:
        rc, text = _once(registry)
    finally:
        _read_only(registry.parent, False)
    assert rc == 1 and text == ""


_WRITER = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("registry_db", sys.argv[1])
registry_db = importlib.util.module_from_spec(spec); sys.modules["registry_db"] = registry_db
spec.loader.exec_module(registry_db)
conn = registry_db.connect(sys.argv[2])
conn.execute("INSERT INTO events(t, event, detail) VALUES ('2027-01-15T07:59:30Z', 'hold', '')")
conn.commit()
print("written", flush=True)
time.sleep(600)
"""


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write beside a read-only registry")
def test_after_its_writer_is_KILLED_a_read_only_view_still_answers(registry):
    """"Where it runs": a dispatcher has no shutdown path, so it is killed, and SQLite's side files
    stay. The exporter then goes on answering with the last numbers written, the killed writer's
    last row included. So "the exporter answers" never means "the dispatcher runs": that is read
    from the newest poll cycle's age."""
    before = _newest_seq(registry)
    writer = subprocess.Popen([sys.executable, "-c", _WRITER, str(ROOT / "fleet/registry_db.py"), str(registry)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert writer.stdout.readline().strip() == "written"
    finally:
        writer.send_signal(signal.SIGKILL)
        writer.wait(timeout=30)
    assert (registry.parent / "runs.sqlite-wal").exists(), "a killed writer leaves its log"
    _read_only(registry.parent, True)
    try:
        rc, text = _once(registry)
    finally:
        _read_only(registry.parent, False)
    assert rc == 0 and f"fleet_registry_last_event_seq {before + 1}" in text


def test_a_registry_that_cannot_be_read_is_an_error_never_an_empty_answer(tmp_path):
    """Inv. 1: and it never creates one. The second path is in a directory that exists and can be
    written, where an ordinary open would leave an empty file behind."""
    for missing in (tmp_path / "nowhere" / "runs.sqlite", tmp_path / "runs.sqlite"):
        with pytest.raises((sqlite3.Error, OSError)):
            _exporter(missing).collect()
        assert exporter_mod.main(["--db", str(missing), "--once"]) == 1
        assert not missing.exists(), "the exporter created the file"
    assert list(tmp_path.iterdir()) == []


def test_the_connection_a_scrape_uses_cannot_write(registry):
    """Inv. 1, by trying."""
    conn = _exporter(registry)._open()
    try:
        for write in ("INSERT INTO settings(key, value) VALUES ('x', '1')", "CREATE TABLE made_here(a)",
                      "DELETE FROM events"):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(write)
    finally:
        conn.close()


def test_a_registry_replaced_by_another_is_a_first_look_again(registry, tmp_path):
    """Inv. 4: the newest row is below the exporter's place, so this is not the registry it was
    following. The rows the new one holds were not appended since the start, and the counts
    already made are kept."""
    exporter = _exporter(registry)
    exporter.collect()
    conn = registry_db.connect(str(registry))
    for _ in range(30):
        _event(conn, T1, "requeue")
    conn.commit()
    conn.close()
    assert 'fleet_events_total{event="requeue"} 30' in _events(exporter.collect())

    other = tmp_path / "other" / "runs.sqlite"
    other.parent.mkdir()
    conn = registry_db.connect(str(other))
    for _ in range(5):
        _event(conn, T1, "requeue")
    _event(conn, T1, "poll_cycle", {"total_sec": 4.0, "phases": {}})
    conn.commit()
    exporter.db = str(other)
    text = exporter.collect()
    assert 'fleet_events_total{event="requeue"} 30' in _events(text), "rows it already held were counted"
    assert "fleet_poll_cycle_seconds 4" in _samples(text) and "fleet_registry_last_event_seq 6" in _samples(text)
    _event(conn, T1, "requeue")
    conn.commit()
    conn.close()
    assert 'fleet_events_total{event="requeue"} 31' in _events(exporter.collect())


def test_the_states_served_are_the_state_machines(registry):
    assert set(exporter_mod.TASK_STATES) == set(registry_db.LEGAL_STATES)
    assert len(exporter_mod.TASK_STATES) == len(set(exporter_mod.TASK_STATES))


def test_it_does_not_load_the_dispatcher():
    source = (ROOT / "fleet/metrics_exporter.py").read_text()
    assert "import dispatcher" not in source and "from dispatcher" not in source


def test_the_route(registry, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), exporter_mod.make_handler(_exporter(registry)))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/metrics", timeout=10) as answer:
            assert answer.status == 200
            assert answer.headers["Content-Type"].startswith("text/plain; version=0.0.4")
            body = answer.read()
            assert "fleet_tasks" in body.decode()
        head = urllib.request.Request(f"{base}/metrics", method="HEAD")
        with urllib.request.urlopen(head, timeout=10) as answer:
            assert answer.status == 200 and int(answer.headers["Content-Length"]) == len(body)
            assert answer.read() == b""
        for path in ("/", "/metrics/", "/runs.sqlite"):
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(base + path, timeout=10)
            assert refused.value.code == 404
    finally:
        server.shutdown()

    gone = ThreadingHTTPServer(("127.0.0.1", 0), exporter_mod.make_handler(_exporter(tmp_path / "gone.sqlite")))
    Thread(target=gone.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(f"http://127.0.0.1:{gone.server_address[1]}/metrics", timeout=10)
        assert failed.value.code == 500 and b"fleet_" not in failed.value.read()
    finally:
        gone.shutdown()
    assert not (tmp_path / "gone.sqlite").exists(), "a scrape of a missing registry made one"


def test_an_error_nobody_foresaw_is_still_an_answer_of_down(registry):
    """A connection closed with no answer is harder to tell from a network fault than a 500."""
    exporter = _exporter(registry)
    exporter.collect = lambda: (_ for _ in ()).throw(RuntimeError("unforeseen"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), exporter_mod.make_handler(exporter))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/metrics", timeout=10)
        assert failed.value.code == 500 and b"fleet_" not in failed.value.read()
    finally:
        server.shutdown()


def test_a_registry_whose_path_holds_uri_syntax_is_still_opened(tmp_path):
    odd = tmp_path / "a dir with ? and # and %41"
    odd.mkdir()
    conn = registry_db.connect(str(odd / "runs.sqlite"))
    conn.close()
    assert "fleet_tasks" in _exporter(odd / "runs.sqlite").collect()
