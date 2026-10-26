# ADR-0001: Historical ingestion ownership boundary

**Status:** Accepted for local implementation checkpoint

## Decision

`market_data_ingestion` owns historical capability discovery, exact instrument
resolution, acquisition, raw evidence, normalization, validation, repair planning,
derived historical timeframes, manifests, dataset hashes and dataset registration.

It does not own strategy logic, backtesting, research scheduling, result aggregation,
paper/live execution, position management, or a live market feed.

## Consequences

- Core `Candle` remains an output/consumer compatibility boundary.
- Ingestion uses richer source-specific records internally.
- Results Aggregator remains the experiment registry; ingestion has only the minimum
  immutable Dataset Registry required to issue `DatasetDescriptor`.
- Live market data requires a separate component and cannot reuse this service by
  changing a flag.
