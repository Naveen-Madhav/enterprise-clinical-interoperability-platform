"""ER7 profile boundaries. Field positions follow HL7 v2.5.1 chapters 2, 3 and 7."""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from hl7apy.parser import parse_message

from .errors import InterfaceError

MAX_MESSAGE = 1024 * 1024
PROFILES = {
    "ADT^A01": ("MSH", "EVN", "PID", "PV1"),
    "ADT^A03": ("MSH", "EVN", "PID", "PV1"),
    "ORU^R01": ("MSH", "PID", "OBR", "OBX"),
    "ORM^O01": ("MSH", "PID", "ORC", "OBR"),
    "SIU^S12": ("MSH", "SCH", "PID", "RGS", "AIS"),
}


@dataclass(frozen=True)
class Segment:
    name: str
    fields: tuple[str, ...]

    def field(self, number: int) -> str:
        if self.name == "MSH":
            if number == 1:
                return "|"
            number -= 1
        return self.fields[number - 1] if number <= len(self.fields) else ""


@dataclass(frozen=True)
class Message:
    segments: tuple[Segment, ...]
    raw: str

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    def one(self, name: str) -> Segment:
        matches = self.all(name)
        if len(matches) != 1:
            raise InterfaceError(f"profile.{name}.cardinality")
        return matches[0]

    @property
    def workflow(self) -> str:
        return "^".join(self.one("MSH").field(9).split("^")[:2])

    @property
    def control_id(self) -> str:
        return self.one("MSH").field(10)

    @property
    def source_key(self) -> str:
        msh = self.one("MSH")
        return "\x1f".join(msh.field(n) for n in (3, 4, 10))


def text(value: str) -> str:
    """Decode the five delimiter escapes; reject unsupported escape semantics."""
    if value == '""' or "~" in value:
        raise InterfaceError("profile.null-or-repetition")
    escapes = {"F": "|", "S": "^", "R": "~", "T": "&", "E": "\\"}

    def decode(match):
        try:
            return escapes[match.group(1)]
        except KeyError as exc:
            raise InterfaceError("profile.escape") from exc

    result = re.sub(r"\\([^\\]*)\\", decode, value)
    if "\\" in re.sub(r"\\([^\\]*)\\", "", value):
        raise InterfaceError("profile.escape")
    return result


def timestamp(value: str) -> str:
    # Full precision and an explicit offset are project requirements, not universal HL7 rules.
    if not re.fullmatch(r"\d{14}[+-]\d{4}", value):
        raise InterfaceError("profile.timestamp.precision-or-offset")
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S%z").isoformat()
    except ValueError as exc:
        raise InterfaceError("profile.timestamp.calendar") from exc


def birth_date(value: str) -> str:
    if not re.fullmatch(r"\d{8}", value):
        raise InterfaceError("profile.birth-date")
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise InterfaceError("profile.birth-date") from exc


def number(value: str) -> Decimal:
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
        raise InterfaceError("profile.NM")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise InterfaceError("profile.NM") from exc


