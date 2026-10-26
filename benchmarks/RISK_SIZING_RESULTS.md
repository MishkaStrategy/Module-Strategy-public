# Risk sizing benchmark results

Environment: Python 3.13.5, Linux x86_64, module 1.1.0. Short workloads use at least five runs and report the median. Stress runs use one deterministic run or checkpointed chunks where appropriate.

## Pure calculator

| Events | Runs | Min s | Median s | Max s | Median throughput/s |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 5 | 0.00918 | 0.00925 | 0.00926 | 108,117 |
| 50,000 | 5 | 0.44611 | 0.45120 | 0.45981 | 110,816 |
| 100,000 | 5 | 0.89545 | 0.90267 | 0.93643 | 110,782 |
| 250,000 | 5 | 2.26366 | 2.43727 | 2.64097 | 102,574 |
| 1,000,000 | 1 | 9.36867 | 9.36867 | 9.36867 | 106,739 |

All runs produced digest `74f07e994ada159b6e35920774ad4969ea0997f8c2bb617c019f7a27c6b96aad`.

Recorded 1.0.0 baseline: 100,000 calculations in 1.009 seconds, approximately 99,108/s, one run. The 1.1.0 five-run median is approximately 10.5% faster while applying stricter correctness checks.

## Full Module API path

This workload constructs real core `Candle`, `Feature`, `FeatureStore`, `FeatureView` and `MarketView` objects and executes `RiskSizingModule.on_bar`.

| Events | Runs | Min s | Median s | Max s | Median throughput/s |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 5 | 0.20650 | 0.21408 | 0.22474 | 4,671 |
| 50,000 | 3 | 10.05456 | 10.16082 | 10.65943 | 4,921 |

Stable throughput provides no evidence of O(history) processing.

## Extended restart workload

250,000 full events were processed as five 50,000-event chunks, restoring the previous JSON checkpoint before each chunk. The sequence was repeated and both final checkpoints were byte-identical:

```text
SHA-256 186f36d0813ec65750c1ad954fd67e13f82db4875e0bb89c6c51b38aa42ce154
received=250000
approved=250000
cached requests=2048
checkpoint bytes=500089
```

A one-million-event full Module API process was not used because the execution environment imposes a single-command runtime ceiling. The one-million-event class was completed for the pure hot path; the full path used the largest reproducible checkpointed workload under that ceiling.

## Memory and checkpoint

`tracemalloc`, 10,000 full events:

- current traced bytes: 254,993;
- peak traced bytes: 2,291,279;
- checkpoint cache remains bounded at 2,048 request and event fingerprints.

Checkpoint after 5,000 events:

- cached entries: 2,048;
- JSON size: 491,892 bytes;
- restore runs: 5;
- minimum: 0.913 ms;
- median: 1.088 ms;
- maximum: 1.712 ms.

## Profiling result

`cProfile` identified repeated Decimal context calculation and output validation as the measured hot spots. The final implementation caches immutable request precision, price geometry and step ratios; uses one-pass capacity selection; and uses a narrow internal constructor for already-derived decisions while preserving strict public validation. No risk semantics were relaxed for speed.
