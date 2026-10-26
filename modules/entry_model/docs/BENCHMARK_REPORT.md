> Historical report; physical runner identifiers are pseudonymized in this export. This is not new-root acceptance.

# Entry Model — Self-Hosted Benchmark Report

## Evidence

- GitHub Actions run: `29835220983`.
- Performance artifact: `8498237319`.
- Audited code/evidence head: `d699e8c2f677b19ab744b5442d0e743a3dd1a179`.
- Generated: `2026-07-21T14:16:22+00:00` (artifact publication time).

## Environment

- Python: `3.13.14` (Clang 22.1.3).
- Platform: `macOS 27.0`, ARM64, Mach-O.
- Runner: `archived-darwin-runner`, labels `[self-hosted, macOS, ARM64]`.
- Module: `0.2.0`.
- State cap: 10,000 processed setup records + 10,000 source-feature records.
- Inputs: deterministic synthetic completed 5-minute bars and in-memory features.
- The module performs no network access, file market-data loading, exchange access or system-clock reads.

## Method

Short workloads use five runs; extended/stress workloads use three. Median is the primary value. Every repeated workload must produce one identical SHA-256 output digest. The restart workload snapshots and restores after 25,000 of 50,000 events. The 20,000-plan memory workload measures tracemalloc and process maximum RSS. Darwin `ru_maxrss` bytes are normalized to KiB before publication.

## Results

| Workload | Runs | Events | Median seconds | Median throughput | Plans | Checkpoint bytes |
|---|---:|---:|---:|---:|---:|---:|
| smoke-empty | 5 | 1,000 | 0.008448 | 118,367.71/s | 0 | 369 |
| smoke-plans | 5 | 1,000 | 0.158117 | 6,324.44/s | 1,000 | 307,160 |
| standard-plans | 5 | 50,000 | 8.038005 | 6,220.45/s | 50,000 | 3,110,382 |
| extended-plans | 3 | 250,000 | 40.811005 | 6,125.80/s | 250,000 | 3,130,384 |
| stress-empty | 3 | 1,000,000 | 8.429573 | 118,629.97/s | 0 | 372 |
| restart-standard | 3 | 50,000 | 8.165004 | 6,123.70/s | 50,000 | 3,110,382 |

## Determinism and restart

- `standard-plans` digest: `39a9293cdd14ba19007d349738ed02164891cbfb3a38b0d344537c4067446211`.
- `restart-standard` digest: `39a9293cdd14ba19007d349738ed02164891cbfb3a38b0d344537c4067446211`.
- Uninterrupted and restart digests are identical.
- `extended-plans` digest: `d3a5971648e73d2f800cc6c835c3585f59d701d55a340df4b1aafdfcabf78087`.
- The 250,000-event digest matches the prior independently verified release-candidate digest.
- Standard, extended and restart runs finish at the configured 10,000 + 10,000 state bound.

## Memory and restore

- Tracemalloc workload: 20,000 unique plans.
- Current tracked bytes: `45,743`.
- Peak tracked bytes: `17,074,795`.
- Normalized process maximum RSS: `68,704 KiB` (approximately 67.1 MiB).
- Checkpoint size: `3,110,382 bytes`.
- Restore runs: 20.
- Restore minimum: `0.061671 s`.
- Restore median: `0.063146 s`.
- Restore maximum: `0.070910 s`.

## Interpretation

Throughput remains stable between 50,000 and 250,000 unique planning events, with no O(history) degradation and bounded memory. Restart changes neither output identity nor final bounded state. The benchmark includes Candle/MarketView and feature construction, so the reported rate is an end-to-end module-path measurement rather than a formula-only microbenchmark.

The previous macOS artifact that labeled raw Darwin RSS bytes as KiB is superseded by this report and by regression tests for platform-aware RSS normalization.
