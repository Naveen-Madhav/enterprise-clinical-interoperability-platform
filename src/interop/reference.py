"""Loopback-only OAuth/FHIR test peer, with real HTTP and atomic resource updates.

This is a deterministic interoperability lab peer, not an authorization service
or a general FHIR server. It has no user accounts and issues runtime-only tokens.
"""

import base64
import json
import secrets
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import InterfaceError
from .fhir import validate_resource


def capability_statement() -> dict:
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-10-12",
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "interaction": [{"code": "transaction"}],
                "resource": [
                    {
                        "type": kind,
                        "interaction": [{"code": "read"}, {"code": "update"}],
                        "updateCreate": True,
                        "versioning": "no-version",
                    }
                    for kind in (
                        "Patient",
                        "Encounter",
                        "Observation",
                        "DiagnosticReport",
                        "ServiceRequest",
                        "Appointment",
                        "ImagingStudy",
                        "Binary",
                        "DocumentReference",
                    )
                ],
            }
        ],
    }


class ReferenceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, handler=None):
        self.secret = secrets.token_urlsafe(32)
        self.tokens = {}
        self.resources = {}
        self.lock = threading.RLock()
        self.fhir_failures = 0
        self.fhir_requests = 0
        self.token_requests = 0
        super().__init__(("127.0.0.1", port), handler or ReferenceHandler)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def issue_token(self, scope):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = {"expires": time.monotonic() + 120, "scope": scope}
        return {"access_token": token, "token_type": "Bearer", "expires_in": 120, "scope": scope}


class ReferenceHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def reply(self, status, data, mime="application/fhir+json"):
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def outcome(self, status, code):
        self.reply(
            status,
            {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": code}]},
        )

    def authorized(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        with self.server.lock:
            grant = self.server.tokens.get(token)
            if grant and grant["expires"] > time.monotonic() and grant["scope"] == "bridge.write":
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer error="invalid_token"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def body(self):
        self.connection.settimeout(8)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise InterfaceError("http.body-length") from exc
        if not 0 < length <= 2 * 1024 * 1024:
            raise InterfaceError("http.body-length")
        result = self.rfile.read(length)
        if len(result) != length:
            raise InterfaceError("http.body-truncated")
        return result

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/fhir/metadata":
            self.reply(200, capability_statement())
        elif self.authorized():
            with self.server.lock:
                resource = self.server.resources.get(path.removeprefix("/fhir/"))
            if resource:
                self.reply(200, resource)
            else:
                self.outcome(404, "not-found")

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/oauth/token":
                self.server.token_requests += 1
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    self.reply(415, {"error": "invalid_request"}, "application/json")
                    return
                expected = (
                    "Basic "
                    + base64.b64encode(f"legacy-bridge:{self.server.secret}".encode()).decode()
                )
                if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
                    self.reply(401, {"error": "invalid_client"}, "application/json")
                    return
                params = parse_qs(self.body().decode())
                if params != {"grant_type": ["client_credentials"], "scope": ["bridge.write"]}:
                    self.reply(400, {"error": "invalid_scope"}, "application/json")
                    return
                self.reply(200, self.server.issue_token("bridge.write"), "application/json")
            elif path == "/fhir":
                if not self.authorized():
                    return
                if self.headers.get_content_type() != "application/fhir+json":
                    self.outcome(415, "not-supported")
                    return
                bundle = validate_resource(json.loads(self.body()))
                with self.server.lock:
                    self.server.fhir_requests += 1
                    if self.server.fhir_failures:
                        self.server.fhir_failures -= 1
                        self.outcome(503, "transient")
                        return
                    staged = dict(self.server.resources)
                    responses = []
                    for entry in bundle["entry"]:
                        target = entry["request"]["url"]
                        status = "200 OK" if target in staged else "201 Created"
                        staged[target] = entry["resource"]
                        responses.append({"response": {"status": status, "location": target}})
                    self.server.resources = staged
                self.reply(
                    200,
                    {"resourceType": "Bundle", "type": "transaction-response", "entry": responses},
                )
            else:
                self.outcome(404, "not-found")
        except (InterfaceError, ValueError, UnicodeError, OSError):
            self.outcome(400, "invalid")


@contextmanager
def reference_peer(port=0):
    with ReferenceServer(port) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=10)
