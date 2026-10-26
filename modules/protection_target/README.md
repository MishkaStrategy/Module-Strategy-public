# Protection & Target Module 1.1.1

Independent Module API plugin for deterministic invalidation, stop-loss and take-profit planning.

## Responsibility

The module consumes a completed `entry_model.entry_plan` feature and emits either:

- `protection_target.plan` — a validated immutable protection plan; or
- `protection_target.rejected` — an explainable rejection with a typed reason code.

It does **not** size positions, round by exchange tick size, place orders, mutate portfolios,
manage open positions, access an exchange, load market data, or read system time.

## Public API

```python
from modular_strategy_protection_target import ProtectionTargetModule
```

Entry point group:

```text
modular_strategy.modules
protection_target = modular_strategy_protection_target:ProtectionTargetModule
```

## Input contract

The legacy input remains `entry_model.entry_plan` schema `1`. The current Entry Model
`entry_model.plan` schema `2` is also accepted when configured through
`entry_feature_key="entry_model.plan"`; payload and outer feature schema versions must match.

Legacy schema 1 uses `entry_plan_id`, `entry_price` and optional `valid_until`. Current
schema 2 uses `plan_id`, `price` and `expires_at` and keeps canonical Entry Model metadata
available for dotted field-path lookups.

For lifecycle-aware composition, `max_holding_seconds_field` and
`no_progress_rule_field` are optional dotted EntryPlan paths. When configured, the resolved
values are validated fail-closed and forwarded into `protection_target.plan`; when omitted,
legacy behavior and the default configuration fingerprint remain unchanged.

## Output geometry

BUY:

```text
stop_loss <= invalidation_price < entry_price < take_profit
```

SELL:

```text
take_profit < entry_price < invalidation_price <= stop_loss
```

Fixed-R target is calculated from the actual buffered stop distance.

## Modes

Invalidation:

- `entry_plan`;
- `fixed_percent`;
- `feature`.

Target:

- `fixed_r`;
- `structural`, from EntryPlan or a structural feature.

If multiple distinct structural features are equally current for the same setup, processing fails
closed with `protection_target.ambiguous_structural_feature`.

## Temporal semantics

- `FeatureView.timestamp` must exactly equal the current completed bar close.
- Future features are filtered by Core and future-data invariance is regression-tested.
- Exact duplicate events are idempotent.
- A changed feature/gap context at the same bar timestamp is rejected as a contradictory event.
- Out-of-order bars and stream changes are rejected.

## State

State schema: `2`.

The JSON-compatible checkpoint contains:

- module/config identity;
- stream identity;
- bar and event signatures;
- bounded EntryPlan deduplication state;
- counters;
- SHA-256 `state_digest`.

Restore is transactional. Version 1 checkpoints produced by module 1.0.0 are migrated through an
explicit compatibility layer when their legacy configuration fingerprint matches.

## Local quality evidence

- tests: 232 passed;
- branch-aware coverage: 93.34%;
- Ruff 0.15.22: passed;
- strict mypy 1.20.2: passed;
- compileall: passed;
- wheel and sdist build: passed;
- clean wheel installation and entry-point smoke: passed;
- deterministic 50,000-event replay: identical output and state digests;
- 1,000,000-event multi-process restart stress: passed, state bounded at 4,096 entries.

GitHub CI: pending publication of the audited 1.1.1 commit to Draft PR #12.

## Commands

```bash
python -m pip install /path/to/modular_strategy_core-0.4.0-py3-none-any.whl
python -m pip install dist/modular_strategy_protection_target-1.1.1-py3-none-any.whl
python -m pytest -q tests
ruff check src tests benchmarks
MYPYPATH=/path/to/core mypy --strict src/modular_strategy_protection_target
python -m compileall -q src tests benchmarks
python -m build --no-isolation
```

## Known limitations

- Core Module API 1.0 has no explicit `Candle.is_closed`; the module relies on Core's completed-bar contract.
- OHLC data cannot reveal intrabar path.
- Instrument tick-size rounding belongs to the downstream instrument/execution layer.
- One module instance owns exactly one `(symbol, timeframe)` stream.
- Module parameters require instrument-specific backtest and walk-forward research; this module does not prove trading edge.
- Full paper/live readiness depends on Entry Model, Risk & Position Sizing, execution/reconciliation and Position & Exit Management.
