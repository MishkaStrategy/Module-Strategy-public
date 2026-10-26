# Backtest Control v1

## Scope

Backtest Control exposes the existing deterministic `modular_backtest` engine through the local,
paper-only Control Plane. The browser and terminal clients do not execute shell commands and do
not provide filesystem paths to the engine.

The server is the authority for:

- managed Backtest configuration identities;
- registered immutable dataset references inside those configs;
- durable job state;
- worker scheduling;
- result storage roots;
- result integrity inspection and A/B comparison.

## Safety boundary

A create request contains only a bounded `config_id`. The server resolves it as a regular YAML
file directly under `MODULAR_CONTROL_BACKTEST_CONFIG_ROOT`. Symlinks, missing files and paths
outside that root fail closed.

Control Plane Backtests require the resolved config to use a registered dataset identity
(`dataset_id` + `version`). Raw-path data configs are rejected by the managed adapter.

The config's own result directory is ignored for Control Plane execution. Results are forced under
`MODULAR_CONTROL_BACKTEST_OUTPUT_ROOT`. The durable SQLite store may retain the resolved result
path internally, but API contracts never return that path.

No Live-trading capability, private exchange credential, signed private request, arbitrary Python,
or shell-command surface is added by this contract.

## Authentication

Read operations use the existing optional Control reader token:

- `MODULAR_CONTROL_READER_TOKEN`

Backtest creation is a mutation and requires a separate operator token:

- `MODULAR_CONTROL_OPERATOR_TOKEN`

Backtest Control cannot be enabled with a managed config root unless the operator token is
configured. Tokens are never printed by `modular-control-api --check` and are not persisted in the
browser.

## Durable job model

`POST /api/control/v1/backtests` requires:

- `Authorization: Bearer <operator token>`;
- `Idempotency-Key`;
- body `{ "schema_version": 1, "config_id": "..." }`.

The SQLite job is committed as `QUEUED` before it is submitted to the server-owned executor.
Actor-scoped idempotency makes an identical retry return the same durable job. Reusing the key for
a different request fails with a typed conflict.

The local executor owns scheduling independently from the HTTP response lifecycle. On server
startup it re-enqueues persisted `QUEUED` jobs. Duplicate local submissions are collapsed while a
job is already scheduled.

If scheduling fails after durable creation, the API returns a retryable typed error and leaves the
job `QUEUED`; retrying the identical request with the same idempotency key can schedule it later.

## Progress and cancellation

The existing Backtest engine does not currently expose a measured per-run progress callback.
Therefore a running Control job reports an honest indeterminate `EXECUTING` phase. The UI must not
invent a percentage.

No cancel endpoint is exposed in v1. Cancellation will not be added until the engine provides a
cooperative cancellation contract that can prove a durable terminal `CANCELLED` state without
conflating cancellation and failure.

## Restart semantics

Persisted `QUEUED` jobs are recovered and re-enqueued on a normal Control Plane startup.

If the process stops while a job is already `STARTING`, `RUNNING` or `RECOVERING`, a new store
instance reconciles that job to an explicit retryable `FAILED` state with code
`BACKTEST_SERVER_INTERRUPTED`. The engine does not currently support resuming a partially executed
run, so the Control Plane must not claim that it does.

This v1 composition assumes one local Control Plane process owns a Backtest state database. The
SQLite store is durable and transactional, but there is not yet a cross-process lease/heartbeat
protocol for two simultaneous Control Plane processes sharing the same Backtest DB. Running two
owners against the same state path is unsupported and must not be presented as highly available.

## API

Read surfaces:

- `GET /api/control/v1/backtests?limit=...&offset=...`
- `GET /api/control/v1/backtests/{job_id}`
- `GET /api/control/v1/backtests/{job_id}/result`
- `POST /api/control/v1/backtests/compare`

Mutation surface:

- `POST /api/control/v1/backtests`

Result inspection delegates to the existing `inspect_result` integrity checks. A/B comparison
delegates to `compare_results` and then verifies that both returned run identities still match the
durable job result identities before projecting metric deltas.

Neither result inspection nor comparison returns managed filesystem paths.

## Native Dashboard workspace

The unified Dashboard exposes the accepted Backtest surface at `#/backtests`. It is a native
workspace inside the single Control Dashboard shell; it does not mount a second sidebar or a
standalone Backtest application.

The workspace provides:

- Backtest component health from the typed Control Plane adapter;
- a durable job table with config identity, server status, truthful progress phase and update time;
- creation by bounded server-owned `config_id` only;
- verified result metrics for successful durable jobs;
- A/B comparison of two distinct successful durable job identities;
- explicit safe error codes/actions without raw server bodies or filesystem paths.

The browser keeps both `Control reader token` and `Control operator token` in React memory only.
They are intentionally lost on reload. Durable jobs are server state, so re-authenticating after a
reload shows the same completed job instead of reconstructing it from browser storage.

A fresh create action generates an in-memory idempotency key. If the submission fails with a
retryable transport/server error, `Retry same request` reuses the same request and idempotency key;
it does not silently create a second operation.

The Dashboard does not expose a Cancel button, filesystem picker, arbitrary path field, shell
input or Live-trading mode. These absences are part of the v1 contract rather than unfinished UI
placeholders.

## Server configuration

- `MODULAR_CONTROL_BACKTEST_CONFIG_ROOT`
- `MODULAR_CONTROL_BACKTEST_OUTPUT_ROOT`
- `MODULAR_CONTROL_BACKTEST_STATE_PATH`
- `MODULAR_CONTROL_BACKTEST_WORKERS` (1..4, default 1)
- `MODULAR_CONTROL_OPERATOR_TOKEN`

`modular-control-api --check` reports only configuration state and bounded worker count. It never
prints secret values.

## v1 acceptance requirements

Before the Backtest Dashboard is treated as accepted:

1. Python 3.12 and 3.13 exact-head Control Plane gates must pass compile, Ruff, strict MyPy,
   component tests, lightweight-import boundary, paper-only safety scan, build and installed-wheel
   smoke.
2. The browser must use only typed Control Plane endpoints; no raw path or shell input may appear.
3. Reader and operator credentials must remain memory-only and disappear on reload.
4. A real-loopback browser test must create a durable run from a registered immutable dataset,
   prove that terminal server truth survives a browser reload/re-authentication, inspect a verified
   result, create a second distinct deterministic run and compare the two completed run identities.
5. Existing standalone `modular-backtest` CLI behavior remains unchanged.
