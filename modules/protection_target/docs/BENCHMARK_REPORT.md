# Benchmark Report — Protection & Target 1.1.1

## Environment and method

- CPython 3.13.5;
- Modular Strategy Core 0.4.0 / Module API 1.0;
- short workload: 2,000 unique alternating BUY/SELL EntryPlans;
- five independent runs with `tracemalloc`;
- primary metric: median, never best run;
- stress workloads use checkpoint/restore at every segment boundary;
- all measurements are local and are repeated by self-hosted CI after publication.

## Short benchmark

| Metric | Remote baseline 1.0.0 | Final 1.1.1 | Change |
|---|---:|---:|---:|
| Median seconds | 1.226193 | 1.091572 | **-10.98%** |
| Throughput | 1,631.06 plans/s | 1,832.22 plans/s | **+12.33%** |
| Peak traced memory | 0.573992 MiB | 0.574074 MiB | +0.01% |
| Checkpoint | 235,377 B | 235,551 B | +0.07% |
| Restore median | 2.994 ms | 5.266 ms | +75.89% |

Restore latency increased because schema 2 verifies a complete SHA-256 state digest and performs stricter semantic validation. The absolute latency remains approximately five milliseconds for 2,000 retained plans. Trading semantics were not changed for performance.

Against the immediate post-error baseline of 1,805.19 plans/s, the final median is approximately 1.50% faster and remains within the no-regression gate.

## Restart loads

| Workload | Aggregate throughput | Max checkpoint | Median restore | Final state |
|---|---:|---:|---:|---:|
| 50,000 | 8,327.41 events/s | 488,087 B | 10.123 ms | 4,096 |
| 300,000 | 7,779.55 events/s | 492,185 B | 9.881 ms | 4,096 |
| 1,000,000 | 7,832.76 events/s | 492,187 B | 10.373 ms | 4,096 |

Million-event output digest:

```text
e9c981490aeada907304cabf8b54195e8c7a6c1b136eb4f97d40641348bce73c
```

Two independent 50,000-event runs produced identical output digests, signed state digests and checkpoint sizes.

## Complexity verdict

- per-event processing is independent of historical candle count;
- persistent dedupe state is capped by `max_processed_plans`;
- diagnostics are capped by `max_diagnostic_results`;
- counters are capped at signed 64-bit range;
- checkpoint size stabilizes after state saturation;
- cProfile found expected Feature construction, hashing and Core validation costs, with no hidden O(history) loop.
