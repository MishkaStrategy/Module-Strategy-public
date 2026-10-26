# Research Runner checkpoint 02 — durable local scheduling

## Scope

Checkpoint 02 adds the execution-control layer above the existing official
Backtest Engine adapter. It does not implement strategy, order, fill, position,
or metric logic.

## Implemented

- SQLite state repository with a versioned schema, foreign keys, atomic
  `BEGIN IMMEDIATE` writes, deterministic run ordering, and idempotent
  experiment/job registration.
- Explicit run state machine with audited transitions and append-only state
  events.
- Durable worker leases, heartbeat extension, stale-lease recovery, and bounded
  retry attempts.
- Typed retry and resume policies with deterministic exponential backoff.
- Bounded local thread scheduler that invokes only
  `ModularBacktestEngineAdapter.execute`.
- Cancellation of pending work while already-running Backtest Engine calls are
  allowed to drain safely.
- Reversible canonical JSON for safe persistence of Decimal, float, UTC
  datetime, timedelta, and Path values without arbitrary object loading.

## Safety invariants

1. A run can be claimed by only one worker at a time.
2. A result is accepted only from the active lease owner and only for the exact
   requested run ID.
3. Successful runs are never executed again during resume unless a later
   artifact verification marks them corrupted.
4. Retry timing is persisted and survives process restart.
5. Worker/completion order cannot change run identities or returned result
   ordering.
6. Terminal state transitions are rejected rather than silently overwritten.

## Local evidence

- 98 Research Runner tests passed.
- 92% branch-aware package coverage.
- Python compileall passed.
