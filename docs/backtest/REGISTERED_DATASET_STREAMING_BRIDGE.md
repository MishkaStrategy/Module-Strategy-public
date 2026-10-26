# Registered Dataset → Backtest Engine Streaming Bridge

## Status

This document records the isolated reader and adapter phase of the registered-dataset
streaming workstream.

- Audited integration branch: `integration/modular-strategy-full-rc`
- Audited integration SHA: `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- Historical registered-dataset bridge: merged PR #285
- Active Backtest/Core dependency: Draft PR #287
- Optimization branch: `optimization/registered-dataset-backtest-streaming`
- Bridge contract version: `registered-dataset-streaming-v1`

The canonical legacy loader is unchanged and remains the default. The bounded reader
is exposed only through `open_streaming_registered_candles`. This branch does not
modify `BacktestEngine.run`, `src/modular_backtest/runner.py`, Core retention,
checkpoint persistence, the registry or manifest schema, the Parquet schema, the
checksum algorithm, or ingestion writers.

## Audited materialized data flow

| Stage | File / function | Input | Output | Full-history copy | Ownership / lifetime |
|---|---|---|---|---:|---|
| Configuration | `src/modular_backtest/config.py:DataSourceConfig` | dataset ID, version, storage root | immutable config | 0 | run lifetime |
| Registry and manifest | `market_data_ingestion.dataset_reader.resolve_registered_kline_dataset` | registered identity | selection + manifest + partitions | 0 candles | selection lifetime |
| Deep integrity preflight | `market_data_ingestion.integrity.verify_registered_dataset(deep=True)` | manifest and registry descriptor | partition/raw-artifact verification report | 0 candles | hashes every partition and verifies raw lineage before first candle |
| Parquet decode | `market_data_ingestion.dataset_reader._partition_records` | `ParquetFile` | `RecordBatch.to_pylist()` | batch-sized list of row dictionaries | batch lifetime |
| Domain normalization | same function | dictionaries | `KlineRecord` objects | batch-local Python objects | until generator advances |
| Legacy Backtest adapter | `src/modular_backtest/registered_data.py:open_registered_candles` | `KlineRecord` iterator | one-shot `Candle` iterator | 0 full history | generator lifetime |
| Validation/materialization | `src/modular_backtest/data.py:_validated_stream` | candle iterator | `list[Candle]` | **1 full list** | complete run |
| Tuple compatibility | `src/modular_backtest/registered_data.py:load_registered_candles` | candle iterator | `tuple[Candle, ...]` | **1 full tuple** | caller lifetime |
| Replay session | `src/modular_backtest/recovery.py:_ReplaySession.__init__` | `Sequence[Candle]` | `tuple(bars)` | **another full tuple when input is a list** | replay-session lifetime |
| Backtest iteration | replay/Core engine | full sequence | events, fills, trades, equity | source sequence remains retained | run lifetime |
| Legacy checkpoint | replay checkpoint | engine/runtime evidence | prefix-replay checkpoint | source cursor absent | artifact lifetime |

The active PR #287 adds a replayable two-pass Backtest input for checkpoint-disabled
runs. It owns `runner.py`, the scalable engine, retention, and checkpoint integration.
This PR deliberately stays outside those files.

## Target streaming flow

```text
Registry descriptor + manifest preflight
        ↓
Deterministically ordered partition descriptors
        ↓
Current Parquet partition checksum
        ↓
Bounded Arrow RecordBatch
        ↓
Direct scalar conversion (no to_pylist/to_pydict)
        ↓
Bounded tuple[KlineRecord, ...]
        ↓
Cross-batch and cross-partition validation state
        ↓
One Candle at a time
        ↓
