"""Box readings — `docs/specs/task-dispatcher.spec.md` invariant 31.

Each box's newest reading is kept in one `settings` row a status system can read, the probe reads
heat, power, pressure and disk, and a faster reading-only probe can be switched on. The other half
of every test here is what must NOT change: the `box_measured` event, `_box_res`, the connection
tracker, and what is sent to a box that is not due.
"""

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")

BEFORE_31 = ("NPROC 20\n"
             "LOAD 2.50 0.20 0.10 1/900 123\n"
             "MEM 32000 24000\n"
             "GPU 8192, 512, 40, 10, A GPU\n"
             "CPUQ 10.0000\nCPUU 1000000000\nMEMCG 2147483648 17179869184\nMEMANON 1073741824\n")
NEW_LINES = ("GPUX 71, 140.25, 175.00, 0x0000000000000024\n"
             "PSI cpu some 2500000\nPSI cpu full 0\n"
             "PSI memory some 1000000\nPSI memory full 500000\n"
             "PSI io some 250000\nPSI io full 125000\n"
             "DISK 1055916032 262668288\n")
PROBE = BEFORE_31 + NEW_LINES
NEW_KEYS = ("gpu_temp_c", "gpu_power_w", "gpu_power_limit_w", "gpu_throttle", "psi",
            "disk_total_gb", "disk_free_gb")


# ---------------------------------------------------------------------------------------------
# 31b: the probe and its parser
# ---------------------------------------------------------------------------------------------
def test_the_three_new_lines_parse():
    m = disp.parse_box_probe(PROBE)
    assert (m["gpu_temp_c"], m["gpu_power_w"], m["gpu_power_limit_w"]) == (71.0, 140.25, 175.0)
    assert m["gpu_throttle"] == 0x24
    assert m["psi"] == {"cpu_some": 2500000, "mem_some": 1000000, "mem_full": 500000,
                        "io_some": 250000, "io_full": 125000}, "and no cpu_full: a machine has none"
    assert round(m["disk_total_gb"], 1) == 1007.0 and round(m["disk_free_gb"], 1) == 250.5


def test_a_probe_without_them_parses_exactly_as_before():
    old, new = disp.parse_box_probe(BEFORE_31), disp.parse_box_probe(PROBE)
    assert all(old[key] is None for key in NEW_KEYS)
    assert {k: v for k, v in new.items() if k not in NEW_KEYS} == \
           {k: v for k, v in old.items() if k not in NEW_KEYS}


@pytest.mark.parametrize("line,expect", [
    ("GPUX [N/A], [N/A], [N/A], [N/A]", (None, None, None, None)),
    ("GPUX Unable to determine the device handle for GPU0: Unknown Error", (None, None, None, None)),
    ("GPUX 71, [N/A], 175.00, [Not Supported]", (71.0, None, 175.0, None)),
    ("GPUX 71, 140, 175", (71.0, 140.0, 175.0, None)),                  # a driver with no mask field
    ("GPUX 71, 140, 175, 0x4\nGPUX 99, 300, 300, 0x80", (71.0, 140.0, 175.0, 4)),   # first card
    ("GPUX prose first\nGPUX 55, 80, 175, 0x0", (55.0, 80.0, 175.0, 0)),
    ("GPUX nan, -5, inf, 12", (None, None, None, None)),                # not readings, not a mask
])
def test_what_a_driver_prints_instead_of_a_reading_costs_only_that_field(line, expect):
    m = disp.parse_box_probe(BEFORE_31 + line + "\n")
    assert (m["gpu_temp_c"], m["gpu_power_w"], m["gpu_power_limit_w"], m["gpu_throttle"]) == expect
    assert m["vram_total_gb"] == 8.0 and m["cores"] == 20, "the rest of the probe is untouched"


@pytest.mark.parametrize("lines,psi,disk", [
    ("PSI cpu some nan\nPSI swap some 5\nPSI io maybe 5\nPSI io some\n", None, (None, None)),
    ("PSI io some 7\nPSI io some 9\n", {"io_some": 7}, (None, None)),
    ("DISK 0 0\n", None, (None, None)),
    ("DISK lots\n", None, (None, None)),
    ("DISK 1048576\n", None, (1.0, None)),
    ("DISK 2097152 1048576 extra\n", None, (2.0, 1.0)),
])
def test_pressure_and_disk_lines_that_are_not_readings(lines, psi, disk):
    m = disp.parse_box_probe(BEFORE_31 + lines)
    assert m["psi"] == psi and (m["disk_total_gb"], m["disk_free_gb"]) == disk


def test_the_line_that_admits_work_is_sent_as_it_was():
    """31b: the `GPU` line feeds the VRAM gate and the GPU-loss alert; nothing was added to it."""
    assert ("nvidia-smi --query-gpu=memory.total,memory.used,utilization.gpu,utilization.memory,name "
            "--format=csv,noheader,nounits 2>/dev/null | awk '{print \"GPU\", $0}'; ") in disp.BOX_PROBE_CMD
    for mask in ("clocks_event_reasons.active", "clocks_throttle_reasons.active"):
        assert disp.BOX_PROBE_CMD.count(mask) == 1
    assert disp.BOX_PROBE_CMD.rstrip().endswith("fi"), "the last arm still ends in a fallback"


# The probe is a Linux box's command: it reads /proc and uses the tools every box has.
_A_BOXS_SHELL = pytest.mark.skipif(
    sys.platform != "linux" or not all(shutil.which(tool) for tool in ("sh", "nproc", "free", "df", "awk", "timeout")),
    reason="needs Linux and the tools a box has")


