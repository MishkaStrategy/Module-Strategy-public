# Known limitations and remaining release blockers

## Accepted core limitations

- Module API remains draft pending real downstream executor cross-audit.
- Batch backtests retain input bars, journal and equity evidence; total memory is not fully streaming/bounded.
- Reconciliation is a deterministic foundation, not a concrete venue repair implementation.
- Intrabar OHLC ambiguity remains policy-based because bar data cannot reveal the real path.
- Local vulnerability lookup required remote CI due unavailable DNS.

## Production blockers outside this core hardening scope

- Actual Entry Model integration and audit.
- Actual Protection & Target integration and audit.
- Actual Risk & Position Sizing integration and audit.
- Actual Position & Exit Management integration and audit.
- Production paper execution adapter.
- Production live exchange adapter.
- End-to-end restart/reconciliation against real pending orders and open positions.
- Kill switch and operational safety system tests.
- Unified application CLI and production runbooks.
- Multi-year all-module executor-chain load test.
- Final stable Module API freeze and migration policy.

No v1.0.0 tag, stable release or production-live claim is authorized.
