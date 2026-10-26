# Core / Cross-Component Change Request

## PS-CCR-001 — Adjustment-aware RiskRequest semantics

- **Status:** CLOSED
- **Original severity:** High integration blocker for fully compliant scale-in risk validation.
- **Requesting component:** Position Scaling & Adjustment 0.2.1+.
- **Implementing component:** Risk & Position Sizing 1.2.0.
- **Core impact:** no strategy-specific Core source change; the executor-chain contract harness must preserve and test the typed flow.

## Historical gap

The original `RiskRequest` schema represented an entry leg with entry price, stop loss, equity, instrument rules, portfolio aggregates and an optional risk fraction. It did not distinguish an existing position from a requested adjustment or express the complete projected position after that adjustment.

A scale-in is not a new trade in the portfolio-count sense. Correct validation must distinguish:

- opening a new position;
- adding to an existing position;
- replacing or reducing an already-approved delta;
- the current position from the requested delta;
- current and projected average price, quantity, notional, leverage, margin and stop risk.

Without those semantics, a risk calculator can charge an add against `max_positions` and can enforce limits against only the delta rather than the resulting whole position.

## Implemented contract

Risk & Position Sizing 1.2.0 implements typed adjustment-aware request and decision semantics while retaining compatibility for ordinary entry sizing. The typed request carries, at minimum:

- `request_id`, `adjustment_id`, `position_id` and `setup_id`;
- whether the request opens a new position;
- current quantity, average price, notional and open risk;
- requested delta quantity and price;
- projected quantity and weighted-average price;
- current and projected stop geometry;
- projected notional, leverage, margin use and total position risk;
- instrument, portfolio, freshness and correlation identity.

The typed Risk decision returns the approved delta and the projected whole-position evidence used for approval.

## Backward compatibility

- Ordinary schema-1 entry sizing remains supported.
- Adjustment-aware requests use explicit typed geometry rather than relying on unvalidated metadata.
- Existing `risk.request` and `risk.decision` namespaces remain stable.
- Legacy metadata is retained as auditable compatibility evidence and must agree with the typed fields.

## Verified behavior

The closure gate verifies:

- add at maximum position count without charging a new-position slot;
- partial approval of an adjustment delta;
- stale or mismatched position identity rejection;
- changed stop or average-price rejection;
- projected total notional, leverage, margin and risk limits;
- duplicate and conflicting requests after restart;
- typed request and typed decision agreement;
- final Position Scaling approval not exceeding the Risk-approved delta.

## Closure evidence

Pinned cross-module workflow run `29866061235`: **SUCCESS**.

Verified path:

`position_scaling.plan → risk.request → risk.decision → position_scaling.approved`

Pinned source evidence:

- Position Scaling: `b338264e872bab99d48acb63304f041aa1a182db`;
- Risk Sizing: `b2393048d09ab345bdbfe3addc0fea20be685c8a`.

Observed quantities:

- requested: `0.25`;
- Risk-approved: `0.250`;
- final Position Scaling approval: `0.25`.

Typed request, typed decision and legacy metadata agreed exactly; no planner, Risk or approval skips occurred. Artifact digest: `sha256:2e3174bf46cc20783cb55fa1a9450c0f81f16c6550237e04e7685d89b63a5682`.

PS-CCR-001 is closed. Wider execution, persistence, factual-fill reconciliation and operational monitoring remain separate integration responsibilities.
