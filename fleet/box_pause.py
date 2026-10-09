"""Pause / drain an owned box (the home laptop) without wedging the fleet.

Spec: docs/specs/box-pause.spec.md. Two modes matching two real situations, plus resume + status:

    python fleet/box_pause.py pause     # "back soon" — freeze in place, requeue if forgotten
    python fleet/box_pause.py hold      # "finish up" — no NEW work; running tasks untouched
    python fleet/box_pause.py drain     # "long wait" — checkpoint-stop + hold until resume
    python fleet/box_pause.py resume     # back to normal (live, unfrozen)
    python fleet/box_pause.py status     # what's the box doing right now

`pause` (soft): SIGSTOP the box's trainers (GPU/CPU instantly idle, VRAM retained, zero lost work,
resumes on SIGCONT) and stop packing new work onto it. The work is NOT requeued — unless you forget:
after `soft_pause_timeout_min` (default 30) the dispatcher auto-escalates to a hard drain so the jobs
move elsewhere instead of sitting frozen forever.

`drain` (hard): each task checkpoints then exits (≤~5 min) and requeues on the fleet; the box then
holds — no new work until `resume`.

All durable state is the shared registry DB (`instances.state='paused'` + a per-box `pause_i<ID>`
settings row); the immediate freeze is a `~/spool/FREEZE` marker ssh'd to the box (the worker acts on
it within ~2s). The dispatcher enforces admission-blocking, the 30-min watchdog, and drain eviction on
its poll loop — so a pause takes full effect only once the box-pause-aware daemon is running
(merge → `make dispatch-restart` → then pause). The freeze itself does not need the dispatcher.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_client  # noqa: E402
import registry_db  # noqa: E402
import dispatcher  # noqa: E402  (ssh_run + the shared experiments root; import is side-effect-free)


def _default_db() -> str:
    return str(registry_db.shared_experiments_root() / "runs.sqlite")


def _resolve_box(conn, label: str | None) -> dict:
    """The target owned box: the sole `source='owned'` row, or the one matching --label. Refuses to
    guess when there are 0 or >1 owned boxes and no --label was given (trust boundary)."""
    if label:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM instances WHERE source='owned' AND label=?", (label,))]
        if not rows:
            raise SystemExit(f"box_pause: no owned box with label {label!r}")
        return rows[0]
    rows = [dict(r) for r in conn.execute("SELECT * FROM instances WHERE source='owned'")]
    if not rows:
        raise SystemExit("box_pause: no owned boxes registered (register_owned_box.py first)")
    if len(rows) > 1:
        labels = ", ".join(sorted(r["label"] for r in rows))
        raise SystemExit(f"box_pause: multiple owned boxes ({labels}); pass --label")
    return rows[0]


def _ssh_marker(inst: dict, cmd: str) -> bool:
    """Best-effort marker op on the box (touch/rm ~/spool/FREEZE). Owned boxes are always reached
    on their direct LAN endpoint. Returns True on success; a failure (box asleep/off-LAN) is only a
    warning — the durable DB state is what the dispatcher acts on, and an off-LAN box is already quiet."""
    host, port = inst.get("ssh_host"), inst.get("ssh_port")
    if not host or not port:
        return False
    res = dispatcher.ssh_run(host, port, cmd)
    return res.returncode == 0


def _pause_meta(conn, iid: int):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (f"pause_i{iid}",)).fetchone()
    return json.loads(row["value"]) if row else None


def _set_pause_meta(conn, iid: int, meta: dict) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"pause_i{iid}", json.dumps(meta)))
    conn.commit()


def _clear_pause_meta(conn, iid: int) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (f"pause_i{iid}",))
    conn.commit()


def _occupants(conn, iid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, state FROM tasks WHERE instance_id=? AND state IN "
        "('claimed','shipped','running','preempting','cancelling') ORDER BY name", (iid,))]


def _elapsed_str(at_iso: str | None) -> str:
    try:
        at = datetime.strptime(at_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "?"
    secs = int((datetime.now(timezone.utc) - at).total_seconds())
    return f"{secs // 60}m{secs % 60:02d}s"


def _print_status(conn, inst: dict) -> None:
    iid = inst["id"]
    # Re-read state fresh: a mutating command (pause/drain/resume) prints status right after its
    # UPDATE, and the `inst` dict was resolved before that write.
    row = conn.execute("SELECT state, slots_total FROM instances WHERE id=?", (iid,)).fetchone()
    state = row["state"] if row else inst["state"]
    slots = row["slots_total"] if row else inst["slots_total"]
    meta = _pause_meta(conn, iid)
    occ = _occupants(conn, iid)
    print(f"box {inst['label']} (id={iid})  state={state}  slots={slots}")
    if meta:
        line = f"  pause: mode={meta.get('mode')}  since={meta.get('at')} ({_elapsed_str(meta.get('at'))})"
        if meta.get("escalated_at"):
            line += f"  escalated_at={meta['escalated_at']}"
        print(line)
    else:
        print("  pause: none")
    if occ:
        print(f"  occupants ({len(occ)}):")
        for t in occ:
            print(f"    {t['state']:<11} {t['name']}  ({t['id']})")
    else:
        print("  occupants: none")


def cmd_pause(conn, inst: dict) -> int:
    iid = inst["id"]
    conn.execute("UPDATE instances SET state='paused' WHERE id=?", (iid,))
    conn.commit()
    _set_pause_meta(conn, iid, {"mode": "soft", "at": registry_db.now_iso()})
    registry_db.log_event(conn, "pause", "soft pause: freeze in place, requeue only if forgotten",
                          instance_id=iid)
    conn.commit()
    froze = _ssh_marker(inst, "touch ~/spool/FREEZE")
    print(f"PAUSED (soft) {inst['label']}: trainers "
          + ("freezing (SIGSTOP within ~2s)" if froze else "NOT reached over ssh — box may be off/asleep"))
    print("  no new work will be packed; frozen work is NOT requeued unless you forget "
          f"(>{dispatcher.DEFAULT_SETTINGS['soft_pause_timeout_min']}min -> auto-drain elsewhere).")
    print("  `make resume` when you're back.")
    _print_status(conn, inst)
    return 0


def cmd_drain(conn, inst: dict) -> int:
    iid = inst["id"]
    conn.execute("UPDATE instances SET state='paused' WHERE id=?", (iid,))
    conn.commit()
    _set_pause_meta(conn, iid, {"mode": "hard", "at": registry_db.now_iso()})
    registry_db.log_event(conn, "drain", "hard drain: checkpoint-stop + requeue elsewhere, then hold",
                          instance_id=iid)
    conn.commit()
    _ssh_marker(inst, "rm -f ~/spool/FREEZE")  # a drain runs to checkpoint; never frozen
    print(f"DRAINING (hard) {inst['label']}: each task runs to its NEXT checkpoint, then exits and "
          "requeues on the fleet.")
    print("  (graceful — never a hard kill; so a task is only as fast to drain as its checkpoint")
    print("   interval. Tasks that checkpoint infrequently take that long to leave.)")
    print("  the box will hold — no new work — until `make resume`.")
    print("  (eviction is driven by the dispatcher's next poll; needs the coordinator running.)")
    _print_status(conn, inst)
    return 0


def cmd_hold(conn, inst: dict) -> int:
    """QUIESCE: stop packing new work, and DO NOT TOUCH what is already running.

    The third real situation, and the one `pause`/`drain` between them could not express (owner,
    2026-08-08: "can you drain the desktop without interrupting current work — just don't add new
    work"). `pause` SIGSTOPs the trainers and auto-escalates to a drain after
    `soft_pause_timeout_min`; `drain` checkpoints them out and requeues them elsewhere. Both are
    right when you want the machine BACK. Neither is right when you want the box to finish what it
    has and then be idle — before recreating its container, updating a driver, or handing the desktop
    back for the evening.

    It needs no new enforcement, only a mode the existing machinery already declines to act on:
      * `place()` admits only `live` instances, so `state='paused'` blocks packing for free;
      * `_signal_drain` evicts a paused owned box ONLY when its mode is 'hard' — it `continue`s past
        anything else, so nothing is preempted;
      * `_reap_paused_soft_timeout` escalates ONLY mode 'soft', so this never becomes a drain;
      * the orphan / stall / dead-worker reapers already treat `paused` as live-ish, so the running
        occupants are not mistaken for strays while they finish;
      * HEARTBEAT and result pulls already cover `paused` boxes, so those occupants still complete,
        report, and land their artifacts normally.
    And NO FREEZE marker is written, which is the whole difference from `pause`.

    So the box drains ITSELF, at its own pace, losing nothing — and `resume` puts it back."""
    iid = inst["id"]
    conn.execute("UPDATE instances SET state='paused' WHERE id=?", (iid,))
    conn.commit()
    _set_pause_meta(conn, iid, {"mode": "hold", "at": registry_db.now_iso()})
    registry_db.log_event(conn, "hold", "hold: no new work; running tasks finish untouched",
                          instance_id=iid)
    conn.commit()
    _ssh_marker(inst, "rm -f ~/spool/FREEZE")   # a hold never freezes; clear a stale marker
    print(f"HOLDING {inst['label']}: no NEW work will be packed onto it.")
    print("  running tasks are UNTOUCHED — not frozen, not requeued; they finish normally and the "
          "box empties on its own.")
    print("  nothing auto-escalates: this is not a soft pause, so it will never become a drain.")
    print("  `make resume` to let it take work again.")
    _print_status(conn, inst)
    return 0


def cmd_resume(conn, inst: dict) -> int:
    iid = inst["id"]
    conn.execute("UPDATE instances SET state='live' WHERE id=?", (iid,))
    conn.commit()
    _clear_pause_meta(conn, iid)
    registry_db.log_event(conn, "resume", "resume: back to live, unfrozen", instance_id=iid)
    conn.commit()
    _ssh_marker(inst, "rm -f ~/spool/FREEZE")
    print(f"RESUMED {inst['label']}: live again; any frozen trainers SIGCONT on the worker's next "
          "poll; the dispatcher will pack it again.")
    _print_status(conn, inst)
    return 0


def cmd_status(conn, inst: dict) -> int:
    _print_status(conn, inst)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pause", "hold", "drain", "resume", "status"])
    ap.add_argument("--label", default=None, help="owned box label (default: the sole owned box)")
    ap.add_argument("--db", default=None, help="registry DB path (default: the shared experiments DB)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd != "status" and api_client.enabled():
        # The API writes a one-shot row; the DISPATCHER performs the verb, because pausing needs ssh
        # and `coord-api` has no network (remote-submit inv. 3/20). `status` is a READ and keeps
        # using the registry directly — reads are unchanged by this feature (inv. 19).
        label = a.label or _resolve_box(registry_db.connect(a.db or _default_db()), None)["label"]
        return api_client.run(api_client.ApiClient().box, label, a.cmd)
    conn = registry_db.connect(a.db or _default_db())
    inst = _resolve_box(conn, a.label)
    return {"pause": cmd_pause, "hold": cmd_hold, "drain": cmd_drain,
            "resume": cmd_resume, "status": cmd_status}[a.cmd](conn, inst)


if __name__ == "__main__":
    sys.exit(main())
