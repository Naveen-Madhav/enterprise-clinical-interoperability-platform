# Operations runbook

The complete default workflow is `interop-platform demo`. It creates temporary peers and credentials and verifies all protocol boundaries without any external clinical service. The commands below support separately configured destinations; they do not start a real EHR or provision a client registration.

## Interface service and monitoring

Copy `config/local.json` outside the repository and set each endpoint's FHIR base, OAuth token URL, client ID, scope and secret environment-variable name. `config/routes.json` is executable routing configuration. Every named destination must have an endpoint registration. Destination APIs must support R4 transaction, update-as-create and read. The API URLs in the supplied configuration are loopback examples; the demo instead uses ephemeral ports.

Supply secrets through the environment. For bash/zsh, repeat for each configured endpoint:

```bash
read -r -s FHIR_REPOSITORY_SECRET
export FHIR_REPOSITORY_SECRET
```

The default names are `FHIR_REPOSITORY_SECRET`, `FHIR_RIS_SECRET`, `FHIR_SCHEDULER_SECRET` and `FHIR_EXTERNAL_RESULTS_SECRET`. No real secret belongs in JSON, Git, logs or shell command arguments.

```bash
mkdir -p runtime
interop-platform probe --config /path/to/local-config.json
interop-platform serve --config /path/to/local-config.json 2> runtime/events.log
```

Run one dispatcher process per database. Stop it with Ctrl+C. Service startup restores interrupted delivery claims to pending. Operational reads do not reset a live claim.

In a separate terminal, record independent sender evidence and send a message:

```bash
interop-platform source-record samples/orm_o01.hl7 --database runtime/transactions.sqlite
interop-platform send samples/orm_o01.hl7
interop-platform health
interop-platform reconcile --refresh
interop-platform exceptions
interop-platform logs runtime/events.log
```

Use the same database and `--config` path as the running service. The sender accepts `--host`/`--port`. Source evidence in a real deployment must come from the source's own export or transaction log. Recording only received messages cannot detect unsent source events.

Health exits 1 when exceptions exist or pending work exceeds the age threshold. Reconciliation exits 1 for missing/changed source evidence, incomplete delivery, or missing/changed destination content. `--refresh` reads the current resources from their recorded destination names. Without it, the report uses historical snapshots. Input/configuration errors exit 2.

Retries cover transient HTTP/transport failures with bounded exponential backoff. An exception remains inspectable with its transaction ID, attempts and safe diagnostic. After repairing its destination, use `interop-platform requeue ID`. A newer transaction touching the same destination/resource prevents stale replay. Correct rejected source messages at the sender and assign a new MSH-10; replay does not edit clinical data.

## DICOM storage and metadata

The local storage peer offers the documented Secondary Capture profile and AE registry:

```bash
interop-platform pacs-serve --pacs-config config/pacs.json
```

Then, from another terminal:

```bash
interop-platform dicom-send samples/secondary_capture.dcm --pacs-config config/pacs.json
```

The sender checks C-ECHO and C-STORE separately. Change a copy of `pacs.json` to address a compatible PACS destination. Registration, network policy, supported SOP classes/transfer syntaxes and transport protection must agree with that destination. A TCP connection or echo response alone does not establish storage acceptance.

To store the instance and queue its FHIR metadata through configured routes:

```bash
interop-platform dicom-publish samples/secondary_capture.dcm --order samples/orm_o01.hl7 --config /path/to/local-config.json
```

This command validates patient/order identity before transfer, retries transient storage failures up to three attempts, and queues metadata only after success. The separately running dispatcher delivers the queued ImagingStudy transaction. If interrupted before queueing, rerun the same instance; the reference SCP detects identical retransmission. DICOM transfer itself is not a durable outbox job.

## C-CDA exchange

```bash
interop-platform prepare-standards
interop-platform ccda-export samples/oru_r01.hl7 --output runtime/result-note.xml
interop-platform validate runtime/result-note.xml --format ccda
```

Export refuses to overwrite an existing file. The note represents the ORU-derived report, uses a device author from the fictional lab, and states that its contents are synthetic. It passes CDA XSD and the supported C-CDA template error rules before being written.

For import, supply the corresponding Patient resource as a local JSON file:

```bash
interop-platform ccda-import runtime/result-note.xml --patient /path/to/synthetic-patient.json --config /path/to/local-config.json
```

Import binds the document's MRN authority and identifier to the Patient, then queues Patient/Binary/DocumentReference delivery. It does not interpret the narrative as clinical observations. Document IDs are derived from content so changed exports get new document identities.

## Registered SMART application

Register a public client and exact redirect URI with the target EHR. Start an authorization session:

```bash
interop-platform smart-start --base https://ehr.example/fhir --client-id registered-client --redirect-uri https://app.example/callback
```

Open the returned authorization URL and complete the target EHR's authorization flow. For EHR launch, also pass `--launch` with the EHR-provided context identifier. After the EHR redirects to the registered URI, supply that complete callback URL:

```bash
interop-platform smart-finish --callback 'https://app.example/callback?code=RETURNED_CODE&state=RETURNED_STATE'
```

Those domains and parameter values are examples, not operational credentials. The CLI provides a manual callback handoff; it does not host a web callback handler. State is held in `runtime/smart-session.json` with mode 0600 and expires after five minutes. Finish checks URI/state, performs the code exchange, reads the context Patient, reports only context/type/scopes, and consumes the session. It does not print or persist the access token. A failed state/URI check leaves the unconsumed session available; an attempted exchange consumes it.

## Storage and diagnostics

Runtime database files contain source messages, mapped bundles, delivery attempts and hashes. The database is created with mode 0600 and its directory with mode 0700. DICOM files and exported documents are also local clinical-data artifacts if used beyond this synthetic lab. Keep them, tokens, captures and credentials outside version control.

| Symptom | Boundary to inspect |
| --- | --- |
| No HL7 ACK | Framing, timeout, listener endpoint, ASCII and size limit |
| AE/AR | ERR diagnostic and documented profile; MSA-2 correlation |
| FHIR pending | Destination availability, retry schedule and oldest queue age |
| FHIR exception | Capability support, permission, response contract and read-back mismatch |
| DICOM association rejection | Calling/called AE registration and negotiated contexts |
| DICOM store failure | Returned DIMSE status; no available metadata should be queued |
| C-CDA rejection | Schema/template contract, embedded body, MRN authority and identity |
| SMART failure | Discovery, audience, callback registration, state, PKCE and granted patient scopes |

The event logger records internal IDs, states and bounded diagnostics rather than messages or bearer tokens. Library debug logging is not enabled. Reconciliation paths and local payloads remain available to authorized operators; they are not anonymization tools.
