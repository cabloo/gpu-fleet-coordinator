"""Job artifact contract — self-describing jobs, artifact-agnostic coordinator.

Spec: docs/specs/job-artifact-contract.spec.md. Covers the manifest schema/validation, the
entrypoints.resolve() seam (manifest vs table fallback), the git-optional + hoisted-config
snapshot, the registry migration, and the runq submit / add --config end-to-end paths.

v2 (2026-07-27): there is NO `job.json`. The run contract is a reserved `job` section of the
trainer config, and the caller names that config in full. These tests pin the properties that made
v1 unsafe: no filename is magic, nothing is inherited, and a config with no `job` section is a
hard reject rather than a fallback.
"""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# fleet on path for the sibling imports the modules below do at import time. `src` needs no
# insert — `pythonpath = ["src", "tests"]` in pyproject.toml already prepends it.
sys.path.insert(0, str(ROOT / "fleet"))
reg = _load("registry_db", "fleet/registry_db.py")
cs = _load("code_snapshot", "fleet/code_snapshot.py")
jm = _load("job_manifest", "fleet/job_manifest.py")
ep = _load("entrypoints", "fleet/entrypoints.py")
runq = _load("runq", "fleet/runq.py")


@pytest.fixture(autouse=True)
def _deterministic_actor(monkeypatch):
    # run-registry invariant 15: add/submit need a usable `created_by`, resolved from
    # --by / $RUNQ_ACTOR / the current git branch. These end-to-end tests call `runq.main`
    # without --by, so on a `master`/`main`/detached checkout branch resolution would reject
    # the actor and the command would exit 2 — a false failure that depends only on which
    # branch the suite runs from. Pin a deterministic actor so the tests exercise the manifest
    # paths regardless of checkout (mirrors test_runq.py's $RUNQ_ACTOR injection).
    monkeypatch.setenv("RUNQ_ACTOR", "job-manifest-test")


def _full_manifest():
    return {
        "manifest_version": 1,
        "run": ["python", "-m", "native.training.curriculum"],
        "completion_artifact": "results.json",
        "resume": {"flag": "--init-from", "checkpoint": "ckpt_latest.pt"},
        "setup": {"pip": ["numpy>=2,<3", "tensorboard"], "apt": ["libegl1"]},
        "resources": {"slots": 1, "vram_gb": 3, "cores": 2, "est_minutes": 240},
    }


# --------------------------------------------------------------------------- parse / validate

def test_parse_valid_and_to_entrypoint():
    m = jm.parse(_full_manifest())
    e = jm.to_entrypoint(m)
    hand = ep.Entrypoint(
        argv=["python", "-m", "native.training.curriculum"], completion_artifact="results.json",
        resume_flag="--init-from", live=True, pip_extras=["numpy>=2,<3", "tensorboard"],
        apt_packages=["libegl1"])
    assert e.argv == hand.argv
    assert e.completion_artifact == hand.completion_artifact
    assert e.resume_flag == hand.resume_flag
    assert e.pip_extras == hand.pip_extras
    assert e.apt_packages == hand.apt_packages


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("run"),
    lambda d: d.pop("completion_artifact"),
    lambda d: d.__setitem__("run", "python -m x"),          # not a list
    lambda d: d.__setitem__("run", [1, 2]),                  # not list[str]
    lambda d: d.__setitem__("run", []),                      # empty
    lambda d: d.__setitem__("manifest_version", 999),
    lambda d: d.__setitem__("completion_artifact", "/abs/results.json"),
    lambda d: d.__setitem__("completion_artifact", "../escape.json"),
    lambda d: d.__setitem__("completion_artifact", ""),
    lambda d: d.__setitem__("setup", {"pip": "numpy"}),      # pip not a list
    lambda d: d.__setitem__("resume", {"checkpoint": "x"}),  # resume missing flag
    lambda d: d.__setitem__("resources", {"est_minutes": "lots"}),  # non-numeric
])
def test_parse_invalid_raises(mutate):
    d = _full_manifest()
    mutate(d)
    with pytest.raises(jm.JobManifestError):
        jm.parse(d)


