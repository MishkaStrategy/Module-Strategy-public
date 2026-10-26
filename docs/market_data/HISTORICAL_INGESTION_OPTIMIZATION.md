# Historical Market Data Ingestion Performance and I/O Optimization

## Scope and audited identity

- Repository: `MishkaStrategy/Module-Strategy`
- Component: `modules/market_data_ingestion`
- Audited integration branch: `integration/modular-strategy-full-rc`
- Audited base SHA: `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- Optimization branch: `optimization/historical-data-ingestion-io`
- Draft PR: `#290`
- Measured optimization source head: `23f1975171137c9943774fabb80eb159949cb884`

This work changes only ingestion-owned staging, partition checksum reuse, tests, and
benchmark evidence. It does not change the manifest schema, dataset identity, checksum
algorithm, normalized record schema, Parquet encoding options, UTC policy, OHLCV
interpretation, gap policy, duplicate policy, Dataset Registry contract, Backtest loader,
Core, or trading semantics.

## Pipeline architecture

The audited production lifecycle is:

`request -> capability discovery -> instrument resolution -> download plan -> raw restore/download -> immutable raw publication -> normalized RecordSpool -> strict ordered validation -> monthly normalized/derived partition publication -> manifest hash -> manifest publication -> registry transaction -> checkpoint/final report -> staging cleanup`

| Stage | Input | Output | Main ownership and failure boundary |
|---|---|---|---|
| Request validation | `MarketDataRequest` | validated request identity | `IngestionService.execute`; fail closed before acquisition |
| Capability discovery | adapter | supported market/data families | exchange adapter contract |
| Instrument resolution | symbol/market | canonical instrument + raw metadata evidence | adapter; identity drift checked again after acquisition |
| Planning | effective range | deterministic `DownloadChunk` sequence | adapter; checkpoint identity includes execution settings |
| Acquisition | archive/REST payload | typed normalized records + immutable raw payload | adapter and `LocalStorage.write_raw` |
| Raw publication | bytes + descriptor metadata | raw artifact and metadata pair | atomic write, fsync, immutable conflict check |
| Acquired spool | typed normalized records | SQLite canonical JSON rows | `RecordSpool`; bounded transaction batch |
| Validation | sorted acquired stream | validation report + accepted membership | `validate_sorted_records`; strict boundaries, OHLCV, gaps and duplicates |
| Partitioning | accepted ordered stream | UTC calendar-month groups | `IngestionService._write_monthly` |
| Serialization | one monthly group | deterministic Parquet/CSV bytes | `LocalStorage.write_partition` |
| Partition checksum | serialized bytes | canonical SHA-256 descriptor | calculated from the exact payload written |
| Publication | temporary file | immutable partition path | same-filesystem atomic replace + directory fsync |
| Derived data | accepted 5m stream | requested complete aggregate groups | existing streaming aggregation contract |
| Manifest | descriptors + validation + lineage | immutable manifest JSON | canonical dataset hash unchanged |
| Registry | dataset descriptor | request-bound immutable registry row | SQLite registry transaction after manifest publication |
| Recovery | checkpoint/raw artifacts | resumed deterministic run | bounded retry/resume; completed raw chunks restored |
| Cleanup | SQLite staging/WAL/SHM | no staging residue | `RecordSpool.__exit__` on success and failure |

## Benchmark corpus

### Inventory source

`download_summary.csv` was verified independently:

- SHA-256: `a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893`
- 50 rows, 10 symbols, timeframes `5m`, `10m`, `15m`, `1h`, `4h`
- 12,968,018 total rows
- 6,693,179 base 5m rows
- reported gap events: 0

The summary is inventory evidence only and is not treated as market-data content.

### Real large corpus

- Symbol: `BTCUSDT`
- Market: Binance USDT-M perpetual
- Timeframe: `5m`
- Range: `2019-09-08T17:55:00Z` through `2026-07-19T06:55:00Z`
- Input rows: 721,597
- Source size: 34,020,301 bytes
- Source SHA-256: `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1`
- Monthly partitions: 83
- Cache condition: paired warm-source-cache runs
- Repetitions: 2 baseline and 2 optimized on the same runner surface

### Deterministic scale ladder

- Small: 20,000 generated BTCUSDT 5m rows, 3 repetitions per variant
- Medium: 100,000 generated BTCUSDT 5m rows, 3 repetitions per variant
- Multi-symbol: ADAUSDT, BTCUSDT, ETHUSDT; 8,928 rows each; 26,784 total rows; 3 repetitions per variant

Synthetic corpora exercise the exact typed spool, strict validation, boundary state, and
typed reread path. The real corpus remains the authoritative end-to-end scale result.

## Baseline evidence

The retained v0.4 evidence on Linux x86-64, Python 3.13.5, and PyArrow 25.0.0 was:

| Dataset | Rows | Wall time | Rows/s | Peak RSS | Partitions |
|---|---:|---:|---:|---:|---:|
| BTCUSDT 5m retained release benchmark | 721,597 | 1,101.233 s | 655.263 | 297.04 MiB | 83 |

