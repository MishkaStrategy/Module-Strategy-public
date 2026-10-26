# Evidence Control v1

## Purpose

Evidence Control v1 adds a bounded, read-only provenance surface to the local Modular Strategy Control Plane and Dashboard.

It does not create evidence, recompute research/audit truth, initialize persistence, authorize paper admission, run acceptance jobs or expose arbitrary filesystem artifacts. Strategy Audit & Validation and Results Aggregator remain the authoritative producers and owners of their durable state.

`Acceptance` remains a separate later Control UI phase for local durable acceptance/soak orchestration. Phase 5c exposes only `Evidence`.

## Authoritative sources

### Independent Strategy Audit ledger

`research/strategy_audit_validation` owns the paper-admission audit ledger.

The normal `SQLiteAuditStore` is intentionally not used for display reads because its constructor creates parent directories, opens writable SQLite/WAL state and initializes schema.

The additive `ReadOnlyAuditStore`:

- requires an already existing regular non-symlink SQLite file;
- requires a quiescent owner snapshot with no `-wal`, `-shm` or `-journal` sidecar; active sidecars fail closed without modification;
- opens SQLite with `mode=ro&immutable=1` and `PRAGMA query_only = ON`;
- never creates directories, schema, WAL/SHM state or decisions;
- validates exact audit schema metadata;
- canonical-decodes every stored evidence payload and verifies its SHA-256;
- verifies candidate/evidence identity binding;
- reconstructs every stored `AuditDecision` and therefore verifies its decision hash and contract shape;
- verifies contiguous decision sequence, previous-record linkage and every append-only record hash;
- verifies every decision is bound to the exact stored evidence hash/candidate;
- bounds full-ledger verification to 10,000 evidence rows and 10,000 decisions;
- fails closed on missing, active, incompatible or tampered state.

An actively written audit database is therefore reported unavailable/degraded until the owning writer reaches a quiescent SQLite snapshot. The observer never tries to checkpoint, merge or repair the owner's WAL.

### HMAC boundary

The independent auditor signs decisions with a process-held symmetric HMAC key. That key is deliberately not persisted in the audit database and is not supplied to Control Plane.

Therefore Evidence Control distinguishes two different integrity claims:

- `hash_chain_verified=true`: the observer independently reverified canonical evidence hashes, decision hashes and the unkeyed append-only record chain;
- `hmac_signature_reverified=false`: the observer did not and cannot re-authenticate historical HMAC signatures without the independent auditor key.

The public UI may display the recorded paper-admission outcome, but it must never label that outcome as newly HMAC-verified by Control Plane. The signature value itself is not returned.

### Results Aggregator evidence registry

`modules/results_aggregator` owns immutable research evidence and its relations.

The normal `SQLiteRegistry` is not used for display reads because its constructor opens writable SQLite/WAL state and can initialize or migrate schema.

The additive `ReadOnlyEvidenceRegistry`:

- requires the existing Results Aggregator registry and current schema version;
- requires a quiescent snapshot with no active SQLite sidecars and fails closed instead of touching them;
- uses `mode=ro&immutable=1` plus query-only SQLite;
- never invokes registry initialization, migration, registration, annotation, checkpoint or quarantine paths;
- canonical-decodes every immutable evidence payload and verifies the stored content hash;
- validates evidence identity, experiment, type, status, producer, severity, timestamps and SHA-256 fields;
- verifies stored run/artifact relation tables exactly match the immutable evidence payload;
- bounds full evidence verification to 10,000 rows;
- exposes relation and limitation counts only, not the raw scope/limitations/artifact content.

## Control Plane configuration

Optional existing owner databases are configured by server-owned environment variables:

- `MODULAR_CONTROL_STRATEGY_AUDIT_STATE_PATH`
- `MODULAR_CONTROL_RESULTS_REGISTRY_PATH`

At least one source is required for Evidence Control to be registered.

Configured paths must already exist, must be regular files and must not be symlinks. `modular-control-api --check` exposes only:

- `evidence_control_enabled`;
- `strategy_audit_evidence_enabled`;
- `results_registry_evidence_enabled`.

It never prints either filesystem path.

The Control Plane adapters import the dedicated owner query modules lazily only when Evidence is configured/read. The owner packages' frozen top-level public API blobs are not changed merely to expose the Control UI readers. Importing lightweight `modular_control_plane` does not import `strategy_audit_validation` or `modular_results_registry`.

