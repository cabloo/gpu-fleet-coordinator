"""Ship-artifact store — derived from docs/specs/ship-artifact-build.spec.md Fixtures.

Covers invariants 2/4/5/6/7/13/14/15 and the `blob_id` address (Output contract).
"""
import importlib.util
import sys
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


store = _load("artifact_store", "fleet/artifact_store.py")
registry = _load("registry_db", "fleet/registry_db.py")
bundle = _load("bundle", "fleet/bundle.py")

BASE = dict(code_hash="abc123", abi="cpython-312-x86_64-linux-gnu",
            packages=["src/native", "src/shared"], entry_source_path="src/native/t.py",
            code_format="compiled")


# ---- blob_id: every build input is part of the address -------------------

def test_blob_id_is_stable_across_calls():
    assert store.blob_id(**BASE) == store.blob_id(**BASE)


@pytest.mark.parametrize("field,other", [
    ("code_hash", "def456"),
    ("abi", "cpython-311-x86_64-linux-gnu"),
    ("packages", ["src/native"]),
    ("entry_source_path", "src/native/other.py"),
    ("code_format", "snapshot"),
])
def test_blob_id_changes_when_any_input_changes(field, other):
    """A key that misses an input silently ships STALE BINARIES — the worst failure class here."""
    assert store.blob_id(**{**BASE, field: other}) != store.blob_id(**BASE)


def test_blob_id_ignores_package_order():
    a = store.blob_id(**{**BASE, "packages": ["src/shared", "src/native"]})
    assert a == store.blob_id(**BASE)


def test_entry_path_is_in_the_address_because_overlay_is_per_task():
    """One compiled tree serves many trainers; each overlays its own entry .py, so two tasks at the
    same commit with different entries are DIFFERENT ship-ready bytes."""
    assert store.blob_id(**{**BASE, "entry_source_path": "src/native/a.py"}) != \
           store.blob_id(**{**BASE, "entry_source_path": "src/native/b.py"})


# ---- put/load: atomic publish (inv. 5) -----------------------------------

def test_put_then_load_roundtrip(tmp_path):
    store.put(tmp_path, "bid1", b"payload")
    assert store.load(tmp_path, "bid1") == b"payload"


def test_load_missing_is_none_not_an_error(tmp_path):
    assert store.load(tmp_path, "nope") is None
    assert store.load(tmp_path, "") is None


