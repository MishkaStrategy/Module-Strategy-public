# Wave 2 Stream D — Dataset Registry and LocalStorage

## Identity

- Base branch: `integration/optimization-wave-1`
- Audited base SHA: `041ee4466232feab448daa377fb33d7898d50fd9`
- Working branch: `optimization/wave2-dataset-storage-slo`
- Draft PR: `#310`
- Contract version: `wave2d-dataset-storage-v1`
- Phase: measurement, durability invariants, bounded recovery and Stream E contract freeze

The stream remains Draft and must not be merged, marked Ready, auto-merged, rebased,
force-pushed, released, deployed or connected to live trading without separate project
authorization.

## Outcome

**Technical verdict: `ACCEPTED — MEASUREMENT COMPLETE`.**

The 1/100/1,000/10,000-dataset baseline meets the proposed SLO on both Python 3.12 and
3.13. Query plans use the existing primary-key and dataset-hash indexes. No repeatable
performance SLO breach and no concrete query, serialization, hashing or locking
bottleneck were established. No speculative performance optimization or new index was
introduced.

The measurement phase did identify correctness and operability gaps:

- Registry schema version was implicit (`PRAGMA user_version=0`);
- a newer Registry schema was not rejected explicitly before use;
- deterministic manifest inventory and bounded aged-temp cleanup were absent;
- a crash after immutable manifest publication but before Registry commit could leave an
  unregistered manifest with no first-class detector;
- the first versioned wrapper used SQLite transaction context managers without explicit
  connection closure, which Python 3.13 surfaced as `ResourceWarning` evidence.

These gaps are remediated through additive, compatible Wave 2 entrypoints. Legacy
`SQLiteDatasetRegistry` and `LocalStorage` behavior remains available unchanged. No
checksum, foreign key, WAL, `synchronous=FULL`, file `fsync`, atomic replace or directory
`fsync` protection was disabled.

## Live recovery checkpoint

Before implementation, live GitHub verification established:

- PR #306 was open, Draft and based on the expected Wave 1 integration branch;
- PR #310 was open and Draft on `optimization/wave2-dataset-storage-slo`;
- initial PR #310 head was `4986a52fd74b622396e620ac481270f4e589454d`;
- no PR conversation comments, review submissions or unresolved review threads existed;
- initial Fast checks and Core CI were successful;
- PRs #308–#310 had no competing full Stream D workload when the 10,000-dataset capture
  was run;
- no full BTC corpus workload was launched by Stream D.

Queued or in-progress checks are never counted as successful. Exact-head checks after
this documentation checkpoint remain authoritative for final GitHub acceptance.

## Ownership inventory

### Owned implementation paths

- `modules/market_data_ingestion/src/market_data_ingestion/registry.py`
- `modules/market_data_ingestion/src/market_data_ingestion/registry_schema.py`
- `modules/market_data_ingestion/src/market_data_ingestion/storage.py`
- `modules/market_data_ingestion/src/market_data_ingestion/storage_recovery.py`
- `modules/market_data_ingestion/src/market_data_ingestion/contracts.py`
- `modules/market_data_ingestion/src/market_data_ingestion/manifest_io.py`
- `modules/market_data_ingestion/src/market_data_ingestion/integrity.py`

Only the two additive Wave 2 implementation files were introduced by this stream. The
legacy files were inspected and tested but not optimized speculatively.

### Owned measurement, test and evidence paths

- `modules/market_data_ingestion/scripts/benchmark_dataset_storage_slo.py`
- `modules/market_data_ingestion/scripts/benchmark_dataset_storage_recovery.py`
- `modules/market_data_ingestion/tests/test_dataset_storage_slo.py`
- `modules/market_data_ingestion/tests/test_dataset_storage_recovery_benchmark.py`
- `modules/market_data_ingestion/tests/test_dataset_storage_orphans.py`
- `modules/market_data_ingestion/reports/dataset-storage-baseline-summary.json`
- `modules/market_data_ingestion/reports/dataset-storage-slo-proposal.json`
- `modules/market_data_ingestion/reports/dataset-storage-handoff-e.json`
- `docs/optimization/wave2/STREAM_D_DATASET_STORAGE.md`

