import httpx
import pytest

from interop.errors import InterfaceError
from interop.smart import SmartClient, challenge
from interop.smart_reference import smart_peer


def test_pkce_rfc7636_vector():
    assert (
        challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.mark.parametrize("launch", [None, "SYN-LAUNCH-001"])
def test_real_smart_discovery_launch_exchange_and_patient_read(launch):
    with (
        smart_peer("synthetic-patient") as peer,
        httpx.Client(timeout=8, trust_env=False) as client,
    ):
        peer.resources["Patient/synthetic-patient"] = {
            "resourceType": "Patient",
            "id": "synthetic-patient",
        }
        smart = SmartClient(peer.origin + "/fhir", "portfolio-viewer", peer.redirect_uri, client)
        session = smart.start(launch)
        response = client.get(session.url)
        assert response.status_code == 302
        grant = smart.exchange(session, response.headers["Location"])
        assert grant.patient_id == "synthetic-patient"
        headers = {"Authorization": "Bearer " + grant.access_token()}
        assert (
            client.get(peer.origin + "/fhir/Patient/synthetic-patient", headers=headers).status_code
            == 200
        )
        assert (
            client.get(peer.origin + "/fhir/Patient/another-patient", headers=headers).status_code
            == 403
        )
        assert client.post(peer.origin + "/fhir", json={}, headers=headers).status_code == 403
        with pytest.raises(InterfaceError, match="launch-session"):
            smart.exchange(session, response.headers["Location"])


def test_state_and_callback_binding_prevent_token_exchange():
    with (
        smart_peer("synthetic-patient") as peer,
        httpx.Client(timeout=8, trust_env=False) as client,
    ):
        smart = SmartClient(peer.origin + "/fhir", "portfolio-viewer", peer.redirect_uri, client)
        session = smart.start()
        callback = client.get(session.url).headers["Location"]
        with pytest.raises(InterfaceError, match="oauth.state"):
            smart.exchange(session, callback.replace(session.state, "wrong-state"))
        with pytest.raises(InterfaceError, match="callback-uri"):
            smart.exchange(session, callback.replace("8765", "8766"))
        assert not session.used


def test_wrong_verifier_and_code_replay_are_rejected():
    with (
        smart_peer("synthetic-patient") as peer,
        httpx.Client(timeout=8, trust_env=False) as client,
    ):
        smart = SmartClient(peer.origin + "/fhir", "portfolio-viewer", peer.redirect_uri, client)
        session = smart.start()
        callback = client.get(session.url).headers["Location"]
        session.verifier = "a" * 64
        with pytest.raises(InterfaceError, match="http.400"):
            smart.exchange(session, callback)
        assert session.used
        assert not peer.codes


def test_wrong_audience_and_unregistered_redirect_are_rejected():
    with (
        smart_peer("synthetic-patient") as peer,
        httpx.Client(timeout=8, trust_env=False) as client,
    ):
        smart = SmartClient(peer.origin + "/fhir", "portfolio-viewer", peer.redirect_uri, client)
        session = smart.start()
        assert client.get(session.url.replace("aud=", "unrecognized=", 1)).status_code == 400
        assert client.get(session.url.replace("8765", "8766")).status_code == 400
