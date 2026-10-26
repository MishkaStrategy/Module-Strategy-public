# Market Data legacy CLI parity audit

- Status: audit enforced; read parity, durable UPDATE/BACKFILL and download-config migration implemented
- Accepted audit parent: `cc185c52287c580bec5319672ab92eedd84b579b`
- Exchange read parity parent: `50ba58d1640508468631d12ebb1b6ec1bea0e751`
- Durable UPDATE parent: `54e89e203be62e800e3058b74e59c5b4c9f5405a`
- Durable BACKFILL parent: `3a9b9e655b12669307154b68d22c3192b75418cc`
- Download migration parent: `b1ec60bacefe3d7bf5d9f837f742ac2bb9d64a4d`
- Legacy entry point: `market-data`
- Durable entry point: `market-data-v1`
- Migration entry point: `market-data-v1-migrate`
- Machine-readable source: `docs/integration/MARKET_DATA_CLI_PARITY_MATRIX.json`

## Purpose

This audit records the actual migration boundary between the original argparse CLI and the durable
application CLI. It does not authorize legacy entry-point removal, automatic mutation aliases,
integration merge, release or deployment.

Parity does not mean preserving unsafe behavior. A migration is accepted only when operation,
identity, recovery, filesystem and error contracts are equivalent or replaced by safer documented
contracts.

## Command decisions

| Legacy command | Classification | V1 migration | Decision |
| --- | --- | --- | --- |
| `exchanges` | equivalent | `exchanges` | Shared application composition lists configured adapters. |
| `markets` | equivalent | `markets --exchange` | Returns exact configured adapter market types. |
| `capabilities` | equivalent | `capabilities --exchange` | Returns the detailed typed capability contract. |
| `instruments` | equivalent | `instruments --exchange --market --symbol` | Resolves typed metadata without raw evidence or source URIs. |
| `download` | safely replaced | `market-data-v1-migrate`, `download`, `jobs show/events/cancel` | Legacy YAML is converted without execution to strict request JSON; execution becomes an idempotent durable job. |
| `inspect` | equivalent | `datasets show` | Both resolve an immutable Registry version. |
| `validate` | equivalent | `datasets validate` | Deep validation remains explicit; envelopes differ. |
| `doctor` | equivalent | `doctor` | Local storage, Registry and adapter diagnostics remain available. |
| `status` | safely replaced | `doctor` | Registry health is included in the broader diagnostic response. |
| `update` | safely replaced | `datasets update`, `jobs show/events` | Durable job pins the parent version/hash, recovers by identity and publishes one child. |
| `backfill` | safely replaced | `datasets backfill`, `jobs show/events` | Durable job pins parent/timeframe, rejects overlap, directs replacement to repair and reconciles publication. |
| `repair` | safely replaced | repair preview/confirmation/execution | Direct mutation becomes preview, actor binding, one-time confirmation and durable execution. |
| `export` | intentionally incompatible | `datasets export`, `artifacts show`, `jobs show` | User-selected dataset export destination remains prohibited. |
| `resume` | intentionally incompatible | automatic recovery plus job inspection | Manual run-ID resume becomes leases, fencing and reconciliation. |
| `cancel` | safely replaced | `jobs cancel` | Identity changes to durable job ID and cancellation remains phase-limited. |

## Legacy download YAML migration

The isolated base-dependency command is:

```text
market-data-v1-migrate --config legacy.yaml [--as-of ISO-8601] [--output request.json]
```

It performs no exchange acquisition, Registry mutation or dataset write. Successful stdout is exactly
one deterministic `CreateDownloadRequest` JSON document. When `--output` is supplied, the same bytes
are published atomically to a new private-mode file; an existing target, symlink target or symlinked
parent is refused.

The converter delegates schema and semantic validation to existing backend `load_config`, then maps:

- exchange, market and symbol;
- required and optional data types;
- base and derived timeframes;
- explicit/full-history UTC range;
- storage format and compression;
- unresolved-required-gap policy;
- concurrency and memory bound.

Fail-closed boundaries:

- `latest_closed` requires explicit `--as-of` so the result is reproducible;
- explicit legacy `request_id` cannot replace durable job identity;
- non-default arbitrary `storage.root` cannot be embedded in request JSON;
- `resume: false` cannot be represented by durable automatic recovery;
- explicitly selected network runtime settings remain composition settings, not request fields;
- legacy validation controls absent from `CreateDownloadRequest` are rejected rather than dropped;
- credential-like keys are rejected and values are never copied into diagnostics;
- input must be bounded UTF-8, regular and non-symlink;
- no output file is written without explicit `--output`.

Migration error stdout uses the standard structured error envelope. Exit codes are `0,1,2,7,9`.

## Durable UPDATE replacement

- CLI: `market-data-v1 datasets update <dataset-id> --request <json> --idempotency-key <key> --actor-id <actor>`;
- API: `POST /api/v1/datasets/{dataset_id}/updates`.

UPDATE pins exact parent version/hash/end, scopes idempotency to the actor, invokes backend
`DatasetOperations.update` with `request_id=job_id`, uses existing leases/fencing and reconciles an
already published request without another version. Cancellation remains fail-closed until a canonical
backend cancellation identity is proven.

## Durable BACKFILL replacement

- CLI: `market-data-v1 datasets backfill <dataset-id> --request <json> --idempotency-key <key> --actor-id <actor>`;
- API: `POST /api/v1/datasets/{dataset_id}/backfills`.

BACKFILL pins exact parent version/hash and normalized stream timeframe, rejects overlap with
registered partitions of the same stream, returns `use_repair=true` for replacement intent, uses
deterministic `request_id=job_id` and recovers/reconciles without duplicate versions. Cancellation
remains fail-closed until a canonical backend cancellation identity is proven.

## Explicit incompatibilities

- Direct repair aliases remain prohibited because they bypass preview and confirmation.
- Caller-selected dataset export destinations remain prohibited.
- Manual backend run-ID resume remains replaced by durable startup recovery.
- Cancellation remains available only for operations/phases with proven backend identity.
- Migration tooling does not authorize deletion or replacement of `market-data`.

## Automated enforcement

`tests/test_cli_parity_audit.py` builds actual argparse and Typer/Click trees, compares options and
positionals, verifies every legacy classification and declared v1 path, extracts literal exit codes
through the Python AST and confirms no release/integration authorization.

`tests/test_download_config_migration.py` proves deterministic field preservation, no execution,
`latest_closed --as-of`, full-history conversion, fail-closed identity/runtime/destination/secret
boundaries, bounded non-symlink input, atomic private no-clobber output and structured CLI errors.

Dedicated clean-wheel evidence must execute all three installed scripts:

- `market-data`;
- `market-data-v1`;
- `market-data-v1-migrate`.

## Next implementation order

1. Document end-user migration examples and runtime-setting handoff.
2. Add measured progress counters for durable acquisition jobs.
3. Add request/rate bounds and authenticated administrative audit logging.
4. Consider legacy entry-point deprecation only in a separately authorized slice; do not remove it
   during migration implementation.