Retained phase times were decode/spool 226.502 s, validation 444.886 s,
publication 210.964 s, and checksum-verified typed reread 218.563 s. Those values are
historical evidence, not used as the direct paired performance denominator because the
hardware and cache surface differ.

The paired control reran the audited base and optimized head on the same runner:

| Dataset | Rows | Baseline wall | Optimized wall | Baseline rows/s | Optimized rows/s | Change |
|---|---:|---:|---:|---:|---:|---:|
| Small | 20,000 | 10.430 s | 7.336 s | 1,917.5 | 2,726.4 | -29.67% |
| Medium | 100,000 | 49.024 s | 33.941 s | 2,039.8 | 2,946.3 | -30.77% |
| Multi-symbol | 26,784 | 4.528 s | 3.397 s | 5,915.0 | 7,883.5 | -24.98% |
| Real BTCUSDT 5m | 721,597 | 890.201 s | 731.976 s | 810.600 | 985.821 | -17.77% |

## CPU, I/O, and memory profile

### Confirmed hot path

The baseline `RecordSpool` performed these operations for every accepted row:

1. create a primitive normalized row;
2. encode canonical JSON;
3. independently canonicalize and hash the same row identity;
4. insert a full acquired payload;
5. decode and reconstruct it during validation;
6. canonicalize it again for tamper verification;
7. encode and insert an identical full validated payload;
8. decode and reconstruct the second payload during partition publication.

The baseline also committed fixed 1,000-row batches under `synchronous=FULL`.

### Pass count before and after

| Data surface | Baseline | Optimized | Change |
|---|---:|---:|---|
| Source Parquet scans | 1 | 1 | unchanged |
| Acquired payload writes | 1 per row | 1 per row | unchanged |
| Full validated payload writes | 1 per accepted row | 0 in the normal path | removed |
| Validation membership writes | none | one compact rowid | added compact link |
| Acquired spool full scans | 1 | 1 | unchanged strict validation |
| Validated spool full scans | 1 | 1 | unchanged publication input |
| Serialized payload copies per logical row | 2.0 | 1.0 | -50% |
| New Parquet checksum rereads during publication | 1 per new partition | 0 | removed |
| Existing immutable destination verification | 1 on collision/retry | 1 | preserved trust boundary |
| Final checksum-verified typed partition reread | 1 | 1 | preserved golden evidence |

The optimization removes duplicate bytes and redundant hashing without removing a
correctness scan. The final registered-partition reread remains intentionally independent.

### Storage and RSS

For 100,000 rows, median peak sampled spool bytes fell from 224,350,472 to
90,481,016 bytes (-59.67%). For 20,000 rows, they fell from 45,854,512 to 21,012,712
bytes (-54.18%). The three-symbol total fell from 66,590,376 to 42,172,896 bytes
(-36.67%).

The real paired run reported peak RSS increasing from 190,570,496 bytes (181.74 MiB) to
215,171,072 bytes (205.20 MiB), a 12.91% increase or 23.46 MiB absolute. The retained
optimized state is bounded by a 1,024-entry validation-rowid correlation window and an
8,192-link integer buffer; it does not scale with history length. The optimized peak also
remains approximately 91.8 MiB below the retained 297.04 MiB release benchmark peak.
The increase is documented rather than hidden because it is slightly above the preferred
10% threshold. Further reduction should target compact correlation keys, not restore the
second full payload copy.

## Implemented changes

### `market_data_ingestion/spool.py`

- computes canonical identity from the canonical payload already encoded for storage;
- uses schema v2 `validated_records(record_rowid)` membership links;
- reuses the immutable acquired payload for accepted records;
- preserves full canonical payload, typed round-trip, indexed metadata, symbol, market,
  timeframe, timestamp, and identity checks;
- keeps a compatibility path for direct validated-stage writes;
- bounds pending rowid ownership to 1,024 records;
- batches records and membership links with a configurable default of 8,192;
- flushes links before reads, counts, clear operations, and close;
- removes staging database, WAL, and SHM files on context exit.

### `market_data_ingestion/storage.py`

- hashes exact Parquet payload bytes before atomic publication;
- passes the digest into immutable collision checking and partition descriptor creation;
- removes the unconditional full reread of a newly written partition;
- preserves destination reread when a file already exists, because retry/idempotency must
  compare the actual immutable destination;
- preserves SHA-256 and exact partition ID construction.

### Benchmarks and tests

Added deterministic benchmark harnesses and ingestion-specific workflows for paired base
and head execution. Added tests for canonical identity, validation links, direct-write
compatibility, batch boundaries, cross-batch duplicates and gaps, bounded pending state,
evicted-row fallback, clear/cleanup behavior, canonical tamper rejection, typed round-trip,
and Parquet checksum reuse.

## Phase profile: real BTCUSDT 5m