def test_defaults_for_minimal_manifest():
    m = jm.parse({"manifest_version": 1, "run": ["python", "go.py"],
                  "completion_artifact": "out.json"})
    assert m.resume_flag is None
    assert m.resume_checkpoint == "ckpt_latest.pt"
    assert m.pip == [] and m.apt == []
    assert m.resources == {}


def test_to_dict_roundtrips_through_parse():
    m = jm.parse(_full_manifest())
    assert jm.parse(jm.to_dict(m)) == m


# --------------------------------------------------------------------------- readers

def _tar_with(members):  # members: list[(name, bytes)] -> gzipped tar bytes, in given order
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _cfg_bytes(job=None, **extra):
    """A trainer config carrying the reserved `job` section (v2)."""
    d = dict(extra)
    if job is not None:
        d["job"] = job
    return json.dumps(d).encode()


def test_config_without_a_job_section_is_absent_not_inferred(tmp_path):
    """The v1 bug in one assertion: absence must be ABSENCE, never a fallback to some other file."""
    p = tmp_path / "plain.json"
    p.write_text(json.dumps({"seeds": [0]}))
    assert jm.read_config(p) is None
    assert jm.from_config(p) is None
    assert jm.read_tar(_tar_with([("c.json", _cfg_bytes(None, seeds=[0]))]), "c.json") is None


def test_read_config_missing_file_raises(tmp_path):
    with pytest.raises(jm.JobManifestError):
        jm.read_config(tmp_path / "nope.json")


def test_read_tar_finds_the_NAMED_config_regardless_of_order():
    cfg = _cfg_bytes(_full_manifest())
    big = b"y" * (256 * 1024)
    # named member first (the hoist optimization) AND last (correctness is order-independent)
    for members in ([("configs/x.json", cfg), ("src/big.py", big)],
                    [("src/big.py", big), ("configs/x.json", cfg)]):
        got = jm.read_tar(_tar_with(members), "configs/x.json")
        assert got["run"] == _full_manifest()["run"]


def test_read_tar_absent_member_raises_naming_the_path():
    """v1 returned None for a missing job.json and the caller printed a generic message. v2 names
    the path, because 'you asked for a config that is not in the tar' is a caller error."""
    with pytest.raises(jm.JobManifestError, match="typo.json"):
        jm.read_tar(_tar_with([("configs/x.json", _cfg_bytes(_full_manifest()))]), "typo.json")


def test_no_member_is_magic_by_filename():
    """A tar carrying a v1-style job.json is NOT readable without naming it — the magic filename is
    gone, so an old artifact cannot be silently picked up as if it were the contract."""
    tar = _tar_with([("job.json", json.dumps(_full_manifest()).encode())])
    with pytest.raises(jm.JobManifestError):
        jm.read_tar(tar, "configs/x.json")


def test_run_may_not_carry_its_own_config():
    """Two sources for one path is how a cell ends up running a config nobody named."""
    m = _full_manifest()
    m["run"] = ["python", "-m", "native.train", "--config", "configs/other.json"]
    with pytest.raises(jm.JobManifestError, match="--config"):
        jm.parse(m)


def test_read_tar_rejects_non_tar():
    with pytest.raises(jm.JobManifestError):
        jm.read_tar(b"this is not a gzip tar", "configs/x.json")


# --------------------------------------------------------------------------- resolve() seam

def test_resolve_prefers_manifest_over_table():
    row = {"job_manifest_json": json.dumps({
        "manifest_version": 1, "run": ["python", "-m", "native.brand_new"],
        "completion_artifact": "results.json"}), "entrypoint": "native.brand_new"}
    e = ep.resolve(row)
    assert e.argv == ["python", "-m", "native.brand_new"]  # NOT in ENTRYPOINTS — cannot be unknown


def test_resolve_falls_back_to_table_when_no_manifest():
    e = ep.resolve({"job_manifest_json": None, "entrypoint": "smoke"})
    assert e.completion_artifact == ep.ENTRYPOINTS["smoke"].completion_artifact


def test_resolve_unknown_named_entrypoint_still_errors():
    with pytest.raises(SystemExit):
        ep.resolve({"job_manifest_json": None, "entrypoint": "does_not_exist"})


# --------------------------------------------------------------------------- snapshot: git-optional

