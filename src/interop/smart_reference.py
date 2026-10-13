"""Synthetic established-user context for exercising the SMART client on loopback."""

import re
import secrets
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse

from .reference import ReferenceHandler, ReferenceServer
from .smart import READ_SCOPES, challenge


class SmartServer(ReferenceServer):
    def __init__(self, patient_id: str, port=0):
        self.patient_id = patient_id
        self.codes = {}
        self.redirect_uri = "http://127.0.0.1:8765/callback"
        super().__init__(port, handler=SmartHandler)


class SmartHandler(ReferenceHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/fhir/.well-known/smart-configuration":
            self.reply(
                200,
                {
                    "authorization_endpoint": self.server.origin + "/oauth/authorize",
                    "token_endpoint": self.server.origin + "/oauth/token",
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                    "grant_types_supported": ["authorization_code", "client_credentials"],
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["launch", "launch/patient", *READ_SCOPES],
                    "capabilities": [
                        "launch-ehr",
                        "launch-standalone",
                        "client-public",
                        "context-ehr-patient",
                        "context-standalone-patient",
                        "permission-patient",
                    ],
                },
                "application/json",
            )
        elif path == "/oauth/authorize":
            self.authorize()
        else:
            super().do_GET()

    def authorize(self):
        params = parse_qs(urlparse(self.path).query)

        def one(key):
            values = params.get(key, [])
            return values[0] if len(values) == 1 else ""

        scopes = one("scope").split()
        launch_ok = ("launch" in scopes and one("launch") == "SYN-LAUNCH-001") or (
            "launch/patient" in scopes and "launch" not in params
        )
        if (
            one("response_type") != "code"
            or one("client_id") != "portfolio-viewer"
            or one("redirect_uri") != self.server.redirect_uri
            or one("aud") != self.server.origin + "/fhir"
            or one("code_challenge_method") != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", one("code_challenge"))
            or not re.fullmatch(r"[A-Za-z0-9_-]{22,}", one("state"))
            or not launch_ok
            or not set(READ_SCOPES) <= set(scopes)
            or not set(scopes) <= {"launch", "launch/patient", *READ_SCOPES}
        ):
            self.reply(400, {"error": "invalid_request"}, "application/json")
            return
        code = secrets.token_urlsafe(32)
        with self.server.lock:
            self.server.codes[code] = {
                "client_id": one("client_id"),
                "redirect_uri": one("redirect_uri"),
                "challenge": one("code_challenge"),
                "expires": time.monotonic() + 60,
                "scope": " ".join(scopes),
            }
        self.send_response(302)
        self.send_header(
            "Location",
            self.server.redirect_uri + "?" + urlencode({"code": code, "state": one("state")}),
        )
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/oauth/token" or self.headers.get("Authorization", ""):
            super().do_POST()
            return
        try:
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("content type")
            params = parse_qs(self.body().decode())
            if any(len(value) != 1 for value in params.values()):
                raise ValueError("repeated parameters")
            params = {key: value[0] for key, value in params.items()}
            with self.server.lock:
                record = self.server.codes.pop(params.get("code", ""), None)
            if (
                params.get("grant_type") != "authorization_code"
                or not record
                or record["expires"] <= time.monotonic()
                or params.get("client_id") != record["client_id"]
                or params.get("redirect_uri") != record["redirect_uri"]
                or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", params.get("code_verifier", ""))
                or not secrets.compare_digest(
                    challenge(params["code_verifier"]), record["challenge"]
                )
            ):
                raise ValueError("invalid grant")
            grant = self.server.issue_token(record["scope"])
            grant["patient"] = self.server.patient_id
            with self.server.lock:
                self.server.tokens[grant["access_token"]]["patient"] = self.server.patient_id
            self.reply(200, grant, "application/json")
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "invalid_grant"}, "application/json")

    def authorized(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        with self.server.lock:
            grant = self.server.tokens.get(token)
            if not grant or grant["expires"] <= time.monotonic() or "patient" not in grant:
                return super().authorized()
            path = urlparse(self.path).path.removeprefix("/fhir/")
            kind = path.split("/")[0]
            resource = self.server.resources.get(path)
            patient = grant["patient"]
            belongs = resource and (
                (kind == "Patient" and resource.get("id") == patient)
                or resource.get("subject", {}).get("reference") == f"Patient/{patient}"
            )
            if self.command == "GET" and f"patient/{kind}.r" in grant["scope"].split() and belongs:
                return True
        self.outcome(403, "forbidden")
        return False


@contextmanager
def smart_peer(patient_id: str):
    with SmartServer(patient_id) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=10)