def test_put_leaves_no_tmp_file_behind(tmp_path):
    store.put(tmp_path, "bid1", b"x" * 4096)
    leftovers = [p.name for p in store.store_dir(tmp_path).iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_put_is_immutable_first_publisher_wins(tmp_path):
    """⛔ THE INVERSION OF WHAT THIS TEST USED TO ASSERT, and the old assertion was the bug.

    It required the SECOND `put` to win (`os.replace`), on the premise that any blob under a given
    id is as good as any other. It is not: `blob_id` addresses the BUILD INPUTS, the tar.gz is not
    byte-reproducible, and a box caches by id while verifying by SHA-256 (inv. 6). So a second,
    byte-different blob published under a live id makes every task recording its digest fail
    integrity against whatever that box already cached — measured live on `pcbed_reopen/cb_bias`,
    `sha256 3d68fe6190ff != manifest 9d465425d297`.

    The guard encoded the same wrong premise as the code, which is why nothing caught it.
    """
    first = store.put(tmp_path, "bid1", b"y" * 9000)
    second = store.put(tmp_path, "bid1", b"z" * 11)
    assert first == b"y" * 9000
    assert second == b"y" * 9000, "the loser must return the WINNER's bytes, not its own"
    assert store.load(tmp_path, "bid1") == b"y" * 9000
    leftovers = [p.name for p in store.store_dir(tmp_path).iterdir() if ".tmp" in p.name]
    assert leftovers == []


# ---- gc: refcount FIRST (inv. 7) -----------------------------------------

def test_gc_never_evicts_a_live_blob_regardless_of_keep_max(tmp_path):
    """The bug in the compile cache this replaces: LRU-capped with NO reference to open tasks, so a
    busy period could evict the very tree a queued task was waiting to ship."""
    for i in range(5):
        store.put(tmp_path, f"b{i}", b"x")
    store.gc(tmp_path, live={"b0", "b1"}, keep_max=1)
    assert store.load(tmp_path, "b0") == b"x"
    assert store.load(tmp_path, "b1") == b"x"


def test_gc_trims_unreferenced_blobs_to_keep_max(tmp_path):
    import os
    import time
    for i in range(6):
        store.put(tmp_path, f"b{i}", b"x")
        os.utime(store.blob_path(tmp_path, f"b{i}"), (time.time() - (10 - i), time.time() - (10 - i)))
    removed = store.gc(tmp_path, live=set(), keep_max=2)
    assert removed == 4
    assert store.load(tmp_path, "b5") == b"x"     # newest survive
    assert store.load(tmp_path, "b0") is None     # oldest go first


def test_gc_on_a_missing_store_is_a_noop(tmp_path):
    assert store.gc(tmp_path / "nothing", live=set(), keep_max=1) == 0


def test_live_blob_ids_covers_open_states_only(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    for tid, state, blob in (("t1", "queued", "b1"), ("t2", "running", "b2"),
                             ("t3", "done", "b3"), ("t4", "task_failed", "b4")):
        conn.execute(
            "INSERT INTO tasks(id, created_at, created_by, grp, name, entrypoint, args_json, "
            "config_json, config_hash, arm_hash, git_sha, est_minutes, state, code_blob, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "2026-07-31T00:00:00Z", "t", "g", tid, "smoke", "[]", "{}", "c" + tid,
             "a" + tid, "sha", 10, state, blob, "2026-07-31T00:00:00Z"))
    conn.commit()
    assert store.live_blob_ids(conn) == {"b1", "b2"}


# ---- build: NO source fallback, either class (inv. 13) -------------------

class _FakeBundle:
    CompileFailed = bundle.CompileFailed
    BundleError = bundle.BundleError

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = 0

    def compile_tree(self, code_tar, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return b"COMPILED:" + code_tar

    def overlay_entry_source(self, compiled, entry_rel, source_tar):
        return compiled + b"|overlay:" + entry_rel.encode()


REC = {"bundle_compile": True, "bundle_compile_abi": BASE["abi"],
       "bundle_compile_packages": BASE["packages"], "bundle_compile_backend": "local",
       "bundle_compile_python": None, "bundle_compile_image": None}


def test_compile_error_raises_build_failed_no_fallback():
    """A compiler-rejected code bug. Today this fails the task at SHIP time, after it has claimed a
    slot on a paid box; now it never gets queued."""
    fb = _FakeBundle(bundle.CompileFailed("bad syntax in foo.py"))
    with pytest.raises(store.BuildFailed) as e:
        store.build_ship_ready(b"src", entry_source_path="src/native/t.py", rec=REC, bundle_mod=fb)
    assert "compile ERROR" in str(e.value) and "bad syntax" in str(e.value)


def test_toolchain_unavailable_ALSO_raises_never_ships_source():
    """The behaviour change the owner asked for: `BundleError` used to degrade to shipping SOURCE.
    That insured against the COORDINATOR's toolchain being a fleet-wide single point of failure —
    building here makes the failure session-local, so the insurance buys nothing."""
    fb = _FakeBundle(bundle.BundleError("no cpython-312 interpreter"))
    with pytest.raises(store.BuildFailed) as e:
        store.build_ship_ready(b"src", entry_source_path="src/native/t.py", rec=REC, bundle_mod=fb)
    assert "toolchain unavailable" in str(e.value)
    assert "THIS session only" in e.value.fatal_hint


def test_successful_build_compiles_then_overlays_the_entry():
    fb = _FakeBundle()
    data, fmt = store.build_ship_ready(b"src", entry_source_path="src/native/t.py", rec=REC,
                                       bundle_mod=fb)
    assert fmt == "compiled"
    assert data == b"COMPILED:src|overlay:src/native/t.py"


def test_bundle_compile_false_ships_source_deliberately():
    """A choice, not a degradation (Q4) — and the only remaining route to `snapshot`."""
    fb = _FakeBundle(bundle.CompileFailed("would have failed"))
    data, fmt = store.build_ship_ready(b"src", entry_source_path="src/native/t.py",
                                       rec={**REC, "bundle_compile": False}, bundle_mod=fb)
    assert (data, fmt) == (b"src", "snapshot")
    assert fb.calls == 0


# ---- build_and_store: build ONCE per snapshot (inv. 4) -------------------

class _CompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _FakeRun:
    """`subprocess.run` for the three subprocesses `build_venv_python` shells out to.

    These tests are about the STORE — blob reuse, the recorded digest, nothing persisted on a
    failed build — and they already fake the COMPILER (`_FakeBundle.compile_tree` ignores
    `python_bin` entirely). But `build_and_store` passes an `experiments_root`, which is exactly
    what makes `build_ship_ready` take the `backend='local'` arm, so each of them was creating a
    REAL `python -m venv` and running a REAL `pip install cython setuptools` **against PyPI** —
    43–65s apiece and a network dependency in a pure-store unit test. (A PyPI `ReadTimeoutError`
    is what surfaced this class of flake, in tests/test_job_manifest.py, 2026-08-25.)

    Faking `run` and not the whole helper keeps `build_venv_python`'s real logic under test — it
    still resolves the interpreter, keys the venv by version and caches it — while the argv the
    fake had to learn is itself the record of what a build shells out to. `calls` lets a test
    assert the venv is prepared ONCE for a 12-cell sweep."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["uv", "python"]:            # resolve_build_python's interpreter lookup
            return _CompletedProcess(stdout="/usr/bin/python3.12\n")
        if argv[1:2] == ["-c"]:                     # the version probe that keys the venv dir
            return _CompletedProcess(stdout="312\n")
        return _CompletedProcess()                  # `-m venv` and `-m pip install`


def test_build_and_store_reuses_an_existing_blob_a_sweep_pays_one_build(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    fb, run = _FakeBundle(), _FakeRun()
    seen = []
    for _ in range(12):        # a 12-cell sweep, one shared snapshot
        out = store.build_and_store(conn, tmp_path, code_tar=b"src", code_hash="h1",
                                    entry_source_path="src/native/t.py", bundle_mod=fb, run=run,
                                    on_reuse=lambda b: seen.append("reuse"),
                                    on_build=lambda b, s: seen.append("build"))
    assert fb.calls == 1                      # compiled ONCE for 12 cells
    assert seen.count("build") == 1 and seen.count("reuse") == 11
    assert out["code_format"] == "compiled"
    assert store.load(tmp_path, out["code_blob"]) is not None
    # …and the toolchain was prepared ONCE, not twelve times: the 11 reuses are served from the
    # store before `build_ship_ready` is reached at all, which is the point of inv. 4.
    assert sum(1 for a in run.calls if a[1:3] == ["-m", "pip"]) == 1


def test_build_and_store_records_the_digest_the_coordinator_rechecks(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    out = store.build_and_store(conn, tmp_path, code_tar=b"src", code_hash="h1",
                                entry_source_path=None, bundle_mod=_FakeBundle(), run=_FakeRun())
    assert out["code_sha256"] == store.digest(store.load(tmp_path, out["code_blob"]))


def test_build_and_store_stores_nothing_when_the_build_fails(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    fb = _FakeBundle(bundle.CompileFailed("nope"))
    with pytest.raises(store.BuildFailed):
        store.build_and_store(conn, tmp_path, code_tar=b"src", code_hash="h1",
                              entry_source_path=None, bundle_mod=fb, run=_FakeRun())
    assert not list(store.store_dir(tmp_path).glob("*")) or \
        all(p.name.startswith(".") for p in store.store_dir(tmp_path).glob("*"))


# ---- recipe: one source of truth for the ABI ----------------------------

def test_recipe_falls_back_to_defaults_on_an_empty_settings_table(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    assert store.recipe(conn)["bundle_compile_abi"] == store.RECIPE_DEFAULTS["bundle_compile_abi"]


def test_recipe_prefers_the_settings_row(tmp_path):
    conn = registry.connect(str(tmp_path / "r.sqlite"))
    conn.execute("INSERT INTO settings(key,value) VALUES ('bundle_compile_abi', '\"cpython-313\"')")
    conn.commit()
    assert store.recipe(conn)["bundle_compile_abi"] == "cpython-313"


def test_recipe_defaults_match_the_dispatchers_so_the_abi_cannot_diverge():
    """Two processes now build for one fleet. A divergent default would ship a `.so` the box cannot
    import — caught here rather than on a paid box."""
    disp = _load("dispatcher", "fleet/dispatcher.py")
    for key, val in store.RECIPE_DEFAULTS.items():
        if key == "bundle_compile_image":
            continue                       # resolved from settings; only used by backend="docker"
        assert disp.DEFAULT_SETTINGS[key] == val, f"{key} diverged from dispatcher.DEFAULT_SETTINGS"
