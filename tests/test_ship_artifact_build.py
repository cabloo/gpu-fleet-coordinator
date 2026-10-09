"""Coordinator side of ship-artifact-build — docs/specs/ship-artifact-build.spec.md Fixtures.

Covers the invariants that live in `dispatcher.py`: 6 (validate at the trust boundary), 12 (reclaim
ship staging), 15 (a bad blob fails the task loudly and names its owner), and the blob-first read
that keeps the coordinator from compiling at all.

Kept OUT of `tests/test_dispatcher.py` on purpose: that file is being reworked concurrently for the
ship fan-out, and these need none of its fixtures.
"""
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "fleet"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("dispatcher", "fleet/dispatcher.py")
store = _load("artifact_store", "fleet/artifact_store.py")
registry = _load("registry_db", "fleet/registry_db.py")


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
    monkeypatch.setattr(store, "STORE_VER", store.STORE_VER)   # keep the address stable
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"),
                           run=lambda *a, **k: None, vastai_run=lambda *a, **k: None)


def _task(d, tid="t1", *, blob=None, sha=None, fmt="compiled", by="alice"):
    d.conn.execute(
        "INSERT INTO tasks(id, created_at, created_by, grp, name, entrypoint, args_json, "
        "config_json, config_hash, arm_hash, git_sha, est_minutes, state, code_blob, "
        "code_sha256, code_format, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "2026-07-31T00:00:00Z", by, "g", tid, "smoke", "[]", "{}", "c" + tid, "a" + tid,
         "sha", 10, "claimed", blob, sha, fmt, "2026-07-31T00:00:00Z"))
    d.conn.commit()
    return dict(d.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def _alerts(d, monkeypatch):
    seen = []
    monkeypatch.setattr(d, "_alert", lambda msg: seen.append(msg))
    return seen


# ---- blob-first: the coordinator does not build (inv. 1) ----------------

def test_a_task_with_a_blob_is_read_straight_from_the_store(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    store.put(tmp_path, "b1", b"READY-BYTES")
    t = _task(d, blob="b1", sha=store.digest(b"READY-BYTES"))
    assert d._build_code_tar(t) == (b"READY-BYTES", "compiled")


def test_the_coordinator_has_NO_build_surface_left(tmp_path, monkeypatch):
    """Inv. 1, as a structural guard rather than a behavioural one: the machinery is deleted, so
    the way it regresses is somebody re-adding it. Named explicitly so that reads as a decision."""
    d = _mk(tmp_path, monkeypatch)
    for gone in ("_source_code_tar", "_compiled_tree", "_shipped_tree", "_log_compile",
                 "_prune_cache_dir", "_build_venv_python"):
        assert not hasattr(d, gone), f"{gone} came back — the coordinator must not build"
    for gone in ("_compile_cache_key", "_ship_tree_cache_key"):
        assert not hasattr(disp, gone), f"{gone} came back"
    src = (Path(disp.__file__)).read_text()
    assert "compile_tree" not in src, "dispatcher must not reference the compiler at all"


def test_a_pre_cutover_row_now_fails_loudly_instead_of_being_built(tmp_path, monkeypatch):
    """Q3 resolved: `backfill_ship_blobs.py` gave every open pre-cutover task a blob, so the legacy
    build path was deleted. A row that still has none cannot be shipped by anyone — say so and name
    the owner, rather than stranding it in an open state forever."""
    d = _mk(tmp_path, monkeypatch)
    seen = _alerts(d, monkeypatch)
    t = _task(d, blob=None, by="carol")
    code, _fmt = d._build_code_tar(t)
    assert code is None
    assert d.conn.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()[0] == "task_failed"
    assert "carol" in seen[0] and "pre-cutover" in seen[0]


def test_code_format_travels_from_the_row(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    store.put(tmp_path, "b1", b"SRC")
    t = _task(d, blob="b1", sha=store.digest(b"SRC"), fmt="snapshot")
    assert d._build_code_tar(t) == (b"SRC", "snapshot")


# ---- inv. 6 + 15: validate, then fail loudly and name the owner ---------

def test_a_missing_blob_fails_the_task_and_names_created_by(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    seen = _alerts(d, monkeypatch)
    t = _task(d, blob="gone", sha="x" * 64, by="agent-m49-beta")
    code, _fmt = d._build_code_tar(t)
    assert code is None                                     # nothing is shipped
    state = d.conn.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()[0]
    assert state == "task_failed"                           # never silently re-queued
    assert seen and "agent-m49-beta" in seen[0]            # it cannot fix it — so it says who can
    assert "MISSING" in seen[0]


def test_a_corrupt_blob_fails_the_task_and_is_never_shipped(tmp_path, monkeypatch):
    """The trust boundary that did not exist before: these are bytes the coordinator did not build."""
    d = _mk(tmp_path, monkeypatch)
    seen = _alerts(d, monkeypatch)
    store.put(tmp_path, "b1", b"TAMPERED")
    t = _task(d, blob="b1", sha=store.digest(b"ORIGINAL"), by="bob")
    code, _fmt = d._build_code_tar(t)
    assert code is None
    assert d.conn.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()[0] == "task_failed"
    assert "integrity" in seen[0] and "bob" in seen[0]


def test_a_blob_with_no_recorded_digest_is_still_served(tmp_path, monkeypatch):
    """Belt-and-braces for a row written before `code_sha256` existed alongside `code_blob`."""
    d = _mk(tmp_path, monkeypatch)
    store.put(tmp_path, "b1", b"BYTES")
    t = _task(d, blob="b1", sha=None)
    assert d._build_code_tar(t) == (b"BYTES", "compiled")


def test_a_failed_blob_check_yields_no_payload_at_all(tmp_path, monkeypatch):
    """There is nothing to fall back TO any more — but the contract that matters is that a bad blob
    yields no bytes, so `_ship` returns before it pushes anything."""
    d = _mk(tmp_path, monkeypatch)
    _alerts(d, monkeypatch)
    t = _task(d, blob="gone", sha="y" * 64)
    assert d._build_code_tar(t)[0] is None


# ---- inv. 12: ship staging is reclaimed ---------------------------------

def test_gc_reclaims_old_staging_dirs_and_leaves_fresh_ones(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    ship = tmp_path / ".ship"
    old, new = ship / "old-task", ship / "new-task"
    for p in (old, new):
        p.mkdir(parents=True)
        (p / "bundle.tar").write_bytes(b"x" * 1024)
    ancient = time.time() - 400 * 3600
    import os
    os.utime(old, (ancient, ancient))
    d._gc_ship_staging()
    assert not old.exists(), "a staging dir far past the ship window must be reclaimed"
    assert new.exists(), "an in-flight ship's staging must never be touched"


def test_gc_logs_what_it_reclaimed(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    p = tmp_path / ".ship" / "old"
    p.mkdir(parents=True)
    (p / "bundle.tar").write_bytes(b"x" * 2048)
    import os
    ancient = time.time() - 400 * 3600
    os.utime(p, (ancient, ancient))
    d._gc_ship_staging()
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='gc_staging' ORDER BY seq DESC LIMIT 1").fetchone()
    assert row and "reclaimed 1" in row[0]


def test_gc_with_no_staging_dir_is_a_noop(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    d._gc_ship_staging()          # must not raise


# ---- schema v4 ----------------------------------------------------------

def test_migration_adds_the_columns_to_an_old_db(tmp_path):
    """A v3 registry in the wild must gain the columns without losing rows."""
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(registry.SCHEMA_SQL.replace("  code_blob     TEXT,\n", "")
                       .replace("  code_sha256   TEXT,\n", "")
                       .replace("  code_format   TEXT,\n", ""))
    conn.execute(
        "INSERT INTO tasks(id, created_at, created_by, grp, name, entrypoint, args_json, "
        "config_json, config_hash, arm_hash, git_sha, est_minutes, state, updated_at) "
        "VALUES ('t1','2026-07-01T00:00:00Z','t','g','n','smoke','[]','{}','c','a','s',10,"
        "'queued','2026-07-01T00:00:00Z')")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()

    conn = registry.connect(str(path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"code_blob", "code_sha256", "code_format"} <= cols
    row = conn.execute("SELECT code_blob, state FROM tasks WHERE id='t1'").fetchone()
    assert row[0] is None and row[1] == "queued"   # pre-cutover marker, row intact
    assert conn.execute("PRAGMA user_version").fetchone()[0] == registry.SCHEMA_VERSION


# ---- inv. 13/14 end-to-end through the CLI ------------------------------
# A forced ABI mismatch is the cheapest real `BundleError`: `compile_tree`'s local backend compares
# the build interpreter's EXT_SUFFIX against `bundle_compile_abi` and refuses before compiling
# anything, so these run in milliseconds and still exercise the true failure path.
#
# ⚠ THAT PREMISE NEEDS `_prebuilt_build_venv` TO STAY TRUE. `build_venv_python` moved out of the
# coordinator and IN FRONT of `compile_tree` (2026-07-31), so the ABI guard is no longer the first
# thing the build does: reaching it meant a real `python -m venv` and a real `pip install cython
# setuptools` against PyPI first — 38s and a network dependency, in a test whose comment promised
# milliseconds. See tests/test_artifact_store.py::_FakeRun for the same flake in the unit tests, and
# tests/test_job_manifest.py::_min_db for the `ReadTimeoutError` that surfaced it (2026-08-25).
import os as _os
import subprocess as _sp

RUNQ = _ROOT / "fleet" / "runq.py"
IMPOSSIBLE_ABI = '"cpython-999-x86_64-linux-gnu"'


def _prebuilt_build_venv(tmp_path):
    """Satisfy `build_venv_python`'s cache so the build reaches the ABI guard without touching PyPI.

    It returns early when `<experiments_root>/.dispatcher/buildenv-py<XY>/bin/python` exists, keyed
    by the version of the interpreter `resolve_build_python` picks — which, for an ABI that matches
    no `cpython-3<minor>`, is the `runq` process's own `sys.executable`. Symlinking THAT is what
    keeps the guard honest: it reports the real EXT_SUFFIX, so the mismatch against
    `cpython-999-…` is a genuine one, not a stub's opinion."""
    py = Path(tmp_path) / ".dispatcher" / f"buildenv-py{sys.version_info[0]}{sys.version_info[1]}" / "bin"
    py.mkdir(parents=True, exist_ok=True)
    (py / "python").symlink_to(sys.executable)


def _seed(db, **settings):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for k, v in settings.items():
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()


def _cli(db, *args):
    env = dict(_os.environ)
    env["RUNQ_ACTOR"] = "build-tests"
    return _sp.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                   capture_output=True, text=True, cwd=_ROOT, env=env)


def _n_tasks(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def test_a_broken_toolchain_blows_the_task_and_queues_nothing(tmp_path):
    """Owner decision 2026-07-31: no source fallback. Before this, a BundleError shipped SOURCE."""
    db = tmp_path / "runs.sqlite"
    _seed(db, bundle_compile="true", bundle_compile_abi=IMPOSSIBLE_ABI)
    _prebuilt_build_venv(tmp_path)
    r = _cli(db, "add", "--group", "g", "--name", "t1", "--entrypoint", "smoke",
             "--est-minutes", "5", "--", "--updates", "1")
    assert r.returncode == 5, r.stderr
    assert "BUILD FAILED" in r.stderr and "nothing was queued" in r.stderr.lower()
    assert "THIS session only" in r.stderr        # the blast radius is session-local now
    # …and it failed for the reason this fixture NAMES. `build_venv_python` and `compile_tree` both
    # raise `BundleError` into the SAME hint, so before `_prebuilt_build_venv` a PyPI timeout during
    # the venv's `pip install` made this test PASS — green, on an arm that never reached the ABI
    # guard. Naming the mismatch is what makes that a failure instead of a silent hollowing-out.
    assert "ABI mismatch" in r.stderr and "cpython-999" in r.stderr, r.stderr
    assert _n_tasks(db) == 0                       # no row, no slot, no rental
    assert not list(store.store_dir(tmp_path).glob("*.tar.gz"))


def test_a_failing_sweep_queues_ZERO_cells_not_a_partial_grid(tmp_path):
    """Inv. 14 — the failure mode that is invisible until a sweep half-lands."""
    db = tmp_path / "runs.sqlite"
    _seed(db, bundle_compile="true", bundle_compile_abi=IMPOSSIBLE_ABI)
    _prebuilt_build_venv(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "job": {"run": "python -m native.smoke", "completion_artifact": "results.json"},
        "seed": 0, "lr": 0.1}))
    sweep = tmp_path / "s.sweep.json"
    sweep.write_text(json.dumps({"group": "g", "config": str(cfg),
                                 "axes": {"seed": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]}}))
    r = _cli(db, "sweep", str(sweep))
    assert r.returncode != 0
    assert _n_tasks(db) == 0, "a failing build must strand no partial grid"


def test_a_healthy_build_queues_and_records_the_blob(tmp_path):
    db = tmp_path / "runs.sqlite"
    _seed(db, bundle_compile="false")      # the deliberate off-switch (Q4), not a degradation
    r = _cli(db, "add", "--group", "g", "--name", "t1", "--entrypoint", "smoke",
             "--est-minutes", "5", "--", "--updates", "1")
    assert r.returncode == 0, r.stderr
    import sqlite3
    conn = sqlite3.connect(str(db))
    blob, sha, fmt = conn.execute(
        "SELECT code_blob, code_sha256, code_format FROM tasks").fetchone()
    conn.close()
    assert blob and sha and fmt == "snapshot"
    assert store.load(tmp_path, blob) is not None
    assert store.digest(store.load(tmp_path, blob)) == sha   # what the coordinator re-checks


# ---- inv. 7g: leaked rsync pull temps ------------------------------------

def _temp(p: Path, size=1024, age_h=10.0, mode=0o600):
    import os
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    p.chmod(mode)
    t = time.time() - age_h * 3600
    os.utime(p, (t, t))
    return p


def test_pull_uses_partial_dir_so_a_killed_transfer_resumes(monkeypatch):
    """The fix for 7f's '60s is still 60s' caveat: without this every timed-out pull leaks a
    full-size temp AND restarts from zero, so a big checkpoint never lands at all."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        import subprocess as sp
        return sp.CompletedProcess(cmd, 0, "", "")

    disp.rsync_pull("h", 22, "remote/", "local/", ["ckpt_latest.pt"], run=fake_run)
    assert f"--partial-dir={disp.PARTIAL_DIR}" in seen["cmd"]
    assert "--inplace" not in seen["cmd"], "7f: --inplace corrupts a non-append destination"


def test_append_pull_keeps_inplace_and_takes_no_partial_dir(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        import subprocess as sp
        return sp.CompletedProcess(cmd, 0, "", "")

    disp.rsync_pull("h", 22, "r/", "l/", ["tb/**"], append=True, run=fake_run)
    assert "--inplace" in seen["cmd"] and "--append" in seen["cmd"]
    assert not any(str(c).startswith("--partial-dir") for c in seen["cmd"])


def test_gc_removes_legacy_orphans_but_not_an_in_flight_one(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    old = _temp(tmp_path / "g" / "run" / ".ckpt_latest.pt.Pgujvb", size=4096, age_h=10)
    live = _temp(tmp_path / "g" / "run" / ".ckpt_latest.pt.aB3xY9", size=4096, age_h=0.01)
    d._gc_pull_temps()
    assert not old.exists()
    assert live.exists(), "an rsync mid-write must never have its own temp deleted underneath it"


def test_gc_never_touches_a_real_output_file(tmp_path, monkeypatch):
    """Both guards must hold: the name pattern AND mode 0600."""
    d = _mk(tmp_path, monkeypatch)
    real = _temp(tmp_path / "g" / "run" / "ckpt_latest.pt", age_h=99, mode=0o644)
    prev = _temp(tmp_path / "g" / "run" / "ckpt_latest.pt.prev", age_h=99, mode=0o644)
    dotted = _temp(tmp_path / "g" / "run" / ".summary.md.backup", age_h=99, mode=0o644)
    d._gc_pull_temps()
    assert real.exists() and prev.exists()
    assert dotted.exists(), "a 0644 dotfile that merely matches the shape must survive"


def test_gc_keeps_a_fresh_partial_but_drops_a_stale_one(tmp_path, monkeypatch):
    """A `.rsync-partial` entry is LOAD-BEARING while its task runs — that is the whole point."""
    d = _mk(tmp_path, monkeypatch)
    fresh = _temp(tmp_path / "g" / "run" / disp.PARTIAL_DIR / "ckpt_latest.pt", age_h=5)
    stale = _temp(tmp_path / "g" / "old" / disp.PARTIAL_DIR / "ckpt_latest.pt", age_h=200)
    d._gc_pull_temps()
    assert fresh.exists(), "a partial younger than a long run must be kept for the next attempt"
    assert not stale.exists()


def test_gc_pull_temps_skips_the_dispatcher_dir(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    keep = _temp(tmp_path / ".dispatcher" / "blobs" / ".x.tar.gz.abc123", age_h=99)
    d._gc_pull_temps()
    assert keep.exists(), "the store's own tmp files are owned by artifact_store, not this reaper"


def test_gc_pull_temps_logs_what_it_freed(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    _temp(tmp_path / "g" / "r" / ".a.pt.Pgujvb", size=2048, age_h=10)
    d._gc_pull_temps()
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='gc_pull_temps' ORDER BY seq DESC LIMIT 1").fetchone()
    assert row and "reclaimed 1" in row[0]


# ---- inv. 12a: code snapshots of finished tasks --------------------------

def _snap(d, tmp_path, tid, age_h):
    import os
    p = tmp_path / ".dispatcher" / "snapshots" / f"{tid}.tar.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"tar")
    t = time.time() - age_h * 3600
    os.utime(p, (t, t))
    return p


def test_snapshot_gc_keeps_open_tasks_however_old(tmp_path, monkeypatch):
    """Refcounted, like the blob store: this is the copy the ship path reads."""
    d = _mk(tmp_path, monkeypatch)
    _task(d, "open1")                                   # state='claimed'
    p = _snap(d, tmp_path, "open1", age_h=1000)
    d._gc_code_snapshots()
    assert p.exists()


def test_snapshot_gc_drops_terminal_tasks_past_the_window(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    _task(d, "gone")
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='gone'")
    d.conn.commit()
    p = _snap(d, tmp_path, "gone", age_h=500)
    d._gc_code_snapshots()
    assert not p.exists()


def test_snapshot_gc_keeps_a_recently_failed_task_for_forensics(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    _task(d, "justfailed")
    d.conn.execute("UPDATE tasks SET state='task_failed' WHERE id='justfailed'")
    d.conn.commit()
    p = _snap(d, tmp_path, "justfailed", age_h=1)
    d._gc_code_snapshots()
    assert p.exists()


def test_snapshot_gc_with_no_dir_is_a_noop(tmp_path, monkeypatch):
    _mk(tmp_path, monkeypatch)._gc_code_snapshots()


# ---- inv. 11: ship the tree ONCE per (blob, box) -------------------------
import tarfile as _tar
import io as _io

_bundle = _load("bundle", "fleet/bundle.py")


def _codetar(payload=b"hello"):
    """Payloads are given verbatim; callers needing SIZE must pass incompressible bytes, since a
    gzip member of repeated filler is a few hundred bytes however long the input."""
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as tf:
        info = _tar.TarInfo("f.txt"); info.size = len(payload)
        tf.addfile(info, _io.BytesIO(payload))
    return buf.getvalue()


def test_a_code_ref_bundle_omits_the_tree_entirely(tmp_path):
    code = _codetar(b"hello")
    ref = tmp_path / "ref.tar"; emb = tmp_path / "emb.tar"
    _bundle.build_bundle(ref, code_tar=code, task_json={"a": 1}, git_sha="s", task_id="t",
                         code_ref="blob123")
    _bundle.build_bundle(emb, code_tar=code, task_json={"a": 1}, git_sha="s", task_id="t")
    # Structural, not size-based: gzip makes a filler payload tiny whatever its length, so "smaller"
    # is a weak claim. What matters is that the tree is NOT A MEMBER of the tar at all.
    with _tar.open(ref) as tf:
        assert _bundle.CODE_NAME not in tf.getnames()
    with _tar.open(emb) as tf:
        assert _bundle.CODE_NAME in tf.getnames()
    m = _bundle.read_manifest(ref)
    assert m["bundle_version"] == _bundle.BUNDLE_VERSION_CODE_REF
    assert m["code_ref"]["blob_id"] == "blob123"
    assert m["code_ref"]["sha256"] == _bundle._sha256(code)   # still bound by digest
    assert _bundle.CODE_NAME not in m["members"]


def test_an_embedded_bundle_still_declares_v1_so_old_workers_accept_it(tmp_path):
    """The rollout hinge: a worker rejects any bundle_version it does not know, so bumping
    unconditionally would break every live box at once."""
    p = tmp_path / "b.tar"
    _bundle.build_bundle(p, code_tar=_codetar(), task_json={}, git_sha="s", task_id="t")
    assert _bundle.read_manifest(p)["bundle_version"] == _bundle.BUNDLE_VERSION == 1


def test_unpack_reads_the_referenced_blob_from_the_shared_dir(tmp_path):
    code = _codetar(b"payload-here")
    blobs = tmp_path / "blobs"; blobs.mkdir()
    (blobs / "b1.tar.gz").write_bytes(code)
    p = tmp_path / "b.tar"
    _bundle.build_bundle(p, code_tar=code, task_json={"k": "v"}, git_sha="s", task_id="t",
                         code_ref="b1")
    out = tmp_path / "active"
    task = _bundle.unpack_bundle(p, out, blob_dir=blobs)
    assert task == {"k": "v"}
    assert (out / "repo" / "f.txt").read_bytes() == b"payload-here"


def test_a_TAMPERED_shared_blob_is_refused(tmp_path):
    """The shared blob is the one thing the outer tar's own integrity check cannot cover, so it is
    verified explicitly — otherwise a corrupt box-side copy would be extracted and RUN."""
    blobs = tmp_path / "blobs"; blobs.mkdir()
    (blobs / "b1.tar.gz").write_bytes(_codetar(b"EVIL"))
    p = tmp_path / "b.tar"
    _bundle.build_bundle(p, code_tar=_codetar(b"good"), task_json={}, git_sha="s", task_id="t",
                         code_ref="b1")
    with pytest.raises(_bundle.BundleError, match="FAILED integrity"):
        _bundle.unpack_bundle(p, tmp_path / "a", blob_dir=blobs)
    assert not (tmp_path / "a" / "repo").exists(), "nothing may be extracted from a bad blob"


def test_a_missing_shared_blob_is_refused_not_silently_empty(tmp_path):
    p = tmp_path / "b.tar"
    _bundle.build_bundle(p, code_tar=_codetar(), task_json={}, git_sha="s", task_id="t",
                         code_ref="absent")
    with pytest.raises(_bundle.BundleError, match="unreadable"):
        _bundle.unpack_bundle(p, tmp_path / "a", blob_dir=tmp_path / "blobs")


def test_a_code_ref_bundle_on_a_worker_with_no_blob_dir_says_so(tmp_path):
    """Belt and braces for a mixed rollout: a pre-inv-11 worker cannot reach here (it rejects v2
    outright), but if one ever did the error must name the cause."""
    p = tmp_path / "b.tar"
    _bundle.build_bundle(p, code_tar=_codetar(), task_json={}, git_sha="s", task_id="t",
                         code_ref="b1")
    with pytest.raises(_bundle.BundleError, match="predates inv. 11"):
        _bundle.unpack_bundle(p, tmp_path / "a")


# ---- dispatcher: probe, then push once ----------------------------------

class _CapRun:
    """Records ssh/rsync argv and answers the CAPS + blob-existence probes."""

    def __init__(self, caps=True, blob_present=False):
        self.caps, self.blob_present, self.calls = caps, blob_present, []

    def __call__(self, cmd, **kw):
        import subprocess as sp
        self.calls.append(" ".join(str(c) for c in cmd))
        joined = self.calls[-1]
        if "spool/CAPS" in joined:
            return sp.CompletedProcess(cmd, 0, "blobref\n" if self.caps else "", "")
        if "test -f" in joined and "blobs/" in joined:
            return sp.CompletedProcess(cmd, 0, "PRESENT" if self.blob_present else "ABSENT", "")
        return sp.CompletedProcess(cmd, 0, "", "")


def _shipdisp(tmp_path, monkeypatch, run):
    monkeypatch.setattr(disp, "EXPERIMENTS_ROOT", tmp_path)
    return disp.Dispatcher(str(tmp_path / "runs.sqlite"), run=run, vastai_run=run)


def test_a_capable_box_gets_a_reference_and_the_blob_is_pushed_once(tmp_path, monkeypatch):
    run = _CapRun(caps=True, blob_present=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    store.put(tmp_path, "b1", _codetar())
    t = _task(d, blob="b1", sha=store.digest(_codetar()))
    inst = {"id": 1, "ssh_host": "h", "ssh_port": 22}
    plan = d._ship_prepare(t, inst, "h", 22)
    assert plan["blob_id"] == "b1"
    d._ship_io(plan, "h", 22)
    pushes = [c for c in run.calls if "rsync" in c and "b1.tar.gz" in c]
    assert len(pushes) == 1, "the tree must be sent exactly once when absent"


def test_a_blob_already_on_the_box_is_NOT_resent(tmp_path, monkeypatch):
    """The 55%-of-placements case: N cells of one sweep, one transfer."""
    run = _CapRun(caps=True, blob_present=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    store.put(tmp_path, "b1", _codetar())
    t = _task(d, blob="b1", sha=store.digest(_codetar()))
    plan = d._ship_prepare(t, {"id": 1}, "h", 22)
    d._ship_io(plan, "h", 22)
    assert not [c for c in run.calls if "rsync" in c and "b1.tar.gz" in c]


def test_an_OLD_worker_still_gets_a_self_contained_bundle(tmp_path, monkeypatch):
    """Rollout safety: no CAPS file -> v1 with the tree embedded, so a live box mid-upgrade keeps
    working instead of rejecting every delivery."""
    run = _CapRun(caps=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    store.put(tmp_path, "b1", _codetar())
    t = _task(d, blob="b1", sha=store.digest(_codetar()))
    plan = d._ship_prepare(t, {"id": 1}, "h", 22)
    assert plan["blob_id"] is None
    m = _bundle.read_manifest(plan["bundle_path"])
    assert m["bundle_version"] == _bundle.BUNDLE_VERSION
    assert _bundle.CODE_NAME in m["members"]


def test_the_capability_probe_is_cached_per_box(tmp_path, monkeypatch):
    run = _CapRun(caps=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    inst = {"id": 7}
    assert d._box_supports_blobs(inst, "h", 22) is True
    n = len([c for c in run.calls if "CAPS" in c])
    assert d._box_supports_blobs(inst, "h", 22) is True
    assert len([c for c in run.calls if "CAPS" in c]) == n, "a known capability must not re-probe"


def test_a_box_without_the_capability_is_REPROBED(tmp_path, monkeypatch):
    """So a worker redeployed mid-life is picked up without a coordinator restart."""
    run = _CapRun(caps=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    d._box_supports_blobs({"id": 7}, "h", 22)
    n = len([c for c in run.calls if "CAPS" in c])
    d._box_supports_blobs({"id": 7}, "h", 22)
    assert len([c for c in run.calls if "CAPS" in c]) > n


# ---- inv. 20i: workers self-update ---------------------------------------

_worker = _load("spool_worker", "fleet/spool_worker.py")


def test_the_fingerprint_changes_when_any_source_file_changes(tmp_path, monkeypatch):
    src = tmp_path / "bin"; src.mkdir()
    for n in _worker.SOURCE_FILES:
        (src / n).write_text("x = 1\n")
    monkeypatch.setattr(_worker, "__file__", str(src / "spool_worker.py"))
    a = _worker._source_fingerprint()
    assert a == _worker._source_fingerprint()             # deterministic
    (src / "bundle.py").write_text("x = 2\n")
    assert _worker._source_fingerprint() != a             # any file counts, not just the entry


def test_an_unreadable_source_does_not_raise(tmp_path, monkeypatch):
    """A fingerprint is a comparison, never a health check — `_sources_compile` is the health check."""
    src = tmp_path / "bin"; src.mkdir()
    (src / "spool_worker.py").write_text("x = 1\n")
    monkeypatch.setattr(_worker, "__file__", str(src / "spool_worker.py"))
    assert _worker._source_fingerprint()                  # missing siblings -> sentinel, no raise


def test_a_half_written_source_is_NOT_exec_into(tmp_path, monkeypatch):
    """The load-bearing guard: nothing relaunches a worker that dies on a rental, so a broken or
    mid-rsync file must never be exec'd."""
    src = tmp_path / "bin"; src.mkdir()
    for n in _worker.SOURCE_FILES:
        (src / n).write_text("x = 1\n")
    monkeypatch.setattr(_worker, "__file__", str(src / "spool_worker.py"))
    assert _worker._sources_compile() is True
    (src / "bundle.py").write_text("def broken(:\n")      # syntax error, as a torn rsync looks
    assert _worker._sources_compile() is False


def test_worker_refresh_is_throttled_per_box(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    d.conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
        "VALUES (1,'b','2026-08-01T00:00:00Z','live',0.1,4,'2026-08-03T00:00:00Z')")
    d.conn.commit()
    calls = []
    monkeypatch.setattr(d, "_retire_pre_20i_worker", lambda inst: None)
    monkeypatch.setattr(d, "_bring_up_worker", lambda inst, deny: calls.append(inst["id"]))
    d._refresh_workers()
    d._refresh_workers()
    assert calls == [1], "a refresh is 1 ssh + an rsync — once per worker_refresh_min, not per poll"


def test_worker_refresh_covers_OWNED_boxes_too(tmp_path, monkeypatch):
    """They are the ones that never churn, so they are exactly the ones that needed this."""
    d = _mk(tmp_path, monkeypatch)
    d.conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at,source) "
        "VALUES (-1,'laptop','2026-07-15T00:00:00Z','live',0.0,6,'2027-01-01T00:00:00Z','owned')")
    d.conn.commit()
    seen = []
    monkeypatch.setattr(d, "_retire_pre_20i_worker", lambda inst: None)
    monkeypatch.setattr(d, "_bring_up_worker", lambda inst, deny: seen.append(inst["id"]))
    d._refresh_workers()
    assert seen == [-1]


def test_a_failing_refresh_never_breaks_the_poll(tmp_path, monkeypatch):
    d = _mk(tmp_path, monkeypatch)
    d.conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at) "
        "VALUES (1,'b','2026-08-01T00:00:00Z','live',0.1,4,'2026-08-03T00:00:00Z')")
    d.conn.commit()

    def boom(inst, deny):
        raise OSError("box unreachable")
    monkeypatch.setattr(d, "_bring_up_worker", boom)
    d._refresh_workers()                                  # must not raise


# ---- inv. 20i bootstrap: retire a worker that cannot self-update ---------

class _VersionRun:
    def __init__(self, legacy=True):
        self.legacy, self.calls = legacy, []

    def __call__(self, cmd, **kw):
        import subprocess as sp
        self.calls.append(" ".join(str(c) for c in cmd))
        if "WORKER_VERSION" in self.calls[-1]:
            return sp.CompletedProcess(cmd, 0, "LEGACY" if self.legacy else "SELFUPDATING", "")
        return sp.CompletedProcess(cmd, 0, "", "")


def _live_box(d, iid=1, source="vast"):
    d.conn.execute(
        "INSERT INTO instances(id,label,created_at,state,dph_usd,slots_total,hard_cap_at,source,"
        "ssh_host,ssh_port) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (iid, f"b{iid}", "2026-08-01T00:00:00Z", "live", 0.1, 4, "2026-08-03T00:00:00Z", source,
         "h", 22))
    d.conn.commit()


