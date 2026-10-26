# Benchmark Report — FVG 0.2.1

## Environment

- Python 3.13.5;
- Linux x86_64 local container;
- Modular Strategy Core 0.4.0;
- FVG 0.2.1;
- completed-bar `MarketView`;
- raw evidence: `artifacts/benchmark_results.json`.

All short comparisons use at least five runs and report the median. Large workloads use a bounded rolling three-bar input so the benchmark harness does not create artificial O(history) memory growth.

## Hot-path comparison

Equivalent workload: 5,000 bars, 15,000 `on_bar` calls, two exact duplicate calls per bar, full-history comparison harness.

| Version | Runs | Median seconds | Median calls/s | Median peak allocation |
|---|---:|---:|---:|---:|
| Local RC 0.2.0 | 5 | 2.763726 | 5,427.45 | 845,571 B |
| Hardened 0.2.1 | 5 | 2.880441 | 5,207.54 | 846,322 B |

Throughput change: **−4.052%**, inside the established 5% non-regression gate. The additional work verifies retained A/B source history and full OHLCV duplicate identity.

An initial correct but unoptimized implementation measured roughly 2,967 calls/s and was rejected. cProfile identified repeated cryptographic revalidation. The final implementation preserves cryptographic fallback for replacement/custom candles while using bounded object-identity evidence for immutable Core `Candle` objects.

## Scale results

| Workload | Runs | Median time | Median throughput | Checkpoint |
|---|---:|---:|---:|---:|
| Smoke 1,000 events | 3 | 0.086619 s | 11,544.84 events/s | 668 B |
| Standard 10,000 events | 3 | 0.909065 s | 11,000.31 events/s | 668 B |
| 10,000 events + 2 duplicates/event | 3 | 1.078376 s | 27,819.61 calls/s | 668 B |
| CI extended 50,000 events | 1 | 4.489827 s | 11,136.29 events/s | 668 B |
| Extended 250,000 events | 1 | 22.686738 s | 11,019.65 events/s | 668 B |

The rolling workload remains approximately flat from 1,000 through 250,000 events, supporting amortized O(1) processing with bounded module state.

## Memory

A 10,000-event rolling workload under `tracemalloc` measured:

- peak Python allocation: **7,637 bytes**;
- final checkpoint: **668 bytes**;
- active zones: 0.

The comparison harness intentionally retains full history and therefore measured a median process allocation of 846,322 bytes. That number includes benchmark-owned history and is not the module's bounded-state footprint.

## Restart and restore

10,000-event deterministic workload with checkpoint at event 5,000:

- uninterrupted digest: `7e3a301243cdeea6c5b9efdf7d3cb05c841dc017af8b0dd7bd41936620de80a9`;
- restarted digest: `7e3a301243cdeea6c5b9efdf7d3cb05c841dc017af8b0dd7bd41936620de80a9`;
- digests match: **true**;
- checkpoint at split: 1,343 bytes;
- measured restore latency: 216,961 ns.

Restore distribution over 250 iterations:

- median: 88,298 ns;
- minimum: 85,477 ns;
- maximum: 134,298 ns;
- checkpoint: 1,005 bytes.

## Stress boundary

The completed representative maximum is 250,000 events. Earlier 500,000 and 1,000,000 single-call attempts exceeded the execution limit of the local tool and are explicitly **not** reported as passed. The self-hosted workflow runs a bounded deterministic checkpoint workload rather than consuming runner time on every push.

## Verdict

- correctness hardening retained;
- hot-path change within 5% gate;
- throughput remains stable through 250k events;
- memory and checkpoint state remain bounded;
- restart digest is identical.
