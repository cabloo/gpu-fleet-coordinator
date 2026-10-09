"""Coordinator API — the ONLY way a client writes the run registry (remote-submit spec, M1/M2).

WHAT THIS IS. `runq` used to write the data root directly: the task row, the ship blob, the compile
caches, the blob GC. That assumed the queuer and the coordinator shared a filesystem AND a uid, which
stopped being true at the 2026-09-17 cutover — and the stopgap that restored it (a recursive ACL onto
the data root) hands every unattended agent in the devcontainer write access to the whole registry and
~190 GB of results. This service replaces those writes with an authenticated API, so the only
capability a client holds is *queue a task* and *cancel its own*.

WHAT THIS IS NOT. It does no authentication of its own (inv. 1): mutual TLS is terminated by stock
nginx in front, and this process receives a VERIFIED identity in headers over a unix socket it shares
with that proxy and nothing else. It also never opens a blob (inv. 13) — a blob is opaque bytes with a
digest, which is what lets the platform stay payload-blind.

⚠ IT HAS NO NETWORK (`network_mode: none`, inv. 3). That is deliberate and it constrains the design:
anything needing to reach a box — the `probe`/`pause` routes of M2 — cannot ssh from here. Those write
a one-shot request row that the DISPATCHER consumes on its next poll, the shape `roll_now.py` already
established.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socketserver
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_store  # noqa: E402
import job_manifest  # noqa: E402
import registry_db  # noqa: E402

API_VERSION = 1
SOCKET_PATH = os.environ.get("COORD_API_SOCKET", "/run/coord-api/api.sock")
CLIENTS_PATH = os.environ.get("COORD_API_CLIENTS", "/run/secrets/api/clients.json")
MAX_BLOB_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
BLOB_ID_RE = re.compile(r"[0-9a-f]{8,64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: Every column `registry_db.insert_task` accepts from a submission. Derived from the insert's own
#: signature in spirit: a field NOT in here is refused rather than passed through (inv. 14), so a
#: client can never set `state`, `instance_id`, `retries_used` or anything else the coordinator owns.
TASK_FIELDS = {
    "grp", "name", "entrypoint", "args_json", "config_json", "config_hash", "arm_hash",
    "git_sha", "slots", "est_minutes", "priority", "max_retries", "resource_hint_json",
    "resume_checkpoint", "job_manifest_json", "code_format",
}
REQUIRED_TASK_FIELDS = {"grp", "name", "entrypoint", "args_json", "config_json", "config_hash",
                        "arm_hash", "slots", "est_minutes", "priority"}
INT_TASK_FIELDS = {"slots", "est_minutes", "priority", "max_retries"}


class ApiError(Exception):
    """A refusal with the HTTP status and the machine-readable `code` the client maps to an exit."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _clients() -> dict:
    """The authorization table, re-read PER REQUEST (inv. 4) so `api_pki.sh revoke` and an edit to
    this file take effect immediately rather than at the next restart. Unreadable or malformed ⇒ {},
    i.e. DENY EVERYTHING: an authorization file we cannot parse must never fail open."""
    try:
        with open(CLIENTS_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _blob_keep_max(conn) -> int:
    """Unreferenced ship blobs to retain — the same `bundle_compile_cache_max` knob `runq` read when
    it owned the GC (inv. 18). Defaults on any unreadable/absent row rather than guessing wider."""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='bundle_compile_cache_max'").fetchone()
        return max(1, int(json.loads(row[0]))) if row else 64
    except Exception:  # noqa: BLE001
        return 64


def _require(cond, status, code, message):
    if not cond:
        raise ApiError(status, code, message)


def _validate_task(t: dict) -> dict:
    _require(isinstance(t, dict), 422, "validation", "each task must be a JSON object")
    unknown = set(t) - TASK_FIELDS
    _require(not unknown, 422, "validation", f"unknown task field(s): {sorted(unknown)}")
    missing = REQUIRED_TASK_FIELDS - set(t)
    _require(not missing, 422, "validation", f"missing task field(s): {sorted(missing)}")
    for k in ("grp", "name", "entrypoint"):
        _require(isinstance(t[k], str) and t[k].strip(), 422, "validation", f"{k} must be non-empty")
    for k in INT_TASK_FIELDS:
        if k in t and t[k] is not None:
            _require(isinstance(t[k], int) and not isinstance(t[k], bool), 422, "validation",
                     f"{k} must be an integer")
    if t.get("job_manifest_json"):
        try:
            job_manifest.parse(json.loads(t["job_manifest_json"]))
        except (ValueError, job_manifest.JobManifestError) as e:
            raise ApiError(422, "validation", f"job manifest rejected: {e}") from None
    return t


def _validate_envelope(env: dict) -> dict:
    _require(isinstance(env, dict), 422, "validation", "envelope must be a JSON object")
    _require(env.get("envelope_version") == API_VERSION, 422, "validation",
             f"envelope_version must be {API_VERSION}")
    for k in ("submission_id", "created_by", "blob_id", "code_sha256"):
        _require(isinstance(env.get(k), str) and env[k].strip(), 422, "validation",
                 f"{k} must be a non-empty string")
    _require(bool(BLOB_ID_RE.fullmatch(env["blob_id"])), 422, "validation", "blob_id is not a blob id")
    _require(bool(SHA256_RE.fullmatch(env["code_sha256"])), 422, "validation",
             "code_sha256 is not a sha256")
    tasks = env.get("tasks")
    _require(isinstance(tasks, list) and tasks, 422, "validation", "tasks must be a non-empty list")
    for t in tasks:
        _validate_task(t)
    return env


class Api:
    """Route handling, separated from the HTTP plumbing so the tests drive it directly."""

    def __init__(self, db_path: str, root: Path):
        self.db_path, self.root = db_path, Path(root)

    # -- helpers -------------------------------------------------------------------------------
    def _conn(self):
        return registry_db.connect(self.db_path)

    def _authorize(self, client: str, action: str) -> None:
        """DENY BY DEFAULT (inv. 4). An unknown client, or a known one without this action, is 403 —
        there is no implicit grant anywhere, including for a client that holds every other action."""
        actions = _clients().get(client)
        _require(isinstance(actions, list) and action in actions, 403, "forbidden",
                 f"client {client!r} may not {action}")

    # -- routes --------------------------------------------------------------------------------
    def ping(self) -> dict:
        return {"ok": True, "api": API_VERSION}

    def get_blob(self, client: str, blob_id: str) -> dict:
        self._authorize(client, "blob")
        _require(bool(BLOB_ID_RE.fullmatch(blob_id)), 422, "validation", "not a blob id")
        data = artifact_store.load(self.root, blob_id)
        _require(data is not None, 404, "not_found", "no such blob")
        return {"ok": True, "sha256": artifact_store.digest(data), "size": len(data)}

    def put_blob(self, client: str, blob_id: str, body: bytes, declared_sha: str | None) -> dict:
        """Opaque bytes in, digest checked, published. The service NEVER opens the tar (inv. 13):
        that is what keeps the platform payload-blind — a Go build and a Cython tree are the same
        object here."""
        self._authorize(client, "blob")
        _require(bool(BLOB_ID_RE.fullmatch(blob_id)), 422, "validation", "not a blob id")
        _require(bool(declared_sha and SHA256_RE.fullmatch(declared_sha)), 422, "validation",
                 "X-Content-SHA256 must be a sha256")
        got = hashlib.sha256(body).hexdigest()
        _require(got == declared_sha, 422, "validation",
                 f"body sha256 {got[:12]} != declared {declared_sha[:12]}")
        # FIRST PUBLISHER WINS, exactly as `artifact_store.put` does locally. A blob id addresses the
        # build INPUTS, not the bytes, and a rebuild is not byte-reproducible — so the same id with
        # different bytes is the NORMAL case of two builds of one tree, not a conflict. The reply
        # carries the digest of what is actually STORED, and that is the digest a submission must
        # record: boxes cache a blob by id and verify it by sha256, so recording the loser's digest
        # fails integrity on every box that holds the winner (artifact_store.put, 2026-08-14).
        existing = artifact_store.load(self.root, blob_id)
        if existing is not None:
            return {"ok": True, "stored": False, "sha256": artifact_store.digest(existing),
                    "size": len(existing)}
        published = artifact_store.put(self.root, blob_id, body)   # may still lose a race: fine
        stored_sha = artifact_store.digest(published)
        return {"ok": True, "stored": stored_sha == got, "sha256": stored_sha,
                "size": len(published)}

    def submit(self, client: str, env: dict) -> tuple[int, dict]:
        self._authorize(client, "submit")
        env = _validate_envelope(env)
        conn = self._conn()
        blob = artifact_store.load(self.root, env["blob_id"])
        _require(blob is not None, 422, "validation",
                 f"blob {env['blob_id']} is not stored — PUT it first")
        _require(artifact_store.digest(blob) == env["code_sha256"], 422, "validation",
                 "the stored blob's digest does not match code_sha256")

        replayed = self._replay(conn, client, env["submission_id"])
        if replayed is not None:
            return 201, {"ok": True, "task_ids": replayed, "replayed": True}

        # ONE TRANSACTION for every row (inv. 15): a sweep is all-or-nothing, so a cell that trips
        # dedupe leaves the registry exactly as it was rather than a half-queued grid.
        task_ids, now = [], registry_db.now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for t in env["tasks"]:
                # dispatcher inv. 4i-1: `resource_hint_json` is an opaque string to the allowlist
                # above, so a `force_box` hint survives the transport untouched — and is re-checked
                # HERE against the registry, because the client's own check is not a trust boundary.
                hint_err = registry_db.force_box_error(conn, t.get("resource_hint_json"))
                if hint_err is not None:
                    raise ApiError(422, "validation", hint_err)
                if not env.get("force"):
                    clash = registry_db.find_clash(conn, t["config_hash"])
                    if clash is not None:
                        raise ApiError(409, "duplicate",
                                       f"task {clash['id']} ({clash['grp']}/{clash['name']}) already "
                                       f"has this exact config (state={clash['state']})")
                tid = registry_db.new_task_id()
                fields = {k: v for k, v in t.items() if k in TASK_FIELDS}
                fields.setdefault("max_retries", 2)
                fields.setdefault("git_sha", "")
                # ⛔ THE LINK BETWEEN A TASK AND ITS CODE. The envelope carries the artifact once, at
                # the top level, for every row; the dispatcher ships whatever `code_blob` names and
                # re-checks it against `code_sha256` (ship-artifact-build inv. 1/6). Omitting these
                # was the first live-acceptance failure, 2026-09-18: the task failed at ship with
                # "task carries no ship artifact (pre-cutover row)" — a unit test never read them.
                fields["code_blob"] = env["blob_id"]
                fields["code_sha256"] = env["code_sha256"]
                conn.execute(
                    "INSERT INTO tasks(id, created_at, updated_at, created_by, state, retries_used, "
                    + ", ".join(fields) + ") VALUES (?,?,?,?,?,?," + ",".join("?" * len(fields)) + ")",
                    (tid, now, now, env["created_by"], "queued", 0, *fields.values()))
                registry_db.log_event(
                    conn, "add",
                    json.dumps({"by": env["created_by"], "client": client,
                                "submission_id": env["submission_id"]}, separators=(",", ":")),
                    task_id=tid)
                task_ids.append(tid)
            ce = env.get("compile_event")
            if isinstance(ce, dict):
                registry_db.log_event(conn, "compile", json.dumps(ce, separators=(",", ":")))
            conn.commit()
        except ApiError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError as e:
            # `UNIQUE(grp, name)` is the registry's lane identity (run-registry inv. 4). A client
            # re-using a lane name is a CLIENT error — 409, with the constraint named — never the
            # 500 it first produced here, which would have read as "the coordinator is broken".
            conn.rollback()
            raise ApiError(409, "duplicate", f"a task already occupies that lane: {e}") from None
        except Exception as e:  # noqa: BLE001 — any failure leaves the registry untouched
            conn.rollback()
            raise ApiError(500, "server_error", f"{type(e).__name__}: {e}") from None

        # Blob GC moves with the store write (inv. 18) — the call `runq.py` used to make itself, and
        # the reason a blob PUT whose submission never arrives cannot accumulate.
        try:
            artifact_store.gc(self.root, artifact_store.live_blob_ids(conn), _blob_keep_max(conn))
        except Exception:  # noqa: BLE001 — GC must never fail a submission that COMMITTED
            pass
        return 201, {"ok": True, "task_ids": task_ids, "replayed": False}

    def _replay(self, conn, client: str, submission_id: str) -> list[str] | None:
        """Idempotent per client (inv. 17). A client that lost the response to a timeout resends and
        gets its original task ids; the same id from a DIFFERENT client is a collision, not a retry."""
        rows = conn.execute(
            "SELECT task_id, detail FROM events WHERE event='add' AND detail LIKE ?",
            (f'%"submission_id":"{submission_id}"%',)).fetchall()
        if not rows:
            return None
        for r in rows:
            try:
                if json.loads(r["detail"]).get("client") != client:
                    raise ApiError(409, "duplicate",
                                   "that submission_id was used by a different client")
            except ValueError:
                continue
        return [r["task_id"] for r in rows]

    def cancel(self, client: str, task_id: str, body: dict) -> dict:
        reason = body.get("reason")
        _require(isinstance(reason, str) and reason.strip(), 422, "validation",
                 "cancel requires a reason (registry inv. 4a)")
        conn = self._conn()
        row = registry_db.get_task(conn, task_id)
        _require(row is not None, 404, "not_found", "no such task")
        if not self._may_cancel_any(client):
            self._authorize(client, "cancel_own")
            _require(self._submitting_client(conn, task_id) == client, 403, "forbidden",
                     "cancel_own may cancel only tasks this client submitted")
        result = registry_db.cancel_task(conn, task_id, reason)
        _require(result.ok, 409, "illegal_transition",
                 f"task is {row['state']} and cannot be cancelled ({result.reason})")
        return {"ok": True, "task_id": task_id, "state": "cancelling/cancelled"}

    def _may_cancel_any(self, client: str) -> bool:
        return "cancel_any" in (_clients().get(client) or [])

    def _submitting_client(self, conn, task_id: str) -> str | None:
        row = conn.execute("SELECT detail FROM events WHERE task_id=? AND event='add' "
                           "ORDER BY seq LIMIT 1", (task_id,)).fetchone()
        try:
            return json.loads(row["detail"]).get("client") if row else None
        except (ValueError, TypeError):
            return None   # a pre-API task carries a plain-text add event: nobody owns it

    # -- M2: operator writers. NONE of these ssh anywhere — see the module docstring. -----------
    def box_request(self, client: str, box: str, verb: str) -> dict:
        action = {"probe": "probe", "pause": "pause", "hold": "pause", "drain": "pause",
                  "resume": "pause"}[verb]
        self._authorize(client, action)
        conn = self._conn()
        row = conn.execute("SELECT id, label FROM instances WHERE CAST(id AS TEXT)=? OR label=?",
                           (box, box)).fetchone()
        _require(row is not None, 404, "not_found", f"no instance with id or label {box!r}")
        key = f"probe_request_i{row['id']}" if verb == "probe" else f"{verb}_request_i{row['id']}"
        conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps({"at": registry_db.now_iso(), "by": client})))
        conn.commit()
        return {"ok": True, "instance": row["id"], "verb": verb,
                "note": "the dispatcher consumes this on its next poll"}

    #: An address that means "this host" is the 2026-09-17 cutover bug in one line: owned box -2 was
    #: registered at the DESKTOP's docker gateway, so from tower the coordinator ssh'd into
    #: ITSELF. A box is reached from the coordinator's container — it must be named accordingly.
    LOCAL_ADDRS = {"127.0.0.1", "localhost", "::1", "172.17.0.1", "host.docker.internal"}

    def register_box(self, client: str, body: dict) -> dict:
        self._authorize(client, "register_box")
        label = body.get("label")
        host = body.get("host")
        _require(isinstance(label, str) and label.strip(), 422, "validation", "label is required")
        _require(isinstance(host, str) and host.strip(), 422, "validation", "host is required")
        _require(host not in self.LOCAL_ADDRS, 422, "validation",
                 f"{host!r} means 'this host' from inside the coordinator — register a LAN name")
        # Absent or null = "not supplied": keep the box's current value (task-dispatcher 20e-1). The
        # old `int(body.get("port") or 22)` turned an omitted field into a RESET to the default.
        port, slots, gpu = body.get("port"), body.get("slots"), body.get("gpu_name")
        is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)   # noqa: E731
        _require(port is None or (is_int(port) and 1 <= port <= 65535), 422, "validation",
                 "port must be an integer in 1..65535")
        _require(slots is None or (is_int(slots) and slots >= 1), 422, "validation",
                 "slots must be an integer >= 1")
        _require(gpu is None or (isinstance(gpu, str) and gpu.strip()), 422, "validation",
                 "gpu_name must be a non-empty string")
        prefer = body.get("prefer")
        _require(prefer is None or is_int(prefer), 422, "validation", "prefer must be an integer")
        conn = self._conn()
        box = registry_db.upsert_owned_box(conn, label, host, port=port, slots=slots, gpu_name=gpu)
        iid, port, slots = box["id"], box["port"], box["slots"]
        if prefer is not None:
            # Invariant 4b' pack preference, same settings row `register_owned_box.py --prefer` writes
            # on the local transport. Read at dispatcher start, so it needs a coordinator roll.
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f"box_preference_i{iid}", json.dumps(prefer)))
        registry_db.log_event(conn, "register", json.dumps(
            {"label": label, "host": host, "port": port, "slots": slots, "prefer": prefer,
             "state": box["state"], "by": client}, separators=(",", ":")), instance_id=iid)
        conn.commit()
        return {"ok": True, "instance": iid, "label": label, "state": box["state"],
                "kept_paused": box["kept_paused"],
                "note": ("the box was paused and stays paused — resume it when it should take work"
                         if box["kept_paused"] else
                         "the dispatcher brings its worker up on the next refresh")}

    def roll_now(self, client: str) -> dict:
        self._authorize(client, "roll_now")
        conn = self._conn()
        conn.execute("INSERT INTO settings(key, value) VALUES ('worker_roll_now','true') "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        conn.commit()
        return {"ok": True, "armed": True}


def _handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "coord-api"

        def log_message(self, fmt, *args):   # one line per request, on stdout (inv. 8/14)
            client = self.headers.get("X-Client-DN", "-")
            sys.stdout.write(f"{self.log_date_time_string()} {client} {fmt % args}\n")
            sys.stdout.flush()

        # -- plumbing ---------------------------------------------------------------------------
        def _identity(self) -> str:
            """The proxy OVERWRITES these headers on every request (inv. 2), so what arrives here is
            a verified identity or nothing. Refusing an unverified one is belt-and-braces: if the
            proxy is ever misconfigured, this service fails CLOSED rather than trusting a client."""
            verify = self.headers.get("X-Client-Verify")
            dn = self.headers.get("X-Client-DN") or ""
            _require(verify == "SUCCESS", 403, "client_cert",
                     "no verified client certificate on this request")
            m = re.search(r"CN\s*=\s*([^,/]+)", dn)
            _require(m is not None, 403, "client_cert", f"no CN in client DN {dn!r}")
            return m.group(1).strip()

        def _body(self, limit: int) -> bytes:
            length = self.headers.get("Content-Length")
            _require(length is not None and length.isdigit(), 411, "validation",
                     "Content-Length is required")
            n = int(length)
            _require(n <= limit, 413, "validation", f"body of {n} bytes exceeds the {limit}-byte cap")
            return self.rfile.read(n)

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _run(self, fn):
            try:
                status, payload = fn()
            except ApiError as e:
                self._send(e.status, {"ok": False, "code": e.code, "error": e.message})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"ok": False, "code": "server_error",
                                 "error": f"{type(e).__name__}: {e}"})
            else:
                self._send(status, payload)

        # -- routes -----------------------------------------------------------------------------
        def do_GET(self):
            if self.path == "/v1/ping":
                return self._run(lambda: (200, api.ping()))
            m = re.fullmatch(r"/v1/blobs/([^/]+)", self.path)
            if m:
                return self._run(lambda: (200, api.get_blob(self._identity(), m.group(1))))
            self._send(404, {"ok": False, "code": "not_found", "error": "no such route"})

        def do_PUT(self):
            m = re.fullmatch(r"/v1/blobs/([^/]+)", self.path)
            if not m:
                return self._send(404, {"ok": False, "code": "not_found", "error": "no such route"})

            def go():
                who = self._identity()
                body = self._body(MAX_BLOB_BYTES)
                return 201, api.put_blob(who, m.group(1), body,
                                         self.headers.get("X-Content-SHA256"))
            self._run(go)

        def do_POST(self):
            def json_body():
                raw = self._body(MAX_JSON_BYTES)
                try:
                    return json.loads(raw)
                except ValueError as e:
                    raise ApiError(422, "validation", f"body is not JSON: {e}") from None

            if self.path == "/v1/submissions":
                return self._run(lambda: api.submit(self._identity(), json_body()))
            m = re.fullmatch(r"/v1/tasks/([^/]+)/cancel", self.path)
            if m:
                return self._run(lambda: (200, api.cancel(self._identity(), m.group(1), json_body())))
            m = re.fullmatch(r"/v1/boxes/([^/]+)/(probe|pause|hold|drain|resume)", self.path)
            if m:
                return self._run(
                    lambda: (200, api.box_request(self._identity(), m.group(1), m.group(2))))
            if self.path == "/v1/boxes":
                return self._run(lambda: (201, api.register_box(self._identity(), json_body())))
            if self.path == "/v1/roll-now":
                return self._run(lambda: (200, api.roll_now(self._identity())))
            self._send(404, {"ok": False, "code": "not_found", "error": "no such route"})

    return Handler


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        req, _ = super().get_request()
        return req, ("unix", 0)   # BaseHTTPRequestHandler wants an (addr, port) pair


def serve(db_path: str, root: str | Path, socket_path: str = SOCKET_PATH) -> None:
    """Listen ONLY on a unix socket (inv. 3). No TCP listener exists in this process, so the proxy
    is not merely the front door — it is the only door, and `network_mode: none` removes the rest."""
    sock = Path(socket_path)
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()
    api = Api(db_path, Path(root))
    server = _UnixServer(str(sock), _handler(api))
    os.chmod(sock, 0o600)
    threading.current_thread().name = "coord-api"
    print(f"coord-api: listening on {sock} (db={db_path}, root={root})", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(os.environ.get("COORD_DATA", "/srv/fleet/experiments"))
    serve(str(root / "runs.sqlite"), root, argv[0] if argv else SOCKET_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
