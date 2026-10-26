# Intermediate audit after rounds 23–24

## Decision
**Continue.** Long-history visibility is bounded only when explicitly configured, and the default public history contract remains unchanged.

## Changes
- Optional bounded base history for module views.
- Preflight against declared module warm-up requirements.
- Existing HTF `max_history` exercised together with the base bound.
- Repeatable multi-timeframe load and deterministic-run gate.

## Public contracts
One optional `DataConfig.max_base_history` field was added with default `None`. Existing modules and configurations observe unchanged full-history behavior.

## Evidence
- Full suite: 186 tests passed; branch coverage: 83%; elapsed=3.84 maxrss_kb=64260.
- Load: 30,000 bars, 5 modules, 5m/15m/1h, 3 runs.
- Median: 2.9710s; throughput: 50489 module calls/s.
- Peak RSS: 100,384 KiB; checkpoint: 1084 bytes.
- Repeated run identity: True (`run-446dd7abc33ba7f6`).

## No-look-ahead / determinism
A bounded view is still an O(1) frozen end boundary and never gains future bars. Run hashes include the data config, so bounded/unbounded configurations cannot be confused.

## Residual risks
The batch backtest still materializes input candles and retains complete journal/equity evidence. Total process memory is therefore not constant with dataset length. A streaming research mode would require a separately designed metrics/event-retention policy and is not smuggled into this hardening change.
