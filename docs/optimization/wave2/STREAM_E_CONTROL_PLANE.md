# Optimization Wave 2E — Market Data Control Plane SLO

## Identity and boundary

- Repository: `MishkaStrategy/Module-Strategy`
- Upstream stream: Optimization Wave 2D — Dataset Registry and LocalStorage SLO
- Upstream Draft PR: `#310`
- Accepted upstream head: `2d1268040295a894168ac8564f3d38d83746961e`
- Frozen handoff: `modules/market_data_ingestion/reports/dataset-storage-handoff-e.json`
- Base branch: `optimization/wave2-dataset-storage-slo`
- Working branch: `optimization/wave2-market-data-control-plane-slo`
- Draft PR: `#313`

This stream remains Draft. It does not authorize merge, Ready transition, auto-merge,
force-push, shared-history rebase, release, deployment, public server binding, production
dataset mutation, credentials or live trading.

## Ownership inventory

### Stream E implementation ownership

- `modules/market_data_ingestion/src/market_data_application/api.py`
- `modules/market_data_ingestion/src/market_data_application/server.py`
- `modules/market_data_ingestion/src/market_data_application/cli.py`
- `modules/market_data_ingestion/src/market_data_application/application.py`
- `modules/market_data_ingestion/src/market_data_application/durable_application.py`
- `modules/market_data_ingestion/src/market_data_application/composition.py`
- `modules/market_data_ingestion/src/market_data_application/catalog.py`
- `modules/market_data_ingestion/src/market_data_application/job_store.py`
- `modules/market_data_ingestion/src/market_data_application/lease_store.py`
- `modules/market_data_ingestion/src/market_data_application/control_plane_http.py`
- `modules/market_data_ingestion/src/market_data_application/control_plane_runtime.py`
- `modules/market_data_ingestion/src/market_data_application/control_plane_security.py`
- `modules/market_data_ingestion/src/market_data_application/control_plane_store.py`
- directly related tests, scripts, reports and this document

### Read-only upstream ownership

Stream E does not change Stream D Registry schema, immutable storage publication,
recovery semantics, checksum or lineage validation. It also does not change downloader,
exchange adapters, normalized partition semantics, Backtest/Core, Strategy, Router,
Signal, Paper Runtime, shared workflows or root tooling.

## Frozen Stream D consumption

Control Plane composition now uses the accepted additive entrypoints:

- `VersionedSQLiteDatasetRegistry` through `open_versioned_registry`;
- `RecoverableLocalStorage` through `open_recoverable_storage`.

The application does not write Registry tables directly, alter durability PRAGMAs,
disable foreign keys, bypass schema checks, mutate immutable manifests, delete immutable
storage files, auto-delete unregistered manifests, bypass checksums or lineage, weaken
root containment or symlink rejection, or weaken atomic publication.

## Lifecycle inventory

### Startup and dependency injection

`build_application_context` creates one storage root and composes recoverable storage,
versioned Registry, artifact storage, durable job store, lease store, repair-confirmation
store, ingestion service and configured adapters. `MarketDataApplication` then owns the
job executor and transport-neutral service layer. FastAPI and Typer consume the same
application instance and therefore share job identities, Registry state and error
semantics.

### Request lifecycle

FastAPI validates correlation, authentication, authorization and request limits before
calling application services. Dataset list/detail open short-lived read-only SQLite
connections and close them in `finally`. Hardened administrative operations append an
HMAC-chained audit record through a short-lived Control Plane SQLite connection. Success,
authentication failure, authorization failure and validation failure paths are exercised
under explicit TestClient context management.

### Durable job lifecycle

Job creation derives a deterministic identity from the normalized request and
idempotency key. SQLite persists the job before scheduling. State transitions,
progress, terminal errors and cancellation append ordered durable events. Worker leases
fence concurrent execution. Restart recovery discovers non-terminal durable jobs,
reacquires execution ownership and reaches a terminal state with the no-network fake
adapter workload.

### SSE and replay