### Read-only without coordinator handoff

- Market Data API, CLI, durable jobs and Web Console/backend;
- acquisition, downloader, adapter and normalization algorithms;
- Backtest/Core, Research, Strategy, Router, Signal and Paper Execution streams;
- shared DTOs/contracts outside the owned storage contract;
- `.github/workflows/**`, root tooling and root configuration.

No file in those read-only surfaces is changed by Stream D.

## Storage model inventory

### Registry schema

Canonical Registry tables at the audited base:

```sql
CREATE TABLE datasets (
    dataset_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    dataset_hash TEXT NOT NULL CHECK(length(dataset_hash) = 64),
    descriptor_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY(dataset_id, version)
);

CREATE INDEX idx_datasets_hash ON datasets(dataset_hash);

CREATE TABLE ingestion_requests (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    descriptor_json TEXT,
    updated_at TEXT NOT NULL
);
```

Dataset identity is immutable `dataset_id + version`; canonical content is bound by
`dataset_hash` plus strict descriptor JSON. The dataset hash is indexed but intentionally
not globally unique because identical content may be represented by different valid
version identities.

### Schema version and migration

Baseline Registry databases used `PRAGMA user_version=0`. The additive
`VersionedSQLiteDatasetRegistry` contract:

1. rejects symlinked Registry paths before path resolution;
2. inspects existing tables and the hash index before mutation;
3. accepts only exact legacy version `0` or supported version `1`;
4. adopts a valid legacy Registry idempotently with `BEGIN IMMEDIATE` and
   `PRAGMA user_version=1`;
5. rejects future schemas with `REGISTRY_SCHEMA_DOWNGRADE_REJECTED`;
6. rejects unknown table/index drift with `REGISTRY_SCHEMA_INVALID`;
7. explicitly closes every direct SQLite inspection connection.

No table or column migration is performed in Wave 2D. Schema version `1` describes the
already-existing canonical table layout.

### Transactions and connection lifecycle

- each canonical public Registry operation opens a short-lived SQLite connection;
- `timeout=10` seconds is retained;
- `foreign_keys=ON`, `journal_mode=WAL` and `synchronous=FULL` are applied;
- the operation commits on success and rolls back on any exception;
- connections are always closed;
- `register_for_request` atomically validates the request claim, registers the dataset and
  attaches the descriptor to the request;
- no hidden internal retry loop is promised;
- contention may surface as SQLite operational failure or typed Registry failure for the
  caller to classify.

### Query plans and indexes

The baseline captures `EXPLAIN QUERY PLAN` for:

- exact `dataset_id + version` lookup;
- latest version by `dataset_id ORDER BY version DESC LIMIT 1`;
- `dataset_hash` lookup.

The exact/latest routes use the primary-key index and checksum lookup uses
`idx_datasets_hash`. No missing-index scan was observed at 10,000 datasets. Therefore no
index was added “on sight.”

### LocalStorage layout

`LocalStorage` creates contained directories for:

- `raw`;
- `normalized`;
- `derived`;
- `manifests`;
- `checkpoints`;
- `runs`;
- `cancellations`;
- `quarantine`;
- `repair_audits`;
- `staging`;
- `locks`.

Every constructed destination is checked for root containment. Unsafe path segments,
path traversal, symlinked parent components and symlink destinations fail closed.

### Atomic publication and durability

Canonical publication sequence remains:

1. create a temporary file in the destination directory;
2. write bytes;
3. flush and `fsync` the file;
4. set final file permissions;
5. atomically `os.replace` the temporary file;
6. `fsync` the containing directory where supported.

