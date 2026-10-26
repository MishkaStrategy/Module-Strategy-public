# Hardening log — local cycles 01–03

Every round below produced code or evidence and was followed by targeted tests. A round is
not counted merely for re-running the same suite.

## Round 1 — canonical request identity

Added strict identifiers, UTC datetimes, version checks and deterministic request hashing.
Same content with different request IDs hashes identically; reuse of one request ID with
different content fails closed.

## Round 2 — state and checkpoint contracts

Added deterministic transitions and checkpoint serialization. Fixed a mutable datetime
default and covered invalid transitions.

## Round 3 — raw immutability

Added atomic writes, SHA-256, immutable conflict detection, restrictive modes, companion
metadata and path containment.

## Round 4 — exception lifecycle

A test exposed that a frozen slotted dataclass exception could not accept traceback state.
Replaced it with a normal structured exception and retained a regression.

## Round 5 — validation invariants

Added mixed exchange/market/symbol/timeframe checks, UTC alignment, exact/conflicting
duplicates, OHLC/volume checks and provisional-candle rejection.

## Round 6 — deterministic aggregation

Implemented complete-group-only UTC aggregation. Incomplete groups are omitted, never
interpolated or published as complete.

## Round 7 — registry atomicity

Added immutable SQLite versions, request claims and atomic dataset/request completion to
close the lost-ack registration window.

## Round 8 — raw-based recovery

Added adapter restoration contracts. Completed chunks are reconstructed from verified raw
bytes; only pending chunks use the source after restart.

## Round 9 — registered integrity

Added partition checksum and manifest verification. Tamper injection is detected before
consumption/export.

## Round 10 — archive and path security

Added ZIP member/path checks, expansion limits, compression-ratio limits, truncated raw
framing rejection and traversal tests.

## Round 11 — semantic stream separation

Introduced distinct traded kline, synthetic price, funding, open-interest and metadata
records so mark/index data cannot be interpreted as traded OHLCV.

## Round 12 — multi-type orchestration

Added per-data-type plans, validation reports, partitions and source lineage. Required
unsupported streams fail before download; optional unavailable streams are explicit.

## Round 13 — Binance auxiliary contracts

Added USD-M funding, mark, index, premium and open-interest endpoints plus parsers and
capability boundaries. Spot cannot request futures-only streams.

## Round 14 — official retention policy

Required Binance open-interest history outside the official recent-history window fails
closed; optional history is clipped with a recorded limitation.

## Round 15 — archive-to-REST source fidelity

A missing monthly archive now records `official_rest` as the actual source. Resume selects
the matching raw parser rather than treating REST bytes as ZIP content.

## Round 16 — update and backfill

Implemented immutable version creation, parent partition reuse, overlap rejection and
stream-specific historical backfill.

## Round 17 — damaged normalized repair

Checksum corruption triggers exact range replacement and removal/rebuild of dependent
derived partitions.

## Round 18 — local derived repair

A damaged derived partition is rebuilt from verified normalized source partitions without
network access.

## Round 19 — metadata-from-raw repair

Instrument and exchange metadata snapshots are restored from immutable raw exchange-info
evidence with adapter and identity checks.

## Round 20 — exact gap repair

Known missing kline intervals are requested exactly, merged with healthy monthly data,
validated and published as a new version.

## Round 21 — leading/trailing gap evidence

Validation now checks requested boundaries, not only internal adjacency. Partial datasets
retain explicit leading and trailing evidence.

## Round 22 — listing/delisting classification

Absence outside an instrument lifecycle is classified as expected INFO evidence rather
than an unresolved market-data gap. Fully out-of-lifecycle requests are rejected.

## Round 23 — dataset-hash semantics

Removed dataset ID and registry version from the content hash. Migrated the legacy
`UNIQUE(dataset_hash)` registry schema so equal logical content can exist in separate
immutable versions.

## Round 24 — compatibility gates

Centralized package, normalization, manifest and hash-policy versions. Incompatible base
manifests, adapters and resume envelopes fail closed.

## Round 25 — bounded concurrency

Implemented bounded parallel acquisition with deterministic raw/checkpoint publication.
Successful neighbors from a partially failed batch survive and are reused by resume.

## Round 26 — lock and atomic-write recovery

Covered live lock contention, stale dead-process lock cleanup, corrupt/symlink locks,
destination/parent symlinks, disk-full, permission-denied and temporary cleanup.

## Round 27 — network fault injection

Covered `429 Retry-After`, bounded `5xx`, timeout, circuit breaker, repeated page, empty
middle page, non-monotonic page and oversized response behavior.

## Round 28 — expanded archive security

Covered traversal, multiple unexpected members, damaged ZIP, excessive compression ratio
and checksum mismatch.

## Round 29 — source-schema/rule drift

Instrument metadata is resolved again before registration. Changes to tick size, contract
type, lifecycle or identity pause the run rather than publishing mixed-rule data.

## Round 30 — explicit resume and cancel

Added immutable resume specifications, CLI resume, cancellation markers and paused-run
transition to `CANCELLED`.

## Round 31 — bounded-memory CSV pipeline

Replaced full CSV materialization with one-pass atomic writing and monthly ordered groups.
Temporary output is removed when an iterator fails.

## Round 32 — strict SQLite record spool

Added a JSON-only SQLite spool with external ordering, 1,000-row batches, schema version,
symlink rejection and deterministic record codec for every record family.

