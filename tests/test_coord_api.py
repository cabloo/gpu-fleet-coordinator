"""Coordinator API — remote-submit spec M1/M2 fixtures.

Driven over a REAL unix socket against the real handler, because the parts most worth pinning are the
plumbing ones: that an unverified request never reaches a route, that a body cap is enforced before
the body is read, and that a sweep is all-or-nothing.
"""

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(ROOT / "fleet"))
registry_db = _load("registry_db", "fleet/registry_db.py")
artifact_store = _load("artifact_store", "fleet/artifact_store.py")
api_server = _load("api_server", "fleet/coordinator/api_server.py")

BLOB = b"not-a-tar-and-that-is-the-point"
BLOB_ID = "a" * 24
CLIENTS = {
    "agent": ["blob", "submit", "cancel_own", "probe"],
    "operator": ["blob", "submit", "cancel_own", "cancel_any", "probe", "pause", "roll_now",
                 "register_box"],
    "reader": [],
}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    clients = tmp_path / "clients.json"
    clients.write_text(json.dumps(CLIENTS))
    monkeypatch.setattr(api_server, "CLIENTS_PATH", str(clients))
    db = str(tmp_path / "runs.sqlite")
    registry_db.connect(db).close()
    sock_path = tmp_path / "api.sock"
    server = api_server._UnixServer(str(sock_path), api_server._handler(
        api_server.Api(db, tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield _Caller(sock_path, db, tmp_path, clients)
    server.shutdown()


class _Caller:
    def __init__(self, sock_path, db, root, clients):
        self.sock_path, self.db, self.root, self.clients = sock_path, db, root, clients

    def raw(self, request: bytes) -> tuple[int, dict]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(self.sock_path))
        s.sendall(request)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        while len(rest) < length:
            rest += s.recv(65536)
        s.close()
        try:
            return status, json.loads(rest or b"{}")
        except ValueError:
            return status, {}

    def call(self, method, path, body=b"", client="agent", verify="SUCCESS", extra=None,
             content_length=None):
        headers = [f"{method} {path} HTTP/1.1", "Host: api"]
        if verify is not None:
            headers.append(f"X-Client-Verify: {verify}")
        if client is not None:
            headers.append(f"X-Client-DN: CN={client},O=fleet")
        headers.append(f"Content-Length: {len(body) if content_length is None else content_length}")
        for k, v in (extra or {}).items():
            headers.append(f"{k}: {v}")
        return self.raw(("\r\n".join(headers) + "\r\n\r\n").encode() + body)

    def store_blob(self):
        artifact_store.put(self.root, BLOB_ID, BLOB)

    def envelope(self, n=1, submission_id="s-1", force=False, **over):
        tasks = [{
            "grp": "g", "name": f"t{i}", "entrypoint": "smoke", "args_json": "[]",
            "config_json": "{}", "config_hash": f"c{i}", "arm_hash": f"a{i}",
            "slots": 1, "est_minutes": 5, "priority": 50, "code_format": "compiled",
        } for i in range(n)]
        env = {"envelope_version": 1, "submission_id": submission_id, "created_by": "me",
               "blob_id": BLOB_ID, "code_sha256": artifact_store.digest(BLOB),
               "force": force, "tasks": tasks}
        env.update(over)
        return json.dumps(env).encode()

    def tasks(self):
        conn = registry_db.connect(self.db)
        return conn.execute("SELECT id, grp, name, state, created_by FROM tasks").fetchall()


# -- identity and authorization ----------------------------------------------------------------

def test_a_request_without_a_verified_certificate_is_refused(api):
    status, body = api.call("GET", f"/v1/blobs/{BLOB_ID}", verify=None)
    assert status == 403 and body["code"] == "client_cert"


def test_a_failed_verification_is_refused_even_with_a_DN(api):
    status, body = api.call("GET", f"/v1/blobs/{BLOB_ID}", verify="FAILED:self signed")
    assert status == 403 and body["code"] == "client_cert"


def test_ping_needs_no_authorization_and_touches_nothing(api):
    status, body = api.call("GET", "/v1/ping")
    assert status == 200 and body == {"ok": True, "api": 1}


def test_an_unknown_client_is_denied_by_default(api):
    api.store_blob()
    status, body = api.call("GET", f"/v1/blobs/{BLOB_ID}", client="stranger")
    assert status == 403 and body["code"] == "forbidden"


def test_a_known_client_without_the_action_is_denied(api):
    status, body = api.call("POST", "/v1/roll-now", body=b"{}", client="agent")
    assert status == 403 and body["code"] == "forbidden"


def test_authorization_is_re_read_per_request(api):
    """`api_pki.sh revoke` and an edit to clients.json must bite on the NEXT call, not at restart."""
    api.store_blob()
    assert api.call("GET", f"/v1/blobs/{BLOB_ID}")[0] == 200
    api.clients.write_text(json.dumps({**CLIENTS, "agent": []}))
    assert api.call("GET", f"/v1/blobs/{BLOB_ID}")[0] == 403


def test_an_unparseable_clients_file_denies_everything(api):
    """Fail CLOSED: an authorization table we cannot read must never mean 'allow'."""
    api.store_blob()
    api.clients.write_text("{ this is not json")
    assert api.call("GET", f"/v1/blobs/{BLOB_ID}")[0] == 403


# -- blobs -------------------------------------------------------------------------------------

def test_a_blob_is_opaque_bytes(api):
    """It is NOT a tar, and that is deliberate — the platform must not care what a payload is."""
    status, body = api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=BLOB,
                            extra={"X-Content-SHA256": artifact_store.digest(BLOB)})
    assert status == 201 and body["stored"] is True
    assert artifact_store.load(api.root, BLOB_ID) == BLOB


def test_a_digest_mismatch_is_refused(api):
    status, body = api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=b"different",
                            extra={"X-Content-SHA256": artifact_store.digest(BLOB)})
    assert status == 422 and body["code"] == "validation"
    assert artifact_store.load(api.root, BLOB_ID) is None


