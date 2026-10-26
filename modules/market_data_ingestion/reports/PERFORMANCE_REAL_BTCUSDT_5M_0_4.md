# Real BTCUSDT 5m Multi-Year Streaming Benchmark — v0.4

## Result

**PASS** — the Historical Market Data Ingestion component processed the complete real
BTCUSDT 5m dataset through typed normalization, streaming validation, monthly Parquet
publication, and checksum-verified typed reread without loading the full history into
Python memory.

## Evidence scope

- Component package: `market_data_ingestion` 0.4.0
- Input: `BTCUSDT_5m.parquet.bin`
- Input SHA-256:
  `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1`
- Input size: 34,020,301 bytes
- Coverage: 2019-09-08T17:55:00Z through 2026-07-19T06:55:00Z
- Rows: 721,597
- Source batches: 89 × up to 8,192 rows
- Monthly normalized partitions: 83
- Maximum records materialized for one month: 8,928

## Pipeline exercised

1. Read the external Parquet file in bounded Arrow batches.
2. Convert every source row to a strict `KlineRecord`.
3. Write acquired records to the SQLite-backed `RecordSpool`.
4. Run `validate_sorted_records` as a streaming validator.
5. Write accepted records to a second spool stage.
6. Publish deterministic monthly Parquet partitions with Zstandard compression.
7. Reopen every partition through `LocalStorage.iter_partition_records`, which verifies
   the stored checksum before typed deserialization.
8. Recompute continuity and a deterministic typed-record digest across the complete reread.

## Validation evidence

- Validation status: `VALID`
- Rows checked: 721,597
- Validation issues: 0
- Gaps: 0
- Duplicates: 0
- Reread gap intervals: 0
- Reread duplicates: 0
- Reread out-of-order records: 0
- Typed reread SHA-256:
  `e6acf7a5335d5f73b6d3dc4b052dc1b91cd2338b98bab8130b19496f01a1343b`
- Unique partition checksums: 83
- Sum of partition rows: 721,597

## Memory evidence

- Starting RSS: 146.25 MiB
- Peak sampled RSS: 297.04 MiB
- Peak RSS increase: 150.79 MiB
- Peak Python heap: 12.48 MiB
- Peak Arrow allocation: 54.44 MiB
- Release threshold: peak RSS below 1 GiB

The maximum monthly materialization was only 8,928 records. Disk-backed spool growth did
not cause proportional RSS growth, demonstrating that history length is decoupled from
Python heap usage.

## Performance evidence

- Total wall time: 1,101.233 seconds
- End-to-end throughput: 655.263 rows/second
- Source decode and spool: 226.502 seconds
- Streaming validation: 444.886 seconds
- Monthly Parquet publication: 210.964 seconds
- Checksum-verified typed reread: 218.563 seconds

This benchmark favors strict evidence and deterministic typed contracts over maximum raw
scan speed. The measured throughput includes two SQLite spool stages, validation, Parquet
writes, checksum verification, and complete typed reread.

## Environment

- Python: 3.13.5
- PyArrow: 25.0.0
- Platform: `Linux-6.12.13-x86_64-with-glibc2.41`
- Completed: 2026-07-27T18:34:27.680810Z

## Gate

All required criteria passed:

- all 721,597 rows processed through every stage;
- strict validation returned `VALID`;
- zero gaps, duplicates, and ordering errors;
- monthly bounded materialization remained below 10,000 records;
- peak RSS remained below 1 GiB.

The benchmark closes the multi-year real-data bounded-memory acceptance gate for this
component version. It does not authorize merging, release publication, deployment, or live
trading.
