# Core Change Requests

The module does not require a strategy-specific Core source change.

## Closed request

- **PS-CCR-001 — adjustment-aware RiskRequest semantics: CLOSED**

Risk & Position Sizing 1.2.0 now accepts typed whole-position adjustment geometry and independently enforces projected quantity, notional, leverage, margin and stop-risk limits. The pinned cross-module gate verified:

`position_scaling.plan → risk.request → risk.decision → position_scaling.approved`

See `CORE_CHANGE_REQUEST.md` for the historical request, implemented contract, compatibility rules and closure evidence.
