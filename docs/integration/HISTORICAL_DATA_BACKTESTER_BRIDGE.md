# Historical Data Registry to Backtest Engine Bridge

Status: Draft integration slice. Paper-only. No live execution.

## Purpose

This bridge makes an immutable registered dataset, rather than an arbitrary local
file path, the identity of a historical backtest input. A backtest selects an
exact `dataset_id` and positive `dataset_version` from a local
`SQLiteDatasetRegistry`. The reader then validates the descriptor, manifest,
lineage, checksums, canonical Parquet schema, UTC timestamps, row identity and
5-minute continuity before converting records to Core `Candle` objects.

Raw and normalized market bytes remain outside Git and outside GitHub Actions
artifacts.

## Persistent local layout

The recommended Mac storage root is:

```text
/Users/<macOS-user>/ModuleStrategyData/market_data
```

The canonical storage implementation creates the following durable surfaces
below that root:

```text
registry.sqlite3
raw/
normalized/
manifests/
```

Use one stable storage root. Do not point several registries at the same
partition tree, do not symlink the root, and do not move files inside the tree by
hand. Registry and manifest paths are validated fail-closed against traversal and
symbolic links.

## Register the accepted external corpus

Install both project surfaces in the same environment:

```bash
uv pip install -e ".[backtest]" -e "modules/market_data_ingestion[parquet]"
```

Build a private strict corpus spec from the accepted local baseline folder. The
`drive_corpus` helper accepts a normal local folder; Google Drive is not required.

```bash
CORPUS_ROOT="$HOME/Google Drive/My Drive/binance_usdtm_futures_history"
INDEX="modules/market_data_ingestion/configs/market_data/binance_usdtm_baseline_drive_index.json"
PRIVATE_SPEC="$HOME/ModuleStrategyData/private/binance-usdtm-baseline-spec.json"
PRIVATE_REPORT="$HOME/ModuleStrategyData/private/binance-usdtm-baseline-inventory.json"

mkdir -p "$(dirname "$PRIVATE_SPEC")"
python -m market_data_ingestion.drive_corpus \
  --corpus-root "$CORPUS_ROOT" \
  --drive-index "$INDEX" \
  --output "$PRIVATE_SPEC" \
  --report "$PRIVATE_REPORT"
```

Register into durable storage:

```bash
STORAGE_ROOT="$HOME/ModuleStrategyData/market_data"
EVIDENCE="$HOME/ModuleStrategyData/evidence/binance-usdtm-registration.json"

mkdir -p "$(dirname "$EVIDENCE")"
market-data register-external-corpus \
  --spec "$PRIVATE_SPEC" \
  --storage-root "$STORAGE_ROOT" \
  --output "$EVIDENCE"
```

Registration is idempotent for the same accepted lineage. A repeated command
reuses the exact version instead of creating duplicate datasets. Any changed
source bytes produce a different lineage and must be reviewed as a new dataset
version.

The accepted baseline produces ten version-1 datasets. BTCUSDT has the canonical
identity:

```text
binance_usdt-m-perpetual_btcusdt_5m
```

## Inspect and validate a registered dataset

```bash
market-data inspect \
  --storage-root "$HOME/ModuleStrategyData/market_data" \
  --dataset-id binance_usdt-m-perpetual_btcusdt_5m \
  --version 1
```

Backtest consumption requires all of the following:

- descriptor status `VALID`;
- no unresolved registry issues;
- compatible consumer `modular_backtest`;
- traded `klines` data type;
- registered `5m` timeframe;
- manifest identity equal to the descriptor;
- all registered raw and normalized hashes valid;
- exact canonical Kline Parquet schema;
- closed rows with matching exchange, market, symbol and timeframe;
- exact 5-minute continuity in the selected UTC range.

A missing dependency, unsafe path, checksum mismatch, row identity mismatch,
unknown dataset version or time gap stops the run before strategy execution.

## Backtest configuration

Copy `configs/backtest.registered.example.yaml` and replace the absolute storage
root and desired UTC range. The data block is mutually exclusive with legacy
file mode:

```yaml
data:
  storage_root: /Users/USERNAME/ModuleStrategyData/market_data
  dataset_id: binance_usdt-m-perpetual_btcusdt_5m
  dataset_version: 1
  symbol: BTCUSDT
  base_timeframe: 5m
  gap_policy: reject
```

Run validation before the engine:

```bash
modular-backtest validate-data --config configs/backtest.registered.example.yaml
```

Then run the paper backtest:

```bash
modular-backtest run --config configs/backtest.registered.example.yaml
```

The emitted validation provenance uses a sanitized URI such as:

```text
dataset://binance_usdt-m-perpetual_btcusdt_5m/v1
```

It records dataset ID, version, dataset hash and selected partition count. It does
not record the private local storage path.

## Timeframe policy

The Backtest Engine consumes registered traded 5-minute Klines as the source of
truth. Strategy context timeframes are constructed deterministically inside the
engine from completed 5-minute bars:

- 15m from 5m;
- 1h from 5m;
- 4h from 5m;
- 1d from 5m;
- UTC boundaries only;
- unfinished higher-timeframe candles are never exposed.

Registered 15m, 1h and 4h files remain valuable for parity audits, but they are
not substituted into the execution stream. The accepted 10m data is registered
for lineage completeness and is not required by the current BTC strategy.

## Security and governance

- Do not commit `registry.sqlite3`, private corpus specs, raw files, normalized
  partitions or local evidence containing paths.
- Do not use a symlink as the storage root or for a registered partition.
- Do not silently resolve `latest`; every backtest must name an exact positive
  version.
- Do not mark the bridge PR Ready or merge it until persistent registration and a
  registry-based BTCUSDT smoke run pass on the exact candidate head.
- This bridge does not enable exchange credentials, order placement, portfolio
  mutation outside the paper engine, deployment or live trading.
