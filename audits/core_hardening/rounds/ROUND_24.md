# Round 24 — repeatable multi-timeframe load gate

Added a reproducible long-history benchmark with multiple modules and 5m/15m/1h streams. It records median throughput, peak RSS, checkpoint size and repeated run identity. Regression tests cover 5,000-bar deterministic repeats and default backward-compatible unbounded history.

Audit/equity retention remains intentionally complete and therefore grows with run length; this is documented rather than hidden behind a false bounded-memory claim.
