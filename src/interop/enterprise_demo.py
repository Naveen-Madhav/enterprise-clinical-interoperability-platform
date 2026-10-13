"""One synthetic patient journey through all implemented interoperability boundaries."""

import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

import httpx
from pydicom import dcmread

from .artifacts import publish_document, publish_image
from .ccda import build_note
from .dicom import DicomClient, dataset_digest, storage_peer, synthetic_image
from .fhir import transform
from .hl7 import parse
from .http import ClientCredentials, FhirClient
from .ledger import Ledger
from .mllp import listening, send
from .pipeline import Bridge
from .reference import reference_peer
from .routing import Router
from .smart import READ_SCOPES, SmartClient
from .smart_reference import smart_peer


def run_platform_demo(samples=Path("samples"), routes_file=Path("config/routes.json")) -> dict:
    admission = transform(parse((samples / "adt_a01.hl7").read_text()))
    patient = admission["entry"][0]["resource"]
    with ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        peers = {"repository": stack.enter_context(smart_peer(patient["id"]))}
        peers.update(
            {
                name: stack.enter_context(reference_peer())
                for name in ("ris", "scheduler", "external-results")
            }
        )
        client = stack.enter_context(
            httpx.Client(timeout=8, trust_env=False, follow_redirects=False)
        )
        clients = {}
        for name, peer in peers.items():
            tokens = ClientCredentials(
                peer.origin + "/oauth/token", "legacy-bridge", peer.secret, "bridge.write", client
            )
            clients[name] = FhirClient(peer.origin + "/fhir", tokens, client)
        ledger = Ledger(str(directory / "transactions.sqlite"))
        stack.callback(ledger.close)
        router = Router(clients, json.loads(routes_file.read_text()))
        bridge = Bridge(ledger, None, router=router, retry_base=0)
        peers["ris"].fhir_failures = 1
        listener = stack.enter_context(listening(bridge))
        for filename in ("adt_a01.hl7", "orm_o01.hl7", "siu_s12.hl7", "oru_r01.hl7"):
            raw = (samples / filename).read_text()
            message = parse(raw)
            ledger.record_source(message.source_key, message.raw)
            if "MSA|AA|" not in send(*listener.server_address, raw):
                raise RuntimeError("A supported workflow did not receive a durable acceptance")
        while bridge.dispatch_one():
            pass
        order = transform(parse((samples / "orm_o01.hl7").read_text()))["entry"][1]["resource"]
        image = synthetic_image()
        pacs = stack.enter_context(storage_peer(directory / "pacs"))
        pacs.failures = 1
        imaging_client = DicomClient(pacs.destination)
        echo = imaging_client.echo()
        imaging = publish_image(image, imaging_client, bridge, patient, order, retry_base=0)
        stored = dcmread(directory / "pacs" / f"{image.SOPInstanceUID}.dcm")
        if dataset_digest(stored) != dataset_digest(image):
            raise RuntimeError("PACS stored content did not match the sent instance")
        results = [
            e["resource"] for e in transform(parse((samples / "oru_r01.hl7").read_text()))["entry"]
        ]
        document = build_note(results[0], results[-1], results[1:-1])
        document_status = publish_document(document, bridge, patient)
        while bridge.dispatch_one():
            pass
        primary = peers["repository"]
        smart = SmartClient(
            primary.origin + "/fhir", "portfolio-viewer", primary.redirect_uri, client
        )
        session = smart.start("SYN-LAUNCH-001")
        redirect = client.get(session.url)
        if redirect.status_code != 302:
            raise RuntimeError("SMART authorization did not produce a callback")
        grant = smart.exchange(session, redirect.headers["Location"])
        viewer = FhirClient(primary.origin + "/fhir", grant, client)
        read_types = []
        for scope in READ_SCOPES:
            kind = scope.removeprefix("patient/").removesuffix(".r")
            target = next(path for path in primary.resources if path.startswith(kind + "/"))
            resource = viewer.request("GET", "/" + target)
            if resource["resourceType"] != kind:
                raise RuntimeError("SMART read returned the wrong resource type")
            read_types.append(kind)
        reconciliation = ledger.reconcile()
        if any(r["outcome"] != "delivered" for r in reconciliation["transactions"]) or any(
            r["outcome"] != "matched" for r in reconciliation["resources"]
        ):
            raise RuntimeError("Platform reconciliation failed")
        return {
            "hl7_workflows": ["ADT^A01", "ORM^O01", "SIU^S12", "ORU^R01"],
            "health": ledger.health(),
            "destination_resource_counts": {n: len(p.resources) for n, p in peers.items()},
            "dicom": {**echo, **imaging, "stored_content_matches": True},
            "ccda": document_status,
            "smart": {"launch": "EHR", "pkce": "S256", "read_types": read_types},
            "reconciliation": {
                "delivered_transactions": len(reconciliation["transactions"]),
                "matched_destination_resources": len(reconciliation["resources"]),
            },
        }