@_A_BOXS_SHELL
def test_the_probe_runs_in_a_plain_shell_and_its_output_parses():
    """The command is one long string a box's shell runs. Run it here: a quoting slip is a probe
    that fails on every box at once, which no parsed-fixture test can see."""
    done = subprocess.run(["sh", "-c", disp.BOX_PROBE_CMD], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    m = disp.parse_box_probe(done.stdout)
    assert m is not None and m["cores"] >= 1
    assert m["disk_total_gb"] and m["disk_free_gb"] is not None, done.stdout
    assert [ln for ln in done.stdout.splitlines() if ln.startswith("DISK")][0].count(" ") == 2
    for ln in done.stdout.splitlines():
        if ln.startswith("PSI"):
            name, resource, kind, total = ln.split()
            assert resource in ("cpu", "memory", "io") and kind in ("some", "full") and total.isdigit()


_FAKE_DRIVER_TOOL = """#!/bin/sh
echo "$*" >> "{log}"
{first}
for field in none {refused}; do
  case "$*" in *"$field"*) echo "Field \\"$field\\" is not a valid field to query."; echo; exit 2 ;; esac
done
case "$*" in
  *memory.total*) echo "8192, 512, 40, 10, A GPU" ;;
  *clocks_*) echo "71, 140.25, 175.00, 0x0000000000000024" ;;
  *) echo "71, 140.25, 175.00" ;;
esac
"""
_OFF_THE_BUS = 'echo "Unable to determine the device handle for GPU0: Unknown Error"; exit 255'
_HANGS = 'case "$*" in *temperature.gpu*) exec sleep 60 ;; esac'
_HANGS_DEAF = 'case "$*" in *temperature.gpu*) trap "" TERM; exec sleep 60 ;; esac'   # ignores the stop signal
_BOTH_NAMES = "clocks_event_reasons.active clocks_throttle_reasons.active"


@_A_BOXS_SHELL
@pytest.mark.parametrize("first,refused,asked,line", [
    ("", "", 1, "GPUX 71, 140.25, 175.00, 0x0000000000000024"),
    ("", "clocks_event_reasons.active", 2, "GPUX 71, 140.25, 175.00, 0x0000000000000024"),
    ("", _BOTH_NAMES, 3, "GPUX 71, 140.25, 175.00"),
    (_OFF_THE_BUS, _BOTH_NAMES, 1, None),
    (_HANGS, _BOTH_NAMES, 1, None),
    (_HANGS_DEAF, _BOTH_NAMES, 1, None),
])
def test_an_older_name_is_asked_only_of_a_driver_that_refused_the_field(tmp_path, first, refused, asked, line):
    """31b. The next form of the query is for a driver that does not KNOW the field. A driver that
    is failing, or hanging, is asked once: three more calls to it could take the whole probe past
    its limit, and the probe is what admission reads. A driver that ignores being asked to stop
    is killed 2 s later; without that the probe would wait on it for as long as it liked."""
    log = tmp_path / "asked"
    tool = tmp_path / "bin" / "nvidia-smi"
    tool.parent.mkdir()
    tool.write_text(_FAKE_DRIVER_TOOL.format(log=log, first=first, refused=refused))
    tool.chmod(0o755)
    began = time.time()
    done = subprocess.run(["sh", "-c", disp.BOX_PROBE_CMD], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, PATH=f"{tool.parent}{os.pathsep}{os.environ['PATH']}"))
    assert done.returncode == 0, done.stderr
    assert time.time() - began < 15, "a hanging driver costs its one bounded call"
    assert len([ln for ln in log.read_text().splitlines() if "temperature.gpu" in ln]) == asked
    assert [ln for ln in done.stdout.splitlines() if ln.startswith("GPUX")] == ([line] if line else [])
    m = disp.parse_box_probe(done.stdout)
    assert m["cores"] >= 1 and m["disk_total_gb"], "the rest of the probe answered"
    assert m["gpu_temp_c"] == (71.0 if line else None)
    assert m["gpu_throttle"] == (0x24 if line and line.count(",") == 3 else None)


# ---------------------------------------------------------------------------------------------
# 31a, c, d, e: the row
# ---------------------------------------------------------------------------------------------
class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Run:
    """A subprocess-shaped fake: every ssh that carries the probe gets `doc` back."""

    def __init__(self, doc=PROBE, rc=0):
        self.doc, self.rc, self.calls, self.boom = doc, rc, [], False

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.boom:
            raise OSError("no ssh here")
        return _Proc(self.rc, self.doc if "NPROC" in cmd[-1] else "")

    def sent(self):
        return [cmd[-1] for cmd, _ in self.calls]


def _mk(tmp_path, run=None, source="owned", iid=-1):
    run = run or _Run()
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_Run(rc=1))
    now = disp.registry_db.now_iso()
    d.conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
        "slots_total, hard_cap_at, source, gpu_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (iid, "laptop-gpu", now, "live", 0.0, "192.168.0.9", 22, 6, now, source, "A GPU"))
    d.conn.commit()
    return d, run


def _row(d, iid=-1):
    row = d.conn.execute("SELECT value FROM settings WHERE key=?", (f"box_reading_i{iid}",)).fetchone()
    return None if row is None else json.loads(row["value"])


def _set_row(d, value, iid=-1):
    d.conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                   "value=excluded.value", (f"box_reading_i{iid}", json.dumps(value)))
    d.conn.commit()


def _fast(d, minutes=1):
    d.conn.execute("UPDATE settings SET value=? WHERE key='box_reading_every_min'", (json.dumps(minutes),))
    d.conn.commit()


