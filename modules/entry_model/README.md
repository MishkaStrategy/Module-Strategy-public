# Entry Model 0.2.0

Independent deterministic `setup.confirmed` → `entry_model.plan` module for Modular Strategy Core 0.4.x / Module API 1.0.

## Responsibility

Entry Model consumes a canonical confirmed setup visible on a completed bar, selects an entry kind, calculates exact `Decimal` prices, applies bar-based TTL and an optional setup cutoff, and publishes an immutable typed plan. Invalid, stale, conflicting or incomplete input produces a structured rejection.

The module deliberately does **not**:

- calculate stop loss, invalidation or take profit;
- size positions or enforce portfolio risk;
- round price/quantity to exchange filters;
- check live post-only marketability;
- submit, cancel or reconcile orders;
- manage open positions or exits.

## Public API

```python
from modular_modules.entry_model import (
    ConfirmedSetup,
    EntryKind,
    EntryModelConfig,
    EntryModelDataError,
    EntryModelModule,
    EntryPlan,
    EntryRejectionReason,
)
```

Package entry point:

```text
modular_strategy.modules:entry_model
```

## Manifest

```text
name: entry_model
version: 0.2.0
api_version: 1.0
required_features: setup.confirmed
produced_features: entry_model.plan, entry_model.rejection
required_timeframes: none
state_schema_version: 2
```

## Supported entry kinds

| Kind | Order type | Required setup prices |
|---|---|---|
| `market` | market | none beyond confirmation |
| `limit_confirmation` | limit | confirmation |
| `limit_retracement` | limit | confirmation + directional extreme |
| `limit_explicit` | limit | explicit entry price |
| `stop_market` | stop market | trigger price |
| `stop_limit` | stop limit | trigger and optional limit price |

Retracement formula:

```text
entry = confirmation + retracement_fraction × (extreme − confirmation)
```

Default fraction is `0.382`.

## Determinism and no look-ahead

- financial arithmetic uses `Decimal`;
- all IDs derive from canonical SHA-256 payloads;
- no UUID/random/system-clock input is used;
- only the completed current bar and already-visible features are consumed;
- `confirmed_at` cannot be after feature publication or current bar close;
- stale/future/out-of-order input fails closed;
- later bars do not alter an emitted plan;
- repeated exact input is idempotent;
- conflicting duplicate identity is rejected.

## State and restart

State schema 2 contains:

- configuration fingerprint;
- bound symbol/timeframe;
- last bar timestamp/signature;
- bounded processed-setup records;
- bounded source-feature records;
- bounded counters.

Restore is strict and atomic. Wrong schema/config, unknown fields, partial cursor, duplicate rows, corrupt fingerprints and oversized state are rejected without mutating live state.

## Configuration

```yaml
modules:
  - plugin: entry_model
    parameters:
      default_entry_kind: limit_retracement
      retracement_fraction: "0.382"
      ttl_bars: 36
      honor_setup_hint: true
      post_only_limits: true
      stop_limit_offset_bps: "0"
      max_setup_age_seconds: "0"
      max_processed_setups: 10000
      allowed_entry_kinds:
        - market
        - limit_confirmation
        - limit_retracement
        - limit_explicit
        - stop_market
        - stop_limit
```

`max_setup_age_seconds: "0"` requires same-bar consumption. Increase it explicitly only when delayed feature consumption is part of the integration contract.

## Install and test

```bash
python -m pip install /path/to/modular_strategy_core-0.4.0-py3-none-any.whl
python -m pip install -e '.[dev]'
pytest -q
coverage run -m pytest
coverage report --fail-under=90
ruff check src tests benchmarks examples scripts
mypy --strict src/modular_modules/entry_model
python -m build
```

## Integration boundary

The upstream producer must publish canonical `setup.confirmed` schema 1. A strategy-specific confirmation key requires a separate adapter/upstream integration change; Entry Model intentionally does not understand strategy-specific payloads.

Canonical setup metadata may include `entry_ttl_bars` as a positive-integer TTL hint. The hint can only shorten the configured `ttl_bars` ceiling and therefore cannot extend Entry Model lifetime policy. Router-facing metadata keys such as `priority`, `strategy_id` and `module_id` are promoted into EntryPlan metadata while the complete upstream metadata remains preserved under `source_setup_metadata`.

Downstream modules should parse plans through:

```python
plan = EntryPlan.from_feature_value(feature.value)
```

See `INTEGRATION_CONTRACT.md`, `HANDOFF.md` and the files under `docs/` in the release bundle.

## Local release status

`LOCAL RELEASE CANDIDATE — READY FOR CODEX INTEGRATION`

This is not a full live-trading release and does not claim completion of Protection, Risk, Execution or Position Management.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