def test_the_same_blob_twice_is_idempotent(api):
    sha = artifact_store.digest(BLOB)
    api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=BLOB, extra={"X-Content-SHA256": sha})
    status, body = api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=BLOB, extra={"X-Content-SHA256": sha})
    assert status == 201 and body["stored"] is False


def test_a_different_body_under_the_same_id_is_first_publisher_wins(api):
    """A blob id addresses the build INPUTS, and a rebuild is not byte-reproducible — so the same id
    with different bytes is two builds of one tree, not a conflict. The store keeps the first, and
    the reply names the digest of what is STORED, which is what the submission must record. (The
    first live acceptance run, 2026-09-18, refused a second submission for recording its own
    rebuild's digest instead.)"""
    api.store_blob()
    other = b"other bytes entirely"
    status, body = api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=other,
                            extra={"X-Content-SHA256": artifact_store.digest(other)})
    assert status == 201 and body["stored"] is False
    assert body["sha256"] == artifact_store.digest(BLOB)
    assert artifact_store.load(api.root, BLOB_ID) == BLOB


def test_a_missing_blob_is_404_so_the_client_knows_to_PUT(api):
    assert api.call("GET", f"/v1/blobs/{BLOB_ID}")[0] == 404


# -- limits and routing ---------------------------------------------------------------------------

def test_a_body_over_the_cap_is_refused_without_being_read(api):
    status, body = api.call("PUT", f"/v1/blobs/{BLOB_ID}", body=b"x",
                            content_length=api_server.MAX_BLOB_BYTES + 1,
                            extra={"X-Content-SHA256": artifact_store.digest(b"x")})
    assert status == 413 and body["code"] == "validation"


def test_a_missing_content_length_is_refused(api):
    status, _ = api.raw(b"POST /v1/submissions HTTP/1.1\r\nHost: a\r\n"
                        b"X-Client-Verify: SUCCESS\r\nX-Client-DN: CN=agent\r\n\r\n")
    assert status == 411


def test_an_unlisted_route_is_404(api):
    assert api.call("GET", "/v1/secrets")[0] == 404
    assert api.call("POST", "/v1/tasks")[0] == 404


# -- submissions ------------------------------------------------------------------------------

def test_a_submission_inserts_its_tasks(api):
    api.store_blob()
    status, body = api.call("POST", "/v1/submissions", body=api.envelope(n=2))
    assert status == 201 and len(body["task_ids"]) == 2 and body["replayed"] is False
    rows = api.tasks()
    assert {r["name"] for r in rows} == {"t0", "t1"}
    assert {r["state"] for r in rows} == {"queued"}
    assert {r["created_by"] for r in rows} == {"me"}


