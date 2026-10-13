from pathlib import Path

import pytest
from hl7apy.parser import parse_message

from interop.errors import InterfaceError
from interop.hl7 import acknowledge, birth_date, number, parse, text, timestamp

SAMPLES = Path(__file__).parents[1] / "samples"


@pytest.mark.parametrize("name", ["adt_a01", "oru_r01"])
def test_nominal_standard_structure(name):
    message = parse((SAMPLES / f"{name}.hl7").read_text())
    assert parse_message(message.raw).validate()
    assert message.one("PID").field(3) == "SYN-1001^^^FICTIONAL_MRN^MR"


@pytest.mark.parametrize(
    "before,after,code",
    [
        ("SYN-1001^^^FICTIONAL_MRN^MR", "", "profile.PID-3"),
        ("19880214", "19880230", "profile.birth-date"),
        ("92|mg/dL", "ninety|mg/dL", "profile.NM"),
        ("|||F", "|||X", "profile.OBX-status-or-repetition"),
        ("ORU^R01^ORU_R01", "ORM^O01^ORM_O01", "profile.segment-order"),
        ("|P|2.5.1", "|P|2.4", "profile.version-or-processing"),
    ],
)
def test_profile_rejection(before, after, code):
    with pytest.raises(InterfaceError, match=code):
        parse((SAMPLES / "oru_r01.hl7").read_text().replace(before, after))


def test_reordered_segments_rejected():
    raw = (SAMPLES / "oru_r01.hl7").read_text().splitlines()
    raw[2], raw[3] = raw[3], raw[2]
    with pytest.raises(InterfaceError, match="segment-order"):
        parse("\r".join(raw))


@pytest.mark.parametrize("value", ["20260230120000-0500", "20260610", "20260610120000"])
def test_timestamp_contract(value):
    with pytest.raises(InterfaceError):
        timestamp(value)


def test_timestamp_offset_and_birth_date():
    assert timestamp("20260610120500-0500") == "2026-06-10T12:05:00-05:00"
    assert birth_date("19880214") == "1988-02-14"


def test_delimiter_escapes_and_numeric_precision():
    assert text("A\\F\\B\\E\\C") == "A|B\\C"
    assert str(number("0.001")) == "0.001"
    with pytest.raises(InterfaceError):
        text("A\\X41\\")


def test_ack_correlation_and_standard_structure():
    raw = (SAMPLES / "adt_a01.hl7").read_text()
    ack = acknowledge(raw, "AA")
    model = parse_message(ack)
    assert model.validate()
    assert str(model.msa.msa_2.value) == "SYN-ADT-001"
    assert str(model.msh.msh_10.value) != "SYN-ADT-001"
    assert str(model.msh.msh_3.value) == "BRIDGE"
