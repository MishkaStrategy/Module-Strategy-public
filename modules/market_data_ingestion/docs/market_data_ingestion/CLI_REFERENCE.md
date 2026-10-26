# CLI reference

Executable entry point:

```text
market-data
```

Every mutating dataset operation creates or resumes immutable artifacts. No command
silently edits a registered dataset version.

## Discovery

```text
market-data exchanges
market-data markets --exchange <id>
market-data capabilities --exchange <id>
market-data instruments --exchange <id> --market <market> --symbol <symbol>
```

`instruments` performs exact resolution. It is not fuzzy search.

## Ingestion

```text
market-data download --config <yaml>
```

The YAML controls exchange, market, symbol, required/optional data types, time range,
base/derived timeframes, concurrency, retry policy, memory bound and storage format.

## Immutable dataset operations

```text
market-data update \
  --storage-root <path> --dataset-id <id> [--version N] \
  --to <ISO-8601> [--format csv|parquet] [--compression zstd]

market-data backfill \
  --storage-root <path> --dataset-id <id> [--version N] \
  --data-type <type> [--from <ISO-8601>] [--to <ISO-8601>] \
  [--format csv|parquet] [--compression zstd]

market-data repair \
  --storage-root <path> --dataset-id <id> [--version N] \
  [--data-type <type>] [--format csv|parquet] [--compression zstd]
```

- `update` appends only the new range and publishes a new version.
- `backfill` adds a non-overlapping historical stream/range and publishes a new version.
- `repair` verifies registered partitions, restores damaged partitions and can request
  exact unresolved fixed-interval gaps. Healthy unrelated partitions are retained.

Funding-rate gaps are not inferred from a fixed cadence because the historical funding
interval can vary.

## Recovery and cancellation

```text
market-data resume --storage-root <path> --run-id <run-id>
market-data cancel --storage-root <path> --run-id <run-id>
```

`resume` verifies the immutable execution envelope, adapter version, checkpoint and raw
checksums. Confirmed chunks are restored from raw evidence; only missing chunks are
requested again. `cancel` writes a cancellation marker and moves a paused run to
`CANCELLED`.

## Inspection, validation and export

```text
market-data inspect \
  --storage-root <path> --dataset-id <id> [--version N]

market-data validate \
  --storage-root <path> --dataset-id <id> [--version N] [--deep]

market-data export \
  --storage-root <path> --dataset-id <id> [--version N] --target <directory>

market-data status --storage-root <path>
market-data doctor [--storage-root <path>]
```

`validate --deep` checks manifest identity, content hash, partition checksums and row
counts, raw evidence metadata/checksums and partition/raw lineage. Export performs
integrity validation before atomically publishing a copy.

## Storage dependencies

CSV storage uses only the base package. Parquet storage requires the optional `parquet`
extra, which installs PyArrow:

```text
pip install 'modular-market-data-ingestion[parquet]'
```

The permanent Python 3.12/3.13 CI matrix, public Binance acceptance and multi-year real-data
benchmarks all execute the Parquet path. If PyArrow is unavailable, the CLI fails closed
instead of silently changing the requested storage format.


## External corpus verification

```text
market-data verify-external-corpus --spec <json> --output <manifest.json>
```

The strict JSON spec names every local Parquet file, its persistence-safe source
URI, expected row count and UTC bounds, plus an optional expected SHA-256. The
command validates the complete symbol/timeframe matrix, reads Parquet in bounded
batches and fails closed on schema drift, checksum mismatch, invalid OHLCV, gaps,
duplicates or changed bounds. It writes no raw market data to GitHub; the output is
a content-addressed evidence manifest consumed by the later Dataset Registry import.
