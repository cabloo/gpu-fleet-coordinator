"""Job services (docs/specs/job-services.spec.md): a task that asks for a home service gets the
service's files on its box, and on a rented box a reverse tunnel to it — and a task that does not
ask gets nothing of this at all.

What these pin, in the spec's order: the validators and the queue-time refusal (inv. 2), the
ship-time failure (inv. 3), the files going over STDIN and never into a command line (inv. 4/5),
the tunnel's lifecycle across passes (inv. 6/8/9), the files leaving a box (inv. 10), the task's
environment variable (inv. 11), and the box-side shell commands run for real against a temporary
home.
"""
import argparse
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
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
_tmod = _load("test_dispatcher_helpers", "tests/test_dispatcher.py")
reg = disp.registry_db
jm = _load("job_manifest", "fleet/job_manifest.py")

SECRET = b"the-service-key-0123456789"
HOME = "172.17.0.1:18120"
BOX_PORT = 18100


@pytest.fixture(autouse=True)
def _isolate_experiments_root(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", root)
    monkeypatch.setattr(_tmod.disp, "EXPERIMENTS_ROOT", root)


@pytest.fixture(autouse=True)
def phase_faults(monkeypatch):
    """Every `service_tunnels_failed` any test's dispatcher logs. The phase swallows its own faults
    (inv. 6), so without this a broken step would pass every test that does not look for it; a test
    that expects one empties the list after reading it."""
    faults = []
    real = disp.Dispatcher.log

    def log(self, event, detail, task_id=None, instance_id=None):
        if event == "service_tunnels_failed":
            faults.append(detail)
        return real(self, event, detail, task_id=task_id, instance_id=instance_id)

    monkeypatch.setattr(disp.Dispatcher, "log", log)
    yield faults
    assert faults == [], f"the tunnel phase met a fault of its own: {faults}"


def _definition(tmp_path, *, owned_files=True):
    rented = tmp_path / "rented"
    rented.mkdir(exist_ok=True)
    (rented / "api-key").write_bytes(SECRET)
    (rented / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\n")
    owned = tmp_path / "owned"
    owned.mkdir(exist_ok=True)
    (owned / "api-key").write_bytes(b"the-home-key")
    return {
        "rented": {"files": str(rented), "tunnel": {"home": HOME, "box_port": BOX_PORT},
                   "values": {"url": f"https://127.0.0.1:{BOX_PORT}", "tls_name": "checker.example"}},
        "owned": {**({"files": str(owned)} if owned_files else {}),
                  "values": {"url": "https://192.168.0.10:18100", "tls_name": "checker.example"}},
    }


def _set_services(conn, services):
    conn.execute("INSERT INTO settings(key, value) VALUES ('services', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(services),))
    conn.commit()


# Above the kernel's largest pid_max (2**22), so no stand-in's pid can name a real process: the
# dispatcher signals a tunnel's GROUP, and a group kill on a borrowed pid would be a real one.
FAKE_PID = 2 ** 22 + 1000


class _Proc:
    """A stand-in for the tunnel's `Popen`: alive until told otherwise."""

    def __init__(self, pid):
        self.pid, self.rc, self.terminated = pid, None, False

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.terminated = self.terminated or sig == signal.SIGTERM
        self.rc = -sig

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        return self.rc


class _Popen:
    def __init__(self, order=None):
        self.calls, self.procs, self.order = [], [], order

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.order is not None:
            self.order.append(("open", cmd[cmd.index("-R") + 1]))
        proc = _Proc(FAKE_PID + len(self.procs))
        self.procs.append(proc)
        return proc


class _Run:
    """Records each subprocess-shaped call WITH its keyword arguments (stdin included)."""

    def __init__(self, rc_for=None, out_for=None, order=None):
        self.calls, self.rc_for = [], rc_for or (lambda cmd: 0)
        self.out_for = out_for or (lambda cmd: "")
        self.order = order

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.order is not None and cmd and cmd[0] == "ssh":
            self.order.append(("ssh", cmd[-1]))
        rc = self.rc_for(cmd)
        return subprocess.CompletedProcess(cmd, rc, self.out_for(cmd),
                                           "" if rc == 0 else "it went wrong")

    def remote(self):
        return [c[-1] for c, _ in self.calls if c and c[0] == "ssh"]


def _dispatcher(tmp_path, run=None, popen=None):
    run = run or _Run()
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run,
                           popen=popen or _Popen())


def _asking_task(d, tmp_path, *, owned=False, services=("checker",), task_id="SVC1", define=True):
    _tmod._seed_instance_and_task(d.conn, task_id=task_id, entrypoint="smoke")
    if owned:
        d.conn.execute("UPDATE instances SET source='owned' WHERE id=1")
    if services is not None:
        d.conn.execute("UPDATE tasks SET resource_hint_json=? WHERE id=?",
                       (json.dumps({"services": list(services)}), task_id))
    if define:
        _set_services(d.conn, {"checker": _definition(tmp_path)})
    d.conn.commit()
    task = dict(d.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    inst = dict(d.conn.execute("SELECT * FROM instances WHERE id=1").fetchone())
    return task, inst


def _members(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        return {m.name: (m.mode, tf.extractfile(m).read()) for m in tf.getmembers()}


def _events(d, event):
    return [json.loads(r["detail"]) for r in d.conn.execute(
        "SELECT detail FROM events WHERE event=? ORDER BY seq", (event,))]


# -- the definition and the queue-time refusal (inv. 2) -------------------------------------------

def test_a_complete_definition_is_accepted(tmp_path):
    assert reg.service_definition_error("checker", _definition(tmp_path)) is None


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.pop("owned"), "needs `owned`"),
    (lambda d: d.pop("rented"), "needs `rented`"),
    (lambda d: d["owned"].__setitem__("tunnel", {"home": HOME, "box_port": 1}), "takes no tunnel"),
    (lambda d: d["rented"].pop("tunnel"), "needs `tunnel`"),
    (lambda d: d["rented"]["tunnel"].__setitem__("home", "no-port"), "HOST:PORT"),
    (lambda d: d["rented"]["tunnel"].__setitem__("home", "h:99999"), "HOST:PORT"),
    (lambda d: d["rented"]["tunnel"].__setitem__("box_port", 22), "1024 to 65535"),
    (lambda d: d["rented"]["tunnel"].__setitem__("box_port", True), "1024 to 65535"),
    (lambda d: d["rented"].__setitem__("files", "relative/dir"), "absolute path"),
    (lambda d: d["rented"]["values"].__setitem__("Bad-Key", "x"), "must match"),
    (lambda d: d["rented"]["values"].__setitem__("nested", {"a": 1}), "string, number or boolean"),
    (lambda d: d.__setitem__("extra", {}), "unknown key"),
    (lambda d: d["owned"].__setitem__("extra", 1), "unknown key"),
])
def test_a_wrong_definition_names_its_fault(tmp_path, mutate, why):
    d = _definition(tmp_path)
    mutate(d)
    err = reg.service_definition_error("checker", d)
    assert err is not None and why in err, err


def test_two_services_on_one_box_port_are_refused(tmp_path):
    other = _definition(tmp_path)
    services = {"checker": _definition(tmp_path), "other": other}
    assert "'checker' and 'other' both use box port" in reg.service_port_clash(services, "checker")
    assert "'other' and 'checker'" in reg.service_port_clash(services, "other")
    other["rented"]["tunnel"]["box_port"] = BOX_PORT + 1
    assert reg.service_port_clash(services, "checker") is None
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    other["rented"]["tunnel"]["box_port"] = BOX_PORT
    _set_services(conn, services)
    assert "both use box port" in reg.services_error(conn, {"services": ["checker"]})


def test_a_bad_service_name_is_refused():
    assert "must match" in reg.service_definition_error("Checker", {})


@pytest.mark.parametrize("names,why", [
    ("checker", "list of service names"),
    (["checker", "checker"], "twice"),
    (["a", "b", "c", "d", "e"], "at most 4"),
    (["Bad_Name"], "must match"),
])
def test_the_structure_is_checked_without_a_registry(names, why):
    err = reg.services_error(None, {"services": names})
    assert err is not None and why in err, err


def test_a_hint_without_services_is_never_inspected():
    assert reg.services_error(None, {"box": "x"}) is None
    assert reg.services_error(None, '{"box": "x"}') is None
    assert reg.services_error(None, "{not json") is None      # no key: nothing to check


def test_the_names_are_checked_against_the_registry(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    assert "defined: none" in reg.services_error(conn, {"services": ["checker"]})
    _set_services(conn, {"checker": _definition(tmp_path)})
    assert reg.services_error(conn, {"services": ["checker"]}) is None
    assert "no service 'other'" in reg.services_error(conn, {"services": ["other"]})
    broken = _definition(tmp_path)
    broken.pop("owned")
    _set_services(conn, {"checker": broken})
    assert "setting is wrong" in reg.services_error(conn, {"services": ["checker"]})


def test_an_unparseable_setting_offers_nothing(tmp_path):
    conn = reg.connect(str(tmp_path / "runs.sqlite"))
    conn.execute("INSERT INTO settings(key, value) VALUES ('services', '[1,2')")
    conn.commit()
    assert reg.load_services(conn) == {}


def test_services_of_a_corrupt_hint_is_none():
    assert reg.services_of_hint('{"services": "checker"}') == []
    assert reg.services_of_hint("{broken services") == []
    assert reg.services_of_hint(None) == []
    assert reg.services_of_hint('{"services": ["checker"]}') == ["checker"]


# -- the job's side: manifest and runq ------------------------------------------------------------

BASE = {"manifest_version": jm.JOB_MANIFEST_VERSION, "run": ["python", "x.py"],
        "completion_artifact": "done.json"}


def test_the_manifest_carries_services_through_its_round_trip():
    m = jm.parse({**BASE, "services": ["checker"]})
    assert m.services == ["checker"]
    assert jm.parse(jm.to_dict(m)).services == ["checker"]
    assert "services" not in jm.to_dict(jm.parse(BASE))      # absent stays absent
    with pytest.raises(jm.JobManifestError, match="services"):
        jm.parse({**BASE, "services": "checker"})


def _resolve(tmp_path, services, *, define=True):
    runq = _load("runq", "fleet/runq.py")
    db = tmp_path / "runs.sqlite"
    conn = reg.connect(str(db))
    if define:
        _set_services(conn, {"checker": _definition(tmp_path)})
    conn.close()
    a = argparse.Namespace(est_minutes=5, vram_per_lane_gb=None, cores_per_lane=None, box=None,
                           colocate=None, force_box=False, db=str(db))
    return runq._manifest_est_and_hint(a, jm.parse({**BASE, "services": services}))


def test_runq_copies_the_jobs_services_onto_the_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNQ_TRANSPORT", "local")
    _, hint, err = _resolve(tmp_path, ["checker"])
    assert err is None and json.loads(hint) == {"services": ["checker"]}


def test_runq_refuses_a_service_the_site_does_not_define(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNQ_TRANSPORT", "local")
    _, hint, err = _resolve(tmp_path, ["checker"], define=False)
    assert hint is None and "no service 'checker'" in err


def test_a_job_without_services_has_no_services_on_its_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNQ_TRANSPORT", "local")
    _, hint, err = _resolve(tmp_path, [])
    assert err is None and hint is None


# -- the files: read, packed, never in a command line (inv. 3/4) ----------------------------------

def test_only_regular_files_with_sendable_names_are_read(tmp_path):
    d = tmp_path / "files"
    d.mkdir()
    (d / "api-key").write_bytes(SECRET)
    (d / ".hidden").write_bytes(b"x")
    (d / "sub").mkdir()
    (tmp_path / "elsewhere").write_bytes(b"outside")
    (d / "link").symlink_to(tmp_path / "elsewhere")
    os.mkfifo(d / "pipe")
    assert disp.read_service_files(str(d)) == {"api-key": SECRET}


@pytest.mark.parametrize("make,why", [
    (lambda d: (d / "service.json").write_bytes(b"{}"), "the name the service's values"),
    (lambda d: (d / "big").write_bytes(b"x" * (64 * 1024 + 1)), "larger than"),
    (lambda d: [(d / f"f{i}").write_bytes(b"x") for i in range(17)], "more than 16"),
])
def test_a_files_directory_past_a_limit_is_refused(tmp_path, make, why):
    d = tmp_path / "files"
    d.mkdir()
    make(d)
    with pytest.raises(disp.ServiceUnavailable, match=why):
        disp.read_service_files(str(d))


def test_a_missing_files_directory_is_refused(tmp_path):
    with pytest.raises(disp.ServiceUnavailable, match="cannot read"):
        disp.read_service_files(str(tmp_path / "absent"))


def test_the_tar_holds_each_file_and_the_values_at_mode_0600():
    members = _members(disp.service_tar({"api-key": SECRET}, {"url": "https://x"}))
    assert members["api-key"] == (0o600, SECRET)
    assert members["service.json"][0] == 0o600
    assert json.loads(members["service.json"][1]) == {"url": "https://x"}


def test_a_rented_box_gets_the_rented_half(tmp_path):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    (svc,) = plan["services"]
    members = _members(svc["tar"])
    assert svc["name"] == "checker" and svc["files"] == ["api-key", "ca.crt"]
    assert members["api-key"][1] == SECRET
    assert json.loads(members["service.json"][1])["url"] == f"https://127.0.0.1:{BOX_PORT}"


def test_an_owned_box_gets_the_owned_half(tmp_path):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path, owned=True)
    (svc,) = d._ship_prepare(task, inst, "example.com", 2222)["services"]
    members = _members(svc["tar"])
    assert members["api-key"][1] == b"the-home-key"
    assert json.loads(members["service.json"][1])["url"] == "https://192.168.0.10:18100"


def test_a_service_no_longer_defined_fails_the_task_at_ship(tmp_path):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path, define=False)
    assert d._ship_prepare(task, inst, "example.com", 2222) is None
    row = d.conn.execute("SELECT state FROM tasks WHERE id='SVC1'").fetchone()
    assert row["state"] == "task_failed"
    (ev,) = d.conn.execute("SELECT detail FROM events WHERE task_id='SVC1' AND "
                           "event='task_failed'").fetchall()
    assert "not defined in the `services` setting" in ev["detail"]


def test_a_service_that_cannot_be_given_raises_an_alert(tmp_path, capsys):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path, define=False)
    assert d._ship_prepare(task, inst, "example.com", 2222) is None
    assert "[ALERT]" in capsys.readouterr().err