def test_a_submission_naming_an_absent_blob_is_refused(api):
    status, body = api.call("POST", "/v1/submissions", body=api.envelope())
    assert status == 422 and "not stored" in body["error"]
    assert api.tasks() == []


def test_a_submission_whose_blob_digest_disagrees_is_refused(api):
    api.store_blob()
    body = api.envelope(code_sha256="b" * 64)
    status, payload = api.call("POST", "/v1/submissions", body=body)
    assert status == 422 and api.tasks() == []


@pytest.mark.parametrize("mutate,why", [
    (lambda e: e.update(envelope_version=99), "unknown version"),
    (lambda e: e.update(created_by=""), "empty actor"),
    (lambda e: e.update(tasks=[]), "no tasks"),
    (lambda e: e["tasks"][0].update(state="running"), "a field the coordinator owns"),
    (lambda e: e["tasks"][0].pop("grp"), "missing required field"),
    (lambda e: e["tasks"][0].update(slots="lots"), "wrong type"),
    (lambda e: e["tasks"][0].update(job_manifest_json='{"run": 5}'), "bad manifest"),
])
def test_every_malformed_envelope_class_is_refused_and_writes_nothing(api, mutate, why):
    api.store_blob()
    env = json.loads(api.envelope())
    mutate(env)
    status, body = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 422, why
    assert api.tasks() == [], why


def test_a_sweep_is_all_or_nothing(api):
    """One cell tripping dedupe must leave the registry EXACTLY as it was — not a partial grid."""
    api.store_blob()
    api.call("POST", "/v1/submissions", body=api.envelope(n=1, submission_id="first"))
    before = {r["id"] for r in api.tasks()}
    env = json.loads(api.envelope(n=3, submission_id="second"))
    env["tasks"][2]["config_hash"] = "c0"        # collides with the task already queued
    status, body = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 409 and body["code"] == "duplicate"
    assert {r["id"] for r in api.tasks()} == before


def test_force_overrides_the_dedupe(api):
    api.store_blob()
    api.call("POST", "/v1/submissions", body=api.envelope(submission_id="first"))
    env = json.loads(api.envelope(submission_id="second", force=True))
    env["tasks"][0]["name"] = "t0-again"      # same config_hash, its own LANE
    status, _ = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 201 and len(api.tasks()) == 2


def _register_boxes(api):
    """One owned box and one rental, for the forced-placement hint (dispatcher inv. 4i-1)."""
    conn = registry_db.connect(api.db)
    for iid, label, source in ((-4, "gpudesktop", "owned"), (77, "runq_x", "vast")):
        conn.execute(
            "INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
            "hard_cap_at, source) VALUES (?,?,?,?,?,?,?,?)",
            (iid, label, registry_db.now_iso(), "live", 0.0, 4, "2099-01-01T00:00:00Z", source))
    conn.commit()
    conn.close()


def test_a_force_box_hint_SURVIVES_the_transport(api):
    """`resource_hint_json` is an opaque string to the envelope allowlist, so the key is carried
    verbatim onto the row — which is what the dispatcher reads to decide a task is forced."""
    api.store_blob()
    _register_boxes(api)
    hint = json.dumps({"vram_per_lane_gb": 14.0, "cores_per_lane": 22,
                       "box": "gpudesktop", "force_box": True})
    env = json.loads(api.envelope())
    env["tasks"][0]["resource_hint_json"] = hint
    status, _ = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 201
    conn = registry_db.connect(api.db)
    (stored,) = conn.execute("SELECT resource_hint_json FROM tasks").fetchone()
    assert json.loads(stored) == json.loads(hint)


@pytest.mark.parametrize("hint,why", [
    ({"force_box": True}, "requires --box"),
    ({"box": "gpudesktop", "force_box": True, "colocate": "g:1"}, "mutually exclusive"),
    ({"box": "typo", "force_box": True}, "no registered box"),
    ({"box": "runq_x", "force_box": True}, "not an owned box"),
    ({"box": "gpudesktop", "force_box": "true"}, "must be a boolean"),
])
def test_a_bad_force_box_hint_is_refused_SERVER_side_and_writes_nothing(api, hint, why):
    """The client's own check is not a trust boundary: the coordinator re-validates against ITS
    registry, and one bad cell refuses the whole submission (inv. 15)."""
    api.store_blob()
    _register_boxes(api)
    env = json.loads(api.envelope(n=2))
    env["tasks"][1]["resource_hint_json"] = json.dumps(hint)
    status, body = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 422 and body["code"] == "validation" and why in body["error"], body
    assert api.tasks() == []


