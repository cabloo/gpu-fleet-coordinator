"""`runq --colocate` + `runq colocate` — the queue-time and the AUDIT half of dispatcher inv. 4g.

Co-location is best-effort by construction: if the pinned box is torn down mid-campaign the group
re-pins rather than wedging forever. So "I asked for co-location" is a WEAKER claim than "the arms
were co-located", and only the second one licenses a paired verdict. `runq colocate --verify` is
what turns the difference into an answer — it reads `tasks.instance_id`, the fact stamped at claim,
and fails for exactly the reason it names. A published campaign once co-located 2 of its 3 pairs
with nothing in the outputs saying so; this is the check that would have caught it.

Subprocess-level, like `test_runq.py`: exit codes and stdout ARE the contract.
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNQ = ROOT / "fleet" / "runq.py"


def _no_compile(db):
    pathlib.Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('bundle_compile', 'false')")
    conn.commit()
    conn.close()


def _runq(db, *args, actor="colocate-tests"):
    env = dict(os.environ)
    env.pop("RUNQ_ACTOR", None)
    env["RUNQ_ACTOR"] = actor
    _no_compile(db)
    return subprocess.run([sys.executable, str(RUNQ), "--db", str(db), *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def _add(db, name, *extra, seed=None):
    args = ["add", "--group", "camp", "--name", name, "--entrypoint", "smoke",
            "--est-minutes", "5", *extra, "--", "--updates", "10"]
    if seed is not None:
        args += ["--seed", str(seed)]
    return _runq(db, *args)


def _set_instance(db, task_name, instance_id):
    """Stand in for the dispatcher's claim, which is the only writer of `instance_id`."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE tasks SET instance_id=? WHERE name=?", (instance_id, task_name))
    conn.commit()
    conn.close()


def _started_on(db, task_name, *instance_ids):
    """Stand in for the worker's `start` rows — what the task ACTUALLY ran on. More than one means
    it was requeued onto a second box mid-campaign, and `tasks.instance_id` then shows only the
    last."""
    conn = sqlite3.connect(str(db))
    tid = conn.execute("SELECT id FROM tasks WHERE name=?", (task_name,)).fetchone()[0]
    for iid in instance_ids:
        conn.execute("INSERT INTO events(t, task_id, instance_id, event, detail) "
                     "VALUES ('2026-08-16T00:00:00Z',?,?,'start','')", (tid, iid))
    conn.execute("UPDATE tasks SET instance_id=? WHERE name=?", (instance_ids[-1], task_name))
    conn.commit()
    conn.close()


def test_the_flag_lands_in_the_resource_hint(tmp_path):
    db = tmp_path / "runs.sqlite"
    assert _add(db, "a", "--colocate", "camp:seed1", seed=1).returncode == 0
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE name='a'").fetchone()
    assert json.loads(row["resource_hint_json"]) == {"colocate": "camp:seed1"}


def test_box_and_colocate_are_refused_TOGETHER_at_the_point_of_spend(tmp_path):
    """They are opposite requests — one says "I chose the machine", the other "you choose it".
    Any precedence rule silently makes one of them a no-op, which is the class of failure the
    feature exists to remove, so the combination is refused rather than resolved."""
    db = tmp_path / "runs.sqlite"
    r = _add(db, "a", "--colocate", "camp:seed1", "--box", "laptop-gpu", seed=1)
    assert r.returncode == 2 and "mutually exclusive" in r.stderr, r.stderr
    assert _rows(db) == []


def _rows(db):
    if not Path(db).exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tasks")]
    except sqlite3.OperationalError:
        return []


def test_verify_PASSES_when_the_arms_shared_a_box(tmp_path):
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "arm", "--colocate", "camp:seed1", seed=2).returncode == 0
    _set_instance(db, "ctrl", 42)
    _set_instance(db, "arm", 42)
    r = _runq(db, "colocate", "--verify")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout and "camp:seed1" in r.stdout


def test_verify_FAILS_and_NAMES_the_group_when_the_arms_were_split(tmp_path):
    """⛔ THE ONE THAT MATTERS. A split pair is not a paired measurement: the box selects the
    attractor, so a control that collapsed on the other machine manufactures a win. This must be a
    non-zero exit an agent cannot report past, not a line in a log."""
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "arm", "--colocate", "camp:seed1", seed=2).returncode == 0
    _set_instance(db, "ctrl", 42)
    _set_instance(db, "arm", 43)
    r = _runq(db, "colocate", "--verify")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "SPLIT" in r.stdout
    assert "camp:seed1" in r.stderr and "not paired" in r.stderr


