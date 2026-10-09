"""Task-dispatcher invariant 8b — launch pacing is the COORDINATOR's; the box holds a cached copy.

The numbers the worker paces launches by (settle time, the idle bypass, the GPU-utilisation
ceiling, the CPU reserve, the free-VRAM requirement) were constants in box-side code, so changing
one meant shipping worker code to every box, while coordinator settings were tuned against them
without owning them. They are one coordinator setting now, delivered on the measure probe's own ssh
call and read by the worker from the copy that lands in its spool.
"""

import base64
import importlib.util
import json
import os
import sys
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
reg = _load("registry_db", "fleet/registry_db.py")
sup = _load("sweep_supervisor", "fleet/sweep_supervisor.py")
sw = _load("spool_worker", "fleet/spool_worker.py")

DEFAULT = disp.DEFAULT_SETTINGS["launch_gate"]
IDLE = {"gpu_util": None, "vram_free_gb": None, "vram_total_gb": None, "proc_vram_gb": {},
        "load1": 1.0, "cores": 16}


# ---------------------------------------------------------------------------------- one source
class TestOneSetOfNumbers:
    def test_the_workers_built_ins_equal_the_coordinators_default(self):
        """A box before its first probe paces by its built-ins. If those drift from the setting,
        a fresh box behaves differently from every other one until it is first measured."""
        assert sw.launch_gate_defaults() == DEFAULT

    def test_the_default_is_the_constants_the_box_used_to_carry(self):
        """Adopting 8b must change no behaviour."""
        assert DEFAULT == {"settle_minutes": 3.0, "settle_floor_min": 0.5, "settle_idle_frac": 0.5,
                           "util_ceiling": 90.0, "cpu_reserve_cores": 1.0, "vram_lane_mult": 1.25,
                           "vram_free_frac": 0.2}

    def test_the_manual_sweep_lane_is_unchanged(self):
        """`should_launch` handed a config WITHOUT the three new keys (a sweep file) decides
        exactly as it did when they were literals in its body."""
        cfg = dict(sup.AUTO_DEFAULTS)
        assert sup.should_launch({**IDLE, "load1": 15.0}, None, 2, 99.0, cfg) == (False, "cpu_load")
        assert sup.should_launch({**IDLE, "load1": 14.9}, None, 2, 99.0, cfg)[0]
        gpu = {**IDLE, "gpu_util": 5.0, "vram_total_gb": 10.0}
        assert sup.should_launch({**gpu, "vram_free_gb": 1.9}, None, 2, 99.0, cfg) == (False, "vram")
        assert sup.should_launch({**gpu, "vram_free_gb": 2.0}, None, 2, 99.0, cfg)[0]
        assert sup.should_launch({**gpu, "vram_free_gb": 0.74}, 0.6, 2, 99.0, cfg) == (False, "vram")
        assert sup.should_launch({**gpu, "vram_free_gb": 0.75}, 0.6, 2, 99.0, cfg)[0]


# ---------------------------------------------------------------------------------- the box
class _Live:
    pid = 4321

    def poll(self):
        return None


def _worker(tmp_path, monkeypatch, hw, n_live=2, since_min=None):
    """A worker with `n_live` trainers running and one prepared task waiting, on a box measuring
    `hw`. `should_launch` is the real one."""
    w = sw.Worker(tmp_path / "spool")
    d = tmp_path / "spool" / "active" / "new"
    (d / "repo").mkdir(parents=True)
    spec = {"task_id": "new", "grp": "g", "name": "new", "argv": ["python", "x.py"], "env": {},
            "est_minutes": 1, "git_sha": "d", "pip_extras": [], "resume_from": None}
    w.active = {f"busy{i}": sw.ActiveTask(f"busy{i}", tmp_path / f"b{i}", proc=_Live())
                for i in range(n_live)}
    w.active["new"] = sw.ActiveTask("new", d, spec=spec)
    launched = []
    monkeypatch.setattr(sw.subprocess, "Popen", lambda argv, **kw: launched.append(argv) or _Live())
    monkeypatch.setattr(sw, "sample_hw", lambda: dict(hw))
    if since_min is not None:
        w._last_launch = sw.time.time() - since_min * 60
    return w, launched


