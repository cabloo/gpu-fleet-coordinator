"""`runq`'s API transport — build locally, push to the coordinator (remote-submit spec, M1).

Two rules shape this file.

**The token is never sent to an unverified server** (inv. 6) — here, the client certificate. The TLS
context trusts ONLY `COORD_API_CA`, checks the hostname, and requires TLS 1.3; a verification failure
raises before any request line is written, so a machine impersonating the coordinator learns nothing.

**There is no fallback** (inv. 10). Under `RUNQ_TRANSPORT=api` this module is the only writer; if the
coordinator cannot be reached, the command FAILS rather than quietly writing the data root, because a
silent fallback is what keeps the ACL alive and makes the seal depend on host permissions nobody
watches.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from http.client import HTTPSConnection
from urllib.parse import urlsplit

#: Exit codes, extending `runq`'s (0 ok · 2 validation · 3 duplicate · 4 illegal transition · 5 build).
EXIT_TRANSPORT = 6     # never reached the coordinator: DNS, connect, TLS verification, timeout
EXIT_UNAUTHORIZED = 7  # reached it and was refused: no/!valid certificate, or not allowed to

_CODE_TO_EXIT = {
    "validation": 2, "duplicate": 3, "illegal_transition": 4, "blob_conflict": 2,
    "forbidden": EXIT_UNAUTHORIZED, "client_cert": EXIT_UNAUTHORIZED,
    "rate_limited": EXIT_UNAUTHORIZED, "not_found": 2, "server_error": 1,
}


class TransportError(Exception):
    """Could not reach the coordinator at all. Distinct from a refusal BY it (inv. output contract)."""


class ApiRefusal(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    @property
    def exit_code(self) -> int:
        return _CODE_TO_EXIT.get(self.code, 1)


def enabled() -> bool:
    """EXPLICIT (inv. 10): the transport is chosen by configuration, never inferred from whether the
    data root happens to be writable — inference is what would silently resurrect the direct path."""
    return os.environ.get("RUNQ_TRANSPORT", "local").strip().lower() == "api"


class ApiClient:
    def __init__(self, url=None, ca=None, cert=None, key=None, timeout=120.0):
        self.url = url or os.environ.get("COORD_API_URL", "")
        self.ca = ca or os.environ.get("COORD_API_CA", "")
        self.cert = cert or os.environ.get("COORD_API_CERT", "")
        self.key = key or os.environ.get("COORD_API_KEY", "")
        self.timeout = timeout
        missing = [n for n, v in (("COORD_API_URL", self.url), ("COORD_API_CA", self.ca),
                                  ("COORD_API_CERT", self.cert), ("COORD_API_KEY", self.key)) if not v]
        if missing:
            raise TransportError(
                "RUNQ_TRANSPORT=api but " + ", ".join(missing) + " is unset. The client needs the "
                "coordinator's URL, the CA that signed its certificate, and its own keypair.")
        # A set-but-absent file would otherwise surface as "cannot reach https://…: [Errno 2]" — a
        # networking message for an enrolment problem, which sends the reader to the wrong fix.
        absent = [f"{n}={p}" for n, p in (("COORD_API_CA", self.ca), ("COORD_API_CERT", self.cert),
                                          ("COORD_API_KEY", self.key)) if not os.path.isfile(p)]
        if absent:
            raise TransportError(
                "this client is not enrolled: " + ", ".join(absent) + " not found. Enrol with "
                "`fleet/coordinator/api_pki.sh csr <name>` and have the coordinator's host "
                "sign it (see host_setup.sh step 2c).")

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=self.ca)   # ONLY our CA — the system store is not loaded
        ctx.load_cert_chain(certfile=self.cert, keyfile=self.key)
        return ctx

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        parts = urlsplit(self.url)
        try:
            conn = HTTPSConnection(parts.hostname, parts.port or 443,
                                   timeout=self.timeout, context=self._context())
            hdrs = dict(headers or {})
            hdrs["Content-Length"] = str(len(body or b""))
            conn.request(method, path, body=body or b"", headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            conn.close()
        except ssl.SSLError as e:
            raise TransportError(f"TLS failure talking to {self.url}: {e}") from None
        except OSError as e:
            raise TransportError(f"cannot reach {self.url}: {e}") from None
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"ok": False, "code": "server_error",
                       "error": f"non-JSON reply ({status}): {raw[:200]!r}"}
        if status >= 400 or not payload.get("ok", False):
            raise ApiRefusal(status, str(payload.get("code") or "server_error"),
                             str(payload.get("error") or f"HTTP {status}"))
        return status, payload

    # -- the calls `runq` makes ----------------------------------------------------------------
    def blob_digest(self, blob_id: str) -> str | None:
        """The sha256 of the blob the coordinator HOLDS under this id, or None if it holds none."""
        try:
            return self.request("GET", f"/v1/blobs/{blob_id}")[1].get("sha256")
        except ApiRefusal as e:
            if e.status == 404:
                return None
            raise

    def has_blob(self, blob_id: str) -> bool:
        return self.blob_digest(blob_id) is not None

    def put_blob(self, blob_id: str, data: bytes, sha256: str) -> dict:
        _, payload = self.request("PUT", f"/v1/blobs/{blob_id}", body=data,
                                  headers={"X-Content-SHA256": sha256,
                                           "Content-Type": "application/octet-stream"})
        return payload

    def submit(self, envelope: dict) -> dict:
        _, payload = self.request("POST", "/v1/submissions",
                                  body=json.dumps(envelope).encode(),
                                  headers={"Content-Type": "application/json"})
        return payload

    def cancel(self, task_id: str, reason: str, by: str) -> dict:
        _, payload = self.request("POST", f"/v1/tasks/{task_id}/cancel",
                                  body=json.dumps({"reason": reason, "by": by}).encode(),
                                  headers={"Content-Type": "application/json"})
        return payload

    def box(self, box: str, verb: str) -> dict:
        _, payload = self.request("POST", f"/v1/boxes/{box}/{verb}", body=b"{}",
                                  headers={"Content-Type": "application/json"})
        return payload

    def register_box(self, **fields) -> dict:
        _, payload = self.request("POST", "/v1/boxes", body=json.dumps(fields).encode(),
                                  headers={"Content-Type": "application/json"})
        return payload

    def roll_now(self) -> dict:
        _, payload = self.request("POST", "/v1/roll-now", body=b"{}",
                                  headers={"Content-Type": "application/json"})
        return payload


def run(fn, *args, **kwargs) -> int:
    """Call an `ApiClient` method, turning both failure classes into `runq`'s exit codes and a line
    on stderr. Keeps the distinction the output contract insists on: 6 = never reached it, 7 = it
    refused you, 2/3/4 = it understood you and said no."""
    try:
        fn(*args, **kwargs)
        return 0
    except TransportError as e:
        print(f"runq: {e}", file=sys.stderr)
        return EXIT_TRANSPORT
    except ApiRefusal as e:
        print(f"runq: coordinator refused ({e.status} {e.code}): {e.message}", file=sys.stderr)
        return e.exit_code
