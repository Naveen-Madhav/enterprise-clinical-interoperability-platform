"""Publish non-HL7 evidence through the same durable FHIR delivery path."""

import hashlib
import time

from .ccda import document_transaction
from .dicom import dataset_digest, study_transaction
from .errors import InterfaceError
from .fhir import canonical, validate_resource
from .pipeline import event


def enqueue_bundle(bridge, key: str, source_material: str, bundle: dict) -> int:
    bundle = validate_resource(bundle)
    routes = bridge.router.routes_for(bundle) if bridge.router else None
    bridge.ledger.record_source(key, source_material)
    transaction_id = bridge.ledger.enqueue(key, source_material, bundle, routes=routes)
    event("accepted", transaction_id=transaction_id)
    return transaction_id


def publish_image(image, client, bridge, patient, order, *, max_attempts=3, retry_base=2.0):
    # Check identity before sending; publish available metadata only after C-STORE succeeds.
    bundle = study_transaction(image, patient, order)
    if max_attempts < 1 or retry_base < 0:
        raise ValueError("Invalid DICOM retry policy")
    for attempt in range(1, max_attempts + 1):
        try:
            result = client.store(image)
            break
        except InterfaceError as exc:
            event("artifact-transfer", attempt=attempt, state="failed", diagnostic=exc.code)
            if not exc.retryable or attempt == max_attempts:
                raise
            time.sleep(retry_base * 2 ** (attempt - 1))
    source = canonical(
        {
            "format": "DICOM",
            "sop_instance_uid": str(image.SOPInstanceUID),
            "dataset_digest": dataset_digest(image),
        }
    )
    transaction_id = enqueue_bundle(bridge, "dicom:" + str(image.SOPInstanceUID), source, bundle)
    return {**result, "attempts": attempt, "metadata_transaction": transaction_id}


def publish_document(document: bytes, bridge, patient):
    bundle = document_transaction(document, patient)
    source = canonical({"format": "C-CDA", "document_digest": hashlib.sha256(document).hexdigest()})
    document_id = bundle["entry"][-1]["resource"]["id"]
    transaction_id = enqueue_bundle(bridge, "ccda:" + document_id, source, bundle)
    return {
        "validation": "CDA R2 XSD and supported C-CDA R2.1 error rules",
        "document_transaction": transaction_id,
    }