def _push(w, doc):
    """What the coordinator's probe leaves in the spool."""
    path = w.spool / sw.LAUNCH_GATE_FILE
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # a visibly new mtime
    return path


def _log(w, event):
    if not w.worker_log.exists():
        return []
    rows = [json.loads(line) for line in w.worker_log.read_text().splitlines()]
    return [r["detail"] for r in rows if r["event"] == event]


class TestTheBoxPacesByItsPushedCopy:
    def test_with_no_copy_it_uses_the_built_ins_and_says_nothing(self, tmp_path, monkeypatch):
        w, launched = _worker(tmp_path, monkeypatch, {**IDLE, "load1": 12.0}, since_min=1.0)
        w.launch_ready()
        assert launched == [] and _log(w, "launch_gate")[0].startswith("settling: 1.0min")
        assert _log(w, "launch_gate_config") == []

    def test_a_pushed_settle_time_changes_the_decision(self, tmp_path, monkeypatch):
        """The same box, the same minute since its last launch: held by the built-in 3-minute
        settle, launched once the coordinator says 0.5 — with no worker code delivered."""
        w, launched = _worker(tmp_path, monkeypatch, {**IDLE, "load1": 12.0}, since_min=1.0)
        w.launch_ready()
        assert launched == []
        _push(w, {**DEFAULT, "settle_minutes": 0.5})
        w.launch_ready()
        assert len(launched) == 1
        (line,) = _log(w, "launch_gate_config")
        assert line.startswith("from the coordinator:") and '"settle_minutes":0.5' in line

    def test_a_pushed_cpu_reserve_changes_the_decision_and_the_line_quotes_it(self, tmp_path,
                                                                             monkeypatch):
        w, launched = _worker(tmp_path, monkeypatch, {**IDLE, "load1": 13.5})
        w.launch_ready()
        assert len(launched) == 1                       # 13.5 < 16 - 1
        w.active["new"].proc = None
        w._last_launch = 0.0                            # long settled: only the CPU rule is in play
        _push(w, {**DEFAULT, "cpu_reserve_cores": 4.0})
        w.launch_ready()
        assert len(launched) == 1                       # 13.5 >= 16 - 4: held
        assert _log(w, "launch_gate")[-1] == "cpu_load: load1 13.50 >= cores-4 12"

    def test_the_copy_is_re_read_only_when_it_changes(self, tmp_path, monkeypatch):
        w, _ = _worker(tmp_path, monkeypatch, IDLE)
        _push(w, {**DEFAULT, "util_ceiling": 80.0})
        for _ in range(5):
            assert w._launch_gate()["util_ceiling"] == 80.0
        assert len(_log(w, "launch_gate_config")) == 1
        _push(w, {**DEFAULT, "util_ceiling": 70.0})
        assert w._launch_gate()["util_ceiling"] == 70.0
        assert len(_log(w, "launch_gate_config")) == 2

    def test_the_copy_survives_a_worker_restart(self, tmp_path, monkeypatch):
        """'Fine if the box keeps a cache': a NEW worker process on the same spool, with no
        coordinator in sight, paces by what the last one was handed."""
        w, _ = _worker(tmp_path, monkeypatch, IDLE)
        _push(w, {**DEFAULT, "settle_minutes": 1.5})
        again = sw.Worker(tmp_path / "spool")
        assert again._launch_gate()["settle_minutes"] == 1.5

    def test_an_idle_worker_still_acknowledges_a_new_copy(self, tmp_path, monkeypatch):
        """`tick` looks even with nothing to launch, so a push is visible in `worker.jsonl` within
        one worker poll rather than at the box's next launch."""
        monkeypatch.setattr(sw, "sample_hw", lambda: dict(IDLE))
        w = sw.Worker(tmp_path / "spool")
        _push(w, DEFAULT)
        w.tick()
        assert len(_log(w, "launch_gate_config")) == 1


