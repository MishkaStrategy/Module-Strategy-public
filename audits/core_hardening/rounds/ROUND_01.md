# Round 01 — data and time boundary invariants

- Rejected zero-length parsed timeframes at `Candle` construction.
- Rejected conflicts between configured base interval and declared candle timeframe.
- Added regression tests for both fail-closed paths.
- Public DTO layout unchanged.
