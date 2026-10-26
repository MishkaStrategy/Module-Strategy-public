# Backtest Engine bounded-memory execution (P0)

## Incident baseline

A full registered BTC backtest on a 16 GiB macOS host reached an observed physical footprint of approximately **65.2 GiB**, approximately **64.5 GiB swap**, and more than **1 h 30 min** of runtime before completion. Sampling showed the main Python thread repeatedly traversing a very large cyclic object graph. This is treated as a P0 retention/scalability defect, not as ordinary slow execution.

The original `process`, `vmmap`, and `sample` files remain local evidence and are intentionally not committed. The unsafe pre-remediation full-history run was not repeated because doing so would knowingly recreate host-wide memory exhaustion.

## Confirmed retention sources

Static analysis of integration base `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37` and lifecycle profiling confirmed the following multiplicative retention paths:

1. registered Parquet batches were converted to Python dictionaries and then fully materialized as `Candle` objects;
2. application validation retained a full `list[Candle]` and Core validation could create another sequence;
3. checkpoint audit converted the sequence again and executed continuous plus restarted simulations;
4. every not-ready bar produced both a `module.skip` event envelope and a specialized skip record;
5. every bar produced an `EquityPoint`, a frozen `portfolio.mark` payload, and later JSON/JSONL/CSV representations;
6. `Journal` retained the complete event stream, event IDs, entity indexes, and specialized projections together;
7. `JsonLinesEventStore` mirrored the complete stream through an internal in-memory store;
8. base and HTF histories were unbounded, while naïve slicing would create repeated copies;
9. immutable payload construction and deep freezing amplified allocation and GC pressure;
10. reports and replay evidence remained live until the complete run finished.

GC traversal was a consequence of the retained graph. Disabling cyclic GC alone is not used as the fix.

## Execution profiles

### `canonical_audit`

- exact per-bar equity is retained in compact primitive columns;
- repeated skips are represented as deterministic ranges;
- the complete critical event stream is append-only JSONL on disk;
- per-bar `portfolio.mark` envelopes are replaced by the exact compact equity series;
- a bounded event tail and event-type counters remain in RAM;
- diagnostics remain enabled, subject to explicit limits;
- runtime snapshot restart remains a separate explicit audit operation.

### `research_fast`

- uses the same broker, module dispatch, order priority, fill, stop/TP, fee, funding, slippage, and position-state code;
- aggregates repeated skips and suppresses per-bar diagnostics;
- retains exact compact equity for metrics and a sampled curve for export;
- bounds setup, feature, signal, base-history, HTF-history, and event-tail projections;
- does not execute runtime snapshot restart auditing unless the caller explicitly selects canonical audit.

The profile is recorded in result metadata. Differences between profiles are restricted to observability representation; trading results remain exact.

## Registered dataset boundary

Ordinary `checkpoint: disabled` runs use a replayable prepared source:

1. one incremental validation and canonical identity pass;
2. one fresh simulation pass.

This is two source traversals but only **one trading simulation pass**. The simulation calls `ScalableBacktestEngine.run_prepared()` and does not retain a complete registered `list[Candle]`.

Checkpoint audit now uses **runtime snapshot schema v1** rather than deterministic prefix re-simulation:

1. validate and identify the source once;
2. execute the prefix once;
3. write a file-backed bounded runtime payload and hash the append-only event sidecar;
4. close and release the first engine/session;
5. create fresh module instances, verify schema/kernel/payload/event integrity, restore runtime and module state;
6. reopen only the source suffix and continue from the next row.

The prefix is not sent through the broker or strategy modules twice. For local Parquet, completed row groups and rows inside Arrow batches are skipped without constructing prefix `Candle` objects. Registered datasets resume from the deterministic UTC boundary so completed partitions are skipped by the registered reader. The checkpoint path therefore performs one trading simulation, two execution-source opens, and zero replayed strategy rows after the preparation pass.

The snapshot payload is a trusted internal, versioned runtime format. It is never accepted from an arbitrary user-supplied pickle. Payload size, payload SHA-256, event-stream size, event-stream SHA-256, envelope integrity, kernel version, run manifest, module state schema, and source continuation boundary are verified fail-closed before continuation.