class TestTheCopyIsATrustBoundary:
    """The file arrives over the wire and sits where every task on the box can write."""

    def test_one_bad_value_costs_only_that_key(self):
        cfg, problems = sw.parse_launch_gate(json.dumps(
            {**DEFAULT, "settle_minutes": -5, "util_ceiling": 55.0}))
        assert cfg["settle_minutes"] == 3.0 and cfg["util_ceiling"] == 55.0
        assert len(problems) == 1 and "settle_minutes=-5" in problems[0]

    @pytest.mark.parametrize("key,bad", [("settle_minutes", "3"), ("settle_minutes", True),
                                         ("settle_idle_frac", 1.5), ("util_ceiling", 101),
                                         ("vram_free_frac", -0.1), ("cpu_reserve_cores", None),
                                         ("vram_lane_mult", float("nan")),
                                         ("settle_floor_min", float("inf"))])
    def test_a_value_that_is_not_a_number_in_range_is_refused(self, key, bad):
        text = json.dumps({key: bad}) if bad == bad and bad not in (float("inf"),) else \
            '{"%s": %s}' % (key, "NaN" if bad != bad else "Infinity")
        cfg, problems = sw.parse_launch_gate(text)
        assert cfg == sw.launch_gate_defaults() and len(problems) == 1 and key in problems[0]

    def test_an_unknown_key_is_ignored_not_refused(self):
        """A coordinator newer than this worker may send one. Rejecting the whole file would leave
        the box pacing by stale numbers, silently."""
        cfg, problems = sw.parse_launch_gate(json.dumps({"settle_minutes": 2.0, "future_knob": 7}))
        assert cfg["settle_minutes"] == 2.0 and problems == [] and "future_knob" not in cfg

    @pytest.mark.parametrize("text,why", [("{not json", "not valid JSON"), ("[1, 2]", "not a JSON object"),
                                          ("", "not valid JSON")])
    def test_a_file_that_is_not_an_object_leaves_the_defaults(self, text, why):
        cfg, problems = sw.parse_launch_gate(text)
        assert cfg == sw.launch_gate_defaults() and problems == [why]

    def test_no_file_is_not_a_problem(self):
        assert sw.parse_launch_gate(None) == (sw.launch_gate_defaults(), [])

    def test_a_refused_value_is_reported_in_the_workers_log(self, tmp_path, monkeypatch):
        w, _ = _worker(tmp_path, monkeypatch, IDLE)
        _push(w, {**DEFAULT, "util_ceiling": 900})
        assert w._launch_gate()["util_ceiling"] == 90.0
        (line,) = _log(w, "launch_gate_config")
        assert "REFUSED" in line and "util_ceiling=900" in line


# ---------------------------------------------------------------------------------- the push
class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


PROBE_OK = "NPROC 16\nLOAD 1.0 1.0 1.0 1/1 1\nMEM 32000 24000\n"


class _Box:
    """An ssh double for one box: answers the measure probe and says what happened to its copy."""

    def __init__(self, report="updated"):
        self.report, self.calls = report, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "NPROC" in cmd[-1]:
            return _Proc(0, ("FRZ 0\n" + (f"LG {self.report}\n" if self.report else "") + PROBE_OK))
        return _Proc(0, "")

    def probes(self):
        return [c[-1] for c in self.calls if "NPROC" in c[-1]]


def _pushed(probe_cmd):
    seg = probe_cmd.split("launch_gate.json", 1)[0]
    b64 = seg.rsplit("echo ", 1)[1].split(" ", 1)[0]
    return json.loads(base64.b64decode(b64))


def _dispatcher(tmp_path, box, source="owned", iid=-1):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=box, vastai_run=_Box())
    d.conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
        "ssh_port, slots_total, hard_cap_at, source) VALUES (?,NULL,'box',?,'live',0,'box',2222,8,"
        "'2099-01-01T00:00:00Z',?)", (iid, reg.now_iso(), source))
    d.conn.commit()
    return d


def _events(d, name):
    return [r["detail"] for r in d.conn.execute(
        "SELECT detail FROM events WHERE event=? ORDER BY seq", (name,))]


