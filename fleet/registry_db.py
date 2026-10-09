"""Run registry: SQLite schema v1 + CAS state machine (docs/specs/run-registry.spec.md).

Pure DB layer — no subprocess, no `vastai`, no entrypoint handshake (that's `runq.py`'s job at
the CLI boundary). Every consumer (runq, dispatcher, dashboard) talks to this schema directly;
this module is a convenience wrapper, not a hidden cross-feature API.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 5


def shared_experiments_root() -> Path:
    """The ONE `experiments/` dir every worktree's `runq`/`dispatcher` converges on by default
    (registry DB, dispatcher lock, ship-staging) — resolved via `git rev-parse --git-common-dir`,
    which always points at the MAIN repo's shared `.git` regardless of which linked worktree
    invokes it (worktrees of one repo share a single object database; a commit made in any of
    them is immediately `git archive`-able from any other — the ONLY thing that doesn't
    automatically follow is `experiments/`, since it's gitignored, so each worktree otherwise
    gets its own empty queue no other worktree's dispatcher is watching). This means `runq add`
    run from a feature-branch worktree lands in the SAME queue a dispatcher started from the
    main checkout is polling, and the shipped payload (`git archive <task.git_sha>`) is that
    worktree's exact commit — testing on Vast has never required merging to master first, only
    committing. Pass `--db`/construct `Dispatcher(db_path=...)` explicitly to opt out (e.g. the
    docker integration test and unit tests always do)."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=here,
                              capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            common = Path(out.stdout.strip())
            if not common.is_absolute():
                common = (here / common).resolve()
            return common.parent / "experiments"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return here.parent / "experiments"  # not a git checkout at all — best-effort fallback

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  created_by    TEXT NOT NULL,
  grp           TEXT NOT NULL,
  name          TEXT NOT NULL,
  entrypoint    TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  config_json   TEXT NOT NULL,
  config_hash   TEXT NOT NULL,
  arm_hash      TEXT NOT NULL,
  git_sha       TEXT NOT NULL,
  slots         INTEGER NOT NULL DEFAULT 1,
  est_minutes   INTEGER NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 50,
  state         TEXT NOT NULL DEFAULT 'queued',
  retries_used  REAL NOT NULL DEFAULT 0,
  max_retries   REAL NOT NULL DEFAULT 10,
  instance_id   INTEGER,
  result_path   TEXT,
  resource_hint_json TEXT,
  resume_checkpoint  TEXT,
  job_manifest_json  TEXT,
  code_blob     TEXT,
  code_sha256   TEXT,
  code_format   TEXT,
  updated_at    TEXT NOT NULL,
  UNIQUE(grp, name)
);
CREATE TABLE IF NOT EXISTS instances (
  id            INTEGER PRIMARY KEY,
  machine_id    INTEGER,
  label         TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  state         TEXT NOT NULL,
  dph_usd       REAL NOT NULL,
  gpu_name      TEXT,
  ssh_host      TEXT,
  ssh_port      INTEGER,
  slots_total   INTEGER NOT NULL,
  hard_cap_at   TEXT NOT NULL,
  destroyed_at  TEXT,
  cost_usd      REAL,
  source        TEXT NOT NULL DEFAULT 'vast'
);
CREATE TABLE IF NOT EXISTS events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  t           TEXT NOT NULL,
  task_id     TEXT,
  instance_id INTEGER,
  event       TEXT NOT NULL,
  detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Sibling co-location (dispatcher invariant 4g). One row per colocation GROUP, written the first
