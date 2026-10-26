# Intermediate audit after rounds 07–08

## Decision

**Continue.** Reconciliation and position snapshot outputs are internally consistent, immutable, deterministic and compatible with the broker-generated state.

## Changes

- Reconciliation flags require real booleans and exactly match recorded mismatches.
- Every mismatch mandates a safe-halt verdict; IDs are nonblank and unique.
- Additive `issue_count` exposes a stable mismatch counter.
- `PositionSnapshot` validates identity, side, time, prices, PnL and fill counters.

## Public contracts

Existing fields and positional prefix remain unchanged. `issue_count` is additive. Valid broker/reconciliation outputs preserve their serialized values; malformed direct DTO construction now fails.

## Evidence

- Full repository suite: **356 passed**.
- Coverage: `TOTAL                                3298    384   1084    248    85%`.
- Ruff / strict mypy / compileall: pass.
- Test peak RSS: **72,944 KiB**.
- Event-loop median: **1.1130 s**, deterministic=True.
- Multi-timeframe median: **1.5437 s**, peak RSS **79,192 KiB**, checkpoint **1173 bytes**.

## Determinism / no-look-ahead / recovery

Reconciliation identity collections and issue counts are deterministic. No market-time behavior changed. Position snapshots normalize entry time to UTC. Checkpoint/replay suites pass.

## Compatibility

Portfolio-produced snapshots and `reconcile_execution_state` already satisfy all new invariants. No downstream migration is required.

## GitHub workflow status

Remote v0.4.1 remains green; candidate v0.4.2 has not yet been pushed. Self-hosted runner validation remains pending.

## Residual risks

Real venue reconciliation and operational repair actions remain outside this universal DTO hardening scope.
