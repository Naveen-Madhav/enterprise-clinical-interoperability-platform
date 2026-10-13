import copy
from pathlib import Path

import pytest

from interop.errors import InterfaceError
from interop.fhir import logical_id, transform, validate_resource
from interop.hl7 import parse

SAMPLES = Path(__file__).parents[1] / "samples"


def test_adt_patient_and_encounter():
    bundle = transform(parse((SAMPLES / "adt_a01.hl7").read_text()))
    patient, encounter = [e["resource"] for e in bundle["entry"]]
    assert patient["gender"] == "unknown"
    assert patient["birthDate"] == "1988-02-14"
    assert encounter["subject"]["reference"] == f"Patient/{patient['id']}"
    assert encounter["class"]["code"] == "IMP"


def test_results_preserve_units_status_and_references():
    bundle = transform(parse((SAMPLES / "oru_r01.hl7").read_text()))
    patient, observation, report = [e["resource"] for e in bundle["entry"]]
    assert observation["valueQuantity"] == {
        "value": 92,
        "unit": "mg/dL",
        "system": "http://unitsofmeasure.org",
        "code": "mg/dL",
    }
    assert report["result"] == [{"reference": f"Observation/{observation['id']}"}]
    assert report["subject"] == {"reference": f"Patient/{patient['id']}"}
    assert observation["effectiveDateTime"] == "2026-06-10T12:05:00-05:00"


def test_correction_updates_same_logical_observation():
    raw = (SAMPLES / "oru_r01.hl7").read_text()
    first = transform(parse(raw))
    corrected = transform(parse(raw.replace("92|mg/dL", "94|mg/dL").replace("|||F", "|||C")))
    assert first["entry"][1]["resource"]["id"] == corrected["entry"][1]["resource"]["id"]
    assert corrected["entry"][1]["resource"]["status"] == "corrected"


def test_identity_includes_authority():
    assert logical_id("patient", "A", "1") != logical_id("patient", "B", "1")


@pytest.mark.parametrize("mutation", ["missing-status", "unknown-field", "unresolved", "duplicate"])
def test_json_validation_rejects_unsafe_transaction(mutation):
    bundle = copy.deepcopy(transform(parse((SAMPLES / "oru_r01.hl7").read_text())))
    if mutation == "missing-status":
        del bundle["entry"][1]["resource"]["status"]
    elif mutation == "unknown-field":
        bundle["entry"][0]["resource"]["patientName"] = "Wrong R4 shape"
    elif mutation == "unresolved":
        bundle["entry"][1]["resource"]["subject"]["reference"] = "Patient/missing"
    else:
        bundle["entry"].append(bundle["entry"][0])
    with pytest.raises(InterfaceError):
        validate_resource(bundle)
