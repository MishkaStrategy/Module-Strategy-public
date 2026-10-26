# Benchmark Report

Environment for saved local evidence: Python 3.13.5, Core 0.4.1. Median is used for repeated short benchmarks.

## Results

| Workload | Events | Runs | Result |
|---|---:|---:|---:|
| full plan creation | 1,000 | 5 | ~21.5k plans/s |
| full plan creation | 10,000 | 5 | ~21.4k plans/s |
| full plan creation | 50,000 | 3 | ~21.5k plans/s |
| RiskRequest creation | 10,000 | 5 | ~177k requests/s |
| no-trigger early rejection | 50,000 | 5 | ~669k events/s |
| no-trigger early rejection | 250,000 | 3 | ~679k events/s |
| no-trigger early rejection | 1,000,000 | 1 | ~707k events/s |
| bounded state | 50,000 | 1 | ~3.24M events/s |
| restart state load | 250,000 / 5 chunks | 1 | ~2.38M events/s |

## Memory and state

- full-plan traced peak: 5,820 bytes;
- idle checkpoint: 897 bytes;
- bounded-state checkpoint: 37,311 bytes in standard load;
- saturated restart checkpoint: 39,616 bytes;
- idle median restore: ~0.041 ms;
- saturated median restore: ~2.29 ms;
- processed-event cache cap: 2,048 in load harness;
- history cap: 256 in extended load.

All repeated workloads use deterministic digests. The one-million-event test targets the representative O(1) early-rejection path; one million cryptographic full-plan constructions was not used as a release gate because 50,000 full plans already represents the expensive path without wasting runner time.

Profiling showed plan DTO validation/canonicalization and SHA-256 identity creation as the dominant work. The accepted optimization removed duplicate hashing without reducing Decimal precision, validation, auditability or determinism.
