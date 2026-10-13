from pathlib import Path

import pytest

from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse
from interop.ledger import Ledger
from interop.pipeline import Bridge
from interop.routing import Router

SAMPLES = Path(__file__).parents[1] / "samples"


@pytest.mark.parametrize("name,kind", [("orm_o01", "ServiceRequest"), ("siu_s12", "Appointment")])
def test_new_profiles_validate_and_map(name, kind):
    bundle = transform(parse((SAMPLES / f"{name}.hl7").read_text()))
    resource = bundle["entry"][1]["resource"]
    assert resource["resourceType"] == kind
    if kind == "ServiceRequest":
        assert resource["intent"] == "order"
        assert resource["status"] == "active"
    else:
        assert resource["status"] == "booked"
        assert resource["start"] == "2026-06-11T10:00:00-05:00"
        assert resource["end"] == "2026-06-11T10:30:00-05:00"


def test_order_identity_must_match():
    raw = (SAMPLES / "orm_o01.hl7").read_text().replace("ORC|NW|SYN-ORDER-001", "ORC|NW|OTHER")
    with pytest.raises(InterfaceError, match="order-identity"):
        parse(raw)


def test_schedule_end_must_follow_start():
    raw = (SAMPLES / "siu_s12.hl7").read_text().replace("20260611103000", "20260611093000")
    with pytest.raises(InterfaceError, match="schedule-range"):
        parse(raw)


def test_fanout_retries_without_losing_destination_identity():
    class Destination:
        def __init__(self, failures=0):
            self.failures, self.calls = failures, 0

        def deliver(self, bundle):
            self.calls += 1
            if self.calls <= self.failures:
                raise InterfaceError("http.503", retryable=True)
            return {e["request"]["url"]: e["resource"] for e in bundle["entry"]}

    primary, secondary = Destination(), Destination(failures=1)
    router = Router(
        {"repository": primary, "ris": secondary}, {"ServiceRequest": ["repository", "ris"]}
    )
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, None, router=router, retry_base=0)
    bridge.receive((SAMPLES / "orm_o01.hl7").read_text())
    assert bridge.dispatch_one()
    assert ledger.health()["states"] == {"pending": 1}
    assert bridge.dispatch_one()
    assert primary.calls == 2 and secondary.calls == 2
    paths = {r["path"] for r in ledger.reconcile()["resources"]}
    assert len(paths) == 4
    assert {p.split("|")[0] for p in paths} == {"repository", "ris"}
    ledger.close()


def test_discharge_updates_the_same_encounter_to_finished():
    admit = transform(parse((SAMPLES / "adt_a01.hl7").read_text()))["entry"][1]["resource"]
    discharge = transform(parse((SAMPLES / "adt_a03.hl7").read_text()))["entry"][1]["resource"]
    assert discharge["id"] == admit["id"]
    assert discharge["status"] == "finished"
