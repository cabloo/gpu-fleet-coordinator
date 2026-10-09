"""register_owned_box.py — registers a self-owned box's `instances` row directly (bypassing
`_rent`/`_provision`, which are Vast-specific) and deploys spool_worker.py to it."""

import importlib.util
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
reg_owned = _load("register_owned_box", "fleet/register_owned_box.py")


class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


class _RecordingRun:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return _FakeProc(0 if self.ok else 1, "")

    def joined(self):
        return [" ".join(c) for c in self.calls]


def _mock_dispatcher(bring_up_result):
    """`register_owned_box.register()` reads `d.settings[...]` for its own retry loop around
    `_bring_up_worker` (invariant 20e: no poll loop of its own to retry across, unlike
    `_advance_provisioning`) — `ssh_probe_attempts=1` keeps a failing case from actually
    sleeping through `ssh_probe_interval_s` retries in the test."""
    calls = []

    class D:
        settings = {"ssh_probe_attempts": 1, "ssh_probe_interval_s": 0}

        def _bring_up_worker(self, inst, deny_file):
            calls.append(inst)
            return bring_up_result

    return (lambda db_path: D()), calls


class TestRegisterOwnedBox:
    def test_fresh_registration_inserts_negative_id_live_owned_row(self, tmp_path, monkeypatch):
        factory, _ = _mock_dispatcher(True)
        monkeypatch.setattr(reg_owned.dispatcher, "Dispatcher", factory)
        db = str(tmp_path / "runs.sqlite")
        rc = reg_owned.register(db, "laptop-gpu", "192.168.0.14", 22, 1, "RTX 4080", bootstrap=True)
        assert rc == 0
        conn = disp.registry_db.connect(db)
        row = dict(conn.execute("SELECT * FROM instances WHERE label='laptop-gpu'").fetchone())
        assert row["id"] < 0
        assert row["state"] == "live"
        assert row["source"] == "owned"
        assert row["dph_usd"] == 0.0
        assert row["ssh_host"] == "192.168.0.14"
        assert row["slots_total"] == 1
        assert row["destroyed_at"] is None

    def test_second_owned_box_gets_a_disjoint_negative_id(self, tmp_path, monkeypatch):
        factory, _ = _mock_dispatcher(True)
        monkeypatch.setattr(reg_owned.dispatcher, "Dispatcher", factory)
        db = str(tmp_path / "runs.sqlite")
        reg_owned.register(db, "laptop-gpu", "192.168.0.14", 22, 1, None, bootstrap=False)
        reg_owned.register(db, "desktop-3090", "192.168.0.15", 22, 1, None, bootstrap=False)
        conn = disp.registry_db.connect(db)
        ids = {r["id"] for r in conn.execute("SELECT id FROM instances")}
        assert len(ids) == 2
        assert all(i < 0 for i in ids)

    def test_reregistration_updates_in_place_not_a_duplicate(self, tmp_path, monkeypatch):
        factory, _ = _mock_dispatcher(True)
        monkeypatch.setattr(reg_owned.dispatcher, "Dispatcher", factory)
        db = str(tmp_path / "runs.sqlite")
        reg_owned.register(db, "laptop-gpu", "192.168.0.14", 22, 1, None, bootstrap=False)
        reg_owned.register(db, "laptop-gpu", "192.168.0.98", 2222, 2, None, bootstrap=False)
        conn = disp.registry_db.connect(db)
        rows = conn.execute("SELECT * FROM instances WHERE label='laptop-gpu'").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["ssh_host"] == "192.168.0.98"
        assert row["ssh_port"] == 2222
        assert row["slots_total"] == 2

    def test_bootstrap_failure_returns_nonzero_but_row_stays_registered(self, tmp_path, monkeypatch):
        factory, _ = _mock_dispatcher(False)
        monkeypatch.setattr(reg_owned.dispatcher, "Dispatcher", factory)
        db = str(tmp_path / "runs.sqlite")
        rc = reg_owned.register(db, "laptop-gpu", "192.168.0.14", 22, 1, None, bootstrap=True)
        assert rc == 1
        conn = disp.registry_db.connect(db)
        row = dict(conn.execute("SELECT * FROM instances WHERE label='laptop-gpu'").fetchone())
        assert row["state"] == "live"  # still registered — just not yet running a worker

    def test_no_bootstrap_flag_skips_deployment(self, tmp_path, monkeypatch):
        factory, calls = _mock_dispatcher(True)
        monkeypatch.setattr(reg_owned.dispatcher, "Dispatcher", factory)
        db = str(tmp_path / "runs.sqlite")
        rc = reg_owned.register(db, "laptop-gpu", "192.168.0.14", 22, 1, None, bootstrap=False)
        assert rc == 0
        assert calls == []