def test_non_git_snapshot_walk_and_job_first(tmp_path):
    d = tmp_path / "artifact"
    (d / "src").mkdir(parents=True)
    (d / "cfg.json").write_text(json.dumps({"job": _full_manifest()}))
    (d / "src" / "trainer.py").write_text("print('hi')\n")
    (d / ".dispatchignore").write_text("*.log\nscratch\n")
    (d / "debug.log").write_text("noise\n")
    (d / "scratch").mkdir(); (d / "scratch" / "tmp.py").write_text("junk\n")
    (d / "__pycache__").mkdir(); (d / "__pycache__" / "x.pyc").write_bytes(b"\0")

    snap = cs.make_snapshot(d, hoist="cfg.json")  # not a git tree -> plain walk
    with tarfile.open(fileobj=io.BytesIO(snap.code_tar), mode="r:gz") as tf:
        members = [m.name for m in tf.getmembers() if m.isfile()]
    assert "cfg.json" in members and "src/trainer.py" in members
    assert "debug.log" not in members            # .dispatchignore glob
    assert "scratch/tmp.py" not in members        # .dispatchignore dir
    assert not any(n.endswith(".pyc") for n in members)  # default denylist
    assert members[0] == "cfg.json"               # inv. 10a: the NAMED config hoisted to member 0
    # v1 force-included an untracked repo-root job.json here; v2 must not resurrect a magic name.
    assert cs.make_snapshot(d).code_hash == snap.code_hash  # hoist is write-order only, not identity


def test_snapshot_allow_non_git_false_rejects(tmp_path):
    d = tmp_path / "plain"; d.mkdir()
    (d / "a.py").write_text("x=1\n")
    with pytest.raises(cs.SnapshotError):
        cs.make_snapshot(d, allow_non_git=False)


# --------------------------------------------------------------------------- registry migration

def test_migration_v1_to_v2_adds_column(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.sqlite")
    raw = sqlite3.connect(db)
    # Minimal v1-era tasks table WITHOUT job_manifest_json.
    raw.executescript("""
      CREATE TABLE tasks (id TEXT PRIMARY KEY, entrypoint TEXT NOT NULL, state TEXT NOT NULL);
      PRAGMA user_version=1;
    """)
    raw.execute("INSERT INTO tasks(id, entrypoint, state) VALUES ('t0','smoke','done')")
    raw.commit(); raw.close()

    conn = reg.connect(db)  # should migrate 1 -> SCHEMA_VERSION in place
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "job_manifest_json" in cols
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    assert ver == reg.SCHEMA_VERSION
    assert conn.execute("SELECT state FROM tasks WHERE id='t0'").fetchone()[0] == "done"


# --------------------------------------------------------------------------- runq end-to-end

def _min_db(tmp_path):
    """A DB path seeded with the deliberate no-compile off-switch (Q4), like `_no_compile` in
    test_runq.py / test_runq_sweep.py / test_runq_manifest_slots.py / test_runq_probe_priority.py.

    These tests exercise the MANIFEST paths, not compilation. `runq` builds a ship-ready artifact at
    add time (ship-artifact-build spec inv. 2), and with `bundle_compile` left at its default TRUE
    each `add`/`submit` here created a fresh `<tmp_path>/.dispatcher/buildenv-py312` venv and ran a
    real `pip install cython setuptools` **against PyPI** — so this file made four network round
    trips it has no interest in and took ~190s, and a PyPI `ReadTimeoutError` failed
    `test_add_config_end_to_end` outright (observed 2026-08-25; it passed on retry). The off-switch
    still exercises the real build path — `build_ship_ready` returns the source tree and records
    `code_format="snapshot"`. Compilation itself is covered by tests/test_artifact_store.py."""
    import sqlite3
    db = tmp_path / "runs.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile', 'false')")
    conn.commit()
    conn.close()
    return str(db)


