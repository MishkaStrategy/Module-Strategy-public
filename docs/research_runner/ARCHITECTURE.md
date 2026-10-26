# Research Runner & Parameter Optimization — Architecture

## Scope boundary

The Research Runner is a strategy-independent orchestration layer. It plans experiments,
controls phased candidate promotion, persists durable execution state, and delegates one
backtest run at a time to the authoritative `modular_backtest` application contract.

It must never own:

- signal discovery or strategy rules;
- entry, stop, target, or sizing calculations;
- candle/event processing;
- order, fill, or position simulation;
- equity accounting;
- Profit Factor, drawdown, Sharpe, expectancy, or other trading-metric calculation.

The adapter accepts a typed `BacktestJob`, builds an official Backtest Engine configuration
through an injected factory, calls `modular_backtest.run_backtest`, and stores only immutable
artifact references and externally produced metrics.

## End-to-end flow

```text
ResearchSpecification
  -> canonical specification and lineage fingerprints
  -> deterministic search-space materialization
  -> chronological / walk-forward split templates
  -> train-only initial jobs or optimization_000 for staged search
  -> durable SQLite repository and local scheduler
  -> authoritative Modular Backtest Engine
  -> immutable result reference / Results Aggregator envelope
  -> externally supplied ranking metrics
  -> atomic frozen-candidate promotion
  -> validation -> test or OOS -> optional final_untouched
```

## Determinism

Canonical serialization preserves value types and normalizes aware timestamps to UTC.
Equivalent mapping and parameter declaration order produce the same hashes.

The semantic seed hierarchy derives seeds from the root seed and stable labels. It never
uses worker index, queue order, or completion order. Random search materializes the complete
sampled candidate set before execution, so changing parallelism cannot change the selected
configurations.

## Data lineage

Every dataset records source, market type, instrument, timeframe, UTC period, row count,
format, size, SHA-256, preprocessing/aggregation/metadata versions, funding hash when
available, gaps, duplicates, and quality status.

An invalid dataset cannot enter a research specification. A changed dataset hash changes the
specification fingerprint and experiment identity.

## Split and leakage integrity

The implementation supports:

- fixed and percentage chronological train/validation/test splits;
- optional final untouched test;
- rolling and anchored expanding-train walk-forward windows;
- purge and embargo intervals;
- train-only initial registration;
- staged successive-halving over increasing prefixes of the frozen train range;
- strictly decreasing, immutable promotion limits between optimization stages;
- immutable candidate sets for every later phase;
- prerequisite-success checks before promotion;
- one-time final grants consumed atomically with final-job registration.

No random time-series shuffling exists. Validation, test, OOS, and final jobs do not exist in
the initial run set. Staged search cannot be combined with walk-forward in schema version 1;
the unsupported composition fails during specification validation rather than silently
inventing nested optimization semantics.

## Durable execution

`SqliteExperimentRepository` provides transactional registration, claims, leases, heartbeat,
append-only transition history, retry/backoff state, stale-claim recovery, cancellation,
idempotency, and artifact verification on resume.

`LocalResearchScheduler` supports bounded thread workers and spawn-based process workers.
Process mode requires pickle-safe programmatic callables and does not import executable code
from YAML or JSON specifications.

## Results boundary

Research Runner does not recalculate financial metrics. Ranking and promotion consume
externally supplied metrics under explicit objective and hard-constraint policies. Bootstrap,
Monte Carlo path permutation, and signal-skip utilities produce deterministic plans only.
Versioned result envelopes provide immutable references for Results Aggregator ingestion.
For completed Backtest Engine outputs, the registry boundary first verifies the authoritative
`artifact-manifest.json`, exact file set, and every declared SHA-256. It then copies the full
Core, provenance, validation, restart, and optional replay evidence into a separate
content-addressed package directory and writes the Aggregator's exact
`artifact-package.json` schema there. The source Engine output is never edited, and no trading
artifact or financial metric is recalculated.

## Validated release profile

The automated candidate profile covers:

- compile, Ruff, strict MyPy, branch-aware coverage, package and CLI gates;
- Python 3.12 and 3.13 component matrices;
- authoritative Backtest Engine and Results Aggregator contract integration;
- full-repository integration and clean wheel installation;
- optimized-runtime execution under `python -O`;
- reproducible wheel and normalized source-distribution builds;
- clean installation and metadata validation from the source distribution;
- Bandit, scoped secret detection, runtime dependency audit, CycloneDX SBOM and checksummed
  security evidence;
- deterministic high-volume planning and durable SQLite registration/recovery checks.

These checks establish an integration-ready automated candidate. They do not replace the
final human architecture, methodology, security and release review.

## Intentional limitations and remaining non-automated gates

Version `0.3.0` intentionally remains a local orchestration layer:

- remote multi-machine worker transport is not implemented;
- staged successive-halving plus walk-forward remains rejected until a separately audited
  nested-window methodology exists;
- strategy logic, financial metrics and exchange/network access remain outside this module;
- release approval should still include representative long-duration workload observation and
  an architecture/methodology review by a human reviewer.

Distributed execution remains behind an explicit future transport contract rather than being
implicitly coupled to GitHub Actions or exchange/network access.