def test_an_ordinary_hint_is_not_inspected(api):
    """A hint without the key is stored exactly as before — even a box the registry never heard of
    (plain `--box` holds and says so; it was never refused at submission)."""
    api.store_blob()
    env = json.loads(api.envelope())
    env["tasks"][0]["resource_hint_json"] = json.dumps({"box": "not-registered"})
    status, _ = api.call("POST", "/v1/submissions", body=json.dumps(env).encode())
    assert status == 201 and len(api.tasks()) == 1


def test_re_using_a_LANE_name_is_a_409_not_a_500(api):
    """`UNIQUE(grp, name)` is a client error. It first surfaced as a 500, which would have read as
    'the coordinator is broken' for what is simply a name already in use."""
    api.store_blob()
    api.call("POST", "/v1/submissions", body=api.envelope(submission_id="first"))
    status, body = api.call("POST", "/v1/submissions",
                            body=api.envelope(submission_id="second", force=True))
    assert status == 409 and body["code"] == "duplicate"
    assert len(api.tasks()) == 1


def test_a_replayed_submission_returns_the_SAME_task_ids(api):
    """A client that lost the response to a timeout retries safely (inv. 17)."""
    api.store_blob()
    _, first = api.call("POST", "/v1/submissions", body=api.envelope(n=2))
    status, again = api.call("POST", "/v1/submissions", body=api.envelope(n=2))
    assert status == 201
    assert again["replayed"] is True and again["task_ids"] == first["task_ids"]
    assert len(api.tasks()) == 2


def test_a_replay_from_a_DIFFERENT_client_is_a_conflict(api):
    api.store_blob()
    api.call("POST", "/v1/submissions", body=api.envelope())
    status, body = api.call("POST", "/v1/submissions", body=api.envelope(), client="operator")
    assert status == 409 and body["code"] == "duplicate"


# -- cancel -------------------------------------------------------------------------------------

def test_cancel_own_cancels_a_task_this_client_submitted(api):
    api.store_blob()
    _, body = api.call("POST", "/v1/submissions", body=api.envelope())
    tid = body["task_ids"][0]
    status, _ = api.call("POST", f"/v1/tasks/{tid}/cancel",
                         body=json.dumps({"reason": "changed my mind"}).encode())
    assert status == 200
    assert [r["state"] for r in api.tasks()] == ["cancelled"]


def test_cancel_own_may_NOT_cancel_another_clients_task(api):
    api.store_blob()
    _, body = api.call("POST", "/v1/submissions", body=api.envelope(), client="operator")
    tid = body["task_ids"][0]
    status, payload = api.call("POST", f"/v1/tasks/{tid}/cancel", client="agent",
                               body=json.dumps({"reason": "not mine"}).encode())
    assert status == 403 and payload["code"] == "forbidden"
    assert [r["state"] for r in api.tasks()] == ["queued"]


def test_cancel_any_may(api):
    api.store_blob()
    _, body = api.call("POST", "/v1/submissions", body=api.envelope(), client="agent")
    tid = body["task_ids"][0]
    status, _ = api.call("POST", f"/v1/tasks/{tid}/cancel", client="operator",
                         body=json.dumps({"reason": "operator override"}).encode())
    assert status == 200


def test_cancel_requires_a_reason(api):
    api.store_blob()
    _, body = api.call("POST", "/v1/submissions", body=api.envelope())
    tid = body["task_ids"][0]
    status, payload = api.call("POST", f"/v1/tasks/{tid}/cancel", body=b"{}")
    assert status == 422 and payload["code"] == "validation"


# -- M2: operator writers, none of which ssh anywhere -------------------------------------------

def test_a_box_probe_writes_the_one_shot_request_the_dispatcher_consumes(api):
    conn = registry_db.connect(api.db)
    conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
                 "hard_cap_at) VALUES (-2,'desktop','now','live',0.0,4,'later')")
    conn.commit()
    status, _ = api.call("POST", "/v1/boxes/desktop/probe", body=b"{}")
    assert status == 200
    row = conn.execute("SELECT value FROM settings WHERE key='probe_request_i-2'").fetchone()
    assert row is not None and "agent" in row[0]


