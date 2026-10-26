# BTCUSDT Drive dataset attestation

## Identity

- Attestation ID: `btcusdt-drive-history-20260719-v1`
- Attestation schema: `1`
- Canonical JSON SHA-256: `e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91`
- Status: `BYTE_AND_CONTENT_ATTESTED_MARKET_ENDPOINT_UNPROVEN`
- Decision queue item: `STRATEGY-312-05`
- Source provider: Google Drive
- Source folder ID: `1mm_1z1alqfTUF_8stXoaccCQergs6Sk_`
- Source folder label: `binance_usdtm_futures_history`
- Symbol: `BTCUSDT`
- Timezone: `UTC`

The source folder label claims Binance USDT-M futures history. The retained download configuration does not record an API endpoint or explicit market-type field. Therefore the dataset bytes and contents are attested, but Binance USDT-M endpoint provenance is not yet proven.

## Committed source evidence

- Download configuration:
  - path: `research/strategy_audit_validation/evidence/btcusdt-drive-download-config.json`
  - Drive file ID: `1OHVgRkhLtAxJQMuDb6SqhH-2pig5DuZ1`
  - size: `481` bytes
  - SHA-256: `ffd5b7d777e683f960dccc43ee0cdea6d2a8ca7abab795b7dddd28c6e345d5fe`
- Download summary:
  - path: `research/strategy_audit_validation/evidence/btcusdt-drive-download-summary.csv`
  - Drive file ID: `1nIsjXMI64hPLVu2SpoLbfE9BaRVU_WBs`
  - size: `3882` bytes
  - SHA-256: `a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893`
- Machine-readable attestation:
  - path: `research/strategy_audit_validation/evidence/btcusdt-drive-dataset-attestation-v1.json`

The raw Parquet files remain in Google Drive because their combined size is unsuitable for this governance PR. Their byte identities are pinned below.

## Raw files

| Timeframe | Drive file ID | Rows | Size | SHA-256 |
|---|---|---:|---:|---|
| 5m | `1yfVgqpzxaev0cyOtxW8obdx57MlSJNR6` | 721597 | 34020301 | `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1` |
| 15m | `1fd-Kwzh9XWkG2dxVyzvZp9s8OxTYsFvn` | 240532 | 14746795 | `05d8dbe7c8f71383171dd01a635f8c0cfad8e498d2043f5825715a3600fa116f` |
| 1h | `18k4utKCm1yABDRvDz3Oo93niKcI7ru0i` | 60133 | 4660189 | `ed51fd133eaf7d318959dcd21de68c67c02901ab911c02077a30c913d7bbc067` |
| 4h | `1goH8ED_pysHoH0OMdJAqfUmIZI2zG51J` | 15032 | 1059387 | `1fdfd3f4df53f03ffbed0c833f47ed3f5902ec9e5ea3c2274aceb96e7d45135f` |

## Coverage

### 5m base

- First open: `2019-09-08T17:55:00.000Z`
- First close: `2019-09-08T17:59:59.999Z`
- Last open: `2026-07-19T06:55:00.000Z`
- Last close: `2026-07-19T06:59:59.999Z`
- Rows: `721597`
- Gap events: `0`
- Estimated missing rows: `0`
- Duplicate timestamps: `0`
- Interval mismatches: `0`
- Close-time mismatches: `0`

### Derived files

- 15m: `2019-09-08T18:00:00Z` through `2026-07-19T06:45:00Z`
- 1h: `2019-09-08T18:00:00Z` through `2026-07-19T06:00:00Z`
- 4h: `2019-09-08T20:00:00Z` through `2026-07-19T00:00:00Z`

Every stored 15m, 1h, and 4h row was independently compared with UTC-aligned aggregation from the 5m source.

## Independent validation

The verification did not trust the CSV summary as the content source. It independently:

1. parsed the Parquet compact-Thrift footer;
2. decompressed ZSTD data pages;
3. decoded RLE, dictionary, and plain encoded columns;
4. scanned every row and every attested column;
5. checked timestamp monotonicity, duplicate and interval counts;
6. checked OHLC body/range invariants;
7. checked finite and non-negative volume/trade fields;
8. checked taker-buy values do not exceed total values;
9. rebuilt 15m, 1h, and 4h bars from 5m in UTC;
10. compared timestamps, OHLC, close times, trades, volume, quote volume, and taker-buy totals.

Validation report SHA-256: `1a97613471aca6cdcd59c6fced740787f2e1fdb1d745b1e33b4ca400753bdcfd`.

### Base invariant verdict

All checked violation counts are zero:

- non-finite numeric values;
- non-positive prices;
- high below open/close or low;
- low above open/close;
- negative volume, quote volume, trades, or taker-buy values;
- taker-buy base above total volume;
- taker-buy quote above total quote volume;
- zero-volume/nonzero-quote anomalies;
- zero-trade/nonzero-volume anomalies.

### Derived parity verdict

For 15m, 1h, and 4h:

- row counts match;
- open/high/low/close match exactly;
- timestamps and close times match exactly;
- trade counts match exactly;
- volume, quote volume, taker-buy base, and taker-buy quote match within floating-point summation tolerance.

The largest observed absolute difference was `3.814697265625e-06` in 4h quote-volume summation.

## Fail-closed validator

Public API:

- `load_drive_dataset_attestation`
- `verify_attestation_repository_sources`

The loader accepts only the immutable canonical v1 payload. Any unapproved mutation changes the canonical SHA-256 and is rejected. The source verifier recomputes the committed config and summary sizes and SHA-256 values.

The validator explicitly requires these unresolved gates to remain false:

- market endpoint proven;
- dataset registered in repository;
- component ledgers present;
- combined 395-trade ledger present;
- strategy reproduction complete.

## Remaining STRATEGY-312-05 requirements

The dataset byte/content identity is no longer missing. The remaining requirements are:

1. authoritative proof of the Binance USDT-M API endpoint or equivalent source lineage;
2. approved repository dataset ID and version;
3. 283-trade Final 15m ledger and SHA-256;
4. 91-trade Frozen 1H ledger and SHA-256;
5. 21-trade Frozen 4H ledger and SHA-256;
6. combined 395-trade ledger and SHA-256;
7. pre-portfolio signal counts;
8. execution-contract SHA-256;
9. fees, funding, slippage, and final-equity controls;
10. combined semantic digest and exact generating commit.

## Scope boundary

This attestation does not:

- prove the missing Frozen 4H strategy rules;
- approve any runtime adapter or composition order;
- prove mark-price, index-price, funding-rate, or liquidation streams;
- reproduce the 395 accepted trades;
- make the composition canonical, executable, paper-ready, or live-ready.
