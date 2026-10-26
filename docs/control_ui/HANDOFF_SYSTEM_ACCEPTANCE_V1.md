# Control UI Handoff — Durable System Acceptance V1

Owner Issue: #382  
Owner PR: #474  
Control UI parent: #371

This document describes the target adapter. The owner issue stays open until exact-head CI and production phase composition are accepted; final accepted SHA/run IDs are recorded in Issue #382 / PR #474.

## Canonical owner

Control Plane must consume the application owner; GitHub Actions status is never application runtime truth.

Core contracts:

```text
SystemAcceptanceOwner
SQLiteAcceptanceStore
SystemAcceptanceDaemon
AcceptanceExecutorRegistry
AcceptanceRunSpec
AcceptanceRunSnapshot
AcceptanceMutationRequest / Result
AcceptanceCheckpoint
PhaseExecutionContext / PhaseOutcome
CheckpointedSoakExecutor
PinnedSystemAcceptanceHttpServer
```

`PinnedSystemAcceptanceHttpServer` is the canonical Control Plane transport. The lower-level base HTTP class is not approved for Control UI composition because exact candidate identity must come from trusted owner/server configuration.

## Trusted candidate identity

At owner process startup, the pinned transport receives the exact full-RC Git SHA from trusted deployment/configuration. Browser JSON cannot provide or override it.

The transport also fixes execution mode to `PAPER`. Browser `mode` and `full_rc_sha` fields are rejected.

START may contain only:

```text
request_id
spec.dataset_id
spec.dataset_digest
spec.strategy_id
spec.strategy_digest
spec.config_digest
spec.enable_soak_24h
spec.enable_soak_72h
```

## Durable run projection

Control UI may render only the owner snapshot:

```text
acceptance_run_id
status
phase
created_at
started_at
updated_at
completed_at
active_test_time
wall_clock_elapsed
restart_count
checkpoint
resources
warnings
error
next_action
evidence
immutable candidate identity
```

`active_test_time` and `wall_clock_elapsed` must remain separate in UI and CLI.

## Persistence / idempotency

SQLite owner persistence uses WAL, synchronous FULL and `BEGIN IMMEDIATE` mutations.

- request ID is durable;
- exact completed duplicate replays;
- same request ID with different mutation/operator/spec conflicts;
- an uncertain `IN_PROGRESS` mutation is not re-executed after restart;
- only one non-terminal acceptance run may exist;
- phase side-effect attempt identity is persisted before execution;
- restart recovery calls `recover()` with the same attempt ID rather than starting a second side effect.

Control Plane must never open/migrate owner SQLite or recreate this state machine.

## Phases / statuses

Phases:

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

Statuses:

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

24h/72h are modelled; 72h requires 24h. Activation policy remains owner configuration/run spec, not a browser timer.

## Browser-independent continuation

`SystemAcceptanceDaemon` owns progress. Closing Dashboard has no effect on a run. Reopening reads the same `acceptance_run_id` and current persisted progress.

Control Plane restart likewise must not become a cancellation signal. The owner daemon/process continues independently; after owner restart it performs lease/checkpoint recovery.

## Soak semantics

`CheckpointedSoakExecutor` advances active time only for successfully persisted healthy slices. It does not infer progress from wall clock.

- healthy slice: bounded active time increment;
- unhealthy slice: zero active time;
- probe exception: zero active time, bounded warning code;
- process/host downtime: zero active time;
- restart: resume last persisted checkpoint;
- tampered checkpoint: `RECOVERY_REQUIRED`.

Default targets are 2h / 24h / 72h and are owner-side constants, not browser input.

## Resources / evidence / privacy

Resource samples contain bounded timestamp/CPU/RAM/disk-free values only.

Evidence is reference-only:

```text
kind
digest
reference
```

References are opaque identifiers, not filesystem paths or URLs. Warnings/errors/next actions are bounded owner codes. No raw environment, host username, market-data dump, stack trace, private path or secret is projected.

## HTTP adapter

Reader:

```text
GET /acceptance/v1/current
```

Operator:

```text
POST /acceptance/v1/start
POST /acceptance/v1/stop
POST /acceptance/v1/cancel
POST /acceptance/v1/retry
```

Loopback only; distinct downstream reader/operator credentials; `Cache-Control: no-store`; owner assigns trusted operator identity.

## Error taxonomy

```text
INVALID_REQUEST
UNAUTHORIZED
IDEMPOTENCY_CONFLICT
RUN_ALREADY_ACTIVE
RUN_NOT_FOUND
INVALID_TRANSITION
RECOVERY_REQUIRED
STORAGE_FAILURE
OWNER_FAILURE
PHASE_FAILURE
PHASE_BLOCKED
CANCELLED
PAPER_ONLY
```

Raw owner/storage/executor exceptions must never be returned by the Control Plane.

## Control UI capability after owner acceptance

Acceptance workspace may show current run, every enabled phase, resources, checkpoint freshness and evidence. Start/Stop/Cancel/Retry are typed operator actions with explicit confirmation and request ID.

No UI timer may manufacture soak progress. No GitHub workflow status may manufacture run progress. No browser close may stop the run.

## Remaining owner acceptance requirement

Before #382 closes, production composition must explicitly provide authoritative executors for required non-soak phases. Missing phase adapters must produce deterministic `PHASE_BLOCKED`; there is no shell-command or GitHub-workflow fallback.
