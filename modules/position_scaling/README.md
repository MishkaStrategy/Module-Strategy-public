# Position Scaling & Adjustment 0.2.2

Standalone typed module package for **Modular Strategy Core 0.4.x / Module API 1.0**.

The module manages deterministic changes to the size of an already-open position:

- averaging down;
- pyramiding;
- fixed and weighted DCA;
- strategic scale-out;
- rebalance to target quantity, notional or equity allocation;
- explicit termination of further scaling.

It does **not** place exchange orders, modify a portfolio directly, round exchange quantities, confirm fills, weaken stops, or replace Position & Exit Management.

## Pipeline

```text
Open Position
  → PositionScalingPlannerModule
  → position_scaling.plan + risk.request
  → Risk & Position Sizing
  → risk.decision
  → PositionScalingApprovalModule
  → position_scaling.approved | position_scaling.rejected
  → execution adapter / OrderIntent producer
```

The split planner/approval design works with the existing deterministic same-bar dependency pipeline without changing Core.

## Public imports

```python
from modular_strategy_position_scaling import (
    PositionScalingPlannerModule,
    PositionScalingApprovalModule,
    PositionAdjustmentPlan,
    ApprovedAdjustment,
    RejectedAdjustment,
    PositionScalingConfig,
    ScalingRule,
)
```

## Entry points

- `modular_strategy.modules:position_scaling_planner`
- `modular_strategy.modules:position_scaling_approval`

## Safe defaults

Scaling is disabled until explicitly enabled. Averaging, pyramiding, fixed DCA, weighted DCA, rebalance and martingale-like sizing are disabled independently. Additions fail closed on unknown liquidation distance, unknown margin usage, invalid setup, gap, pending exit, conflicting pending order, invalid/weakening stop or stale risk decision.

## Local validation snapshot

- Python 3.13.5;
- Core 0.4.1, Module API 1.0;
- 228 tests passed;
- 90% branch-aware combined coverage after hardening;
- Ruff and Ruff format passed;
- strict mypy passed;
- compileall passed;
- wheel and sdist built;
- clean environment passed using the local module wheel and Core 0.4.1;
- real-Core BacktestEngine integration smoke passed.

Parameters do not prove trading edge. Averaging, DCA and pyramiding policies require instrument-specific backtest, walk-forward validation and portfolio-level risk review.

GitHub CI is evaluated from the published hardening branch; do not infer CI status from this local snapshot.