## Bounded structures and guards

Default limits are conservative for a 16 GiB development host:

- memory budget: 9,216 MiB;
- retained event tail: 2,000;
- base history: 65,536 candles with chunked compaction;
- each HTF history: 4,096 visible candles with chunked compaction;
- events: 2,000,000;
- skip ranges: 250,000;
- diagnostics: 250,000;
- exact equity points: 5,000,000.

The memory guard is enforced against **current RSS**, not process-lifetime peak RSS. A stale high-water mark from earlier unrelated work therefore cannot falsely terminate a later bounded run. Peak RSS is still recorded for diagnostics.

Runtime counters now include current RSS, peak RSS, retained events, retained base candles, retained HTF candles, journal entity-index entries, equity points, skip ranges, diagnostics, and trades. A resource-limit failure is fail-closed: no authoritative result is published, and `failure.json` records `authoritative=false`, limits, counters, and the largest tracked retained collections.

## Semantic equivalence

Regression coverage compares legacy Core execution with schema-v2 scalable execution for a deterministic order lifecycle. The following are exact matches:

- metrics and final equity;
- every fill, including price, quantity, fee, liquidity, reason, and timestamp;
- every trade and R-multiple;
- order state transitions, excluding observability-only event IDs shifted by removed `portfolio.mark` envelopes;
- exact equity points;
- critical event-type order after filtering legacy per-bar `portfolio.mark` events.

Existing tests continue to cover completed-candle semantics, UTC/HTF alignment, gap resets, fill ordering, stop priority, TP behavior, TTL, end-of-data closes, fees, deterministic replay, and fail-closed publication.

## Reproducible benchmark environment

Measurements below were produced from the exact working tree published by this Draft PR, using:

- Linux `6.12.13-x86_64`;
- CPython `3.13.5`;
- one process, no parallel heavy jobs;
- memory guard `1,024 MiB`;
- `/usr/bin/time -v` for OS peak RSS and swap count;
- `process_time()` for CPU time;
- fresh process per scale point.

The synthetic harness deliberately materializes its input list so it can measure engine behavior independently of registered-source I/O. Consequently, total RSS still includes one synthetic `Candle` list. Registered `run_prepared()` execution avoids that list.

### No-op, `research_fast`, checkpoint disabled

| Bars | Wall, s | CPU, s | Peak RSS, MiB | Swap | Events / retained | Exact / sampled equity | Peak base | Peak HTF per series | Status |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1,000 | 0.066 | 0.064 | 147.2 | 0 | 2 / 2 | 1,000 / 85 | 1,000 | 333 | pass |
| 10,000 | 0.589 | 0.588 | 150.8 | 0 | 2 / 2 | 10,000 / 835 | 10,000 | 3,333 | pass |
| 50,000 | 2.621 | 2.619 | 165.6 | 0 | 2 / 2 | 50,000 / 4,168 | 50,000 | 5,120 | pass |
| 100,000 | 5.276 | 5.274 | 182.6 | 0 | 2 / 2 | 100,000 / 8,335 | 69,632 | 5,120 | pass |
| 250,000 | 13.396 | 13.393 | 230.7 | 0 | 2 / 2 | 250,000 / 20,835 | 69,632 | 5,120 | pass |

From 1k to 250k, total peak RSS increased by approximately **352 bytes per additional synthetic input bar**. The bounded event tail remained constant, base retention plateaued at the compaction threshold, and every HTF series remained at or below 5,120 underlying candles. Exact compact equity intentionally grows with the number of bars.

### Order/fill/trade lifecycle, `research_fast`, checkpoint disabled

The scenario submits one deterministic bracketed market entry every 100 bars and produces repeated fills, targets, trades, and entity-index entries.

