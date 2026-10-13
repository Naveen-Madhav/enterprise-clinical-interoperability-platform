# Enterprise Clinical Interoperability Platform

A clinical integration platform connecting HL7 v2 workflows, FHIR R4 APIs, SMART applications, DICOM storage, and C-CDA documents through explicit delivery contracts.

## Sample output

Recorded test and demo summary using Python 3.11; see the [verification evidence](docs/verification.md).

```text
Unit/integration suite: 78 tests passed
Source transactions delivered: 6
Destination resources matched: 16
Central repository resources: 9
Forced RIS 503: recovered; destination read-backs match
```

## Integration problem

Patient events, orders, scheduling messages, results, imaging instances, and clinical documents reach different systems through different protocols. Receiving an HL7 acknowledgment does not establish that a FHIR transaction completed or that PACS stored an image. The integration layer needs consistent identity, durable delivery state, and evidence from each destination.

This platform follows one fictional patient through those boundaries. A central clinical repository receives mapped resources while configured routes also deliver orders to a RIS API, appointments to a scheduling API, and results to an external clinical service. Imaging metadata is published only after successful DICOM storage. A C-CDA result note is validated and exchanged as a FHIR document. A SMART application then reads the patient's clinical resources with a patient-scoped grant.

## Architecture and data flow

```text
EHR / RIS / LIS -> MLLP -> HL7 validation -> R4 mapping -> durable SQLite outbox
                                                            |
                                      configured routing + OAuth client credentials
                                                            |
                            +-------------------------------+-------------------+
                            |                  |                |               |
                      Clinical repository    RIS API       Scheduling API   External results
                            |                  |                |               |
                            +---------------- read-back / SQL reconciliation ---+
                            |
                SMART discovery -> authorization code + PKCE -> patient-scoped reads

Synthetic imaging -> DICOM association / C-ECHO / C-STORE -> storage destination
                                                                |
                                                        ImagingStudy publication

ORU-derived C-CDA -> CDA schema + supported C-CDA error rules -> Binary + DocumentReference
```

The bridge returns original-mode `AA` after validation and durable queue acceptance. Destination delivery remains separately observable. Invalid messages receive `AE`; unsupported contracts and control-ID collisions receive `AR`. Identical resends reuse their receipt. The outbox retains arrival order through backoff and uses deterministic resource IDs for replay-safe PUT transactions.

Each route is recorded with the queued transaction. Fan-out retries can repeat a successful destination after another destination fails; resource IDs make those repeats converge. Reconciliation keeps separate hashes for each destination. There is no atomic transaction spanning multiple systems.

## Components and standards

| Component | Working boundary |
| --- | --- |
| `hl7.py`, `mllp.py` | HL7 v2.5.1 ER7, ADT A01/A03, ORM O01, ORU R01, SIU S12, original-mode ACKs, bounded TCP framing |
| `fhir.py` | R4 Patient, Encounter, ServiceRequest, Observation, DiagnosticReport, Appointment, ImagingStudy, Binary and DocumentReference mapping |
| `routing.py`, `pipeline.py`, `ledger.py` | Configured fan-out, durable queue, bounded retries, exceptions, delivery attempts and reconciliation |
| `http.py` | OAuth 2.0 client credentials, bearer delivery, expiry caching, one token refresh on 401, HTTPS endpoint policy |
| `smart.py`, `smart_reference.py` | SMART 2.2 discovery, EHR/standalone launch, authorization code, S256 PKCE, callback/state checks and patient-scoped reads |
| `dicom.py`, `artifacts.py` | Association negotiation, AE registry, C-ECHO, Secondary Capture C-STORE, stored-content verification and gated metadata publication |
| `ccda.py`, `standards_assets.py` | C-CDA R2.1 Unstructured Document export/import, core CDA XSD, published template error rules and patient identity binding |
| `cli.py`, `platform_commands.py`, `operations.py` | Interface startup, validation, transformation, artifact transfer, SMART launch and operational checks |
| `tests/`, `samples/`, `config/`, `docs/`, `sql/` | Reproducible scenarios, synthetic fixtures, endpoint contracts, runbooks and reconciliation queries |

Python 3.11 or later is required. Dependencies are pinned: hl7apy 1.3.5, fhir.resources 6.5.0 for R4 4.0.1, Pydantic 1.10.26, HTTPX 0.28.1, pydicom 3.0.2, pynetdicom 3.0.4 and lxml 6.0.2. [Standards and mapping decisions](docs/standards.md) distinguish base requirements from this project's profile.

