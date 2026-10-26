# Market Data CLI and Web — architecture audit

- Audit date: 2026-07-27
- Repository: `mishkacher/Module-Strategy`
- Interface branch: `interface/market-data-web-cli`
- Backend dependency branch: `data/historical-market-data-ingestion`
- Backend head audited: `02860eb3c21b783b90778b77fd50d2992f399b5a`
- Backend Draft PR: #221

## Executive result

The Historical Market Data Ingestion backend is a substantial deterministic component,
not a placeholder. It already owns acquisition, normalization, validation, repair,
versioning, checkpoint/resume/cancel, export, raw lineage and a detailed SQLite Dataset
Registry. The interface project must wrap these services rather than reimplement them.

The principal missing boundary is not exchange functionality. It is a durable,
transport-neutral application layer for jobs, progress, query pagination, structured
errors and interface idempotency. The existing CLI is useful backend acceptance tooling
but is not yet the canonical stable CLI required by the interface specification.

## Canonical repository state

The current staged project line is `integration/modular-strategy-v0.6.0-rc`. Frozen Core
0.4.2 remains authoritative for generic trading contracts. The market-data component was
created from the exact staged checkpoint recorded by its handoff and targets the staged
branch through Draft PR #221.

The interface branch is intentionally stacked on the backend component branch. This
avoids copying unmerged backend code and keeps the interface diff reviewable.

## Backend capabilities found

### Domain and application-adjacent services

- `market_data_ingestion.pipeline.IngestionService`;
- `market_data_ingestion.operations.DatasetOperations`;
- capability-driven exchange adapters;
- `LocalStorage` with immutable raw/normalized/derived publication;
- `SQLiteDatasetRegistry`;
- deterministic canonical hashing;
- validation and integrity verification;
- export service;
- doctor checks;
- checkpoint, resume specification and cancel marker support.

### Supported lifecycle

The backend machine enum already includes:

- `CREATED`;
- `VALIDATING_REQUEST`;
- `DISCOVERING_CAPABILITIES`;
- `RESOLVING_INSTRUMENT`;
- `PLANNING`;
- `DOWNLOADING`;
- `VERIFYING_RAW`;
- `NORMALIZING`;
- `VALIDATING_DATA`;
- `REPAIRING`;
- `BUILDING_DERIVED`;
- `HASHING`;
- `REGISTERING`;
- `COMPLETED`;
- `COMPLETED_WITH_WARNINGS`;
- `PAUSED`;
- `CANCELLED`;
- `FAILED`;
- `DATASET_REJECTED`.

These machine values are retained unchanged. Russian labels belong in presentation
metadata, not the enum.

### Existing idempotency and recovery

`IngestionService` derives a deterministic execution hash and run ID. The Dataset
Registry reserves `request_id` with a request hash, returns an already registered
dataset for identical completed content and rejects reuse of an ID with different
canonical content. Checkpoints, resume specs and cancel markers protect long-running
execution.

This is a strong domain-level foundation, but it does not yet provide a public job ID,
job list, mutable job metadata, append-only events, user-facing logs, actor identity or
HTTP idempotency-key records.

## Existing CLI findings

The backend currently exposes `market-data` through
`market_data_ingestion.cli:main` and Python `argparse`.

Implemented commands include discovery, download from YAML, inspect, validate, update,
backfill, repair, export, resume, cancel and doctor. Output is JSON.

Current limitations relative to the required canonical CLI:

1. CLI directly constructs concrete adapters, storage, registry and ingestion services.
2. No shared application facade exists for CLI and HTTP API.
3. No Typer command tree, Rich human output or shell completion.
4. Exit codes do not match the required stable policy.
5. No common `--output table|json|yaml`, `--quiet`, `--verbose`, `--debug`,
   `--no-color`, `--yes`, `--dry-run`, `--profile` contract.
6. No durable public jobs list/show/logs surface.
7. No local Web/API launcher.
8. `repair` lacks a separate persisted repair-plan confirmation boundary.
9. Discovery uses exact symbol resolution rather than a paginated search contract.
10. CLI error payloads are not yet the same versioned contracts planned for HTTP.

The existing CLI remains valuable as backend regression coverage during migration. It
must not be deleted until the new CLI reaches command and test parity.

## HTTP API and frontend findings