def _events(d):
    return [tuple(r) for r in d.conn.execute("SELECT event, detail FROM events ORDER BY seq")]


def test_a_full_probe_writes_the_row(tmp_path):
    d, _ = _mk(tmp_path)
    before = time.time()
    d._measure_box_resources()
    row = _row(d)
    assert before - 1 <= row["at"] <= time.time() + 1
    assert (row["cores"], row["load1"], row["vram_total_gb"], row["gpu_util"]) == (20, 2.5, 8.0, 40.0)
    assert (row["gpu_temp_c"], row["gpu_power_w"], row["gpu_power_limit_w"]) == (71.0, 140.2, 175.0)
    assert row["gpu_throttle"] == 0x24 and row["psi"]["mem_full"] == 500000
    assert (row["disk_total_gb"], row["disk_free_gb"]) == (1007.0, 250.5)
    assert (row["slots_total"], row["running"], row["open"]) == (6, 0, 0)
    assert row["cpu_usage_usec"] == 1000000000.0 and row["cpu_used_cores"] is None, "no row before it"
    assert "ep" not in row and "grp" not in row, "the row is numbers about a box, not somebody's work"


def test_the_event_a_probe_logs_is_the_one_it_logged_before(tmp_path):
    """31c: byte for byte. The same box answers with and without the new lines."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_lines, _ = _mk(tmp_path / "a", _Run(PROBE))
    without, _ = _mk(tmp_path / "b", _Run(BEFORE_31))
    with_lines._measure_box_resources()
    without._measure_box_resources()
    (new,) = [detail for event, detail in _events(with_lines) if event == "box_measured"]
    (old,) = [detail for event, detail in _events(without) if event == "box_measured"]
    assert new == old
    assert set(json.loads(new.partition("| ")[2])) == {
        "v", "cores", "load1", "ram_total_gb", "ram_avail_gb", "cpu_quota_cores", "cpu_used_cores",
        "cpu_usage_usec", "mem_limit_gb", "mem_used_gb", "mem_anon_gb", "vram_total_gb",
        "vram_used_gb", "gpu_util", "gpu_mem_util", "gpu_name", "gpu_count", "slots_total",
        "slots_eff", "running", "open", "ep", "grp"}


# What the commit before invariant 31 logged for BEFORE_31 on this box (its code was run to get it).
GOLDEN_BOX_MEASURED = (
    'load 2.50/20 cores, ram 23.4GB free, vram 0.5/8.0GB util 40% | {"v":1,"cores":20,"load1":2.5,'
    '"ram_total_gb":31.2,"ram_avail_gb":23.4,"cpu_quota_cores":10.0,"cpu_used_cores":null,'
    '"cpu_usage_usec":1000000000.0,"mem_limit_gb":16.0,"mem_used_gb":2.0,"mem_anon_gb":1.0,'
    '"vram_total_gb":8.0,"vram_used_gb":0.5,"gpu_util":40.0,"gpu_mem_util":10.0,"gpu_name":"A GPU",'
    '"gpu_count":1,"slots_total":6,"slots_eff":6,"running":0,"open":0,"ep":null,"grp":null}')


def test_the_event_is_the_string_the_commit_before_this_one_logged(tmp_path):
    """31c against a fixed string: two runs of today's code agreeing with each other would both
    move if the payload moved."""
    for name, doc in (("with", PROBE), ("without", BEFORE_31)):
        (tmp_path / name).mkdir()
        d, _ = _mk(tmp_path / name, _Run(doc))
        d._measure_box_resources()
        assert [detail for event, detail in _events(d) if event == "box_measured"] == [GOLDEN_BOX_MEASURED]


def test_a_row_that_cannot_be_written_costs_the_row_and_nothing_else(tmp_path):
    """31a: the row is written after the probe's own work, and a write that fails (a full disk, a
    table somebody changed) neither undoes that work nor raises into the poll."""
    d, _ = _mk(tmp_path)
    d.conn.execute("CREATE TRIGGER no_reading BEFORE INSERT ON settings "
                   "WHEN NEW.key GLOB 'box_reading_i*' BEGIN SELECT RAISE(ABORT, 'no room'); END")
    d.conn.commit()
    checked = []
    d._check_gpu_alert = lambda inst: checked.append(inst["id"])
    d._measure_box_resources()
    assert _row(d) is None
    assert len([event for event, _ in _events(d) if event == "box_measured"]) == 1
    assert d._box_res[-1]["cores"] == 20 and checked == [-1], "admission's sample and the GPU-loss check"
    d.conn.execute("DROP TRIGGER no_reading")
    d.conn.commit()
    d._last_res_measure[-1] -= 3600
    d._measure_box_resources()
    assert _row(d)["cores"] == 20, "and the next probe writes it"


def test_the_on_demand_probe_reports_the_new_fields_and_writes_no_row(tmp_path):
    """31c: `runq box probe` answers with the parsed probe as it is, so its event gained the new
    fields. It is a diagnosis, not a reading: no row."""
    d, _ = _mk(tmp_path)
    d._sync_ssh_config = lambda: None
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('probe_request_i-1', '1')")
    d.conn.commit()
    d._consume_box_probes()
    (detail,) = [json.loads(detail) for event, detail in _events(d) if event == "box_probe"]
    assert detail["reachable"] and detail["measured"]["gpu_temp_c"] == 71.0
    assert detail["measured"]["psi"]["io_full"] == 125000 and detail["measured"]["disk_free_gb"]
    assert _row(d) is None


def test_the_on_demand_probes_event_leaves_the_per_core_table_out(tmp_path):
    """31f. An event row is kept for ever and a person reads it: one count a core tells them
    nothing. The machine's time and the network bytes are there."""
    d, _ = _mk(tmp_path, _Run(PROBE_31F))
    d._sync_ssh_config = lambda: None
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('probe_request_i-1', '1')")
    d.conn.commit()
    d._consume_box_probes()
    (detail,) = [json.loads(detail) for event, detail in _events(d) if event == "box_probe"]
    measured = detail["measured"]
    assert "host_cpu_core_busy_usec" not in measured
    assert (measured["host_cpu_busy_usec"], measured["net_rx_bytes"], measured["net_tx_bytes"]) == \
           (1234560000, 5000000, 7000000)
    assert measured["cores"] == 20 and measured["gpu_temp_c"] == 71.0, "and nothing else is left out"


