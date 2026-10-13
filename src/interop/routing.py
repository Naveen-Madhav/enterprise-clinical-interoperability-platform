"""Configured FHIR fan-out with per-destination read-back evidence."""

from .errors import InterfaceError
from .fhir import digest


class Router:
    def __init__(self, clients: dict, rules: dict):
        self.clients, self.rules = clients, rules
        if not clients or any(
            not names or any(n not in clients for n in names) for names in rules.values()
        ):
            raise ValueError("Routing rules must resolve to registered endpoints")

    def routes_for(self, bundle: dict) -> list[str]:
        kinds = {e["resource"]["resourceType"] for e in bundle["entry"]}
        # More specific workflows take precedence over shared Patient entries.
        for kind in (
            "ImagingStudy",
            "DocumentReference",
            "DiagnosticReport",
            "ServiceRequest",
            "Appointment",
            "Encounter",
        ):
            if kind in kinds:
                if kind not in self.rules:
                    raise InterfaceError("routing.missing-route", ack="AR")
                return list(dict.fromkeys(self.rules[kind]))
        raise InterfaceError("routing.missing-route", ack="AR")

    def deliver(self, bundle: dict, routes: list[str]) -> dict:
        received = {}
        for name in routes:
            if name not in self.clients:
                raise InterfaceError("routing.destination-removed")
            snapshots = self.clients[name].deliver(bundle)
            for entry in bundle["entry"]:
                path = entry["request"]["url"]
                actual = snapshots.get(path)
                if actual is None:
                    raise InterfaceError("reconcile.missing-resource", retryable=True)
                clinical = {k: v for k, v in actual.items() if k != "meta"}
                if digest(clinical) != digest(entry["resource"]):
                    raise InterfaceError("reconcile.resource-mismatch")
                received[f"{name}|{path}"] = clinical
        return received