## Quickstart

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
interop-platform prepare-standards
interop-platform demo
pytest -q
ruff check .
ruff format --check .
```

On Windows, activate with `.venv\Scripts\activate`. Initial standards preparation downloads about 65 MB of checksum-pinned HL7 schemas, Schematron and vocabulary into ignored `runtime/standards/`. It requires internet access once. Subsequent document validation uses the verified cache. Project code and fixtures are MIT licensed; downloaded standards artifacts retain their upstream terms.

The demo uses ephemeral loopback ports and temporary databases, DICOM files, secrets and tokens. It needs no Docker, clinical credentials or external healthcare service. It starts actual MLLP, HTTP and DICOM listeners and shuts them down after checking the workflow.

Expected evidence: six delivered source transactions, 16 matching resources across four destination endpoints, nine resources in the central repository, successful C-ECHO/C-STORE with a recovered storage failure, a validated C-CDA document, and SMART reads of Patient, Encounter, Observation, ServiceRequest and DiagnosticReport. A forced RIS 503 exercises FHIR fan-out retry.

Explore individual boundaries:

```bash
interop-platform validate samples/orm_o01.hl7
interop-platform validate samples/ccda_lab_note.xml --format ccda
interop-platform transform samples/siu_s12.hl7
interop-platform generate-dicom --output runtime/another-synthetic.dcm
interop-platform --help
```

The [operations runbook](docs/operations.md) covers separately running endpoints, monitoring, exception replay, DICOM transfer and registered SMART clients. The [verification record](docs/verification.md) lists tested behavior and the remaining validation boundaries.

## Sample data

Every fixture is synthetic. The patient is `ExampleOnly^Avery` with a `SYN-` identifier; organizations, staff, visits and orders are fictional. No patient data, employer logs or clinical credentials are included.

Five nominal HL7 messages cover admission, discharge, a CT chest order, its scheduling event, and a separate glucose result. JSON files show the mapped transactions. The C-CDA note contains an explicit synthetic-data statement. The DICOM fixture is a generated 16 by 16 Secondary Capture image with invented pixels. It is associated with the synthetic imaging order to exercise identity and transport; it does not represent a CT acquisition or completion of a clinical procedure.

## Honest Scope and Limitations

- This is a working integration reference with local evidence, not a deployed hospital platform, regulatory certification, or proof of historical clinical work. EHR, RIS, scheduling and external services are reference endpoints; PACS connectivity is verified against a local DICOM storage SCP. A compatible remote PACS can be configured, but no vendor PACS connection is claimed here.
- HL7 coverage is deliberately bounded to version 2.5.1, standard delimiters, ASCII, original acknowledgments, one patient and one order group. ORM supports new orders; SIU supports new bookings. Repetitions, additional workflows, enhanced acknowledgments, modality worklist and query/retrieve are outside the implementation.
- R4 model validation and project checks are not a full FHIR conformance or terminology validator. Local identity, status aggregation and resource ownership policies are documented. PUT replaces resources with the mapped authoritative subset, so independently maintained destination fields require a negotiated merge policy.
- The SMART client performs the authorization protocol; the loopback issuer supplies a synthetic established-user context. It is not an EHR login system. OIDC identity, refresh tokens, confidential SMART clients and SMART Backend Services authentication are not implemented. Integration delivery uses ordinary OAuth client credentials separately from the SMART read workflow.
- C-CDA validation covers the core CDA schema and every published error rule for the supported US Realm Header and Unstructured Document templates. It does not validate other document templates, SHOULD recommendations, structured clinical sections or current terminology membership. External entities and referenced document bodies are refused by the adapter.
- DICOM supports the documented Secondary Capture/Explicit VR Little Endian profile. C-ECHO alone is not proof of storage. AE allowlists are not cryptographic authentication. MLLP and DIMSE are loopback lab transports without TLS; remote FHIR and OAuth endpoints require HTTPS.
- The SQLite outbox supports one dispatcher process and at-least-once delivery. Fan-out, DICOM storage and FHIR publication are not one distributed transaction. DICOM retries are bounded in the transfer process; an interrupted transfer must be rerun. FHIR metadata/document delivery is durable once queued. There is no high availability or multi-process lease mechanism.
- Reconciliation depends on independent source evidence and destination snapshots. A snapshot establishes content at the observation time; use `reconcile --refresh` for a current check. Runtime state contains payloads and must remain outside Git. A clinical deployment would need approved patient matching, access controls, retention and network policy.

## Author and license

Naveen Madhav | [LinkedIn](https://linkedin.com/in/naveenmadhav)

Licensed under the [MIT License](LICENSE).

**AI assistance:** I defined the project concept, requirements, and validation rule priorities based on my healthcare integration experience. AI coding assistants helped with planning, design exploration, implementation, debugging, testing, and documentation. I take responsibility for reviewing the project, checking its claims, and deciding what to submit.
