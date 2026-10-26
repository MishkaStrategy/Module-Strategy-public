# Round 02 — duplicate, ordering and aggregation stream identity

- Added stable data-validation reason codes for empty, duplicate, out-of-order, mixed-symbol, mixed-timeframe and gap failures.
- Timeframe aggregation now rejects mixed symbol/timeframe streams and non-increasing timestamps.
- Added adversarial regression tests.
