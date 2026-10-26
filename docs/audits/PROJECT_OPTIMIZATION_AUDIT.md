> Historical report; physical runner identifiers are pseudonymized in this export. This is not new-root acceptance.

# Project-Wide Optimization Audit

**Repository:** `MishkaStrategy/Module-Strategy`
**Audited base:** `integration/modular-strategy-full-rc` at `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
**Audit branch:** `audit/project-wide-optimization-review`
**Draft PR:** #288
**Audit date:** 2026-08-01
**Scope:** evidence-driven performance, memory, I/O, scalability, correctness, architecture, tests, CI, dependencies, security and observability. Production code and trading semantics were not changed.

Score interpretation: `0` is poor/high risk, `5` is strong. “Optimization need” uses `0 = no current need`, `5 = immediate need`.

## 1. Executive summary

The project is not uniformly “slow” and does not justify a broad rewrite. The audit classifies **2 components as `OPTIMIZE NOW`**, **4 as `OPTIMIZE SOON`**, **12 as `MONITOR`**, and **14 as `NO OPTIMIZATION NEEDED`**.

The only confirmed project-wide P0 is the integrated **Core/Backtest retention model**. A real full registered BTC run reached approximately **65.2 GB physical footprint and 64.5 GB swap** on a 16 GB Mac. Static inspection and local scale tests confirm that this is architectural multiplicative retention rather than one isolated leak: candles are materialized multiple times, HTF histories are unbounded, every bar generates retained equity and event objects, the journal keeps several indexes/projections, the JSONL event store mirrors all events in memory, and checkpoint mode deliberately performs more than one simulation. Draft PR #287 is the correct active remediation workstream; this audit does not duplicate its production changes.

The second clear optimization area is **CI/CD**. The exact base SHA produced **50 workflow runs, 84 jobs and 5,213 job-seconds (~1.45 hours)**, while only about **22 seconds** ran on GitHub-hosted runners. Cost in billed hosted minutes is therefore currently small, but self-hosted queue pressure and feedback latency are material. Repeated installation/build steps, overlapping same-SHA triggers, mutable Action tags and inconsistent use of the mandatory reusable preflight are the main targets.

Historical ingestion is memory-bounded but slow: a measured 721,597-row BTCUSDT 5m pipeline took **1,101.233 seconds (~655 rows/s)**, dominated by validation and checksum rereads. This is a P2 throughput optimization, not a correctness emergency.

Most analytical and execution modules already have strong deterministic, bounded-state and large-workload evidence. Rewriting Session Range, Market Regime, FVG, Entry, Protection/Target, Position Scaling, Position Exit, Wave, Fibonacci or Strategy Factory would be speculative.

## 2. Repository state

| Item | Live state at audit start |
|---|---|
| Default branch | `main` |
| Current full-project candidate | `integration/modular-strategy-full-rc` |
| Audited base SHA | `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37` |
| Canonical integration PR | Draft PR #265 |
| Audit branch | `audit/project-wide-optimization-review` |
| Audit PR | Draft PR #288 |
| Backtest P0 workstream | Draft PR #287 |
| Base CI | Full component matrix green after one superseded runner-preflight transient |
| Release mode | Paper-only; live execution and release remain blocked |

The exact base had successful Core, Backtest Engine, Market Data, persistent registration, Research Runner, Results Aggregator, Strategy Factory, Strategy Audit, Paper Execution, Trading Runner, analytical-module and full-release-readiness workflows.

One earlier Core run on the same SHA failed before Python provisioning because a self-hosted runner reported `0 GiB free` with a 1 GiB minimum. Its Python 3.13 job passed, and a parallel Core run started three seconds later passed Python 3.12 and 3.13. This is classified as an infrastructure transient and runner-health issue, not a source-code failure.

Open governance/development context was reviewed. In particular, PR #287 already owns the Backtest memory remediation and PR #265 must remain Draft and paper-only.

## 3. Component map

The repository contains **1,918 tracked files, 1,135 Python files, 257,949 Python lines, 549 test files, 59 base workflows and 21 separately packaged projects**.

Primary data flow:

`completed market data → Core/ModuleRegistry → analytical modules → setup/entry/protection/risk → Trade Plan Router → Paper Execution → Position/Exit → Trading Runner`, with Backtest/Research/Results/Factory/Auditor as the research and admission chain.

| Component | Path | Criticality | Status | Priority | Optimization need 0–5 | Evidence summary |
|---|---|---:|---|---:|---:|---|
| Core event engine, journal and timeframe store | `src/modular_core` | critical | OPTIMIZE NOW | P0 | 5 | No-op 10k run retained 10,002 events and one equity point per bar; cProfile at 5k attributed material time to timeframe push, equity recording, event emission and deep-freeze. Journal keeps projections plus eve |
| Backtest Engine application and recovery | `src/modular_backtest; tests/backtest_engine` | critical | OPTIMIZE NOW | P0 | 5 | Full registered BTC incident reached ~65.2 GB physical footprint and ~64.5 GB swap. Local base benchmark: 10k no-op 4.436 s, ~139.8 MiB RSS, 10,002 events; 5k checkpoint mode 6.631 s and ~20.48 MiB traced peak  |
| Registered dataset → Backtest bridge and loader | `src/modular_backtest/registered_data.py; src/modular_backtest/data.py; integration bridge tests` | critical | OPTIMIZE SOON | P1 | 4 | Static path materializes validated candles into a list and HistoricalDataFeed converts the iterable to a tuple before Core validation/replay; external registration helpers also use whole-file read_bytes and Arr |
| Historical Market Data Ingestion pipeline | `modules/market_data_ingestion/src/market_data_ingestion` | critical | OPTIMIZE SOON | P2 | 4 | Real BTCUSDT 5m evidence: 721,597 rows, 83 partitions, 1,101.233 s (~655 rows/s), sampled RSS ~297 MiB. Validation 444.89 s, source decode/spool 226.50 s, publish 210.96 s, checksum reread 218.56 s. Memory is b |
| Dataset Registry, LocalStorage and lineage | `modules/market_data_ingestion/src/market_data_ingestion/registry.py; storage.py; dataset contracts` | critical | MONITOR | P2 | 2 | Persistent smoke passed for 10 datasets, 12,968,018 rows and 3,865 partitions. Static audit found full-file read_bytes/hash comparisons in some external registration paths, but no demonstrated registry bottlene |
| Market Data CLI, API, jobs and Web Console | `modules/market_data_ingestion/src/market_data_ingestion/{cli,api,jobs}; interfaces; web console` | high | MONITOR | P2 | 2 | Control-plane, browser E2E, durable update/backfill/repair and security workflows are green. Large DatasetOperations/IngestionService/BinanceAdapter/LocalStorage classes increase change risk, but no measured us |
| Research Runner and parameter optimization | `research/experiment_runner` | high | MONITOR | P2 | 2 | Extensive deterministic scheduler, lease fencing, restart and package tests are green. Thread/process execution exists, but no exact integrated multi-process serialization/RSS benchmark was found in the audited |
| Results Aggregator and experiment registry | `modules/results_aggregator` | high | MONITOR | P2 | 2 | Fast CI and immutable package validation are green; existing issues document prior producer/consumer version skew, now integrated. No current throughput/RSS bottleneck was measured. |
| Strategy Factory | `research/strategy_factory` | high | NO OPTIMIZATION NEEDED | P3 | 0 | Measured evidence includes 10k canonical hashes in 6.64 s, 500 artifact cycles in 1.45 s, 2k idempotency cycles in 0.33 s and traced peak near 215 KiB; CI/hardening/soak are green. |
| Independent Strategy Audit & Validation | `research/strategy_audit_validation` | critical | MONITOR | P2 | 2 | Package gates are green; one package job took 200 s and another 125 s on the exact base, but duration includes environment/build work and does not prove runtime algorithm bottleneck. |
| Paper Execution | `modules/paper_execution` | critical | MONITOR | P2 | 2 | Python 3.12/3.13 CI, replay, reconciliation and fail-closed tests are green. No production-like order-volume latency/RSS SLO was found. |
| Trading Runner | `applications/trading_runner` | critical | MONITOR | P2 | 2 | Lifecycle/recovery/supervision gates pass; paper-only boundary is explicit. No long multi-symbol staging latency and memory profile was available. |
| Signal Bot | `src/signal_bot` | medium | MONITOR | P3 | 2 | Local smoke suite passed in ~5.25 s with peak RSS ~199 MiB; exact base CI is green. No high fan-out/chat benchmark was found. |
| Trade Plan Router | `modules/trade_plan_router` | critical | MONITOR | P2 | 2 | 349-test final evidence, fuzz/stress/replay are documented; large route methods (up to ~484 LOC/62 branch nodes) are maintainability hotspots. No production latency/RSS SLO was found. |
| Entry Model | `modules/entry_model` | high | NO OPTIMIZATION NEEDED | P3 | 0 | 50k plans ~8.04 s (~6,220/s); 250k ~40.81 s; bounded state with ~17.1 MiB traced peak and ~67.1 MiB RSS in stress evidence. |
| Protection & Target | `modules/protection_target` | high | NO OPTIMIZATION NEEDED | P3 | 0 | Million-event/bounded-state evidence and Python 3.12/3.13 gates are present; exact-base CI succeeded. |
| Risk & Position Sizing | `modules/risk_sizing; src/strategy_modules/risk_sizing.py` | critical | NO OPTIMIZATION NEEDED | P3 | 0 | Pure numeric logic, extensive precision/boundary/adversarial tests and no measured bottleneck; only one small duplicated helper group was detected. |
| Position Scaling & Adjustment | `modules/position_scaling` | high | NO OPTIMIZATION NEEDED | P3 | 0 | Existing heavy evidence reports bounded state and high throughput; exact base integration gates are green. |
| Position & Exit Management | `modules/position_exit_management` | critical | NO OPTIMIZATION NEEDED | P3 | 0 | Evidence ranges from ~13.8k no-op events/s to ~50k+/s at larger active workloads with bounded state; CI and recovery gates are green. |
| Canonical Setup Adapter | `modules/setup_adapter` | high | NO OPTIMIZATION NEEDED | P3 | 0 | Small adapter surface (~917 source LOC), deterministic contracts and green integrated acceptance; no bottleneck evidence. |
| Session Range | `modules/session_range` | medium | NO OPTIMIZATION NEEDED | P3 | 0 | ~42–45k events/s through 1,000,000-event stress; bounded checkpoint near 1.2 KiB. |
| Market Regime | `modules/market_regime` | medium | NO OPTIMIZATION NEEDED | P3 | 0 | ~11,061 calls/s in 20k×2 evidence, traced peak ~62.9 KiB and checkpoint 3,438 bytes. |
| Fair Value Gap plugin | `plugins/fvg` | high | NO OPTIMIZATION NEEDED | P2 | 0 | ~11k calls/s through 250k workload with ~7.7 KiB rolling traced state and deterministic restart. restore_state is a complexity hotspot (~401 LOC/132 branches), but performance is adequate. |
| Breakout–Return | `src/strategy_modules/breakout_return.py` | high | NO OPTIMIZATION NEEDED | P2 | 0 | Quality/hardening gates are green; no measured runtime bottleneck. Module class is large (~1,390 LOC/51 methods), making maintainability—not speed—the concern. |
| Wave Analysis | `modules/wave_analysis` | medium | NO OPTIMIZATION NEEDED | P3 | 0 | 100k bars ~24.76 s (~4,039/s); 1m chunked median ~4,025/s; bounded state ~316 KiB. Longest workflow wall time was 901 s, but slowest actual job 337 s includes full quality/package gates. |
| Liquidity Sweep | `src/strategy_modules/liquidity_sweep.py` | high | NO OPTIMIZATION NEEDED | P2 | 0 | Runtime gates and deep hardening are green; CI quality jobs took 197/190 s and workflow wall 830 s, pointing primarily to CI setup duplication rather than algorithm evidence. |
| Fibonacci plugin | `plugins/fibonacci` | medium | NO OPTIMIZATION NEEDED | P3 | 0 | Dedicated module CI succeeded on exact base; no bottleneck evidence was found. |
| CI/CD and GitHub Actions | `.github/workflows; .github/actions; scripts/runner_preflight.sh` | high | OPTIMIZE SOON | P1 | 4 | Base has 59 workflows; exact SHA produced 50 runs/84 jobs, 5,213 job-seconds (~1.45 h), 12.16 MB artifacts; only ~22 s ran hosted. 40 jobs do not invoke the mandated reusable Python preflight, many repeat insta |
| Test architecture and fixtures | `tests; component test trees` | high | OPTIMIZE SOON | P2 | 4 | 549 test files and strong component coverage. Naive repo-wide pytest collection fails on duplicate module basenames and optional dependencies; CI works by isolated subprocess suites. |
| Dependencies and packaging | `21 pyproject.toml files; packaging workflows` | high | MONITOR | P2 | 2 | 21 separately packaged projects, broad minimum dependency ranges and no repository lockfile were found. Offline editable install could not satisfy isolated setuptools build requirements in the audit container;  |
| Security, secret handling and supply chain | `repository-wide; workflows` | critical | MONITOR | P1 | 2 | Static AST scan found no eval/exec/os.system, unsafe pickle/yaml, shell=True or obvious literal secrets in production; release readiness secret-pattern checks passed. Mutable Action tags remain the main identif |
| Documentation, governance and release process | `README.md; docs; RELEASE_CHECKLIST.md; integration ledgers` | high | MONITOR | P3 | 2 | Full-RC ledger, release-readiness evidence and paper-only governance are strong. Numerous historical branches/issues increase navigation cost, but no runtime bottleneck. |

Detailed inputs, outputs and contracts:

- **Core/Backtest:** consumes ordered timezone-aware completed candles, module manifests and engine configuration; emits events, orders, fills, trades, equity, manifests and deterministic artifacts. Public interfaces include `BacktestEngine`, module APIs, event stores, journal, timeframe views and CLI.
- **Market Data:** consumes exchange archives/API responses and configuration; emits immutable raw lineage, normalized partitions, manifests, registered dataset descriptors and repair/update job evidence. Public interfaces include CLI/API/job services, `LocalStorage`, registry and exchange adapters.
- **Research chain:** consumes immutable datasets/configurations and emits experiment plans, engine evidence, registered result packages, candidates and signed paper-admission decisions.
- **Execution chain:** consumes canonical trade plans and paper venue evidence; emits orders/fills/positions/reconciliation/checkpoints and optional Signal Bot notifications.
- **Analytical modules:** consume completed point-in-time market views and emit immutable features/setups/decisions plus bounded checkpoint state.

All critical components have unit/contract/integration gates. Dedicated benchmark evidence is strongest for Core/Backtest, Market Data, Session Range, Market Regime, FVG, Entry, Wave, Position/Exit, Position Scaling and Strategy Factory. Operational scale evidence is incomplete for Results Aggregator, Research Runner process serialization, Paper Execution and Trading Runner staging loads; these are classified `MONITOR`, not assumed defective.

## 4. Methodology

1. Recovered live repository, branch, PR, Issue and Actions state through GitHub.
2. Exported an exact tracked-source snapshot through a temporary read-only workflow, verified SHA-256 and removed the workflow before final publication.
3. Parsed all Python sources with `ast`: **561 production files, 4,997 functions, 1,267 classes, zero syntax errors**.
4. Counted packages, tests, workflows, jobs, dependency declarations, Action references and preflight usage.
5. Ran local compile, import, focused test and Backtest scale/profile checks in Python 3.13.
6. Reused repository-owned historical benchmark artifacts only when they were bound to explicit workloads.
7. Collected exact-base workflow, job and artifact metadata through the GitHub Actions API.
8. Reviewed correctness boundaries before recommending speed changes.
9. Classified findings only when backed by measurement, static path evidence, CI behavior or a concrete architecture risk.

No value was invented. Unavailable measurements are `null` with reasons in `PROJECT_OPTIMIZATION_BASELINE.json`.

## 5. Baseline

### Local source and smoke baseline

| Check | Result | Wall time | Peak RSS |
|---|---:|---:|---:|
| Compile all source/tests | PASS | 0.72 s | 112,808 KiB |
| Backtest focused tests, registered-data test excluded | PASS | 3.29 s | 153,420 KiB |
| Signal Bot focused tests, Hypothesis tests excluded | PASS | 3.25 s | 197,704 KiB |
| Key package imports | PASS | mostly tens to ~150 ms above process startup | not a bottleneck |

The two exclusions were environmental: `pyarrow` and `hypothesis` were unavailable offline. Exact-base GitHub workflows covering those dependencies passed.

### Backtest no-op scale

| Bars | Mode | Wall time | Throughput | Traced peak | RSS | Events |
|---:|---|---:|---:|---:|---:|---:|
| 1,000 | single | 0.431 s | 2,322 bars/s | 1.27 MiB | 114.3 MiB | 1,002 |
| 5,000 | single | 2.146 s | 2,330 bars/s | 5.85 MiB | 125.6 MiB | 5,002 |
| 10,000 | single | 4.436 s | 2,254 bars/s | 10.54 MiB | 139.8 MiB | 10,002 |
| 5,000 | checkpoint/restart | 6.631 s | 754 bars/s | 20.48 MiB | 153.3 MiB | replay verified |

Even a no-op strategy retains approximately one event and one equity point per bar. At 5,000 bars, checkpoint mode was roughly **3.09× slower** and used roughly **3.5× the traced peak** of single-pass execution.

### Dataset baseline

`download_summary.csv` describes **10 symbols × 5 timeframes = 50 series** and **12,968,018 rows**, including **6,691,902 base 5m rows**. Reported base-series gaps are zero. The persistent registry smoke verified 10 datasets and 3,865 partitions without uploading raw market bytes.

## 6. Results by component

### Immediate action

- **Core event/journal/timeframe retention:** correctness is strong, but memory scalability is not. Observability must become an explicit bounded policy rather than an incidental whole-run object graph.
- **Backtest Engine:** full-history research is unsafe on bounded hardware until PR #287 proves scale and trade equivalence.

### Near-term optimization

- **Registered dataset bridge:** remove final all-candle copies through streaming validation and manifest accumulation.
- **CI/CD:** consolidate repeated setup, pin Actions, normalize preflight and eliminate overlapping same-SHA work.
- **Market Data pipeline:** optimize measured validation/checksum phases after line/system profiling.
- **Test architecture:** make root collection reproducible without depending on hidden isolated invocation knowledge.

### Monitor with new metrics

Research Runner, Results Aggregator, Dataset Registry, Market Data control plane, Strategy Auditor, Paper Execution, Trading Runner, Signal Bot and Trade Plan Router need capacity benchmarks or staging telemetry before architectural changes.

### No current optimization need

Strategy Factory and the bounded analytical/execution modules have adequate workload evidence. Their next changes should be correctness- or feature-driven, not performance rewrites.

## 7. Confirmed bottlenecks

1. **Multiplicative Backtest retention.** Multiple full-history containers and per-bar projections grow together.
2. **Checkpoint amplification.** Deliberate continuous/restarted execution multiplies work and retained evidence.
3. **Per-bar observability cost.** At 5k profile scale, `_append_equity`, `record_equity`, event emission and deep-freeze are material hot paths.
4. **Timeframe aggregation cost.** `TimeframeStore.push` and bucket alignment are significant in the no-op profile.
5. **Market Data validation/checksum I/O.** Validation plus checksum reread consumed about 60% of the measured 721k-row ingestion.
6. **CI setup duplication.** Component workflows repeat installation, compile, lint, typing and packaging across Python matrices.

No evidence supports a project-wide pandas→Polars/Numba rewrite. The primary Backtest path is object/event driven, and the Market Data pipeline is already Arrow/partition oriented in important areas. Technology migration must follow a focused benchmark and exact-equivalence plan.

## 8. Components that should not be optimized now

- Session Range: ~42–45k events/s through one million events; bounded checkpoint.
- Market Regime: ~11k calls/s; tiny traced/checkpoint state.
- FVG: ~11k calls/s through 250k with bounded rolling state.
- Entry Model: ~6.2k plans/s through 250k and bounded memory.
- Wave Analysis: ~4k bars/s through one million chunked bars; bounded state.
- Protection/Target, Position Scaling and Position/Exit: large bounded-state/restart evidence.
- Strategy Factory: fast hashing/artifact/idempotency evidence and very small traced peak.
- Setup Adapter, Risk Sizing and Fibonacci: no measured bottleneck and strong correctness gates.

Some of these have maintainability hotspots, but refactoring is not the same as performance optimization and must use golden/replay tests.

## 9. Correctness findings

No new confirmed look-ahead, unfinished-candle, timezone, gap, duplicate, fill-priority, funding, sizing or nondeterminism defect was found on the audited exact base.

Positive evidence includes:

- completed-candle and N+1 execution semantics;
- timezone-aware UTC boundaries;
- completed HTF aggregation;
- fail-closed gap/duplicate/out-of-order handling;
- conservative OHLC ambiguity policies;
- immutable dataset identity and lineage;
- deterministic replay/checkpoints;
- paper-only execution and independent admission authority;
- full exact-base CI and release-readiness gates.

Optimization risks:

- Streaming input must preserve sorted validation, duplicate conflict rejection, gap reset and manifest identity.
- Bounded journal/equity must not silently downgrade canonical audit evidence.
- Any vectorization must preserve Decimal/rounding, order precedence and event order.
- Checkpoint fast paths must remain explicitly non-authoritative unless exact replay evidence is retained.

## 10. Architecture findings

The architecture has strong component boundaries and paper-only safety, but four maintainability concentrations deserve attention:

- `IngestionService.execute`: ~1,017 LOC and high branch density.
- Market Data operations/validation repair paths: several 300–500+ LOC functions.
- Trade Plan Router standalone risk-reduction route: ~484 LOC/high branch density.
- FVG `restore_state`: ~401 LOC and very high branch density.

Large classes include `DatasetOperations`, `IngestionService`, `BinanceAdapter`, `LocalStorage`, `BreakoutReturnModule` and `BrokerSimulator`.

Exact clone detection found only two small groups, so broad deduplication is not justified. Refactoring should target responsibility and testability, not arbitrary LOC reduction.

## 11. Memory analysis

### Confirmed

- Backtest input and replay create multiple all-history representations.
- Journal stores domain projections plus generic events and indexes.
- JSONL storage is not memory-bounded because it owns an in-memory mirror.
- Equity is retained and serialized in multiple forms.
- Default HTF histories can grow without a configured maximum.
- Full BTC incident demonstrates catastrophic aggregate impact.

### Bounded areas

- Market Data ingestion holds monthly/batch-oriented materializations and stayed near ~297 MiB sampled RSS for 721k rows.
- Analytical modules mostly expose bounded rolling/checkpoint state.
- Strategy Factory benchmark memory is small.

### Required next measurement

For PR #287 record RSS, macOS physical footprint, swap, GC time, Python heap, Arrow/native allocations, retained event/equity counts and artifact bytes at every scale step.

## 12. I/O analysis

Market Data is the main measured I/O workload. The 721k-row run spent:

- 226.50 s decoding/spooling source;
- 444.89 s validating;
- 210.96 s publishing;
- 218.56 s rereading for checksums.

The full 6.69m-row base corpus would take roughly **2.8 hours** at the same observed rate; this is an inference, not a measured full-corpus duration.

Backtest export also duplicates serialization work: full journal JSON, event JSONL and equity CSV are produced, then files are reread to hash. Bounded modes should avoid retaining all representations simultaneously, but integrity hashes must remain.

Registry helpers that use `read_bytes()` and `to_pylist()` should be replaced by file/record-batch streaming only after equivalence tests.

## 13. Backtest Engine audit

The event loop is deterministic and well tested; the defect is resource policy.

The 5k cProfile/tracemalloc run produced about 4.19 million calls in 5.854 s. Major cumulative areas were:

- `_process_bar`: 5.174 s;
- timeframe push: 1.802 s;
- equity append: 1.383 s;
- journal equity record: 1.261 s;
- event emission: 0.879 s;
- deep-freeze: ~0.5 s.

Recommended sequence:

1. Prove bounded journal/equity/skip policies.
2. Bound timeframe/base histories.
3. Keep checkpoint-disabled research single-pass.
4. Add hard memory/event guards.
5. Prove exact trade equivalence.
6. Stream registered data into the engine.
7. Only then optimize bucket alignment or object allocation if profiles still justify it.

Do not disable GC as a “fix”; it cannot remove the retained graph.

## 14. Historical Market Data audit

Strengths:

- resume/retry/backoff, durable update/backfill/repair, validation, atomic publication and lineage are extensively tested;
- persistent registration passed on the supplied 10-dataset corpus;
- raw/normalized evidence is not uploaded to GitHub;
- memory is bounded by partition/batch behavior.

Optimization opportunities:

- validation/checksum phase throughput;
- whole-file reads in external registration utilities;
- large orchestration methods;
- explicit per-phase I/O counters and cache hit/miss metrics;
- registry latency/DB growth benchmarks.

No recommendation is made to change Parquet/Arrow format, partition identity or checksum rules without a separate migration design.

## 15. Tests

The test estate is a strength: **549 test files**, extensive negative paths, deterministic replay, property-based tests, package smoke, security and multi-version CI.

Problems:

- duplicate test basenames make naive root collection fail under the default import mode;
- component extras such as PyArrow/Hypothesis are not obvious to an offline local run;
- setup and package installation are repeated across many workflows;
- root success depends on isolated subprocess conventions that should be formalized.

Recommended suites: fast smoke, unit/contract, integration, regression/golden, security/package, and manual/nightly heavy/backtest. Do not delete slow correctness tests.

## 16. GitHub Actions and CI

Exact-base metadata:

- 50 runs across 45 workflow names;
- 84 jobs;
- 5,213 job-seconds (~1.45 h);
- 63 artifacts totaling ~12.16 MB;
- ~22 hosted-runner seconds, with the rest self-hosted;
- 43 successful runs, 6 intentional skips and 1 superseded runner-preflight failure.

### Longest workflow wall times

| Rank | Workflow | Wall seconds | Jobs | Artifacts | Conclusion |
|---:|---|---:|---:|---:|---|
| 1 | wave-analysis-ci | 901 | 2 | 0.64 MiB | success |
| 2 | liquidity-sweep-hardening-ci | 830 | 2 | 0.97 MiB | success |
| 3 | breakout-return-hardening-ci | 738 | 2 | 0.94 MiB | success |
| 4 | market-data-ingestion-ci | 725 | 4 | 1.91 MiB | success |
| 5 | results-aggregator-fast | 686 | 2 | 0.38 MiB | success |
| 6 | paper-execution-ci | 641 | 2 | 0.37 MiB | success |
| 7 | local-pretest-tooling-ci | 638 | 2 | 0.00 MiB | success |
| 8 | market-data-external-corpus-ci | 627 | 2 | 0.00 MiB | success |
| 9 | backtest-engine-fast | 622 | 2 | 0.02 MiB | success |
| 10 | market-regime-fast-ci | 620 | 2 | 0.05 MiB | success |
| 11 | market-data-export-artifacts-ci | 614 | 2 | 0.90 MiB | success |
| 12 | protection-target-ci | 581 | 2 | 0.23 MiB | success |
| 13 | market-data-cli-parity-audit-ci | 578 | 2 | 0.00 MiB | success |
| 14 | fibonacci-module-ci | 574 | 2 | 0.12 MiB | success |
| 15 | core-ci | 551 | 2 | 0.59 MiB | success |

Workflow wall time includes queue/parallel behavior and must not be summed as compute consumption.

### Longest actual jobs

| Rank | Workflow | Job | Seconds | Runner |
|---:|---|---|---:|---|
| 1 | wave-analysis-ci | quality (3.13) | 337 | archived-runner-08 |
| 2 | strategy-audit-validation | package (3.12) | 200 | archived-runner-08 |
| 3 | liquidity-sweep-hardening-ci | quality (3.13) | 197 | archived-runner-06 |
| 4 | liquidity-sweep-hardening-ci | quality (3.12) | 190 | archived-runner-07 |
| 5 | historical-data-persistent-registration-smoke-v2 | register-and-smoke | 176 | archived-runner-02 |
| 6 | market-regime-fast-ci | Fast quality (Python 3.12) | 155 | archived-runner-07 |
| 7 | paper-execution-ci | Paper Execution / Python 3.12 | 140 | archived-runner-07 |
| 8 | protection-target-ci | verify (3.13) | 138 | archived-runner-01 |
| 9 | breakout-return-hardening-ci | quality (3.12) | 129 | archived-runner-08 |
| 10 | strategy-audit-validation | package (3.13) | 125 | archived-runner-08 |
| 11 | market-data-web-console-e2e | browser-e2e | 124 | archived-runner-03 |
| 12 | strategy-factory-soak | Strategy Factory extended restart soak / Python 3.13 | 122 | archived-runner-04 |
| 13 | breakout-return-hardening-ci | quality (3.13) | 111 | archived-runner-04 |
| 14 | v0.6-executable-acceptance-ci | v0.6 executable acceptance / Python 3.12 | 88 | archived-runner-03 |
| 15 | v0.6-executable-acceptance-ci | v0.6 executable acceptance / Python 3.13 | 88 | archived-runner-05 |

Static workflow findings:

- 59 base workflows and 71 declared jobs;
- roughly 40 jobs do not invoke the mandated reusable Python preflight; non-Python/trigger jobs need explicit classification;
- 38 jobs use matrices;
- many workflows repeat `uv pip install`, compile, lint, typing and package steps;
- many third-party Actions are referenced by mutable major tags;
- several workflows lack concurrency cancellation;
- current runner logs warn that Node 20 Actions are being forced to Node 24.

Target: reduce summed job time by at least 30% without removing gates, and eliminate duplicate same-SHA work.

## 17. Dependencies

There are 21 separately packaged Python projects. Root runtime dependencies are small, but dev/security/data extras are broad. No repository lock/constraints file was found.

The audit container could not perform a clean offline editable install because build isolation needed `setuptools>=68`, and the fallback environment lacked `setuptools.build_meta`. This does not contradict the exact-base green online CI, but demonstrates that local reproduction depends on network/build tooling state.

Recommended policy:

- keep library dependency ranges in package metadata;
- generate reviewed constraints for CI on Python 3.12 and 3.13;
- cache wheel/build environments;
- run online `pip-audit`, SBOM and clean-install gates on every constraints change;
- do not mass-upgrade packages in this audit PR.

## 18. Security and reliability

Static production scan found no calls to `eval`, `exec`, `os.system`, unsafe pickle/YAML loading, `subprocess(..., shell=True)` or obvious literal secrets. Release-readiness secret-pattern and artifact inventory checks passed.

Primary identified security improvement: pin third-party Actions to immutable SHAs. Also retain least-privilege workflow permissions, short artifact retention for sensitive evidence and explicit source/evidence inventories.

No secret values were found or disclosed.

## 19. Prioritized plan

### P0

1. Complete PR #287 bounded-memory Backtest remediation.
2. Define Core bounded-observability contract and exact canonical/fast mode semantics.

### P1

1. Stream registered datasets into Backtest.
2. Consolidate CI setup and component quality gates.
3. Pin all third-party Actions.
4. Normalize mandatory preflight and harden runner disk reporting.
5. Preserve security/SBOM gates while changing workflow structure.

### P2

1. Optimize Market Data validation/checksum phases.
2. Decompose high-complexity orchestration behind unchanged contracts.
3. Add registry, Research Runner, Results Aggregator, Paper/Trading operational benchmarks.
4. Fix root test collection and dependency reproducibility.
5. Add Trade Plan Router and FVG golden-refactor workstreams.
6. Publish phase-specific CI timing.

### P3

1. Consolidate tiny duplicate helpers only during adjacent work.
2. Archive stale workstreams after governance approval.
3. Keep existing bounded-module benchmarks as regression gates.

Full issue-ready descriptions are in `PROJECT_OPTIMIZATION_BACKLOG.md`.

## 20. Recommended PR sequence

1. **PR #287 completion:** Backtest/Core P0, no other refactors.
2. **Streaming registered input PR:** loader/validation/manifest only.
3. **CI evidence PR:** phase timings and duplicate-trigger map, no gate changes.
4. **CI consolidation/security PR:** reusable workflows, SHA pins, preflight normalization.
5. **Market Data performance PR:** validation/checksum improvements with fixed-corpus equivalence.
6. **Test collection/dependency reproducibility PR.**
7. **Operational benchmark PRs** for Research, Aggregator, Paper and Trading.
8. **Maintainability refactors** only after golden baselines.

Each PR should be independently reversible and must not combine semantic and performance changes.

## 21. Optimization risks

- Faster but semantically different backtests are unacceptable.
- Sampling/aggregation can weaken evidence if mode metadata is ambiguous.
- Streaming can hide late gap/duplicate/hash failures unless validation is incremental and fail-closed.
- CI consolidation can accidentally stop running a gate.
- Dependency constraints can over-pin library consumers.
- Large-method refactors can alter reason-code or state-restoration order.
- Parallelism can violate scheduler fencing or increase serialization memory.
- Historical benchmarks may not represent a full multi-symbol integrated run.

Mitigation: exact golden artifacts, trade/event equivalence, scale ladders, negative paths, crash/restart tests and mode/version declarations.

## 22. Final conclusion

The project already has strong correctness, determinism, safety and component-level testing. Optimization should be narrow:

- **Fix Core/Backtest retention first.**
- **Then stream registered data.**
- **Reduce CI duplication and strengthen supply-chain reproducibility.**
- **Improve Market Data throughput without weakening integrity.**
- **Measure operational components before changing them.**
- **Do not rewrite bounded analytical modules that already meet their workloads.**

This audit PR changes documentation only. It does not change trading logic, dataset formats, public APIs, dependencies, workflow policy or production behavior.
