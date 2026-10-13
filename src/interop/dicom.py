"""DICOM association, verification, durable storage and FHIR study metadata.

DIMSE-C contract: https://dicom.nema.org/medical/dicom/current/output/chtml/part07/chapter_9.html
"""

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydicom import dcmread
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, SecondaryCaptureImageStorage
from pynetdicom import AE, evt
from pynetdicom.sop_class import Verification

from .errors import InterfaceError
from .fhir import logical_id, transaction


def uid_for(value: str) -> str:
    return "2.25." + str(uuid.uuid5(uuid.NAMESPACE_URL, value).int)


def synthetic_image(order_id="SYN-ORDER-001", patient_id="SYN-1001") -> FileDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = uid_for("synthetic-instance:" + order_id)
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid_for("fictional-imaging-generator")
    image = FileDataset(None, {}, file_meta=meta, preamble=b"\x00" * 128)
    values = {
        "SOPClassUID": meta.MediaStorageSOPClassUID,
        "SOPInstanceUID": meta.MediaStorageSOPInstanceUID,
        "PatientName": "ExampleOnly^Avery",
        "PatientID": patient_id,
        "PatientBirthDate": "19880214",
        "PatientSex": "",
        "StudyInstanceUID": uid_for("synthetic-study:" + order_id),
        "SeriesInstanceUID": uid_for("synthetic-series:" + order_id),
        "StudyDate": "20260610",
        "StudyTime": "120500",
        "StudyID": "SYN-STUDY-001",
        "AccessionNumber": order_id,
        "ReferringPhysicianName": "",
        "SeriesNumber": 1,
        "InstanceNumber": 1,
        "Modality": "OT",
        "ConversionType": "SYN",
        "Manufacturer": "Fictional Imaging Lab",
        "PatientOrientation": "",
        "ContentDate": "20260610",
        "ContentTime": "120500",
        "TimezoneOffsetFromUTC": "-0500",
        "ImageType": ["DERIVED", "SECONDARY"],
        "BurnedInAnnotation": "NO",
        "Rows": 16,
        "Columns": 16,
        "SamplesPerPixel": 1,
        "PhotometricInterpretation": "MONOCHROME2",
        "BitsAllocated": 8,
        "BitsStored": 8,
        "HighBit": 7,
        "PixelRepresentation": 0,
        "PixelData": bytes((x + y) * 8 for y in range(16) for x in range(16)),
    }
    for name, value in values.items():
        setattr(image, name, value)
    return image


def validate_image(image):
    if image.SOPClassUID != SecondaryCaptureImageStorage:
        raise InterfaceError("dicom.unsupported-sop-class")
    if any(
        not UID(str(getattr(image, key, ""))).is_valid
        for key in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID")
    ):
        raise InterfaceError("dicom.uid")
    if (
        image.file_meta.TransferSyntaxUID != ExplicitVRLittleEndian
        or image.Rows != 16
        or image.Columns != 16
        or image.SamplesPerPixel != 1
        or image.BitsAllocated != 8
        or image.BitsStored != 8
        or image.HighBit != 7
        or image.PixelRepresentation != 0
        or image.PhotometricInterpretation != "MONOCHROME2"
        or len(image.PixelData) != 256
    ):
        raise InterfaceError("dicom.synthetic-profile")


