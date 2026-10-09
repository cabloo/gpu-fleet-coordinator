"""box-pause spec inv. 20a–20d: the hard GPU power cap, the coordinator pushing each owned box's
schedule to its host, the host enforcer, and `owned_box_setup.sh`'s self-contained bundle."""

import base64
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
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
cap = _load("capacity", "fleet/capacity.py")
bca = _load("box_capacity_apply", "fleet/box_capacity_apply.py")

SCHED = {"tz": "UTC", "cores": 20, "vram_gb": 8, "_comment": "stripped before the push",
         "windows": [{"from": "23:00", "to": "07:00", "cpu": 0.875, "vram": 0.875},
                     {"from": "07:00", "to": "23:00", "cpu": 0.5, "vram": 0.75, "gpu_power": 0.6}]}


def _at(h):
    return datetime(2026, 1, 1, h, 0, 0, tzinfo=timezone.utc)


# ---- inv. 20a: the schema -------------------------------------------------------------------
class TestGpuPowerSchema:
    def test_fraction_follows_the_window(self):
        s = cap.load(SCHED)
        assert cap.gpu_power_fraction(s, _at(10)) == 0.6
        assert cap.gpu_power_fraction(s, _at(2)) is None          # night window: stock limit

    @pytest.mark.parametrize("bad", [0, 0.0, 1.5, -0.1, "0.5"])
    def test_out_of_range_rejected(self, bad):
        s = json.loads(json.dumps(SCHED))
        s["windows"][1]["gpu_power"] = bad
        with pytest.raises(ValueError, match="gpu_power"):
            cap.load(s)

    def test_slots_ignore_gpu_power(self):
        """The coordinator never reads gpu_power — packing is still bounded by vram (inv. 20a)."""
        s = cap.load(SCHED)
        plain = json.loads(json.dumps(SCHED))
        del plain["windows"][1]["gpu_power"]
        assert cap.effective_slots(s, _at(10), 1, 0.6) == cap.effective_slots(plain, _at(10), 1, 0.6)


