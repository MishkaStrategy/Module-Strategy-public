# Binance BTCUSDT Full-History Acceptance — v0.4

## Result

**PASS** — the production Binance adapter downloaded and registered the complete available
BTCUSDT USD-M perpetual 5m history from the exact known first candle through the latest
closed candle, then passed deep raw/partition/manifest integrity validation.

## Request

- Exchange: Binance
- Market: USD-M perpetual
- Instrument: BTCUSDT
- Required data: traded 5m klines only
- Range mode: `full_available_history` through `latest_closed`
- Source policy: official monthly archives plus bounded public REST for partial months
- Storage: monthly Parquet with Zstandard compression
- Authentication: none
- Private endpoints: none

The acceptance intentionally excluded multi-year auxiliary REST streams. Funding, mark,
index, premium and open-interest lifecycle coverage is tracked separately; this gate tests
the complete traded-kline history path through the production adapter.

## Dataset evidence

- Start: `2019-09-08T17:55:00Z`
- End: `2026-07-27T19:20:00Z` exclusive
- Kline rows: 724,049
- Rows including instrument/exchange metadata: 724,051
- Kline monthly partitions: 83
- Total registered partitions including metadata: 85
- Dataset status: `VALID`
- Final run state: `COMPLETED`
- Gaps: 0
- Duplicates: 0
- Validation issues: 0
- Warnings: 0
- Unresolved dataset issues: 0
- Dataset SHA-256:
  `198844fea7b8efbe35eb30a5bddb46cef8c87a7175f370554d6dbe372e83cfb2`

The run enforced the existing baseline floor of 721,597 rows and required the exact known
first timestamp. The additional 2,452 records extend the earlier 2026-07-19 baseline through
the acceptance run's latest closed candle.

## Deep integrity evidence

- Status: `ok`
- Raw artifacts checked: 98
- Partitions checked: 85
- Rows checked: 724,051
- Integrity issues: 0

Raw storage used 38,823,956 bytes and normalized storage used 47,661,873 bytes. The raw
file count was 196 because each logical raw artifact has payload and metadata evidence.

## Runtime evidence

- Download and registration elapsed time: 210 seconds
- Workflow head:
  `6b1e855a7ae6c1aafd69fc4e107f485573e8876d`
- Run ID: `mdi:418e877f2dc774e27d9e12d7`
- Download exit code: 0
- Evidence artifact:
  `market-data-ingestion-full-history-30297784757`

## Acceptance criteria

All criteria passed:

- exact earliest timestamp;
- row count at or above the known multi-year baseline;
- required kline stream present;
- validation status `VALID`;
- zero gaps, duplicates, issues and warnings;
- no unresolved dataset issues;
- deep raw, partition and manifest integrity status `ok`.

This closes the full earliest-history Binance adapter gate for BTCUSDT 5m. It does not
close the separate multi-year live update/resume/repair lifecycle gate and does not
authorize merge, release, deployment, package publication or live trading.
