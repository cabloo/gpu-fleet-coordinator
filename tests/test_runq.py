"""`runq` CLI — exit codes + fixtures from docs/specs/run-registry.spec.md's Fixtures section.

Subprocess-level (not importlib) since exit codes and stdout/stderr ARE the contract here.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import pathlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNQ = ROOT / "fleet" / "runq.py"



def _no_compile(db):
    """These tests exercise QUEUEING, not compilation. `runq` now builds a ship-ready artifact at
    add time (ship-artifact-build spec inv. 2), so without this every `add` would run a real ~170s
    cythonize. Seeding the deliberate off-switch (Q4) keeps them fast and still exercises the real
    build path — `build_ship_ready` returns the source tree and records `code_format="snapshot"`.
    Compilation itself is covered by tests/test_artifact_store.py."""
    import sqlite3
    pathlib.Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile', 'false')")
    conn.commit()
    conn.close()


def _runq(db, *args, actor="runq-tests"):
    # run-registry invariant 15: add/submit need a usable actor. Inject a deterministic $RUNQ_ACTOR
    # so tests don't depend on the ambient git branch; pass actor=None to exercise the no-actor path.
    env = dict(os.environ)
    env.pop("RUNQ_ACTOR", None)
    if actor is not None:
        env["RUNQ_ACTOR"] = actor
    _no_compile(db)
    return subprocess.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                           capture_output=True, text=True, cwd=ROOT, env=env)


def test_bootstrap_against_missing_db_is_idempotent(tmp_path):
    db = tmp_path / "runs.sqlite"
    r1 = _runq(db, "ls")
    assert r1.returncode == 0
    assert db.exists()
    r2 = _runq(db, "ls")
    assert r2.returncode == 0
    assert r1.stdout == r2.stdout == ""


def test_add_dedupe_and_arm_warning_and_cancel_and_illegal_transition(tmp_path):
    db = tmp_path / "runs.sqlite"
    r1 = _runq(db, "add", "--group", "demo", "--name", "t1", "--entrypoint", "smoke",
                "--est-minutes", "5", "--", "--updates", "10")
    assert r1.returncode == 0
    task_id = r1.stdout.strip()

    r2 = _runq(db, "add", "--group", "demo", "--name", "t2", "--entrypoint", "smoke",
                "--est-minutes", "5", "--", "--updates", "10")
    assert r2.returncode == 3
    assert task_id in r2.stderr

    r3 = _runq(db, "add", "--group", "demo", "--name", "t3", "--entrypoint", "smoke",
                "--est-minutes", "5", "--", "--updates", "10", "--seed", "4")
    assert r3.returncode == 0
    assert "same arm as" in r3.stderr

    # --reason is REQUIRED (owner directive 2026-07-31): a bare `cancelled` cannot be told apart
    # from a budget-complete stop, which corrupts every cost/completion read over the registry.
    r_noreason = _runq(db, "cancel", task_id)
    assert r_noreason.returncode == 2
    assert "--reason" in r_noreason.stderr

    r4 = _runq(db, "cancel", task_id, "--reason", "flops budget reached")
    assert r4.returncode == 0

    r5 = _runq(db, "cancel", task_id, "--reason", "flops budget reached")
    assert r5.returncode == 4  # illegal: already cancelled


def test_add_init_from_sets_resume_checkpoint(tmp_path):
    # Cross-task init handoff (2026-07-09): --init-from <ckpt> sets resume_checkpoint so the
    # dispatcher ships it as resume.pt and wires the entrypoint's --init-from (adaptation runs).
    import sqlite3
    db = tmp_path / "runs.sqlite"
    ckpt = tmp_path / "pretrain.pt"
    ckpt.write_bytes(b"fake-checkpoint")
    r = _runq(db, "add", "--group", "demo", "--name", "adapt", "--entrypoint", "smoke",
              "--est-minutes", "5", "--init-from", str(ckpt), "--", "--updates", "10")
    assert r.returncode == 0, r.stderr
    tid = r.stdout.strip()
    rc = sqlite3.connect(str(db)).execute(
        "SELECT resume_checkpoint FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert rc == str(ckpt.resolve())


def test_add_init_from_missing_file_errors(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "adapt", "--entrypoint", "smoke",
              "--est-minutes", "5", "--init-from", str(tmp_path / "nope.pt"), "--", "--updates", "10")
    assert r.returncode == 2
    assert "not found" in r.stderr


def test_handshake_failure_no_task_row_no_event(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "bad", "--entrypoint", "smoke",
              "--est-minutes", "5", "--", "--bogus-flag", "1")
    assert r.returncode == 2
    r2 = _runq(db, "ls", "--json")
    assert json.loads(r2.stdout) == []


def test_unknown_entrypoint_is_validation_error(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "x", "--entrypoint", "nope",
              "--est-minutes", "5", "--")
    assert r.returncode == 2


def test_non_positive_est_minutes_rejected(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "x", "--entrypoint", "smoke",
              "--est-minutes", "0", "--")
    assert r.returncode == 2


def test_rate_and_spend_default_zero(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "rate", "--json")
    assert json.loads(r.stdout) == {"rate": 0.0}
    r2 = _runq(db, "spend", "--json")
    assert json.loads(r2.stdout) == {"spend": 0.0}


# --- Actor required (run-registry invariant 15) --------------------------------------------------

def _created_by(db, tid):
    return sqlite3.connect(str(db)).execute(
        "SELECT created_by FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_add_explicit_by_beats_env_and_is_recorded(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "t1", "--entrypoint", "smoke",
              "--est-minutes", "5", "--by", "feat-x", "--", "--updates", "10", actor="env-agent")
    assert r.returncode == 0, r.stderr
    assert _created_by(db, r.stdout.strip()) == "feat-x"


def test_add_actor_from_env(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "t1", "--entrypoint", "smoke",
              "--est-minutes", "5", "--", "--updates", "10", actor="cool-agent")
    assert r.returncode == 0, r.stderr
    assert _created_by(db, r.stdout.strip()) == "cool-agent"


def test_add_rejects_reserved_by(tmp_path):
    db = tmp_path / "runs.sqlite"
    for bad in ("master", "main", "unknown", "HEAD"):
        r = _runq(db, "add", "--group", "demo", "--name", f"n_{bad}", "--entrypoint", "smoke",
                  "--est-minutes", "5", "--by", bad, "--", "--updates", "10")
        assert r.returncode == 2, (bad, r.stdout, r.stderr)
        assert "--by is required" in r.stderr
    assert json.loads(_runq(db, "ls", "--json").stdout) == []  # nothing inserted


def test_add_rejects_malformed_by(tmp_path):
    db = tmp_path / "runs.sqlite"
    r = _runq(db, "add", "--group", "demo", "--name", "n", "--entrypoint", "smoke",
              "--est-minutes", "5", "--by", "bad name!", "--", "--updates", "10")
    assert r.returncode == 2
    assert json.loads(_runq(db, "ls", "--json").stdout) == []


def _load_runq():
    spec = importlib.util.spec_from_file_location("runq_mod", RUNQ)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_actor_underivable_is_error(monkeypatch):
    # No --by, no $RUNQ_ACTOR, and no usable branch (detached / non-git) -> exit 2, not 'unknown'.
    runq = _load_runq()
    monkeypatch.delenv("RUNQ_ACTOR", raising=False)
    monkeypatch.setattr(runq, "_git_branch", lambda *a, **k: "")
    label, err = runq._resolve_actor(argparse.Namespace(by=None, cmd="add"))
    assert label is None and err == 2


def test_resolve_actor_rejects_reserved_branch(monkeypatch):
    # Auto-derivation must not silently adopt the trunk branch as an identity.
    runq = _load_runq()
    monkeypatch.delenv("RUNQ_ACTOR", raising=False)
    monkeypatch.setattr(runq, "_git_branch", lambda *a, **k: "master")
    label, err = runq._resolve_actor(argparse.Namespace(by=None, cmd="add"))
    assert label is None and err == 2


def test_resolve_actor_from_branch(monkeypatch):
    runq = _load_runq()
    monkeypatch.delenv("RUNQ_ACTOR", raising=False)
    monkeypatch.setattr(runq, "_git_branch", lambda *a, **k: "worktree-cool-feature")
    label, err = runq._resolve_actor(argparse.Namespace(by=None, cmd="add"))
    assert err is None and label == "worktree-cool-feature"


def test_force_readd_under_a_taken_name_fails_CLEANLY_not_with_a_traceback(tmp_path):
    """⛔ `(grp, name)` is UNIQUE across EVERY row — `cancelled` and `failed` included.

    The dedup guard refuses a same-config re-add with exit 3 and a clear message, but `--force` is
    precisely the flag that SKIPS that guard, so a re-queue under a previously-used name reached the
    raw `sqlite3.IntegrityError` instead. `cmd_sweep` has caught that since it was written; the
    SINGLE-TASK path never did — and the single-task path is the one you reach for when re-queueing a
    CANCELLED arm, which is exactly the moment the old name is still taken.

    Hit for real on 2026-08-15 repinning a paired scout off a box whose declared per-lane budget
    could not admit it. The contract is: non-zero exit, the offending name, the blocking task's state,
    and an instruction — never a constraint string the caller has to decode.
    """
    db = tmp_path / "runs.sqlite"
    r1 = _runq(db, "add", "--group", "demo", "--name", "arm", "--entrypoint", "smoke",
               "--est-minutes", "5", "--", "--updates", "10")
    assert r1.returncode == 0
    first = r1.stdout.strip()

    r2 = _runq(db, "cancel", first, "--reason", "repin to a different box")
    assert r2.returncode == 0

    # A DIFFERENT config under the SAME name, forced past the dedup guard: the collision is the
    # UNIQUE constraint, and the cancelled row still holds the name.
    r3 = _runq(db, "add", "--group", "demo", "--name", "arm", "--entrypoint", "smoke",
               "--est-minutes", "5", "--force", "--", "--updates", "10", "--seed", "7")
    assert r3.returncode == 2, f"expected a clean refusal, got {r3.returncode}: {r3.stderr}"
    assert "IntegrityError" not in r3.stderr and "Traceback" not in r3.stderr, \
        f"leaked a raw exception instead of a message: {r3.stderr}"
    assert "ALREADY TAKEN" in r3.stderr
    assert "demo/arm" in r3.stderr
    assert "cancelled" in r3.stderr, "must say WHY the name is held, or the fix is not obvious"

    # ...and the escape hatch works, so the message is actionable rather than merely polite.
    r4 = _runq(db, "add", "--group", "demo", "--name", "armb", "--entrypoint", "smoke",
               "--est-minutes", "5", "--", "--updates", "10", "--seed", "7")
    assert r4.returncode == 0
