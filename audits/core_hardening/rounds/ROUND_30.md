# Round 30 — integration and backward-compatibility readiness

## Goal
Demonstrate compatibility with the five audited analytical modules and the frozen v0.4.0 public surface while preparing, but not implementing, the four downstream executor modules.

## Evidence
- Session Range 0.1.0 loaded and state round-tripped.
- Breakout–Return 1.0.0 loaded and state round-tripped.
- FVG 0.1.0 loaded and state round-tripped.
- Liquidity Sweep 0.1.0 loaded and state round-tripped.
- Market Regime 1.0.0 loaded and state round-tripped.
- Dependency planning succeeds with required 15m, 4h and 1h timeframes.
- A machine-generated snapshot of the original v0.4.0 wheel is checked against the candidate at test time.
- Every original field and method parameter remains present with compatible ordering/defaults; only optional additive parameters are allowed.
- A source-boundary test verifies that no integrated strategy name or concept is branched on inside `src/modular_core`.

## Scope boundary
The executor probes live only in the test harness. They are not packaged as production Entry, Protection/Target, Risk or Position Management implementations.
