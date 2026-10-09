"""The registry owns which ssh identity reaches a box — remote-submit inv. 22a.

The bug this prevents, measured 2026-09-17: the key was chosen by a `Host` pattern in a root-owned
file the registry knew nothing about, so re-pointing owned box -2 to its real address matched no
pattern, ssh fell back to a default identity, and every connection failed `Permission denied
(publickey)` with a perfectly good key on the box.
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


def _mk(tmp_path, monkeypatch, boxes, keys_present=("fleet_ed25519",), settings=None):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    for k in keys_present:
        (home / ".ssh" / k).write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("COORD_ROLE", "live")   # only the live daemon writes ~/.ssh
    d = disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=lambda *a, **k: None,
                        vastai_run=lambda *a, **k: None)
    now = disp.registry_db.now_iso()
    for iid, label, host, port, source in boxes:
        d.conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, ssh_port, "
            "slots_total, hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, label, now, "live", 0.0, host, port, 4, now, source))
    for k, v in (settings or {}).items():
        d.conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
        d.settings[k] = v
    d.conn.commit()
    return d, home


def test_every_owned_box_gets_its_identity_from_the_registry(tmp_path, monkeypatch):
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")])
    d._sync_ssh_config()
    cfg = (home / ".ssh" / "config.fleet").read_text()
    assert "Host westdesktop" in cfg
    assert "Port 2222" in cfg
    assert "IdentityFile ~/.ssh/fleet_ed25519" in cfg
    assert "IdentitiesOnly yes" in cfg


def test_re_pointing_a_box_re_points_its_identity(tmp_path, monkeypatch):
    """The whole point: address and key can never desync, because both come from the same row."""
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "172.17.0.1", 2222, "owned")])
    d._sync_ssh_config()
    assert "Host 172.17.0.1" in (home / ".ssh" / "config.fleet").read_text()
    d.conn.execute("UPDATE instances SET ssh_host='westdesktop' WHERE id=-2")
    d.conn.commit()
    d._sync_ssh_config()
    cfg = (home / ".ssh" / "config.fleet").read_text()
    assert "Host westdesktop" in cfg and "172.17.0.1" not in cfg


def test_a_box_may_override_the_fleet_key(tmp_path, monkeypatch):
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")],
                  keys_present=("fleet_ed25519", "desktop_ed25519"),
                  settings={"ssh_key_i-2": "desktop_ed25519"})
    d._sync_ssh_config()
    assert "IdentityFile ~/.ssh/desktop_ed25519" in (home / ".ssh" / "config.fleet").read_text()


def test_an_operator_override_applies_with_NO_restart(tmp_path, monkeypatch):
    """`runq box key` promises "applied on the next poll". `self.settings` is loaded once in
    __init__, so reading it there would have made that a lie — the setting must be read LIVE."""
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")],
                  keys_present=("fleet_ed25519", "desktop_ed25519"))
    d._sync_ssh_config()
    assert "IdentityFile ~/.ssh/fleet_ed25519" in (home / ".ssh" / "config.fleet").read_text()
    # written by another process (the CLI), with no restart and no touch of d.settings
    d.conn.execute("INSERT INTO settings(key, value) VALUES ('ssh_key_i-2', '\"desktop_ed25519\"')")
    d.conn.commit()
    d._sync_ssh_config()
    assert "IdentityFile ~/.ssh/desktop_ed25519" in (home / ".ssh" / "config.fleet").read_text()


def test_a_missing_key_file_writes_NO_block(tmp_path, monkeypatch):
    """FAIL-SAFE: before the fleet key exists, this must not touch boxes that work today."""
    d, home = _mk(tmp_path, monkeypatch, [(-1, "laptop", "gpulaptop", 2222, "owned")],
                  keys_present=())
    d._sync_ssh_config()
    cfg = (home / ".ssh" / "config.fleet").read_text()
    assert "Host gpulaptop" not in cfg
    (ev,) = [json.loads(r["detail"]) for r in
             d.conn.execute("SELECT detail FROM events WHERE event='ssh_config'")]
    assert ev["boxes"] == 0 and "no such key" in ev["skipped"][0]


def test_a_key_name_that_is_a_path_is_refused(tmp_path, monkeypatch):
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")],
                  settings={"ssh_key_i-2": "../../etc/shadow"})
    d._sync_ssh_config()
    cfg = (home / ".ssh" / "config.fleet").read_text()
    assert "shadow" not in cfg and "Host westdesktop" not in cfg


def test_rented_boxes_are_left_alone(tmp_path, monkeypatch):
    """A Vast rental authenticates with the account's own keypair, not ours."""
    d, home = _mk(tmp_path, monkeypatch, [(123, "runq_x", "ssh5.vast.ai", 30000, "vast")])
    d._sync_ssh_config()
    assert "ssh5.vast.ai" not in (home / ".ssh" / "config.fleet").read_text()


def test_the_include_comes_first_and_is_written_once(tmp_path, monkeypatch):
    """ssh takes the FIRST value for a keyword, so the registry must win over any stale pattern."""
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")])
    (home / ".ssh" / "config").write_text("Host westdesktop\n    IdentityFile ~/.ssh/stale\n")
    d._sync_ssh_config()
    d._sync_ssh_config()
    base = (home / ".ssh" / "config").read_text()
    assert base.startswith("Include ~/.ssh/config.fleet\n")
    assert base.count("Include ~/.ssh/config.fleet") == 1
    assert "stale" in base        # the operator's own entries are preserved, just outranked


def test_it_rewrites_only_when_the_registry_changes(tmp_path, monkeypatch):
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")])
    d._sync_ssh_config()
    before = (home / ".ssh" / "config.fleet").stat().st_mtime_ns
    d._sync_ssh_config()
    assert (home / ".ssh" / "config.fleet").stat().st_mtime_ns == before
    assert len(d.conn.execute("SELECT 1 FROM events WHERE event='ssh_config'").fetchall()) == 1


def test_only_the_live_daemon_writes_a_developers_ssh_dir(tmp_path, monkeypatch):
    """`poll_once` runs in tests and in local exploration, where ~ is a HUMAN's home. The first
    version of this wrote config.fleet into a developer's ~/.ssh and prepended an Include to their
    own ssh config, from a plain `make test` — measured 2026-09-17 in this devcontainer."""
    d, home = _mk(tmp_path, monkeypatch, [(-2, "desktop", "westdesktop", 2222, "owned")])
    monkeypatch.delenv("COORD_ROLE", raising=False)
    (home / ".ssh" / "config").write_text("Host somewhere\n")
    d._sync_ssh_config()
    assert not (home / ".ssh" / "config.fleet").exists()
    assert (home / ".ssh" / "config").read_text() == "Host somewhere\n"
