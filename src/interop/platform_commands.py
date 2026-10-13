"""Artifact transfer and manual SMART launch commands for registered endpoints."""

import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

import httpx
from pydicom import dcmread

from .artifacts import publish_document, publish_image
from .ccda import build_note, validate_document
from .dicom import Destination, DicomClient, StoragePeer, synthetic_image
from .errors import InterfaceError
from .fhir import transform, validate_resource
from .hl7 import parse
from .http import FhirClient
from .ledger import Ledger
from .pipeline import Bridge
from .smart import LaunchSession, SmartClient
from .standards_assets import prepare_assets


def add_commands(commands):
    commands.add_parser(
        "prepare-standards", help="Cache checksum-pinned HL7 schema and rule assets"
    )
    generator = commands.add_parser("generate-dicom")
    generator.add_argument("--output", type=Path, default=Path("runtime/synthetic.dcm"))
    for name in ("pacs-serve", "dicom-send", "dicom-publish"):
        sub = commands.add_parser(name)
        sub.add_argument("--pacs-config", type=Path, default=Path("config/pacs.json"))
        if name == "pacs-serve":
            sub.add_argument("--directory", type=Path, default=Path("runtime/pacs"))
        else:
            sub.add_argument("file", type=Path)
        if name == "dicom-publish":
            sub.add_argument(
                "--order",
                type=Path,
                required=True,
                help="ORM profile providing patient/order identity",
            )
            sub.add_argument("--config", type=Path, default=Path("config/local.json"))
    exporter = commands.add_parser("ccda-export")
    exporter.add_argument("file", type=Path, help="ORU message")
    exporter.add_argument("--output", type=Path, default=Path("runtime/synthetic-note.xml"))
    importer = commands.add_parser("ccda-import")
    importer.add_argument("file", type=Path)
    importer.add_argument("--patient", type=Path, required=True, help="Patient resource JSON")
    importer.add_argument("--config", type=Path, default=Path("config/local.json"))
    starter = commands.add_parser("smart-start")
    starter.add_argument("--base", required=True)
    starter.add_argument("--client-id", required=True)
    starter.add_argument("--redirect-uri", required=True)
    starter.add_argument("--launch")
    starter.add_argument("--session", type=Path, default=Path("runtime/smart-session.json"))
    finish = commands.add_parser("smart-finish")
    finish.add_argument("--session", type=Path, default=Path("runtime/smart-session.json"))
    finish.add_argument(
        "--callback", required=True, help="Full callback URL from the authorization server"
    )


def write_private(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def execute_platform_command(args, api):
    if args.command == "prepare-standards":
        prepare_assets()
        return {"standards_assets": "verified and cached"}, 0
    if args.command == "generate-dicom":
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.output.exists():
            raise ValueError("Output exists")
        synthetic_image().save_as(args.output, enforce_file_format=True)
        os.chmod(args.output, 0o600)
        return {"created": str(args.output), "synthetic": True}, 0
    if args.command in {"pacs-serve", "dicom-send", "dicom-publish"}:
        destination = Destination(**json.loads(args.pacs_config.read_text()))
        if args.command == "pacs-serve":
            if (
                destination.host != "127.0.0.1"
                or destination.called_aet != "SYN_PACS"
                or destination.calling_aet != "SYN_MODALITY"
            ):
                raise ValueError("Storage peer requires the documented loopback AE registry")
            peer = StoragePeer(args.directory, destination.port)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                return {"stopped": True}, 0
            finally:
                peer.close()
        client = DicomClient(destination)
        image = dcmread(args.file)
        if args.command == "dicom-send":
            return {**client.echo(), **client.store(image)}, 0
        resources = [e["resource"] for e in transform(parse(args.order.read_text()))["entry"]]
        if len(resources) != 2 or resources[1]["resourceType"] != "ServiceRequest":
            raise InterfaceError("dicom.order-profile")
        config = json.loads(args.config.read_text())
        ledger = Ledger(config["database"])
        try:
            with api(config) as (router, _):
                bridge = Bridge(ledger, None, router=router)
                return publish_image(image, client, bridge, *resources), 0
        finally:
            ledger.close()
    if args.command == "ccda-export":
        resources = [e["resource"] for e in transform(parse(args.file.read_text()))["entry"]]
        if resources[-1]["resourceType"] != "DiagnosticReport":
            raise InterfaceError("ccda.result-profile")
        document = build_note(resources[0], resources[-1], resources[1:-1])
        validate_document(document)
        write_private(args.output, document)
        return {"created": str(args.output), "schema_and_supported_error_rules": "passed"}, 0
    if args.command == "ccda-import":
        patient = validate_resource(json.loads(args.patient.read_text()))
        if patient["resourceType"] != "Patient":
            raise InterfaceError("ccda.patient-resource")
        config = json.loads(args.config.read_text())
        ledger = Ledger(config["database"])
        try:
            with api(config) as (router, _):
                return publish_document(
                    args.file.read_bytes(), Bridge(ledger, None, router=router), patient
                ), 0
        finally:
            ledger.close()
    if args.command == "smart-start":
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            smart = SmartClient(args.base, args.client_id, args.redirect_uri, client)
            session = smart.start(args.launch)
            data = {
                "base": smart.base,
                "client_id": smart.client_id,
                "metadata": smart.metadata,
                "session": asdict(session),
            }
            write_private(args.session, json.dumps(data).encode())
            return {"authorization_url": session.url, "session_file": str(args.session)}, 0
    if args.command == "smart-finish":
        data = json.loads(args.session.read_text())
        session = LaunchSession(**data["session"])
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            smart = SmartClient(data["base"], data["client_id"], session.redirect_uri, client)
            smart.metadata = data["metadata"]
            try:
                grant = smart.exchange(session, args.callback)
                patient = FhirClient(smart.base, grant, client).request(
                    "GET", "/Patient/" + grant.patient_id
                )
                return {
                    "patient_context": grant.patient_id,
                    "resource_type": patient["resourceType"],
                    "granted_scopes": grant.scopes,
                }, 0
            finally:
                if session.used:
                    args.session.unlink()
    return None