Replayable Backtest input contract from PR #287
```

## Legacy compatibility boundary

`open_registered_candles` and `load_registered_candles` retain their original
behavior:

- `resolve_registered_kline_dataset(..., deep=True)` remains mandatory;
- all registered partition checksums are verified before iteration;
- raw source artifacts and manifest lineage are deep-verified;
- existing return types and one-shot iterator semantics remain unchanged;
- `load_validated_candles` and current `runner.py` continue using the legacy path.

The streaming implementation is a separate opt-in API. This prevents an optimization
PR from silently weakening preflight integrity or changing failure timing for current
production callers.

## Lightweight streaming preflight

`modules/market_data_ingestion/src/market_data_ingestion/streaming_dataset_reader.py`
adds `resolve_streaming_registered_kline_dataset`.

It validates before opening the first partition:

- storage-root and registry safety;
- exact dataset ID, version, and storage identity;
- VALID status, unresolved issues, and compatible consumer;
- Kline/timeframe capability;
- strict manifest parsing;
- descriptor ↔ manifest dataset hash and identity;
- manifest checksum stability while being read;
- declared partition order, exact continuity, bounds, and non-empty partitions.

It intentionally does not hash every Parquet partition or reread raw source artifacts
during preflight. Instead, each selected Parquet partition is checksummed immediately
before it opens and again after full exhaustion. A damaged future partition therefore
fails when reached and must leave the run incomplete. Registry/manifest lineage is
propagated, but raw source-artifact bytes are not revalidated by this lightweight path.
The unchanged legacy loader remains available whenever full deep preflight is required.

## Bounded partition reader

`iter_registered_kline_batches`:

- consumes partitions only in manifest order;
- projects only canonical Kline columns;
- enforces batch sizes from `1` through `1,000,000`, default `8,192`;
- validates column names, row count, checksum, symbol, timeframe, exchange,
  market type, and completed-candle identity;
- constructs `KlineRecord` directly from Arrow scalars;
- does not use `to_pylist`, `to_pydict`, pandas, `combine_chunks`, or
  `Table.from_batches`;
- closes the Parquet reader on completion, cancellation, or failure;
- reports partition identity and row offset at the error boundary;
- supports restart by partition index plus physical row offset.

Memory ownership is bounded to one Arrow batch, one bounded tuple of
`KlineRecord` objects, the current `Candle`, and constant validation/cursor state.
Consumed batches and partitions are not retained by the source.

## Replayable Candle source

`src/modular_backtest/registered_data.py` exposes
`open_streaming_registered_candles`, returning
`StreamingRegisteredCandleSource`.

The source provides:

- `open(start_position=None) -> StreamingRegisteredCandleIterator`;
- a fresh single-use iterator for every open;
- `source_open_count` telemetry;
- manifest-backed metadata without a candle pre-scan;
- strict dataset, symbol, timeframe, and completed-candle identity;
- duplicate, ordering, and gap validation across RecordBatch and partition boundaries;
- strict registered-dataset gap rejection, independent of the generic file gap policy;
- `checkpoint_position()` returning the exact next physical row;
- deterministic restart without re-emitting the last committed candle;
- clean cancellation and partial-source error propagation.

The cursor advances when a candle is yielded. Final Backtest integration must persist
that cursor only after the corresponding bar has been committed successfully.

## Validation boundary state

The source retains only:

- previous UTC timestamp;
- expected timeframe interval;
- dataset/symbol/timeframe identity;
- partition index and physical row offset;
- last processed timestamp;
- emitted-row state.

This detects duplicates, reverse ordering, and interval gaps across batch and partition
boundaries. `KlineRecord` continues to enforce UTC, close-time/completed-candle
semantics, finite numeric values, OHLC geometry, and non-negative volume/trade fields.

## Source cursor

`RegisteredSourcePosition` contains serializable values only:

- dataset ID and version;
- dataset content hash;
- manifest checksum;
- logical Kline schema fingerprint;
- bridge/source version;
- partition index;
- physical row offset;
- last processed UTC timestamp.

Cursor validation rejects:

- a different dataset, manifest, schema, or bridge version;
- non-UTC timestamps;
- out-of-range or non-canonical offsets;
- a timestamp inconsistent with the physical partition/row position.

No Arrow object, RecordBatch, Parquet reader, open file handle, or local path appears
in the cursor.

## Dataset identity and lineage

`RegisteredDatasetMetadata` propagates:

- registry dataset ID and version;
- dataset content hash;
- manifest checksum;
- logical Kline schema fingerprint;
- symbol and timeframe;
- manifest row count and time range;
- partition count;
- sanitized `dataset://<id>/v<version>` URI;
- manifest lineage;
- bridge version.