def parse(raw: str) -> Message:
    raw = raw.replace("\r\n", "\r").replace("\n", "\r").rstrip("\r") + "\r"
    if len(raw) > MAX_MESSAGE or not raw.isascii() or not raw.startswith("MSH|^~\\&|"):
        raise InterfaceError("profile.encoding", ack="AR")
    lines = raw.rstrip("\r").split("\r")
    if len(lines) > 200 or any(not re.match(r"^[A-Z0-9]{3}\|", line) for line in lines):
        raise InterfaceError("profile.segment")
    message = Message(tuple(Segment(line[:3], tuple(line[4:].split("|"))) for line in lines), raw)
    msh = message.one("MSH")
    if not all(msh.field(n) for n in (3, 4, 5, 6, 7, 9, 10, 11, 12)):
        raise InterfaceError("profile.header")
    if msh.field(12) != "2.5.1" or msh.field(11) != "P":
        raise InterfaceError("profile.version-or-processing", ack="AR")
    if msh.field(15) or msh.field(16):
        raise InterfaceError("profile.enhanced-ack-unsupported", ack="AR")
    if message.workflow not in PROFILES:
        raise InterfaceError("profile.workflow", ack="AR")
    required = PROFILES[message.workflow]
    names = tuple(s.name for s in message.segments)
    expected = (
        required
        if message.workflow != "ORU^R01"
        else required[:-1] + ("OBX",) * len(message.all("OBX"))
    )
    if names != expected or (message.workflow == "ORU^R01" and not message.all("OBX")):
        raise InterfaceError("profile.segment-order")
    timestamp(msh.field(7))
    pid = message.one("PID")
    cx = pid.field(3).split("^")
    if len(cx) < 5 or not cx[0] or not cx[3] or cx[4] != "MR" or "~" in pid.field(3):
        raise InterfaceError("profile.PID-3")
    if not pid.field(5):
        raise InterfaceError("profile.PID-5")
    birth_date(pid.field(7))
    if pid.field(8) not in {"M", "F", "O", "U"}:
        raise InterfaceError("profile.PID-8")
    if message.workflow.startswith("ADT"):
        timestamp(message.one("EVN").field(2))
        pv1 = message.one("PV1")
        if pv1.field(2) not in {"I", "O", "E"} or not pv1.field(19):
            raise InterfaceError("profile.PV1")
    elif message.workflow == "ORM^O01":
        orc, obr = message.one("ORC"), message.one("OBR")
        if orc.field(1) != "NW" or not orc.field(2) or orc.field(2) != obr.field(2):
            raise InterfaceError("profile.order-identity")
        if not obr.field(4):
            raise InterfaceError("profile.OBR")
        timestamp(obr.field(7))
    elif message.workflow == "SIU^S12":
        sch, ais = message.one("SCH"), message.one("AIS")
        if not sch.field(1) or not sch.field(2) or not sch.field(6) or not ais.field(3):
            raise InterfaceError("profile.schedule")
        timing = sch.field(11).split("^")
        if len(timing) < 5:
            raise InterfaceError("profile.schedule-timing")
        start, end = timestamp(timing[3]), timestamp(timing[4])
        if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
            raise InterfaceError("profile.schedule-range")
        if timestamp(ais.field(4)) != start:
            raise InterfaceError("profile.schedule-service-time")
    else:
        obr = message.one("OBR")
        if not obr.field(2) or not obr.field(3) or not obr.field(4):
            raise InterfaceError("profile.OBR")
        timestamp(obr.field(7))
        identities = set()
        for obx in message.all("OBX"):
            if obx.field(2) not in {"NM", "ST", "TX"} or not obx.field(3) or not obx.field(5):
                raise InterfaceError("profile.OBX")
            if obx.field(11) not in {"F", "P", "C"} or "~" in obx.field(5):
                raise InterfaceError("profile.OBX-status-or-repetition")
            identity = (obx.field(3), obx.field(4))
            if identity in identities:
                raise InterfaceError("profile.OBX-identity")
            identities.add(identity)
            if obx.field(2) == "NM":
                number(obx.field(5))
            else:
                text(obx.field(5))
    try:
        # Library validation checks the standard structure in addition to this site's profile.
        if message.workflow == "ORM^O01":
            # ORM's OBR/RQD/RQ1/RXO/ODS/ODT choice is checked by our exact sequence.
            # hl7apy's generated choice group otherwise demands unselected alternatives.
            for segment in parse_message(raw, find_groups=False).children:
                segment.validate()
        else:
            parse_message(raw, find_groups=True).validate()
    except Exception as exc:
        raise InterfaceError("hl7.structure") from exc
    return message


def acknowledge(raw: str, code: str, diagnostic: str = "") -> str:
    """Original-mode ACK; MSA-2 echoes inbound MSH-10, never the ACK's own ID."""
    header = raw.replace("\n", "\r").split("\r")[0].split("|")

    def field(n):
        return header[n - 1] if n - 1 < len(header) else ""

    when = datetime.now(UTC).strftime("%Y%m%d%H%M%S%z")
    trigger = field(9).split("^")
    event = trigger[1] if len(trigger) > 1 else ""
    msh = (
        f"MSH|^~\\&|{field(5)}|{field(6)}|{field(3)}|{field(4)}|{when}||"
        f"ACK^{event}^ACK|{uuid.uuid4().hex}|P|2.5.1\r"
    )
    msa = f"MSA|{code}|{field(10)}\r"
    error_code, description = "207", "Application internal error"
    if diagnostic == "source.control-id-collision":
        error_code, description = "205", "Duplicate key identifier"
    elif diagnostic == "profile.segment-order":
        error_code, description = "100", "Segment sequence error"
    elif diagnostic in {"profile.PID-3", "profile.PID-5", "profile.header", "profile.OBR"}:
        error_code, description = "101", "Required field missing"
    elif diagnostic == "profile.workflow":
        error_code, description = "200", "Unsupported message type"
    elif diagnostic.startswith(("profile.", "mapping.", "hl7.", "fhir.")):
        error_code, description = "102", "Data type error"
    err = f"ERR|||{error_code}^{description}^HL70357|E|||{diagnostic}\r" if diagnostic else ""
    return msh + msa + err
