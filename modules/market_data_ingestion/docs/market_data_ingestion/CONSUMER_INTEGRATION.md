# Consumer integration

## Backtest Engine

The adapter should translate a registered normalized kline partition to the existing
Backtest `DataSourceConfig`. The experiment must record dataset ID, version, dataset hash,
manifest URI and exact temporal range. Backtest must not call `download` or `update`.

## Research Runner

Translate `DatasetDescriptor` into existing `DatasetLineage` fields: immutable dataset
identity, market type, instrument, timeframe, period, row count, UTC, file format, size,
SHA-256, preprocessing/aggregation versions, known gaps/duplicates and quality status.
A new dataset version creates a new research run identity.

## Results Aggregator

`results_registry_adapter.to_results_registry_dataset_record()` maps the detailed
manifest into the existing dataset-record v1 schema. It does not replace the Results
Aggregator registry.

## Strategy Factory

Strategy Factory submits a `MarketDataRequest` and receives a descriptor plus validation
status, actual coverage, unavailable optional data and source limitations. It never calls
an exchange adapter directly.
