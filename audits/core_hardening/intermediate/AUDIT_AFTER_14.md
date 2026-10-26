# Intermediate audit after rounds 13–14

## Decision
**Continue.** The OHLC ambiguity defect is fixed and regression coverage is clean.

## Changes and invariants
- Same-bar bracket replay is allowed only when the entry is deterministically at the candle open.
- Limit/triggered entries no longer consume pre-fill OHLC extremes.
- Adverse stop-gap behavior is covered for long and short positions.
- Explicit stop/target ambiguity policy remains deterministic.

## Public contracts
No public contract, DTO, feature key, import path, checkpoint schema or module callback changed.

## Evidence
- Full suite: 166 passed.
- Branch coverage: 83%.
- Elapsed: 1.51s; max RSS: 54,168 KiB.
- No-op benchmark: 20,000 bars × 10 modules = 4.4007s.

## Backward compatibility
Existing market-entry same-bar behavior remains available under the same opt-in flag. Safer behavior applies only to previously ambiguous intrabar fills.

## No-look-ahead and determinism
Improved: the engine no longer assumes an intrabar sequence not present in OHLC data. Repeated policy tests are deterministic.

## Restart/replay
No persistence schema or replay event semantics changed.

## Residual risks
Without tick/intrabar data, the engine cannot know the exact path. Optimistic stop-limit same-bar behavior remains explicitly opt-in and documented by configuration.
