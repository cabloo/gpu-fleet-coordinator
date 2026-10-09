"""Register a self-owned, always-on box (home laptop/desktop GPU) with the dispatcher's
registry, so it's packed onto BEFORE anything is ever rented on Vast — invariant 4a/4b's
pack-first placement order already prefers any `live` instance regardless of provenance
(docs/specs/task-dispatcher.spec.md), so this needs no scheduling changes, only a way to get
a `live` row into the registry that survives Vast-specific reconcile/teardown machinery.

Unlike a Vast rental, an owned box (`instances.source='owned'`) is never provisioned, never
reconciled against `vastai show instances`, and never destroyed on idle timeout or hard cap
(dispatcher.py's `reconcile`/`should_teardown`/`_destroy` all special-case it) — it costs
nothing while idle, so there's never a reason to tear it down. If it goes unreachable, its
occupant tasks still requeue normally via the existing heartbeat/orphan reapers, but the
instance row itself stays `live` so it's packed onto again the moment it's back, with no
re-registration needed.

    python fleet/register_owned_box.py --label laptop-gpu --host 192.168.0.14 --slots 1

Re-running with the same --label updates the existing row in place (idempotent) rather than
creating a duplicate — safe to re-run after changing --host/--port/--slots.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatcher  # noqa: E402
import api_client
import registry_db  # noqa: E402


PAUSED_NOTE = ("box was PAUSED and stays paused (re-registering never resumes a box) — "
               "`make resume BOX={label}` when it should take work again")


def register(db_path: str, label: str, host: str, port: int | None, slots_total: int | None,
              gpu_name: str | None, bootstrap: bool) -> int:
    conn = registry_db.connect(db_path)
    # Inv. 20e-1: the upsert itself is shared with the API route — `None` keeps the row's value, and
    # a paused box stays paused.
    box = registry_db.upsert_owned_box(conn, label, host, port=port, slots=slots_total,
                                       gpu_name=gpu_name)
    instance_id, port, slots_total = box["id"], box["port"], box["slots"]
    registry_db.log_event(conn, "register_owned",
                          f"{label} at {host}:{port} ({slots_total} slots)"
                          + (" — kept paused" if box["kept_paused"] else ""),
                          instance_id=instance_id)
    conn.commit()
    print(f"registered instance id={instance_id} label={label} host={host}:{port} "
          f"slots={slots_total}")
    if box["kept_paused"]:
        print(PAUSED_NOTE.format(label=label))

    if bootstrap:
        d = dispatcher.Dispatcher(db_path)
        inst = {"id": instance_id, "ssh_host": host, "ssh_port": port, "slots_total": slots_total}
        # `_bring_up_worker` is ONE attempt (invariant 5b: production retries it across polls via
        # `_advance_provisioning`) — this script has no poll loop of its own to retry across, so
        # it does its own bounded retry here instead, exactly like the old blocking `_provision`
        # did before the async-provisioning split (invariant 20e).
        ok = False
        for attempt in range(max(1, d.settings["ssh_probe_attempts"])):
            if d._bring_up_worker(inst, dispatcher.MACHINES_DENY_RUNTIME):
                ok = True
                break
            if attempt < d.settings["ssh_probe_attempts"] - 1:
                time.sleep(d.settings["ssh_probe_interval_s"])
        if ok:
            print("spool_worker.py deployed and started")
        else:
            print("WARNING: worker bootstrap failed (ssh unreachable?) — box is registered but "
                  "not yet running a worker; fix connectivity and re-run to retry bootstrap",
                  file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=dispatcher.DEFAULT_DB)
    p.add_argument("--label", required=True, help="stable name, e.g. laptop-gpu")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=None,
                    help="ssh port (a NEW box defaults to 22; omit on a re-registration to keep "
                         "the box's current port)")
    p.add_argument("--slots", type=int, default=None,
                    help="concurrent task lanes this box offers (a NEW box defaults to 1; omit on "
                         "a re-registration to keep the current count; raise once you've observed "
                         "real headroom — the overpack-cap reaper will self-correct downward if "
                         "it's ever too optimistic)")
    p.add_argument("--gpu-name", default=None,
                    help="omit on a re-registration to keep the box's current GPU name")
    p.add_argument("--prefer", type=int, default=None, metavar="N",
                    help="operator PACK PREFERENCE for this box (invariant 4b'): higher wins, 0 is "
                         "the inert default. Applies only among boxes of EQUAL marginal cost (the "
                         "all-free case) and is checked BEFORE tightest-fit, so a preferred box "
                         "fills first instead of last. Use it to make your fastest always-on box "
                         "the fleet's default worker; it can never cause a rental, because cost "
                         "still wins outright. Needs `make dispatch-restart` to take effect "
                         "(settings are read once at daemon start).")
    p.add_argument("--no-bootstrap", dest="bootstrap", action="store_false",
                    help="register the DB row only; don't push/start spool_worker.py yet")
    a = p.parse_args(argv)
    if api_client.enabled():
        # The API writes the row; the DISPATCHER brings the worker up on its next refresh (inv. 20i),
        # which is also why `--no-bootstrap` is the only sensible behaviour here — a client with no
        # fleet keys cannot rsync a worker anywhere.
        client, reply = api_client.ApiClient(), {}
        rc = api_client.run(lambda **kw: reply.update(client.register_box(**kw) or {}),
                            label=a.label, host=a.host, port=a.port, slots=a.slots,
                            gpu_name=a.gpu_name, prefer=a.prefer)
        if rc == 0 and reply.get("kept_paused"):
            print(PAUSED_NOTE.format(label=a.label))
        return rc
    rc = register(a.db, a.label, a.host, a.port, a.slots, a.gpu_name, a.bootstrap)
    if a.prefer is not None:
        conn = registry_db.connect(a.db)
        row = conn.execute("SELECT id FROM instances WHERE label=? AND source='owned'",
                            (a.label,)).fetchone()
        if row is None:
            print(f"--prefer: no owned box labelled {a.label!r}", file=sys.stderr)
            return 1
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (f"box_preference_i{row['id']}", json.dumps(a.prefer)))
        registry_db.log_event(conn, "box_preference",
                              f"{a.label} (instance {row['id']}) pack preference set to {a.prefer}",
                              instance_id=row["id"])
        conn.commit()
        print(f"pack preference for {a.label} = {a.prefer} "
              f"(restart the dispatcher for it to take effect)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