| Stage | Baseline median | Optimized median | Change |
|---|---:|---:|---:|
| Source decode and acquired spool | 181.918 s | 135.853 s | -25.32% |
| Strict stream validation and promotion | 355.421 s | 249.945 s | -29.68% |
| Monthly Parquet publication | 160.360 s | 150.871 s | -5.92% |
| Checksum-verified typed reread | 192.342 s | 194.920 s | +1.34% |
| Total | 890.201 s | 731.976 s | -17.77% |

The unchanged final reread is now the largest remaining isolated I/O phase. It is not
removed in this PR because it is an independent registered-byte trust boundary and golden
equivalence gate.

## Golden equivalence

For the paired real benchmark:

- row count equal: yes, 721,597;
- partition count equal: yes, 83;
- typed reread digest equal: yes;
- all validation gates passed: yes;
- schema and column order: unchanged;
- timestamp sequence and UTC boundaries: unchanged;
- OHLCV values: unchanged;
- duplicate count: zero before and after;
- gap count: zero before and after;
- partition naming and boundaries: unchanged;
- checksum algorithm: SHA-256 unchanged;
- manifest schema and dataset identity contract: unchanged;
- deterministic rerun/idempotent existing-file verification: preserved.

Per-symbol typed digests also matched for ADAUSDT, BTCUSDT, and ETHUSDT in all
multi-symbol repetitions.

## Crash recovery and atomic publication

- Raw and normalized writes still use a temporary file in the destination directory,
  flush, `fsync`, mode hardening, atomic `os.replace`, and directory `fsync`.
- Existing immutable destinations are still read and hashed before reuse.
- Registry registration still occurs only after manifest publication.
- A partial staging spool is never a registered dataset.
- Context-manager cleanup still removes SQLite, WAL, and SHM staging files on success or
  failure.
- Checkpoint and raw-artifact resume behavior is unchanged.
- No concurrent publication of one request identity is introduced; the existing storage
  lock remains authoritative.

## Reproduction commands

From `modules/market_data_ingestion` after installing `.[dev,parquet,interface]`:

```bash
python scripts/benchmark_spool_io.py \
  --rows 20000 \
  --source-batch-size 8192 \
  --spool-batch-size 8192 \
  --cache-condition warm-process-isolated \
  --output /tmp/mdi-spool-20000.json

python scripts/benchmark_spool_io.py \
  --rows 100000 \
  --source-batch-size 8192 \
  --spool-batch-size 8192 \
  --cache-condition warm-process-isolated \
  --output /tmp/mdi-spool-100000.json

python scripts/benchmark_spool_multisymbol.py \
  --output /tmp/mdi-spool-multisymbol.json

python scripts/benchmark_real_parquet.py \
  /path/to/BTCUSDT_5m.parquet.bin \
  --expected-sha256 2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1 \
  --batch-size 8192 \
  --output /tmp/mdi-real-btc.json
```

The PR workflows perform paired baseline/head runs in isolated worktrees on the same
runner and upload JSON evidence only. Raw and normalized market bytes are not uploaded.

## Full-corpus planning

The complete 12,968,018-row corpus was not processed end to end in this optimization PR.
At the measured optimized real-BTC throughput of 985.821 rows/s, the row-count-only lower
bound is about 3.65 hours before symbol setup, registry, filesystem variance, and
multi-timeframe overhead. Running all 50 files would duplicate already accepted corpus
validation while increasing runner occupancy and temporary-disk risk. The safe accepted
scope is one real multi-year BTC stream plus a three-symbol deterministic concurrency/
isolation surface.

## Limitations and deferred work

1. The preferred 25% end-to-end real-corpus improvement was not reached; the measured
   paired result is 17.77%. The targeted spool stages exceeded 25%, but the independent
   final reread now dominates.
2. Real-run RSS increased 12.91% on the paired runner. The increase is bounded and below
   the retained release peak, but compact correlation-key ownership remains a safe future
   optimization.
3. The real benchmark removes its spool before its final storage snapshot, so its reported
   `spool_bytes` field is zero. The deterministic 20k/100k and multi-symbol harnesses are
   the authoritative temporary-storage measurements.
4. macOS does not expose `/proc/self/io`; process read/write byte counters are unavailable
   for the paired Mac runs. File-level temporary size and pass counts remain measured.
5. The checksum-verified typed reread was intentionally retained. Combining checksum and
   typed validation would require a separate trust-boundary design and crash-recovery
   review.
6. Parquet encoding, row-group size, compression, and byte identity were not changed.
7. Dataset Registry performance and registered-dataset-to-Backtest streaming are separate
   workstreams.
8. General CI architecture and root pytest organization are outside this PR.

## Next safe optimizations

- replace full-record pending correlation keys with compact canonical identities while
  preserving duplicate ordering and bounded ownership;
- instrument file-open and physical I/O counters on Linux benchmark runners;
- evaluate a verified tee/checksum reader for the final partition scan without weakening
  independent byte verification;
- profile SQLite index selection and WAL checkpoint cost on NVMe and slower storage;
- add an explicit disk-space preflight only after a reliable upper-bound estimator is
  derived from source metadata and selected output format.