def test_a_REQUEUED_arm_that_ran_on_two_boxes_is_a_SPLIT(tmp_path):
    """⛔ THE READ HAS TO COME FROM `events`, NOT `tasks.instance_id`.

    That column holds only the LATEST placement, so an arm requeued onto a second box reports as if
    it had only ever run on the second — and it is exactly the arm worth catching, because it is
    also the one with `resumes > 0`. Reading the column makes the split invisible precisely where it
    is real; `paired_seed_diff.boxes_for` already paid for this lesson (`m51_present_seeds`), and
    this test is why `runq colocate` shares that function instead of re-deriving the read.

    Here both arms' `instance_id` say 42 — a column-based check would report a clean OK."""
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "arm", "--colocate", "camp:seed1", seed=2).returncode == 0
    _started_on(db, "ctrl", 42)
    _started_on(db, "arm", 43, 42)          # preempted off 43, resumed on 42
    conn = sqlite3.connect(str(db))
    assert {r[0] for r in conn.execute("SELECT instance_id FROM tasks")} == {42}
    conn.close()
    r = _runq(db, "colocate", "--verify")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "SPLIT" in r.stdout and "43" in r.stdout


def test_an_unplaced_group_is_PENDING_not_a_failure(tmp_path):
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    r = _runq(db, "colocate", "--verify")
    assert r.returncode == 0 and "PENDING" in r.stdout, (r.stdout, r.stderr)


def test_groups_are_reported_separately_and_can_be_filtered(tmp_path):
    db = tmp_path / "runs.sqlite"
    assert _add(db, "s1a", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "s2a", "--colocate", "camp:seed2", seed=2).returncode == 0
    _set_instance(db, "s1a", 42)
    _set_instance(db, "s2a", 43)
    r = _runq(db, "colocate", "--json")
    got = {g["key"]: g for g in json.loads(r.stdout)}
    assert set(got) == {"camp:seed1", "camp:seed2"}
    # ...and two seeds on two DIFFERENT boxes is the feature working, never a split.
    assert all(g["verdict"] == "OK" for g in got.values()), got
    r2 = _runq(db, "colocate", "--json", "--key", "camp:seed2")
    assert [g["key"] for g in json.loads(r2.stdout)] == ["camp:seed2"]


def test_release_drops_the_pin_so_the_group_can_re_pin(tmp_path):
    db = tmp_path / "runs.sqlite"
    assert _add(db, "s1a", "--colocate", "camp:seed1", seed=1).returncode == 0
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO colocations(key, instance_id, pinned_at, pinned_by) "
                 "VALUES ('camp:seed1', 7, '2026-08-16T00:00:00Z', 'x')")
    conn.commit()
    conn.close()
    r = _runq(db, "colocate", "--release", "camp:seed1")
    assert r.returncode == 0 and "released" in r.stdout and "box 7" in r.stdout
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM colocations").fetchone()[0] == 0


def test_verify_FAILS_when_the_selector_matched_NOTHING(tmp_path):
    """⛔ A VERIFY THAT MATCHED NOTHING IS NOT A PASS — it is a check that never ran.

    Measured 2026-08-18: `runq colocate --group bedknob:seeds1 --verify` printed "no colocation
    groups match" and exited 0, because `--group` takes the TASK GROUP while `bedknob:seeds1` is the
    colocation KEY. An agent that reads only the exit code records "co-location verified" for a pair
    it never looked at — the audit half of inv. 4g silently reporting success on an empty set. That
    is precisely the failure `--verify` exists to prevent, one level up: the check must be able to
    fail for the reason it names."""
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "arm", "--colocate", "camp:seed1", seed=2).returncode == 0
    _set_instance(db, "ctrl", 42)
    _set_instance(db, "arm", 42)
    # The group IS co-located, so this is not a split — but the selector names the KEY, not the
    # task group, and therefore matches nothing.
    r = _runq(db, "colocate", "--group", "camp:seed1", "--verify")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "matched NO colocation group" in r.stderr
    assert "not a pass" in r.stderr
    # It must also say HOW to fix it, or the next reader repeats the mistake.
    assert "--key" in r.stderr


def test_verify_with_the_KEY_passed_correctly_still_passes(tmp_path):
    """The companion to the above: the same group, selected the RIGHT way, is a genuine OK. Without
    this, the fix above could be satisfied by making every selector fail."""
    db = tmp_path / "runs.sqlite"
    assert _add(db, "ctrl", "--colocate", "camp:seed1", seed=1).returncode == 0
    assert _add(db, "arm", "--colocate", "camp:seed1", seed=2).returncode == 0
    _set_instance(db, "ctrl", 42)
    _set_instance(db, "arm", 42)
    r = _runq(db, "colocate", "--key", "camp:seed1", "--verify")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "OK" in r.stdout


def test_an_empty_selector_is_only_an_error_UNDER_verify(tmp_path):
    """Listing nothing is a legitimate answer to a LISTING. Only `--verify` promises an audit, so
    only `--verify` may fail on an empty match — otherwise every `runq colocate` on an idle queue
    starts exiting non-zero."""
    db = tmp_path / "runs.sqlite"
    assert _add(db, "solo", seed=1).returncode == 0
    r = _runq(db, "colocate")
    assert r.returncode == 0, (r.stdout, r.stderr)