def test_a_port_clash_fails_the_task_at_ship_and_opens_no_tunnel(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    task, inst = _asking_task(d, tmp_path)
    _set_services(d.conn, {"checker": _definition(tmp_path), "other": _definition(tmp_path)})
    d._reconcile_service_tunnels()
    assert popen.calls == []
    assert d._ship_prepare(task, inst, "example.com", 2222) is None
    (ev,) = d.conn.execute("SELECT detail FROM events WHERE task_id='SVC1' AND "
                           "event='task_failed'").fetchall()
    assert "both use box port" in ev["detail"]


def test_unreadable_files_fail_the_task_at_ship(tmp_path):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path)
    broken = _definition(tmp_path)
    broken["rented"]["files"] = str(tmp_path / "absent")
    _set_services(d.conn, {"checker": broken})
    assert d._ship_prepare(task, inst, "example.com", 2222) is None
    assert d.conn.execute("SELECT state FROM tasks WHERE id='SVC1'").fetchone()["state"] == "task_failed"


def test_the_files_go_over_stdin_after_apt_and_before_the_bundle(tmp_path):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    res = d._ship_io(plan, "example.com", 2222)
    assert res["ok"], res
    writes = [(c, kw) for c, kw in run.calls if c[0] == "ssh" and "tar -x" in c[-1]]
    (cmd, kw), = writes
    assert kw["input"] == plan["services"][0]["tar"]
    assert "~/spool/services/checker" in cmd[-1]
    kinds = ["write" if (c[0] == "ssh" and "tar -x" in c[-1]) else "bundle"
             for c, _ in run.calls
             if (c[0] == "ssh" and "tar -x" in c[-1]) or (c[0] == "rsync" and "bundle" in " ".join(c))]
    assert kinds[:2] == ["write", "bundle"], kinds              # the write comes before the bundle
    assert res["services_written"] == [("checker", ["api-key", "ca.crt"])]


