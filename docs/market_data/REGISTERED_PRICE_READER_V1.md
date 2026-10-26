# Registered Non-Traded Price Reader V1

This slice adds the Market Data owner-native read-only contract needed by GAP-008 / Issue #376 for futures mark/index valuation evidence.

It does not move valuation semantics into Market Data. It only proves and streams immutable registered non-traded price histories for consumers such as Backtest.

## Supported data families

`RegisteredPriceSelection` supports the existing normalized `PriceKlineRecord` families:

- `mark_price`;
- `index_price`;
- `premium_index`.

The selected data type is explicit. A consumer cannot request `mark_price` and receive traded klines, index price, premium index, or another normalized family.

## Owner identity

The resolver binds one exact registered dataset/version to:

- immutable dataset hash;
- instrument ID and symbol;
- exchange and market type;
- explicit non-traded price data type;
- explicit timeframe;
- compatible consumer;
- VALID descriptor and manifest state;
- exact normalized partitions;
- immutable raw source-artifact lineage.

The reader is strictly read-only. It does not register, repair, migrate, download, or mutate datasets.

## Coverage and continuity

Non-traded price klines are interval series, not sparse events. Therefore V1 is stricter than the funding-event reader:

- selected normalized partitions must be exactly contiguous;
- source-artifact acquisition coverage must not contain uncovered intervals;
- normalized partition coverage must remain inside immutable source coverage;
- raw lineage record counts must be exact and equal the normalized selected row count;
- streamed open timestamps must be exactly one requested timeframe apart;
- duplicate timestamps and gaps fail closed.

The public coverage convention is `[start,end)`.

`iter_registered_prices(selection, start=..., end=...)` requires the requested window to be fully represented. The first returned row must open exactly at `start`, and the last row plus one timeframe must equal the exclusive `end`.

## Partition verification

Every partition read verifies:

- registered safe relative path resolution;
- regular-file presence;
- descriptor SHA-256 before read;
- exact canonical parquet schema for the selected price type;
- canonical `PriceKlineRecord` decoding;
- closed-bar state;
- price type, symbol, timeframe, exchange, market type and source-kind identity;
- row open-time bounds inside its partition;
- exact descriptor row count;
- unchanged SHA-256 after streaming.

Wrong type, identity, schema, checksum, row count, path or bounds fail closed.

## Public evidence

`RegisteredPriceSelection.to_payload()` exposes only consumer-safe identity/evidence:

- dataset ID/version/hash;
- instrument/symbol/exchange/market;
- data type and timeframe;
- partition count and normalized row count;
- immutable raw source row count;
- partition and raw-source `[start,end)` coverage;
- validation status;
- source artifact logical ID, source kind, checksum, record count and interval.

Storage roots and raw source URIs are intentionally omitted.

## Non-goals

This owner slice does not:

- decide whether Backtest equity uses last, mark or index price;
- define liquidation semantics;
- substitute traded close for missing mark/index evidence;
- change funding cashflows;
- publish Results/Research realism assumptions;
- run canonical full-corpus A/B evidence;
- close Issue #376.

The next Backtest slice should consume this accepted owner contract and implement explicit valuation scenarios with deterministic evidence, rather than introducing ad-hoc CSV readers or inferred price sources.
