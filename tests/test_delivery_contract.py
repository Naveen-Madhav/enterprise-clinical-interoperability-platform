import json
from pathlib import Path

import httpx
import pytest

from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse
from interop.http import ClientCredentials, FhirClient
from interop.ledger import Ledger
from interop.reference import reference_peer


def test_transaction_rejection_is_atomic():
    raw = (Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()
    bundle = transform(parse(raw))
    bundle["entry"][1]["resource"]["status"] = "invented"
    with reference_peer() as peer, httpx.Client(timeout=8, trust_env=False) as client:
        tokens = ClientCredentials(
            peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
        )
        response = client.post(
            peer.origin + "/fhir",
            json=bundle,
            headers={
                "Authorization": "Bearer " + tokens.access_token(),
                "Content-Type": "application/fhir+json",
            },
        )
        assert response.status_code == 400
        assert response.json()["resourceType"] == "OperationOutcome"
        assert peer.resources == {}


@pytest.mark.parametrize(
    "response",
    [
        {"resourceType": "OperationOutcome", "issue": []},
        {"resourceType": "Bundle", "type": "transaction-response", "entry": []},
        {
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": [{"response": {"status": "500 Internal Server Error"}}] * 3,
        },
    ],
)
def test_bad_success_body_never_counts_as_delivery(response):
    class Tokens:
        def access_token(self):
            return "synthetic-runtime-token"

    raw = (Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        destination = FhirClient("https://fhir.example.invalid", Tokens(), client)
        with pytest.raises(InterfaceError):
            destination.deliver(transform(parse(raw)))


def test_read_only_operational_connection_does_not_reset_live_claim(tmp_path):
    path = str(tmp_path / "ledger.sqlite")
    ledger = Ledger(path)
    message = parse((Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text())
    ledger.enqueue(message.source_key, message.raw, transform(message))
    assert ledger.claim() is not None
    observer = Ledger(path)
    assert observer.health()["states"] == {"delivering": 1}
    assert ledger.claim() is None
    observer.close()
    ledger.close()


def test_logs_do_not_include_source_or_access_tokens(caplog):
    from interop.pipeline import Bridge

    class BrokenAdapter:
        def deliver(self, bundle):
            raise ValueError("private body and credentials")

    caplog.set_level("INFO", logger="interop.events")
    ledger = Ledger(":memory:")
    bridge = Bridge(ledger, BrokenAdapter())
    bridge.receive((Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text())
    bridge.dispatch_one()
    data = [json.loads(r.message) for r in caplog.records if r.name == "interop.events"]
    assert data[-1]["diagnostic"] == "dispatcher.unexpected"
    assert "ExampleOnly" not in caplog.text
    assert "private" not in caplog.text
    ledger.close()


def test_advertised_individual_updates_match_capability_statement():
    from fhir.resources.capabilitystatement import CapabilityStatement

    raw = (Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()
    patient = transform(parse(raw))["entry"][0]["resource"]
    with reference_peer() as peer, httpx.Client(timeout=8, trust_env=False) as client:
        metadata = client.get(peer.origin + "/fhir/metadata").json()
        assert CapabilityStatement.parse_obj(metadata).kind == "instance"
        assert metadata["implementation"]["url"] == peer.origin + "/fhir"
        tokens = ClientCredentials(
            peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
        )
        url = peer.origin + "/fhir/Patient/" + patient["id"]
        headers = {
            "Authorization": "Bearer " + tokens.access_token(),
            "Content-Type": "application/fhir+json",
        }
        created = client.put(url, json=patient, headers=headers)
        assert created.status_code == 201 and "Last-Modified" in created.headers
        assert created.json()["meta"]["lastUpdated"]
        assert client.put(url, json=patient, headers=headers).status_code == 200
        wrong = dict(patient, id="different")
        assert client.put(url, json=wrong, headers=headers).status_code == 400