@pytest.mark.parametrize("ago_s,rate", [(100, 5.0), (890, 5.0), (1000, None), (-50, None)])
def test_the_rows_cpu_rate_is_against_the_row_it_replaces(tmp_path, ago_s, rate):
    """31a: and a row older than 15 minutes (or from the future) is no predecessor."""
    d, _ = _mk(tmp_path)
    _set_row(d, {"at": time.time() - ago_s, "cpu_usage_usec": 1000000000 - 5.0 * ago_s * 1e6})
    d._measure_box_resources()
    got = _row(d)["cpu_used_cores"]
    assert got is None if rate is None else abs(got - rate) < 0.2, got


@pytest.mark.parametrize("every_min,limit", [
    (5, 900.0), (1, 900.0), (20, 3600.0), ("often", 900.0), (None, 900.0), (-3, 900.0), (0, 900.0),
    (float("inf"), 900.0), (float("nan"), 900.0), (10 ** 400, 900.0),
])
def test_the_rate_limit_is_three_full_probe_periods_and_never_under_15_minutes(tmp_path, every_min, limit):
    d, _ = _mk(tmp_path)
    d.settings["resource_measure_every_min"] = every_min
    assert d._reading_rate_max_gap_s() == limit


@pytest.mark.parametrize("ago_s,rate", [(1300, 5.0), (3500, 5.0), (3700, None)])
def test_a_slower_full_probe_still_gets_a_rate(tmp_path, ago_s, rate):
    """31a: with the full probe every 20 minutes and no faster reading, a fixed 15 minutes would
    leave every row without a rate."""
    d, _ = _mk(tmp_path)
    d.settings["resource_measure_every_min"] = 20
    _set_row(d, {"at": time.time() - ago_s, "cpu_usage_usec": 1000000000 - 5.0 * ago_s * 1e6})
    d._measure_box_resources()
    got = _row(d)["cpu_used_cores"]
    assert got is None if rate is None else abs(got - rate) < 0.2, got


def test_a_probe_reads_the_boxs_slots_and_tasks_ONCE(tmp_path):
    """31d: the row holds the box's open slots, and reading those can log (a schedule that does
    not parse logs an event each time). A full probe reads them once, for its event and its row,
    as it did before there was a row; a reading-only probe reads them once."""
    d, _ = _mk(tmp_path)
    built = []
    payload = d._box_perf_payload
    d._box_perf_payload = lambda inst, m: built.append(1) or payload(inst, m)
    d._measure_box_resources()
    assert len(built) == 1
    (logged,) = [detail for event, detail in _events(d) if event == "box_measured"]
    assert logged.endswith(GOLDEN_BOX_MEASURED.partition("| ")[2]) and _row(d)["slots_eff"] == 6
    _fast(d)
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(built) == 2


def test_the_rows_time_is_when_the_box_ANSWERED(tmp_path, monkeypatch):
    """31a: not when the pass began. A pass waits on each box in turn, and a reading stamped at
    the start would be shown older than it is by every wait before it."""
    d, run = _mk(tmp_path)
    clock = [time.time()]
    monkeypatch.setattr(disp.time, "time", lambda: clock[0])

    def slow(cmd, **kwargs):
        clock[0] += 7.0
        return run(cmd, **kwargs)

    d.run = slow
    began = clock[0]
    d._measure_box_resources()
    assert _row(d)["at"] == round(began + 7.0, 1)
    _fast(d)
    clock[0] += 120.0
    d._measure_box_resources()
    assert _row(d)["at"] == round(began + 7.0 + 120.0 + 7.0, 1), "and so is a reading-only probe's"


@pytest.mark.parametrize("old_row", ["not json", "[1, 2]", '{"at": "then"}', '{"cpu_usage_usec": 5}'])
def test_a_row_somebody_edited_is_replaced_and_gives_no_rate(tmp_path, old_row):
    d, _ = _mk(tmp_path)
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('box_reading_i-1', ?)", (old_row,))
    d.conn.commit()
    d._measure_box_resources()
    assert _row(d)["cores"] == 20 and _row(d)["cpu_used_cores"] is None


@pytest.mark.parametrize("setting", [0, None, "soon", True, -1, float("inf"), 10 ** 400, [1]])
def test_between_full_probes_nothing_is_sent_unless_the_faster_reading_is_on(tmp_path, setting):
    d, run = _mk(tmp_path)
    assert disp.DEFAULT_SETTINGS["box_reading_every_min"] == 0
    d._measure_box_resources()
    d.conn.execute("UPDATE settings SET value=? WHERE key='box_reading_every_min'",
                   (json.dumps(setting) if setting != float("inf") else "Infinity",))
    d.conn.commit()
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 1


