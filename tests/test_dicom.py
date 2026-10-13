import copy
from pathlib import Path

import pytest
from pydicom import dcmread

from interop.dicom import (
    Destination,
    DicomClient,
    dataset_digest,
    storage_peer,
    study_transaction,
    synthetic_image,
)
from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse


def test_real_echo_store_persistence_and_identical_replay(tmp_path):
    image = synthetic_image()
    with storage_peer(tmp_path) as peer:
        client = DicomClient(peer.destination)
        assert client.echo()["c_echo"] == "0x0000"
        assert client.store(image)["c_store"] == "0x0000"
        assert client.store(image)["c_store"] == "0x0000"
        stored = list(tmp_path.glob("*.dcm"))
        assert len(stored) == 1
        assert dataset_digest(dcmread(stored[0])) == dataset_digest(image)
        conflict = copy.deepcopy(image)
        conflict.PixelData = b"\x00" * 256
        with pytest.raises(InterfaceError, match="dicom.store.C000"):
            client.store(conflict)


@pytest.mark.parametrize(
    "title,calling", [("WRONG_PACS", "SYN_MODALITY"), ("SYN_PACS", "UNREGISTERED")]
)
def test_ae_title_rejection(tmp_path, title, calling):
    with storage_peer(tmp_path) as peer:
        endpoint = Destination(peer.destination.host, peer.destination.port, title, calling)
        with pytest.raises(InterfaceError, match="association"):
            DicomClient(endpoint).echo()


def test_store_failure_is_not_reported_as_success(tmp_path):
    with storage_peer(tmp_path) as peer:
        peer.failures = 1
        with pytest.raises(InterfaceError) as raised:
            DicomClient(peer.destination).store(synthetic_image())
        assert raised.value.retryable
        assert not list(tmp_path.glob("*.dcm"))


def test_study_metadata_has_patient_order_and_sop_references():
    bundle = transform(parse((Path(__file__).parents[1] / "samples/orm_o01.hl7").read_text()))
    person, order = [e["resource"] for e in bundle["entry"]]
    image = synthetic_image()
    study = study_transaction(image, person, order)["entry"][-1]["resource"]
    assert study["basedOn"] == [{"reference": f"ServiceRequest/{order['id']}"}]
    assert study["series"][0]["instance"][0]["uid"] == image.SOPInstanceUID
    image.PatientID = "DIFFERENT-PATIENT"
    with pytest.raises(InterfaceError, match="mismatch"):
        study_transaction(image, person, order)
