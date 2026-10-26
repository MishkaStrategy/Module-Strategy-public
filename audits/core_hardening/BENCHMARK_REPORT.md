# Core hardening benchmark report

## Environment

- Python 3.13.5 local hardening environment.
- Linux x86_64.
- Durations use multiple runs and the median.
- Results are evidence for this environment, not universal latency guarantees.

## Event-loop workload

20,000 completed 5m bars × 10 no-op modules = 200,000 module calls.

| Stage | Runs | Median | Throughput |
|---|---:|---:|---:|
| Baseline | 7 | 4.3977 s | ~45,478 calls/s |
| Candidate 0.4.1 | 5 | 2.2799 s | ~87,724 calls/s |

Observed speedup: **1.929×**; median elapsed reduction: **48.2%**.

The final median differs from the immediate round-22 median (2.3245 s) by about 1.9%; this is treated as normal run-to-run/environment variation, not a separate optimization claim.

## Long-history workload

30,000 completed 5m bars × 5 modules, automatic 15m/1h aggregation, three runs, module-visible base history capped at 512.

- Median: **2.9529 s**.
- Throughput: **50,798 module calls/s**.
- Peak RSS: **99,928 KiB**.
- Checkpoint size: **1,084 bytes** each run.
- Run identity repeated exactly: `run-57180b81c2a56502`.

Repeated load-gate measurements remained within normal environment variance; no separate performance claim is made beyond the recorded final median and deterministic identity.

## Scope and limitations

Module-visible history and HTF histories can be bounded. Batch input candles, complete journal evidence and equity history are still retained; therefore total process memory is not constant with dataset length. A streaming retention policy remains a separate future design decision.