Immutable destinations return idempotently only when the existing SHA-256 equals the
incoming content. Conflicting content raises `IMMUTABLE_CONFLICT`.

### Manifests, checksums and lineage

- Dataset manifest contract is version `2`;
- Dataset descriptor contract is version `1`;
- normalized partitions and raw artifacts carry lowercase SHA-256 values;
- readers verify partition/raw checksums before consumption;
- manifest paths are safe relative POSIX paths under the storage root;
- interval partition data type/timeframe declarations must match the manifest exactly;
- duplicate partition/raw IDs are rejected;
- every partition lineage parent must exist;
- self-reference, unknown parents and cycles are rejected;
- deterministic dataset hashes include immutable semantic manifest content.

No corruption check was weakened for throughput.

### Locks and stale-lock recovery

`LocalStorage.lock()` uses exclusive file creation with `O_EXCL` and `O_NOFOLLOW` where
available. The lock records PID and UTC creation time, fsyncs the lock file and removes
only the same inode on exit. A live owner produces retryable `CONCURRENT_RUN`; a dead PID
may be reclaimed; malformed or symlinked lock evidence fails closed.

### Cleanup and recovery

`RecoverableLocalStorage` adds:

- deterministic, sorted, symlink-rejecting `list_manifests()`;
- `find_unregistered_manifests(registry)` for crash-orphan detection;
- `cleanup_orphan_temporary_files(max_age_seconds=...)`.

Cleanup is intentionally narrow. It deletes only aged regular files matching the
same-directory atomic temporary naming pattern and fsyncs affected directories. It does
not delete immutable manifests, partitions or raw artifacts. An immutable manifest with
no Registry row is reported for an explicit repair decision, never auto-deleted.

## Semantic and durability invariants

The Stream D tests cover or reinforce:

- deterministic independent dataset identity;
- idempotent identical manifest publication;
- conflicting manifest content rejection;
- conflicting Registry identity rejection;
- strict descriptor and manifest schema validation;
- missing/corrupted partition detection through existing integrity contracts;
- raw and partition checksum verification;
- lineage parent existence, self-reference and cycle rejection;
- no Registry visibility before commit;
- committed visibility after reopen;
- concurrent committed readers;
- transaction rollback preservation;
- crash before Registry commit leaves no visible dataset;
- crash after commit recovers the complete descriptor and passes integrity check;
- atomic replace failure removes its temporary file;
- aged temporary orphan cleanup leaves fresh and unrelated files untouched;
- unregistered immutable manifest detection;
- stale lock recovery and corrupt lock rejection;
- path traversal, root containment and symlink rejection;
- deterministic manifest listing;
- schema adoption idempotency;
- future-schema downgrade rejection;
- explicit SQLite connection closure and absence of Wave 2 schema connection
  `ResourceWarning`;
- file descriptor growth smoke.

Existing Market Data suites remain authoritative for full manifest parsing, registered
partition corruption, raw corruption, migration parity, external corpus registration and
end-to-end semantic fixtures.

## Benchmark harness

### Registry workload

`benchmark_dataset_storage_slo.py` measures 1, 100, 1,000 and 10,000 datasets with:

- sequential registration;
- duplicate registration;
- conflicting registration;
- exact and latest lookups;
- checksum lookup;
- missing keys;
- four readers plus four writers;
- rollback;
- reopen;
- integrity check;
- query plans;
- database/WAL/SHM growth;
- peak RSS and file descriptors.

The concurrency sample contains 400 reads and 100 writes per scale point. The full
10,000-dataset workload is evidence-only and is not left as an ordinary CI test.

### LocalStorage workload

The same harness measures:

- small immutable manifests;
- a large manifest with 10,000 partition descriptors;
- idempotent republish;
- conflicting/corrupted destination detection;
- deterministic listing;
- stale-lock recovery;
- reopen;
- temporary and total storage bytes.

### Recovery workload

`benchmark_dataset_storage_recovery.py` measures:

- version-0 to version-1 schema adoption;
- subprocess termination immediately after committed registration;
- WAL reopen and committed-data recovery;
- `PRAGMA integrity_check`;
- deterministic manifest listing;
- cleanup of 20 aged atomic temporary files;
- temporary bytes before/after cleanup;
- LocalStorage reopen.

### Realistic-data policy

Synthetic data is used for scale and deterministic latency. Existing repository fixtures
and the inherited full Market Data suite provide semantic parity. The previously accepted
BTCUSDT corpus metadata remains external evidence only; Stream D did not copy market
bytes into Git and did not rerun the full 12,968,018-row corpus.

## Baseline evidence

Full scale source head:
`0564816b9f98dc00c120d1940860dec301c23366`.

Workflow run: `30744695299`.

### 10,000 datasets — Python 3.12

- registration throughput: `911.21 datasets/s`;
- register p95/p99: `1.8685 / 1.9924 ms`;
- exact lookup p95/p99: `0.1435 / 0.2946 ms`;
- latest lookup p95/p99: `0.1357 / 0.1988 ms`;
- writer contention p99: `7.444 ms`;
- concurrent reader availability: `100%`;
- busy errors: `0`;
- integrity check: `12.17 ms`;
- peak process RSS: approximately `21.42 MiB`;
- pinned-reader WAL: `171,832,872 bytes`;
- database: `24,576 bytes`;
- SHM: `360,448 bytes`;
- pinned-reader total pressure: approximately `17,051 bytes/dataset`.

### 10,000 datasets — Python 3.13

- registration throughput: `923.80 datasets/s`;
- register p95/p99: `1.8618 / 1.9871 ms`;
- exact lookup p95/p99: `0.1439 / 0.2764 ms`;
- latest lookup p95/p99: `0.1425 / 0.2350 ms`;
- writer contention p99: `12.7684 ms`;
- concurrent reader availability: `100%`;
- busy errors: `0`;
- integrity check: `12.23 ms`;
- pinned-reader WAL: `171,820,512 bytes`.

The WAL figures are deliberately measured while a reader snapshot prevents ordinary WAL
checkpoint truncation. They are a pressure bound, not steady-state WAL size.

### LocalStorage baseline

Across the recorded environments:

- small manifest p99: no more than `2.7519 ms`;
- 10,000-partition manifest size: approximately `1.78 MiB`;
- large publish/corruption detection: below `318 ms` in the slower recorded sample;
- Registry reopen: below `1.1 ms`;
- LocalStorage reopen: below `0.26 ms`;
- stale-lock recovery: approximately `0.46 ms`;
- immutable conflict detection: PASS;
- no partial published Registry state: PASS.

### Recovery evidence

Recovery source head: `8b91e987883dc8bf8cc28089dc047a4466bc08a2`.

Workflow run: `30745902192`.

Python 3.12:

- schema adoption: `1.299125 ms`;
- crash recovery: `3.075834 ms`;
- integrity check: `0.101917 ms`, result `ok`;
- deterministic listing of 20 manifests: `6.508208 ms`;
- cleanup of 20 aged temporary files: `6.589208 ms`;
- temporary bytes: `170 -> 0`;
- reopen: `0.1895 ms`.

Python 3.13:

- schema adoption: `1.046292 ms`;
- crash recovery: `1.431375 ms`;
- integrity check: `0.093666 ms`, result `ok`;
- deterministic listing of 20 manifests: `3.700458 ms`;
- cleanup of 20 aged temporary files: `5.319542 ms`;
- temporary bytes: `170 -> 0`;
- reopen: `0.18325 ms`.

The same Python 3.13 run exposed unclosed direct schema-inspection connections as
`ResourceWarning`. The Wave 2 wrapper, benchmark and fixtures now use explicit
`contextlib.closing`; a focused regression forces garbage collection and requires zero
`ResourceWarning` from repeated versioned Registry opens.

## Proposed SLO and rationale

