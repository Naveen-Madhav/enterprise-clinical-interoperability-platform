# Verification evidence

Recorded October 13, 2026 (America/Chicago), using Python 3.11. CI reproduces the suite and platform demo on Python 3.11 and 3.12.

| Check | Recorded evidence |
| --- | --- |
| Unit/integration suite | 78 tests pass |
| Combined socket demo | Six source transactions delivered; 16 destination resources matched |
| Central repository | Nine resources: Patient, Encounter, ServiceRequest, Appointment, Observation, DiagnosticReport, ImagingStudy, Binary, DocumentReference |
| Destination fan-out | RIS: two resources; scheduler: two; external results: three |
| FHIR failure recovery | Forced RIS 503 after repository acceptance; repeated PUTs converge and all destination read-backs match |
| SMART | EHR and standalone launch; S256 RFC vector; code/state/callback/audience checks; expiry/replay rejection; patient isolation and denied writes |
| SMART clinical workflow | Authorized reads of Patient, Encounter, Observation, ServiceRequest and DiagnosticReport |
| DICOM | Real association, successful C-ECHO/C-STORE, stored Part 10 read-back, identical replay and conflicting-UID rejection |
| DICOM transient failure | 0xA700 recovered on second attempt; metadata publication remains gated by successful storage |
| C-CDA | Core CDA XSD plus all published error rules for the two supported templates pass; missing required race element fails Schematron |
| Artifact boundaries | Identity mismatch blocks transfer; failed C-STORE queues no available study; XML entity/template/patient defects are rejected |
| Operations | Persistent replay/collision checks, ordered backoff, exceptions/requeue, independent evidence reconciliation, private SMART state and CLI exit behavior |

## Independent DICOM IOD check

The checked-in `samples/secondary_capture.dcm` was checked with pydicom's `dicom-validator` 0.9.0 against DICOM edition 2026a. The command exited 0 and reported the Secondary Capture Image IOD with no validation errors.

Reproduce this optional check in a separate environment:

```bash
python -m pip install dicom-validator==0.9.0
validate_iods --edition 2026a --standard-path /tmp/dicom-standard samples/secondary_capture.dcm
```

The tool downloads the relevant NEMA DocBook specification on first use. Its [documented limitations](https://github.com/pydicom/dicom-validator#limitations) include incomplete condition evaluation and checks that do not establish clinical correctness. This result supports the fixture's module/attribute structure; it is not certification.

## Validation boundaries

The local endpoints implement explicit contracts and share some validation code with the platform. Passing their tests establishes agreement on the tested cases, not independent vendor compatibility. The DICOM IOD check and published HL7 schema/Schematron rules add separate evidence, but do not remove those limits.

The C-CDA adapter compiles the complete error patterns for US Realm Header and Unstructured Document, preserving their assertions and inheritance. It rejects other header template sets. It does not execute SHOULD warnings, unrelated document templates or structured clinical sections. The test suite demonstrates that an XML document can pass core XSD but still fail the applicable C-CDA rules.

FHIR model checks are supplemented with transaction/reference and selected status checks. No full FHIR invariant/terminology service, US Core test pack, performance load test, vendor EHR authorization test, remote PACS test or production monitoring evaluation was performed. Runtime metrics describe synthetic local execution only.
