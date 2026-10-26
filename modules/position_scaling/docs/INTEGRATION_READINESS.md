# Integration Readiness

## Ready aspects

- standalone package under `modules/position_scaling`;
- frozen Core 0.4.2 / Module API 1.0 public contracts only;
- deterministic planner and approval modules;
- state schema 4 with legacy migration and integrity fingerprint;
- wheel/sdist, public imports, PEP 561 and two entry points;
- dependency order planner → Risk → approval;
- typed adjustment-aware `risk.request` and `risk.decision` flow;
- projected whole-position risk enforcement by Risk Sizing 1.2.0;
- no direct execution, portfolio mutation or stop weakening;
- typed approved/rejected downstream payloads;
- exact-head tests, coverage, static analysis, clean install, security, SBOM and benchmark evidence.

## Integration order

1. install and freeze Core 0.4.2;
2. register `position_scaling_planner` after open-position and context producers;
3. pass the typed adjustment-aware request to Risk Sizing 1.2.0;
4. register `position_scaling_approval` after Risk;
5. connect only the Risk-approved adjustment to the execution or OrderIntent adapter;
6. reconcile only factual `PositionSnapshot` changes;
7. integrate Position & Exit Management with exit priority over additions.

## PS-CCR-001 closure

PS-CCR-001 is closed. Risk Sizing 1.2.0 types and independently enforces projected-position quantity, notional, leverage, margin and stop-risk geometry. Pinned cross-module run `29866061235` verified:

`position_scaling.plan → risk.request → risk.decision → position_scaling.approved`

The final Position Scaling approval did not exceed the Risk-approved delta, and typed request, typed decision and legacy evidence agreed exactly.

## Remaining integration boundary

This component is ready for staged integration review, not production or live deployment by itself. Paper execution, factual fills, persistence, venue reconciliation, operational monitoring and full-chain recovery remain outside this component checkpoint.

## Prohibited interpretation

This candidate does not prove that averaging, DCA or pyramiding has trading edge. Strategy parameters require instrument-specific backtests, walk-forward validation, paper execution and portfolio-level risk review.