def test_the_files_go_after_the_apt_step(tmp_path):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    plan["apt_pkgs"] = "jq"
    assert d._ship_io(plan, "example.com", 2222)["ok"]
    remote = run.remote()
    apt = next(i for i, c in enumerate(remote) if "apt-get install" in c)
    write = next(i for i, c in enumerate(remote) if "tar -x" in c)
    assert apt < write


def test_a_failed_apt_step_writes_no_files(tmp_path):
    run = _Run(rc_for=lambda cmd: 1 if (cmd[0] == "ssh" and "apt-get" in cmd[-1]) else 0)
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    plan["apt_pkgs"] = "jq"
    assert not d._ship_io(plan, "example.com", 2222)["ok"]
    assert not [c for c in run.remote() if "tar -x" in c]


def test_no_command_line_ever_carries_the_secret(tmp_path):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    d._apply_ship_io(task, inst, d._ship_io(plan, "example.com", 2222))
    secret = SECRET.decode()
    assert all(secret not in " ".join(map(str, c)) for c, _ in run.calls)
    dump = "\n".join(str(tuple(r)) for t in ("events", "settings", "tasks")
                     for r in d.conn.execute(f"SELECT * FROM {t}"))
    assert secret not in dump
    staged = [p for p in disp.EXPERIMENTS_ROOT.rglob("*") if p.is_file()]
    assert all(SECRET not in p.read_bytes() for p in staged)


@pytest.mark.parametrize("rc,reached", [(1, True), (2, True), (255, False), (124, False)])
def test_only_sshs_own_failure_counts_against_the_box_link(tmp_path, rc, reached):
    """A fault ON the box (a full disk, a directory that cannot be removed) fails the ship but is not a
    transport failure: it must not feed the proxy-to-direct switch or the quarantine."""
    run = _Run(rc_for=lambda cmd: rc if (cmd[0] == "ssh" and "tar -x" in cmd[-1]) else 0)
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    res = d._ship_io(d._ship_prepare(task, inst, "example.com", 2222), "example.com", 2222)
    assert not res["ok"] and "service checker" in res["fail"]
    assert res["ops"][-1] == (reached, True)


def test_a_failed_write_leaves_the_task_claimed(tmp_path):
    run = _Run(rc_for=lambda cmd: 1 if (cmd[0] == "ssh" and "tar -x" in cmd[-1]) else 0)
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    assert d._ship(task, inst) is False
    assert d.conn.execute("SELECT state FROM tasks WHERE id='SVC1'").fetchone()["state"] == "claimed"
    (ev,) = d.conn.execute("SELECT detail FROM events WHERE event='ship_failed'").fetchall()
    assert "service checker" in ev["detail"]