def test_the_faster_reading_writes_the_row_and_does_nothing_else(tmp_path):
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    d._measure_box_resources()
    assert len(run.calls) == 1, "not due yet: the full probe counted as a reading"

    first, events, held = _row(d), _events(d), dict(d._box_res[-1])
    fails, last_full = dict(d.tracker._fails), dict(d._last_res_measure)
    run.doc = PROBE.replace("CPUU 1000000000", "CPUU 1300000000").replace("GPUX 71,", "GPUX 80,")
    _set_row(d, {**first, "at": first["at"] - 60})
    d._last_reading[-1] -= 61
    d._measure_box_resources()

    assert len(run.calls) == 2
    cmd, kwargs = run.calls[-1]
    assert cmd[-1] == disp.BOX_PROBE_CMD, "the probe alone: no freeze assert, no launch pacing"
    assert kwargs.get("timeout") == disp.BOX_READING_PROBE_TIMEOUT_S == 10
    row = _row(d)
    assert row["gpu_temp_c"] == 80.0 and row["at"] > first["at"] - 60, "a new row, with its own time"
    assert abs(row["cpu_used_cores"] - 5.0) < 0.2, "300 s of CPU over the 60 s since the last ROW"
    assert _events(d) == events, "no event"
    assert d._box_res[-1] == held, "admission's sample is not replaced"
    assert d.tracker._fails == fails and d._last_res_measure == last_full

    d._measure_box_resources()
    assert len(run.calls) == 2, "and not again until it is due"


def test_the_reading_probe_goes_to_the_address_on_the_boxs_own_row(tmp_path, monkeypatch):
    """31d: the endpoint lookup is not asked. For a box that has ever failed three calls in a row
    it makes a call of its own first, outside the reading's 10 s."""
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    looked_up = len(d.vastai_run.calls)
    monkeypatch.setattr(disp, "endpoint_for", lambda *a, **k: pytest.fail("the endpoint lookup was asked"))
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 2 and run.calls[-1][0][:-1] == disp.ssh_cmd("192.168.0.9", 22)
    assert len(d.vastai_run.calls) == looked_up

    d.conn.execute("UPDATE instances SET ssh_host=NULL WHERE id=-1")      # and a row with no address
    d.conn.commit()
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 2, "is not asked at all"


def test_a_reading_only_probe_is_never_the_gpu_loss_checks_sample(tmp_path):
    """31d: the alert reads `box_measured` events and a reading-only probe writes none, so a GPU
    missing from the fast readings raises nothing. The box's next full probe is what the alert reads."""
    d, run = _mk(tmp_path)
    checked, real = [], d._check_gpu_alert
    d._check_gpu_alert = lambda inst: (checked.append(inst["id"]), real(inst))[1]
    d._measure_box_resources()
    _fast(d)
    events = _events(d)
    run.doc = BEFORE_31.replace("GPU 8192, 512, 40, 10, A GPU\n", "")
    for _ in range(3):
        d._last_reading[-1] -= 3600
        d._measure_box_resources()
    assert len(run.calls) == 4 and checked == [-1] and _events(d) == events
    assert _row(d)["vram_total_gb"] is None and _row(d)["gpu_temp_c"] is None, "the row does say so"


def test_a_reading_that_did_not_come_is_not_asked_for_again_until_the_next_full_probe(tmp_path):
    """31d: each reading that does not come costs up to its limit inside the measure phase."""
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    run.rc = 255
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 2
    run.rc = 0
    for _ in range(3):
        d._last_reading[-1] -= 3600                                      # an hour passes each time
        d._measure_box_resources()
    assert len(run.calls) == 2, "the box answers again, and is still not asked"
    d._last_res_measure[-1] -= 3600                                      # its full probe comes due
    d._measure_box_resources()
    assert len(run.calls) == 3 and run.calls[-1][0][-1] != disp.BOX_PROBE_CMD
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 4 and run.calls[-1][0][-1] == disp.BOX_PROBE_CMD, "and the readings resume"


def test_a_rented_box_gets_no_faster_reading(tmp_path):
    d, run = _mk(tmp_path, source="vast", iid=40000001)
    d._measure_box_resources()
    _fast(d)
    d._last_reading[40000001] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 1 and _row(d, 40000001)["cores"] == 20, "it keeps the full probe's row"


def test_a_box_whose_calls_are_failing_is_skipped(tmp_path):
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    d.tracker.record(-1, False)
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 1


@pytest.mark.parametrize("rc,doc", [(255, PROBE), (0, "the box said something else\n"), (0, "")])
def test_a_reading_that_fails_leaves_the_old_row_and_counts_against_nothing(tmp_path, rc, doc):
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    old, events = _row(d), _events(d)
    run.rc, run.doc = rc, doc
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 2 and _row(d) == old and _events(d) == events
    assert d.tracker.consecutive_fails(-1) == 0


def test_an_ssh_that_raises_does_not_raise_out_of_the_pass(tmp_path):
    d, run = _mk(tmp_path)
    d._measure_box_resources()
    _fast(d)
    old = _row(d)
    run.boom = True
    d._last_reading[-1] -= 3600
    d._measure_box_resources()
    assert len(run.calls) == 2 and _row(d) == old


def test_a_box_that_leaves_takes_its_row_with_it(tmp_path):
    d, _ = _mk(tmp_path)
    d._measure_box_resources()
    _set_row(d, {"at": 1.0}, iid=77)                        # a box that is not in the registry
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('box_reading_note', '\"kept\"')")
    d.conn.commit()
    d._measure_box_resources()
    assert _row(d) is not None and _row(d, 77) is None
    d.conn.execute("UPDATE instances SET state='destroyed' WHERE id=-1")
    d.conn.commit()
    d._measure_box_resources()
    assert _row(d) is None
    keys = {r["key"] for r in d.conn.execute("SELECT key FROM settings WHERE key LIKE 'box_reading%'")}
    assert keys == {"box_reading_every_min", "box_reading_note"}, "only the reading rows are swept"