def _runnable_job_dir(tmp_path, name="job"):
    """A non-git tree whose CONFIG carries the run contract (v2). Returns (dir, config path)."""
    d = tmp_path / name; (d / "src").mkdir(parents=True)
    (d / "cfg.json").write_text(json.dumps({
        "job": {"manifest_version": 1, "run": ["python", "train.py"],
                "completion_artifact": "results.json",
                "resume": {"flag": "--init-from"},
                "resources": {"est_minutes": 7}},
        "lr": 0.1}))
    (d / "train.py").write_text(
        "import json, sys\n"
        "if '--print-run-identity' in sys.argv:\n"
        "    print(json.dumps({'config': {'lr': 0.1, 'seed': 0}})); sys.exit(0)\n")
    return d, d / "cfg.json"


def test_add_config_end_to_end(tmp_path, capsys):
    db = _min_db(tmp_path)
    d, cfg = _runnable_job_dir(tmp_path)
    rc = runq.main(["--db", db, "add", "--group", "g", "--name", "n1", "--config", str(cfg)])
    assert rc == 0
    task_id = capsys.readouterr().out.strip()

    conn = reg.connect(db)
    row = dict(reg.get_task(conn, task_id))
    assert row["job_manifest_json"]                       # manifest stored on the row
    assert row["entrypoint"] == "train.py"                # readable display label
    assert row["est_minutes"] == 7                        # from resources
    assert row["git_sha"] == ""                           # non-git dir -> optional provenance empty
    # THE POINT OF v2: the config the caller named is in the args, so the run is self-describing.
    assert json.loads(row["args_json"])[:2] == ["--config", "cfg.json"]
    # The dispatcher would resolve the run contract from the row, no entrypoints.py entry needed.
    e = ep.resolve(row)
    assert e.argv == ["python", "train.py"] and e.completion_artifact == "results.json"
    # And the snapshot was persisted under the DB's parent, the NAMED CONFIG hoisted to member 0.
    snap = cs.load(Path(db).parent, task_id)
    assert snap is not None
    with tarfile.open(fileobj=io.BytesIO(snap[0]), mode="r:gz") as tf:
        assert tf.getmembers()[0].name == "cfg.json"


def test_add_rejects_a_config_with_no_job_section(tmp_path):
    """No inference, no fallback: the whole point of killing the root job.json."""
    db = _min_db(tmp_path)
    d, _ = _runnable_job_dir(tmp_path)
    plain = d / "plain.json"; plain.write_text(json.dumps({"lr": 0.2}))
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "n2",
                      "--config", str(plain)]) == 2


def test_submit_prebuilt_tar_end_to_end(tmp_path, capsys):
    db = _min_db(tmp_path)
    d, _ = _runnable_job_dir(tmp_path)
    tar = cs.make_snapshot(d).code_tar                    # a pre-built artifact
    art = tmp_path / "code.tar.gz"; art.write_bytes(tar)

    rc = runq.main(["--db", db, "submit", str(art), "--group", "g", "--name", "s1",
                    "--config", "cfg.json", "--est-minutes", "9"])
    assert rc == 0
    task_id = capsys.readouterr().out.strip()

    conn = reg.connect(db)
    row = dict(reg.get_task(conn, task_id))
    assert row["job_manifest_json"] and row["git_sha"] == ""
    assert row["est_minutes"] == 9                        # CLI override wins over resources
    # Persisted snapshot equals the submitted bytes' content address (inv. 12), no git involved.
    snap = cs.load(Path(db).parent, task_id)
    assert snap is not None and snap[1] == hashlib.sha256(tar).hexdigest()
    assert ep.resolve(row).completion_artifact == "results.json"


def test_submit_rejects_artifact_whose_named_config_is_absent(tmp_path):
    db = _min_db(tmp_path)
    tar = _tar_with([("src/a.py", b"x=1\n")])             # no such config member
    art = tmp_path / "bad.tar.gz"; art.write_bytes(tar)
    rc = runq.main(["--db", db, "submit", str(art), "--group", "g", "--name", "b1",
                    "--config", "cfg.json", "--est-minutes", "5"])
    assert rc == 2


def test_add_requires_exactly_one_of_entrypoint_or_config(tmp_path):
    db = _min_db(tmp_path)
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "x"]) == 2   # neither
    d, cfg = _runnable_job_dir(tmp_path)
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "y",
                      "--entrypoint", "smoke", "--config", str(cfg)]) == 2       # both


