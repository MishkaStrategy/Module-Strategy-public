# Market Regime 1.2.0 — Integration Contract

## Ownership

Market Regime owns only its validated configuration, bounded analytical state, diagnostics and the `market_regime.*` feature namespace. Core owns DTOs, lifecycle invocation, MarketView, Feature Store, dependency planning, checkpoint orchestration and timeframe aggregation.

No Core source change is required by this module.

## Input contract

The module consumes the current completed base candle and the latest completed candle for its configured target timeframe from Core `MarketView`.

- default target timeframe: `1h`;
- canonical aliases are accepted;
- future target close times are rejected with `market_regime.incomplete_bar`;
- missing target data produces `market_regime.no_target_bar`;
- stale repeated target data produces `market_regime.stale_target`;
- explicit `data_gap` is a reset barrier.

The module does not access exchanges, networks, files, wall-clock time, portfolio state mutations or broker adapters.

## Output contract

The module may publish only these immutable feature keys:

- `market_regime.current`;
- `market_regime.trend`;
- `market_regime.volatility`.

Feature schema version is `1`. Feature identifiers and payloads are deterministic for an identical point-in-time event stream.

The module never publishes orders, signals, setups, cancellations, close requests, update requests or position changes.

## Lifecycle contract

Supported lifecycle:

`constructor → validation → registration → warm-up → processing → output/typed skip → gap/reset → checkpoint → restore → replay → continuation → finish`

- warm-up is deterministic and bounded;
- exact duplicate base events are idempotent;
- conflicting duplicates and out-of-order events raise `MarketRegimeDataError`;
- rejected target validation is transactional;
- data/timeframe gaps reset rolling analytical state while preserving cumulative health counters;
- one instance is bound to one symbol;
- independent instances share no mutable state.

## Configuration contract

- unknown fields fail closed;
- booleans and integers are strictly validated;
- Decimal thresholds are canonicalized for semantic fingerprints;
- `publish_diagnostics` is non-semantic and does not change features or checkpoint compatibility;
- invalid configuration raises `MarketRegimeConfigurationError`.

## State contract

- current schema: `2`;
- readable schemas: `1` and `2`;
- schema-1 migration is covered by a real historical checkpoint fixture;
- checkpoint data is JSON-compatible and bounded;
- restore is atomic;
- corrupted, partial, oversized, unknown-schema and incompatible-config state is rejected with `MarketRegimeStateError`;
- continuous and restored continuation must have identical output and final-state digests.

## Compatibility

- distribution: `modular-strategy-market-regime` 1.2.0;
- Core: `>=0.4,<0.5`, verified locally with 0.4.0;
- Module API major: 1;
- Python: `>=3.12`;
- entry point: `modular_strategy.modules / market_regime`;
- public import: `market_regime:MarketRegimeModule`.

## Integration gates

Before merge:

1. Python 3.12 and 3.13 self-hosted fast quality jobs must pass.
2. Full repository and neighboring-module tests must pass.
3. Wheel, sdist, clean install, entry point and `pip check` must pass remotely.
4. Security/SBOM/checksum evidence must be produced remotely.
5. Deterministic performance, checkpoint size and restore latency must be recorded on the exact PR head.
6. PR must remain Draft until all mandatory gates are green.
