# Integration Contract

## Ownership

Position Scaling owns policy and deterministic adjustment planning. Risk owns permitted delta quantity and portfolio/instrument limits. Execution owns orders, exchange rounding and fills. Position/Exit owns exit priority and open-position management. Core owns orchestration, Feature Store, factual position state and checkpoint mechanisms.

## Plan DTO

`position_scaling.plan`, schema 1, includes adjustment/position/setup identity, symbol/side, action/direction/mode/stage, decision time/index, desired entry kind/price/quantity, current and projected position geometry, risk/notional budgets, stop/invalidation, expiration, reason, optional RiskRequest ID, source feature IDs and config fingerprint.

## Risk request

For an increase the planner publishes standard `risk.request` schema 1 with instrument/portfolio snapshots and metadata containing:

- `adjustment_id`, `position_id`, `opens_new_position=false`;
- current/requested/maximum/projected quantities;
- current/projected average price;
- current/projected risk;
- current/projected contract-size-adjusted notional.

The metadata is immutable/auditable but remains subject to PS-CCR-001.

## Approval DTO

`position_scaling.approved`, schema 1, embeds the exact plan and for increases the exact RiskDecision. Approved quantity is positive and capped by desired quantity, maximum quantity and RiskDecision quantity. Reductions carry no RiskDecision.

## Rejection DTO

`position_scaling.rejected`, schema 1, includes adjustment/position/symbol identity, typed reason, safe message, timestamp and optional RiskRequest identity.

## Time semantics

- decisions use the current completed candle close time;
- future features are invisible through Feature Store point-in-time snapshots;
- RiskDecision for an increase must be same-bar and schema compatible;
- terminal feedback remains valid through the immediate next bar to permit planner ingestion;
- expiration uses both closed-bar index and UTC timestamp.

## Lifecycle

`IDLE → ELIGIBLE → PLAN_CREATED/RISK_PENDING → APPROVED → ORDER_PENDING/PARTIALLY_FILLED → RECONCILED → COOLDOWN` with typed rejected, expired, invalidated, stopped and recovery states.

## Backward compatibility

- Core 0.4.x / Module API 1.0;
- planner state schemas 1–3 migrate to schema 4 where derivable;
- approval schemas 2–3 migrate to schema 4;
- no Core source or strategy-specific kernel change.
