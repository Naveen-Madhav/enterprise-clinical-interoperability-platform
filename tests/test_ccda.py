import base64
from pathlib import Path

import pytest
from lxml import etree

from interop.ccda import NS, build_note, document_transaction, validate_document, validators
from interop.errors import InterfaceError
from interop.fhir import transform
from interop.hl7 import parse


@pytest.fixture(scope="module")
def note():
    resources = [
        e["resource"]
        for e in transform(parse((Path(__file__).parents[1] / "samples/oru_r01.hl7").read_text()))[
            "entry"
        ]
    ]
    person, observation, report = resources
    return build_note(person, report, [observation]), person


def test_nominal_cda_schema_and_official_ccda_schematron(note):
    document, patient = note
    root = validate_document(document)
    assert (
        "SYNTHETIC DATA ONLY"
        in base64.b64decode(root.find("cda:component/cda:nonXMLBody/cda:text", NS).text).decode()
    )
    bundle = document_transaction(document, patient)
    binary, reference = [e["resource"] for e in bundle["entry"][1:]]
    assert base64.b64decode(binary["data"]) == document
    assert reference["content"][0]["attachment"]["url"] == f"Binary/{binary['id']}"


def test_official_schematron_rejects_missing_us_realm_race(note):
    document, _ = note
    root = etree.fromstring(document)
    race = root.find("cda:recordTarget/cda:patientRole/cda:patient/cda:raceCode", NS)
    race.getparent().remove(race)
    schema, schematron = validators(str(Path("runtime/standards").resolve()))
    assert schema.validate(root)
    assert not schematron.validate(root)
    assert schematron.validation_report.xpath("//svrl:failed-assert", namespaces=NS)


@pytest.mark.parametrize("mutation", ["entity", "template", "patient"])
def test_document_boundary_rejections(note, mutation):
    document, patient = note
    if mutation == "entity":
        document = (
            b'<!DOCTYPE x [<!ENTITY content SYSTEM "file:///etc/passwd">]>'
            + document.split(b"?>", 1)[1]
        )
    elif mutation == "template":
        document = document.replace(b"2015-08-01", b"2000-01-01")
    else:
        document = document.replace(b"SYN-1001", b"DIFFERENT-PATIENT")
    with pytest.raises(InterfaceError):
        document_transaction(document, patient)
