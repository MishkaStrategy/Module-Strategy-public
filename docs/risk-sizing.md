# Risk & Position Sizing module v1.2.0

## Purpose

`RiskSizingModule` is the pre-trade risk-policy stage in the modular execution chain:

```text
Confirmed Setup -> EntryPlan -> ProtectionPlan -> risk.request
-> RiskDecision -> OrderIntent -> execution safeguards -> Fill
```

It calculates a maximum safe order quantity. For an increase of an already-open position it also independently validates the projected total position. It does not submit orders, alter a portfolio, construct a stop, query an exchange or read future market data.

## Public API

```python
from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    PositionAdjustment,
    PositionAdjustmentDecision,
    RiskCalculator,
    RiskDecision,
    RiskRequest,
    RiskSizingModule,
)
```

Entry point: `modular_strategy.modules:risk_sizing`.

Manifest:

```text
name: risk_sizing
version: 1.2.0
api_version: 1.0
required feature: risk.request
produced feature: risk.decision
state schema: 2
```

The dependency planner must find exactly one producer of `risk.request`; manual ordering is not required.

## Input: `risk.request` schema 1

Required fields for every request:

```yaml
schema_version: "1"
request_id: btc-setup-20260721-001
symbol: BTCUSDT
side: buy
entry_price: "65000"
stop_loss: "64675"
equity: "10000"
instrument:
  quantity_step: "0.001"
```

Optional fields:

```yaml
requested_risk_fraction: "0.005"
available_margin: "10000"
setup_id: setup-001
instrument:
  min_quantity: "0.001"
  max_quantity: "1000"
  min_notional: "5"
  max_notional: "10000000"
  tick_size: "0.1"
  contract_size: "1"
  max_leverage: "20"
portfolio:
  gross_notional: "0"
  symbol_notional: "0"
  open_risk_amount: "0"
  open_positions: 0
metadata: {}
```

Unknown fields are rejected at request, instrument, portfolio, adjustment and configuration levels. Financial values are parsed through bounded `Decimal` conversion; booleans, NaN, infinity and unsupported magnitudes are rejected.

### Existing-position adjustment

An increase of an already-open position may additionally provide a typed schema-1 block:

```yaml
position_adjustment:
  schema_version: "1"
  adjustment_id: scale-001
  position_id: btc-long-001
  kind: pyramid
  opens_new_position: false
  current_quantity: "0.100"
  current_average_entry: "64000"
  requested_delta_quantity: "0.020"
  maximum_delta_quantity: "0.020"
  projected_quantity: "0.120"
  projected_average_entry: "64166.66666666666666666666667"
  valuation_price: "65000"
  current_notional: "6500"
  projected_notional: "7800"
  current_total_risk: "100"
  projected_total_risk: "120"
```

The request-level `entry_price` is the proposed fill price for the delta order. The portfolio snapshot must already include the current position exposure.

During migration, the geometry emitted by Position Scaling 0.2.2 in request metadata is parsed into the same immutable `PositionAdjustment` contract. If top-level and metadata representations are both supplied, exact semantic agreement is required.

### Temporal contract

A request is valid only on the closed bar whose `close_time` equals `Feature.created_at`. Future, stale, expired or unsupported-schema requests fail closed. The real `FeatureStore` may filter an incompatible feature before it reaches the module; direct adapter tests separately prove fail-closed handling for unfiltered external views.

`risk.decision.valid_until == risk.decision.created_at`; the downstream consumer must act in the same deterministic module pipeline pass.

## Output: `risk.decision` schema 1

Every decision contains:

- request and setup identifiers;
- symbol and status;
- approved and raw delta quantity;
- requested/effective risk fraction and budget;
- actual delta risk amount and fraction;
- delta notional and required leverage;
- binding constraints;
- stable reject reason and message;
- structured diagnostics.

Use `RiskDecision.from_mapping(feature.value)` to recover the typed immutable DTO. `setup_id` is preserved for downstream causality.

For an existing-position adjustment, `RiskDecision.quantity`, `notional` and `actual_risk_amount` retain their backward-compatible meaning: they describe the approved **delta order**. The optional immutable `PositionAdjustmentDecision` additionally reports:

- approved delta quantity;
- resulting total quantity;
- resulting weighted-average entry;
- resulting total notional;
- resulting leverage;
- resulting total risk to the active stop;
- `opens_new_position=false`.

## Calculation rules

### New position or ordinary request

1. Validate positive equity, entry/stop geometry and tick alignment.
2. Select requested risk fraction or the configured default.
3. Calculate risk budget and remaining portfolio risk capacity.
4. Calculate quantity capacities from risk, trade/symbol/portfolio notional, available margin/leverage and instrument maxima.
5. Select the minimum capacity.
6. Floor quantity to an arbitrary positive Decimal `quantity_step`; never round upward.
7. Recheck minimum quantity/notional and actual risk.
8. Return an immutable approved or rejected `RiskDecision`.

### Existing-position increase

