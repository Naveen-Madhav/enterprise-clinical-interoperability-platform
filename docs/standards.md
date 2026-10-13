# Standards and mapping decisions

The platform uses explicit local profiles so unsupported data fails at a defined boundary. A clean result means the stated checks passed; it does not establish compatibility with a vendor's interface specification.

## HL7 contract

| Workflow | Project sequence and mapping |
| --- | --- |
| ADT A01/A03 | MSH, EVN, PID, PV1; Patient plus Encounter in-progress/finished |
| ORM O01 | MSH, PID, ORC, OBR; ORC-1 NW, matching ORC-2/OBR-2, Patient plus active ServiceRequest |
| ORU R01 | MSH, PID, OBR, one or more OBX; Patient, selected Observation values and DiagnosticReport |
| SIU S12 | MSH, SCH, PID, RGS, AIS; booked Appointment with Patient participant and explicit start/end |

Common profile constraints: MSH-11 P, MSH-12 2.5.1, empty MSH-15/16, one local MR identifier in PID-3, one family/given name in PID-5, full birth date in PID-7, and M/F/O/U sex mapping. Patient identity includes the assigning authority. Event timestamps require full second precision and an offset; base HL7 permits reduced precision that this mapping does not support.

ORM order IDs include patient and placer order identity. ORU observation IDs include patient, filler order, observation code and sub-ID. OBX F/P/C maps to final/preliminary/corrected. Report aggregation prefers preliminary if any result is preliminary, then corrected, then final. This aggregation is local policy. Only NM/ST/TX values are mapped. NM requires an explicitly supported UCUM unit. Reference ranges and interpretation flags remain in the source payload.

SCH-11 TQ components 4/5 provide appointment start/end; AIS-4 must agree with start. SCH-6, SCH-16 and SCH-20 remain populated for the base scheduling structure. RGS-1 and AIS-1/3 are required. Repeating appointments and cancellation are outside this profile.

hl7apy validates ADT, ORU and SIU groups. Its generated ORM choice group demands unselected RQD/RQ1/RXO/ODS/ODT alternatives, so ORM uses library segment validation plus the explicit selected sequence and order-identity checks. This avoids treating a library false rejection as a standard requirement.

