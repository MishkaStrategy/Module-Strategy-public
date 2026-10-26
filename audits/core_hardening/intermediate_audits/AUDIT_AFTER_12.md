# Intermediate audit after rounds 11–12

- Critical defect fixed: TAKE_PROFIT orders previously shared STOP trigger direction.
- Order DTO and public enum values: unchanged.
- Fee/slippage policies: unchanged except missing position exit-fee accumulation is corrected.
- Same-bar policy: unchanged and remains explicit.
- Tests: full suite green, including existing partial-fill/reduce-only regressions.
- Backward compatibility: valid order requests behave as intended; only incorrect TP trigger behavior changed.
- Determinism/no-look-ahead: preserved.
- Decision: continue.
