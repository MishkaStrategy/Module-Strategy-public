# Modular Strategy Risk Sizing 1.2.0

Independent Module API v1 plugin implementing fixed-fractional risk budgets, Decimal position sizing, instrument rounding, notional/leverage limits, portfolio risk gates and typed projected-position enforcement for changes to an already-open position.

## Install

```bash
python -m pip install ./modules/risk_sizing
```

## Plugin

```text
entry-point group: modular_strategy.modules
entry-point name: risk_sizing
class: strategy_modules.risk_sizing:RiskSizingModule
```

The module requires `risk.request` and publishes same-bar `risk.decision`. It never submits an order, confirms a fill or mutates the portfolio.

## Existing-position adjustments

A normal schema-1 request remains unchanged. A scale-in may additionally carry a typed `position_adjustment` schema-1 block containing current quantity/average entry, requested delta, projected quantity/average entry, valuation price, current/projected notional and current/projected risk.

Risk Sizing does not trust derived producer values. It independently recalculates the projected position using `Decimal` and instrument `contract_size`, verifies the evidence, applies total-position risk/notional/leverage/quantity gates and returns the approved **delta quantity**. Partial approval is supported. An existing-position adjustment does not consume another `max_positions` slot.

For transition compatibility, the metadata geometry emitted by Position Scaling 0.2.2 is parsed into the same typed contract. When top-level and metadata representations are both present, they must agree exactly or the request fails closed.

See:

- `../../docs/risk-sizing.md` — full contract;
- `../../docs/PS-CCR-001.md` — projected-position change record;
- `../../docs/risk-sizing-baseline.md` — baseline evidence;
- `../../docs/risk-sizing-audit.md` — prior 30 rounds, 15 audits and final audit;
- `../../benchmarks/RISK_SIZING_RESULTS.md` — performance and memory evidence.
