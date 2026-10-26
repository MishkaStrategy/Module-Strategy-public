# Breakout–Return deep-optimization baseline

## Scope

Module: `strategy_modules.breakout_return.BreakoutReturnModule`

Base branch: `integration/modular-strategy-v0.5.0`

Base commit: `5e03b933207a99940244634f49a756d27888a5c3`

Working branch: `modules/breakout-return-v1.1.0-hardening`

Source blob at baseline: `src/strategy_modules/breakout_return/module.py` SHA `1febab97aab477c2c2e9e615cd5dbc5de455165e`.

Module version: `1.0.0`.

Module API: `1.0` draft, revision 3.

Core: Modular Strategy Core `0.4.0`.

Python CI matrix: `3.12`, `3.13`.

## Baseline repository evidence

Integration PR: #8, head `5e03b933207a99940244634f49a756d27888a5c3`.

All five workflows on the baseline head completed successfully:

- `core-ci`;
- `integration-ci`;
- `session-range-ci`;
- `market-regime-ci`;
- `fvg-module-ci`.

Python 3.12 integration artifact evidence:

- root/core + Breakout–Return + Liquidity Sweep + integration: 244 tests;
- Session Range: 84 tests;
- Market Regime: 80 tests;
- FVG: 70 tests;
- total: 478 passed, 0 failed/errors/skipped;
- root statement coverage: `3633 / 4116 = 88.265306%`;
- deterministic smoke run ID: `run-a913a83ae17411b4`;
- smoke: 514 events, 159 features, 2 setups, 1 confirmed SELL;
- four wheels built and installed in a clean virtualenv in CI.

Breakout–Return-specific baseline tests in the root JUnit report: 63 tests:

- 48 unit/edge/negative cases;
- 11 regression cases;
- 4 real-core integration cases.

Breakout–Return coverage:

- `__init__.py`: 100%;
- `config.py`: 100%;
- `models.py`: 94.55%;
- `module.py`: 89.46% (`399 / 446` statements).

## Baseline package and public contract

Dependencies:

- Python `>=3.12`;
- `PyYAML>=6.0` through the root package;
- no module-specific runtime dependency.

Loading:

- public import path: `strategy_modules.breakout_return:BreakoutReturnModule`;
- no module entry point is declared at baseline;
- import-path loading and YAML loading are covered by integration tests.

Produced features:

- `breakout_return.active_setup`;
- `breakout_return.confirmed`.

Feature-mode dependency is the configured namespaced `reference_feature_key`.

State contract:

- `ModuleManifest.state_schema_version = "1"`;
- payload version `1`;
- JSON-compatible extrema queues, active setup, stream identity, last-bar signature and sequence;
- exact module-version and state-schema checks are supplied by core checkpoints.

## Local baseline profile

Environment: CPython `3.13.5`, Linux container, source extracted from the verified baseline wheel.

Workload: rolling mode, lookback 1,000, breakout disabled by a 5,000 bps buffer. Five runs per short/standard workload. `tracemalloc` enabled. Restore median measured over 100 restores per run.

| Events | Runs | Median seconds | Min | Max | Median throughput | Median peak memory | State size | Median restore |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 5 | 0.070854 | 0.069517 | 0.074452 | 14,114 events/s | 21,942 B | 390 B | 13.78 µs |
| 50,000 | 5 | 4.465197 | 4.433628 | 4.571878 | 11,198 events/s | 457,708 B | 446 B | 15.12 µs |

The measured memory includes the module call path and the caller-owned growing visible-bar list under `tracemalloc`; module state itself is bounded by the configured rolling window and serialized to 446 bytes for this monotonic synthetic workload.

## Baseline findings to investigate

1. Configuration fields rely on annotations but direct construction does not strictly reject every bool/float misuse for integer fields.
2. Financial thresholds are stored as floats and converted to `Decimal` repeatedly in the hot path.
3. State payload has no configuration fingerprint, so direct restore can accept a structurally compatible state from different parameters.
4. Numeric duplicate signatures are representation-sensitive (`100` versus `100.0`) because serialized Decimal strings are compared literally.
5. Restored extrema are checked for monotonicity and upper sequence bounds, but not proven to lie inside the restored rolling-history horizon.
6. Feature mode has no optional stale-reference age guard; Feature Store expiry is respected, but an unbounded producer feature can remain eligible indefinitely.
7. The module is import-path loadable but has no root package entry point, unlike several neighboring modules.
8. Existing benchmark reports one best-style comparison rather than five-run min/median/max, throughput, peak memory, checkpoint size and restore latency.
9. `module.py` still uses `Any` for candle arguments despite the real API exposing `Candle`.
10. No explicit cross-process `PYTHONHASHSEED` determinism test is present.

These are baseline hypotheses, not yet accepted defects. Each will be reproduced or rejected by tests before code changes.