def test_the_exporter_reads_what_the_dispatcher_writes(tmp_path):
    """The two halves agree on the row: a probe here, the metrics text there."""
    exporter = _load("metrics_exporter", "fleet/metrics_exporter.py")
    d, _ = _mk(tmp_path)
    d._measure_box_resources()
    text = exporter.Exporter(str(tmp_path / "runs.sqlite")).collect()
    box = 'label="laptop-gpu",instance_id="-1"'
    for line in (f"fleet_box_gpu_temperature_celsius{{{box}}} 71",
                 f"fleet_box_gpu_power_limit_watts{{{box}}} 175",
                 f'fleet_box_gpu_throttled{{{box},reason="power_cap"}} 1',
                 f'fleet_box_gpu_throttled{{{box},reason="thermal"}} 1',
                 f'fleet_box_gpu_throttled{{{box},reason="power_brake"}} 0',
                 f'fleet_box_pressure_stalled_seconds_total{{{box},resource="io"}} 0.125',
                 f"fleet_box_cores{{{box}}} 20"):
        assert line in text.splitlines(), line


# ---------------------------------------------------------------------------------------------
# 31f: the machine's CPU time and the worker's network bytes
# ---------------------------------------------------------------------------------------------
LINES_31F = ("CLKTCK 100\n"
             "CPUS cpu 123456\nCPUS cpu0 60000\nCPUS cpu1 63456\n"
             "NET 5000000 7000000\n")
PROBE_31F = PROBE + LINES_31F
KEYS_31F = ("host_cpu_busy_usec", "host_cpu_core_busy_usec", "net_rx_bytes", "net_tx_bytes")


def _cpu_net(m):
    return tuple(m[key] for key in KEYS_31F)


def test_cpu_time_and_network_bytes_parse():
    m = disp.parse_box_probe(PROBE_31F)
    assert _cpu_net(m) == (1234560000, {"0": 600000000, "1": 634560000}, 5000000, 7000000)
    assert all(type(v) is int for v in (m["host_cpu_busy_usec"], m["net_rx_bytes"], m["net_tx_bytes"]))
    assert all(type(v) is int for v in m["host_cpu_core_busy_usec"].values()), "counts, never floats"


def test_a_probe_without_the_31f_lines_parses_exactly_as_before():
    old, new = disp.parse_box_probe(PROBE), disp.parse_box_probe(PROBE_31F)
    assert _cpu_net(old) == (None, None, None, None)
    assert {k: v for k, v in new.items() if k not in KEYS_31F} == \
           {k: v for k, v in old.items() if k not in KEYS_31F}


@pytest.mark.parametrize("tick_line", [
    "", "CLKTCK\n", "CLKTCK 0\n", "CLKTCK 99.5\n", "CLKTCK 1000000\n", "CLKTCK nan\n", "CLKTCK -100\n",
    "CLKTCK fast\n"])
def test_without_a_usable_tick_rate_there_is_no_cpu_time(tick_line):
    """Ticks mean nothing without their rate, and a guessed rate is a wrong number drawn as right."""
    m = disp.parse_box_probe(PROBE + tick_line + "CPUS cpu 123456\nCPUS cpu0 60000\nNET 5 7\n")
    assert _cpu_net(m) == (None, None, 5, 7), "and the network line does not depend on it"
    assert m["cores"] == 20 and m["gpu_temp_c"] == 71.0, "the rest of the probe is untouched"


def test_another_tick_rate_scales_the_time():
    m = disp.parse_box_probe(PROBE + "CLKTCK 250\nCPUS cpu 1000\nCPUS cpu3 500\n")
    assert _cpu_net(m) == (4000000, {"3": 2000000}, None, None)


@pytest.mark.parametrize("lines,whole,cores", [
    ("CPUS cpu prose\nCPUS cpu0 5\n", None, {"0": 50000}),
    ("CPUS cpu 7\nCPUS cpu 9\n", 70000, None),                       # the first whole-machine line
    ("CPUS cpu0 5\nCPUS cpu0 9\n", None, {"0": 50000}),               # and the first of a core
    ("CPUS cpux 5\nCPUS gpu0 5\nCPUS cpu00000 5\nCPUS cpu-1 5\n", None, None),
    ("CPUS cpu007 5\n", None, {"7": 50000}),                          # a core is its number
    ("CPUS cpu0\nCPUS cpu1 5 6\nCPUS\n", None, None),                 # not `name ticks`
    ("CPUS cpu0 -5\nCPUS cpu1 nan\nCPUS cpu2 1e300\nCPUS cpu3 4\n", None, {"3": 40000}),
])
def test_a_cpu_line_that_is_not_a_reading_costs_only_itself(lines, whole, cores):
    m = disp.parse_box_probe(PROBE + "CLKTCK 100\n" + lines)
    assert (m["host_cpu_busy_usec"], m["host_cpu_core_busy_usec"]) == (whole, cores)


def test_a_row_keeps_at_most_1024_cores():
    lines = "".join(f"CPUS cpu{n} {n + 1}\n" for n in range(1100))
    cores = disp.parse_box_probe(PROBE + "CLKTCK 100\n" + lines)["host_cpu_core_busy_usec"]
    assert len(cores) == disp._PROBE_MAX_CORES == 1024 and cores["0"] == 10000 and "1024" not in cores


