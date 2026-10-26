# Hardening log — release gates 49–54

These rounds extend the implementation hardening log with evidence obtained after the
initial GitHub publication checkpoint.

## Round 49 — repository quality and packaging matrix

The permanent component workflow was hardened until both Python 3.12 and Python 3.13
passed compileall, Ruff lint/format, strict MyPy, the dependency-free static audit, 221
tests including Hypothesis and PyArrow, 80% branch coverage, wheel/sdist build, Twine
verification and clean wheel installation. Coverage was raised with behavioral tests rather
than exclusions or reduced thresholds.

## Round 50 — live Binance signed premium-index drift

A public unauthenticated Binance run exposed that premium-index klines are signed basis
values and may be zero or negative. `PriceKlineRecord` now permits finite signed OHLC only
for `premium_index`; mark and index prices remain strictly positive. Regression tests cover
direct construction and the Binance parser.

## Round 51 — observable public Binance acceptance

The permanent manual workflow generates a fully closed one-hour BTCUSDT USD-M request,
uses public endpoints only, preserves exit/output evidence, registers the real dataset,
runs deep integrity validation and archives bounded evidence. The verified run passed with
7 raw artifacts, 7 partitions and 54 checked rows and no integrity issues.

## Round 52 — multi-year real-data bounded-memory acceptance

A reproducible benchmark processed 721,597 BTCUSDT 5m rows from 2019-09-08 through
2026-07-19 using bounded Arrow batches, strict `KlineRecord` conversion, acquired/validated
SQLite spool stages, streaming validation, 83 monthly Parquet partitions and complete
checksum-verified typed reread. Validation returned `VALID` with zero issues, gaps,
duplicates or ordering errors. Peak RSS was 311,468,032 bytes, below the 1 GiB gate.

## Round 53 — official-data direct-vs-derived equivalence

The module aggregated the complete 5m source and compared every result with independent
15m and 1h Parquet datasets: 240,532 15m rows and 60,133 1h rows. UTC boundaries, OHLC,
trade counts and row counts were exact. Aggregate volume fields differed only by binary
floating-point reduction effects; maximum relative error was below `3e-16`, inside the
recorded `1e-12` policy.

## Round 54 — production adapter full-history acquisition

The Binance adapter downloaded the complete available BTCUSDT USD-M perpetual 5m history
using official monthly archives and bounded REST for partial months:

- exact start `2019-09-08T17:55:00Z`;
- end `2026-07-27T19:20:00Z` exclusive;
- 724,049 kline rows and two metadata rows;
- 83 monthly kline partitions and 85 total partitions;
- 98 logical raw artifacts;
- validation status `VALID`;
- zero issues, gaps, duplicates, warnings or unresolved dataset issues;
- deep integrity status `ok` across 724,051 rows;
- download/registration elapsed time 210 seconds.

The acceptance required the exact known earliest timestamp and a row-count floor based on
the previously validated 721,597-row dataset. Both checks passed, proving that orchestration
listing-date resolution and archive/REST planning do not truncate the early history.

## Current evidence gate

Passed:

- Python 3.12 and 3.13 quality/package matrix;
- Ruff, strict MyPy, Hypothesis, PyArrow and clean installation;
- public Binance small-range download and deep integrity acceptance;
- signed premium-index live regression;
- multi-year bounded-memory validation/publication/reread benchmark;
- official-data 5m-to-15m/1h direct-vs-derived equivalence;
- complete BTCUSDT earliest-history production-adapter acquisition;
- component and Core CI on reviewed clean heads.

Still intentionally open:

- multi-year live update/resume/repair lifecycle acceptance;
- human review and authorized integration of Draft PR #221.

No merge, release, deployment, package publication or live trading was performed.