def test_a_failed_write_fails_the_ship_before_the_bundle(tmp_path):
    run = _Run(rc_for=lambda cmd: 1 if (cmd[0] == "ssh" and "tar -x" in cmd[-1]) else 0)
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    res = d._ship_io(d._ship_prepare(task, inst, "example.com", 2222), "example.com", 2222)
    assert not res["ok"] and "service checker" in res["fail"]
    assert not [c for c, _ in run.calls if c[0] == "rsync" and "bundle" in " ".join(c)]


def test_a_written_service_is_recorded_on_the_box(tmp_path):
    d = _dispatcher(tmp_path)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    d._apply_ship_io(task, inst, d._ship_io(plan, "example.com", 2222))
    assert d._service_files_row(1) == ["checker"]
    assert _events(d, "service_files_written") == [{"service": "checker",
                                                    "files": ["api-key", "ca.crt"]}]


def test_the_ship_records_the_files_before_it_sends_them(tmp_path):
    """Inv. 5/10: a ship whose outcome is never applied (a restart mid-ship), or that finds the task
    already delivered, still leaves the box's row naming the service, so the files are removed."""
    run = _Run(out_for=lambda cmd: "EXISTS\n" if (cmd[0] == "ssh" and "READY" in cmd[-1]) else "")
    d = _dispatcher(tmp_path, run=run)
    task, inst = _asking_task(d, tmp_path)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    assert d._service_files_row(1) == ["checker"]          # before any I/O
    res = d._ship_io(plan, "example.com", 2222)
    assert res["already"] and not [c for c in run.remote() if "tar -x" in c]
    d._apply_ship_io(task, inst, res)
    assert d._service_files_row(1) == ["checker"] and _events(d, "service_files_written") == []


def test_the_task_is_told_the_directory_and_nothing_else(tmp_path):
    d = _dispatcher(tmp_path)
    task, _ = _asking_task(d, tmp_path)
    env = d._build_task_json(task)["env"]
    assert env["FLEET_SERVICE_CHECKER"] == "~/spool/services/checker"
    assert SECRET.decode() not in json.dumps(env)


# -- asked for, or nothing (inv. 1) ---------------------------------------------------------------

def test_a_task_that_does_not_ask_gets_nothing(tmp_path):
    run, popen = _Run(), _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    task, inst = _asking_task(d, tmp_path, services=None)
    plan = d._ship_prepare(task, inst, "example.com", 2222)
    assert plan["services"] == []
    assert not any(k.startswith("FLEET_SERVICE_") for k in d._build_task_json(task)["env"])
    before = len(run.calls)
    d._reconcile_service_tunnels()
    assert popen.calls == [] and len(run.calls) == before      # the phase made no call at all
    d._ship_io(plan, "example.com", 2222)
    assert not [c for c, _ in run.calls if c[0] == "ssh" and "services" in c[-1]]


# -- the tunnel (inv. 6/8/9) ----------------------------------------------------------------------