REST replay and SSE use the durable `job_events` journal. `after_event_id` is exclusive;
returned IDs are strictly greater than the cursor, unique and ordered. Terminal events
remain replayable. SSE admission permits are released when streams close.

### CLI lifecycle

Typer commands use the same application service layer as HTTP. Dataset identity, public
storage identity, job status, events and failure exit behavior are therefore shared.
Repeated in-process CLI invocation is included in the benchmark and cleanup smoke.

### Shutdown and temporary resources

`MarketDataApplication.shutdown` joins the executor. TestClient is always used as a
context manager. Control Plane audit connections close in `finally`; Registry/catalog
connections and benchmark lock probes also close explicitly. Final probes measure file
descriptors, SQLite descriptors, threads, async tasks, temporary bytes and ResourceWarning.

## SQLite connection ownership defect

The accepted Stream D artifact contained 118 entries in the pytest warning summary.
Exact parsing classified them as:

- 117 `ResourceWarning: unclosed database` records;
- one unrelated Starlette TestClient deprecation warning.

The 117 database warnings were attributed to:

- 51 Control Plane server integration groups;
- 47 Control Plane security-core groups;
- 18 Control Plane API integration groups;
- one dataset-operations attribution.

The first remediation removed 111 warnings caused by the production-store lifetime
mismatch: `SQLiteControlPlaneStore._connect` returned a raw `sqlite3.Connection`, while
SQLite's connection context manager commits or rolls back but does not close. Stream E
replaces it with an explicit context manager whose `finally` block always closes the
connection.

Exact-head CI then isolated the remaining six warnings to test-owned corruption/tamper
fixtures in `test_control_plane_security_core.py` and
`test_control_plane_security_edges.py`. Those fixtures used the same raw
`with sqlite3.connect(...)` pattern and now wrap their connections with
`contextlib.closing`. No warning filter, ResourceWarning suppression or durability
weakening was introduced.

Regression coverage includes successful operations, compare-and-swap failure, repeated
authentication failure, validation failure, successful requests, TestClient cleanup,
security tamper paths and FD stability. The post-fix warning count is zero.

## Public storage-path boundary

The immutable Registry descriptor still stores the canonical local `file://` storage URI.
The API and CLI public projection no longer exposes that absolute path. Local descriptors
are represented as the stable opaque identity:

`local://managed/<dataset-id>/v<version>`

Tests prove that API and CLI return the opaque value while the committed Registry row is
unchanged. Credential-bearing markers are also absent from measured responses.

## Benchmark harnesses

### Full Control Plane baseline

```bash
python scripts/benchmark_control_plane_slo.py \
  --dataset-counts 1,100,1000,10000 \
  --job-counts 1,100,1000 \
  --samples 10
```

The harness is local and no-network. It uses FastAPI TestClient, Typer CliRunner,
SQLite WAL and the deterministic fake adapter. It measures health, dataset list/detail,
missing and invalid requests, authorization rejection, concurrent readers, job
submission, duplicate identity, progress updates, cancellation, failure, event replay,
SSE, CLI commands, restart recovery, RSS, FD, warnings, DB/WAL bytes and temporary bytes.

### Contention and resource probe

```bash
python scripts/benchmark_control_plane_contention.py --attempts 100
```

The probe runs eight threads with 20 writers and 80 readers, then deliberately holds a
SQLite write transaction for 50 ms. It reports p50/p95/p99, availability, busy and
unexpected errors, observed/estimated lock wait, open SQLite descriptors, thread/task
growth and ResourceWarning. SQLite descriptor counting supports both Linux
`/proc/self/fd` and macOS `/dev/fd`.

## Measured baseline

Environment: Python 3.13.5, Linux x86_64, five CPUs, localhost/TestClient only, no
external exchange API and no credentials.

