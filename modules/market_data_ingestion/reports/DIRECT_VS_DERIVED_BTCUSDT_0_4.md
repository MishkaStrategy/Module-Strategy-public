# Real BTCUSDT Direct-vs-Derived Timeframe Cross-Check — v0.4

## Result

**PASS** — `aggregate_klines_stream` reproduced the complete multi-year BTCUSDT 15m and
1h datasets from the 5m source with exact candle boundaries, OHLC prices and trade counts.
Aggregate volume-field differences were limited to normal IEEE-754 summation rounding.

## Inputs

| Timeframe | Rows | SHA-256 |
|---|---:|---|
| 5m | 721,597 | `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1` |
| 15m | 240,532 | `05d8dbe7c8f71383171dd01a635f8c0cfad8e498d2043f5825715a3600fa116f` |
| 1h | 60,133 | `ed51fd133eaf7d318959dcd21de68c67c02901ab911c02077a30c913d7bbc067` |

All files use UTC timestamps and the same OHLCV/trade schema.

## 15m comparison

- Compared rows: 240,532 of 240,532
- Range: 2019-09-08T18:00:00Z through 2026-07-19T06:45:00Z
- Timestamp mismatches: 0
- Close-time mismatches: 0
- Trade-count mismatches: 0
- Open/high/low/close mismatches: 0
- Maximum volume error: `0.00000000001`
- Maximum quote-volume error: `0.000001`
- Maximum taker-buy-base error: `0.000000000005`
- Maximum taker-buy-quote error: `0.0000003`
- Maximum observed relative aggregate-field error: below `3e-16`
- Derived digest:
  `3d5db130379bcbb6d451a480be74a1d73416be448510449979396a0c37930e2f`

## 1h comparison

- Compared rows: 60,133 of 60,133
- Range: 2019-09-08T18:00:00Z through 2026-07-19T06:00:00Z
- Timestamp mismatches: 0
- Close-time mismatches: 0
- Trade-count mismatches: 0
- Open/high/low/close mismatches: 0
- Maximum volume error: `0.00000000003`
- Maximum quote-volume error: `0.000001`
- Maximum taker-buy-base error: `0.00000000001`
- Maximum taker-buy-quote error: `0.000001`
- Maximum observed relative aggregate-field error: below `3e-16`
- Derived digest:
  `54e69ca0e3ffab1eb0e41be0e857fdc14defe1b0aad455631346a191c4d36786`

## Acceptance policy

The gate required:

- exact row counts;
- exact UTC open and close boundaries;
- exact trade counts;
- price-field absolute error no greater than `1e-12`;
- aggregate-field absolute error no greater than `1e-8` or relative error no greater
  than `1e-12`.

Both timeframes passed every criterion. Price fields were exactly equal. Non-zero sum-field
comparisons reflect the difference between Decimal accumulation of source float strings and
the direct Parquet files' binary floating-point reductions; the maximum relative difference
was approximately three ten-quadrillionths.

## Runtime evidence

- Package: `market_data_ingestion` 0.4.0
- Python: 3.13.5
- PyArrow: 25.0.0
- Platform: `Linux-6.12.13-x86_64-with-glibc2.41`
- Total elapsed: 52.704 seconds
- Peak RSS: 350,576,640 bytes
- Completed: 2026-07-27T19:00:19.278114Z

This closes the official-data direct-vs-derived 15m/1h aggregation cross-check. It does not
authorize merge, release, deployment or live trading.
