# Research Runner & Parameter Optimization

Deterministic, strategy-independent research orchestration for the Modular Strategy
project.

Version `0.3.0` owns experiment specifications, deterministic identities and seeds,
search-space planning, true staged successive-halving, chronological and walk-forward
stages, durable scheduling,
resume/retry state, promotion gates, the one-time final untouched-test gate,
resampling plans, result envelopes, and reproducible reports.

It deliberately does **not** implement trading signals, bar simulation, orders, fills,
portfolio accounting, equity curves, or financial metrics. A run is delegated to the
authoritative `modular_backtest.run_backtest` contract. Metrics are supplied by the
Backtest Engine / Results Aggregator boundary and are only filtered or ranked according
to an explicit policy.

## Leakage-safe lifecycle

1. `prepare()` freezes provenance and registers only **train** jobs for ordinary searches,
   or only `optimization_000` jobs for staged search.
2. In staged search, externally supplied metrics freeze a bounded candidate set for each
   larger train prefix (`optimization_001`, `optimization_002`, ...). Stages cannot be
   skipped and candidate limits strictly decrease.
3. Externally supplied metrics from train or the final optimization stage are ranked;
   selected configuration hashes are atomically frozen for `validation`.
4. After those jobs finish, a second frozen promotion may activate `test` or walk-forward
   `oos` jobs.
5. The optional `final_untouched` range is absent from all prior job sets. It requires a
   one-time durable grant bound to the specification hash, split fingerprint, and exact
   candidate hashes.
6. Result envelopes contain references to engine and metric artifacts; they never
   recalculate metrics.
7. `LocalResultsAggregatorPackageSink` verifies the completed Backtest Engine
   `artifact-manifest.json`, copies every declared Core and Engine evidence file into a
   separate content-addressed package directory, and writes the exact Results Aggregator
   v1 `artifact-package.json` contract there. The original Engine output is never edited.
   Publication fails closed for dirty Git provenance, missing dependency-lock lineage,
   inventory drift, checksum mismatch, symlinks, missing files, or extra files.

## Main API

```python
from modular_research_runner import (
    AtomicJsonArtifactStore,
    GitProvenance,
    LocalResearchScheduler,
    LocalResultsAggregatorPackageSink,
    ModularBacktestEngineAdapter,
    MonteCarloPlanner,
    ResearchPlanBuilder,
    ResearchRunner,
    ResumePolicy,
    RetryPolicy,
    SqliteExperimentRepository,
    StagedSearchPolicy,
    StageRole,
)
```

Staged search uses explicit increasing `train_fractions` ending at `1.0` and strictly
decreasing `promotion_counts`. Schema version 1 deliberately rejects combining staged search
with walk-forward until a separately audited nested-window methodology exists.

`LocalResearchScheduler` supports bounded thread workers and spawn-based process workers.
Process mode requires programmatically supplied, pickle-safe top-level config/run
callables; callable paths are never loaded from YAML.

## CLI

```bash
modular-research-runner validate research.yaml
modular-research-runner plan research.yaml \
  --output-root ./research-output \
  --engine-version 0.1.0 \
  --metric-schema-version metrics-v1 \
  --module-api-version module-api-v1-draft
modular-research-runner status ./research-output/state.sqlite3 exp-...
```

Execution remains an API operation because the official Backtest Engine configuration
factory is application code and must not be dynamically imported from an untrusted
research file.

## Local verification

```bash
python -m compileall -q research/experiment_runner/src
python -m pytest -q research/experiment_runner/tests
coverage run --branch --source=modular_research_runner \
  -m pytest -q research/experiment_runner/tests
coverage report --fail-under=90
```