## Round 33 — streaming validation and aggregation

Validation consumes one sorted pass and stages accepted records in bounded batches.
Derived OHLCV is aggregated from a stream. Full-dataset Python tuples were removed from
the main CSV pipeline and repair paths.

## Round 34 — deep lineage integrity

Deep validation recomputes manifest/dataset identity, verifies partition rows/checksums,
raw descriptor fields and all raw/partition lineage references.

## Round 35 — raw identity collision audit

Equal bytes in different data-type/source contexts previously could share an artifact ID.
Raw identity now includes checksum plus full stream/source/adapter context.

## Round 36 — normalized schema and spool tamper audit

CSV/Parquet readers require exact ordered schemas. Invalid rows become structured storage
errors. Spool payload identity is cross-checked against indexed type/time/range/hash, and
malformed/unsupported SQLite databases fail closed.

## Round 37 — auxiliary interval gap repair

Generalized exact gap repair to mark, index, premium and open-interest interval streams.
Wrong-stream adapter output produces `SOURCE_CONFLICT` and cannot register a version.

## Round 38 — connection lifecycle

Coverage with warnings exposed unclosed SQLite connections because the standard SQLite
context manager does not close. Registry connections now always commit/rollback and close;
the full suite passes with `ResourceWarning` promoted to an error.

## Round 39 — measurable streaming benchmark

Ran a 20,000-base-row, 20-chunk, 5m→15m/1h ingestion with deep integrity. Peak traced
Python allocations were 3,115,837 bytes, staging was empty after completion and integrity
was `ok`.

## Round 40 — Results Registry boundary hardening

Verified the bridge against the canonical dataset-record v1 schema. Metadata and funding
receive independent lineage hashes, event-only datasets use the explicit `event`
timeframe, and descriptor/manifest mismatches fail closed.

## Round 41 — dependency-free static safety audit

Added a reproducible AST/source auditor. Production code has zero runtime assertions,
bare exceptions, dynamic `eval`/`exec`, `shell=True`, mutable defaults, unsafe
serialization imports, empty functions, missing return annotations or lines over 100
characters.

## Round 42 — deterministic property testing

Added seeded randomized checks for streaming aggregation equivalence, typed kline codec
round-trips, canonical mapping-order independence and timeframe-floor idempotence. These
checks do not replace the blocked Hypothesis gate.

## Round 43 — complete local CLI vertical slice

Exercised exchange/market/capability/instrument discovery plus Fake download, inspect,
deep validate, registry status and missing-dataset errors through the real CLI entrypoint.

## Current local gate

- 133 tests collected and passed;
- `ResourceWarning`-as-error passed;
- total package coverage: 81%;
- package/pipeline/records key paths have targeted fault-injection evidence;
- 20,000-row streaming benchmark passed.

## Gates that cannot be counted in this environment

- Python 3.12;
- Ruff;
- strict MyPy;
- Hypothesis/fuzzing;
- PyArrow/Parquet execution;
- real Binance DNS/archive/REST acceptance;
- repository-integrated consumer and CI checks.

## Round 44 — raw restore context and metadata boundary

Raw payload and metadata symlinks are rejected. Malformed JSON, missing fields, invalid
enums/datetimes and non-object response metadata map to `RAW_METADATA_CORRUPT`. Raw
artifact identity is recomputed from checksum plus instrument/chunk/source/adapter context;
source URI, adapter version, time range, byte-size and artifact-ID tampering fail closed.
Secret-bearing source URIs are rejected before any raw file is published.

## Round 45 — empty-stream and planned-range validation

Every requested interval stream is validated even when the adapter returns zero records.
Expected coverage is derived from the effective `DownloadPlan`, including retention clipping.
A fully empty required interval produces a complete gap and cannot be accepted by the
partial-gap policy.

## Round 46 — adapter envelope and source URI security

`AcquiredData` must match the planned chunk identity, data type and exact range. Raw payload
and metadata types are immutable and validated. Absolute source URIs cannot contain
userinfo, query strings or fragments, preventing credentials from entering manifests,
raw metadata or dataset hashes.

## Round 47 — strict manifest schema and lineage graph

Manifest v2 uses exact-key parsing without string/integer/boolean coercion. Unknown and
missing fields fail closed. Partition/raw identifiers are unique; partition lineage cannot
reference unknown nodes, itself or a cycle; declared data types/timeframes must match the
actual partition graph; partition paths must be safe relative POSIX paths.

## Round 48 — strict dataset descriptor and recovery drift

`DatasetDescriptor` now validates safe manifest/storage URIs, exact positive integer fields,
market enum, non-empty unique data types, unique timeframes, event-only semantics,
unresolved issues and compatible consumers. Registry deserialization uses an exact-key
schema and maps all malformed payloads to `DESCRIPTOR_SCHEMA_INVALID`. A recovery audit
also detected and repaired stale local copies of storage/version contracts before GitHub
publication.

## Current GitHub preflight gate

- 207 tests collected and passed;
- `ResourceWarning` promoted to an error;
- dependency-free static audit: 0 findings;
- package `0.4.0`, manifest schema v2, validation policy `2.0.0`;
- component branch created from exact staging SHA
  `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`;
- Ruff, strict MyPy, Hypothesis, PyArrow and clean dependency installation remain assigned
  to GitHub CI because the local package index returned HTTP 503.