def test_a_pre_20i_worker_is_STOPPED_so_it_relaunches_on_current_code(tmp_path, monkeypatch):
    """The bootstrap gap that made 20i a no-op on every box that existed when it landed: the
    self-update lives IN the worker, so a worker started before it can never notice new files."""
    run = _VersionRun(legacy=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert any("pkill" in c for c in run.calls)
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='worker_retired'").fetchone()
    assert row and "cannot self-update" in row[0]


def test_a_worker_that_ALREADY_self_updates_is_left_alone(tmp_path, monkeypatch):
    """Fires at most once per box, ever — `WORKER_VERSION` is written only by a 20i worker."""
    run = _VersionRun(legacy=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert not any("pkill" in c for c in run.calls)


def test_a_worker_is_NEVER_stopped_mid_delivery(tmp_path, monkeypatch):
    """A worker killed mid-unpack leaves a partial `repo/`, and the restart path treats an existing
    `repo/` as already-extracted — it would run a TRUNCATED tree."""
    run = _VersionRun(legacy=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "mid", blob="b", sha=None)          # seeded as 'claimed'
    d.conn.execute("UPDATE tasks SET instance_id=1 WHERE id='mid'")
    d.conn.commit()
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert not any("pkill" in c for c in run.calls)
    assert not any("WORKER_VERSION" in c for c in run.calls), "must not even probe mid-delivery"


def test_a_running_task_DOES_block_the_retirement(tmp_path, monkeypatch):
    """★ INVERTED 2026-08-01 — this test previously asserted the opposite and was a guard on a bug.

    Its stated rationale was "trainers are launched with start_new_session, so replacing the worker
    does not touch them". Measured in the field, that is false: retiring workers under running tasks
    destroyed **12 cells across 8 campaigns in about an hour**, every one dying with
    `shared.infra.checkpoint.CheckpointRegression` because the task's training restarted from the
    first stage/seed (`sched.stage_index 3 -> 0`, `seed_index 2 -> 0`). No `preempt` and no
    `requeue` event was emitted — the task sat in `running` while the work underneath it restarted.

    Inverted rather than deleted, per the standing rule: if a future change makes it safe to replace
    a worker under running work, THIS test should fail and be re-inverted with the evidence — it must
    never be quietly dropped, because it is the only thing recording what the field measured."""
    run = _VersionRun(legacy=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "run1", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='run1'")
    d.conn.commit()
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert not any("pkill" in c for c in run.calls), "a worker owning RUNNING work must not be retired"
    assert not any("WORKER_VERSION" in c for c in run.calls), "must not even probe an occupied box"


def test_the_retirement_still_fires_once_the_box_DRAINS(tmp_path, monkeypatch):
    """The conservative gate must cost only LATENCY, not the rollout: a box whose task has finished
    is retired on a later pass. Without this, 'never retire an occupied box' could silently become
    'never retire anything', and 20i would stay a no-op on every busy box — the exact bootstrap gap
    the retirement exists to close."""
    run = _VersionRun(legacy=True)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "run1", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='run1'")
    d.conn.commit()
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert not any("pkill" in c for c in run.calls)
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='run1'")   # the box drains
    d.conn.commit()
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})
    assert any("pkill" in c for c in run.calls), "an IDLE box must still be retired"


def test_a_refresh_failure_is_LOGGED_not_swallowed(tmp_path, monkeypatch):
    """The first version suppressed silently, which is why the phase read 0.0s with no explanation
    when it did nothing. A best-effort step still has to say why it gave up."""
    d = _shipdisp(tmp_path, monkeypatch, _VersionRun())
    _live_box(d)
    monkeypatch.setattr(d, "_retire_pre_20i_worker",
                        lambda inst: (_ for _ in ()).throw(OSError("unreachable")))
    d._refresh_workers()
    row = d.conn.execute("SELECT detail FROM events WHERE event='worker_refresh_failed'").fetchone()
    assert row and "unreachable" in row[0]


def test_a_LOCKED_registry_cannot_kill_the_poll(tmp_path, monkeypatch, capsys):
    """The regression this came from: `_refresh_workers` caught a locked-DB error, tried to LOG it
    through the same connection, the log raised, and the daemon died mid-poll. The supervisor then
    respawned it, clearing the in-memory throttle, so the next pass retired the same workers again —
    a crash loop built entirely out of error handling."""
    d = _shipdisp(tmp_path, monkeypatch, _VersionRun())
    _live_box(d)

    def locked(*a, **k):
        import sqlite3 as s
        raise s.OperationalError("database is locked")
    monkeypatch.setattr(d, "log", locked)
    monkeypatch.setattr(d, "_retire_pre_20i_worker",
                        lambda inst: (_ for _ in ()).throw(OSError("boom")))
    d._refresh_workers()                        # must NOT raise even though logging is broken
    assert "[log-failed]" in capsys.readouterr().err


def test_the_retirement_log_also_survives_a_locked_registry(tmp_path, monkeypatch, capsys):
    d = _shipdisp(tmp_path, monkeypatch, _VersionRun(legacy=True))
    _live_box(d)

    def locked(*a, **k):
        import sqlite3 as s
        raise s.OperationalError("database is locked")
    monkeypatch.setattr(d, "log", locked)
    d._retire_pre_20i_worker({"id": 1, "ssh_host": "h", "ssh_port": 22})   # must not raise
    assert "worker_retired" in capsys.readouterr().err


# ---- invariant 20j-1: never push worker bytes to an OCCUPIED box -----------------------------
#
# MEASURED 2026-08-02, twice in ninety minutes. `c3f23565` (an observability commit) moved the
# worker fingerprint; five boxes re-exec'd at 19:30:31-49 and six tasks across five campaigns died
# in eight minutes with `CheckpointRegression`. Then `8d535aa7` — the box-side fix for that very
# bug — was delivered at 20:00:51 and killed THIRTEEN MORE on its way in, because a box-side guard
# cannot protect a box that has not yet received it. Hence a COORDINATOR-side gate.

#: A delivery WRITES to the box (rsync/scp) or kills something (pkill). Everything the gate does to
#: DECIDE is a read — `cat ~/spool/<X>` — and a read is not a delivery.
_READ_ONLY_PROBE = re.compile(r"cat\s+~/spool/\S+")
_WRITES = ("rsync", "scp", "pkill")


def _delivery_calls(run):
    """Calls that are actual worker-code DELIVERY, excluding the gate's own read-only probes.

    ⚠ THIS PREDICATE IS ITSELF A TRAP, and it sprang once (2026-08-03→04). It used to exclude the
    single literal `CAPS`, so when the rolling-upgrade work added a SECOND read-only probe
    (`cat ~/spool/WORKER_VERSION`, from `_consider_worker_roll`) all three hold tests started
    reporting "delivered worker code to an occupied box" while the gate was working perfectly —
    a false RED that sat on master for a day and cost a real investigation. Naming probes one by
    one means the next probe breaks it again, so the rule is now the GENERAL one: a `cat ~/spool/…`
    is a read, and only rsync/scp/pkill move bytes or signals."""
    out = []
    for c in run.calls:
        joined = " ".join(c) if isinstance(c, (list, tuple)) else str(c)
        if _READ_ONLY_PROBE.search(joined) and not any(w in joined for w in _WRITES):
            continue
        out.append(joined)
    return out


def test_worker_code_is_NOT_delivered_to_a_box_running_work(tmp_path, monkeypatch):
    """The delivery is what moves the fingerprint, and the fingerprint is what makes the worker
    re-exec under its own running trainers. Gate the delivery, not the re-exec."""
    run = _VersionRun(legacy=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "busy", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='busy'")
    d.conn.commit()
    d._refresh_workers()
    assert 1 not in d._last_worker_refresh, f"delivered to an occupied box: {run.calls}"
    assert not _delivery_calls(run), f"delivered worker code to an occupied box: {run.calls}"
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='worker_refresh_deferred'").fetchone()
    assert row and "deferred until it drains" in row[0], "a held box must SAY it is held"


def test_a_shipped_or_claimed_task_ALSO_blocks_delivery(tmp_path, monkeypatch):
    """`running` is not the only occupied state — a worker replaced mid-`_unpack` leaves a partial
    `repo/`, which `validate_and_prepare` then treats as already-extracted and runs TRUNCATED."""
    for state in ("claimed", "shipped"):
        sub = tmp_path / state
        sub.mkdir()
        run = _VersionRun(legacy=False)
        d = _shipdisp(sub, monkeypatch, run)
        _live_box(d)
        _task(d, "occ", blob="b", sha=None)
        d.conn.execute("UPDATE tasks SET instance_id=1, state=? WHERE id='occ'", (state,))
        d.conn.commit()
        d._refresh_workers()
        assert not _delivery_calls(run), f"delivered to a box with a {state} task: {run.calls}"


def test_delivery_RESUMES_once_the_box_drains(tmp_path, monkeypatch):
    """The gate must cost only LATENCY. Without this, 'never deliver to an occupied box' silently
    becomes 'never deliver', and the fleet stops updating — a quieter version of the same failure.
    This is also the NEGATIVE half: it fails if the gate is hard-wired to skip everything."""
    run = _VersionRun(legacy=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "gone", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='gone'")
    d.conn.commit()
    d._refresh_workers()
    assert not _delivery_calls(run)
    d.conn.execute("UPDATE tasks SET state='done' WHERE id='gone'")
    d.conn.commit()
    d._refresh_workers()
    assert _delivery_calls(run), "a DRAINED box must be refreshed on a later pass"
    row = d.conn.execute(
        "SELECT detail FROM events WHERE event='worker_refresh_resumed'").fetchone()
    assert row, "clearing the hold must be logged too, or 'held for days' is unreadable"


def test_the_hold_is_edge_triggered_not_logged_every_poll(tmp_path, monkeypatch):
    """`_refresh_workers` runs every poll; logging unconditionally floods a file the dispatcher
    rsyncs. One line per transition is the diagnostic content (invariant 19h-4's rationale)."""
    run = _VersionRun(legacy=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "busy", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='busy'")
    d.conn.commit()
    for _ in range(5):
        d._refresh_workers()
    (n,) = d.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event='worker_refresh_deferred'").fetchone()
    assert n == 1, f"expected one edge-triggered line, got {n}"


def test_a_held_box_does_NOT_back_off_for_another_refresh_interval(tmp_path, monkeypatch):
    """`_last_worker_refresh` must NOT be stamped on the held path, or a box that drains one minute
    after a hold waits a full `worker_refresh_min` before anyone notices."""
    run = _VersionRun(legacy=False)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "busy", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='busy'")
    d.conn.commit()
    d._refresh_workers()
    assert 1 not in d._last_worker_refresh, "a held box must not consume its refresh throttle"


# ---- invariant 20j-2: a CheckpointRegression over REAL PROGRESS is INFRA, not a task fault -----

def _failed_task_with_crash(tmp_path, monkeypatch, traceback_text, *, progressed):
    """Drive `_complete_failed` with a planted crash.json, stubbing the two remote pulls."""
    d = _shipdisp(tmp_path, monkeypatch, _VersionRun(legacy=False))
    _live_box(d)
    _task(d, "t1", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='t1'")
    d.conn.commit()
    task = dict(d.conn.execute("SELECT * FROM tasks WHERE id='t1'").fetchone())
    out = d._result_dir(task)
    out.mkdir(parents=True, exist_ok=True)
    (out / "crash.json").write_text(json.dumps({"traceback": traceback_text}))
    monkeypatch.setattr(disp, "rsync_pull", lambda *a, **k: True)
    monkeypatch.setattr(d, "_task_made_progress", lambda _o: progressed)
    d._complete_failed(task, {"id": 1}, "h", 22, "FAILED_1")
    return d


_CKPT_REGRESSION_TB = (
    "Traceback (most recent call last):\n"
    '  File "src/shared/infra/checkpoint.py", line 134, in save_latest\n'
    "shared.infra.checkpoint.CheckpointRegression: refusing to write ../out/ckpt_latest.pt: "
    "progress would go BACKWARDS — seed_index 2 -> 0."
)


def test_checkpoint_regression_over_progress_is_INFRA_and_REQUEUES(tmp_path, monkeypatch):
    """The guard fires when the task was RESTARTED underneath, not when its code is wrong. It is
    fully resumable from the checkpoint the guard just protected, so it must requeue — and keep
    `resume_checkpoint`, so the requeue RESUMES instead of redoing the work."""
    d = _failed_task_with_crash(tmp_path, monkeypatch, _CKPT_REGRESSION_TB, progressed=True)
    row = d.conn.execute("SELECT state, retries_used FROM tasks WHERE id='t1'").fetchone()
    assert row["state"] == "queued", f"should have requeued, got {row['state']}"
    assert row["retries_used"] == 0.5, "an infra failure costs HALF a retry, not a whole one"
    ev = {r[0] for r in d.conn.execute("SELECT event FROM events WHERE task_id='t1'")}
    assert "infra_failed" in ev and "task_failed" not in ev, ev


def test_checkpoint_regression_with_ZERO_progress_stays_TERMINAL(tmp_path, monkeypatch):
    """★ The negative half, and the important one. The same guard fires on a genuine self-clobbering
    code bug (m49 162127c). Routing THAT to infra would turn one loud terminal failure into up to
    2*max_retries silent retries — fail-closed converted back into fail-silent, which is the exact
    defect this class keeps re-producing. No progress ⇒ no valid work was protected ⇒ it is the code."""
    d = _failed_task_with_crash(tmp_path, monkeypatch, _CKPT_REGRESSION_TB, progressed=False)
    row = d.conn.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()
    assert row["state"] == "task_failed", f"a zero-progress regression must stay terminal, got {row['state']}"


def test_an_ordinary_crash_with_progress_is_still_TASK_FAILED(tmp_path, monkeypatch):
    """Second negative half: the branch must key on the REGRESSION, not merely on having progressed.
    Without this, any crash in a long run would be laundered into an infinite infra retry loop."""
    d = _failed_task_with_crash(
        tmp_path, monkeypatch,
        'Traceback (most recent call last):\n  File "t.py", line 1\nValueError: bad config',
        progressed=True)
    row = d.conn.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()
    assert row["state"] == "task_failed", f"an ordinary crash must stay terminal, got {row['state']}"


# ---- invariant 20j-3: the hold is only for workers that CANNOT survive their own restart -------

class _CapsRun:
    """ssh stub whose `cat ~/spool/CAPS` reports a chosen capability set."""

    def __init__(self, caps: str):
        self.caps, self.calls = caps, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        import subprocess as sp
        # `cmd` is an argv LIST — join before matching, or the probe silently reads as absent and
        # every capability test passes for the wrong reason.
        out = self.caps if "CAPS" in " ".join(cmd) else ""
        return sp.CompletedProcess(cmd, 0, stdout=out, stderr="")


def _occupied_box(tmp_path, monkeypatch, caps):
    run = _CapsRun(caps)
    d = _shipdisp(tmp_path, monkeypatch, run)
    _live_box(d)
    _task(d, "busy", blob="b", sha=None)
    d.conn.execute("UPDATE tasks SET instance_id=1, state='running' WHERE id='busy'")
    d.conn.commit()
    d._refresh_workers()
    return d, run


def test_a_reattach_capable_box_IS_delivered_to_while_busy(tmp_path, monkeypatch):
    """★ Without this, 20j-1 is a deadlock dressed as safety: the box is held because it is
    occupied, and the placer keeps it occupied — so a busy box NEVER updates. A worker advertising
    `reattach` re-adopts its trainers across the restart, so delivering to it is a no-op for the
    running work."""
    d, run = _occupied_box(tmp_path, monkeypatch, "blobref\nreattach\n")
    assert any("CAPS" in " ".join(c) for c in run.calls), "the capability must actually be probed"
    assert 1 in d._last_worker_refresh, \
        "a reattach-capable box must ENTER the delivery path while busy, not be held"
    assert not d.conn.execute(
        "SELECT 1 FROM events WHERE event='worker_refresh_deferred'").fetchone()


def test_a_box_WITHOUT_the_capability_is_still_held(tmp_path, monkeypatch):
    """The negative half, and the fail-safe: an older worker (or one we cannot probe) gets exactly
    the pre-20j-3 conservative hold. The capability may only ever WIDEN what is allowed."""
    d, _run = _occupied_box(tmp_path, monkeypatch, "blobref\n")
    assert d.conn.execute(
        "SELECT 1 FROM events WHERE event='worker_refresh_deferred'").fetchone(), \
        "a worker that cannot survive its restart must still be held"
    assert 1 not in d._last_worker_refresh, "a held box must not consume its refresh throttle"
