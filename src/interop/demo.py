import tempfile
from pathlib import Path

import httpx

from .hl7 import parse
from .http import ClientCredentials, FhirClient
from .ledger import Ledger
from .mllp import listening, send
from .operations import probe
from .pipeline import Bridge
from .reference import reference_peer


def run_demo(samples=Path("samples")) -> dict:
    with tempfile.TemporaryDirectory() as directory, reference_peer() as peer:
        with httpx.Client(timeout=8, trust_env=False) as client:
            tokens = ClientCredentials(
                peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
            )
            destination = FhirClient(peer.origin + "/fhir", tokens, client)
            ledger = Ledger(str(Path(directory) / "transactions.sqlite"))
            try:
                bridge = Bridge(ledger, destination, retry_base=0)
                peer.fhir_failures = 1
                with listening(bridge) as listener:
                    for name in ("adt_a01.hl7", "oru_r01.hl7"):
                        raw = (samples / name).read_text()
                        message = parse(raw)
                        # Record sending-system evidence separately from ingress processing.
                        ledger.record_source(message.source_key, message.raw)
                        if "MSA|AA|" not in send(*listener.server_address, raw):
                            raise RuntimeError("Nominal sample was rejected")
                    replay = (samples / "oru_r01.hl7").read_text()
                    if "MSA|AA|" not in send(*listener.server_address, replay):
                        raise RuntimeError("Identical resend did not reuse the receipt")
                    malformed = replay.replace("SYN-1001^^^FICTIONAL_MRN^MR", "")
                    if "MSA|AE|" not in send(*listener.server_address, malformed):
                        raise RuntimeError("Invalid sample was accepted")
                    while bridge.dispatch_one():
                        pass
                    reconciliation = ledger.reconcile()
                    if any(r["outcome"] != "delivered" for r in reconciliation["transactions"]):
                        raise RuntimeError("Source/integration reconciliation failed")
                    if any(r["outcome"] != "matched" for r in reconciliation["resources"]):
                        raise RuntimeError("Destination reconciliation failed")
                    return {
                        "capabilities": probe(peer.origin + "/fhir", client),
                        "transport": "MLLP and HTTP loopback sockets",
                        "health": ledger.health(),
                        "reconciliation": reconciliation,
                        "fhir_transaction_requests": peer.fhir_requests,
                        "oauth_token_requests": peer.token_requests,
                        "destination_resource_count": len(peer.resources),
                    }
            finally:
                ledger.close()
