# Market Data measured download progress

- Status: stacked Draft PR #263 under exact-head validation
- Parent: `23594b7598919574849742f5f5b404714369656e`
- Formatting: canonical Ruff 0.16.0 import sorting and formatting applied
- Scope: direct durable `DOWNLOAD` jobs only
- Non-goals: synthetic whole-job percentage, exchange semantics changes, UPDATE/BACKFILL redesign,
  Web mutation controls, integration merge, release or deployment

## Source of truth

Progress is observability, not execution authority. The existing backend remains authoritative for
planning, checkpoint validation, acquisition, raw publication, normalization, validation and Dataset
Registry publication.

Measured progress consumes three existing backend contracts:

1. the adapter's exact ordered `DownloadPlan.chunks`;
2. the validated checkpoint's completed chunk IDs;
3. committed pipeline log records emitted in deterministic plan order after raw restore/download
   outcomes are accepted.

No source URI, credential, raw payload or private adapter evidence enters the public progress model.
Observer failures are suppressed and cannot change ingestion outcomes.

## Durable mapping

The backend emits scoped `IngestionProgressSnapshot` values. The canonical application facade maps
them to the existing `JobProgress` contract and persists them through fenced
`SQLiteJobStore.record_progress` writes.

- `PLANNING`: indeterminate; `planned_chunks` and checkpoint count are available as counters;
- `DOWNLOADING`: determinate only when the plan contains historical chunks;
- percentage: unique checkpoint-completed plus newly committed chunks divided by exact plan chunks;
- later phases: indeterminate while preserving measured counters and current committed range;
- progress is visible through job detail, ordered `PROGRESS` events, REST, SSE and CLI job events.

Counters:

- `planned_chunks`;
- `checkpoint_completed_chunks`;
- `downloaded_chunks`;
- `restored_chunks`;
- `downloaded_records`;
- `restored_records`;
- `downloaded_bytes`.

`current_range_start/end` identify only the most recently committed chunk range. Data type and
bounded timeframe may be included in the human-readable message; source location is excluded.

## Resume and monotonicity

A resumed process initializes unique completed units from the validated checkpoint and cumulative
counters from the last durable progress snapshot. The tracker records restored checkpoint chunks and
newly downloaded chunks without reducing any prior counter.

Within one progress phase:

- determinate progress cannot become indeterminate;
- total units cannot change;
- completed units cannot decrease;
- named counters cannot decrease.

Regressive, stale or wrong-phase snapshots are rejected by the application observer and suppressed by
the non-authoritative backend observer.

## Concurrency

Acquisition workers may finish out of order. Progress is not emitted from worker completion callbacks.
It is derived from the pipeline's committed outcome log records, which are produced in deterministic
plan order. Therefore public current ranges and percentages remain stable across concurrency values.

## Acceptance

Focused Python 3.12/3.13 tests must prove:

- observer scoping, recursion protection and failure isolation;
- exact six-chunk percentage sequence with concurrent FakeAdapter acquisition;
- deterministic current-range order;
- actual rows and raw-byte counters;
- durable REST/event/CLI visibility;
- pause, restart, checkpoint restoration and monotonic counters;
- persistence failure does not change a successful dataset outcome;
- invalid snapshot and duplicate committed-event rejection;
- no source URI leakage.

Inherited migration, BACKFILL, UPDATE, CLI parity, exchange read, package/interface, export, Web,
real browser E2E and Core gates remain mandatory before merge.