Sources: [control and acknowledgments](https://hl7.eu/HL7v2x/v251/std251/ch02.html), [patient administration](https://hl7.eu/HL7v2x/v251/std251/ch03.html), [orders](https://hl7.eu/HL7v2x/v251/std251/ch04.html), [results](https://hl7.eu/HL7v2x/v251/std251/ch07.html), [scheduling](https://hl7.eu/HL7v2x/v251/std251/ch10.html), [MLLP](https://hl7.eu/refactored/transport01mllp.html), [hl7apy](https://hl7apy.readthedocs.io/en/latest/tutorial/).

## FHIR delivery and identity

Mapped resources use R4 4.0.1 shapes. Bundles use PUT requests with deterministic IDs, absolute fullUrl values under a reserved example domain, and relative resource references. The destination must support transaction, update-as-create and read. Each destination's transaction is atomic; fan-out across destinations is not. The client checks the transaction response and reads content back. Resource `meta` is excluded from content comparison.

Routes are selected by the most specific resource type and stored with the receipt. Encounter routes to the repository; ServiceRequest also routes to RIS; Appointment also routes to scheduling; DiagnosticReport also routes to the external results service. ImagingStudy and DocumentReference route to the repository. Reconciliation keys include destination name so equal logical IDs at different endpoints remain independently observable.

The imaging accession must match the ServiceRequest identifier and its PatientID must match the Patient MRN before transfer. ImagingStudy uses DICOM study/series/instance UIDs and references the order. C-CDA uses the fixed fictional MRN authority; a mismatched patient or assigning authority is rejected. DocumentReference points to a Binary resource containing the original validated XML. Embedded text is preserved rather than converted into clinical assertions.

Sources: [FHIR R4 HTTP](https://hl7.org/fhir/R4/http.html#transaction), [Patient](https://hl7.org/fhir/R4/patient.html), [Encounter](https://hl7.org/fhir/R4/encounter.html), [Observation](https://hl7.org/fhir/R4/observation.html), [ServiceRequest](https://hl7.org/fhir/R4/servicerequest.html), [DiagnosticReport](https://hl7.org/fhir/R4/diagnosticreport.html), [Appointment](https://hl7.org/fhir/R4/appointment.html), [ImagingStudy](https://hl7.org/fhir/R4/imagingstudy.html), [DocumentReference](https://hl7.org/fhir/R4/documentreference.html), [Binary](https://hl7.org/fhir/R4/binary.html).

The CT order fixture uses [LOINC 24627-2, CT Chest](https://loinc.org/24627-2/), which is designated for both order and observation use. The glucose result uses LOINC 2345-7 and UCUM mg/dL. The code mapper checks the declared system and shape, not live terminology membership.

## SMART and OAuth boundaries

SMART 2.2 discovery uses `[FHIR base]/.well-known/smart-configuration`. Authorization requests bind client ID, registered callback URI, audience and unpredictable state to an S256 challenge. The verifier is sent only during code exchange. Standalone launch requests `launch/patient`; EHR launch requests `launch` and passes the launch ID. The client requests read access to five patient resource types and requires a returned patient context and matching read scopes.

The loopback peer issues one-use, expiring authorization codes bound to client, callback and challenge. It denies access to another patient and refuses writes using the SMART read grant. The client does not request openid/fhirUser, so it makes no end-user identity claim. Expired read grants require a new launch. Client credentials used by the delivery workers are a separate OAuth grant with a local `bridge.write` scope, not SMART Backend Services.

Sources: [SMART launch](https://hl7.org/fhir/smart-app-launch/STU2.2/app-launch.html), [SMART conformance](https://hl7.org/fhir/smart-app-launch/STU2.2/conformance.html), [OAuth 2.0](https://www.rfc-editor.org/rfc/rfc6749), [bearer tokens](https://www.rfc-editor.org/rfc/rfc6750), [PKCE](https://www.rfc-editor.org/rfc/rfc7636).

## DICOM and C-CDA boundaries

DICOM uses DIMSE-C association negotiation with Verification and Secondary Capture Image Storage, Explicit VR Little Endian, calling/called AE checks, and explicit status handling. A failed C-STORE never announces available ImagingStudy metadata. The local SCP persists a Part 10 file before returning success, recognizes identical retransmissions and refuses conflicting content under an existing SOP Instance UID.

The synthetic image uses the Secondary Capture IOD, not CT Image Storage. Its pixels are invented. The independent IOD check is recorded in the verification document; the runtime adapter checks the narrower transport fixture profile.

C-CDA targets R2.1 US Realm Header and Unstructured Document with 2015-08-01 template extensions. Validation first uses the normative CDA R2 XSD, then all error rules for those two templates from the pinned published Schematron. All original assertions and abstract-rule inheritance for those templates are retained. Other document patterns are excluded because they are outside the adapter's contract and contain unrelated generator artifacts. Vocabulary references resolve to the verified local file. SHOULD warnings are not evaluated.

`standards-assets.json` records immutable source commit URLs and SHA-256 digests. Cached files are verified before compilation. Source documents cannot load DTDs or entities. The adapter accepts embedded base64 UTF-8 text only, never a referenced external body.

Sources: [DICOM DIMSE-C](https://dicom.nema.org/medical/dicom/current/output/chtml/part07/chapter_9.html), [Secondary Capture IOD](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_A.8.html), [pynetdicom association](https://pydicom.github.io/pynetdicom/stable/user/association_requesting.html), [CDA core schemas](https://github.com/HL7/CDA-core-2.0), [C-CDA R2.1 validation artifacts](https://github.com/HL7/CDA-ccda-2.1), [official examples](https://github.com/HL7/C-CDA-Examples), [lxml validation](https://lxml.de/validation.html#schematron).
