"""Operator verbs over the API — remote-submit M2 (inv. 20).

The API has no network, so it writes a one-shot row and the dispatcher performs the verb. These pin
the half that matters: the request is consumed exactly once, the verb is `box_pause`'s OWN function
(so CLI and API cannot drift into two meanings of "pause"), and a failure cannot break the poll.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fleet"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")


class _Proc:
    returncode, stdout, stderr = 0, "", ""


def _mk(tmp_path, state="live"):
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=lambda *a, **k: _Proc(),
                        vastai_run=lambda *a, **k: _Proc())
    now = disp.registry_db.now_iso()
    d.conn.execute(
        "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
        "slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (-2, "desktop", now, state, 0.0, "westdesktop", 2222, 4, now, "owned"))
    d.conn.commit()
    return d


def _request(d, verb, iid=-2):
    d.conn.execute("INSERT INTO settings(key, value) VALUES (?,?)",
                   (f"{verb}_request_i{iid}", json.dumps({"by": "operator"})))
    d.conn.commit()


def _events(d):
    return [json.loads(r["detail"]) for r in
            d.conn.execute("SELECT detail FROM events WHERE event='box_request' ORDER BY seq")]


def _settings(d, like):
    return [r["key"] for r in d.conn.execute(
        "SELECT key FROM settings WHERE key LIKE ?", (like,))]


def test_a_hold_request_holds_the_box(tmp_path):
    d = _mk(tmp_path)
    _request(d, "hold")
    d._consume_box_requests()
    (state,) = d.conn.execute("SELECT state FROM instances WHERE id=-2").fetchone()
    assert state == "paused"
    assert _events(d)[0]["ok"] is True


def test_a_resume_request_brings_it_back(tmp_path):
    d = _mk(tmp_path, state="paused")
    _request(d, "resume")
    d._consume_box_requests()
    (state,) = d.conn.execute("SELECT state FROM instances WHERE id=-2").fetchone()
    assert state == "live"


def test_the_request_is_consumed_exactly_once(tmp_path):
    d = _mk(tmp_path)
    _request(d, "hold")
    d._consume_box_requests()
    assert _settings(d, "%_request_i%") == []
    d._consume_box_requests()
    assert len(_events(d)) == 1


def test_a_request_for_an_unknown_box_is_reported_not_crashed(tmp_path):
    d = _mk(tmp_path)
    _request(d, "hold", iid=-99)
    d._consume_box_requests()
    assert _events(d)[0]["error"] == "no such instance"


def test_a_failing_verb_cannot_break_the_poll(tmp_path, monkeypatch):
    """This runs inside `poll_once`; an exception here would cost the whole cycle."""
    d = _mk(tmp_path)
    import box_pause
    monkeypatch.setattr(box_pause, "cmd_hold",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _request(d, "hold")
    d._consume_box_requests()
    ev = _events(d)[0]
    assert "RuntimeError" in ev["error"]
    assert _settings(d, "%_request_i%") == []


def test_the_verb_is_box_pauses_own_function(tmp_path, monkeypatch):
    """⛔ The logic is NOT duplicated: the dispatcher calls box_pause.cmd_* verbatim, so the CLI and
    the API cannot drift into two different meanings of "pause"."""
    d = _mk(tmp_path)
    import box_pause
    called = []
    monkeypatch.setattr(box_pause, "cmd_drain", lambda conn, inst: called.append(inst["id"]) or 0)
    _request(d, "drain")
    d._consume_box_requests()
    assert called == [-2]