1. Parse and validate immutable adjustment evidence.
2. Recalculate projected quantity and weighted-average entry independently.
3. Recalculate current/projected notional and total risk using instrument `contract_size`.
4. Require producer evidence to match the independent result.
5. Bound the delta by requested/maximum quantity, remaining risk, total-position quantity/notional/leverage, portfolio/symbol capacities and available margin.
6. Floor only the approved delta to `quantity_step` and recalculate the resulting position.
7. Reject missing current exposure, impossible algebra, unsupported kinds, inconsistent evidence or exhausted capacity.
8. Publish the delta decision plus typed resulting-position evidence.

An adjustment bound to an existing position does not consume another `max_positions` slot. Ordinary requests retain the original position-count rule.

`contract_size` participates in risk and notional calculations for both the delta and resulting position.

## Configuration defaults

| Parameter | Default | Constraint |
|---|---:|---|
| `default_risk_fraction` | 0.005 | `min <= default <= max` |
| `min_risk_fraction` | 0.0001 | positive |
| `max_risk_fraction` | 0.02 | <= 1 |
| `max_trade_notional_multiple` | 3 | positive; <= portfolio cap |
| `max_symbol_notional_multiple` | 3 | positive; <= portfolio cap |
| `max_portfolio_notional_multiple` | 5 | positive |
| `max_portfolio_risk_fraction` | 0.03 | `(0, 1]` |
| `max_leverage` | 3 | positive |
| `max_positions` | 10 | integer >= 1 |
| `allow_risk_reduction` | true | strict boolean |
| `require_price_alignment` | true | strict boolean |
| `max_processed_requests` | 2048 | integer >= 1 |

Configuration numbers must be exactly representable by the numeric parameter contract used in the Core run manifest. This prevents two exact Decimal configurations from sharing one float-based run identity.

## State and recovery

State schema 2 contains:

- exact configuration fingerprint;
- fixed stream symbol;
- ordered bounded request fingerprints;
- ordered bounded event fingerprints;
- observability counters.

Restore is atomic. It rejects wrong schema/configuration, malformed counters, duplicate order entries, inconsistent maps, invalid symbols and partial state. Real Core tuple-like checkpoint sequences are accepted. Oversized valid state is deterministically trimmed to the configured bound.

The cache is intentionally bounded. A request replayed after it has aged out of both bounded caches cannot be recognized by this module alone; durable global event replay and run checkpoint boundaries remain Core responsibilities.

## Duplicate, ordering and gap behaviour

- exact event replay: ignored;
- same request payload under a new feature event: ignored;
- same `request_id` with changed payload or adjustment evidence: rejected;
- unsupported malformed event: one rejection, then idempotent suppression;
- stale/out-of-order request: rejected;
- symbol stream change inside one instance: rejected;
- `data_gap=True`: never permits restore republish into an incompatible stream;
- separate module instances remain independent for separate symbols/runs.

## Core and cross-module compatibility

The module uses only public Core contracts and does not add strategy-specific logic to the kernel. It does not replace `modular_core.risk.FixedFractionalSizer`; it supplies an explicit approved quantity to downstream execution.

PS-CCR-001 is implemented inside Risk Sizing rather than Core. Closure still requires the Position Scaling producer to publish the typed top-level block while retaining metadata compatibility, plus cross-module tests for full, partial and rejected approvals, derivative `contract_size`, `max_positions`, restart identity and factual post-fill reconciliation.

## Installation

```bash
python -m pip install ./modules/risk_sizing
```

```yaml
modules:
  - class: strategy_modules.risk_sizing:RiskSizingModule
    enabled: true
    parameters:
      default_risk_fraction: 0.005
      max_trade_notional_multiple: 3.0
      max_portfolio_risk_fraction: 0.03
```

## Performance evidence

The 1.1 baseline remains the last completed broad performance campaign:

- 100,000 pure decisions: median 0.9027 s across five runs, about 110,782/s;
- 1,000,000 pure decisions: 9.3687 s, about 106,739/s;
- 50,000 full Module API events: median 10.1608 s across three runs, about 4,921/s;
- 250,000 events across five checkpoint/restart chunks completed twice with byte-identical final state;
- 10,000-event traced run peak: 2,291,279 bytes;
- 2,048-entry checkpoint: 491,892 bytes; median restore 1.09 ms.

Risk Sizing 1.2 must repeat the relevant benchmark and restart workloads before the PR leaves Draft. See `benchmarks/RISK_SIZING_RESULTS.md`, `docs/PS-CCR-001.md` and `docs/risk-sizing-audit.md`.

## Known limitations

- Correlation, sector/group concentration and cross-account exposure require a portfolio-risk service.
- The module does not confirm fills or mutate portfolio state; execution and reconciliation remain downstream responsibilities.
- Position Scaling must migrate from metadata-only evidence to the typed top-level adjustment block.
- One mutable module instance is bound to one symbol stream.
- Module correctness does not establish strategy profitability or whole-system production/live readiness.
