# Known Limitations and Residual Integration Risks

1. The module consumes the engine base timeframe; it does not aggregate candles internally.
2. Session boundaries must align with complete input candles. Partial boundary candles are rejected by design.
3. One instance owns all configured sessions because the current core allows one producer per feature key.
4. Nonexistent local DST boundaries reject the session date; ambiguous boundaries follow configured `dst_fold`.
5. The feature payload exposes float convenience values because Core 0.4.x candle/JSON contracts use floats; `width_decimal` retains exact width only. Extreme values that cannot be represented as finite floats are rejected.
6. A maximum of 64 session definitions and 1,024 tracked streams is enforced per instance.
7. The package was executed locally on Python 3.13.5. Python 3.12 compatibility is targeted by syntax, Ruff and mypy configuration, but a Python 3.12 interpreter was not available in this container.
8. This release candidate is not a trading strategy, execution adapter, paper/live engine, or exchange integration.
9. Linux GitHub Actions and repository-wide integration are intentionally deferred.
10. Downstream consumers that persisted exact v0.1 completed feature-ID strings must update; semantic feature keys are unchanged.

No Core Change Request is required.
