# ADR 0004: Shared application layer for Market Data CLI and Web

- Status: Accepted for implementation
- Date: 2026-07-27
- Branch: `interface/market-data-web-cli`
- Depends on: `data/historical-market-data-ingestion`
- Related package: `modules/market_data_ingestion`

## Context

Historical Market Data Ingestion already owns deterministic acquisition, validation,
repair, immutable dataset versioning, checkpoint/resume/cancel, export and the detailed
SQLite-backed Dataset Registry. Its current `market-data` command is an `argparse`
adapter that directly constructs exchange adapters, `LocalStorage`,
`SQLiteDatasetRegistry`, `IngestionService` and `DatasetOperations`.

The interface project must add a stable automation-oriented CLI and a local/server Web
Console without copying domain logic or allowing the frontend to call exchanges
directly. Both interfaces also need durable jobs, progress events, structured errors,
idempotency, pagination and version compatibility. Those concerns do not belong in the
exchange adapters or dataset domain.

No approved repository-wide FastAPI, Typer or React stack was found in the audited
canonical files and code index. The interface therefore follows the project prompt's
fallback stack: FastAPI, Pydantic v2, Typer, Rich, React, TypeScript, Vite, TanStack
Query and React Router.

## Decision

Add a shared application package and keep transport adapters thin:

```text
market_data_cli ─────────────┐
                             │
market_data_web → market_data_api
                             │
                  market_data_application
                             │
                  market_data_ingestion
                             │
       exchange adapters / storage / Dataset Registry
```

The Python packages live in the existing component distribution while the frontend is
built separately:

```text
modules/market_data_ingestion/src/
├── market_data_ingestion/
├── market_data_application/
├── market_data_api/
└── market_data_cli/

web/market-data-console/
```

This preserves one installable component, one version boundary and the existing
`market-data` executable while preventing the CLI and HTTP routes from importing
concrete adapters, storage or registry classes directly.

## Application ownership

`market_data_application` owns interface-facing orchestration only:

- composition of configured ingestion services;
- canonical command/request models;
- durable job creation and lookup;
- job state, progress, event and log persistence;
- idempotency-key resolution;
- dataset query pagination;
- mapping domain errors to stable application errors;
- pause, resume and cancel coordination;
- startup recovery of non-terminal jobs;
- version and capability discovery;
- repair-plan generation and confirmation boundaries.

It must call `market_data_ingestion` for every domain operation. It must not implement
exchange paging, validation, aggregation, hashing, partition publication, repair or
Dataset Registry semantics.

## Transport ownership

`market_data_cli` owns Typer commands, configuration/YAML loading, confirmation,
output rendering and exit-code mapping. It calls the application facade in-process by
default. A later explicit remote profile may use the versioned HTTP API, but command
semantics and output contracts remain the same.

`market_data_api` owns `/api/v1`, authentication/authorization, pagination,
`Idempotency-Key`, structured HTTP errors, OpenAPI and Server-Sent Events. HTTP
requests create or query jobs; they never execute the complete ingestion pipeline in
the request lifecycle.

`market_data_web` owns presentation and browser state only. Backend state is
canonical. Browser refresh reconnects to the job event stream and never re-submits a
job without the same idempotency key.

## Job model

A durable SQLite job store is separate from, but colocated with, the dataset registry.
The dataset registry remains immutable dataset metadata; the job store records mutable
operational state.

Required durable records:

- job identity and operation;
- canonical request and request hash;
- idempotency key;
- backend run ID when available;
- machine state and user-facing phase;
- timestamps and terminal result reference;
- structured error;
- append-only progress events;
- bounded structured logs;
- pause/cancel intent;
- actor and correlation metadata.

The application job ID is stable before execution starts. The backend `run_id` remains
an ingestion execution identity and is stored as a related field, not reused as the
public job primary key.

## Idempotency

The application layer computes a canonical request hash before enqueueing.

- same idempotency key and same canonical request: return the original job;
- same idempotency key and different canonical request: return conflict;
- missing key: CLI generates a stable request ID for one invocation; Web generates and
  persists one for the submission attempt.

The existing ingestion `request_id`/request-hash claim remains the final domain-level
protection. Application idempotency does not replace it.

## Progress and events

The current ingestion state machine is authoritative for machine enum values. A
non-breaking observer interface will be added to the ingestion service so the
application layer can persist transitions and measured counters. The observer must not
change decisions or timing.

No percentage is emitted unless total work is known. Otherwise events include phase,
completed chunks/records/bytes and current range.

SSE is the primary Web progress transport. Event IDs are monotonically increasing per
job, support `Last-Event-ID`, bounded replay and reconnect. WebSocket is not introduced
for the first release.

## Errors

Domain `StructuredIngestionError` values are mapped to a versioned `ApiError` shape with
code, safe message, details and correlation ID. Tracebacks remain in protected
technical logs. CLI uses the same code mapping to stable exit codes.

## Security boundary

Local mode binds to `127.0.0.1` only. Server mode requires authentication, role checks,
CORS allowlisting, HTTPS termination guidance, rate limiting and audit logs. Exchange
secrets are managed by a backend secret provider and never enter frontend storage,
manifests or logs.

## Consequences

### Positive

- one domain implementation for CLI and Web;
- stable typed contracts and OpenAPI;
- durable jobs survive browser and API restarts;
- existing Dataset Registry remains authoritative;
- current backend idempotency and checkpoints are reused;
- frontend can be developed without exchange-specific logic.

### Costs

- the current direct `argparse` CLI must be migrated behind the application facade;
- ingestion needs a read-only progress observer hook;
- a durable job store and recovery coordinator are new infrastructure;
- package metadata gains optional CLI/API development dependencies;
- frontend/backend compatibility must be tested and released together.

## Rejected alternatives

### Separate CLI and Web orchestration

Rejected because it duplicates command validation, idempotency, job state and error
mapping and creates inconsistent behavior.

### Frontend calling exchange APIs

Rejected because it bypasses validation, storage, audit, rate-limit handling and
security boundaries.

### Treating HTTP requests as jobs

Rejected because ingestion is long-running, resumable and must survive disconnects and
server restarts.

### Reusing the Dataset Registry as a job store

Rejected because datasets are immutable registered artifacts while jobs are mutable
operational records with different retention and recovery rules.

### Polling every second

Rejected as the primary progress mechanism because SSE provides ordered reconnectable
events with lower overhead. Coarse fallback polling may be used only when SSE is
unavailable.
