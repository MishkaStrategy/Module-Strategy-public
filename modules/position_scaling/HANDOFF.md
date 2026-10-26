# Handoff — Position Scaling & Adjustment 0.2.2

## Purpose

Deterministically plan and validate changes to an already-open position: averaging down, pyramiding, fixed/weighted DCA, scale-out, rebalance and stop-scaling.

## Target and public surface

- target folder: `modules/position_scaling`;
- package: `modular_strategy_position_scaling`;
- entry points: `position_scaling_planner`, `position_scaling_approval`;
- Core: 0.4.x, locally validated with 0.4.1;
- Module API: 1.0;
- state schema: 4.

## Feature contract

Planner produces:

- `position_scaling.plan`;
- `risk.request` for increases;
- `position_scaling.status`.

Approval consumes:

- `position_scaling.plan`;
- `risk.decision` for increases.

Approval produces:

- `position_scaling.approved`;
- `position_scaling.rejected`.

Planner optionally consumes setup validity, market regime, ATR, instrument rules, portfolio risk, scaling context and terminal approval/rejection feedback.

## Dependency order

`open factual position/context → position_scaling_planner → Risk Sizing → position_scaling_approval → executor/OrderIntent → factual fill → planner reconciliation`

Exit actions have priority over scale-in. The module never places orders, changes portfolio state or confirms fills.

## Installation and tests

```bash
python -m pip install -e ./modules/position_scaling[dev]
python -m pytest modules/position_scaling/tests
ruff check modules/position_scaling
mypy --strict modules/position_scaling/src/modular_strategy_position_scaling
python -m build modules/position_scaling
```

## Files to add

Add the entire `modules/position_scaling/` package and `.github/workflows/position-scaling-hardening-ci.yml`. Do not replace Core, Feature Store, registry, executor or neighboring modules.

## Integration scenarios

1. full Risk approval and factual fill;
2. partial Risk approval and partial/final fills across restart;
3. Risk rejection;
4. position closed or changed before execution;
5. exit pending or data gap after approval;
6. duplicate/conflicting feedback;
7. checkpoint/restore with tuple-like Core state;
8. long/short and derivative contract-size notional.

## Change request

PS-CCR-001 requests typed adjustment-aware RiskRequest semantics. The schema-1 metadata workaround is evidence only and must not be represented as complete portfolio-risk enforcement.

## Status

Local hardening and 30 rounds are complete. Final status is conditional on published-commit self-hosted CI and PS-CCR-001 resolution/acceptance. No merge is authorized.
