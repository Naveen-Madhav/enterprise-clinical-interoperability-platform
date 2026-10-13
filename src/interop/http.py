"""OAuth 2.0 client credentials and replay-safe FHIR transaction delivery."""

import time
from urllib.parse import quote_plus, urlparse

import httpx

from .errors import InterfaceError


def require_secure_url(url: str):
    parts = urlparse(url)
    if parts.username or parts.password or parts.fragment or parts.query:
        raise ValueError("Endpoints must not contain credentials, query strings or fragments")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("HTTPS is required except for loopback reference endpoints")


def check_http(response: httpx.Response):
    if response.status_code >= 400:
        raise InterfaceError(
            f"http.{response.status_code}",
            retryable=response.status_code in {408, 429} or response.status_code >= 500,
        )
    if response.status_code >= 300:
        raise InterfaceError("http.redirect")


class ClientCredentials:
    # RFC 6749 section 4.4; this is OAuth, not SMART Backend Services authentication.
    def __init__(
        self, endpoint: str, client_id: str, secret: str, scope: str, client: httpx.Client
    ):
        require_secure_url(endpoint)
        if not secret:
            raise ValueError("BRIDGE_CLIENT_SECRET is required")
        self.endpoint, self.client_id, self.secret, self.scope = endpoint, client_id, secret, scope
        self.client = client
        self.token = None
        self.expires = 0.0

    def invalidate(self):
        self.token = None

    def access_token(self) -> str:
        if self.token and time.monotonic() < self.expires:
            return self.token
        try:
            response = self.client.post(
                self.endpoint,
                auth=(quote_plus(self.client_id, safe=""), quote_plus(self.secret, safe="")),
                data={"grant_type": "client_credentials", "scope": self.scope},
            )
            check_http(response)
            data = response.json()
            if (
                data.get("token_type", "").lower() != "bearer"
                or not data.get("access_token")
                or not set(self.scope.split()) <= set(data.get("scope", "").split())
            ):
                raise InterfaceError("oauth.token-contract")
            lifetime = int(data["expires_in"])
            if lifetime <= 0:
                raise InterfaceError("oauth.token-expiry")
            self.token = data["access_token"]
            self.expires = time.monotonic() + max(0, lifetime - 10)
            return self.token
        except (ValueError, KeyError, TypeError) as exc:
            raise InterfaceError("oauth.token-contract") from exc
        except httpx.RequestError as exc:
            raise InterfaceError("oauth.transport", retryable=True) from exc


class FhirClient:
    def __init__(self, base: str, tokens, client: httpx.Client):
        require_secure_url(base)
        self.base, self.tokens, self.client = base.rstrip("/"), tokens, client

    def request(self, method: str, path: str = "", **kwargs):
        try:
            for attempt in range(2):
                response = self.client.request(
                    method,
                    self.base + path,
                    headers={
                        "Authorization": "Bearer " + self.tokens.access_token(),
                        "Accept": "application/fhir+json",
                        "Content-Type": "application/fhir+json",
                    },
                    **kwargs,
                )
                if response.status_code == 401 and attempt == 0:
                    self.tokens.invalidate()
                    continue
                check_http(response)
                try:
                    return response.json()
                except ValueError as exc:
                    raise InterfaceError("fhir.response-json") from exc
        except httpx.RequestError as exc:
            raise InterfaceError("fhir.transport", retryable=True) from exc

    def deliver(self, bundle: dict) -> dict[str, dict]:
        response = self.request("POST", json=bundle)
        entries = response.get("entry", [])
        if (
            response.get("type") != "transaction-response"
            or response.get("resourceType") != "Bundle"
            or len(entries) != len(bundle["entry"])
        ):
            raise InterfaceError("fhir.transaction-response")
        for entry in entries:
            status = entry.get("response", {}).get("status", "")
            if status.split(" ")[0] not in {"200", "201"}:
                raise InterfaceError("fhir.entry-response")
        received = {}
        for entry in bundle["entry"]:
            path = entry["request"]["url"]
            received[path] = self.request("GET", "/" + path)
        return received
