# Registered Streaming → Bounded Backtest Integration

## Status

This document describes the disposable integration branch
`integration/registered-streaming-on-backtest-memory` and Draft PR #294.

The branch combines, without modifying their source branches:

- Draft PR #287: bounded-memory Backtest/Core execution;
- Draft PR #292: bounded registered Parquet reader and replayable Candle source.

The canonical integration branch and `main` are unchanged. PR #294 is an integration
validation PR and must remain Draft until the private data-bearing gates are complete.

## Supported execution boundary

The integration supports the bounded single-pass runner and a deterministic replay
verification path for registered streaming data.

The legacy replay implementation remains unchanged for file sources and registered
sources using `legacy_materialized`. A registered source using `streaming` can use the
bounded recovery harness described below without materializing the complete Candle
history.

## Typed loader configuration

The registered loader is selected through the typed `data` configuration. The default
remains fully backward compatible:

```yaml
data:
  symbol: BTCUSDT
  storage_root: ../registered-data
  dataset_id: btcusdt-5m
  dataset_version: 1
  registered_mode: legacy_materialized
```

The bounded reader is an explicit opt-in:

```yaml
data:
  symbol: BTCUSDT
  storage_root: ../registered-data
  dataset_id: btcusdt-5m
  dataset_version: 1
  registered_mode: streaming
  stream_batch_size: 8192
```

Accepted modes are exactly:

- `legacy_materialized`;
- `streaming`.

`stream_batch_size` must be a positive integer. It is rejected in legacy mode and
defaults to `8192` when streaming mode is selected without an explicit value. File
sources reject both non-default registered mode and registered batch settings.

Embedded callers can select the same contract explicitly:

```python
prepared = prepare_candle_source(
    data_config,
    registered_mode="streaming",
    stream_batch_size=8192,
)
```

Process-scoped environment overrides remain available for operational rollback and
controlled experiments:

```bash
export MODULAR_BACKTEST_REGISTERED_DATASET_MODE=streaming
export MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE=8192
python -m modular_backtest run --config configs/backtest.example.yaml
```

Selection precedence is deterministic:

1. explicit `prepare_candle_source` arguments;
2. environment overrides;
3. typed YAML / `DataSourceConfig`;
4. the streaming default batch size of `8192` when required.

## Deterministic two-pass behavior

The selected mode and batch size are resolved once during source preparation and
stored on `PreparedCandleSource`. The execution pass reuses those stored values rather
than rereading YAML or environment state.

This prevents configuration changes between validation and simulation from switching
the reader contract after the canonical Candle-sequence hash was produced.

For registered sources the validation payload records:

- `source_mode`;
- `stream_batch_size`;
- registered dataset ID and version;
- dataset hash;
- partition count;
- selected row range and validation counters.

Because the runner already serializes `ValidationSummary`, this evidence appears in
both `data-validation.json` and the `validation` section of `application-run.json`
without a separate publication path.

## Memory ownership

In streaming mode each traversal owns at most:

- one open Parquet reader;
- one Arrow `RecordBatch`;
- one bounded tuple of normalized records;
- current validation/hash state;
- bounded Backtest/Core runtime state from PR #287.

The adapter does not create a complete Candle list or tuple. Source preparation
intentionally traverses the registered source twice:

1. incremental validation and canonical Candle-sequence hashing;
2. one trading simulation pass from a fresh iterator.

Two source traversals are not two trading simulations and do not require retaining the
full history.

## Integrity boundary

`legacy_materialized` keeps the existing deep preflight, including registered
partition and raw-lineage verification before the first Candle.

`streaming` performs lightweight registry/manifest preflight and validates Parquet
partition bytes on demand. It checks the partition checksum before opening and again
after complete exhaustion. Cancellation closes the active reader immediately.

The selected mode is therefore part of authoritative run evidence. A result produced
with one mode must not be relabeled as the other.

## Registered streaming replay recovery

`run_with_registered_streaming_replay_restart` provides bounded deterministic prefix
replay with physical source-cursor continuation. It is intentionally **not** described
as direct serialized runtime restoration.

At the configured committed-bar boundary it records canonical evidence containing:

- the existing `KernelCheckpoint` identity and module-state hashes;
- the number of successfully committed bars;
- `RegisteredSourcePosition`, which points to the next physical row;
- runtime, event-stream and equity SHA-256 digests;
- seed and source commit evidence.

The restarted execution then:

1. opens the registered source from its configured beginning;
2. deterministically replays the prefix to reconstruct bounded runtime state;
3. verifies the complete checkpoint boundary and physical cursor;
4. closes the prefix Parquet reader;
5. opens a new reader at the persisted next-row cursor;
6. continues through the remaining data;
7. requires the complete continuous and restarted result digests to match.

This preserves bounded memory and proves that continuation does not re-emit the last
committed Candle. The continuous run opens the source once. The restarted verification
run opens it twice: once for prefix reconstruction and once at the persisted cursor.

The recovery contracts cover:

- a checkpoint that crosses a monthly partition boundary;
- exact partition index, row offset and last committed UTC timestamp;
- streaming batch sizes 1, 3 and 64;
- selected `start` / exclusive `end` ranges;
- non-empty orders, fills and trades;
- invalid boundaries and legacy-source rejection;
- identical full result digest after cursor continuation.

The recovery tree at commit
`70b219b97448e618a4674574c96217754e73d1e1` was validated by GitHub Actions run
`30694837469`. Python 3.12 and 3.13 both passed installation, `pip check`, compile,
Ruff lint and formatting, strict MyPy, and the complete configured streaming plus
recovery contract suite. The temporary diagnostic workflow was removed after the
successful run.

## Golden compatibility coverage

The integration tests require:

- legacy remains the default;
- typed YAML parsing and fail-closed validation;
- API → environment → YAML precedence;
- mode and batch size remain frozen between validation and simulation;
- exact Candle-sequence equality between legacy and streaming;
- identical canonical data hash and selected boundaries;
- batch-size-independent bounded-engine results;
- a non-trivial order lifecycle with non-empty fills and trades;
- identical run ID, metrics, order transitions, fills, trades, equity and errors;
- runner publication of loader mode and batch size in both result JSON artifacts;
- fail-closed invalid mode and batch handling.

The broader PR #292 tests continue to cover partition ordering, cursor restart,
exclusive range boundaries, partial failure, cancellation cleanup, duplicate/gap
handling and manifest/schema identity checks.

The typed loader integration tree was validated on GitHub-hosted Ubuntu with CPython
3.12 and 3.13. Each interpreter passed compile checks, Ruff lint and formatting,
strict MyPy, 92 focused contracts, and the 1,000-row benchmark smoke. The benchmark
required the legacy run and streaming runs at batch sizes 1 and 1,024 to produce the
same result hash while reporting one full dataset compatibility copy for legacy and
zero complete dataset copies for streaming.

## Checkpoint routing contract

The public checkpoint surface remains backward compatible:

```yaml
checkpoint:
  mode: replay
  restart_at_bar: 100000
```

Routing is determined by the already validated data-source contract:

- registered + `registered_mode: streaming` → bounded deterministic prefix replay and
  physical cursor continuation;
- file source or registered + `legacy_materialized` → unchanged legacy materialized
  replay.

No third checkpoint mode is introduced. `checkpoint.mode: disabled` continues to run a
single bounded simulation pass.

## Rollback

The preferred rollback is configuration-only:

```yaml
data:
  registered_mode: legacy_materialized
```

Remove `stream_batch_size` when returning to legacy mode. Also unset any process-level
overrides:

```bash
unset MODULAR_BACKTEST_REGISTERED_DATASET_MODE
unset MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE
```

No code revert is required. The temporary integration branch and PR #294 can also be
closed or deleted without changing PR #287, PR #292, the canonical integration branch
or `main`.

## Remaining gates

Before production merge:

1. Run the safe scale ladder on the private registered BTC corpus.
2. Execute the full 721,597-row BTCUSDT 5m run only after the preceding memory gates
   pass without swap or unbounded retention.

The private registry, manifest and Parquet corpus are not stored in GitHub and remain
required for the data-bearing scale and full-history evidence.
