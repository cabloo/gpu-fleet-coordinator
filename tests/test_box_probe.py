"""On-demand box probe — `docs/specs/remote-submit.spec.md` inv. 19a/19b (draft, branch
`spec/remote-submit`; the implementation landed first as reusable observability).

The fixture set the spec names. The point of this feature is the FAILURE path: a failed measurement
logs nothing (task-dispatcher 23-Q1), so the probe's whole job is to record WHY a box is unreachable,
and `Permission denied (publickey)` / `Could not resolve hostname` / a timeout must be told apart.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")

PROBE_OK = ("NPROC 20\n"
            "LOAD 0.30 0.20 0.10 1/900 123\n"
            "MEM 32000 24000\n"
            "GPU 8192, 512, 0, 0, NVIDIA GeForce RTX 3070 Ti\n")


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Run:
    """A subprocess-shaped fake that answers every ssh with one canned result."""

    def __init__(self, proc):
        self.proc, self.calls = proc, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self.proc


def _mk(tmp_path, proc, state="paused", host="westdesktop", port=2222):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=_Run(proc), vastai_run=_Run(_Proc(1)))
    now = disp.registry_db.now_iso()
    d.conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
        "slots_total, hard_cap_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (-2, "desktop", now, state, 0.0, host, port, 4, now))
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('probe_request_i-2', '{}')")
    d.conn.commit()
    return d


def _probe_events(d):
    return [json.loads(r["detail"]) for r in
            d.conn.execute("SELECT detail FROM events WHERE event='box_probe' ORDER BY seq")]


def _requests(d):
    return [r["key"] for r in d.conn.execute(
        "SELECT key FROM settings WHERE key LIKE 'probe_request_i%'")]


def test_a_reachable_box_records_its_measurement(tmp_path):
    d = _mk(tmp_path, _Proc(0, PROBE_OK))
    d._consume_box_probes()
    (ev,) = _probe_events(d)
    assert ev["reachable"] is True
    assert ev["endpoint"] == "westdesktop:2222"
    assert ev["measured"]["cores"] == 20
    assert ev["measured"]["vram_total_gb"] == 8.0


def test_the_request_is_consumed_exactly_once(tmp_path):
    """One shot, like `worker_roll_now`: a probe can never become a standing behaviour, and a box
    that hangs cannot re-probe every poll."""
    d = _mk(tmp_path, _Proc(0, PROBE_OK))
    d._consume_box_probes()
    assert _requests(d) == []
    d._consume_box_probes()
    assert len(_probe_events(d)) == 1


def test_an_unreachable_box_records_the_ssh_stderr(tmp_path):
    """23-Q1: the measure loop drops this on the floor. The stderr tail IS the diagnosis."""
    d = _mk(tmp_path, _Proc(255, "", "root@westdesktop: Permission denied (publickey).\n"))
    d._consume_box_probes()
    (ev,) = _probe_events(d)
    assert ev["reachable"] is False
    assert "Permission denied (publickey)" in ev["error"]
    assert ev["rc"] == 255


def test_an_unresolvable_host_is_distinguishable_from_a_refused_key(tmp_path):
    d = _mk(tmp_path, _Proc(255, "", "ssh: Could not resolve hostname westdesktop\n"))
    d._consume_box_probes()
    (ev,) = _probe_events(d)
    assert "Could not resolve hostname" in ev["error"]


def test_a_probe_that_succeeds_but_cannot_be_parsed_is_not_reachable(tmp_path):
    """A driver shouting prose at stdout is not a measurement."""
    d = _mk(tmp_path, _Proc(0, "Failed to initialize NVML: GPU access blocked\n"))
    d._consume_box_probes()
    (ev,) = _probe_events(d)
    assert ev["reachable"] is False
    assert "unparseable" in ev["error"]


def test_it_never_resumes_a_paused_box(tmp_path):
    """Diagnostic only: probing a held box must not hand it back to the packer."""
    d = _mk(tmp_path, _Proc(0, PROBE_OK), state="paused")
    d._consume_box_probes()
    (state,) = d.conn.execute("SELECT state FROM instances WHERE id=-2").fetchone()
    assert state == "paused"


def test_it_does_not_push_the_box_onto_its_direct_endpoint_fallback(tmp_path):
    """A probe records nothing in the connection tracker — three failed diagnostics must not flip a
    box to the `vastai ssh-url` fallback (invariant 9a), which for an owned box resolves to nothing."""
    d = _mk(tmp_path, _Proc(255, "", "timed out"))
    for _ in range(3):
        d.conn.execute("INSERT OR REPLACE INTO settings(key, value) "
                       "VALUES ('probe_request_i-2', '{}')")
        d.conn.commit()
        d._consume_box_probes()
    assert d.tracker.is_direct(-2) is False
    assert d.tracker.consecutive_fails(-2) == 0


def test_an_unknown_instance_is_reported_not_crashed(tmp_path):
    d = _mk(tmp_path, _Proc(0, PROBE_OK))
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('probe_request_i-99', '{}')")
    d.conn.commit()
    d._consume_box_probes()
    errs = [e for e in _probe_events(d) if e.get("instance") == -99]
    assert errs and errs[0]["error"] == "no such instance"


def test_runq_box_probe_goes_through_the_api_when_the_root_is_read_only(tmp_path, monkeypatch):
    """On the API transport the registry is READ-ONLY (remote-submit inv. 20), and `runq box probe`
    used to write `probe_request_i<id>` straight into it and crash with `attempt to write a readonly
    database` (2026-09-25, first probe of gpudesktop). It must ask the coordinator instead."""
    import argparse
    runq = _load("runq", "fleet/runq.py")
    db = str(tmp_path / "runs.sqlite")
    conn = disp.registry_db.connect(db)
    now = disp.registry_db.now_iso()
    conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
                 "slots_total, hard_cap_at) VALUES (-4,'gpudesktop',?,'live',0,'gpudesktop',"
                 "2222,24,?)", (now, now))
    conn.commit()
    asked = []
    monkeypatch.setattr(runq.api_client, "enabled", lambda: True)
    monkeypatch.setattr(runq.api_client.ApiClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(runq.api_client.ApiClient, "box",
                        lambda self, box, verb: asked.append((box, verb)) or {"ok": True})
    rc = runq.cmd_box(argparse.Namespace(box="gpudesktop", wait=False, db=db, box_cmd="probe"))
    assert rc == 0 and asked == [("gpudesktop", "probe")]
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'probe_request_%'"
                        ).fetchone()[0] == 0


def test_a_probe_never_breaks_the_poll(tmp_path, monkeypatch):
    """`_consume_box_probes` runs inside `poll_once`; an exception here would cost the whole cycle."""
    d = _mk(tmp_path, _Proc(0, PROBE_OK))
    monkeypatch.setattr(disp, "ssh_run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    d._consume_box_probes()
    (ev,) = _probe_events(d)
    assert ev["reachable"] is False
    assert "OSError" in ev["error"]
    assert _requests(d) == []


def test_the_ssh_config_is_re_synced_BEFORE_the_probe_connects(tmp_path):
    """19a-1. `config.fleet` is written once, at the START of a poll cycle, and a probe is asked for
    exactly when a box's address has just changed. A probe served in the cycle already running read
    the NEW host off the registry, found no `Host` block for it, and reported `Permission denied
    (publickey)` for a box whose key was fine (`desktop`, 2026-10-03: re-pointed at 18:18:00, probed
    at 18:18:24 inside a cycle that began at 18:17:09)."""
    d = _mk(tmp_path, _Proc(0, PROBE_OK), host="old-desktop.lan")
    order = []
    d._sync_ssh_config = lambda: order.append("sync")
    real_run = d.run
    d.run = lambda cmd, **kw: order.append("ssh") or real_run(cmd, **kw)
    d._consume_box_probes()
    assert order == ["sync", "ssh"]
    assert _probe_events(d)[0]["endpoint"] == "old-desktop.lan:2222"


def test_no_probe_request_means_no_ssh_config_rewrite(tmp_path):
    """The sync belongs to serving a probe; an idle poll must not grow a second config write."""
    d = _mk(tmp_path, _Proc(0, PROBE_OK))
    d.conn.execute("DELETE FROM settings WHERE key LIKE 'probe_request_i%'")
    d.conn.commit()
    calls = []
    d._sync_ssh_config = lambda: calls.append("sync")
    d._consume_box_probes()
    assert calls == [] and _probe_events(d) == []
