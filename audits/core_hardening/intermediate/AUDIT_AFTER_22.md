# Intermediate audit after rounds 21–22

## Decision
**Continue.** The measured event-loop optimizations preserve behavior and materially improve the profiled workload.

## Profile evidence
Workload: 20,000 closed 5m bars, 10 no-op modules, 200,000 module calls, Python 3.13.5. Seven independent runs were used for each median.

- Before median: 4.3977s.
- After median: 2.3245s.
- Elapsed reduction: 47.14%.
- Throughput multiplier: 1.89x.
- After range: 2.2854s–2.4098s.

## Changes
- Cached timeframe parsing.
- Reused base-series, timeframe mapping, session and identical readiness views per bar.
- Avoided copies of already immutable mappings.
- Skipped empty event-dispatch passes.
- Fast-pathed Feature Store snapshot cache hits.
- Preserved equity event schema with a lower-overhead serializer.

## Public contracts
No API signature, DTO, feature, event ordering, checkpoint or module callback changed.

## Correctness evidence
- Full suite: 182 tests collected and passed.
- Branch coverage: 83%.
- Test elapsed: 1.53s; max RSS: 53,144 KiB.
- Immediate module callbacks, immutable saved views and event payload compatibility are regression tested.

## No-look-ahead / determinism / replay
The saved `ReadOnlySeries` end boundary remains fixed. Same-bar feature/order visibility is preserved. Run identity and event ordering remain deterministic.

## Residual risks
The benchmark is CPU/memory-local and does not represent external I/O. No optimization claim is made for paper/live latency.
