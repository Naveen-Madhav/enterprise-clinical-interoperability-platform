"""C-CDA R2.1 unstructured result notes and FHIR document exchange."""

import base64
import threading
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from lxml import etree, isoschematron

from .errors import InterfaceError
from .fhir import digest, logical_id, transaction
from .hl7 import timestamp
from .standards_assets import prepare_assets

CDA = "urn:hl7-org:v3"
NS = {"cda": CDA, "svrl": "http://purl.oclc.org/dsdl/svrl"}
VALIDATION_LOCK = threading.RLock()
MRN_ROOT = "2.16.840.1.113883.19.5.99999.1"
HEADER = "2.16.840.1.113883.10.20.22.1.1"
UNSTRUCTURED = "2.16.840.1.113883.10.20.22.1.10"


def element(parent, tag, content=None, **attributes):
    child = etree.SubElement(parent, f"{{{CDA}}}{tag}", **attributes)
    if content is not None:
        child.text = content
    return child


def build_note(patient: dict, report: dict, observations: list[dict]) -> bytes:
    if patient["identifier"][0]["system"] != "urn:id:FICTIONAL_MRN":
        raise InterfaceError("ccda.assigning-authority")
    material = digest({"patient": patient, "report": report, "observations": observations})
    document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, material))
    root = etree.Element(f"{{{CDA}}}ClinicalDocument", nsmap={None: CDA})
    element(root, "realmCode", code="US")
    element(root, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    element(root, "templateId", root=HEADER, extension="2015-08-01")
    element(root, "templateId", root=UNSTRUCTURED, extension="2015-08-01")
    element(root, "id", root=document_id)
    element(
        root,
        "code",
        code="11502-2",
        codeSystem="2.16.840.1.113883.6.1",
        displayName="Laboratory report",
    )
    element(root, "title", "Synthetic laboratory result note")
    when = datetime.fromisoformat(report["effectiveDateTime"]).strftime("%Y%m%d%H%M%S%z")
    element(root, "effectiveTime", value=when)
    element(root, "confidentialityCode", code="N", codeSystem="2.16.840.1.113883.5.25")
    element(root, "languageCode", code="en-US")
    role = element(element(root, "recordTarget"), "patientRole")
    element(role, "id", root=MRN_ROOT, extension=patient["identifier"][0]["value"])
    element(role, "addr", nullFlavor="UNK")
    element(role, "telecom", nullFlavor="UNK")
    person = element(role, "patient")
    name = element(person, "name", use="L")
    element(name, "given", patient["name"][0]["given"][0])
    element(name, "family", patient["name"][0]["family"])
    gender = {"male": "M", "female": "F"}.get(patient["gender"])
    if gender:
        element(person, "administrativeGenderCode", code=gender, codeSystem="2.16.840.1.113883.5.1")
    else:
        element(person, "administrativeGenderCode", nullFlavor="UNK")
    element(person, "birthTime", value=patient["birthDate"].replace("-", ""))
    element(person, "raceCode", nullFlavor="UNK")
    element(person, "ethnicGroupCode", nullFlavor="UNK")
    language = element(person, "languageCommunication")
    element(language, "languageCode", code="en-US")
    author = element(root, "author")
    element(author, "time", value=when)
    assigned = element(author, "assignedAuthor")
    element(assigned, "id", root="2.16.840.1.113883.19.5.99999.2", extension="SYN-AUTHOR-001")
    element(assigned, "addr", nullFlavor="UNK")
    element(assigned, "telecom", nullFlavor="UNK")
    device = element(assigned, "assignedAuthoringDevice")
    element(device, "manufacturerModelName", "Fictional interface laboratory")
    element(device, "softwareName", "Synthetic result note exporter")
    custodian = element(element(root, "custodian"), "assignedCustodian")
    organization = element(custodian, "representedCustodianOrganization")
    element(organization, "id", root="2.16.840.1.113883.19.5.99999.3")
    element(organization, "name", "Fictional Clinical Integration Laboratory")
    element(organization, "telecom", nullFlavor="UNK")
    element(organization, "addr", nullFlavor="UNK")
    lines = ["SYNTHETIC DATA ONLY. Not a clinical report.", f"Report status: {report['status']}."]
    for observation in observations:
        coding = observation["code"]["coding"][0]
        quantity = observation.get("valueQuantity")
        value = (
            f"{quantity['value']} {quantity['unit']}" if quantity else observation["valueString"]
        )
        lines.append(
            f"{coding['code']} {coding.get('display', '')}: {value} ({observation['status']})"
        )
    body = element(element(root, "component"), "nonXMLBody")
    element(
        body,
        "text",
        base64.b64encode("\n".join(lines).encode()).decode(),
        mediaType="text/plain",
        representation="B64",
    )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


@lru_cache(maxsize=2)
def validators(directory: str):
    assets = prepare_assets(Path(directory))
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    schema = etree.XMLSchema(etree.parse(str(assets / "cda/infrastructure/cda/CDA.xsd"), parser))
    source = etree.parse(str(assets / "ccda/Consolidation.sch"), parser)
    # Make the pinned local vocabulary location explicit for the generated XSLT document() calls.
    for node in source.iter():
        if "test" in node.attrib:
            node.attrib["test"] = node.attrib["test"].replace(
                "document('voc.xml')", f"document('{(assets / 'ccda/voc.xml').resolve().as_uri()}')"
            )
    # Compile every published rule for the two templates this adapter supports.
    # Other document templates are outside this contract and contain unrelated
    # generator artifacts that do not validate as ISO Schematron.
    sch = "{http://purl.oclc.org/dsdl/schematron}"
    selected = {
        f"p-urn-hl7ii-{template}-2015-08-01-{phase}"
        for template in (HEADER, UNSTRUCTURED)
        for phase in ("errors", "warnings")
    }
    for pattern in source.findall(sch + "pattern"):
        if pattern.get("id") not in selected:
            source.getroot().remove(pattern)
    for active in source.findall(".//" + sch + "active"):
        if active.get("pattern") not in selected:
            active.getparent().remove(active)
    errors = isoschematron.Schematron(source, phase="errors", store_report=True)
    return schema, errors


def validate_document(document: bytes, directory=Path("runtime/standards")) -> etree._Element:
    if len(document) > 1024 * 1024:
        raise InterfaceError("ccda.size")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(document, parser)
        if root.getroottree().docinfo.doctype or root.tag != f"{{{CDA}}}ClinicalDocument":
            raise InterfaceError("ccda.xml-contract")
        templates = {
            (n.get("root"), n.get("extension")) for n in root.findall(f"{{{CDA}}}templateId")
        }
        if templates != {(HEADER, "2015-08-01"), (UNSTRUCTURED, "2015-08-01")}:
            raise InterfaceError("ccda.template")
        schema, schematron = validators(str(directory.resolve()))
        with VALIDATION_LOCK:
            if not schema.validate(root):
                raise InterfaceError("ccda.cda-schema")
            if not schematron.validate(root):
                raise InterfaceError("ccda.schematron")
        embedded = root.find("cda:component/cda:nonXMLBody/cda:text", NS)
        if (
            embedded is None
            or embedded.get("mediaType") != "text/plain"
            or embedded.get("representation") != "B64"
            or len(embedded) != 0
        ):
            raise InterfaceError("ccda.embedded-text-profile")
        base64.b64decode(embedded.text or "", validate=True).decode("utf-8")
        return root
    except (etree.XMLSyntaxError, ValueError, UnicodeError) as exc:
        raise InterfaceError("ccda.xml") from exc


def document_transaction(
    document: bytes, patient: dict, directory=Path("runtime/standards")
) -> dict:
    root = validate_document(document, directory)
    if patient["identifier"][0]["system"] != "urn:id:FICTIONAL_MRN":
        raise InterfaceError("ccda.assigning-authority")
    identifiers = root.findall("cda:recordTarget/cda:patientRole/cda:id", NS)
    if not any(
        n.get("root") == MRN_ROOT and n.get("extension") == patient["identifier"][0]["value"]
        for n in identifiers
    ):
        raise InterfaceError("ccda.patient-mismatch")
    identifier = root.find("cda:id", NS).get("root")
    code = root.find("cda:code", NS)
    binary_id = logical_id("binary", identifier)
    binary = {
        "resourceType": "Binary",
        "id": binary_id,
        "contentType": "application/xml",
        "data": base64.b64encode(document).decode(),
    }
    reference = {
        "resourceType": "DocumentReference",
        "id": logical_id("document", identifier),
        "status": "current",
        "subject": {"reference": f"Patient/{patient['id']}"},
        "type": {"coding": [{"system": "http://loinc.org", "code": code.get("code")}]},
        "date": timestamp(root.find("cda:effectiveTime", NS).get("value")),
        "content": [
            {
                "attachment": {
                    "contentType": "application/xml",
                    "url": f"Binary/{binary_id}",
                    "title": "Synthetic laboratory result note",
                }
            }
        ],
    }
    return transaction([patient, binary, reference])