Machine-readable thresholds are in
`modules/market_data_ingestion/reports/dataset-storage-slo-proposal.json`.

| Dimension | Proposed threshold | Rationale |
| --- | ---: | --- |
| Registration at 10k | >= 750/s | approximately 18% below the slower observed 911/s |
| Register p95 / p99 | <= 2.5 / 4 ms | above both observed distributions without hiding regressions |
| Exact/latest lookup p95 / p99 | <= 0.5 / 1 ms | materially above observed sub-0.30 ms p99 |
| Concurrent reads | 100% committed-state availability | semantic zero-loss requirement |
| Busy errors | 0 in reference 4R+4W workload | observed zero; any increase requires classification |
| Writer p99 | <= 25 ms | approximately 2x the slower observed 12.77 ms |
| Integrity check at 10k | <= 25 ms | approximately 2x observed 12.23 ms |
| Registry reopen | <= 5 ms | more than 4x observed |
| Peak RSS | <= 64 MiB | approximately 3x observed process peak |
| FD growth | <= 2 | catches connection/file leaks |
| Pinned-reader WAL at 10k | <= 240 MiB | above observed 164 MiB pressure case |
| Pinned total bytes/dataset | <= 24 KiB | above observed approximately 16.7 KiB |
| Small manifest p99 | <= 5 ms | above observed 2.75 ms |
| 10k-partition manifest publish/detect | <= 500 ms | above slowest observed 318 ms |
| Crash recovery | <= 10 ms | more than 3x observed max |
| 20-file aged-temp cleanup | <= 25 ms | more than 3x observed max |
| Partial publication | 0 | durability invariant |
| Committed-data loss | 0 | durability invariant |
| Unregistered manifest detection misses | 0 | recovery invariant |
| Schema downgrade acceptance | 0 | compatibility invariant |

Each numeric threshold is workload- and environment-bound, not a universal hardware
claim. A production threshold revision requires repeat measurements and variance.

## Profiling and optimization decision

### SLO verdict

- registration: PASS;
- lookups: PASS;
- concurrent readers: PASS;
- writer contention: PASS;
- RSS and FD bounds: PASS after explicit connection lifecycle remediation;
- DB/WAL pressure: PASS;
- reopen and integrity: PASS;
- corruption detection: PASS;
- crash recovery: PASS;
- atomic temporary cleanup: PASS;
- zero partial publication and zero committed descriptor loss: PASS.

### Bottleneck verdict

No production performance bottleneck was confirmed. Existing query plans are indexed,
transaction duration remains bounded at 10,000 datasets and no busy failures were
observed in the reference concurrency sample.

### Optimization decision

No performance optimization was performed. In particular, Stream D did not:

- add an index without query-plan evidence;
- batch independent Registry commits by weakening transaction boundaries;
- disable WAL, foreign keys or `synchronous=FULL`;
- remove checksums or deep integrity checks;
- weaken atomic publication or directory fsync;
- cache mutable Registry state in memory;
- alter descriptor, manifest or lineage identity.

The only implementation changes are additive correctness/recovery contracts and explicit
resource closure. If a future workload breaches SLO, the required gate is:

1. repeat the identical workload at least three times;
2. exclude cold filesystem and dependency setup noise;
3. capture query plans;
4. measure transaction duration and lock wait;
5. localize serialization, hashing, fsync or locking cost;
6. propose the smallest change;
7. compare before/after on the same database and environment;
8. rerun integrity, corruption and recovery tests.

## Stream E contract freeze

Machine-readable handoff:
`modules/market_data_ingestion/reports/dataset-storage-handoff-e.json`.

### Registry contract

Stream E should open the Registry through:

- class: `market_data_ingestion.registry_schema.VersionedSQLiteDatasetRegistry`;
- helper: `market_data_ingestion.registry_schema.open_versioned_registry`.

The inherited public methods remain:

- `claim_request`;
- `complete_request`;
- `next_version`;
- `register`;
- `register_for_request`;
- `inspect`;
- `health`.

The legacy `SQLiteDatasetRegistry` remains compatible, but the versioned entrypoint is
the frozen Wave 2 contract for new Control Plane consumers.

### LocalStorage contract

Stream E should open storage through:

- class: `market_data_ingestion.storage_recovery.RecoverableLocalStorage`;
- helper: `market_data_ingestion.storage_recovery.open_recoverable_storage`.

Additional read/recovery operations:

- `list_manifests()`;
- `find_unregistered_manifests(registry)`;
- `cleanup_orphan_temporary_files(max_age_seconds=...)`.

### Error and contention behavior

New fail-closed error codes include:

- `REGISTRY_PATH_INVALID`;
- `REGISTRY_SCHEMA_IO`;
- `REGISTRY_SCHEMA_INVALID`;
- `REGISTRY_SCHEMA_VERSION_UNSUPPORTED`;
- `REGISTRY_SCHEMA_DOWNGRADE_REJECTED`;
- `REGISTRY_SCHEMA_VERSION_INVALID`;
- `MANIFEST_PATH_INVALID`.

Existing Registry/Storage conflict, checksum, traversal, symlink, capacity, permission and
I/O errors remain authoritative. Stream E must inspect `retryable`; it must not retry a
semantic conflict or unsafe path as if it were transient contention.

### Forbidden direct mutations for Stream E

Stream E must not:

- write Registry tables directly;
- change Registry PRAGMAs;
- replace or delete manifests, partitions or raw artifacts directly;
- auto-delete immutable unregistered manifests;
- bypass descriptor/manifest validation;
- bypass checksums, lineage, root containment or symlink checks;
- infer a dataset from a path without immutable Registry identity.

The storage implementation paths listed in the machine-readable handoff are read-only for
Stream E unless the coordinator grants an explicit ownership transfer.

## Validation surface

Mandatory exact-head validation:

- `compileall`;
- Ruff check;
- Ruff format check;
- strict MyPy;
- complete Market Data tests and coverage;
- Registry and LocalStorage invariants;
- schema adoption/downgrade tests;
- concurrency and rollback tests;
- subprocess crash/recovery tests;
- corruption and path-security tests;
- benchmark smoke and recovery smoke;
- deterministic repeat and FD/resource lifecycle smoke;
- clean distribution and wheel installation;
- Core compatibility;
- relevant Market Data interface/export workflows.

Historical exact-head evidence at `8b91e987883dc8bf8cc28089dc047a4466bc08a2`:

- Fast checks `30745902179`: PASS;
- Core CI `30745902175`: PASS on Python 3.12/3.13;
- Market Data ingestion `30745902192`: all quality-package and interface jobs PASS on
  Python 3.12/3.13;
- quality package included compile, Ruff, format, strict MyPy, static security, 458 tests,
  branch coverage above the 80% gate, distributions, Twine and clean-wheel checks;
- Export Python 3.13 passed compile, Ruff, MyPy, security and 100 application tests but
  encountered a transient PyPI TLS EOF while isolated build attempted to fetch
  `setuptools`; this transport failure is not classified as product success.

The final documentation/resource-lifecycle head requires a fresh exact-head run. No
queued or in-progress run may be used as acceptance evidence.

## Evidence files

- `dataset-storage-baseline-summary.json`: full scale headline measurements;
- `dataset-storage-slo-proposal.json`: thresholds, rationale, observations and verdict;
- `dataset-storage-handoff-e.json`: frozen Control Plane contract;
- GitHub Actions artifacts retain Python-specific raw smoke and recovery JSON.

No raw market data, Registry database, WAL, SHM, local path inventory, credentials or
large binary artifact is committed.

## Governance

Keep PR #310 Draft. No merge, Ready-for-Review transition, auto-merge, force-push,
rebase, release, tag, deployment, private credentials, production dataset mutation or
live trading is authorized.