| Metric | Workload | Observed |
| --- | --- | ---: |
| Dataset list p95/p99 | 200-item page, 10,000 datasets | 35.18 / 35.18 ms |
| Dataset detail p95/p99 | latest lookup, 10,000 datasets | 1.69 / 1.69 ms |
| Concurrent API p95/p99 | eight mixed list/detail readers | 165.79 / 169.61 ms |
| Concurrent reader availability | eight readers | 100% |
| Job submission p95/p99 | 1,000 durable jobs | 5.70 / 7.54 ms |
| Job submission throughput | 1,000 durable jobs | 293.5 jobs/s |
| Progress update p95/p99 | 20 durable updates | 5.66 / 6.19 ms |
| REST replay p95 | cursor replay | 2.37 ms |
| SSE replay p95/p99 | cursor replay | 4.57 / 4.57 ms |
| CLI dataset list p95 | 10,000 datasets | 32.90 ms |
| Restart startup | durable recovery workload | 2.54 ms |
| Recovery call | one non-terminal job | 1.62 ms |
| Recovery to terminal | fake-adapter execution | 180.59 ms |
| Peak RSS growth | complete baseline | 41.70 MiB |
| FD growth | after shutdown and GC | 0 |
| ResourceWarning | complete baseline | 0 |
| Temporary bytes | complete baseline | 0 |

Contention smoke measured 100% availability, zero busy errors, zero unexpected errors,
p95 10.54 ms, p99 56.44 ms, a 56.58 ms blocked writer under the deliberate 50 ms hold,
zero open SQLite descriptors after cleanup, zero thread/task growth and zero warnings.

## Semantic evidence

The measured and regression workloads establish:

- committed Registry state is the only dataset state visible through API/CLI;
- API and CLI expose compatible dataset and job identities;
- duplicate job handling is deterministic;
- progress is monotonic;
- cancellation and failure are terminal and durable;
- restart recovers durable non-terminal jobs;
- SSE cursor behavior and ordering are exact;
- shutdown preserves committed job state;
- local paths and credentials are not exposed;
- no raw market-data payload is published by Control Plane list/detail/status endpoints;
- post-cleanup SQLite connection, warning, task and orphan-job counts are zero.

Stream D continues to own corruption, immutable conflict, symlink, traversal, schema
downgrade, checksum and lineage fail-closed behavior. Stream E consumes rather than
reimplements those contracts.

## Proposed SLO and profiling gate

Machine-readable thresholds are in
`modules/market_data_ingestion/reports/control-plane-slo-proposal.json`.

All measured baseline thresholds pass. No repeatable production performance bottleneck
was established, so Stream E does not add a cache, index, pool or unbounded in-memory
state. The only implementation changes are confirmed lifecycle and information-boundary
defect fixes.

A production optimization requires the same SLO to fail in three isolated repetitions on
the same environment. Profiling must then separate validation, Registry lookup,
serialization, job persistence, event replay, connection lifetime, transaction duration
and lock contention before a minimal change is proposed.

## Validation evidence

Local substantive validation includes:

- `python -m compileall` over source, scripts and tests;
- 115 focused Stream E tests passing with pytest unraisable warnings treated as errors;
- a follow-up 31-test security/server/benchmark run passing with `ResourceWarning` as an
  error after the final six fixture cleanup corrections;
- API, server, security core/edges, HTTP/runtime, catalog, transports, durable job store,
  leases, cancellation, measured progress, dataset operations and CLI workflow tests;
- benchmark and contention smoke;
- explicit connection-lifecycle and path-redaction regressions.

The local sdist environment cannot execute repository-level CLI parity because the root
matrix is outside the sdist. It also lacks optional `pyarrow` and two Stream D benchmark
scripts, so the entire module collection is delegated to exact-head repository CI. These
are environment limitations, not accepted passes.

## Evidence files

- `modules/market_data_ingestion/reports/control-plane-warning-baseline.json`
- `modules/market_data_ingestion/reports/control-plane-baseline-summary.json`
- `modules/market_data_ingestion/reports/control-plane-contention-smoke.json`
- `modules/market_data_ingestion/reports/control-plane-deterministic-repeat.json`
- `modules/market_data_ingestion/reports/control-plane-slo-proposal.json`

## Current verdict

Measurement and local remediation are complete. Final acceptance remains conditional on
substantive exact-head GitHub Actions passing and zero unresolved review threads while PR
`#313` remains Draft.
