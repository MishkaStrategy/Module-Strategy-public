# Streaming performance report — package 0.3.0

## Environment

- Python: 3.13.5
- Platform: Linux 6.12.13 x86_64
- Storage format: CSV
- Adapter: deterministic Fake USD-M
- Required stream: BTCUSDT 5m klines
- Derived streams: 15m and 1h
- Concurrency: 1
- Chunk/memory bound: 1,000 records
- Integrity verification: enabled after registration
- Measurement: `tracemalloc` for Python allocations and process peak RSS from `resource`

## Result

| Metric | Value |
|---|---:|
| Requested/registered base rows | 20,000 / 20,000 |
| Adapter chunks | 20 |
| Ingestion wall time | 32.159945 s |
| Base-row throughput | 621.892 rows/s |
| Peak traced Python allocation | 3,115,837 bytes |
| Process peak RSS | 125,583,360 bytes |
| Registered partitions | 11 |
| Raw artifacts | 22 |
| Raw layer | 2,035,338 bytes |
| Normalized layer | 2,960,516 bytes |
| Derived layer | 1,218,470 bytes |
| Manifest layer | 26,707 bytes |
| Total measured storage + registry | 6,265,607 bytes |
| Deep integrity result | `ok` |
| Deep integrity time | 0.005716 s |
| Staging files after completion | 0 |

## Interpretation

The total row count is twenty times the configured in-memory chunk bound, while peak
traced Python allocations remained about 3.12 MB. This is evidence that the CSV pipeline
no longer accumulates the complete dataset in Python containers. It is not a universal
memory guarantee: the adapter still materializes one source chunk, SQLite and the Python
runtime contribute native memory, and RSS includes the entire interpreter process.

The result is a deterministic fake-workload benchmark, not Binance network throughput.
Network latency, archive decompression, filesystem type and PyArrow will materially change
production performance.

Machine-readable evidence: `reports/benchmark-streaming-20000.json`.