# ------------------------------------------------- resume contract on the manifest path (2026-07-26)
# The named-entrypoint table carried `resume_flag` and the coordinator honoured it. The artifact-
# agnostic path moved that declaration into each job's own job.json, where omitting it is silent:
# `native.training.m49_curriculum_ab` ran 212 tasks over 22 hours with resume_flag=None, so no checkpoint
# was ever pulled and `--init-from` was never re-appended. Eight died with nothing to restart from.

def _job_dir_with(tmp_path, *, est_minutes, resume=True):
    d = tmp_path / f"job{est_minutes}{resume}"; d.mkdir(parents=True)
    m = {"manifest_version": 1, "run": ["python", "train.py"],
         "completion_artifact": "results.json", "resources": {"est_minutes": est_minutes}}
    if resume:
        m["resume"] = {"flag": "--init-from", "checkpoint": "ckpt_latest.pt"}
    (d / "cfg.json").write_text(json.dumps({"job": m}))
    (d / "train.py").write_text(
        "import json, sys\n"
        "if '--print-run-identity' in sys.argv:\n"
        "    print(json.dumps({'config': {'lr': 0.1, 'seed': 0}})); sys.exit(0)\n")
    return d


def _committed_configs_with_job():
    """Every committed config carrying a `job` block.

    Since the opt-out became a declared field (`resume: {"none": ...}`) there is no longer a class
    of job whose rule lives somewhere other than the file, so nothing is out of scope here. The
    previous carve-out — "standalone flag-driven jobs are governed by the ADD-TIME refusal, not a
    declaration in the file" — WAS the divergence: it conceded that the queue-time gate and this
    audit could not agree, because the opt-out lived on a CLI flag that was never persisted."""
    for p in sorted(ROOT.glob("configs/**/*.json")):
        if p.name.endswith(".sweep.json"):
            continue
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if isinstance(d, dict) and isinstance(d.get("job"), dict):
            yield p, d["job"]


def test_every_committed_manifest_declares_resume():
    """Every queueable config is a job we intend to actually spend money on.

    This is a BACKSTOP, not the contract. The contract is `job_manifest.check_submittable`, which
    runs inside `runq add`/`submit` at the point of spend — a test only fires when somebody runs
    the suite, so it must never be the only thing standing between a preemption and a lost run.
    What this adds is COVERAGE the runtime gate structurally cannot have: a config committed and
    never queued has crossed no trust boundary, so nothing at runtime has ever looked at it."""
    missing = []
    for p, job in _committed_configs_with_job():
        # THE SAME FUNCTION the queue-time gate calls — deliberately not a re-implementation. A
        # second copy of the predicate is precisely how this audit and the gate drifted apart:
        # 33b10735 taught the TEST to honour `_resume_note` while the runtime kept ignoring it, so
        # the suite went green over a rule the coordinator was still not applying.
        if jm.check_submittable(jm.parse(job)):
            missing.append(str(p.relative_to(ROOT)))
    assert not missing, (f"configs whose `job` block neither declares `resume.flag` nor documents "
                         f"`resume.none`: {missing}. A preemption restarts these from scratch — "
                         f"add resume.flag (and honour it), or say why it cannot resume.")


def test_the_resume_opt_out_still_fails_for_the_reason_it_names():
    """The exemption must not be a hole: a long job with no resume block at all is still refused,
    and a blank reason does not buy the exemption."""
    long_job = {"manifest_version": 1, "run": ["python", "go.py"],
                "completion_artifact": "out.json", "resources": {"est_minutes": 30}}
    assert jm.check_submittable(jm.parse(long_job))                          # nothing said
    with pytest.raises(jm.JobManifestError):                                 # nothing said, loudly
        jm.parse({**long_job, "resume": {"none": "   "}})
    assert not jm.check_submittable(
        jm.parse({**long_job, "resume": {"none": "writes its artifact once, at the end"}}))


def test_a_manifest_may_not_both_resume_and_decline_to():
    """`flag` and `none` are contradictory claims about the same job; picking a winner silently is
    how a manifest ends up asserting a checkpoint that never gets written."""
    with pytest.raises(jm.JobManifestError, match="both"):
        jm.parse({"manifest_version": 1, "run": ["python", "go.py"],
                  "completion_artifact": "out.json",
                  "resume": {"flag": "--init-from", "none": "cannot resume"}})