No approved project-wide FastAPI, Typer, React or Vite stack was found in the inspected
canonical root metadata, component documentation or repository code index. No Market
Data HTTP API or browser frontend exists on the backend branch.

The fallback stack from the interface specification is therefore accepted:

- FastAPI and Pydantic v2;
- Uvicorn;
- SSE as the primary progress transport;
- Typer and Rich;
- React, TypeScript and Vite;
- TanStack Query and React Router;
- OpenAPI-derived or strictly synchronized frontend types.

This conclusion must be revisited if a repository-wide console stack lands before the
frontend foundation is committed.

## Logging and observability findings

The ingestion service accepts a logger and configures backend logging, but no durable
interface event/log protocol is exposed. The Web/CLI contract needs a safe structured
record with timestamp, level, component, event, correlation ID, request ID, job ID,
chunk ID and bounded payload.

A progress observer must be additive and non-authoritative: it records backend state
and measured counters but cannot alter ingestion decisions.

## Dataset Registry findings

The existing SQLite registry correctly owns immutable dataset descriptors and request
claims. It must remain the dataset source of truth.

Missing query capabilities required by the interfaces include:

- server-side list pagination;
- filters by exchange, market, instrument, status, data type and timeframe;
- version listing;
- explicit manifest, validation and lineage query services;
- safe export job creation;
- size/usage aggregation for dashboard and doctor.

These are application query adapters over the existing registry and manifests, not a
second registry.

## Job contract gap

The backend run/checkpoint model is execution-focused. The interface requires a durable
job aggregate created before background execution begins.

Minimum first contract set:

- `CreateDownloadRequest`;
- `JobCreatedResponse`;
- `JobSummary`;
- `JobDetails`;
- `JobProgress`;
- `JobEvent`;
- `JobError`;
- `ApiError`;
- `Pagination`;
- `HealthResponse`;
- `DoctorResponse`.

Update, validation, repair, backfill and export contracts can then reuse the same job
envelope.

## Required backend adaptations

The interface project may add the following non-domain hooks:

1. a composition root for adapters, storage and registry;
2. a read-only progress observer protocol for state transitions and measured counters;
3. registry query methods or a separate query adapter for pagination;
4. a repair-plan service that describes affected ranges before mutation;
5. safe log/event sinks with redaction;
6. cancellation/pause coordination exposed through application services.

It must not duplicate exchange paging, record validation, aggregation, hashing,
partition writes or dataset registration.

## Initial repository layout decision

```text
modules/market_data_ingestion/
├── src/
│   ├── market_data_ingestion/
│   ├── market_data_application/
│   ├── market_data_api/
│   └── market_data_cli/
├── tests/
├── tests_application/
├── tests_api/
└── tests_cli/

web/
└── market-data-console/
```

The exact test directory split may be adjusted to existing workflow conventions, but
public package names and ownership boundaries stay fixed.

## Security findings

The backend currently uses public unauthenticated market-data endpoints and contains no
private credential path. Local Web mode must preserve that safety boundary and bind only
to `127.0.0.1` by default.

Server mode is not allowed to become externally reachable merely through `--host
0.0.0.0`. Startup validation must require an explicit server profile and configured
authentication/security policy before accepting non-loopback binding.

## Risks and mitigations

### Stacked branch dependency

Risk: backend branch changes while interfaces are built.

Mitigation: record the exact audited backend head in every cycle, compare before code
changes and adapt without copying backend internals.

### False progress

Risk: the frontend invents percentages from lifecycle states.

Mitigation: progress contracts distinguish determinate and indeterminate work and emit
only measured totals.

### Duplicate state machines

Risk: job state diverges from ingestion state.

Mitigation: backend `RunState` remains authoritative for execution phase; the job store
adds queue/ownership metadata but does not redefine domain transitions.

### Request identity mismatch

Risk: API/CLI idempotency differs from backend request claims.

Mitigation: one canonical serializer/hash is used before enqueueing and the same request
identity is passed into ingestion.

### Registry overloading

Risk: mutable job data weakens immutable dataset guarantees.

Mitigation: separate SQLite schemas/files and explicit transaction boundaries.

## Stage 1 conclusion

Architecture audit: complete.

Implementation release gate: not passed.

The next implementation slice is the shared Pydantic contract package plus durable job
store and application facade tests. No HTTP route or frontend page should be added before
that slice establishes canonical request hashing, structured errors and job recovery.
