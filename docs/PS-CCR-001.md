# PS-CCR-001 — projected-position risk contract

## Status

**CLOSED by Risk & Position Sizing 1.2.0.**

Closure is supported by:

- final clean Risk Sizing head `b2393048d09ab345bdbfe3addc0fea20be685c8a`;
- permanent self-hosted matrix run `29864972869` on Python 3.12 and 3.13;
- 195 Risk Sizing tests passed;
- 96% branch-aware coverage against a 95% gate;
- wheel and source-distribution verification, including wheel rebuild from sdist;
- clean installation, `pip check`, public import, entry point and registry discovery;
- Bandit, detect-secrets, pip-audit and CycloneDX SBOM;
- Position Scaling exact-head run `29866156845`;
- pinned cross-module run `29866061235` proving the real producer → Risk → approval path.

The Pull Requests remain Draft and unmerged. Closing this change request does not authorize merge or production deployment.

## Original gap

Risk Sizing 1.1 evaluated a Position Scaling increase as a standalone order. The producer supplied current/projected position geometry in generic metadata, but the consumer neither typed nor independently enforced the whole resulting position.

That allowed an adjustment to be constrained by its delta while omitting explicit validation of the resulting weighted entry, total quantity, total notional, total leverage and full risk to the active stop.

## Contract

`risk.request` remains schema version `1`. A request may add an optional `position_adjustment` object with schema version `1` containing:

- `adjustment_id` and `position_id`;
- supported increase kind;
- `opens_new_position=false`;
- current, requested, maximum and projected quantity;
- current and projected weighted-average entry;
- current valuation price;
- current and projected notional;
- current and projected total risk to stop.

The request-level `entry_price` is the proposed adjustment fill price. `RiskDecision.quantity`, `notional` and `actual_risk_amount` continue to describe the approved delta order for backward compatibility.

Adjustment decisions additionally publish:

- approved delta quantity;
- resulting total quantity;
- resulting weighted-average entry;
- resulting notional;
- resulting leverage;
- resulting total risk;
- `opens_new_position=false`.

## Independent enforcement

Risk Sizing recalculates all derived geometry with bounded `Decimal` arithmetic and instrument `contract_size`. Producer evidence must equal the independent result.

The approved delta is bounded by:

- requested and maximum adjustment quantity;
- additional risk budget;
- portfolio risk capacity;
- projected trade notional;
- portfolio and symbol notional capacity;
- projected total leverage;
- available-margin leverage;
- instrument maximum total quantity and notional;
- instrument quantity step and minimum order rules.

The portfolio snapshot must already include the current position. This prevents repeated scale-in requests from treating existing exposure as absent.

## Position count semantics

An adjustment explicitly bound to an existing position does not create another position and therefore does not fail merely because `open_positions == max_positions`.

Requests without a valid typed adjustment retain the original new-position behavior and remain subject to `max_positions`.

## Compatibility

Legacy ordinary schema-1 requests are unchanged.

Position Scaling 0.2.2 publishes the top-level typed block while retaining migration metadata. When both representations are present, exact agreement is mandatory.

## Fail-closed cases

Requests are rejected for unsupported schema or kind, blank identifiers, `opens_new_position=true`, missing or non-finite decimals, inconsistent quantity algebra, invalid stop geometry, mismatched weighted entry/notional/risk evidence, a portfolio snapshot that omits current exposure, exhausted total-position capacity, conflicting duplicate identity or stale/future feature evidence.

## Closure evidence

### Risk Sizing permanent matrix

Workflow: `risk-sizing-position-adjustments-ci`

Run: `29864972869`

Head: `b2393048d09ab345bdbfe3addc0fea20be685c8a`

- Python 3.12: success;
- Python 3.13: success;
- tests: 195 passed;
- coverage: 96%, gate 95%;
- Python 3.13 artifact digest: `sha256:1725c80c74443fba5328485fb7f7454243f962aeefc5babfcc1c7c5c84b49217`;
- Python 3.12 artifact digest: `sha256:48c28beed024252ccdca3330b347349540e456d4d2ad4029d5ce5c8453a39d26`.

### Position Scaling permanent matrix

Workflow: `position-scaling-hardening-ci`

Run: `29866156845`

Head: `431ff71b20ef0fbfff3b54e62ae5a210d44e3491`

- Python 3.12: success;
- Python 3.13: success;
- Position Scaling tests: 251 passed;
- coverage: 90%, gate 90%;
- repository and neighboring-module compatibility suites: success.

### Pinned cross-module proof

Run: `29866061235`

Pinned source:

- Position Scaling: `b338264e872bab99d48acb63304f041aa1a182db`;
- Risk Sizing: `b2393048d09ab345bdbfe3addc0fea20be685c8a`.

Verified path:

`position_scaling.plan → risk.request → risk.decision → position_scaling.approved`

Evidence:

- requested quantity: `0.25`;
- Risk-approved quantity: `0.250`;
- approved quantity: `0.25`;
- typed request and typed decision present;
- legacy metadata agrees exactly;
- no planner, Risk or approval skips;
- artifact digest: `sha256:2e3174bf46cc20783cb55fa1a9450c0f81f16c6550237e04e7685d89b63a5682`.

## Residual scope

PS-CCR-001 is closed. The following remain outside this change request:

- complete executor/broker/persistence/factual-fill integration;
- venue-specific rounding and rejection reconciliation;
- portfolio correlation/group risk service;
- paper/live operational readiness and monitoring.
