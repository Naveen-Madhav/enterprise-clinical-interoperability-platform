import json
import logging

from .errors import InterfaceError
from .fhir import digest, transform
from .hl7 import acknowledge, parse

LOGGER = logging.getLogger("interop.events")


def event(name: str, **fields):
    LOGGER.info(json.dumps({"event": name, **fields}, sort_keys=True))


class Bridge:
    def __init__(self, ledger, destination, *, router=None, max_attempts=3, retry_base=2.0):
        if max_attempts < 1 or retry_base < 0:
            raise ValueError("Invalid retry policy")
        self.ledger, self.destination = ledger, destination
        self.router = router
        self.max_attempts, self.retry_base = max_attempts, retry_base

    def receive(self, raw: str) -> str:
        try:
            message = parse(raw)
            bundle = transform(message)
            transaction_id = self.ledger.enqueue(
                message.source_key,
                message.raw,
                bundle,
                routes=self.router.routes_for(bundle) if self.router else None,
            )
            event("accepted", transaction_id=transaction_id)
            return acknowledge(raw, "AA")
        except InterfaceError as exc:
            self.ledger.reject(raw, exc.code)
            event("rejected", diagnostic=exc.code)
            return acknowledge(raw, exc.ack, exc.code)

    def dispatch_one(self) -> bool:
        row = self.ledger.claim()
        if row is None:
            return False
        bundle = json.loads(row["bundle"])
        error, snapshots = None, None
        try:
            if self.router:
                snapshots = self.router.deliver(bundle, json.loads(row["routes"]))
            else:
                snapshots = self.destination.deliver(bundle)
            for entry in [] if self.router else bundle["entry"]:
                path = entry["request"]["url"]
                actual = snapshots.get(path)
                # Server metadata is not clinical content; keep it out of equality checks.
                if actual is None:
                    raise InterfaceError("reconcile.missing-resource", retryable=True)
                actual = {k: v for k, v in actual.items() if k != "meta"}
                snapshots[path] = actual
                if digest(actual) != digest(entry["resource"]):
                    raise InterfaceError("reconcile.resource-mismatch")
        except InterfaceError as exc:
            error = exc
        except Exception:
            # An adapter defect must not strand an outbox claim or publish its exception body.
            error = InterfaceError("dispatcher.unexpected")
        state = self.ledger.finish(row, snapshots, error, self.max_attempts, self.retry_base)
        event(
            "delivery",
            transaction_id=row["id"],
            state=state,
            diagnostic=error.code if error else None,
        )
        return True
