"""experiments/ retention — TTL + per-category FIFO budget (docs/specs/experiments-retention.spec.md).

One test per spec fixture. Each builds a synthetic experiments root in tmp_path with a REAL
registry schema (via registry_db.connect) so the dir->state resolution under test is the one
production uses, then asserts against the spec's numbered invariants.
"""

import importlib.util
import json
import os
import sys
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


registry_db = _load("registry_db", "fleet/registry_db.py")
pr = _load("prune_experiments", "fleet/prune_experiments.py")

DAY = 86400.0


# --------------------------------------------------------------------------- harness


class Root:
    """A synthetic experiments root: files with controlled mtimes + matching task rows."""

    def __init__(self, path: Path):
        self.path = path
        self.conn = registry_db.connect(str(path / "runs.sqlite"))
        self._n = 0

    def task(self, grp, name, state, result_path=None):
        self._n += 1
        self.conn.execute(
            "INSERT INTO tasks(id, created_at, created_by, grp, name, entrypoint, args_json,"
            " config_json, config_hash, arm_hash, git_sha, slots, est_minutes, priority, state,"
            " retries_used, max_retries, result_path, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{self._n}", registry_db.now_iso(), "test", grp, name, "ep", "[]", "{}",
             f"c{self._n}", f"a{self._n}", "deadbeef", 1, 10, 0, state, 0, 2,
             result_path, registry_db.now_iso()))
        self.conn.commit()

    def file(self, rel, age_days=30.0, size=1024):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        t = time.time() - age_days * DAY
        os.utime(p, (t, t))
        return p

    def run(self, *argv):
        """Invoke the CLI against this root; returns (exit_code, report_dict)."""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pr.main(["--root", str(self.path), "--json", *argv])
        return rc, (json.loads(buf.getvalue()) if buf.getvalue().strip() else {})


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "experiments"
    path.mkdir(parents=True, exist_ok=True)
    r = Root(path)
    yield r
    r.conn.close()


def _exists(p):
    return Path(p).exists()


# --------------------------------------------------------------------------- fixtures


def test_record_never_pruned(root):
    """inv. 2 — the record is 2% of the bytes and the only irreplaceable part."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    j = root.file("g/a/results.json", age_days=400)
    tb = root.file("g/a/events.out.tfevents.1234", age_days=400)
    rc, rep = root.run("--apply", "--cat-budget-gb", "live_weights=0")
    assert rc == 0
    assert _exists(j) and _exists(tb)
    assert rep["categories"]["record"]["ttl_reclaim_gb"] == 0.0
    assert rep["categories"]["record"]["budget_reclaim_gb"] == 0.0


def test_prev_ttl(root):
    """inv. 3 — .prev is ~12 min of training behind the head; 2-day clock."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    old = root.file("g/a/ckpt_latest.pt.prev", age_days=3)
    root.task("g", "b", "done", str(root.path / "g" / "b"))
    young = root.file("g/b/ckpt_latest.pt.prev", age_days=1)
    rc, _ = root.run("--apply")
    assert rc == 0
    assert not _exists(old)
    assert _exists(young)


def test_dead_vs_live_ttl(root):
    """Categories — identical ckpts, different owning state, different clock."""
    root.task("g", "dead", "task_failed")
    root.task("g", "alive", "done", str(root.path / "g" / "alive"))
    dead = root.file("g/dead/ckpt_substrate_seed0.pt", age_days=30)
    alive = root.file("g/alive/ckpt_substrate_seed0.pt", age_days=30)
    rc, _ = root.run("--apply")
    assert rc == 0
    assert not _exists(dead), "30d > 7d dead_weights TTL"
    assert _exists(alive), "30d < 90d live_weights TTL"


