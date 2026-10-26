# Benchmark Report — Session Range 0.2.0 Post-Error-Fix Candidate

## Method

- CPython 3.13.5; Core 0.4.0.
- Baseline 0.1.0 loaded from the integrated wheel; candidate 0.2.0 loaded from source.
- Synthetic completed BTCUSDT 5-minute candles; UTC 00:00–04:00 session.
- Timing excludes process startup and input history retention.
- Standard comparisons alternate baseline and candidate processes to reduce environmental bias.
- Median is authoritative for repeated workloads.
- Memory, extended and stress workloads are isolated so instrumentation or sustained CPU throttling cannot contaminate subsequent measurements.

## Interleaved baseline comparison

| Events | Runs | Baseline median | Candidate median | Baseline throughput | Candidate throughput | Change |
|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 5 each | 0.247072 s | 0.237157 s | 40,474/s | 42,166/s | **+4.18%** |
| 50,000 | 5 each | 1.222998 s | 1.171879 s | 40,883/s | 42,667/s | **+4.36%** |

The candidate no longer has a >5% hot-path regression. The improvement followed removal of duplicate validation for immutable Core `Candle` DTOs; strict validation remains on checkpoint restore.

## Reproducible candidate profiles

| Profile | Events | Runs | Median | Throughput | Checkpoint | Restore |
|---|---:|---:|---:|---:|---:|---:|
| smoke | 1,000 | 5 | 0.023142 s | 43,211/s | 1,144 B | 76.1 µs |
| standard | 10,000 | 5 | 0.221965 s | 45,052/s | 1,145 B | 74.7 µs |
| standard | 50,000 | 5 | 1.103076 s | 45,328/s | 1,158 B | 84.2 µs |
| extended | 250,000 | 3 | 5.685015 s | 43,975/s | 1,466 B | 144.9 µs |
| stress | 1,000,000 | 1 | 23.695705 s | 42,202/s | 1,157 B | 108.8 µs |

## Memory

Five isolated traced 10,000-event runs:

- baseline peak median: 62,927 bytes (61,527–65,932);
- candidate peak median: 63,276 bytes (61,111–69,906);
- difference: +349 bytes / +0.55%.

Memory and checkpoint size remain bounded; no event history is retained by the module.

## Commands

```bash
PYTHONPATH=modules/session_range/src python modules/session_range/benchmarks/session_range_benchmark.py --output standard.json
PYTHONPATH=modules/session_range/src python modules/session_range/benchmarks/session_range_benchmark.py --events 250000 --runs 3 --output extended.json
PYTHONPATH=modules/session_range/src python modules/session_range/benchmarks/session_range_benchmark.py --events 1000000 --runs 1 --output stress.json
```

Raw evidence: `artifacts/benchmark_results.json`.
