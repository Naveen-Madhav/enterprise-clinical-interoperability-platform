"""FHIR R4 resource mapping and transaction assembly.

The 6.5.0 models target R4, not the R4B models in later package releases.
Resource contracts: https://hl7.org/fhir/R4/resourcelist.html
"""

import hashlib
import json
import re

from fhir.resources import construct_fhir_element
from pydantic import ValidationError

from .errors import InterfaceError
from .hl7 import Message, birth_date, number, text, timestamp


def logical_id(kind: str, *parts: str) -> str:
    material = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_resource(resource: dict) -> dict:
    allowed = {
        "Patient",
        "Encounter",
        "Observation",
        "DiagnosticReport",
        "Bundle",
        "ServiceRequest",
        "Appointment",
        "DocumentReference",
        "Binary",
        "ImagingStudy",
    }
    kind = resource.get("resourceType")
    if kind not in allowed:
        raise InterfaceError("fhir.resource-type")
    try:
        model = construct_fhir_element(kind, resource)
        normalized = json.loads(model.json(exclude_none=True))
    except (ValidationError, ValueError, TypeError) as exc:
        raise InterfaceError("fhir.shape") from exc
    statuses = {
        "Encounter": {"in-progress", "finished"},
        "Observation": {"final", "preliminary", "corrected"},
        "DiagnosticReport": {"final", "preliminary", "corrected"},
    }
    if kind in statuses and normalized.get("status") not in statuses[kind]:
        raise InterfaceError("fhir.status")
    if kind == "Bundle":
        if normalized.get("type") != "transaction" or not normalized.get("entry"):
            raise InterfaceError("fhir.transaction")
        paths = set()
        for entry in normalized["entry"]:
            item = validate_resource(entry.get("resource", {}))
            path = f"{item['resourceType']}/{item.get('id', '')}"
            if entry.get("request") != {"method": "PUT", "url": path} or path in paths:
                raise InterfaceError("fhir.transaction-request")
            if entry.get("fullUrl") != f"https://bridge.example.invalid/fhir/{path}":
                raise InterfaceError("fhir.transaction-full-url")
            paths.add(path)
        for entry in normalized["entry"]:
            check_references(entry["resource"], paths)
    return normalized


def check_references(value, paths: set[str]):
    if isinstance(value, dict):
        if "reference" in value and value["reference"] not in paths:
            raise InterfaceError("fhir.unresolved-reference")
        for child in value.values():
            check_references(child, paths)
    elif isinstance(value, list):
        for child in value:
            check_references(child, paths)


def coding(value: str) -> dict:
    parts = value.split("^")
    if len(parts) != 3 or parts[2] != "LN" or not re.fullmatch(r"\d+-\d", parts[0]):
        raise InterfaceError("mapping.code-system")
    return {"coding": [{"system": "http://loinc.org", "code": parts[0], "display": text(parts[1])}]}


def patient(message: Message) -> dict:
    pid = message.one("PID")
    cx = pid.field(3).split("^")
    if "&" in cx[3]:
        raise InterfaceError("mapping.assigning-authority")
    name = pid.field(5).split("^")
    if len(name) != 2 or not all(name):
        raise InterfaceError("mapping.patient-name")
    return {
        "resourceType": "Patient",
        "id": logical_id("patient", cx[3], cx[0]),
        "identifier": [{"system": f"urn:id:{cx[3]}", "value": text(cx[0])}],
        "name": [{"family": text(name[0]), "given": [text(name[1])]}],
        "birthDate": birth_date(pid.field(7)),
        "gender": {"M": "male", "F": "female", "O": "other", "U": "unknown"}[pid.field(8)],
    }


def transaction(resources: list[dict]) -> dict:
    return validate_resource(
        {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "fullUrl": f"https://bridge.example.invalid/fhir/{r['resourceType']}/{r['id']}",
                    "resource": r,
                    "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
                }
                for r in resources
            ],
        }
    )


def transform(message: Message) -> dict:
    person = patient(message)
    subject = {"reference": f"Patient/{person['id']}"}
    resources = [person]
    if message.workflow.startswith("ADT"):
        pv1 = message.one("PV1")
        resources.append(
            {
                "resourceType": "Encounter",
                "id": logical_id("encounter", message.one("MSH").field(4), pv1.field(19)),
                "identifier": [{"system": "urn:id:fictional-visit", "value": pv1.field(19)}],
                "status": "finished" if message.workflow == "ADT^A03" else "in-progress",
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": {"I": "IMP", "O": "AMB", "E": "EMER"}[pv1.field(2)],
                },
                "subject": subject,
            }
        )
    elif message.workflow == "ORM^O01":
        obr = message.one("OBR")
        resources.append(
            {
                "resourceType": "ServiceRequest",
                "id": logical_id("order", person["id"], obr.field(2)),
                "identifier": [
                    {"system": "urn:id:fictional-placer", "value": obr.field(2).split("^")[0]}
                ],
                "status": "active",
                "intent": "order",
                "code": coding(obr.field(4)),
                "subject": subject,
                "occurrenceDateTime": timestamp(obr.field(7)),
            }
        )
    elif message.workflow == "SIU^S12":
        sch, ais = message.one("SCH"), message.one("AIS")
        timing = sch.field(11).split("^")
        resources.append(
            {
                "resourceType": "Appointment",
                "id": logical_id("appointment", person["id"], sch.field(2)),
                "identifier": [
                    {"system": "urn:id:fictional-scheduler", "value": sch.field(2).split("^")[0]}
                ],
                "status": "booked",
                "serviceType": [coding(ais.field(3))],
                "start": timestamp(timing[3]),
                "end": timestamp(timing[4]),
                "participant": [{"actor": subject, "status": "accepted"}],
            }
        )
    elif message.workflow == "ORU^R01":
        obr = message.one("OBR")
        result_refs = []
        statuses = []
        for obx in message.all("OBX"):
            status = {"F": "final", "P": "preliminary", "C": "corrected"}[obx.field(11)]
            statuses.append(status)
            observation = {
                "resourceType": "Observation",
                "id": logical_id(
                    "observation", person["id"], obr.field(3), obx.field(3), obx.field(4)
                ),
                "status": status,
                "code": coding(obx.field(3)),
                "subject": subject,
                "effectiveDateTime": timestamp(obr.field(7)),
            }
            if obx.field(2) == "NM":
                units = obx.field(6).split("^")
                if len(units) != 3 or units[2] != "UCUM" or units[0] not in {"mg/dL", "mmol/L"}:
                    raise InterfaceError("mapping.units")
                observation["valueQuantity"] = {
                    "value": number(obx.field(5)),
                    "unit": units[0],
                    "system": "http://unitsofmeasure.org",
                    "code": units[0],
                }
            else:
                observation["valueString"] = text(obx.field(5))
            resources.append(observation)
            result_refs.append({"reference": f"Observation/{observation['id']}"})
        resources.append(
            {
                "resourceType": "DiagnosticReport",
                "id": logical_id("report", person["id"], obr.field(3)),
                "identifier": [
                    {"system": "urn:id:fictional-lis", "value": obr.field(3).split("^")[0]}
                ],
                "status": "preliminary"
                if "preliminary" in statuses
                else ("corrected" if "corrected" in statuses else "final"),
                "code": coding(obr.field(4)),
                "subject": subject,
                "effectiveDateTime": timestamp(obr.field(7)),
                "result": result_refs,
            }
        )
    else:
        raise InterfaceError("mapping.workflow", ack="AR")
    return transaction(resources)