class TestReRegistrationChangesOnlyWhatItWasTold:
    """Invariant 20e-1. Re-registering used to write EVERY column and force `state='live'` — on both
    the CLI and the API path, which carried a copy of the upsert each. Measured 2026-10-03 on
    `desktop`: re-pointed at its new address while on hold, it was live and taking work within the
    same poll; and any omitted flag silently reset (port 22, 1 slot, no GPU name)."""

    def _box(self, tmp_path):
        db = str(tmp_path / "runs.sqlite")
        reg_owned.register(db, "desktop", "westdesktop", 2222, 12, "RTX 3070 Ti", bootstrap=False)
        return db, disp.registry_db.connect(db)

    def _row(self, conn):
        return dict(conn.execute("SELECT * FROM instances WHERE label='desktop'").fetchone())

    def test_a_PAUSED_box_stays_paused_and_keeps_its_pause_record(self, tmp_path, capsys):
        db, conn = self._box(tmp_path)
        iid = self._row(conn)["id"]
        conn.execute("UPDATE instances SET state='paused' WHERE id=?", (iid,))
        conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)",
                     (f"pause_i{iid}", '{"mode": "hold", "at": "2026-10-03T18:38:20Z"}'))
        conn.commit()
        reg_owned.register(db, "desktop", "old-desktop.lan", None, None, None, bootstrap=False)
        row = self._row(conn)
        assert row["state"] == "paused" and row["ssh_host"] == "old-desktop.lan"
        assert conn.execute("SELECT 1 FROM settings WHERE key=?", (f"pause_i{iid}",)).fetchone()
        assert "stays paused" in capsys.readouterr().out

    def test_fields_that_were_not_given_KEEP_their_values(self, tmp_path):
        db, conn = self._box(tmp_path)
        reg_owned.register(db, "desktop", "old-desktop.lan", None, None, None, bootstrap=False)
        row = self._row(conn)
        assert (row["ssh_port"], row["slots_total"], row["gpu_name"]) == (2222, 12, "RTX 3070 Ti")

    def test_a_field_that_WAS_given_is_written(self, tmp_path):
        db, conn = self._box(tmp_path)
        reg_owned.register(db, "desktop", "old-desktop.lan", None, 16, None, bootstrap=False)
        row = self._row(conn)
        assert (row["ssh_port"], row["slots_total"]) == (2222, 16)

    def test_a_box_that_is_not_paused_is_revived_to_live(self, tmp_path):
        """The other half: an `unreachable` box behind a stale address must come back."""
        db, conn = self._box(tmp_path)
        conn.execute("UPDATE instances SET state='unreachable', destroyed_at='2026-10-03T00:00:00Z'")
        conn.commit()
        reg_owned.register(db, "desktop", "old-desktop.lan", None, None, None, bootstrap=False)
        row = self._row(conn)
        assert row["state"] == "live" and row["destroyed_at"] is None

    def test_a_NEW_box_still_gets_the_documented_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUNQ_TRANSPORT", "local")
        db = str(tmp_path / "runs.sqlite")
        assert reg_owned.main(["--db", db, "--label", "new", "--host", "newbox",
                               "--no-bootstrap"]) == 0
        row = dict(disp.registry_db.connect(db).execute(
            "SELECT * FROM instances WHERE label='new'").fetchone())
        assert (row["ssh_port"], row["slots_total"], row["gpu_name"], row["state"],
                row["source"]) == (22, 1, None, "live", "owned")

    def test_the_cli_re_registration_does_not_reset_omitted_flags(self, tmp_path, monkeypatch):
        """End to end through argparse: `--port`/`--slots` used to DEFAULT to 22 / 1, so leaving
        them off a re-registration reset the box."""
        monkeypatch.setenv("RUNQ_TRANSPORT", "local")
        db, conn = self._box(tmp_path)
        assert reg_owned.main(["--db", db, "--label", "desktop", "--host", "old-desktop.lan",
                               "--no-bootstrap"]) == 0
        row = self._row(conn)
        assert (row["ssh_host"], row["ssh_port"], row["slots_total"], row["gpu_name"]) == (
            "old-desktop.lan", 2222, 12, "RTX 3070 Ti")

    def test_a_rental_carrying_the_same_label_is_never_the_box_that_gets_updated(self, tmp_path):
        db, conn = self._box(tmp_path)
        now = disp.registry_db.now_iso()
        conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, ssh_host, "
                     "ssh_port, slots_total, hard_cap_at, source) VALUES "
                     "(4242, 'rented', ?, 'live', 0.2, 'ssh9.vast.ai', 30000, 6, ?, 'vast')",
                     (now, now))
        conn.commit()
        box = disp.registry_db.upsert_owned_box(conn, "rented", "newhost")
        conn.commit()
        assert box["created"] and box["id"] < 0
        rental = dict(conn.execute("SELECT * FROM instances WHERE id=4242").fetchone())
        assert (rental["ssh_host"], rental["source"], rental["slots_total"]) == (
            "ssh9.vast.ai", "vast", 6)
