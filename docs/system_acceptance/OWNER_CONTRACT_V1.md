# Durable System Acceptance Owner V1

Issue #382 requires application-level acceptance/soak truth that survives browser and Control Plane restarts. GitHub Actions are validation tooling only; workflow status is not acceptance runtime state.

## Ownership

The owner package is `applications/system_acceptance`.

Public contracts:

```python
from modular_system_acceptance import (
    AcceptanceCheckpoint,
    AcceptanceErrorCode,
    AcceptanceExecutorRegistry,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceMutationResult,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSnapshot,
    AcceptanceRunSpec,
    AcceptanceStatus,
    EvidenceReference,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    ResourceSample,
    SQLiteAcceptanceStore,
    SystemAcceptanceDaemon,
    SystemAcceptanceHttpServer,
    SystemAcceptanceOwner,
    SystemAcceptanceQuery,
    TrustedOperator,
)
```

The owner is PAPER-only. It has no exchange private-key, signed private request or real-order capability.

## Durable run model

A run persists:

- `acceptance_run_id`;
- `status` and current `phase`;
- created/started/updated/completed timestamps;
- `active_test_time` separately from wall-clock elapsed;
- restart count;
- durable checkpoint metadata;
- bounded resource samples;
- bounded warning codes;
- safe error/next-action codes;
- sanitized evidence digest/reference records;
- exact full-RC, dataset, strategy and configuration identities.

Phases are:

```text
ENVIRONMENT
DATASET
BACKTEST_A
BACKTEST_B
DETERMINISM
RESEARCH
RUNTIME_SMOKE
RECOVERY
SOAK_2H
SOAK_24H
SOAK_72H
FINAL_VERIFICATION
```

24h and 72h soaks are modelled but opt-in per run. Enabling 72h requires 24h. `FINAL_VERIFICATION` is always last.

Statuses are:

```text
QUEUED
STARTING
RUNNING
STOPPING
SUCCEEDED
FAILED
CANCELLED
RECOVERING
BLOCKED
```

## Persistence and idempotency

`SQLiteAcceptanceStore` is owner persistence and uses WAL, `synchronous=FULL` and `BEGIN IMMEDIATE` mutations. The Control Plane must never open or migrate this database itself.

Mutation request IDs are durable. START/STOP/CANCEL/RETRY have a fingerprint covering the typed mutation, trusted operator and immutable run identity. Semantics:

- first request is claimed before owner state changes;
- an exact completed duplicate replays the persisted result;
- the same request ID with a different fingerprint is `IDEMPOTENCY_CONFLICT`;
- an `IN_PROGRESS` mutation after uncertain transport/restart is `RECOVERY_REQUIRED` rather than re-executed;
- concurrent duplicate mutations across separate SQLite owner instances serialize through the database;
- only one non-terminal acceptance run may exist.

START creates a deterministic run ID from request identity. Duplicate START cannot create a second run.

## Phase execution and restart recovery

Phase business work is injected through an allowlisted `AcceptanceExecutorRegistry`, keyed by `AcceptancePhase`. There is no browser-provided command, path, executable, argv or shell string.

Before a phase side effect, the owner persists:

- phase attempt number;
- stable `attempt_id`;
- `IN_PROGRESS` execution state;
- owner instance lease and expiry.

The side effect then runs outside the SQLite transaction.

If the process dies or persistence fails after that side effect, the persisted attempt remains `IN_PROGRESS`. After lease expiry a replacement daemon marks the run `RECOVERING` and invokes the executor's `recover()` using the **same attempt ID**. It does not call a new `execute()` for that uncertain attempt.

This is the required no-duplicate-after-restart boundary. Individual phase executors must implement their own authoritative recovery against the supplied attempt/checkpoint identity before they can be registered for production acceptance.

## Browser-independent daemon

`SystemAcceptanceDaemon.run_forever()` first performs restart recovery, then repeatedly calls bounded `owner.step()` slices. Dashboard/browser lifecycle has no control over this loop.

Closing and reopening the Dashboard therefore reads the same `acceptance_run_id` and current owner state.