The reader does not create a new dataset ID and does not expose the configured local
storage root.

## Look-ahead boundary

Technical reading may allocate one bounded future batch, but only the currently yielded
`Candle` crosses the adapter boundary. Manifest end time, later rows, partition metadata,
and reader state are not exposed to strategy modules. No asynchronous prefetch is
implemented in this phase.

## Baseline and benchmark harness

`benchmarks/registered_dataset_streaming.py` compares:

- `legacy_materialized`: canonical deep preflight plus `list[Candle]`;
- `legacy_tuple_compatibility`: canonical deep preflight plus `tuple[Candle, ...]`;
- `streaming`: opt-in bounded source with selectable batch sizes.

The harness records wall and CPU time, time-to-first-bar, rows/s, sampled RSS,
`tracemalloc`, Arrow allocation, retained Candle/container counts, full-history copy
count, source-open count, and deterministic candle-sequence hash.

No bridge-specific performance number is claimed until the harness runs on an
authoritative environment with project extras and PyArrow. Existing evidence from the
separate Backtest/Core workstream is not a loader benchmark:

| Rows | Path | Evidence | Result |
|---:|---|---|---|
| 10,000 | PR #287 synthetic `research_fast` | complete Backtest/Core run | 0.589 s, 150.8 MiB peak RSS; benchmark input itself is materialized |
| 250,000 | PR #287 synthetic `research_fast` no-op | complete Backtest/Core run | 13.396 s, 230.7 MiB peak RSS; not registered-reader evidence |
| 721,597 | previous legacy registered BTC incident | combined application footprint | approximately 65.2 GiB physical footprint and 64.5 GiB swap |

The supplied `download_summary.csv` reports 721,597 BTCUSDT 5m rows, zero reported
gaps, from `2019-09-08T17:55:00+00:00` through
`2026-07-19T06:55:00+00:00`. The CSV is summary evidence only; it does not contain the
registered dataset ID, partition count, manifests, registry, or Parquet files required
for a data-bearing bridge benchmark.

## Reproduction commands

Focused reader and source tests:

```bash
python -m pytest -q \
  modules/market_data_ingestion/tests/test_streaming_dataset_reader.py \
  tests/backtest_engine/test_registered_streaming_source.py \
  tests/backtest_engine/test_registered_streaming_boundaries.py \
  tests/backtest_engine/test_registered_streaming_cursor.py
```

Unchanged legacy regressions:

```bash
python -m pytest -q \
  modules/market_data_ingestion/tests/test_dataset_reader.py \
  tests/backtest_engine/test_registered_data.py
```

Safe synthetic scale ladder:

```bash
python benchmarks/registered_dataset_streaming.py \
  --rows 1000 --rows 10000 --rows 50000 --rows 100000 \
  --batch-size 1 --batch-size 1024 --batch-size 8192 \
  --output registered-streaming-small.json

python benchmarks/registered_dataset_streaming.py \
  --rows 250000 \
  --batch-size 1024 --batch-size 8192 \
  --output registered-streaming-250k.json
```

Run 500k and the complete BTC corpus only after the 250k run demonstrates bounded RSS,
acceptable throughput, no swap growth, and matching sequence hashes. Raw benchmark
outputs and local datasets must remain untracked.

## Golden equivalence

The isolated test phase is designed to prove:

- identical candle sequence across batch sizes;
- deterministic partition and row-group iteration;
- exact next-row cursor restart;
- repeatable source opens with explicit open counting;
- duplicate and gap detection across boundaries;
- failure propagation after partial iteration;
- no full Arrow row-dictionary materialization;
- no change to the canonical legacy loader and its existing regressions.

Full fills, trades, submissions, cancellations, fees, funding, slippage, final equity,
event ordering, and restarted-run result hashes require integration with PR #287's
final Backtest input/checkpoint contract. This isolated PR does not claim those
application-level results.

## Batch-size comparison

The benchmark emits one result hash per batch size. The acceptance rule is exact hash
equality for batch sizes `1`, `1,024`, `8,192`, and a size larger than the fixture.
Wall time and RSS are evidence fields, not correctness inputs. No authoritative result
rows are recorded yet.

## Time-to-first-bar

The streaming timer includes registry/manifest preflight, checksum of the first selected
partition, opening Parquet, and conversion of the first bounded batch. It excludes all
later partitions. The legacy timer includes deep verification of every registered
partition and raw artifact plus complete list/tuple materialization where applicable.

## Dependency on PR #287

The intended integration point is PR #287's replayable `PreparedCandleSource` and
bounded engine path. The minimum follow-up is:

1. preserve the legacy `open_registered_candles` path;
2. explicitly select `open_streaming_registered_candles` for streaming mode;
3. add `legacy_materialized` / `streaming` configuration and result metadata;
4. pass batch size and bridge version into result evidence;
5. bind `RegisteredSourcePosition` to the final checkpoint schema after bar commit;
6. run legacy, streaming, bounded, and restarted golden comparisons;
7. execute the scale ladder before the complete BTC dataset.

No change in this branch duplicates PR #287's runner, journal, timeframe retention,
equity retention, scalable engine, memory guard, or checkpoint implementation.

## Coordination handoff

- Backtest/Core: integrate the explicit opt-in source and cursor after its contracts
  stabilize; own trading equivalence and checkpoint persistence.
- Ingestion: no writer/schema change requested; reader relies on existing manifest and
  canonical Kline Parquet contract.
- Test Architecture: focused suite is the four streaming files above; PyArrow and the
  ingestion package are required; synthetic scale benchmarks are heavy/non-default.
- CI: smoke command is the focused pytest command; benchmark reports are JSON files and
  should be uploaded only by an explicitly owned benchmark workflow.

## Known limitations

- Current `runner.py` remains on `load_validated_candles` and therefore materializes the
  complete registered history.
- PR #287 currently calls the legacy `open_registered_candles`; final integration must
  explicitly switch only streaming mode to `open_streaming_registered_candles`.
- The lightweight preflight does not reread raw source artifacts.
- Physical row-offset resume may reread earlier RecordBatches in the same partition;
  row-group coordinates are a future throughput optimization.
- No asynchronous prefetch is implemented.
- The schema fingerprint identifies the logical Kline column contract; physical values
  and schema names are checked when a partition opens.
- Bridge RSS/time measurements and application-level trading equivalence are pending an
  authoritative execution environment and the stabilized PR #287 contract.

## Migration plan

1. Merge or stack after PR #287's final source contract is fixed.
2. Add an explicit streaming input mode; do not change the default in this PR.
3. Run focused tests and the synthetic scale ladder.
4. Run full legacy/streaming trading golden tests.
5. Validate restart from persisted cursor.
6. Run the private registered BTC corpus only after safe memory evidence.
7. Consider changing the default only in a separate reviewed decision.

## Rollback plan

- select the unchanged legacy materialized mode;
- stop calling `open_streaming_registered_candles`;
- revert the adapter/reader commits without touching registered datasets;
- continue using `dataset_reader`, `open_registered_candles`, and
  `load_validated_candles`.

Rollback does not mutate a registry entry, manifest, checksum, Parquet file, dataset ID,
lineage record, or trading rule.