def dataset_digest(image) -> str:
    return hashlib.sha256(json.dumps(image.to_json_dict(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    called_aet: str = "SYN_PACS"
    calling_aet: str = "SYN_MODALITY"

    def __post_init__(self):
        for title in (self.called_aet, self.calling_aet):
            if not title or len(title) > 16 or not title.isascii() or "\\" in title:
                raise ValueError("Invalid DICOM AE title")
        if not 0 < self.port < 65536:
            raise ValueError("Invalid DICOM port")


class DicomClient:
    def __init__(self, destination: Destination):
        self.destination = destination

    @contextmanager
    def association(self):
        ae = AE(ae_title=self.destination.calling_aet)
        ae.add_requested_context(Verification, ExplicitVRLittleEndian)
        ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 5
        association = ae.associate(
            self.destination.host, self.destination.port, ae_title=self.destination.called_aet
        )
        if not association.is_established:
            raise InterfaceError("dicom.association", retryable=True)
        try:
            yield association
        finally:
            if association.is_established:
                association.release()

    def echo(self):
        with self.association() as association:
            status = association.send_c_echo()
            if not status or status.Status != 0:
                raise InterfaceError("dicom.echo", retryable=True)
        return {"association": "accepted", "c_echo": "0x0000"}

    def store(self, image):
        validate_image(image)
        with self.association() as association:
            status = association.send_c_store(image)
            if not status or "Status" not in status:
                raise InterfaceError("dicom.store-no-response", retryable=True)
            if status.Status != 0:
                raise InterfaceError(
                    f"dicom.store.{status.Status:04X}", retryable=status.Status == 0xA700
                )
        return {"sop_instance_uid": str(image.SOPInstanceUID), "c_store": "0x0000"}


class StoragePeer:
    def __init__(self, directory: Path, port=0):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.failures = 0
        self.ae = AE(ae_title="SYN_PACS")
        self.ae.require_called_aet = True
        self.ae.require_calling_aet = ["SYN_MODALITY"]
        self.ae.add_supported_context(Verification, ExplicitVRLittleEndian)
        self.ae.add_supported_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
        self.ae.maximum_associations = 8
        self.server = self.ae.start_server(
            ("127.0.0.1", port), block=False, evt_handlers=[(evt.EVT_C_STORE, self.store)]
        )

    def store(self, event):
        with self.lock:
            if self.failures:
                self.failures -= 1
                return 0xA700
            try:
                if len(event.request.DataSet.getvalue()) > 2 * 1024 * 1024:
                    return 0xA700
                image = event.dataset
                image.file_meta = event.file_meta
                validate_image(image)
                if image.SOPInstanceUID != event.request.AffectedSOPInstanceUID:
                    return 0xA900
                target = self.directory / f"{image.SOPInstanceUID}.dcm"
                if target.exists():
                    return 0 if dataset_digest(dcmread(target)) == dataset_digest(image) else 0xC000
                temporary = target.with_suffix(".partial")
                image.save_as(temporary, enforce_file_format=True)
                os.chmod(temporary, 0o600)
                with temporary.open("rb") as source:
                    os.fsync(source.fileno())
                os.replace(temporary, target)
                return 0
            except (InterfaceError, ValueError, AttributeError, OSError):
                return 0xC000

    @property
    def destination(self):
        return Destination(*self.server.server_address)

    def close(self):
        self.server.shutdown()


@contextmanager
def storage_peer(directory: Path):
    peer = StoragePeer(directory)
    try:
        yield peer
    finally:
        peer.close()


def study_transaction(image, patient: dict, order: dict) -> dict:
    validate_image(image)
    if (
        image.PatientID != patient["identifier"][0]["value"]
        or image.AccessionNumber != order["identifier"][0]["value"]
    ):
        raise InterfaceError("dicom.patient-or-order-mismatch")
    study = {
        "resourceType": "ImagingStudy",
        "id": logical_id("study", str(image.StudyInstanceUID)),
        "status": "available",
        "subject": {"reference": f"Patient/{patient['id']}"},
        "identifier": [
            {"system": "urn:dicom:uid", "value": "urn:oid:" + str(image.StudyInstanceUID)}
        ],
        "basedOn": [{"reference": f"ServiceRequest/{order['id']}"}],
        "numberOfSeries": 1,
        "numberOfInstances": 1,
        "series": [
            {
                "uid": str(image.SeriesInstanceUID),
                "number": 1,
                "modality": {
                    "system": "http://dicom.nema.org/resources/ontology/DCM",
                    "code": "OT",
                },
                "numberOfInstances": 1,
                "instance": [
                    {
                        "uid": str(image.SOPInstanceUID),
                        "number": 1,
                        "sopClass": {
                            "system": "urn:ietf:rfc:3986",
                            "code": "urn:oid:" + str(image.SOPClassUID),
                        },
                    }
                ],
            }
        ],
    }
    return transaction([patient, order, study])