def test_a_box_route_for_an_unknown_box_is_404(api):
    assert api.call("POST", "/v1/boxes/nope/probe", body=b"{}")[0] == 404


def test_pause_needs_the_pause_action(api):
    conn = registry_db.connect(api.db)
    conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
                 "hard_cap_at) VALUES (-2,'desktop','now','live',0.0,4,'later')")
    conn.commit()
    assert api.call("POST", "/v1/boxes/desktop/pause", body=b"{}", client="agent")[0] == 403
    assert api.call("POST", "/v1/boxes/desktop/pause", body=b"{}", client="operator")[0] == 200


# -- M2: registering a box -----------------------------------------------------------------------

def test_registering_a_box_creates_an_owned_row(api):
    status, body = api.call("POST", "/v1/boxes", client="operator",
                            body=json.dumps({"label": "gpulaptop", "host": "gpulaptop",
                                             "port": 2222, "slots": 16,
                                             "gpu_name": "RTX 4080"}).encode())
    assert status == 201 and body["instance"] < 0
    row = registry_db.connect(api.db).execute(
        "SELECT label, ssh_host, ssh_port, slots_total, source, state, dph_usd "
        "FROM instances WHERE id=?", (body["instance"],)).fetchone()
    assert tuple(row) == ("gpulaptop", "gpulaptop", 2222, 16, "owned", "live", 0.0)


def test_re_registering_updates_in_place(api):
    first = api.call("POST", "/v1/boxes", client="operator",
                     body=json.dumps({"label": "desktop", "host": "old", "port": 2222}).encode())[1]
    second = api.call("POST", "/v1/boxes", client="operator",
                      body=json.dumps({"label": "desktop", "host": "westdesktop",
                                       "port": 2222, "slots": 4}).encode())[1]
    assert first["instance"] == second["instance"]
    row = registry_db.connect(api.db).execute(
        "SELECT ssh_host, slots_total FROM instances WHERE id=?", (first["instance"],)).fetchone()
    assert tuple(row) == ("westdesktop", 4)


def test_registering_sets_the_pack_preference(api):
    """`--prefer` over the API writes the same settings row the local transport does (inv. 4b')."""
    _, body = api.call("POST", "/v1/boxes", client="operator",
                       body=json.dumps({"label": "gpudesktop", "host": "gpudesktop",
                                        "port": 2222, "prefer": -1}).encode())
    row = registry_db.connect(api.db).execute(
        "SELECT value FROM settings WHERE key=?", (f"box_preference_i{body['instance']}",)).fetchone()
    assert row is not None and json.loads(row[0]) == -1


def test_registering_without_prefer_leaves_it_alone(api):
    _, body = api.call("POST", "/v1/boxes", client="operator",
                       body=json.dumps({"label": "b", "host": "b", "prefer": 2}).encode())
    api.call("POST", "/v1/boxes", client="operator",
             body=json.dumps({"label": "b", "host": "b2"}).encode())
    row = registry_db.connect(api.db).execute(
        "SELECT value FROM settings WHERE key=?", (f"box_preference_i{body['instance']}",)).fetchone()
    assert json.loads(row[0]) == 2


@pytest.mark.parametrize("prefer", ["1", 1.5, True])
def test_a_non_integer_prefer_is_refused(api, prefer):
    status, _ = api.call("POST", "/v1/boxes", client="operator",
                         body=json.dumps({"label": "b", "host": "b", "prefer": prefer}).encode())
    assert status == 422


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "172.17.0.1", "host.docker.internal"])
def test_an_address_that_means_THIS_HOST_is_refused(api, host):
    """The 2026-09-17 cutover bug in one line: owned box -2 was registered at the DESKTOP's docker
    gateway, so from tower the coordinator ssh'd into ITSELF and every probe timed out."""
    status, body = api.call("POST", "/v1/boxes", client="operator",
                            body=json.dumps({"label": "desktop", "host": host}).encode())
    assert status == 422 and "this host" in body["error"]


def test_registering_needs_the_register_box_action(api):
    status, _ = api.call("POST", "/v1/boxes", client="agent",
                         body=json.dumps({"label": "x", "host": "y"}).encode())
    assert status == 403


def test_a_drain_request_is_accepted_for_an_operator(api):
    conn = registry_db.connect(api.db)
    conn.execute("INSERT INTO instances(id, label, created_at, state, dph_usd, slots_total, "
                 "hard_cap_at) VALUES (-3,'tower','now','live',0.0,16,'later')")
    conn.commit()
    assert api.call("POST", "/v1/boxes/tower/drain", body=b"{}", client="operator")[0] == 200
    assert conn.execute("SELECT 1 FROM settings WHERE key='drain_request_i-3'").fetchone()