-- time a member of that group is packed: it records WHICH box the coordinator chose, so every later
-- member is placed beside it. Deliberately its own table rather than a `tasks` column — the pin is a
-- property of the GROUP and outlives any individual member (an arm can be cancelled and re-queued
-- under a new name and must still land on the group's box), and a stale pin is dropped by the
-- dispatcher the moment its instance stops being live, which a per-task column could not express.
CREATE TABLE IF NOT EXISTS colocations (
  key         TEXT PRIMARY KEY,
  instance_id INTEGER NOT NULL,
  pinned_at   TEXT NOT NULL,
  pinned_by   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_instance ON tasks(instance_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_instance ON events(instance_id);
"""

# Registry spec invariant 4 + dispatcher spec invariants 10/17: the only legal (from, to) pairs.
LEGAL_TRANSITIONS = frozenset({
    ("queued", "claimed"),
    ("queued", "cancelled"),
    ("claimed", "shipped"),
    ("claimed", "queued"),
    ("claimed", "cancelled"),
    # Retrospective bug (2026-07-15): a task packed onto a box (`claimed`) whose instance is then
    # destroyed/lost is an infra failure just like a `shipped`/`running` one — but this pair was
    # missing, so `_infra_fail`'s first transition silently no-op'd (returns ok=False) and the
    # claimed task could never be requeued. That stranded it forever: `_ship_all` skips a non-live
    # box, the stall reaper only watches running/preempting, the dead-worker reaper needs a live
    # box. `_mark_lost` already called `_infra_fail` on claimed occupants (latently broken); the new
    # `_reap_orphaned_tasks` relies on it too. Observed live 2026-07-15: two tasks sat `claimed` on
    # boxes destroyed 8h earlier. `infra_failed`->`queued` (requeue, half a retry) is already legal.
    ("claimed", "infra_failed"),
    # Ship-time compile error (2026-07-24): the code tree fails to Cython-compile because a file the
    # compiler rejects (a code bug, not an env/toolchain limit — those still fall back to source). A
    # `claimed` task hitting this is failed fast + terminal here, before it ever reaches a box, since
    # retrying re-fails the same code (`task_failed` never auto-requeues) — the one-line code fix
    # requeues it. Mirrors `running → task_failed` but at ship time. Task-bundle spec invariant 6.
    ("claimed", "task_failed"),
    # ⛔ THE WORKER CAN START BEFORE WE FINISH SAYING WE SHIPPED (2026-08-04). `_ship` rsyncs the
    # bundle and only THEN records `claimed → shipped`; the box-side worker polls `incoming/`
    # independently and can claim + launch inside that gap. Measured on owned box -1:
    #
    #     05:02:13  worker  claim          05:02:16  worker  start   <- trainer already running
    #     05:02:26  dispatcher  ship                                 <- bookkeeping, 10s LATER
    #
    # `_pull_worker_state` then applied that `start` row against state `claimed`, this pair was
    # missing, `transition()` returned ok=False WITHOUT RAISING, and the row was never re-read — so
    # the task sat in `shipped` while its trainer ran to completion on the box. That is not cosmetic:
    # `("shipped","done")` is also absent, so `_complete_done`'s CAS would have silently no-opped too,
    # leaving an OPEN row holding a worker slot forever on the fleet's least-churning box.
    #
    # This is the THIRD time a missing pair out of `claimed` has silently stranded a task — see the
    # two retrospective additions directly above, both the same shape. The state machine must tolerate
    # a worker that is FASTER than our own bookkeeping; the alternative is ordering guarantees across
    # two hosts that we do not have.
    ("claimed", "running"),
    ("shipped", "running"),
    ("shipped", "infra_failed"),
    # Over-pack unschedule (2026-07-15, owner directive): `slots_for_offer` can advertise more slots
    # than a GPU actually sustains, so the box-side launch gate wedges the excess task in `shipped`
    # on a live, healthy box with no reaper covering it. `_reap_overpacked_boxes` requeues it — but
    # unlike an infra failure this is the SCHEDULER's misjudgment, not the task's fault, so it costs
    # NO retry (like preemption). `shipped → queued` with `retries_used` unchanged. Dispatcher inv. 19h.
    ("shipped", "queued"),
    # Missed-`start` wedge (2026-07-29, owner directive). `_pull_markers` explicitly queries tasks
    # in `shipped` and can drive ALL FOUR terminal outcomes for them — yet only `shipped -> queued`
    # (PREEMPTED) was legal, so a DONE/FAILED_*/CANCELLED marker on a `shipped` task silently
    # no-op'd (`transition()` returns ok=False and the caller does not check) and the task stuck in
    # `shipped` — an OPEN state — holding its slot forever. Exactly the shape of the 2026-07-14
    # `preempting` bug below, one state earlier.
    #
    # This matters because reaching `running` depends on OBSERVING a `start` row in the box's
    # `worker.jsonl`, which is a PULLED copy. Every reason that pull can lag — the serial ship path
    # starving `_ingest_and_complete`, a transient rsync failure, an on-box worker restart — turns a
    # missed OBSERVATION into permanently lost WORK. Observed live 2026-07-29 on owned box -1: the
    # local copy ran 52 min stale (464494B/03:45 vs 466356B/04:11); three tasks finished with DONE
    # markers and `exit rc=0` in the worker's own log and all three still read `shipped`. They only
    # completed once the pull was refreshed by hand.
    #
    # The terminal marker is GROUND TRUTH about what the box did; our failure to see the start is a
    # gap in our observation, and must never discard a real outcome. `_complete_done` still gates on
    # the completion artifact actually being present (invariant 9d), so this admits a completion,
    # never invents one.
    ("shipped", "done"),
    ("shipped", "task_failed"),
    ("shipped", "cancelled"),
    ("running", "done"),
    ("running", "task_failed"),
    ("running", "infra_failed"),
    ("running", "preempting"),
    ("infra_failed", "queued"),
    ("preempting", "queued"),
    # Cancel-in-flight (2026-07-09): a shipped/running/preempting task can't go straight to
    # `cancelled` (its worker is live) — it goes to `cancelling`, the dispatcher writes a CANCEL
    # marker, the worker stops, and the CANCELLED marker drives `cancelling`->`cancelled`. Unlike
    # preemption (which requeues), cancel is terminal.
    ("shipped", "cancelling"),
    ("running", "cancelling"),
    ("preempting", "cancelling"),
    ("cancelling", "cancelled"),
    # Retrospective bug (2026-07-14): `preempting` is not a distinct execution state — it's
    # `running` with an eviction request pending, and the box-side kill guard (task-dispatcher
    # spec invariant 17b) deliberately never kills until a checkpoint newer than the PREEMPT
    # marker exists. A task with no such checkpoint keeps running to its own natural outcome —
    # DONE, FAILED_<rc>, or its instance going lost — and invariant 17b/17c say "natural
    # completion always wins". But every terminal-outcome transition below was missing from this
    # set, so `_complete_done`/`_complete_failed`/`_infra_fail` silently no-op'd (`transition()`
    # never raises on an illegal pair — it returns ok=False and the caller didn't check) and the
    # task stayed `preempting` forever with its results stranded on disk. Observed live overnight
    # 2026-07-13/14: two pretrains preempted at 00:18/00:20 for a higher-priority job, neither
    # ever wrote a checkpoint, both kept training for 7+ hours undetected. These three close
    # every terminal path `running` already had, so `preempting` can reach the same outcomes.
    ("preempting", "done"),
    ("preempting", "task_failed"),
    ("preempting", "infra_failed"),
})

LEGAL_STATES = frozenset({
    "queued", "claimed", "shipped", "running", "done", "task_failed", "infra_failed",
    "cancelled", "preempting", "cancelling",
})

# States that keep a task "open" on its instance (registry invariant 8's derived slot-usage sum,
# extended by dispatcher spec invariant 8 to include preempting; and `cancelling`, whose worker
# still occupies its slot until it stops).
OPEN_STATES = ("claimed", "shipped", "running", "preempting", "cancelling")
TERMINAL_STATES = ("cancelled", "task_failed", "done")
# A dedupe clash (invariant 3) is any state other than these three.
CLASH_EXCLUDED_STATES = ("cancelled", "task_failed", "infra_failed")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_task_id() -> str:
    return str(uuid.uuid4())


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Forward-only, additive migrations `from_version` -> SCHEMA_VERSION. Each step is idempotent
    (guarded), so a re-run or a partially-applied schema converges. Never drops/retypes a column."""
    if from_version < 2:
        # v1 -> v2 (job-artifact-contract spec inv. 9): the self-describing job manifest column.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "job_manifest_json" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN job_manifest_json TEXT")
    if from_version < 3:
        # v2 -> v3 (owned-box spec): distinguishes a self-owned, always-on box (never rented,
        # never reconciled against `vastai show instances`, never torn down on idle) from a
        # real Vast rental. Every pre-existing row predates this column and is a real Vast
        # rental, so the default is exactly right — no backfill needed. Guarded on the table
        # itself existing: a synthetic/partial old schema (e.g. a minimal fixture carrying only
        # `tasks`, at whatever version) has nothing to add the column to yet.
        has_instances = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instances'").fetchone()
        if has_instances:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
            if "source" not in cols:
                conn.execute("ALTER TABLE instances ADD COLUMN source TEXT NOT NULL DEFAULT 'vast'")
    if from_version < 4:
        # v3 -> v4 (ship-artifact-build spec): the queuer builds a ship-ready artifact and records
        # WHICH blob, its content digest, and the format. NULL on every pre-existing row, which is
        # exactly the "pre-cutover" marker the coordinator keys its legacy build path on — no
        # backfill is possible (those tasks were never built home-side) and none is wanted.
        has_tasks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
        if has_tasks:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            for col in ("code_blob", "code_sha256", "code_format"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    if from_version < 5:
        # v4 -> v5 (task-dispatcher spec inv. 4g): sibling co-location. Purely additive — an existing
        # registry has no groups, so creating the table changes nothing until a task declares
        # `resource_hint.colocate`. `CREATE TABLE IF NOT EXISTS` keeps the step idempotent, and it is
        # the same statement `SCHEMA_SQL` runs for a fresh DB, so the two paths cannot drift.
        # ⚠ THE COST OF ANY BUMP, stated once here: the registry is SHARED across worktrees, and
        # `connect` refuses a `user_version` NEWER than its own code's. So the first new-code write
        # stamps 5 and every checkout still on 4 gets "refusing to open a future schema" until it
        # merges — and the coordinator daemon must be restarted (`make dispatch-restart`) as part of
        # landing, or the next restart is the one that discovers this.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS colocations (
              key         TEXT PRIMARY KEY,
              instance_id INTEGER NOT NULL,
              pinned_at   TEXT NOT NULL,
              pinned_by   TEXT NOT NULL
            )""")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    """Open (creating + initializing the schema if absent) with WAL + a 5s busy timeout —
    invariant 7/10. An older `user_version` is migrated forward in place (additive only); a NEWER
    one (a downgrade) still refuses (raises SystemExit) — the on-disk schema is from the future."""
    conn = sqlite3.connect(path, timeout=5.0, isolation_level="")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version == 0:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    elif version < SCHEMA_VERSION:
        _migrate(conn, version)
    elif version > SCHEMA_VERSION:
        conn.close()
        raise SystemExit(
            f"registry: db schema user_version={version} is newer than this code's "
            f"{SCHEMA_VERSION} — refusing to open a future schema (downgrade the DB or update code)")
    return conn


def log_event(conn: sqlite3.Connection, event: str, detail: str,
              task_id: str | None = None, instance_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO events(t, task_id, instance_id, event, detail) VALUES (?,?,?,?,?)",
        (now_iso(), task_id, instance_id, event, detail))


def find_clash(conn: sqlite3.Connection, config_hash: str) -> sqlite3.Row | None:
    """Invariant 3: a task with the same config_hash in any non-terminal-for-dedupe state."""
    placeholders = ",".join("?" for _ in CLASH_EXCLUDED_STATES)
    row = conn.execute(
        f"SELECT * FROM tasks WHERE config_hash=? AND state NOT IN ({placeholders}) "
        "ORDER BY created_at LIMIT 1",
        (config_hash, *CLASH_EXCLUDED_STATES)).fetchone()
    return row


def find_arm_matches(conn: sqlite3.Connection, arm_hash: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE arm_hash=? ORDER BY created_at",
                         (arm_hash,)).fetchall()


def insert_task(conn: sqlite3.Connection, **fields) -> None:
    fields.setdefault("state", "queued")
    fields.setdefault("retries_used", 0)
    fields.setdefault("updated_at", fields["created_at"])
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO tasks({cols}) VALUES ({qs})", tuple(fields.values()))
    log_event(conn, "add", f"queued by {fields.get('created_by')}", task_id=fields["id"])
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def get_task_by_name(conn: sqlite3.Connection, grp: str, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE grp=? AND name=?", (grp, name)).fetchone()


def list_tasks(conn: sqlite3.Connection, states: list[str] | None = None,
               grp: str | None = None) -> list[sqlite3.Row]:
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({','.join('?' for _ in states)})")
        params.extend(states)
    if grp:
        clauses.append("grp=?")
        params.append(grp)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM tasks{where} ORDER BY priority DESC, created_at ASC", params).fetchall()


def get_events(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()


@dataclass
class TransitionResult:
    ok: bool
    reason: str  # "ok" | "illegal" | "race" | "not_found"


def transition(conn: sqlite3.Connection, task_id: str, to_state: str, event: str, detail: str,
               extra_set: dict | None = None) -> TransitionResult:
    """Invariants 4/5/6: read current state, validate (from,to) against LEGAL_TRANSITIONS,
    CAS-update, log the event atomically in the same transaction. Never raises for a
    validation/race failure — the caller decides what that means (exit code, backoff, etc)."""
    row = get_task(conn, task_id)
    if row is None:
        return TransitionResult(False, "not_found")
    frm = row["state"]
    if (frm, to_state) not in LEGAL_TRANSITIONS:
        return TransitionResult(False, "illegal")
    sets = {"state": to_state, "updated_at": now_iso()}
    if extra_set:
        sets.update(extra_set)
    set_clause = ", ".join(f"{k}=?" for k in sets)
    cur = conn.execute(
        f"UPDATE tasks SET {set_clause} WHERE id=? AND state=?",
        (*sets.values(), task_id, frm))
    if cur.rowcount != 1:
        return TransitionResult(False, "race")
    log_event(conn, event, detail, task_id=task_id, instance_id=row["instance_id"])
    conn.commit()
    return TransitionResult(True, "ok")


def cancel_task(conn: sqlite3.Connection, task_id: str, reason: str) -> TransitionResult:
    """Cancel from any non-terminal state (cancel-in-flight, 2026-07-09).

    ``queued``/``claimed`` have no running worker -> straight to ``cancelled``. ``shipped``/
    ``running``/``preempting`` have a live worker -> ``cancelling`` (a request the dispatcher
    executes by writing a CANCEL marker; the worker stops and the CANCELLED marker completes the
    terminal ``cancelling``->``cancelled``). ``TransitionResult.reason`` distinguishes the no-op
    cases so the CLI can report them precisely.

    `reason` (REQUIRED, owner directive 2026-07-31) is the CANCELLER'S stated reason, recorded in
    the event detail. A terminal `cancelled` previously carried no metadata, so the registry could
    not distinguish a task STOPPED BECAUSE IT FINISHED ITS BUDGET from one ABANDONED — and those
    have opposite meanings for cost and calibration. Measured cost of that ambiguity, 2026-07-31: a
    fleet cost review read 452 cancellations as discarded work and had to be corrected by hand, and
    any report scoring completion as `state='done'` books the entire FLOPs-budgeted workstream at 0%
    completion. Forcing it at the only call site is what keeps the distinction in the registry
    instead of in someone's head."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("cancel_task requires a non-empty reason")
    reason = reason.strip()
    row = get_task(conn, task_id)
    if row is None:
        return TransitionResult(False, "not_found")
    state = row["state"]
    if state in ("queued", "claimed"):
        return transition(conn, task_id, "cancelled", "cancel", f"cancelled via runq: {reason}")
    if state in ("shipped", "running", "preempting"):
        return transition(conn, task_id, "cancelling", "cancel_requested",
                          f"cancel requested via runq; dispatcher will stop the worker: {reason}")
    if state == "cancelling":
        return TransitionResult(False, "already_cancelling")
    return TransitionResult(False, "terminal")


def rate(conn: sqlite3.Connection) -> float:
    """Invariant 11: committed $/hr across provisioning|live|draining instances."""
    (total,) = conn.execute(
        "SELECT COALESCE(SUM(dph_usd),0) FROM instances "
        "WHERE state IN ('provisioning','live','draining')").fetchone()
    return float(total)


def spend(conn: sqlite3.Connection, since: str | None = None) -> float:
    """Realized cost from instances.cost_usd, optionally only spend falling at/after `since`.

    Terminal boxes contribute their stamped `cost_usd`, selected by `destroyed_at`. LIVE boxes are
    counted too (invariant 12b: the dispatcher accrues their `cost_usd` every cycle) — without them
    a spend query reads $0 for a fleet that is burning money right now, which is exactly how a
    $21/day spike stayed invisible until after it ended (2026-07-31).

    For the `since` window a live box is CLIPPED to the window (`dph x hours(max(created_at, since)
    -> now)`) rather than counted whole, so a long-lived box created days before `since` does not
    dump its entire history into a short window. Terminal rows are not clipped: their cost is a
    single stamped scalar with no start/stop detail to apportion, and they are already selected by a
    terminal timestamp inside the window."""
    if since:
        (terminal,) = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM instances WHERE destroyed_at >= ?",
            (since,)).fetchone()
        (live,) = conn.execute(
            "SELECT COALESCE(SUM("
            "  COALESCE(dph_usd,0) * MAX(0.0, (julianday('now') - julianday(MAX(created_at, ?))) * 24.0)"
            "),0) FROM instances WHERE destroyed_at IS NULL", (since,)).fetchone()
        return float(terminal) + float(live)
    (total,) = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM instances").fetchone()
    return float(total)


COLOCATE_HINT_KEY = "colocate"


def colocate_key_of(resource_hint_json: str | None) -> str | None:
    """The colocation group named by a stored `resource_hint_json`, or None.

    Lives here rather than only in the dispatcher because THREE readers need it and must agree:
    the dispatcher (placement), `runq colocate` (the verification report) and the tests. Parsing
    failures degrade to "no group" — a corrupt hint must never wedge a placement pass."""
    if not resource_hint_json or COLOCATE_HINT_KEY not in resource_hint_json:
        return None
    try:
        v = (json.loads(resource_hint_json) or {}).get(COLOCATE_HINT_KEY)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    if v is None:
        return None
    return str(v).strip() or None


FORCE_BOX_HINT_KEY = "force_box"


def force_box_error(conn: sqlite3.Connection | None, resource_hint) -> str | None:
    """Why this hint may NOT carry `force_box` (dispatcher inv. 4i-1), or None if it is coherent.

    `resource_hint` is the FINAL hint, as a dict or as the stored JSON text. Lives here because the
    two submission boundaries — `runq` and the coordinator API — must refuse for identical reasons,
    and this is the one module both already import. A hint with no `force_box` key is always fine:
    the feature costs an ordinary task nothing.

    `conn=None` runs the STRUCTURAL checks only and skips "is the box a registered owned box". That
    is for a `runq` on the API transport: the registry of record is the coordinator's, a remote
    client may hold no copy of it at all, and the server runs this again with its own connection.

    The dispatcher does NOT call this. It re-derives "forced" from the hint and the instance view
    (`dispatcher.forced_box`), and treats anything this would have refused as simply not forced —
    a queue-time refusal is for the operator's benefit, the placement rule is what is safe."""
    if isinstance(resource_hint, str):
        if FORCE_BOX_HINT_KEY not in resource_hint:
            return None           # same cheap pre-filter as `colocate_key_of`; nothing else changes
        try:
            resource_hint = json.loads(resource_hint)
        except (json.JSONDecodeError, TypeError):
            return "resource_hint_json names force_box but is not valid JSON"
    hint = resource_hint if isinstance(resource_hint, dict) else {}
    if FORCE_BOX_HINT_KEY not in hint:
        return None
    force = hint[FORCE_BOX_HINT_KEY]
    if not isinstance(force, bool):
        return f"force_box must be a boolean (true/false), got {force!r}"
    if not force:
        return None
    box = str(hint.get("box") or "").strip()
    if not box:
        return ("--force-box requires --box <label>: it means 'run on THIS box now, ignoring its "
                "capacity gates', so there has to be a box to mean")
    if str(hint.get(COLOCATE_HINT_KEY) or "").strip():
        return ("--force-box and --colocate are mutually exclusive: a forced task names its own "
                "box, a co-located one lets the coordinator choose it")
    if conn is None:
        return None
    row = conn.execute("SELECT id, label, source FROM instances WHERE CAST(id AS TEXT)=? OR label=?",
                       (box, box)).fetchone()
    if row is None:
        return (f"--force-box: no registered box with id or label {box!r}. A forced task bypasses "
                f"an OWNED box's capacity gates, so the box must already be in the registry")
    if row["source"] != "owned":
        return (f"--force-box: box {box!r} is not an owned box (source={row['source']!r}). The "
                f"bypass applies to owned boxes only and never causes or overrides a rental")
    return None


def boxes_started_on(conn: sqlite3.Connection, task_row) -> tuple:
    """Every box a task actually STARTED on, in order — the ground truth for "did these arms run
    together".

    ⛔ READ FROM `events`, NOT `tasks.instance_id`. That column holds only the LATEST placement, so a
    task requeued onto a second box reports as if it had only ever run on the second — and that task
    is exactly the one worth catching, since it is also the one with `resumes > 0`. Reading the
    column makes a split INVISIBLE precisely where it is real. `scripts/diagnostics/paired_seed_diff.py`
    learned this the expensive way (`m51_present_seeds` seed 1 ran its two arms on boxes 40000044 and
    40000043 while seed 2 was co-located — invisible until its check existed); this is that read,
    hoisted here so its second consumer (`runq colocate`) cannot re-acquire the same bug.

    Falls back to the column only when NO `start` row exists — a task claimed but never started has
    no event to read, and reporting nothing there would hide a pending placement rather than a
    split."""
    got = tuple(str(r[0]) for r in conn.execute(
        "SELECT DISTINCT instance_id FROM events WHERE task_id=? AND event='start' "
        "AND instance_id IS NOT NULL ORDER BY seq", (task_row["id"],)))
    if got:
        return got
    return (str(task_row["instance_id"]),) if task_row["instance_id"] is not None else ()


def colocation_pins(conn: sqlite3.Connection) -> dict:
    """Every recorded group pin: `{key: {"instance_id", "pinned_at", "pinned_by"}}`."""
    return {r["key"]: {"instance_id": int(r["instance_id"]), "pinned_at": r["pinned_at"],
                       "pinned_by": r["pinned_by"]}
            for r in conn.execute("SELECT * FROM colocations")}


def pin_colocation(conn: sqlite3.Connection, key: str, instance_id: int, task_id: str) -> int:
    """FIRST WRITER WINS: record `key -> instance_id`, and return the instance the group is ACTUALLY
    pinned to (which is the existing pin if one was already recorded).

    `INSERT OR IGNORE` + re-read rather than a plain insert, because the return value is what the
    caller places the remaining siblings against — silently overwriting an existing pin would split
    a group across two boxes and produce exactly the unpaired campaign this feature exists to stop."""
    conn.execute("INSERT OR IGNORE INTO colocations(key, instance_id, pinned_at, pinned_by) "
                 "VALUES (?,?,?,?)", (key, int(instance_id), now_iso(), task_id))
    conn.commit()
    row = conn.execute("SELECT instance_id FROM colocations WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row is not None else int(instance_id)


def unpin_colocation(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM colocations WHERE key=?", (key,))
    conn.commit()


def colocation_members(conn: sqlite3.Connection, key: str | None = None,
                       grp: str | None = None) -> dict:
    """`{group_key: [task rows]}` for every task declaring a colocation group.

    The key lives inside `resource_hint_json` (where `box` lives — placement directives belong to
    the hint), so this narrows with a `LIKE` on the JSON text and then parses properly; the `LIKE`
    is a cheap prefilter, never the decision."""
    clauses, params = ["resource_hint_json LIKE ?"], [f'%"{COLOCATE_HINT_KEY}"%']
    if grp:
        clauses.append("grp=?")
        params.append(grp)
    out: dict = {}
    for r in conn.execute(f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} "
                          "ORDER BY created_at ASC", params):
        k = colocate_key_of(r["resource_hint_json"])
        if k is None or (key is not None and k != key):
            continue
        out.setdefault(k, []).append(r)
    return out


def upsert_owned_box(conn: sqlite3.Connection, label: str, host: str, port: int | None = None,
                     slots: int | None = None, gpu_name: str | None = None) -> dict:
    """Register an owned box, or update the one already registered under `label` — changing what
    the caller SUPPLIED and nothing else (task-dispatcher inv. 20e-1). Does not commit or log.

    ONE function for both writers (`register_owned_box.py` on the local transport, the API's
    `POST /v1/boxes` on the other). They used to carry a copy each, and both copies wrote every
    column and forced `state='live'` — so re-pointing a HELD box at a new address handed it back to
    the packer (its pause record left behind), and an omitted port / slots / GPU name silently
    reset to 22 / 1 / NULL. Measured 2026-10-03 on `desktop`: re-registered after a reinstall while
    on hold, live within the same poll.

    `None` means "not supplied": keep the row's value, or take the new-box default (port 22, 1
    slot, no GPU name). A `paused` box stays paused; any other state becomes `live`, which is what
    revives a box that had gone `unreachable` behind an old address.

    Returns {id, created, state, kept_paused, host, port, slots, gpu_name} — the row as written."""
    row = conn.execute("SELECT * FROM instances WHERE label=? AND source='owned'",
                       (label,)).fetchone()
    if row is None:
        # Negative id: disjoint from every real Vast instance id (always positive) — one below the
        # lowest id already in use, so a second owned box cannot collide with the first.
        (min_id,) = conn.execute("SELECT COALESCE(MIN(id), 0) FROM instances").fetchone()
        iid = min(min_id, 0) - 1
        port_, slots_ = (22 if port is None else port), (1 if slots is None else slots)
        hard_cap = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO instances(id, machine_id, label, created_at, state, dph_usd, gpu_name, "
            "ssh_host, ssh_port, slots_total, hard_cap_at, cost_usd, source) "
            "VALUES (?,NULL,?,?,?,0.0,?,?,?,?,?,0.0,'owned')",
            (iid, label, now_iso(), "live", gpu_name, host, port_, slots_, hard_cap))
        return {"id": iid, "created": True, "state": "live", "kept_paused": False,
                "host": host, "port": port_, "slots": slots_, "gpu_name": gpu_name}
    iid = row["id"]
    port_ = row["ssh_port"] if port is None else port
    slots_ = row["slots_total"] if slots is None else slots
    gpu_ = row["gpu_name"] if gpu_name is None else gpu_name
    kept_paused = row["state"] == "paused"
    state = "paused" if kept_paused else "live"
    conn.execute("UPDATE instances SET ssh_host=?, ssh_port=?, slots_total=?, gpu_name=?, state=?, "
                 "destroyed_at=NULL WHERE id=?", (host, port_, slots_, gpu_, state, iid))
    return {"id": iid, "created": False, "state": state, "kept_paused": kept_paused,
            "host": host, "port": port_, "slots": slots_, "gpu_name": gpu_}


def used_slots(conn: sqlite3.Connection, instance_id: int) -> int:
    """Registry invariant 8: derived, never stored."""
    placeholders = ",".join("?" for _ in OPEN_STATES)
    (total,) = conn.execute(
        f"SELECT COALESCE(SUM(slots),0) FROM tasks WHERE instance_id=? AND state IN "
        f"({placeholders})", (instance_id, *OPEN_STATES)).fetchone()
    return int(total)