@pytest.mark.parametrize("line,expect", [
    ("NET 5\n", (5, None)),
    ("NET\n", (None, None)),
    ("NET lots more\n", (None, None)),
    ("NET -1 5\n", (None, 5)),
    ("NET 1e30 5\n", (None, 5)),                    # past what a float carries exactly: not a count
    ("NET 5 7 9\n", (5, 7)),
    ("NET 5 7\nNET 50 70\n", (5, 7)),
])
def test_a_network_line_that_is_not_a_reading_costs_only_its_field(line, expect):
    m = disp.parse_box_probe(PROBE + line)
    assert (m["net_rx_bytes"], m["net_tx_bytes"]) == expect and m["disk_total_gb"]


def _busy_ticks_here():
    """This machine's busy ticks, whole and per core, read the way 31f defines them."""
    whole, cores = None, {}
    for ln in Path("/proc/stat").read_text().splitlines():
        f = ln.split()
        if f and f[0].startswith("cpu"):
            n = [int(x) for x in f[1:9]] + [0] * 8
            busy = n[0] + n[1] + n[2] + n[5] + n[6] + n[7]
            if f[0] == "cpu":
                whole = busy
            else:
                cores[str(int(f[0][3:]))] = busy
    return whole, cores


def _net_bytes_here():
    """The bytes of this machine's one interface beside loopback; None when it has none or several."""
    rx = tx = n = 0
    for ln in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, _, rest = ln.partition(":")
        if name.strip() != "lo":
            f = rest.split()
            rx, tx, n = rx + int(f[0]), tx + int(f[8]), n + 1
    return (rx, tx) if n == 1 else None


@_A_BOXS_SHELL
def test_the_probe_reads_this_machines_cpu_time_and_network_bytes():
    """The real command on the real files. Counters only rise, so what the probe read lies between
    a reading taken before it ran and one taken after. The network line is there exactly when this
    machine has one interface beside loopback (a container has; a host that runs containers has not)."""
    if not shutil.which("getconf"):
        pytest.skip("no getconf here")
    hz = os.sysconf("SC_CLK_TCK")
    cpu_before, net_before = _busy_ticks_here(), _net_bytes_here()
    done = subprocess.run(["sh", "-c", disp.BOX_PROBE_CMD], capture_output=True, text=True, timeout=60)
    cpu_after, net_after = _busy_ticks_here(), _net_bytes_here()
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert f"CLKTCK {hz}" in lines
    assert len([ln for ln in lines if ln.startswith("CPUS cpu ")]) == 1
    m = disp.parse_box_probe(done.stdout)

    def usec(ticks):
        return ticks * 1_000_000 // hz

    assert usec(cpu_before[0]) <= m["host_cpu_busy_usec"] <= usec(cpu_after[0])
    assert set(m["host_cpu_core_busy_usec"]) == set(cpu_before[1]), "one line a core, by its number"
    for core, got in m["host_cpu_core_busy_usec"].items():
        assert usec(cpu_before[1][core]) <= got <= usec(cpu_after[1][core]), core
    if net_before is None and net_after is None:
        assert (m["net_rx_bytes"], m["net_tx_bytes"]) == (None, None), \
            "not one interface beside loopback here: no line"
    elif net_before is not None and net_after is not None:
        assert net_before[0] <= m["net_rx_bytes"] <= net_after[0]
        assert net_before[1] <= m["net_tx_bytes"] <= net_after[1]
    # (an interface that came or went while the probe ran: neither is claimed)


_PROC_STAT = """cpu  100 10 50 1000 30 5 7 3 2 1
cpu0 60 4 20 500 10 2 3 1 2 1
cpu1 40 6 30 500 20 3 4 2 0 0
intr 12345 0 0
ctxt 999
btime 1700000000
processes 4242
procs_running 2
softirq 100 1 2 3
"""
_PROC_NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 9999999   10000    0    0    0     0          0         0  9999999   10000    0    0    0     0       0          0
  eth0:12345678901234   5000    0    0    0     0          0         0   222222    4000    0    0    0     0       0          0
