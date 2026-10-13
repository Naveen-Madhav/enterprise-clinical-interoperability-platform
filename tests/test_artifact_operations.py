import argparse
import json
from pathlib import Path

import httpx
import pytest

from interop.artifacts import publish_image
from interop.dicom import synthetic_image
from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse
from interop.ledger import Ledger
from interop.pipeline import Bridge
from interop.platform_commands import execute_platform_command
from interop.smart import SmartClient
from interop.smart_reference import smart_peer

ROOT = Path(__file__).parents[1]


def test_failed_c_store_never_announces_available_imaging_metadata():
    class FailedStore:
        def store(self, image):
            raise InterfaceError("dicom.store.C000")

    resources = [
        e["resource"] for e in transform(parse((ROOT / "samples/orm_o01.hl7").read_text()))["entry"]
    ]
    ledger = Ledger(":memory:")
    with pytest.raises(InterfaceError):
        publish_image(
            synthetic_image(), FailedStore(), Bridge(ledger, None), *resources, retry_base=0
        )
    assert ledger.health()["states"] == {}
    assert ledger.reconcile()["transactions"] == []
    ledger.close()


def test_patient_identity_checked_before_image_transfer():
    class MustNotTransfer:
        def store(self, image):
            raise AssertionError("Identity mismatch must be caught before transfer")

    resources = [
        e["resource"] for e in transform(parse((ROOT / "samples/orm_o01.hl7").read_text()))["entry"]
    ]
    image = synthetic_image(patient_id="OTHER-PATIENT")
    ledger = Ledger(":memory:")
    with pytest.raises(InterfaceError, match="patient-or-order-mismatch"):
        publish_image(image, MustNotTransfer(), Bridge(ledger, None), *resources)
    ledger.close()


def test_ccda_export_command_validates_and_refuses_overwrite(tmp_path):
    output = tmp_path / "note.xml"
    args = argparse.Namespace(
        command="ccda-export", file=ROOT / "samples/oru_r01.hl7", output=output
    )
    report, status = execute_platform_command(args, None)
    assert status == 0 and report["schema_and_supported_error_rules"] == "passed"
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        execute_platform_command(args, None)
    assert output.read_bytes() == original


def test_smart_commands_preserve_private_state_and_consume_it(tmp_path):
    session_file = tmp_path / "smart-session.json"
    with smart_peer("synthetic-patient") as peer:
        peer.resources["Patient/synthetic-patient"] = {
            "resourceType": "Patient",
            "id": "synthetic-patient",
        }
        starter = argparse.Namespace(
            command="smart-start",
            base=peer.origin + "/fhir",
            client_id="portfolio-viewer",
            redirect_uri=peer.redirect_uri,
            launch=None,
            session=session_file,
        )
        result, status = execute_platform_command(starter, None)
        assert status == 0
        assert session_file.stat().st_mode & 0o777 == 0o600
        assert "access_token" not in session_file.read_text()
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            callback = client.get(result["authorization_url"]).headers["Location"]
        finish = argparse.Namespace(command="smart-finish", session=session_file, callback=callback)
        report, status = execute_platform_command(finish, None)
        assert status == 0 and report["patient_context"] == "synthetic-patient"
        assert not session_file.exists()
        assert "access_token" not in json.dumps(report)


def test_expired_authorization_code_is_rejected():
    with (
        smart_peer("synthetic-patient") as peer,
        httpx.Client(timeout=8, trust_env=False) as client,
    ):
        smart = SmartClient(peer.origin + "/fhir", "portfolio-viewer", peer.redirect_uri, client)
        session = smart.start()
        callback = client.get(session.url).headers["Location"]
        for record in peer.codes.values():
            record["expires"] = 0
        with pytest.raises(InterfaceError, match="http.400"):
            smart.exchange(session, callback)


def test_modified_standard_asset_is_not_accepted(tmp_path, monkeypatch):
    from interop.standards_assets import prepare_assets

    class Manifest:
        def joinpath(self, name):
            return self

        def read_text(self):
            return json.dumps(
                {"asset.xsd": {"url": "https://example.invalid/asset.xsd", "sha256": "0" * 64}}
            )

    monkeypatch.setattr("interop.standards_assets.files", lambda name: Manifest())
    (tmp_path / "asset.xsd").write_text("changed local schema")
    with pytest.raises(InterfaceError, match="standards.asset-digest"):
        prepare_assets(tmp_path)
