"""Payload-free operational summaries and destination capability checks."""

import json
from collections import Counter

import httpx

from .errors import InterfaceError
from .http import check_http, require_secure_url


def summarize_logs(lines) -> dict:
    events, states, diagnostics = Counter(), Counter(), Counter()
    malformed = 0
    for line in lines:
        if len(line) > 16_384:
            malformed += 1
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict) or data.get("event") not in {
                "accepted",
                "rejected",
                "delivery",
            }:
                malformed += 1
                continue
            events[data["event"]] += 1
            if data["event"] == "delivery" and data.get("state") in {
                "pending",
                "exception",
                "delivered",
            }:
                states[data["state"]] += 1
            diagnostic = data.get("diagnostic")
            # Never repeat arbitrary input, even if a foreign log puts it in a diagnostic field.
            if (
                isinstance(diagnostic, str)
                and diagnostic.startswith(
                    (
                        "profile.",
                        "http.",
                        "fhir.",
                        "oauth.",
                        "mapping.",
                        "reconcile.",
                        "dispatcher.",
                        "source.",
                        "routing.",
                        "dicom.",
                        "ccda.",
                        "standards.",
                    )
                )
                and len(diagnostic) <= 80
                and all(c.isalnum() or c in ".-" for c in diagnostic)
            ):
                diagnostics[diagnostic] += 1
        except (ValueError, TypeError):
            malformed += 1
    return {
        "events": dict(events),
        "delivery_states": dict(states),
        "diagnostics": dict(diagnostics),
        "malformed_lines": malformed,
    }


def probe(base: str, client: httpx.Client) -> dict:
    require_secure_url(base)
    try:
        response = client.get(
            base.rstrip("/") + "/metadata", headers={"Accept": "application/fhir+json"}
        )
        check_http(response)
        data = response.json()
        if data.get("resourceType") != "CapabilityStatement" or data.get("fhirVersion") != "4.0.1":
            raise InterfaceError("fhir.capability-version")
        servers = [r for r in data.get("rest", []) if r.get("mode") == "server"]
        if not any({"code": "transaction"} in r.get("interaction", []) for r in servers):
            raise InterfaceError("fhir.capability-transaction")
        return {"reachable": True, "fhir_version": data["fhirVersion"], "transaction": True}
    except httpx.RequestError as exc:
        raise InterfaceError("fhir.transport", retryable=True) from exc
    except (ValueError, TypeError) as exc:
        raise InterfaceError("fhir.capability-shape") from exc


def healthy(summary: dict, max_age: float) -> bool:
    return (
        not summary["states"].get("exception", 0) and summary["oldest_pending_seconds"] <= max_age
    )