def test_a_rented_box_holding_an_asking_task_gets_one_tunnel(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    (cmd, kw), = popen.calls
    assert cmd[0] == "ssh" and cmd[-1] == "root@example.com"
    assert cmd[cmd.index("-R") + 1] == f"127.0.0.1:{BOX_PORT}:{HOME}"
    for opt in ("ExitOnForwardFailure=yes", "ForwardAgent=no", "ForwardX11=no",
                "ControlMaster=no", "ServerAliveInterval=15"):
        assert opt in cmd
    assert "-N" in cmd and kw["start_new_session"] and kw["stdin"] is subprocess.DEVNULL
    assert _events(d, "service_tunnel_open")[0]["home"] == HOME
    pid_file = disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels" / "i1.checker.pid"
    assert json.loads(pid_file.read_text()) == {"pid": popen.procs[0].pid, "start": None,
                                                "forward": f"127.0.0.1:{BOX_PORT}:{HOME}"}
    assert (pid_file.parent.stat().st_mode & 0o777) == 0o700
    assert (pid_file.stat().st_mode & 0o777) == 0o600
    d._reconcile_service_tunnels()                    # a live tunnel is left alone
    assert len(popen.calls) == 1


def test_a_tunnel_that_died_is_logged_and_opened_again(tmp_path):
    run, popen = _Run(), _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    popen.procs[0].rc = 255
    d._reconcile_service_tunnels()
    assert len(popen.calls) == 2
    (down,) = _events(d, "service_tunnel_down")
    assert down["rc"] == 255
    assert not [c for c in run.remote() if "ss -Hltnp" in c]     # not refused: no cleanup


def test_a_refused_forward_ends_a_stale_sshd_listener_first(tmp_path):
    order = []
    run, popen = _Run(order=order), _Popen(order=order)
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    err = disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels" / "i1.checker.err"
    err.write_text(f"Error: remote port forwarding failed for listen port {BOX_PORT}\n")
    popen.procs[0].rc = 255
    d._reconcile_service_tunnels()
    (cleanup,) = [c for c in run.remote() if "ss -Hltnp" in c]
    assert f"src 127.0.0.1:{BOX_PORT}" in cleanup and "sshd" in cleanup
    assert len(popen.calls) == 2
    steps = [kind if kind == "open" else ("cleanup" if "ss -Hltnp" in what else "read")
             for kind, what in order]
    assert steps.index("cleanup") < len(steps) - 1 - steps[::-1].index("open")   # before the reopen
    assert steps[-1] == "open"


def test_the_tunnel_closes_when_no_open_task_asks(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='SVC1'")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert popen.procs[0].terminated
    assert _events(d, "service_tunnel_closed")[0]["service"] == "checker"
    assert not (disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels" / "i1.checker.pid").exists()


def test_a_changed_box_port_moves_the_tunnel(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    moved = _definition(tmp_path)
    moved["rented"]["tunnel"]["box_port"] = BOX_PORT + 1
    _set_services(d.conn, {"checker": moved})
    d._reconcile_service_tunnels()
    assert popen.procs[0].terminated
    assert popen.calls[1][0][popen.calls[1][0].index("-R") + 1] == f"127.0.0.1:{BOX_PORT + 1}:{HOME}"
    assert _events(d, "service_tunnel_closed")[0]["why"] == "its endpoint or its setting changed"


def _proc_net_tcp(*local):
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
    return head + "".join(f"   {i}: {a} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0\n"
                          for i, a in enumerate(local)) + disp.TUNNEL_READ_MARK + "\n"


def test_the_listeners_are_read_from_both_tables():
    v6 = lambda words: "".join(w for w in words)                        # noqa: E731
    text = "\n".join([
        "  sl  local_address rem_address   st",
        f"   0: 0100007F:{BOX_PORT:04X} 00000000:0000 0A",                       # 127.0.0.1
        f"   1: 3500007F:{BOX_PORT:04X} 00000000:0000 0A",                       # 127.0.0.53
        f"   2: 0500000A:{BOX_PORT:04X} 00000000:0000 01",                       # not LISTEN
        f"   3: {v6(['00000000'] * 3 + ['01000000'])}:{BOX_PORT:04X} 0 0A",       # ::1
        f"   4: {v6(['00000000'] * 4)}:{BOX_PORT:04X} 0 0A",                     # ::
        f"   5: {v6(['00000000', '00000000', 'FFFF0000', '0100007F'])}:{BOX_PORT:04X} 0 0A",   # ::ffff:127.0.0.1
        f"   6: {v6(['00000000', '00000000', 'FFFF0000', '0500000A'])}:{BOX_PORT:04X} 0 0A",   # ::ffff:10.0.0.5
        f"   7: {v6(['000080FE', '00000000', '00000000', '01000000'])}:{BOX_PORT:04X} 0 0A",   # fe80::1
        f"   8: 0100007F:{BOX_PORT + 1:04X} 00000000:0000 0A",                   # another port
    ])
    listen = disp.tunnel_listeners(text, BOX_PORT)
    assert listen == sorted(["127.0.0.1", "127.0.0.53", "::1", "::", "::ffff:127.0.0.1",
                             "::ffff:10.0.0.5", "fe80::1"])
    beyond = [a for a in listen if not disp.is_loopback_listener(a)]
    assert sorted(beyond) == sorted(["::", "::ffff:10.0.0.5", "fe80::1"])


PORT_HEX = f"{BOX_PORT:04X}"


def _listening(*local):
    return lambda cmd: _proc_net_tcp(*local) if (cmd[0] == "ssh" and "/proc/net/tcp" in cmd[-1]) else ""


def test_a_tunnel_listening_beyond_the_loopback_is_closed_and_kept_closed(tmp_path, capsys):
    run, popen = _Run(out_for=_listening(f"00000000:{PORT_HEX}", f"0100007F:{PORT_HEX}")), _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()                     # opened
    d._reconcile_service_tunnels()                     # read, found exposed, closed
    assert popen.procs[0].terminated
    (ev,) = _events(d, "service_tunnel_exposed")
    assert ev == {"service": "checker", "box_port": BOX_PORT, "listen": ["0.0.0.0"]}
    assert "[ALERT]" in capsys.readouterr().err
    d._reconcile_service_tunnels()
    assert len(popen.calls) == 1                       # not opened again on this box
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='SVC1'")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert d._tunnels_exposed == set()                 # forgotten once nothing there asks


def test_a_loopback_only_tunnel_is_read_once(tmp_path):
    run, popen = _Run(out_for=_listening(f"0100007F:{PORT_HEX}", f"0100007F:{BOX_PORT + 1:04X}",
                                         f"00000000:{BOX_PORT + 1:04X}")), _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    for _ in range(3):
        d._reconcile_service_tunnels()
    assert not popen.procs[0].terminated
    assert _events(d, "service_tunnel_checked") == [{"service": "checker", "listen": ["127.0.0.1"]}]
    assert len([c for c in run.remote() if "/proc/net/tcp" in c]) == 1


@pytest.mark.parametrize("answer", [
    _proc_net_tcp(),                       # read, and nothing listens on the port
    "",                                    # /proc/net/tcp could not be read: no mark
])
def test_a_live_tunnel_never_seen_listening_is_closed_on_the_second_read(tmp_path, capsys, answer):
    """Inv. 6a fails closed: ExitOnForwardFailure=yes means a tunnel alive a pass later had its
    forward taken, so a listener nobody can see is a blind read, not a safe port."""
    run = _Run(out_for=lambda cmd: answer if (cmd[0] == "ssh" and "/proc/net/tcp" in cmd[-1]) else "")
    popen = _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()                     # opened
    d._reconcile_service_tunnels()                     # first read: nothing seen, kept
    assert not popen.procs[0].terminated
    d._reconcile_service_tunnels()                     # second read: closed
    assert popen.procs[0].terminated
    (ev,) = _events(d, "service_tunnel_exposed")
    assert ev == {"service": "checker", "box_port": BOX_PORT, "listen": []}
    assert "could not be seen listening" in capsys.readouterr().err
    d._reconcile_service_tunnels()
    assert len(popen.calls) == 1


def test_a_read_that_ssh_could_not_make_is_tried_again_without_counting(tmp_path):
    run = _Run(rc_for=lambda cmd: 255 if (cmd[0] == "ssh" and "/proc/net/tcp" in cmd[-1]) else 0)
    popen = _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    for _ in range(5):
        d._reconcile_service_tunnels()
    assert not popen.procs[0].terminated
    assert len([c for c in run.remote() if "/proc/net/tcp" in c]) == 4
    assert not _events(d, "service_tunnel_exposed")


def test_an_exposure_is_held_while_a_task_asks_even_if_the_setting_breaks(tmp_path):
    run, popen = _Run(out_for=_listening(f"00000000:{PORT_HEX}")), _Popen()
    d = _dispatcher(tmp_path, run=run, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    d._reconcile_service_tunnels()
    assert d._tunnels_exposed == {(1, "checker")}
    _set_services(d.conn, {"checker": _definition(tmp_path), "other": _definition(tmp_path)})
    d._reconcile_service_tunnels()                     # a port clash: not wanted, still asked
    _set_services(d.conn, {"checker": _definition(tmp_path)})
    d._reconcile_service_tunnels()
    assert d._tunnels_exposed == {(1, "checker")} and len(popen.calls) == 1


def test_a_pid_file_that_could_not_be_written_is_logged_and_written_again(tmp_path, monkeypatch):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    real = disp._write_private
    monkeypatch.setattr(disp, "_write_private",
                        lambda path, data: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    d._reconcile_service_tunnels()
    assert _events(d, "service_tunnel_unrecorded") == [{"service": "checker",
                                                       "why": "No space left on device"}]
    assert (1, "checker") in d._tunnels                # held all the same
    pid_file = disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels" / "i1.checker.pid"
    assert not pid_file.exists()
    monkeypatch.setattr(disp, "_write_private", real)
    d._reconcile_service_tunnels()
    assert json.loads(pid_file.read_text())["pid"] == popen.procs[0].pid
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='SVC1'")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert popen.procs[0].terminated


def test_one_boxs_fault_does_not_stop_the_others(tmp_path, monkeypatch, phase_faults):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    _tmod._seed_instance_and_task(d.conn, task_id="SVC2", instance_id=2, entrypoint="smoke")
    d.conn.execute("UPDATE tasks SET resource_hint_json=? WHERE id='SVC2'",
                   (json.dumps({"services": ["checker"]}),))
    d.conn.commit()
    real = d._open_tunnel
    opened = []

    def open_tunnel(key, *args):
        if not opened:
            opened.append(key)
            raise RuntimeError("first box breaks")
        return real(key, *args)

    monkeypatch.setattr(d, "_open_tunnel", open_tunnel)
    d._reconcile_service_tunnels()
    assert len(phase_faults) == 1 and "first box breaks" in phase_faults[0]
    assert len(popen.calls) == 1                       # the other box's tunnel opened all the same
    phase_faults.clear()
    d._reconcile_service_tunnels()                     # and the next pass opens the first
    assert len(popen.calls) == 2


def test_a_changed_endpoint_moves_the_tunnel(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path)
    d._reconcile_service_tunnels()
    d.conn.execute("UPDATE instances SET ssh_host='example.org' WHERE id=1")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert popen.procs[0].terminated and popen.calls[1][0][-1] == "root@example.org"


def test_an_owned_box_gets_no_tunnel(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path, owned=True)
    d._reconcile_service_tunnels()
    assert popen.calls == []


def test_an_undefined_service_opens_nothing(tmp_path):
    popen = _Popen()
    d = _dispatcher(tmp_path, popen=popen)
    _asking_task(d, tmp_path, define=False)
    d._reconcile_service_tunnels()
    assert popen.calls == []


def test_the_phase_never_raises(tmp_path, monkeypatch, phase_faults):
    d = _dispatcher(tmp_path)
    monkeypatch.setattr(disp.registry_db, "load_services",
                        lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
    d._reconcile_service_tunnels()
    (row,) = d.conn.execute("SELECT detail FROM events WHERE event='service_tunnels_failed'")
    assert "boom" in row["detail"]
    phase_faults.clear()


def test_dry_run_runs_no_tunnel_phase(tmp_path, monkeypatch):
    run, popen = _Run(), _Popen()
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run, popen=popen,
                        dry_run=True)
    _asking_task(d, tmp_path)
    monkeypatch.setattr(d, "do_reconcile", lambda *a, **k: None)
    monkeypatch.setattr(d, "_place_queue", lambda *a, **k: None)
    monkeypatch.setattr(d, "_reconcile_service_tunnels",
                        lambda: (_ for _ in ()).throw(AssertionError("ran in --dry-run")))
    d.poll_once()
    assert popen.calls == []


FORWARD = "127.0.0.1:1:h:1"


def _fake_tunnel(forward=FORWARD):
    """A real process whose command line is a tunnel's (argv[0] `ssh`), in its own session."""
    return subprocess.Popen(["ssh", "-c", "import time; time.sleep(60)", "-N", "-o",
                             "ExitOnForwardFailure=yes", "-o", "ControlPath=none", "-R", forward],
                            executable=sys.executable, start_new_session=True)


def _record(tdir, name, pid, *, start=None, forward=FORWARD):
    if start is None:
        start = disp._proc_stat(pid)[1]
    (tdir / name).write_text(json.dumps({"pid": pid, "start": start, "forward": forward}) + "\n")


def test_a_restarted_dispatcher_ends_the_tunnels_it_no_longer_holds(tmp_path):
    tdir = disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels"
    tdir.mkdir(parents=True)
    tunnel, other = _fake_tunnel(), _fake_tunnel()
    plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _record(tdir, "i1.checker.pid", tunnel.pid)
        _record(tdir, "i2.checker.pid", other.pid, forward="127.0.0.1:2:h:1")   # not its forward
        _record(tdir, "i3.checker.pid", plain.pid)                              # not a tunnel
        d = _dispatcher(tmp_path)
        d._reconcile_service_tunnels()
        assert tunnel.wait(timeout=10) == -signal.SIGTERM
        time.sleep(0.2)
        assert other.poll() is None and plain.poll() is None    # never signalled
        assert list(tdir.glob("*.pid")) == []
    finally:
        for p in (tunnel, other, plain):
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.mark.parametrize("record", [
    lambda pid, start: {"pid": pid, "start": start + 1, "forward": FORWARD},   # the pid was reused
    lambda pid, start: {"pid": pid, "start": None, "forward": FORWARD},
    lambda pid, start: {"pid": str(pid), "start": start, "forward": FORWARD},
    lambda pid, start: pid,                                                    # the old bare pid
])
def test_a_pid_file_that_does_not_prove_its_tunnel_signals_nothing(tmp_path, record):
    tdir = disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels"
    tdir.mkdir(parents=True)
    tunnel = _fake_tunnel()
    try:
        start = disp._proc_stat(tunnel.pid)[1]
        (tdir / "i1.checker.pid").write_text(json.dumps(record(tunnel.pid, start)))
        _dispatcher(tmp_path)._reconcile_service_tunnels()
        time.sleep(0.2)
        assert tunnel.poll() is None
        assert list(tdir.glob("*.pid")) == []
    finally:
        tunnel.kill()
        tunnel.wait()


def test_a_tunnel_opened_for_real_is_ended_by_the_next_dispatcher(tmp_path, monkeypatch):
    """Inv. 8 end to end: a real process through `_open_tunnel` (its pid file holds the start time the
    kernel gives it), then a NEW dispatcher's first pass ends it."""
    monkeypatch.setattr(disp, "tunnel_cmd", lambda host, port, box_port, home: [
        "ssh", "-c", "import time; time.sleep(60)", "-N", "-o", "ExitOnForwardFailure=yes",
        "-o", "ControlPath=none", "-R", disp.tunnel_forward(box_port, home)])
    procs = []

    def popen(cmd, **kwargs):
        procs.append(subprocess.Popen(cmd, executable=sys.executable, **kwargs))
        return procs[-1]

    try:
        first = _dispatcher(tmp_path, popen=popen)
        _asking_task(first, tmp_path)
        first._reconcile_service_tunnels()
        (proc,) = procs
        record = json.loads((disp.EXPERIMENTS_ROOT / ".dispatcher" / "tunnels" / "i1.checker.pid").read_text())
        assert record["pid"] == proc.pid and record["start"] == disp._proc_stat(proc.pid)[1]
        second = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Run(), vastai_run=_Run(),
                                 popen=_Popen())
        second._sweep_tunnel_pids()
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


def _stubborn():
    """A process that ignores SIGTERM, in its own session, once it says it is ready."""
    proc = subprocess.Popen([sys.executable, "-c", "import signal, time; "
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
                             "time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
    assert proc.stdout.readline().strip() == b"ready"
    return proc


def test_a_tunnel_that_ignores_sigterm_is_killed_after_the_grace(monkeypatch):
    monkeypatch.setattr(disp, "TUNNEL_STOP_GRACE_S", 0.3)
    proc = _stubborn()
    try:
        disp._stop_tunnel_process(proc)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_recorded_tunnel_that_ignores_sigterm_is_killed_after_the_grace(monkeypatch):
    monkeypatch.setattr(disp, "TUNNEL_STOP_GRACE_S", 0.3)
    proc = _stubborn()
    try:
        disp._end_recorded_tunnel(proc.pid, disp._proc_stat(proc.pid)[1])
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_the_phase_runs_after_consolidation_and_before_the_ship_and_under_a_hold(tmp_path, monkeypatch):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    calls = []
    for name in ("_sync_ssh_config", "_refresh_heartbeats", "_advance_provisioning",
                 "_ingest_and_complete", "_signal_cancels", "_signal_drain", "_reap_over_capacity",
                 "_push_capacity_schedules", "_consume_box_probes", "_consume_box_requests",
                 "_measure_box_resources", "_book_live_costs", "_teardown_idle", "_gc_ship_staging",
                 "_gc_pull_temps", "_gc_code_snapshots", "_refresh_workers", "do_reconcile", "_place_queue"):
        monkeypatch.setattr(d, name, lambda *a, **k: None)
    for name in ("_consolidate", "_reconcile_service_tunnels", "_ship_all"):
        monkeypatch.setattr(d, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(d, "_check_data_root", lambda: None)
    d._space_hold = False
    d.poll_once()
    assert calls == ["_consolidate", "_reconcile_service_tunnels", "_ship_all"]
    calls.clear()
    d._space_hold = True
    d.poll_once()
    assert calls == ["_reconcile_service_tunnels"]


def test_closing_a_tunnel_ends_its_whole_group(tmp_path):
    """Inv. 6: SIGTERM goes to the group the tunnel leads, so a child it started dies with it."""
    child_pid = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(child_pid)!r}, 'w').write(str(c.pid)); time.sleep(60)")
    leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not child_pid.exists() or not child_pid.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        child = int(child_pid.read_text())
        disp._stop_tunnel_process(leader)
        assert leader.returncode is not None
        deadline = time.monotonic() + 10
        while disp._proc_stat(child) is not None and disp._proc_stat(child)[0] not in ("Z", "X"):
            assert time.monotonic() < deadline, "the tunnel's child outlived it"
            time.sleep(0.05)
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()


# -- the files leave the box (inv. 10) ------------------------------------------------------------

def test_the_files_leave_a_box_with_the_last_asking_task(tmp_path):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    _asking_task(d, tmp_path)
    d._note_service_files(1, "checker")
    d._reconcile_service_tunnels()
    assert not [c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='SVC1'")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert [c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]
    assert _events(d, "service_files_removed") == [{"service": "checker"}]
    assert d._service_files_row(1) == []


def test_a_removal_that_fails_is_tried_again(tmp_path):
    run = _Run(rc_for=lambda cmd: 255 if (cmd[0] == "ssh" and "rm -rf" in cmd[-1]) else 0)
    d = _dispatcher(tmp_path, run=run)
    _asking_task(d, tmp_path, services=None)
    d._note_service_files(1, "checker")
    d._reconcile_service_tunnels()
    removals = [c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]
    assert len(removals) == 1
    assert d._service_files_row(1) == ["checker"] and not _events(d, "service_files_removed")
    d._reconcile_service_tunnels()
    assert len([c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]) == 2


@pytest.mark.parametrize("gone", ["destroyed", "lost for two days", "missing"])
def test_a_box_that_is_gone_loses_its_row_without_a_call(tmp_path, gone):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    _asking_task(d, tmp_path, services=None)
    d._note_service_files(1, "checker")
    if gone == "missing":
        d.conn.execute("DELETE FROM tasks")
        d.conn.execute("DELETE FROM instances WHERE id=1")
    elif gone == "destroyed":
        d.conn.execute("UPDATE instances SET state='destroyed' WHERE id=1")
    else:
        d.conn.execute("UPDATE instances SET state='lost', destroyed_at=? WHERE id=1",
                       (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400)),))
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert d._service_files_row(1) == [] and run.calls == []


def test_a_box_just_lost_keeps_its_row_for_when_it_comes_back(tmp_path):
    """`lost` is most often a transient miss in the provider's listing, and the box is adopted back
    `live` minutes later, still holding the files: so the row is kept, and the removal follows then."""
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    _asking_task(d, tmp_path, services=None)
    d._note_service_files(1, "checker")
    d.conn.execute("UPDATE instances SET state='lost', destroyed_at=? WHERE id=1", (reg.now_iso(),))
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert d._service_files_row(1) == ["checker"] and run.calls == []
    d.conn.execute("UPDATE instances SET state='live', destroyed_at=NULL WHERE id=1")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert [c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]


def test_an_unreachable_box_keeps_its_row_until_it_is_back(tmp_path):
    run = _Run()
    d = _dispatcher(tmp_path, run=run)
    _asking_task(d, tmp_path, services=None)
    d._note_service_files(1, "checker")
    d.conn.execute("UPDATE instances SET state='unreachable' WHERE id=1")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert d._service_files_row(1) == ["checker"] and run.calls == []
    d.conn.execute("UPDATE instances SET state='live' WHERE id=1")
    d.conn.commit()
    d._reconcile_service_tunnels()
    assert [c for c in run.remote() if c.startswith("rm -rf ~/spool/services/checker")]
    assert d._service_files_row(1) == []


# -- the box-side commands, run for real -----------------------------------------------------------

def _box_shell(home, cmd, data=b""):
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    return subprocess.run(["sh", "-c", cmd], input=data, capture_output=True, env=env, timeout=30)


def test_the_write_command_builds_the_directory_and_replaces_the_old_one(tmp_path):
    home = tmp_path / "box"
    home.mkdir()
    first = disp.service_tar({"api-key": SECRET, "old-file": b"x"}, {"url": "a"})
    assert _box_shell(home, disp.service_write_cmd("checker"), first).returncode == 0
    second = disp.service_tar({"api-key": b"new"}, {"url": "b"})
    out = _box_shell(home, disp.service_write_cmd("checker"), second)
    assert out.returncode == 0, out.stderr
    d = home / "spool" / "services" / "checker"
    assert sorted(p.name for p in d.iterdir()) == ["api-key", "service.json"]
    assert (d / "api-key").read_bytes() == b"new"
    assert (d.stat().st_mode & 0o777) == 0o700
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in d.iterdir())
    assert sorted(p.name for p in d.parent.iterdir()) == ["checker"]       # no .new, no .old
    assert _box_shell(home, disp.service_remove_cmd("checker")).returncode == 0
    assert not d.exists()


def test_a_rewrite_keeps_the_directory_and_follows_no_link_in_it(tmp_path):
    home = tmp_path / "box"
    home.mkdir()
    assert _box_shell(home, disp.service_write_cmd("checker"),
                      disp.service_tar({"api-key": b"one"}, {})).returncode == 0
    d = home / "spool" / "services" / "checker"
    inode = d.stat().st_ino
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (d / "api-key").unlink()
    (d / "api-key").symlink_to(elsewhere)            # a link to a directory where a file goes
    (d / "ca.crt").mkdir()                           # and a directory
    out = _box_shell(home, disp.service_write_cmd("checker"),
                     disp.service_tar({"api-key": b"two", "ca.crt": b"cert"}, {}))
    assert out.returncode == 0, out.stderr
    assert d.stat().st_ino == inode                  # the same directory: it never went away
    assert (d / "api-key").read_bytes() == b"two" and not (d / "api-key").is_symlink()
    assert (d / "ca.crt").read_bytes() == b"cert"
    assert list(elsewhere.iterdir()) == []
    assert sorted(p.name for p in d.parent.iterdir()) == ["checker"]


def test_a_rewrite_removes_a_stale_dotfile_too(tmp_path):
    home = tmp_path / "box"
    home.mkdir()
    assert _box_shell(home, disp.service_write_cmd("checker"),
                      disp.service_tar({"api-key": b"one"}, {})).returncode == 0
    d = home / "spool" / "services" / "checker"
    (d / ".planted").write_bytes(b"x")
    (d / "..odd").write_bytes(b"x")
    assert _box_shell(home, disp.service_write_cmd("checker"),
                      disp.service_tar({"api-key": b"two"}, {})).returncode == 0
    assert sorted(p.name for p in d.iterdir()) == ["api-key", "service.json"]


def test_a_link_at_the_services_root_is_replaced_not_followed(tmp_path):
    home = tmp_path / "box"
    (home / "spool").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "spool" / "services").symlink_to(elsewhere)
    out = _box_shell(home, disp.service_write_cmd("checker"), disp.service_tar({"api-key": b"k"}, {}))
    assert out.returncode == 0, out.stderr
    root = home / "spool" / "services"
    assert not root.is_symlink() and (root / "checker" / "api-key").read_bytes() == b"k"
    assert list(elsewhere.iterdir()) == []


def test_a_link_at_the_directorys_name_is_replaced_not_followed(tmp_path):
    home = tmp_path / "box"
    (home / "spool" / "services").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "spool" / "services" / "checker").symlink_to(elsewhere)
    out = _box_shell(home, disp.service_write_cmd("checker"), disp.service_tar({"api-key": b"k"}, {}))
    assert out.returncode == 0, out.stderr
    d = home / "spool" / "services" / "checker"
    assert not d.is_symlink() and (d / "api-key").read_bytes() == b"k"
    assert list(elsewhere.iterdir()) == []


def test_the_listener_cleanup_never_fails_a_box(tmp_path):
    assert _box_shell(tmp_path, disp.tunnel_listener_cleanup_cmd(BOX_PORT)).returncode == 0


def test_the_listener_cleanup_ends_a_connections_process_and_nothing_else(tmp_path):
    """The cleanup run for real: a stand-in `ss` names listeners on the port, each a process whose
    WHOLE command line is the title sshd gives it (a shell waiting on its stdin, under that name).
    A `-N` tunnel opens no session, so the process holding its forward is `sshd: USER`; those, and a
    session's `sshd: USER@...`, are signalled. sshd's own listener, a privileged or post-auth monitor,
    another user's connection and a process of another name are not."""
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    kill = [f"sshd: {user}", f"sshd-session: {user}", f"sshd: {user}@notty"]
    keep = [f"sshd: {user} [priv]", f"sshd-session: {user} [postauth]",
            "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups", "sshd: someone-else", "python3"]
    titles = kill + keep
    procs = [subprocess.Popen([t], executable="/bin/sh", stdin=subprocess.PIPE) for t in titles]
    deadline = time.monotonic() + 10
    while any(Path(f"/proc/{p.pid}/cmdline").read_bytes().rstrip(b"\0") != t.encode()
              for p, t in zip(procs, titles)):
        assert time.monotonic() < deadline, "the stand-ins never took their titles"
        time.sleep(0.05)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    users = ",".join(f'("sshd",pid={p.pid},fd=9)' for p in procs)
    stub = bindir / "ss"
    stub.write_text("#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" > {tmp_path / 'ss.args'}\n"
                    f"echo 'LISTEN 0 128 127.0.0.1:{BOX_PORT} 0.0.0.0:* users:({users})'\n")
    stub.chmod(0o755)
    try:
        env = {"HOME": str(tmp_path), "PATH": f"{bindir}:{os.environ['PATH']}"}
        out = subprocess.run(["sh", "-c", disp.tunnel_listener_cleanup_cmd(BOX_PORT)],
                             capture_output=True, env=env, timeout=30)
        assert out.returncode == 0, out.stderr
        assert (tmp_path / "ss.args").read_text().split() == ["-Hltnp", "src", f"127.0.0.1:{BOX_PORT}"]
        assert [p.wait(timeout=10) for p in procs[:len(kill)]] == [-signal.SIGTERM] * len(kill)
        time.sleep(0.2)
        assert [p.poll() for p in procs[len(kill):]] == [None] * len(keep)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_stdin_reaches_a_real_process():
    out = disp._run_input_or_timeout(subprocess.run, ["cat"], SECRET, 10)
    assert out.returncode == 0 and out.stdout == SECRET.decode()
