# Round 08 — validated immutable position snapshots

- Goal: ensure the read-only position DTO exposed to modules/reconciliation cannot carry invalid accounting or time values.
- Baseline: `PositionSnapshot` was frozen but performed no runtime validation.
- Change: identity/enum, finite positive quantity/price, aware UTC entry time, optional protection prices, PnL, setup ID and exact non-negative fill-count validation.
- Tests: adversarial field matrix, timezone normalization and frozen-instance enforcement.
- Downstream usability: every module sees one validated immutable representation rather than a silently malformed portfolio state.
- Compatibility: snapshots produced by the existing portfolio already satisfy these invariants.
