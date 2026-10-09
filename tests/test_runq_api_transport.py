"""`runq` over the API transport — remote-submit inv. 10/11/12/17.

The load-bearing one is `test_the_data_root_is_untouched`: it is what lets the devcontainer's mount
go read-only and the ACL be revoked. Everything else here is about the client half behaving when the
coordinator says no.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fleet"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


api_client = _load("api_client", "fleet/api_client.py")


class _StubClient:
    """Stands in for the coordinator. Records what the client sent, so the tests assert on the
    ENVELOPE rather than on mocks of our own helpers."""

    def __init__(self, *, has=False, fail=None):
        self.has, self.fail = has, fail
        self.puts, self.submissions, self.cancels = [], [], []

    SERVER_SHA = "d" * 64          # what the coordinator holds — deliberately NOT the client's digest

    def blob_digest(self, blob_id):
        return self.SERVER_SHA if self.has else None

    def has_blob(self, blob_id):
        return self.has

    def put_blob(self, blob_id, data, sha256):
        self.puts.append((blob_id, len(data), sha256))
        return {"ok": True, "sha256": sha256, "stored": True}

    def submit(self, envelope):
        self.submissions.append(envelope)
        if self.fail:
            raise self.fail
        return {"ok": True, "task_ids": [f"id-{i}" for i, _ in enumerate(envelope["tasks"])],
                "replayed": False}

    def cancel(self, task_id, reason, by):
        self.cancels.append((task_id, reason, by))
        return {"ok": True}


@pytest.fixture()
def runq(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNQ_TRANSPORT", "api")
    monkeypatch.setenv("COORD_API_URL", "https://tower:8443")
    for var in ("COORD_API_CA", "COORD_API_CERT", "COORD_API_KEY"):
        f = tmp_path / var
        f.write_text("x")
        monkeypatch.setenv(var, str(f))
    monkeypatch.setenv("RUNQ_BUILD_CACHE", str(tmp_path / "buildcache"))
    return _load("runq", "fleet/runq.py")


def test_the_transport_is_explicit(runq, monkeypatch):
    """Never inferred from whether the data root happens to be writable — inference is what would
    silently resurrect the direct path (inv. 10)."""
    assert api_client.enabled() is True
    monkeypatch.setenv("RUNQ_TRANSPORT", "local")
    assert api_client.enabled() is False
    monkeypatch.delenv("RUNQ_TRANSPORT")
    assert api_client.enabled() is False


def test_missing_client_credentials_fail_LOUDLY(runq, monkeypatch):
    monkeypatch.delenv("COORD_API_CERT")
    with pytest.raises(api_client.TransportError) as e:
        api_client.ApiClient()
    assert "COORD_API_CERT" in str(e.value)


def test_an_unenrolled_client_is_told_so_not_that_the_network_is_down(runq, monkeypatch, tmp_path):
    """A credential path that is SET but absent used to surface as "cannot reach …: [Errno 2]" —
    a networking message for an enrolment problem. That is exactly what a host whose
    initialize.sh selected the API transport sees before its client has been signed."""
    monkeypatch.setenv("COORD_API_CERT", str(tmp_path / "never-signed.pem"))
    with pytest.raises(api_client.TransportError) as e:
        api_client.ApiClient()
    assert "not enrolled" in str(e.value) and "never-signed.pem" in str(e.value)


def test_a_blob_already_held_is_not_uploaded_again(runq, monkeypatch):
    """inv. 12: N cells of one sweep cost ONE transfer."""
    stub = _StubClient(has=True)
    monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
    built = {"blob_id": "b" * 24, "data": b"bytes", "sha256": "c" * 64, "format": "compiled",
             "compile_event": {"mode": "miss", "sec": 1.0, "git_sha": "abc", "packages": 2}}
    assert runq._post([{"grp": "g", "name": "n"}], built, "me", False) == 0
    assert stub.puts == []
    assert stub.submissions[0]["compile_event"]["mode"] == "hit"
    # the digest recorded is the one the COORDINATOR holds, not this client's rebuild — the second
    # live-acceptance failure, 2026-09-18, was exactly this mismatch
    assert stub.submissions[0]["code_sha256"] == _StubClient.SERVER_SHA


def test_an_absent_blob_is_uploaded_then_submitted(runq, monkeypatch):
    stub = _StubClient(has=False)
    monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
    built = {"blob_id": "b" * 24, "data": b"bytes", "sha256": "c" * 64, "format": "compiled",
             "compile_event": {"mode": "miss", "sec": 1.0, "git_sha": "abc", "packages": 2}}
    runq._post([{"grp": "g", "name": "n"}], built, "me", False)
    assert stub.puts == [("b" * 24, 5, "c" * 64)]
    env = stub.submissions[0]
    assert env["envelope_version"] == 1 and env["created_by"] == "me"
    assert env["blob_id"] == "b" * 24 and env["code_sha256"] == "c" * 64
    assert len(env["tasks"]) == 1


def test_every_submission_carries_a_FRESH_id(runq, monkeypatch):
    """The replay guard is per submission_id, so reusing one would make a second grid look like a
    retry of the first and silently queue nothing (inv. 17)."""
    stub = _StubClient(has=True)
    monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
    built = {"blob_id": "b" * 24, "data": b"x", "sha256": "c" * 64, "format": "compiled",
             "compile_event": {"mode": "hit", "sec": 0, "git_sha": "", "packages": 0}}
    runq._post([{"grp": "g", "name": "a"}], built, "me", False)
    runq._post([{"grp": "g", "name": "b"}], built, "me", False)
    ids = [e["submission_id"] for e in stub.submissions]
    assert len(set(ids)) == 2


def test_a_refusal_maps_onto_runqs_exit_codes(runq, monkeypatch):
    for code, expected in (("duplicate", 3), ("validation", 2), ("illegal_transition", 4),
                           ("forbidden", 7), ("client_cert", 7), ("rate_limited", 7)):
        stub = _StubClient(has=True, fail=api_client.ApiRefusal(409, code, "no"))
        monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
        built = {"blob_id": "b" * 24, "data": b"x", "sha256": "c" * 64, "format": "compiled",
                 "compile_event": {}}
        rc = api_client.run(runq._post, [{"grp": "g", "name": "n"}], built, "me", False)
        assert rc == expected, code


def test_being_unable_to_reach_the_coordinator_is_a_DIFFERENT_exit(runq, monkeypatch):
    """6 = never reached it, 7 = it refused you. Conflating them sends the operator to the wrong
    fix: a firewall versus a certificate."""
    def boom(*a, **k):
        raise api_client.TransportError("connection refused")
    assert api_client.run(boom) == api_client.EXIT_TRANSPORT
    assert api_client.EXIT_TRANSPORT != api_client.EXIT_UNAUTHORIZED


def test_the_build_cache_is_NOT_the_data_root(runq, tmp_path):
    """inv. 11 — the compile caches move out, which is half of why the root can go read-only."""
    assert runq._build_cache_root() == tmp_path / "buildcache"


def test_the_data_root_is_untouched(runq, tmp_path, monkeypatch):
    """⛔ THE INVARIANT THE WHOLE FEATURE EXISTS FOR (inv. 10). With the API transport selected, a
    queue writes NOTHING under experiments/ — not the registry, not a blob, not a snapshot — so the
    devcontainer's mount can be read-only and the ACL revoked."""
    registry_db = _load("registry_db", "fleet/registry_db.py")
    root = tmp_path / "experiments"
    root.mkdir()
    db = str(root / "runs.sqlite")
    registry_db.connect(db).close()
    before = {p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    stub = _StubClient(has=True)
    monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
    ns = type("NS", (), {"by": "me", "force": False})()
    rc = runq._finalize_via_api(ns, {"grp": "g", "name": "n"},
                                {"blob_id": "b" * 24, "data": b"x", "sha256": "c" * 64,
                                 "format": "compiled", "compile_event": {}})
    assert rc == 0 and stub.submissions

    after = {p.relative_to(root): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert after == before, "the API transport wrote to the data root"


def test_a_sweep_sends_ONE_envelope_for_the_whole_grid(runq, monkeypatch):
    """inv. 15: the grid is all-or-nothing, which needs it to be one submission, not N."""
    stub = _StubClient(has=True)
    monkeypatch.setattr(runq.api_client, "ApiClient", lambda *a, **k: stub)
    runq._BATCH, runq._BATCH_BUILT = [], None
    ns = type("NS", (), {"by": "me", "force": False})()
    built = {"blob_id": "b" * 24, "data": b"x", "sha256": "c" * 64, "format": "compiled",
             "compile_event": {}}
    for i in range(3):
        assert runq._finalize_via_api(ns, {"grp": "g", "name": f"c{i}"}, built) == 0
    assert stub.submissions == []                      # nothing sent yet — the batch is open
    rows, runq._BATCH = runq._BATCH, None
    api_client.run(runq._post, rows, runq._BATCH_BUILT, "me", False)
    assert len(stub.submissions) == 1
    assert [t["name"] for t in stub.submissions[0]["tasks"]] == ["c0", "c1", "c2"]
