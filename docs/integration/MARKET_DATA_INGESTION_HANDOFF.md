# Historical Market Data Ingestion — integration handoff

## Canonical identity

- repository: `MishkaStrategy/Module-Strategy`;
- component branch: `data/historical-market-data-ingestion`;
- branch base: `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`;
- integration target: `integration/modular-strategy-v0.6.0-rc`;
- component package: `modules/market_data_ingestion`;
- Python distribution: `modular-market-data-ingestion`;
- import package: `market_data_ingestion`;
- CLI: `market-data`;
- package version: `0.4.0`.

The component branch was created directly from the exact canonical staging head above.
Do not retarget it to `main`, frozen Core, or a historical integration checkpoint.

## Scope

The component provides deterministic, resumable historical market-data ingestion with:

- strict exchange, market and instrument resolution;
- immutable raw, normalized and derived storage layers;
- UTC-normalized traded klines, mark/index/premium-index klines, funding events,
  open-interest history and exchange/instrument metadata;
- capability-driven exchange adapters;
- public Binance Spot and USDT-M futures acquisition where implemented;
- validation, duplicate/gap evidence, update, backfill, repair, resume, cancel,
  inspect, deep validate and export;
- immutable dataset manifests and SQLite dataset registration;
- adapters for Backtest, Research Runner and Results Aggregator lineage.

It is a market-data component only. It does not place orders, load private exchange
credentials, sign requests, or enable live execution.

## Quality and packaging evidence

The permanent component matrix passes on Python 3.12 and 3.13 with 221 tests including
Hypothesis and PyArrow, `ResourceWarning` as an error, compileall, Ruff lint/format, strict
MyPy, a zero-finding static audit, 80% branch coverage, wheel/sdist build, Twine verification
and clean wheel installation. The matrix contains no lowered thresholds, invented ignores
or generated type baselines.

## Public Binance small-range acceptance

The public unauthenticated BTCUSDT USD-M workflow passed on a closed one-hour range. It
exercised traded klines, mark-price, index-price and signed premium-index records, immutable
registration, Parquet storage and deep integrity validation. Evidence contained download
exit code `0`, zero gaps/duplicates/unresolved issues, 7 raw artifacts, 7 partitions, 54
checked rows and deep integrity status `ok`.

A live run exposed and fixed the signed premium-index contract: premium basis may be zero
or negative, while mark and index prices remain strictly positive.

## Multi-year bounded-memory evidence

A full BTCUSDT 5m Parquet dataset containing 721,597 rows from 2019-09-08 through
2026-07-19 passed strict typed normalization, acquired/validated SQLite spool stages,
streaming validation, 83 monthly Zstandard Parquet partitions and checksum-verified typed
reread. Validation was `VALID` with zero issues, gaps, duplicates or ordering errors.
Maximum monthly materialization was 8,928 records and peak RSS was 311,468,032 bytes.

## Direct-vs-derived timeframe evidence

The complete 721,597-row 5m source was aggregated by `aggregate_klines_stream` and compared
with independent direct files: 240,532 15m candles and 60,133 1h candles. UTC boundaries,
OHLC, trade counts and row counts were exact. Maximum relative aggregate-volume error was
below `3e-16`, inside the recorded `1e-12` policy.

## Complete Binance history evidence

The production Binance adapter downloaded and registered the complete BTCUSDT USD-M
perpetual 5m history using official monthly archives and bounded REST for partial months:

- exact start `2019-09-08T17:55:00Z`;
- end `2026-07-27T19:20:00Z` exclusive;
- 724,049 kline rows and two metadata rows;
- 83 monthly kline partitions and 85 total partitions;
- 98 logical raw artifacts;
- status `VALID`, with zero issues, gaps, duplicates, warnings or unresolved issues;
- deep integrity `ok` across all 724,051 rows;
- download and registration completed in 210 seconds.

The run required the exact known first timestamp and exceeded the established 721,597-row
baseline, proving that listing-date orchestration and archive/REST planning do not truncate
the early history.

## Multi-year lifecycle evidence

The live public Binance lifecycle gate passed across update/repair run `30369425360` and
exact-head restart/resume run `30372890842`:

- immutable v1 contained 721,597 traded 5m klines and was `VALID`;
- append-only update published v2 with 724,049 klines through
  `2026-07-27T19:20:00Z`, while the v1 manifest remained unchanged;
- deliberate damage to the newly published v2 partition was detected and quarantined;
- repair published v3 with status `REPAIRED`, no unresolved gaps and deep integrity `ok`;
- a separate multi-year process was terminated after 4 durable chunks with 90 pending;
- resume restored 4 chunks from immutable raw evidence without changing their hashes;
- all 94 chunks completed with zero pending chunks;
- the canonical terminal state and checkpoint were `COMPLETED_WITH_WARNINGS`, carrying only
  the expected resume warning;
- the resumed dataset contained 721,597 traded klines, was `VALID`, and passed deep integrity
  across 85 partitions, 96 raw artifacts and 721,599 rows including metadata.

Readable and machine-readable evidence is committed as
`modules/market_data_ingestion/reports/LIFECYCLE_ACCEPTANCE_BTCUSDT_0_4.md` and
`modules/market_data_ingestion/reports/lifecycle-acceptance-btcusdt-0-4.json`.

The reproducible scripts, immutable JSON and readable reports live under
`modules/market_data_ingestion/scripts` and `modules/market_data_ingestion/reports`.

## Required integration method

Keep this PR Draft until human review and explicit integration authorization. Integration
into the canonical staging branch must preserve history and follow the v0.6 handoff policy.
Do not force-push, rebase, squash, enable auto-merge, publish a package, create a release,
or merge to `main` without explicit authorization.

Before integration, verify:

```bash
git merge-base --is-ancestor \
  9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8 \
  data/historical-market-data-ingestion
```

Then use an ordinary history-preserving merge into
`integration/modular-strategy-v0.6.0-rc` only after review and exact-head gates.

## Remaining follow-up gate

The component quality, packaging, public-network, bounded-memory, direct-vs-derived,
complete earliest-history and live update/resume/repair gates have passed. The only
remaining explicit gate is final human review and authorized integration of Draft PR #221.

The manual Binance workflows use public unauthenticated endpoints only. They must never be
extended with account credentials or private trading endpoints as part of this component.
