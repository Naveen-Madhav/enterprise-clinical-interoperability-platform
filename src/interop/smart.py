"""SMART 2.2 public-client launch with OAuth authorization code and PKCE S256.

Contract: https://hl7.org/fhir/smart-app-launch/STU2.2/app-launch.html
No OpenID Connect scopes are requested, so this client makes no user identity claim.
"""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import InterfaceError
from .http import check_http, require_secure_url

READ_SCOPES = [
    f"patient/{kind}.r"
    for kind in ("Patient", "Encounter", "Observation", "ServiceRequest", "DiagnosticReport")
]


def challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )


@dataclass
class LaunchSession:
    url: str
    state: str
    verifier: str
    redirect_uri: str
    scopes: list[str]
    created: float
    used: bool = False


class SmartGrant:
    def __init__(self, token: str, patient_id: str, scopes: str, lifetime: int):
        self.token, self.patient_id, self.scopes = token, patient_id, scopes
        self.expires = time.monotonic() + max(0, lifetime - 10)

    def access_token(self):
        if time.monotonic() >= self.expires:
            raise InterfaceError("oauth.smart-session-expired")
        return self.token

    def invalidate(self):
        self.expires = 0


class SmartClient:
    def __init__(self, base: str, client_id: str, redirect_uri: str, client: httpx.Client):
        require_secure_url(base)
        require_secure_url(redirect_uri)
        self.base, self.client_id, self.redirect_uri = base.rstrip("/"), client_id, redirect_uri
        self.client, self.metadata = client, None

    def discover(self):
        try:
            response = self.client.get(
                self.base + "/.well-known/smart-configuration",
                headers={"Accept": "application/json"},
            )
            check_http(response)
            data = response.json()
            if "S256" not in data.get("code_challenge_methods_supported", []):
                raise InterfaceError("oauth.smart-pkce")
            for key in ("authorization_endpoint", "token_endpoint"):
                require_secure_url(data[key])
            self.metadata = data
            return data
        except httpx.RequestError as exc:
            raise InterfaceError("oauth.transport", retryable=True) from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise InterfaceError("oauth.smart-discovery") from exc

    def start(self, launch: str | None = None) -> LaunchSession:
        metadata = self.metadata or self.discover()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        scopes = ["launch" if launch else "launch/patient", *READ_SCOPES]
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "aud": self.base,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
        }
        if launch:
            params["launch"] = launch
        return LaunchSession(
            metadata["authorization_endpoint"] + "?" + urlencode(params),
            state,
            verifier,
            self.redirect_uri,
            scopes,
            time.monotonic(),
        )

    def exchange(self, session: LaunchSession, callback: str) -> SmartGrant:
        parsed, expected = urlparse(callback), urlparse(session.redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ):
            raise InterfaceError("oauth.callback-uri")
        params = parse_qs(parsed.query)
        if session.used or time.monotonic() - session.created > 300:
            raise InterfaceError("oauth.launch-session")
        if params.get("state") != [session.state]:
            raise InterfaceError("oauth.state")
        session.used = True
        if "error" in params:
            raise InterfaceError("oauth.authorization-denied")
        if len(params.get("code", [])) != 1:
            raise InterfaceError("oauth.code")
        try:
            response = self.client.post(
                self.metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": params["code"][0],
                    "redirect_uri": session.redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": session.verifier,
                },
            )
            check_http(response)
            data = response.json()
            if (
                data.get("token_type", "").lower() != "bearer"
                or not data.get("access_token")
                or not set(READ_SCOPES) <= set(data.get("scope", "").split())
                or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", data.get("patient", ""))
                or int(data.get("expires_in", 0)) <= 10
            ):
                raise InterfaceError("oauth.smart-token-contract")
            return SmartGrant(
                data["access_token"], data["patient"], data["scope"], int(data["expires_in"])
            )
        except httpx.RequestError as exc:
            raise InterfaceError("oauth.transport", retryable=True) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise InterfaceError("oauth.smart-token-contract") from exc
