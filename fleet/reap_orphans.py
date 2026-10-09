"""Reap orphaned / zombie trainer processes on a worker box (fleet hygiene).

Two failure modes this cleans up, both observed live 2026-07-22 on `laptop-gpu`:

- **Runaway orphan** — a trainer this box launched (cwd under `<spool>/active/<id>/repo`) whose task
  is gone: the `active/<id>/` dir was deleted (a requeue/teardown `rm -rf`'d it) or it carries a
  TERMINAL marker (`DONE`/`PREEMPTED`/`CANCELLED`/`FAILED_*`). No live task owns it, yet it keeps
  burning CPU/VRAM. Seen: a `cancelled` task's trainer ran 2h+ at 93% CPU after its dir was removed.
- **Zombie (defunct)** — a dead child the `spool_worker` never `waitpid`'d because it stopped
  tracking the task (e.g. it was requeued out of `active[]`). Only the PARENT can reap it, so this is
  handled by `Worker.reap_orphans` in `spool_worker.py`; the standalone path can only kill runaways.

Standalone (cron-friendly, worker-independent — for when the worker itself is dead):
    python3 reap_orphans.py --spool ~/spool [--dry-run] [--grace 10]
    # cron: */5 * * * * python3 ~/spool_bin/reap_orphans.py --spool ~/spool >> ~/reap_orphans.log 2>&1

SAFETY: only ever touches a process whose cwd resolves under `<spool>/active/` **and that shares this
process's MOUNT NAMESPACE**. Kills the orphan's process SUBTREE by pid, never by process group (a
pre-`start_new_session` trainer shares the worker's group, so a group-kill would take down the worker
and its live siblings).

⛔⛔ **THE NAMESPACE GUARD IS NOT DEFENSIVE PROGRAMMING — IT IS THE FIX FOR A MEASURED MASSACRE.**
A cwd is a string *relative to the reader's mount namespace*, so `/root/spool/active/<id>/repo` names
a DIFFERENT directory inside a container than it does on the host — while `/proc/<pid>/cwd` hands the
host the container's spelling verbatim. On `laptop-gpu` (2026-09-12) a stale host-side worker sat
outside Docker with its own empty `/root/spool/active/`, saw the CONTAINER worker's trainers through
`/proc`, matched the path by string, asked "does `<my spool>/active/<id>` exist?", correctly answered
no — and killed them. `_task_is_gone` was right about its own filesystem and wrong about whose
process it was.

Cost: **every task ever queued to that box died**, 10-30s after launch, for two months. Five pclm
cells plus round 1's ten benchmark attempts, each recorded as
`ZERO PROGRESS (likely code/config bug, not infra)` — a label that sent every investigation looking
at the trainer. The host worker logged `orphan_reap ... (active dir deleted) -> kill subtree` every
single time, in a journal nobody read because nobody knew there was a second worker.

So: **verify the namespace before believing the path**, and FAIL CLOSED. A process whose namespace
cannot be read is not killed — a lingering runaway costs CPU, and a wrong kill costs somebody else's
run."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

TERMINAL_MARKERS = ("DONE", "PREEMPTED", "CANCELLED")


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (OSError, PermissionError):
        return None


def _all_pids() -> list[int]:
    return [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]


def _mount_namespace(pid: int | str) -> str | None:
    """`mnt:[4026532225]`, or None when it cannot be read (no `/proc/<pid>/ns`, or not permitted)."""
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt")
    except (OSError, PermissionError, ValueError):
        return None


def shares_our_mount_namespace(pid: int, ours: str | None) -> bool:
    """Is `pid`'s filesystem view the same one this process's paths are written in?

    ⛔ FAIL CLOSED, both ways. Callers use this to authorise a KILL, so "I could not tell" must mean
    "do not touch it": an unreadable namespace is exactly what a process in another container looks
    like from here. `ours` being unreadable is the same problem one level up — we cannot compare
    against a reference we do not have — and the answer is the same.

    See the module docstring for what this cost when it was absent.
    """
    if ours is None:
        return False
    theirs = _mount_namespace(pid)
    return theirs is not None and theirs == ours


def _active_id_from_cwd(cwd: str, active_root: str) -> str | None:
    """Return the `<id>` if `cwd` is `<active_root>/<id>/repo` (with or without a trailing
    " (deleted)" that readlink appends for an unlinked dir), else None."""
    c = cwd.replace(" (deleted)", "")
    root = active_root.rstrip("/") + "/"
    if not c.startswith(root):
        return None
    rest = c[len(root):]
    return rest.split("/", 1)[0] or None


def _task_is_gone(active_dir: Path, tid: str) -> str | None:
    """Reason the task owning `tid` is no longer live (dir deleted, or a terminal marker present),
    or None if it still looks live. This is the ONLY thing that authorizes a kill."""
    d = active_dir / tid
    if not d.exists():
        return "active dir deleted"
    for m in TERMINAL_MARKERS:
        if (d / m).exists():
            return f"{m} marker present"
    if list(d.glob("FAILED_*")):
        return "FAILED_* marker present"
    return None


def find_orphans(spool: Path, live_pids: frozenset[int] = frozenset()) -> list[dict]:
    """Runaway orphans on this box: a process whose cwd is under `<spool>/active/<id>/repo`, whose
    owning task is gone (dir deleted or terminal), and whose pid is not currently tracked as live."""
    active_root = str((spool / "active").resolve())
    active_dir = spool / "active"
    me = os.getpid()
    ours = _mount_namespace("self")
    out = []
    for pid in _all_pids():
        if pid == me or pid in live_pids:
            continue
        cwd = _proc_cwd(pid)
        if not cwd:
            continue
        tid = _active_id_from_cwd(cwd, active_root)
        if tid is None:
            continue
        # ⛔ BEFORE believing the path, check whose filesystem it is a path IN. A cwd read out of
        # `/proc` is spelled in the TARGET's mount namespace, so a container's
        # `<spool>/active/<id>/repo` matches ours by string while naming a directory we cannot see —
        # and "cannot see" is precisely what `_task_is_gone` reports as grounds to kill.
        if not shares_our_mount_namespace(pid, ours):
            continue
        reason = _task_is_gone(active_dir, tid)
        if reason is None:
            continue  # task still live — never kill
        out.append({"pid": pid, "task_id": tid, "reason": reason, "cwd": cwd})
    return out


def find_live_task_procs(spool: Path) -> list[dict]:
    """Every process whose cwd is under `<spool>/active/<id>/repo`, REGARDLESS of task state.

    Sibling of `find_orphans` and deliberately NOT a kill list — it answers a different question:
    "is a trainer for this task dir still running?". `spool_worker._reattach_after_restart` needs it
    because a trainer survives the worker's `os.execv` (`start_new_session`) while the worker's pid
    table does not, so without this a restarted worker launches a SECOND trainer onto one `out/`
    (invariant 20j / R9).

    Same `/proc` cwd mapping as `find_orphans`, factored here rather than duplicated so the two can
    never disagree about which pid belongs to which task."""
    active_root = str((spool / "active").resolve())
    me = os.getpid()
    ours = _mount_namespace("self")
    out = []
    for pid in _all_pids():
        if pid == me:
            continue
        cwd = _proc_cwd(pid)
        if not cwd:
            continue
        tid = _active_id_from_cwd(cwd, active_root)
        # Same namespace guard, for a different reason: this list decides RE-ADOPTION, and adopting
        # a process from another container would have the worker report a task as running that it
        # neither launched nor can wait on — and, on the other branch, relaunch a task that is
        # already running.
        if tid is not None and shares_our_mount_namespace(pid, ours):
            out.append({"pid": pid, "task_id": tid, "cwd": cwd})
    return out


def _descendants(pid: int) -> list[int]:
    """pid + all descendant pids, from /proc PPID links (so we kill env-worker children too, by pid
    — never via killpg, which could hit the worker's shared group)."""
    children: dict[int, list[int]] = {}
    for p in _all_pids():
        try:
            ppid = int(next(l.split()[1] for l in Path(f"/proc/{p}/status").read_text().splitlines()
                            if l.startswith("PPid:")))
        except (OSError, StopIteration, ValueError):
            continue
        children.setdefault(ppid, []).append(p)
    out, stack = [], [pid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_subtree(pid: int, grace: float = 10.0) -> None:
    tree = _descendants(pid)
    for p in tree:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(_alive(p) for p in tree):
        time.sleep(0.5)
    for p in tree:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def reap(spool: Path, live_pids: frozenset[int] = frozenset(), dry_run: bool = False,
         grace: float = 10.0, log=print) -> list[dict]:
    orphans = find_orphans(spool, live_pids)
    for o in orphans:
        log(f"orphan pid={o['pid']} task={o['task_id'][:8]} ({o['reason']}) -> "
            + ("would kill" if dry_run else "kill subtree"))
        if not dry_run:
            kill_subtree(o["pid"], grace)
    return orphans


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spool", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--grace", type=float, default=10.0)
    a = ap.parse_args(argv)
    reaped = reap(Path(a.spool).expanduser(), dry_run=a.dry_run, grace=a.grace)
    print(f"reap_orphans: {len(reaped)} orphan(s) {'found (dry-run)' if a.dry_run else 'reaped'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