| Bars | Wall, s | CPU, s | Peak RSS, MiB | Swap | Events / retained | Orders | Fills | Trades | Journal index | Status |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 10,000 | 0.662 | 0.660 | 151.9 | 0 | 502 / 502 | 200 | 200 | 100 | 400 | pass |
| 50,000 | 3.125 | 3.111 | 170.4 | 0 | 2,502 / 2,000 | 1,000 | 1,000 | 500 | 2,000 | pass |
| 100,000 | 6.383 | 6.370 | 189.4 | 0 | 5,002 / 2,000 | 2,000 | 2,000 | 1,000 | 4,000 | pass |
| 250,000 | 15.837 | 15.812 | 244.8 | 0 | 12,502 / 2,000 | 5,000 | 5,000 | 2,500 | 10,000 | pass |

The event tail is bounded at 2,000. Orders, fills, trades, and terminal entity evidence remain intentionally exact and therefore grow with actual trading activity.

### `canonical_audit`, checkpoint disabled

| Bars | Wall, s | CPU, s | Peak RSS, MiB | Swap | Exact equity | Simulation passes | Status |
|---:|---:|---:|---:|---:|---:|---:|---|
| 10,000 | 0.620 | 0.618 | 151.2 | 0 | 10,000 | 1 | pass |
| 100,000 | 5.268 | 5.266 | 187.2 | 0 | 100,000 | 1 | pass |
| 250,000 | 13.409 | 13.396 | 238.2 | 0 | 250,000 | 1 | pass |

### Runtime snapshot restart audit

Fresh-process synthetic measurements use `canonical_audit`, a 1,024 MiB guard, a midpoint restart, a file-backed runtime payload, and zero prefix re-simulation.

| Scenario | Bars | Wall, s | Peak RSS, MiB | Snapshot bytes | Events / retained | Orders | Fills | Trades | Simulation passes | Prefix rows replayed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| no-op | 10,000 | 0.833 | 155.8 | 949,175 | 2 / 2 | 0 | 0 | 0 | 1 | 0 |
| no-op | 50,000 | 3.781 | 184.5 | 4,345,601 | 2 / 2 | 0 | 0 | 0 | 1 | 0 |
| no-op | 100,000 | 7.286 | 217.6 | 8,319,206 | 2 / 2 | 0 | 0 | 0 | 1 | 0 |
| no-op | 250,000 | 17.797 | 277.5 | 14,956,464 | 2 / 2 | 0 | 0 | 0 | 1 | 0 |
| order lifecycle | 250,000 | 20.980 | 289.7 | 16,839,254 | 12,502 / 2,000 | 5,000 | 5,000 | 2,500 | 1 | 0 |

The event tail remains bounded at 2,000. Exact equity, orders, fills, trades, and entity evidence remain canonical. The result digest is streamed over each evidence series and the full JSONL event-stream hash; it no longer materializes complete equity or event projections.

### `tracemalloc`

A separate 10k no-op research run started `tracemalloc` **after synthetic input construction**:

- traced current: 1,175,255 bytes;
- traced peak: 2,122,591 bytes;
- traced live allocations: 7,216;
- GC time: 0.0511 s;
- wall time with tracing: 3.081 s;
- peak RSS: 152.8 MiB;
- swap: 0.

Do not compare the traced wall time with the speed baseline; allocation tracing is intentionally expensive.

## CPU profile

A `cProfile` run over 100k no-op research bars recorded 51.9 million calls. The main cumulative costs were:

| Function | Cumulative time, s | Interpretation |
|---|---:|---|
| scalable `_run_source` | 15.295 | complete hot loop under profiler |
| `TimeframeStore.push` | 3.496 | four completed-HTF aggregators |
| `calculate_readiness` | 3.235 | module readiness checks |
| `_build_view` | 2.104 | immutable point-in-time market view |
| synthetic `build_bars` | 1.562 | benchmark-only input materialization |
| `calculate_metrics` | 1.497 | post-run exact metrics |
| incremental data-hash update | 1.267 | deterministic input identity |

The profiler confirms that bounded retention has removed GC traversal as the dominant cost. No speculative optimization of trading semantics was made from this profile.

## Registered BTC dataset evidence

The actual Drive-hosted `BTCUSDT_5m.parquet` was downloaded and verified locally:

- bytes: **34,020,301**;
- rows: **721,597**;
- SHA-256: `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1`;
- canonical candle hash: `afe74d8ece22d748136fd05e6436841fec7a40be686d65e1d700e1eca0a55c52`;
- first open: **2019-09-08 17:55:00 UTC**;
- last open: **2026-07-19 06:55:00 UTC**;
- gaps: **0**;
- duplicates: **0**.

Fresh Linux/CPython 3.13.5 full-file runs used a 1,024 MiB guard and `/usr/bin/time -v`:

| Full BTC workload | Mode | Total wall | OS peak RSS | Swap | Orders | Fills | Trades | Snapshot bytes | Result digest |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| deterministic no-op | direct | 70.78 s | 325.7 MiB | 0 | 0 | 0 | 0 | — | `5734cd8242be…dc9e` |
| deterministic no-op | runtime snapshot | 80.65 s | 381.3 MiB | 0 | 0 | 0 | 0 | 30,626,669 | `5734cd8242be…dc9e` |
| order lifecycle | direct | 79.03 s | 335.0 MiB | 0 | 5,230 | 5,230 | 2,615 | — | `76d46fac43bc…bbe5` |
| order lifecycle | runtime snapshot | 90.70 s | 374.3 MiB | 0 | 5,230 | 5,230 | 2,615 | 32,935,155 | `76d46fac43bc…bbe5` |

Both snapshot runs used exactly one trading simulation pass and replayed **zero** prefix rows through strategy/broker execution. Direct and restarted results matched exactly for metrics, equity, event stream, orders, fills, trades, fees, and terminal state. The full lifecycle run paid identical total fees of `87.06233946812` and ended at identical equity `9902.115903631895`.

These full-file workloads validate the engine, data boundary, execution lifecycle, evidence retention, and restart mechanism. They are deterministic validation modules, not a claim about the profitability of the production FVG strategy.

## Reproduction

```bash
python benchmarks/backtest_memory_scalability.py \
  --bars 100000 \
  --scenario noop_module \
  --mode research_fast \
  --checkpoint disabled \
  --max-memory-mb 1024 \
  --output benchmark-100k.json

python benchmarks/backtest_memory_scalability.py \
  --bars 100000 \
  --scenario order_lifecycle \
  --mode research_fast \
  --checkpoint disabled \
  --max-memory-mb 1024 \
  --output benchmark-orders-100k.json

python benchmarks/backtest_memory_scalability.py \
  --bars 100000 \
  --scenario order_lifecycle \
  --mode canonical_audit \
  --checkpoint audit \
  --restart-at-index 49999 \
  --max-memory-mb 1024 \
  --output benchmark-snapshot-100k.json

python -m cProfile -o profile-100k.prof \
  benchmarks/backtest_memory_scalability.py \
  --bars 100000 \
  --scenario noop_module \
  --mode research_fast \
  --checkpoint disabled \
  --max-memory-mb 1024
```

## Acceptance commands

```bash
python -m pytest -m "not slow" tests/backtest_engine
python -m compileall -q \
  src/modular_backtest src/modular_core/timeframes.py \
  tests/backtest_engine benchmarks/backtest_memory_scalability.py
ruff check src/modular_backtest tests/backtest_engine benchmarks/backtest_memory_scalability.py
ruff format --check src/modular_backtest tests/backtest_engine benchmarks/backtest_memory_scalability.py
mypy --strict --disable-error-code import-untyped src/modular_backtest
```

## Known limits and follow-ups

- Exact compact equity grows with bars by design because it is the authoritative metric source.
- Exact orders, fills, trades, and terminal entity indexes grow with genuine trading activity; they are canonical evidence rather than observability noise.
- Runtime snapshot schema v1 is an internal trusted format tied to the kernel version and module state schemas. A future incompatible schema requires an explicit migration, not permissive deserialization.
- The current checkpoint option is an audit/recovery execution inside one application invocation. Publishing a durable operator-managed pause/resume command across separate invocations would be a separate product feature, not a memory-safety requirement for this PR.
- A strategy whose undeclared lookback exceeds configured history limits must declare the real lookback and raise the limits explicitly.
- The full BTC evidence above uses deterministic validation modules. Production-strategy performance evidence still requires the exact strategy configuration and is intentionally separate from engine scalability acceptance.