`SystemAcceptanceService` is a one-shot process composition around the daemon and canonical pinned HTTP transport. Repeated `start()` before shutdown is idempotent, but after `close()` or a failed startup the service object is terminal and a later `start()` is rejected. A real process restart must construct a fresh service/daemon/HTTP composition over the same durable SQLite owner state; this prevents a closed HTTP server or already-stopped daemon from being exposed as a falsely healthy restarted service.

STOP/CANCEL during an in-flight phase changes owner state to `STOPPING`; the durable stop/cancel flag is applied at the next safe phase boundary. The browser does not kill an owner process or abandon persistence.

## Active test time versus wall clock

`wall_clock_elapsed` is derived from run timestamps.

`active_test_time` advances only by bounded `active_seconds` reported by a successfully persisted phase slice. Long soak executors return `RUNNING` plus a durable checkpoint for each slice. Host downtime, expired leases, browser closure and time spent blocked outside accepted phase work do not automatically count as active test time.

A `RUNNING` phase outcome without a checkpoint is rejected. Per-slice active-time increments are bounded, preventing a single stale callback from crediting an unbounded soak duration.

## Checkpoints and freshness

A checkpoint contains only:

- phase;
- UTC `updated_at`;
- bounded opaque token;
- SHA-256 digest.

It contains no market data dump, filesystem path, host user or environment content. Consumers determine freshness from `updated_at` relative to their configured owner freshness threshold; they must not fabricate progress from wall-clock time when checkpoints stop advancing.

## Resources and evidence

Resource samples are bounded numeric CPU/RAM/disk-free values with timestamps. The owner retains only a bounded recent window.

Evidence is reference-only:

```text
kind
digest
reference
```

`reference` is a bounded opaque identifier, not a path or URL. Raw market data, environment dumps, host usernames, stack traces and private paths are forbidden.

Warnings, errors and next actions are bounded owner codes rather than arbitrary exception/user text.

## Safe loopback transport

`SystemAcceptanceHttpServer` is optional owner transport for Control Plane integration.

It binds only to loopback and requires distinct downstream reader/operator bearer credentials.

Reader endpoint:

```text
GET /acceptance/v1/current
```

Operator endpoints:

```text
POST /acceptance/v1/start
POST /acceptance/v1/stop
POST /acceptance/v1/cancel
POST /acceptance/v1/retry
```

The owner assigns the trusted operator subject. Browser JSON cannot provide operator identity, commands, paths or arbitrary test bodies. Responses use `Cache-Control: no-store`.

START accepts only immutable run identity/digest fields plus 24h/72h soak enable flags and `mode=PAPER`. STOP/CANCEL/RETRY accept only `request_id` and `acceptance_run_id`.

## Error taxonomy

The public owner taxonomy includes:

- `INVALID_REQUEST`;
- `UNAUTHORIZED` at transport boundary;
- `IDEMPOTENCY_CONFLICT`;
- `RUN_ALREADY_ACTIVE`;
- `RUN_NOT_FOUND`;
- `INVALID_TRANSITION`;
- `RECOVERY_REQUIRED`;
- `STORAGE_FAILURE`;
- `OWNER_FAILURE`;
- `PHASE_FAILURE`;
- `PHASE_BLOCKED`;
- `CANCELLED`;
- `PAPER_ONLY`.

Raw executor/storage exception text is never copied into the run's public error fields or HTTP response.

## Control UI handoff

After owner acceptance, #371 may project:

- Current Run;
- all enabled phases and current phase;
- run status and restart count;
- active test time and wall-clock elapsed separately;
- checkpoint timestamp/token/digest and freshness derived from timestamp;
- bounded resources/warnings;
- evidence references/digests;
- Start, Stop, Cancel and Retry through operator-authenticated typed adapters.

The Control Plane must remain an adapter. It must not duplicate the run state machine, schedule soak timers from the browser, use GitHub Actions status as application truth, or open owner SQLite directly.

## V1 limitation

This PR establishes the durable owner, recovery, transport and executor contract. Production phase executors must be registered only when each phase can provide an authoritative idempotent/recoverable implementation for its own subsystem. Missing executors deterministically block the phase instead of falling back to shell commands or GitHub workflow state.
