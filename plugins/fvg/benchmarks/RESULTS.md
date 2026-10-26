# FVG performance results

Environment: current ChatGPT Python 3.13 container, 20.07.2026.

## Tail-processing optimization

Synthetic flat 15m candles, one new candle per call:

| Bars | Before | After |
|---:|---:|---:|
| 2,000 | 11.862 s | 0.124 s |

Speed-up: approximately **95.7x**.

Root cause: full-history `new_indexes` scan and a second full-history future-candle scan on every event.

Fix:

- backward tail cursor from `last_processed`;
- latest-bar future check plus checks only on processed tail;
- duplicate/no-data fast path.

## Current workload

```text
bars=20000 calls=60000 elapsed_seconds=2.060801
calls_per_second=29114.90
```

The benchmark is deterministic in workload but wall-clock results depend on hardware and Python build.
