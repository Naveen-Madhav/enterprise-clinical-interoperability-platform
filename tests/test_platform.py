from pathlib import Path

from interop.enterprise_demo import run_platform_demo


def test_complete_patient_order_imaging_document_and_smart_workflow():
    root = Path(__file__).parents[1]
    report = run_platform_demo(root / "samples", root / "config/routes.json")
    assert report["health"]["states"] == {"delivered": 6}
    assert report["destination_resource_counts"] == {
        "repository": 9,
        "ris": 2,
        "scheduler": 2,
        "external-results": 3,
    }
    assert report["dicom"]["attempts"] == 2
    assert report["dicom"]["stored_content_matches"]
    assert report["smart"]["read_types"] == [
        "Patient",
        "Encounter",
        "Observation",
        "ServiceRequest",
        "DiagnosticReport",
    ]
    assert report["reconciliation"]["matched_destination_resources"] == 16