"""
_A_SECOND_INTERFACE = (
    "  eth1:    1000     10    0    0    0     0          0         0     2000      20    0    0    0     0       0          0\n")


def _probe_on(tmp_path, stat=None, net=None):
    """The probe string with its two /proc files swapped for recorded ones."""
    cmd = disp.BOX_PROBE_CMD
    assert cmd.count("/proc/stat ") == 1 and cmd.count("/proc/net/dev ") == 1
    for name, real, body in (("stat", "/proc/stat ", stat), ("net_dev", "/proc/net/dev ", net)):
        if body is not None:
            (tmp_path / name).write_text(body)
            cmd = cmd.replace(real, f"{tmp_path / name} ")
    done = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@_A_BOXS_SHELL
def test_the_two_programs_give_the_sums_worked_out_by_hand(tmp_path):
    """Busy is user + nice + system + irq + softirq + steal: not idle, not iowait, and not the two
    guest fields, which the kernel has already counted inside user and nice."""
    lines = _probe_on(tmp_path, _PROC_STAT, _PROC_NET_DEV)
    assert [ln for ln in lines if ln.startswith("CPUS")] == ["CPUS cpu 175", "CPUS cpu0 90", "CPUS cpu1 85"]
    assert [ln for ln in lines if ln.startswith("NET")] == ["NET 12345678901234 222222"], \
        "loopback left out, and a count that touches its colon reads"
    m = disp.parse_box_probe("\n".join(lines).replace(
        [ln for ln in lines if ln.startswith("CLKTCK")][0], "CLKTCK 100"))
    assert _cpu_net(m) == (1750000, {"0": 900000, "1": 850000}, 12345678901234, 222222)


@_A_BOXS_SHELL
def test_a_shell_that_sees_two_interfaces_gives_no_network_line(tmp_path):
    """A byte that crosses a card and a bridge is counted on both, and a sum over several falls
    when one goes away: a counter must never. One interface is a reading; two are none."""
    lines = _probe_on(tmp_path, _PROC_STAT, _PROC_NET_DEV + _A_SECOND_INTERFACE)
    assert not [ln for ln in lines if ln.startswith("NET")]
    assert [ln for ln in lines if ln.startswith("CPUS")], "and the CPU lines are still there"
    m = disp.parse_box_probe("\n".join(lines))
    assert (m["net_rx_bytes"], m["net_tx_bytes"]) == (None, None)


@_A_BOXS_SHELL
def test_a_cgroup_v1_boxs_cpu_counter_is_whole_past_two_to_the_31(tmp_path):
    """Invariant 25g. v1's counter is nanoseconds and the probe divides it. mawk, the awk Debian and
    Ubuntu ship, stops `%d` at 2^31 - 1, which a container reaches after 36 minutes of CPU time:
    from then on the counter stood still, the rate read 0.0 and the headroom gate took the
    container for idle."""
    v2, v1 = "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpuacct/cpuacct.usage"
    cmd = disp.BOX_PROBE_CMD
    assert cmd.count(v2) == 2 and cmd.count(v1) == 2, "the test for the file, and the read of it"
    (tmp_path / "usage").write_text("7200000000000\n")            # two hours of CPU time
    cmd = cmd.replace(v2, shlex.quote(str(tmp_path / "no_v2_file")))
    cmd = cmd.replace(v1, shlex.quote(str(tmp_path / "usage")))
    done = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert [ln for ln in done.stdout.splitlines() if ln.startswith("CPUU")] == ["CPUU 7200000000"]
    assert disp.parse_box_probe(done.stdout)["cpu_usage_usec"] == 7200000000


def test_no_count_in_the_probe_is_printed_with_a_32_bit_format():
    """Which awk a box has is not ours to choose, so the form is pinned as well as the value: no
    integer conversion at all, with or without flags and a width (`%5d` and `%i` stop at the same
    place under mawk)."""
    assert not re.findall(r"%[-+ #0]*\d*(?:\.\d+)?[diouxXc]", disp.BOX_PROBE_CMD)
    assert set(re.findall(r"%[-+ #0]*\d*(?:\.\d+)?[a-zA-Z]", disp.BOX_PROBE_CMD)) == {"%s", "%.0f", "%.4f"}


@_A_BOXS_SHELL
@pytest.mark.parametrize("stat,net", [("", ""), ("intr 5\n", _PROC_NET_DEV.split("  eth0")[0])])
def test_a_machine_that_answers_neither_gives_neither_line(tmp_path, stat, net):
    """Empty files, and a network table that holds only loopback: no line, and the probe goes on."""
    lines = _probe_on(tmp_path, stat, net)
    assert not [ln for ln in lines if ln.startswith(("CPUS", "NET"))]
    m = disp.parse_box_probe("\n".join(lines))
    assert m["cores"] >= 1 and _cpu_net(m) == (None, None, None, None)


def test_the_row_holds_them_and_the_event_does_not(tmp_path):
    """31c holds for 31f: the fields go to the row, and `box_measured` is the string it was."""
    d, _ = _mk(tmp_path, _Run(PROBE_31F))
    d._measure_box_resources()
    row = _row(d)
    assert tuple(row[key] for key in KEYS_31F) == (1234560000, {"0": 600000000, "1": 634560000}, 5000000, 7000000)
    assert [detail for event, detail in _events(d) if event == "box_measured"] == [GOLDEN_BOX_MEASURED]


def test_a_box_that_answers_without_them_has_them_null_in_its_row(tmp_path):
    d, _ = _mk(tmp_path)
    d._measure_box_resources()
    row = _row(d)
    assert all(key in row and row[key] is None for key in KEYS_31F)


def test_the_faster_reading_carries_them_too(tmp_path):
    d, run = _mk(tmp_path, _Run(PROBE_31F))
    d._measure_box_resources()
    _fast(d)
    run.doc = PROBE_31F.replace("CPUS cpu 123456", "CPUS cpu 129456").replace("NET 5000000", "NET 5600000")
    _set_row(d, {**_row(d), "at": _row(d)["at"] - 60})
    d._last_reading[-1] -= 61
    d._measure_box_resources()
    row = _row(d)
    assert len(run.calls) == 2 and (row["host_cpu_busy_usec"], row["net_rx_bytes"]) == (1294560000, 5600000)


def test_the_exporter_serves_them_as_seconds_and_bytes(tmp_path):
    exporter = _load("metrics_exporter", "fleet/metrics_exporter.py")
    d, _ = _mk(tmp_path, _Run(PROBE_31F))
    d._measure_box_resources()
    lines = exporter.Exporter(str(tmp_path / "runs.sqlite")).collect().splitlines()
    box = 'label="laptop-gpu",instance_id="-1"'
    for line in (f"fleet_box_cpu_busy_seconds_total{{{box}}} 1234.56",
                 f'fleet_box_cpu_core_busy_seconds_total{{{box},core="0"}} 600',
                 f'fleet_box_cpu_core_busy_seconds_total{{{box},core="1"}} 634.56',
                 f"fleet_box_network_receive_bytes_total{{{box}}} 5000000",
                 f"fleet_box_network_transmit_bytes_total{{{box}}} 7000000",
                 "# TYPE fleet_box_cpu_core_busy_seconds_total counter",
                 "# TYPE fleet_box_network_transmit_bytes_total counter"):
        assert line in lines, line