class TestTheCoordinatorDeliversIt:
    def test_the_measure_probe_carries_the_setting_in_its_own_ssh_call(self, tmp_path):
        box = _Box()
        d = _dispatcher(tmp_path, box)
        d._measure_box_resources()
        assert len(box.calls) == 1, "no extra connection: the push rides the probe"
        (probe,) = box.probes()
        assert _pushed(probe) == DEFAULT
        assert probe.index("launch_gate.json") < probe.index("NPROC")
        assert d._box_res[-1]["cores"] == 16, "the measurement must survive the report lines"

    def test_an_update_is_logged_and_agreement_is_not(self, tmp_path):
        box = _Box("updated")
        d = _dispatcher(tmp_path, box)
        d._measure_box_resources()
        (line,) = _events(d, "launch_gate_pushed")
        assert json.loads(line) == DEFAULT
        box.report = "same"
        d._last_res_measure.clear()
        d._measure_box_resources()
        assert len(_events(d, "launch_gate_pushed")) == 1

    def test_a_box_that_does_not_report_is_not_claimed_as_updated(self, tmp_path):
        d = _dispatcher(tmp_path, _Box(report=None))
        d._measure_box_resources()
        assert _events(d, "launch_gate_pushed") == []

    def test_an_edit_to_the_setting_reaches_the_next_probe_without_a_restart(self, tmp_path):
        """Read LIVE from the registry: `self.settings` is a start-time snapshot, and a change that
        needed a coordinator restart would be the invisible second step this exists to remove."""
        box = _Box()
        d = _dispatcher(tmp_path, box)
        d._measure_box_resources()
        d.conn.execute("UPDATE settings SET value=? WHERE key='launch_gate'",
                       (json.dumps({**DEFAULT, "settle_minutes": 1.0}),))
        d.conn.commit()
        d._last_res_measure.clear()
        d._measure_box_resources()
        assert _pushed(box.probes()[-1])["settle_minutes"] == 1.0
        assert d.settings["launch_gate"]["settle_minutes"] == 3.0   # the snapshot did not move

    def test_rentals_get_it_too(self, tmp_path):
        box = _Box()
        d = _dispatcher(tmp_path, box, source="vast", iid=4242)
        d._measure_box_resources()
        assert _pushed(box.probes()[0]) == DEFAULT

    def test_the_payload_is_canonical_and_survives_a_hand_edited_setting(self):
        """The settings table is hand-editable. Only known keys travel, a value that is not a
        number takes the code default, and the form is byte-stable so the box can compare."""
        a = disp.launch_gate_payload({"util_ceiling": 80, "settle_minutes": "soon", "extra": 1})
        assert json.loads(a) == {**DEFAULT, "util_ceiling": 80.0}
        assert a == disp.launch_gate_payload({"extra": 2, "util_ceiling": 80.0})
        assert json.loads(disp.launch_gate_payload(None)) == DEFAULT
        assert json.loads(disp.launch_gate_payload("garbage")) == DEFAULT

    def test_the_report_line_is_read_and_nothing_else_is(self):
        assert disp.parse_launch_gate_report("FRZ 0\nLG updated\nNPROC 4\n") == "updated"
        assert disp.parse_launch_gate_report("LG same\n") == "same"
        for junk in ("", "LG\n", "LG maybe\n", "LG updated now\n", "NPROC 4\n"):
            assert disp.parse_launch_gate_report(junk) is None, junk

    def test_the_command_writes_atomically_and_only_on_a_difference(self):
        cmd = disp.box_assert_cmd(False, disp.launch_gate_payload(DEFAULT))
        assert "launch_gate.json.tmp" in cmd and "cmp -s" in cmd and "mv -f" in cmd
        assert "echo LG same" in cmd and "echo LG updated" in cmd
        assert "launch_gate" not in disp.box_assert_cmd(False)


class TestTheCoordinatorsOwnGuardReadsItsOwnNumber:
    def test_the_grace_exceeds_the_fill_time_at_the_coordinators_settle(self):
        """Inv. 27e: `ship_launch_grace_min` must exceed `max_slots_cap x settle_minutes`, or the
        over-pack reaper fires on a box that is merely still filling. That product used to span a
        coordinator setting and a constant on the box; both factors are the coordinator's now."""
        s = disp.DEFAULT_SETTINGS
        assert s["ship_launch_grace_min"] > s["max_slots_cap"] * s["launch_gate"]["settle_minutes"]