def test_every_row_carries_its_artifact(api):
    """⛔ THE LINK BETWEEN A TASK AND ITS CODE. The dispatcher ships whatever `code_blob` names and
    re-checks it against `code_sha256`; without them a task fails at ship with "task carries no ship
    artifact". That was the FIRST live-acceptance failure, 2026-09-18 — no unit test read the columns."""
    api.store_blob()
    _, body = api.call("POST", "/v1/submissions", body=api.envelope(n=2))
    conn = registry_db.connect(api.db)
    rows = conn.execute("SELECT code_blob, code_sha256 FROM tasks").fetchall()
    assert len(rows) == 2
    assert {tuple(r) for r in rows} == {(BLOB_ID, artifact_store.digest(BLOB))}


# -- task-dispatcher 20e-1: re-registration changes only what it was told to ---------------------

def _desktop(api):
    body = {"label": "desktop", "host": "westdesktop", "port": 2222, "slots": 12,
            "gpu_name": "NVIDIA GeForce RTX 3070 Ti"}
    return api.call("POST", "/v1/boxes", client="operator", body=json.dumps(body).encode())[1]


def _box_row(api, iid):
    return dict(zip(
        ("ssh_host", "ssh_port", "slots_total", "gpu_name", "state"),
        registry_db.connect(api.db).execute(
            "SELECT ssh_host, ssh_port, slots_total, gpu_name, state FROM instances WHERE id=?",
            (iid,)).fetchone()))


def test_re_registering_a_PAUSED_box_keeps_it_paused(api):
    """Measured 2026-10-03: `desktop`, on hold for a reinstall, was re-pointed at its new address
    and was `live` — taking work — within the same poll, its pause record left behind."""
    iid = _desktop(api)["instance"]
    conn = registry_db.connect(api.db)
    conn.execute("UPDATE instances SET state='paused' WHERE id=?", (iid,))
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)",
                 (f"pause_i{iid}", json.dumps({"mode": "hold", "at": "2026-10-03T18:38:20Z"})))
    conn.commit()
    _, body = api.call("POST", "/v1/boxes", client="operator", body=json.dumps(
        {"label": "desktop", "host": "old-desktop.lan"}).encode())
    assert body["kept_paused"] is True and body["state"] == "paused"
    row = _box_row(api, iid)
    assert row["state"] == "paused" and row["ssh_host"] == "old-desktop.lan"
    assert registry_db.connect(api.db).execute(
        "SELECT 1 FROM settings WHERE key=?", (f"pause_i{iid}",)).fetchone()


def test_re_registering_keeps_every_field_it_was_not_given(api):
    """An omitted field used to reset: port 22, 1 slot, no GPU name — and a box with no GPU name is
    one the GPU-loss alert stops watching."""
    iid = _desktop(api)["instance"]
    api.call("POST", "/v1/boxes", client="operator", body=json.dumps(
        {"label": "desktop", "host": "old-desktop.lan", "port": None, "slots": None,
         "gpu_name": None}).encode())
    assert _box_row(api, iid) == {
        "ssh_host": "old-desktop.lan", "ssh_port": 2222, "slots_total": 12,
        "gpu_name": "NVIDIA GeForce RTX 3070 Ti", "state": "live"}


def test_re_registering_writes_the_one_field_it_was_given(api):
    iid = _desktop(api)["instance"]
    api.call("POST", "/v1/boxes", client="operator", body=json.dumps(
        {"label": "desktop", "host": "westdesktop", "slots": 16}).encode())
    row = _box_row(api, iid)
    assert (row["slots_total"], row["ssh_port"]) == (16, 2222)


@pytest.mark.parametrize("field,value", [("port", 0), ("port", 70000), ("port", "2222"),
                                         ("port", True), ("slots", 0), ("slots", 1.5),
                                         ("slots", "4"), ("gpu_name", ""), ("gpu_name", 3)])
def test_a_malformed_box_field_is_refused_not_coerced(api, field, value):
    status, _ = api.call("POST", "/v1/boxes", client="operator", body=json.dumps(
        {"label": "b", "host": "b", field: value}).encode())
    assert status == 422