# ---- inv. 20a/20c: the host enforcer --------------------------------------------------------
class TestEnforcer:
    def test_power_target_clamps_to_the_card(self):
        assert bca.power_target_w(0.6, 290, 100, 310) == 174
        assert bca.power_target_w(0.2, 290, 100, 310) == 100           # floor of the card
        assert bca.power_target_w(None, 290, 100, 310) == 290          # no cap -> default
        assert bca.power_target_w(1.0, 290, 100, 250) == 250           # max below default

    def test_parse_power_query_keeps_unsettable_cards(self):
        rows = bca.parse_power_query("0, 290.00, 290.00, 100.00, 310.00\n"
                                     "1, [N/A], [N/A], [N/A], [N/A]\n")
        assert rows[0] == {"index": 0, "limit": 290.0, "default": 290.0, "min": 100.0,
                           "max": 310.0, "settable": True}
        assert rows[1]["settable"] is False and rows[1]["index"] == 1

    def _fake(self, monkeypatch, nano_cpus, power_line):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, f"{nano_cpus}\n", "")
            if cmd[0] == "nvidia-smi" and cmd[1].startswith("--query-gpu"):
                return subprocess.CompletedProcess(cmd, 0, power_line, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bca.subprocess, "run", run)
        monkeypatch.setattr(bca.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(bca.os, "cpu_count", lambda: 32)
        return calls

    def test_missing_file_uncaps_both_axes(self, monkeypatch, tmp_path):
        """⛔ The CPU cap is lifted by setting it to EVERY core the host has (32 here), never by
        `--cpus 0` (inv. 20b-1). This test used to assert `--cpus 0.0` — and so pinned the defect:
        `docker update` reads a zero as "field not supplied" and keeps the existing limit, exiting 0.
        On `desktop` (2026-10-03) that left a container at 10 of 20 cores eleven minutes after its
        schedule was removed, the enforcer "uncapping" it once a minute throughout."""
        calls = self._fake(monkeypatch, 10_000_000_000, "0, 174.00, 290.00, 100.00, 310.00\n")
        rc = bca.main(["--config", str(tmp_path / "absent.json"), "--container", "w",
                       "--uncapped-if-missing"])
        assert rc == 0
        assert ["docker", "update", "--cpus", "32.0", "w"] in calls
        assert not [c for c in calls if c[:3] == ["docker", "update", "--cpus"] and float(c[3]) == 0]
        assert ["nvidia-smi", "-i", "0", "-pl", "290"] in calls

    def test_an_uncap_already_held_is_a_noop(self, monkeypatch, tmp_path):
        """Idempotent: once the container carries the all-cores cap the enforcer stops updating it,
        instead of re-running `docker update` every minute as the `--cpus 0` form did."""
        calls = self._fake(monkeypatch, 32_000_000_000, "0, 290.00, 290.00, 100.00, 310.00\n")
        assert bca.main(["--config", str(tmp_path / "absent.json"), "--container", "w",
                         "--uncapped-if-missing"]) == 0
        assert not [c for c in calls if "update" in c or "-pl" in c]

    def test_day_window_applies_cpu_and_gpu_caps(self, monkeypatch, tmp_path):
        cfg = tmp_path / "capacity.json"
        cfg.write_text(json.dumps(SCHED))
        calls = self._fake(monkeypatch, 0, "0, 290.00, 290.00, 100.00, 310.00\n")
        monkeypatch.setattr(bca.capacity, "active_window",
                            lambda s, now: s["windows"][1])      # pin "now" to the day window
        assert bca.main(["--config", str(cfg), "--container", "w"]) == 0
        assert ["docker", "update", "--cpus", "10.0", "w"] in calls
        assert ["nvidia-smi", "-i", "0", "-pl", "174"] in calls

    def test_caps_already_held_is_a_noop(self, monkeypatch, tmp_path):
        cfg = tmp_path / "capacity.json"
        cfg.write_text(json.dumps(SCHED))
        calls = self._fake(monkeypatch, 10_000_000_000, "0, 174.00, 290.00, 100.00, 310.00\n")
        monkeypatch.setattr(bca.capacity, "active_window", lambda s, now: s["windows"][1])
        assert bca.main(["--config", str(cfg), "--container", "w"]) == 0
        assert not [c for c in calls if "update" in c or "-pl" in c]

    def test_malformed_file_changes_nothing(self, monkeypatch, tmp_path):
        cfg = tmp_path / "capacity.json"
        cfg.write_text('{"tz": "UTC"}')
        calls = self._fake(monkeypatch, 0, "")
        assert bca.main(["--config", str(cfg), "--container", "w", "--uncapped-if-missing"]) == 2
        assert calls == []

    def test_cpu_cap_clamped_to_host(self, monkeypatch):
        calls = self._fake(monkeypatch, 0, "")
        bca.apply_cpu(999.0, "w", dry=False)
        assert ["docker", "update", "--cpus", "32.0", "w"] in calls


# ---- inv. 20b: the coordinator push ---------------------------------------------------------
class _Run:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, "", "")

    def pushes(self):
        return [c[-1] for c in self.calls if "fleet_host" in c[-1]]


def _dispatcher(tmp_path, run):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=_Run())
    d.conn.execute(
        "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, ssh_host, "
        "ssh_port, slots_total, hard_cap_at, source) VALUES (-1,NULL,'newbox',?,'live',0,"
        "'newbox',2222,8,'2099-01-01T00:00:00Z','owned')", (reg.now_iso(),))
    d.conn.commit()
    return d


def _decoded(remote_cmd):
    b64 = remote_cmd.split("echo ", 1)[1].split(" ", 1)[0]
    return json.loads(base64.b64decode(b64))