def test_prev_beats_dead(root):
    """Classification order — a .prev in a cancelled dir takes the SHORTER clock."""
    root.task("g", "c", "cancelled")
    assert pr.classify("ckpt_latest.pt.prev", "cancelled") == "prev"
    assert pr.classify("ckpt_substrate_seed0.pt", "cancelled") == "dead_weights"
    p = root.file("g/c/ckpt_latest.pt.prev", age_days=3)
    s = root.file("g/c/ckpt_substrate_seed0.pt", age_days=3)
    rc, _ = root.run("--apply")
    assert rc == 0
    assert not _exists(p), "3d > 2d prev TTL"
    assert _exists(s), "3d < 7d dead_weights TTL"


@pytest.mark.parametrize("state", sorted(pr.OPEN_STATES))
def test_running_untouchable(root, state):
    """inv. 5 — an open task's dir is off limits at any age, under any budget pressure."""
    root.task("g", "a", state)
    c = root.file("g/a/ckpt_latest.pt.prev", age_days=400)
    rc, rep = root.run("--apply", "--cat-budget-gb", "prev=0", "--ttl-days", "prev=0")
    assert rc == 0
    assert _exists(c)
    assert rep["protected_dirs"] == 1


def test_keep_marker(root):
    """inv. 6 — KEEP at the group level protects an arm dir beneath it."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    root.task("h", "a", "done", str(root.path / "h" / "a"))
    root.file("g/KEEP", age_days=1, size=0)
    kept = root.file("g/a/ckpt_latest.pt.prev", age_days=400)
    doomed = root.file("h/a/ckpt_latest.pt.prev", age_days=400)
    rc, _ = root.run("--apply")
    assert rc == 0
    assert _exists(kept), "KEEP protects the whole subtree"
    assert not _exists(doomed)
    assert _exists(root.path / "g" / "KEEP"), "KEEP is a record file — never deleted"


def test_budget_fifo(root):
    """inv. 3 pass 2 — oldest mtime first, until under budget."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    files = {age: root.file(f"g/a/ckpt_substrate_seed{i}.pt", age_days=age, size=10_000)
             for i, age in enumerate((10.0, 20.0, 30.0))}
    # TTL off; budget admits ~2 of the 3 files.
    rc, _ = root.run("--apply", "--ttl-days", "live_weights=99999",
                     "--cat-budget-gb", f"live_weights={25_000 / pr.GB}")
    assert rc == 0
    assert not _exists(files[30.0]), "oldest evicted first"
    assert _exists(files[20.0]) and _exists(files[10.0])


def test_min_age_floor(root):
    """inv. 11 — the floor that makes this safe to run against a live tree."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    fresh = root.file("g/a/ckpt_latest.pt.prev", age_days=1 / 24.0)  # 1 hour
    rc, _ = root.run("--apply", "--ttl-days", "prev=0", "--cat-budget-gb", "prev=0")
    assert rc == 0
    assert _exists(fresh)


def test_dry_run_is_inert(root):
    """inv. 1 — the dry-run plan is exactly what --apply then removes."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    c = root.file("g/a/ckpt_latest.pt.prev", age_days=10, size=4096)
    rc, dry = root.run()
    assert rc == 0
    assert _exists(c), "dry run must not delete"
    assert dry["applied"] is False
    assert dry["reclaim_bytes"] == 4096
    rc, wet = root.run("--apply")
    assert rc == 0
    assert not _exists(c)
    assert wet["applied"] is True
    assert wet["reclaim_bytes"] == dry["reclaim_bytes"]


def test_symlink_not_followed(root, tmp_path):
    """inv. 7 — experiments/ holds symlinks into worktrees; following one prunes another checkout."""
    outside = tmp_path / "other_checkout" / "arm"
    outside.mkdir(parents=True)
    victim = outside / "ckpt_latest.pt.prev"
    victim.write_bytes(b"x" * 4096)
    t = time.time() - 400 * DAY
    os.utime(victim, (t, t))
    (root.path / "linked").symlink_to(outside.parent)
    rc, rep = root.run("--apply")
    assert rc == 0
    assert _exists(victim), "must not delete through a symlink"
    assert rep["categories"]["prev"]["files"] == 0, "must not even scan through it"


