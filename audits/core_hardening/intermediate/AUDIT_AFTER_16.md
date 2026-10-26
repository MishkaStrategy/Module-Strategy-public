# Intermediate audit after rounds 15–16

## Decision
**Continue.** Accounting reconciles and the stale reduce-only lifecycle defect is fixed.

## Changes and invariants
- Added full ledger reconciliation for scale-in, partial close and final close.
- Outstanding reduce-only orders are cancelled when their owning position closes.
- A stale exit can no longer block or alter a future position.

## Public contracts
No public API, DTO, import path, event ordering rule, feature semantics or checkpoint schema changed.

## Evidence
- Collected tests: 0; full suite passed.
- Branch coverage: 83%.
- Elapsed: 1.52s; max RSS: 52,892 KiB.
- No-op benchmark: 20,000 bars × 10 modules = 4.3630s.

## Backward compatibility
Valid reduce-only orders while a position remains open behave unchanged. Only orders without an owning position are now fail-closed.

## Determinism / no-look-ahead / replay
No look-ahead surface added. Cancellation order is stable by pending-order insertion order and is emitted into the append-only audit trail.

## Residual risks
The current core models one net position. Hedge-mode portfolio accounting remains outside the v0.4 contract and is not claimed.