## API

All Evidence routes are reader-authenticated GET endpoints:

- `GET /api/control/v1/evidence/overview`
- `GET /api/control/v1/evidence/audit/decisions`
- `GET /api/control/v1/evidence/audit/evidence`
- `GET /api/control/v1/evidence/results`

Lists are bounded to 1..200 rows per page. `has_more` is determined by a one-record look-ahead after a full page.

There are no Evidence POST, PUT, PATCH or DELETE endpoints in v1.

Typed failures include:

- `CONTROL_EVIDENCE_NOT_CONFIGURED`;
- `CONTROL_EVIDENCE_SOURCE_NOT_CONFIGURED`;
- `CONTROL_EVIDENCE_STATE_INVALID`;
- `CONTROL_EVIDENCE_UNAVAILABLE`.

Raw exception messages are not returned. Component-health degradation identifies only stable owner source IDs, not exception class names or host paths.

## Public Strategy Audit projection

The browser receives only:

- ledger evidence/decision counts;
- append-only chain head;
- explicit hash-chain/HMAC-verification flags;
- audit sequence, audit/candidate/auditor IDs;
- recorded outcome;
- evidence reference and SHA-256;
- bounded reason codes;
- issued-at UTC timestamp;
- audit contract version;
- decision SHA-256.

It does not receive:

- HMAC signature value;
- request hash or record-chain internals;
- raw audit evidence payload;
- private evidence fields;
- filesystem paths;
- signing key or any credential.

## Public Results evidence projection

The browser receives only:

- evidence and experiment IDs;
- evidence type;
- producer/version;
- status and severity;
- created-at UTC timestamp;
- evidence/content SHA-256;
- registry revision;
- counts of linked runs/artifacts/limitations.

It does not receive:

- raw `scope`;
- raw limitation text;
- run IDs or artifact IDs;
- artifact paths/content;
- package payloads;
- annotations/quarantine payloads;
- host filesystem topology.

## Native Dashboard

`#/evidence` is a native workspace in the unified Dashboard shell and uses only the memory-held Control reader credential.

The page shows:

- Evidence component health;
- configured owner sources;
- Strategy Audit hash-chain summary;
- an explicit `HMAC not reverified by observer` boundary;
- bounded recorded paper-admission decisions;
- bounded audit evidence index;
- bounded Results Aggregator immutable evidence metadata.

There are no Approve, Reject, Re-audit, Register, Annotate, Delete or filesystem-browse controls.

`#/acceptance` intentionally remains `contract pending` until the later durable acceptance/soak phase.

## Acceptance criteria

Phase 5c is accepted only when exact-head evidence proves:

1. Strategy Audit read-only tests pass on Python 3.12/3.13 and prove missing reads create no state, symlinks/active SQLite sidecars fail closed, immutable reads create no sidecars and tampering is rejected.
2. Results Aggregator read-only evidence tests pass on Python 3.12/3.13 and prove immutable payload/relation validation without schema migration/checkpoint side effects.
3. Control Plane compile, Ruff, strict MyPy, component tests, lightweight import boundary, build and wheel smoke pass.
4. A real API smoke creates owner state through public owner write contracts, records a closed hash/file inventory for complete SQLite state, reads it through Control Plane, and proves every owner state file remains byte-identical afterward.
5. The API and browser never expose raw evidence payloads, HMAC signatures, private paths, owner worker/session data or secrets.
6. Browser strict TypeScript, unit tests and production build pass.
7. Real Playwright acceptance loads `#/evidence`, proves the HMAC boundary and recorded evidence projection, reloads/re-authenticates with memory-only credentials, proves no Evidence mutation controls exist and leaves complete owner SQLite state unchanged.
8. Existing Market Data, Backtest, Strategy and Research browser acceptance remains green.

## Explicitly out of scope

- re-running independent audit;
- accepting/rejecting candidates;
- obtaining or exposing the HMAC signing key;
- observer-driven SQLite checkpoint/repair of an active owner database;
- Results Registry registration/annotation/quarantine mutation;
- raw artifact download or arbitrary filesystem browsing;
- local acceptance/soak job orchestration;
- GitHub merge/release controls;
- live trading, private exchange credentials or real order placement.
