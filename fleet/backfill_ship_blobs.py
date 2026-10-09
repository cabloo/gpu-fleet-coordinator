"""Backfill ship-ready blobs for PRE-CUTOVER tasks (ship-artifact-build spec, Q3).

Tasks queued before schema v4 carry `code_blob IS NULL` and could only be shipped by the
coordinator's legacy build path. That path is what invariant 1 deletes — so before it can go, every
still-open pre-cutover task needs the artifact the queuer would have built.

This is that migration, and it is the reason Q3 needed no quiet window: the working-tree snapshot
`runq add` already persisted IS the build input, so a blob can be produced now, home-side, exactly
as `runq` would have produced it. Terminal tasks are ignored — nothing will ever ship them again.

Idempotent (a row that already has a blob is skipped) and safe to re-run. Only ever writes the three
`code_*` columns; never touches `state`, so it cannot race the dispatcher's state machine — a task
shipped mid-run simply uses whichever path exists at that moment.

    python fleet/backfill_ship_blobs.py [--db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_store  # noqa: E402
import bundle  # noqa: E402
import code_snapshot  # noqa: E402
import entrypoints  # noqa: E402
import registry_db  # noqa: E402

TERMINAL = ("done", "cancelled", "task_failed", "infra_failed")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(registry_db.shared_experiments_root() / "runs.sqlite"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    root = Path(a.db).parent
    conn = registry_db.connect(a.db)
    qs = ",".join("?" for _ in TERMINAL)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM tasks WHERE code_blob IS NULL AND state NOT IN ({qs})", TERMINAL)]
    if not rows:
        print("backfill: nothing to do — every open task already carries a ship blob")
        return 0
    print(f"backfill: {len(rows)} open pre-cutover task(s)")

    built = skipped = failed = 0
    for t in rows:
        tag = f"{t['grp']}/{t['name']}"
        snap = code_snapshot.load(root, t["id"])
        if snap is None:
            # No snapshot and no toolchain-independent source: the coordinator's `git archive`
            # fallback is the only thing that could ship this, and that is exactly what is being
            # deleted. Say so loudly rather than pretending it migrated.
            print(f"  SKIP {tag}: no persisted snapshot — must be re-queued", file=sys.stderr)
            skipped += 1
            continue
        code_tar, code_hash = snap
        entry_src = entrypoints.entry_source_path(entrypoints.resolve(t))
        if a.dry_run:
            print(f"  DRY  {tag} ({t['state']}) entry={entry_src}")
            continue
        try:
            out = artifact_store.build_and_store(
                conn, root, code_tar=code_tar, code_hash=code_hash,
                entry_source_path=entry_src, bundle_mod=bundle,
                on_reuse=lambda b, tag=tag: print(f"  reuse {tag} -> {b}"),
                on_build=lambda b, s, tag=tag: print(f"  BUILT {tag} -> {b} in {s:.0f}s"))
        except artifact_store.BuildFailed as e:
            print(f"  FAIL {tag}: {e}", file=sys.stderr)
            failed += 1
            continue
        conn.execute("UPDATE tasks SET code_blob=?, code_sha256=?, code_format=? WHERE id=?",
                     (out["code_blob"], out["code_sha256"], out["code_format"], t["id"]))
        conn.commit()
        built += 1
    print(f"backfill: {built} migrated, {skipped} skipped (no snapshot), {failed} failed")
    return 1 if (skipped or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
