from pathlib import Path

import pytest

from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse
from interop.ledger import Ledger
from interop.pipeline import Bridge

RAW = (Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()


class Destination:
    def __init__(self, failures=0, retryable=True):
        self.failures, self.retryable, self.calls = failures, retryable, 0

    def deliver(self, bundle):
        self.calls += 1
        if self.calls <= self.failures:
            raise InterfaceError("http.503", retryable=self.retryable)
        return {e["request"]["url"]: e["resource"] for e in bundle["entry"]}


def test_persistence_replay_collision_and_reconciliation(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    ledger = Ledger(path)
    message = parse(RAW)
    ledger.record_source(message.source_key, message.raw)
    bridge = Bridge(ledger, Destination())
    assert "MSA|AA|" in bridge.receive(RAW)
    assert "MSA|AA|" in bridge.receive(RAW)
    assert "MSA|AR|" in bridge.receive(RAW.replace("92|", "94|"))
    ledger.close()
    ledger = Ledger(path)
    bridge = Bridge(ledger, Destination())
    assert bridge.dispatch_one()
    assert not bridge.dispatch_one()
    report = ledger.reconcile()
    assert report["transactions"][0]["outcome"] == "delivered"
    assert {r["outcome"] for r in report["resources"]} == {"matched"}
    assert ledger.health()["states"] == {"delivered": 1}
    ledger.snapshots({})
    assert {r["outcome"] for r in ledger.reconcile()["resources"]} == {"missing_destination"}
    ledger.close()


def test_retry_exhaustion_and_explicit_requeue():
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, Destination(failures=3), retry_base=0)
    bridge.receive(RAW)
    assert all(bridge.dispatch_one() for _ in range(3))
    assert ledger.health()["states"] == {"exception": 1}
    row = ledger.exception_rows()[0]
    ledger.requeue(row["id"])
    assert bridge.dispatch_one()
    assert ledger.health()["states"] == {"delivered": 1}
    ledger.close()


def test_permanent_failure_does_not_retry():
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, Destination(failures=1, retryable=False), retry_base=0)
    bridge.receive(RAW)
    bridge.dispatch_one()
    assert ledger.exception_rows()[0]["attempts"] == 1
    assert not bridge.dispatch_one()
    ledger.close()


def test_source_manifest_detects_missing_and_changed_receipts():
    ledger = Ledger(":memory:")
    message = parse(RAW)
    ledger.record_source(message.source_key, message.raw)
    assert ledger.reconcile()["transactions"][0]["outcome"] == "missing_integration"
    with pytest.raises(InterfaceError, match="collision"):
        ledger.record_source(message.source_key, message.raw + "different")
    ledger.enqueue(message.source_key, message.raw.replace("92|", "94|"), transform(message))
    assert ledger.reconcile()["transactions"][0]["outcome"] == "source_digest_mismatch"
    ledger.close()


def test_pending_older_delivery_prevents_out_of_order_put():
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, Destination(failures=1), retry_base=60)
    bridge.receive(RAW)
    bridge.receive(RAW.replace("SYN-ORU-001", "SYN-ORU-002").replace("92|", "94|"))
    bridge.dispatch_one()
    assert not bridge.dispatch_one()
    assert ledger.health()["states"] == {"pending": 2}
    ledger.close()


def test_superseded_exception_cannot_overwrite_newer_data():
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, Destination(failures=1, retryable=False))
    bridge.receive(RAW)
    bridge.dispatch_one()
    transaction_id = ledger.exception_rows()[0]["id"]
    bridge.receive(RAW.replace("SYN-ORU-001", "SYN-ORU-002").replace("92|", "94|"))
    with pytest.raises(InterfaceError, match="superseded"):
        ledger.requeue(transaction_id)
    ledger.close()
