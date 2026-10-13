import socket
from pathlib import Path

import httpx
import pytest

from interop.errors import InterfaceError
from interop.http import ClientCredentials, FhirClient, require_secure_url
from interop.ledger import Ledger
from interop.mllp import END, START, FrameReader, listening, send
from interop.pipeline import Bridge
from interop.reference import reference_peer

RAW = (Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()


class FragmentedSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def recv(self, size):
        return next(self.chunks, b"")


def test_fragmented_and_coalesced_frames():
    reader = FrameReader(
        FragmentedSocket([START, b"MS", b"H|a\r" + END[:1], END[1:] + START + b"b" + END])
    )
    assert reader.read() == b"MSH|a\r"
    assert reader.read() == b"b"
    assert reader.read() is None


@pytest.mark.parametrize("chunks", [[b"bad"], [START + b"truncated"], [START + b"12345" + END]])
def test_bad_frames_close_without_corrupting_message(chunks):
    with pytest.raises(ValueError):
        FrameReader(FragmentedSocket(chunks), limit=4).read()


def test_real_mllp_oauth_fhir_retry_and_reconciliation(tmp_path):
    with reference_peer() as peer, httpx.Client(timeout=8, trust_env=False) as client:
        peer.fhir_failures = 1
        tokens = ClientCredentials(
            peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
        )
        destination = FhirClient(peer.origin + "/fhir", tokens, client)
        ledger = Ledger(str(tmp_path / "ledger.sqlite"))
        bridge = Bridge(ledger, destination, retry_base=0)
        with listening(bridge) as listener:
            assert "MSA|AA|SYN-ORU-001" in send(*listener.server_address, RAW)
            assert bridge.dispatch_one()
            assert ledger.health()["states"] == {"pending": 1}
            assert bridge.dispatch_one()
            assert ledger.health()["states"] == {"delivered": 1}
            assert peer.fhir_requests == 2
            assert peer.token_requests == 1
            assert len(peer.resources) == 3
            assert {r["outcome"] for r in ledger.reconcile()["resources"]} == {"matched"}
            with socket.create_connection(listener.server_address, timeout=8) as sock:
                sock.sendall(START + RAW.encode() + END + START + RAW.encode() + END)
                reader = FrameReader(sock)
                assert b"MSA|AA|" in reader.read()
                assert b"MSA|AA|" in reader.read()
        ledger.close()


def test_oauth_rejects_wrong_client_and_expired_token_refreshes():
    with reference_peer() as peer, httpx.Client(timeout=8, trust_env=False) as client:
        bad = ClientCredentials(
            peer.origin + "/oauth/token", "legacy-bridge", "incorrect", "bridge.write", client
        )
        with pytest.raises(InterfaceError, match="http.401"):
            bad.access_token()
        tokens = ClientCredentials(
            peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
        )
        token = tokens.access_token()
        peer.tokens[token]["expires"] = 0
        destination = FhirClient(peer.origin + "/fhir", tokens, client)
        with pytest.raises(InterfaceError, match="http.404"):
            destination.request("GET", "/Patient/missing")
        assert tokens.token != token
        assert peer.token_requests == 3


@pytest.mark.parametrize(
    "url",
    [
        "http://external.example/fhir",
        "https://name:secret@example.test",
        "https://example.test/fhir?token=x",
    ],
)
def test_no_plaintext_remote_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        require_secure_url(url)