class TestPush:
    def test_pushes_once_then_only_on_change(self, tmp_path):
        run = _Run()
        d = _dispatcher(tmp_path, run)
        sched = cap.load(json.loads(json.dumps(SCHED)))
        d._capacity_schedule = lambda inst: sched
        d._push_capacity_schedules()
        d._push_capacity_schedules()
        assert len(run.pushes()) == 1
        body = _decoded(run.pushes()[0])
        assert "_comment" not in body and body["windows"][1]["gpu_power"] == 0.6
        assert "mv -f ~/fleet_host/capacity.json.tmp ~/fleet_host/capacity.json" in run.pushes()[0]
        sched["windows"][1]["gpu_power"] = 0.5
        d._push_capacity_schedules()
        assert len(run.pushes()) == 2 and _decoded(run.pushes()[1])["windows"][1]["gpu_power"] == 0.5
        assert d.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event='capacity_pushed'").fetchone()[0] == 2

    def test_repushes_after_the_interval(self, tmp_path):
        run = _Run()
        d = _dispatcher(tmp_path, run)
        d._capacity_schedule = lambda inst: cap.load(SCHED)
        d._push_capacity_schedules()
        d._cap_pushed[-1] = (d._cap_pushed[-1][0], 0.0)           # last push long ago
        d._push_capacity_schedules()
        assert len(run.pushes()) == 2

    def test_a_box_with_NO_schedule_is_pushed_a_fully_open_one(self, tmp_path):
        """Inv. 20b-1. The file used to be REMOVED, on the reading that the enforcer treats absence
        as uncapped — which it could not act on (`docker update --cpus 0` is a no-op), so a box that
        LOST its schedule kept its last window's CPU cap forever. An explicit all-day `cpu 1.0`
        schedule is the form every installed enforcer already turns into `--cpus <all cores>`."""
        run = _Run()
        d = _dispatcher(tmp_path, run)
        d._capacity_schedule = lambda inst: None
        d._box_res[-1] = {"cores": 20}
        d._push_capacity_schedules()
        (push,) = run.pushes()
        assert "rm -f" not in push
        body = cap.load(_decoded(push))
        assert body["cores"] == 20
        assert all(w["cpu"] == 1.0 and "gpu_power" not in w for w in body["windows"])
        for hour in range(24):
            assert cap.cpu_cores_cap(body, _at(hour)) == 20.0, f"not open at {hour}:00"
        (detail,) = [r[0] for r in d.conn.execute(
            "SELECT detail FROM events WHERE event='capacity_pushed'")]
        assert detail.startswith("fully open (no schedule)")

    def test_an_UNMEASURED_box_with_no_schedule_is_left_alone(self, tmp_path):
        """No core count to size it from yet (the measure phase runs later in the poll): push
        nothing rather than guess — and never remove what the host already holds."""
        run = _Run()
        d = _dispatcher(tmp_path, run)
        d._capacity_schedule = lambda inst: None
        d._push_capacity_schedules()
        assert run.pushes() == []

    def test_the_coordinator_still_admits_a_schedule_less_box_unbudgeted(self, tmp_path):
        """Only the PUSHED copy is synthesised. The coordinator's own gates must keep seeing a box
        with no schedule as exactly that — a fully-open payload with `vram_gb: 0` would otherwise
        infer zero lanes."""
        d = _dispatcher(tmp_path, _Run())
        d._capacity_schedule = lambda inst: None
        d._box_res[-1] = {"cores": 20}
        d._push_capacity_schedules()
        inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=-1").fetchone())
        assert d._capacity_slots(inst) is None

    def test_failed_push_is_retried(self, tmp_path):
        run = _Run(rc=255)
        d = _dispatcher(tmp_path, run)
        d._capacity_schedule = lambda inst: cap.load(SCHED)
        d._push_capacity_schedules()
        run.rc = 0
        d._push_capacity_schedules()
        assert len(run.pushes()) == 2
        assert d.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event='capacity_pushed'").fetchone()[0] == 1

    def test_rentals_are_never_pushed_to(self, tmp_path):
        run = _Run()
        d = _dispatcher(tmp_path, run)
        d.conn.execute("UPDATE instances SET source='vast' WHERE id=-1")
        d.conn.commit()
        d._capacity_schedule = lambda inst: cap.load(SCHED)
        d._push_capacity_schedules()
        assert run.pushes() == []


# ---- owned_box_setup.sh ---------------------------------------------------------------------
SETUP = ROOT / "fleet" / "owned_box_setup.sh"


def test_setup_script_parses():
    subprocess.run(["bash", "-n", str(SETUP)], check=True)


def test_bundle_carries_the_current_payload_files():
    """The bundle is what gets scp'd to a fresh box, so its trailer must be byte-identical to the
    repo's Dockerfile + enforcer — never a stale embedded copy."""
    out = subprocess.run(["bash", str(SETUP), "--bundle"], check=True, capture_output=True,
                         text=True).stdout
    script, _, trailer = out.partition("\n__FLEET_OWNED_BOX_PAYLOAD__\n")
    assert trailer and "set -euo pipefail" in script
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(trailer)), mode="r:gz") as tf:
        names = sorted(tf.getnames())
        assert names == ["Dockerfile.owned_worker", "box_capacity_apply.py", "capacity.py"]
        for n in names:
            assert tf.extractfile(n).read() == (ROOT / "fleet" / n).read_bytes()
