# Historical Market Data Ingestion

Local release-candidate worktree for the Modular Strategy historical-market-data
component.

- Python package: `market_data_ingestion`
- Package version: `0.4.0`
- Normalization contract: `1.3.0`
- GitHub branch: `data/historical-market-data-ingestion`

## Current status

The package has a functional immutable ingestion, recovery and dataset-versioning path.
It is **not yet a production release** because this execution environment cannot run the
Python 3.12, Ruff, strict MyPy, Hypothesis, PyArrow/Parquet or real Binance network gates.
The component branch has been created from canonical staging SHA `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`; GitHub CI and Draft PR remain required.

Implemented and locally exercised:

- strict versioned request, capability, plan, checkpoint, validation, repair, manifest
  and dataset descriptor contracts;
- exact exchange/market/instrument identity with Spot and USD-M separation;
- Fake adapter for Spot and USD-M acceptance tests;
- Binance Spot klines and metadata;
- Binance USD-M klines, funding rates, mark-price klines, index-price klines,
  premium-index klines, open-interest history and metadata;
- official monthly archive preference for complete kline months and bounded REST
  fallback for unavailable/partial archive ranges;
- explicit open-interest retention policy for Binance's short official history window;
- immutable raw evidence with SHA-256, source metadata and deterministic contextual IDs;
- bounded retries, `Retry-After`, exponential backoff, rate limiting and circuit breaker;
- safe ZIP extraction limits and checksum validation;
- SQLite-backed bounded-memory record spool using strict JSON rather than pickle;
- one-pass sorted validation, exact duplicate evidence, conflicting duplicate rejection,
  UTC alignment, leading/internal/trailing gaps and listing/delisting classification;
- streaming CSV normalization, monthly partitioning and streaming 15m/1h aggregation;
- optional Parquet code path when `pyarrow` is installed;
- deterministic content hash, immutable manifests and a minimal SQLite Dataset Registry;
- idempotent request reuse, request-ID conflict detection, update and backfill versions;
- damaged normalized, derived and metadata repair;
- range-scoped gap repair for klines, mark/index/premium price and open-interest streams;
- resumable failed downloads, explicit cancel, safe export and deep integrity validation;
- consumer adapter for the existing Results Aggregator dataset-record contract.

## Local verification checkpoint

```text
207 tests collected
207 passed
ResourceWarning treated as error: passed
Dependency-free AST/source audit: 0 findings
Total package coverage: 81%
20,000-bar streaming benchmark integrity: ok
```

See:

- `reports/ACCEPTANCE_STATUS.md`
- `reports/HARDENING_LOG.md`
- `reports/PERFORMANCE_STREAMING_0_3.md`
- `docs/market_data_ingestion/CAPABILITY_MATRIX.md`

## Local smoke

```bash
PYTHONPATH=src python3 -m market_data_ingestion download \
  --config configs/market_data/fake_btcusdt.yaml

PYTHONPATH=src python3 -m market_data_ingestion validate \
  --storage-root .local-market-data/fake \
  --dataset-id fake_usdt-m-perpetual_btcusdt_5m \
  --deep
```

Benchmark:

```bash
PYTHONPATH=src python3 scripts/benchmark_streaming.py \
  --bars 20000 \
  --chunk-bars 1000 \
  --output reports/benchmark-streaming-20000.json
```

## Safety boundary

This component prepares immutable historical datasets. It does not provide live market
data, signals, backtesting, paper execution or live execution. Binance adapter maturity
remains `TESTED`, not `PRODUCTION`, until official-network acceptance succeeds.
