# Fibonacci cycle-4 regime evidence

Дата standalone candidate: 2026-07-28.

## Standalone candidate gate

- package version: `0.4.0`;
- tests: `181 passed`;
- branch-aware coverage: `90.63%`;
- clean source-tree SHA-256: `58885785df85639e0edf35ff91680a7f888032f20278a16bede9fc628d74e0af`;
- wheel SHA-256: `ce104cdf1df5155d81b60b6a470d32df6b0a3f9115bfb19fdd72661ee4ade2f3`;
- selective patch SHA-256: `ce0126428062339b4e58c894f183a77d3662c372645df4f3f07050b3a5a62514`.

Эти hashes описывают standalone candidate до выборочного переноса в namespace `modular_strategy_fibonacci`; GitHub tree после переноса закономерно имеет другой source hash.

## GitHub namespace quality gate

Replay source head: `8a8059b8d2beebcc06bfb078cd3479c41cdfb8ad`.

- Python 3.12 — Ruff, strict MyPy, `217 passed`, branch coverage `91%`, wheel и metadata checks: success;
- Python 3.13 — тот же gate в изолированном `python:3.13-slim` на self-hosted `docker` runner: success;
- statements: `1649`;
- branches: `574`;
- verified wheel SHA-256: `c95bf0aa0fd080f175be14e591aad68d03242cad1b018a568f8671831615c8ae`;
- wheel artifact ZIP SHA-256: `a69e431e5828e7964d314c9ec4bd65958dff5e665aed5e7b1babe2dc2ad1b534`.

Wheel построен и проверен в CI, затем использован для локального raw replay без восстановления исходников вручную.

## Raw BTC/ETH/SOL replay after namespace port

Каждый поток был обработан слева направо через `SequentialFibonacciEngine.process` по закрытым свечам. 1d построен только из полных UTC-дней с шестью последовательными 4h свечами `00/04/08/12/16/20`; неполные граничные дни исключены.

| Symbol | Raw 4h rows | 4h range | Raw SHA-256 | Strict 1d rows |
|---|---:|---|---|---:|
| BTCUSDT | 15,032 | 2019-09-08 20:00 — 2026-07-19 00:00 UTC | `1fdfd3f4df53f03ffbed0c833f47ed3f5902ec9e5ea3c2274aceb96e7d45135f` | 2,505 |
| ETHUSDT | 14,555 | 2019-11-27 08:00 — 2026-07-19 00:00 UTC | `be8a7640dd2b4ed20404f3cdfb76efdac300137659f44537b4bf24fcf584ce78` | 2,425 |
| SOLUSDT | 12,803 | 2020-09-14 08:00 — 2026-07-19 00:00 UTC | `967979abd61fb91afd332d24af9d15e56758d8fe7fc9f798614085e0654e998c` | 2,133 |

Проверки input integrity: все 11 Parquet columns прочитаны полностью; null timestamps отсутствуют; `open_time` строго возрастает; duplicate `open_time` отсутствуют; OHLCV contracts проходят при создании каждой `Candle`.

### Final 4h scanner

| Symbol | Historical | Recent | Prior | Regime | Recent 0.5 | Recent 0.618 | Terminal streak | Active setup |
|---|---:|---:|---:|---|---:|---:|---:|---|
| BTCUSDT | 18/29 = 62.07% | 6/12 = 50.00% | 10/12 = 83.33% | weakening | 3/5 = 60.00% | 3/7 = 42.86% | 0 | no |
| ETHUSDT | 24/35 = 68.57% | 9/12 = 75.00% | 6/12 = 50.00% | improving | 5/6 = 83.33% | 4/6 = 66.67% | 1 | no |
| SOLUSDT | 31/40 = 77.50% | 6/12 = 50.00% | 11/12 = 91.67% | weakening | 4/6 = 66.67% | 2/6 = 33.33% | 0 | no |

Эти результаты точно воспроизводят округлённые standalone candidate values для BTCUSDT, ETHUSDT и SOLUSDT. Scanner payload остаётся `fibonacci.bearish_reaction_scan/v2`, а `orders` и `trades` пусты.

### Strict UTC 1d scanner

- BTCUSDT: `3` bearish observations;
- ETHUSDT: `4` bearish observations;
- SOLUSDT: `7` bearish observations.

Для всех трёх инструментов 1d остаётся `insufficient_data`; активного setup на финальном cutoff нет.

## History-only walk-forward

Standalone candidate использовал frozen history на каждом сохранённом cutoff. Для SOL 4h legacy filter дал 5 selections, linked filter — 2, linked+regime — 2, linked+regime+level — 2. Выборка слишком мала для статистического или PnL-вывода.

Raw replay подтвердил, что каждый GitHub helper получает только snapshot текущего cutoff и не обращается к будущим свечам. Точный standalone набор из пяти сохранённых cutoffs не был включён в candidate artifact, поэтому исходные значения `5/2/2/2` не подменяются новым exhaustive per-bar sampling. Point-in-time, grouping, duplicate-cutoff rejection и empty-output semantics закреплены regression tests.

## Ограничения точности

- PnL/прибыльность не рассчитывались;
- на финальном проверенном cutoff активного short setup нет;
- standalone walk-forward sample мал и подтверждает только point-in-time/gating logic;
- точный standalone cutoff manifest не был сохранён, поэтому его малую выборку нельзя независимо реконструировать из одних final aggregates;
- raw BTC/ETH/SOL datasets, caches и wheel не добавляются в GitHub tree;
- CI wheel публикуется только как краткоживущий GitHub Actions artifact.