def test_dispatcher_excluded(root):
    """Exclusions — snapshots/blobs have their own GC (dispatcher 72h, spool LRU)."""
    snap = root.file(".dispatcher/snapshots/abc.tar.gz", age_days=400)
    root.file(".dispatcher/blobs/x.tar.gz", age_days=400)
    rc, rep = root.run("--apply")
    assert rc == 0
    assert _exists(snap)
    assert rep["total_gb"] == 0.0, ".dispatcher is not scanned at all"


def test_audit_log_written(root):
    """inv. 9 — a deletion is auditable, with the reason that caused it."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    root.file("g/a/ckpt_latest.pt.prev", age_days=10, size=2048)
    root.task("g", "b", "done", str(root.path / "g" / "b"))
    root.file("g/b/ckpt_substrate_seed0.pt", age_days=10, size=10_000)
    rc, _ = root.run("--apply", "--ttl-days", "live_weights=99999",
                     "--cat-budget-gb", "live_weights=0")
    assert rc == 0
    lines = [json.loads(x) for x in
             (root.path / ".retention" / "prune.log").read_text().splitlines()]
    assert len(lines) == 2
    by_reason = {x["reason"]: x for x in lines}
    assert by_reason["ttl"]["category"] == "prev"
    assert by_reason["budget"]["category"] == "live_weights"
    for x in lines:
        assert x["bytes"] > 0 and x["age_days"] > 0 and not os.path.isabs(x["path"])


def test_empty_dirs_removed(root):
    """inv. 10 — a dir keeping any record file survives."""
    root.task("g", "bare", "task_failed")
    root.task("g", "kept", "task_failed")
    root.file("g/bare/ckpt_substrate_seed0.pt", age_days=30)
    root.file("g/kept/ckpt_substrate_seed0.pt", age_days=30)
    root.file("g/kept/results.json", age_days=30)
    rc, _ = root.run("--apply")
    assert rc == 0
    assert not (root.path / "g" / "bare").exists()
    assert (root.path / "g" / "kept" / "results.json").exists()


@pytest.mark.parametrize("argv,why", [
    (["--cat-budget-gb", "nonsense=5"], "unknown category"),
    (["--cat-budget-gb", "prev=500"], "over the total budget"),
    (["--ttl-days", "prev=-1"], "negative ttl"),
    (["--ttl-days", "prev"], "malformed kv"),
    (["--min-age-hours", "-1"], "negative floor"),
])
def test_bad_args_rejected(root, argv, why):
    """inv. 13 — validate at the boundary, delete nothing on a bad invocation."""
    root.task("g", "a", "done", str(root.path / "g" / "a"))
    c = root.file("g/a/ckpt_latest.pt.prev", age_days=400)
    with pytest.raises(SystemExit) as e:
        rc = pr.main(["--root", str(root.path), "--apply", *argv])
        assert rc != 0, why
        raise SystemExit(rc)
    assert e.value.code != 0, why
    assert _exists(c), f"{why}: must not delete on a rejected invocation"


def test_missing_root_rejected(tmp_path):
    """inv. 13 — refuse a tree with no runs.sqlite rather than pruning something unrecognised."""
    assert pr.main(["--root", str(tmp_path / "nope")]) == 2
    (tmp_path / "empty").mkdir()
    assert pr.main(["--root", str(tmp_path / "empty")]) == 2


def test_unmatched_dir_is_live_weights(root):
    """inv. 8 — a dir no task row owns falls to the conservative 90-day category."""
    c = root.file("manual_run/arm/ckpt_substrate_seed0.pt", age_days=30)
    rc, rep = root.run("--apply")
    assert rc == 0
    assert _exists(c)
    assert rep["categories"]["live_weights"]["files"] == 1