def test_the_declared_opt_out_reaches_the_task_row():
    """The reason must survive onto `job_manifest_json`, or it is the `--no-resume` bug again: an
    opt-out that evaporates when the command returns, leaving nobody able to answer why this run
    had nothing to restart from."""
    m = jm.parse({"manifest_version": 1, "run": ["python", "go.py"],
                  "completion_artifact": "out.json", "resources": {"est_minutes": 30},
                  "resume": {"none": "fits once, emits at the end"}})
    assert jm.to_dict(m)["resume"] == {"none": "fits once, emits at the end"}
    assert jm.parse(jm.to_dict(m)) == m                       # and it round-trips at dispatch


def test_no_job_json_survives_anywhere():
    """v2's headline invariant. A `job.json` is now inert — nothing reads it — so one lying around
    is a trap: it looks authoritative and is ignored. This is the file the root singleton bug lived
    in, and the guard that keeps it from creeping back."""
    stray = [str(p.relative_to(ROOT)) for p in ROOT.rglob("job.json")
             if ".git/" not in str(p) and "worktrees/" not in str(p)]
    assert not stray, (f"job.json files still in the tree: {stray}. The run contract belongs in a "
                       f"`job` section of the trainer config the caller names (v2).")


def test_no_committed_manifest_embeds_a_config():
    """`run` carrying `--config` would fight the path the caller named — the v1 failure exactly."""
    bad = [str(p.relative_to(ROOT)) for p, job in _committed_configs_with_job()
           if "--config" in job.get("run", [])]
    assert not bad, f"`job.run` must not contain --config: {bad}"


def test_add_job_refuses_a_long_job_that_cannot_resume(tmp_path):
    db = _min_db(tmp_path)
    d = _job_dir_with(tmp_path, est_minutes=180, resume=False)
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "n",
                      "--config", str(d / "cfg.json")]) == 2


def test_a_short_job_may_omit_resume(tmp_path):
    """The rule is >10 minutes; a smoke/canary shorter than that is not worth the ceremony."""
    db = _min_db(tmp_path)
    d = _job_dir_with(tmp_path, est_minutes=5, resume=False)
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "n",
                      "--config", str(d / "cfg.json")]) == 0


def test_the_cli_opt_out_is_refused_and_points_at_the_config(tmp_path, capsys):
    """`--no-resume '<reason>'` used to queue the job and DISCARD the reason — runq checked it for
    non-emptiness and never stored it anywhere. That is why a deliberate opt-out was
    indistinguishable from a forgotten resume block, and why the repo-wide audit could not agree
    with the queue-time gate even in principle. It is now refused, with the fix in the message."""
    db = _min_db(tmp_path)
    d = _job_dir_with(tmp_path, est_minutes=180, resume=False)
    base = ["--db", db, "add", "--group", "g", "--config", str(d / "cfg.json")]
    assert runq.main(base + ["--name", "blank", "--no-resume", "   "]) == 2
    assert runq.main(base + ["--name", "reasoned", "--no-resume", "restart is cheap"]) == 2
    assert 'resume": {"none"' in capsys.readouterr().err


def test_a_config_declaring_resume_none_queues(tmp_path):
    """The replacement path end-to-end: the opt-out lives in the file, so it queues AND the reason
    is on the row afterwards."""
    db = _min_db(tmp_path)
    d = _job_dir_with(tmp_path, est_minutes=180, resume=False)
    cfg = d / "cfg.json"
    raw = json.loads(cfg.read_text())
    raw["job"]["resume"] = {"none": "single-shot fit, emits only at the end"}
    cfg.write_text(json.dumps(raw))
    assert runq.main(["--db", db, "add", "--group", "g", "--name", "ok", "--config", str(cfg)]) == 0


# `test_no_resume_opt_out_requires_a_reason` lived here. It asserted that
# `--no-resume '<reason>'` QUEUES the job (exit 0) — the behaviour this change removes, because the
# reason it demanded was then thrown away. Its two halves are now
# `test_the_cli_opt_out_is_refused_and_points_at_the_config` (the flag is refused) and
# `test_a_config_declaring_resume_none_queues` (the declared opt-out is what queues).
