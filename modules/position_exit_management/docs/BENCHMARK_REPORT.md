# Benchmark Report — Position & Exit Management 0.2.1

## Environment

- Python: `3.13.5`
- Core: `0.4.0`
- Symbol/timeframe: `BTCUSDT 5m`
- Workload: deterministic synthetic completed-bar stream
- Multi-run result: median unless only one stress run is specified

## Final results

| Workload | Events | Runs | Median throughput | Peak traced memory | Maximum checkpoint | Median restore |
|---|---:|---:|---:|---:|---:|---:|
| Smoke no-op traced | 1,000 | 5 | 13,739.12/s | 7,996 B | 815 B | n/a |
| Standard no-op traced | 50,000 | 5 | 13,782.75/s | 13,040 B | 816 B | n/a |
| Standard active + restart | 50,000 | 5 | 44,514.80/s | untraced | 819 B | 0.115 ms |
| Status traced | 1,000 | 5 | 3,453.77/s | 59,681 B | 818 B | n/a |
| Extended active + restart | 250,000 | 2 | 50,212.81/s | untraced | 820 B | 0.086 ms |
| Stress no-op + restart | 1,000,000 | 1 | 70,933.57/s | untraced | 818 B | 0.104 ms |
| Flat cleanup, 4,096 orders | 200 | 3 | 265.68/s | untraced | 1813 B | n/a |

## Profile-guided optimization result

The bounded cleanup selection changed from a full `O(n log n)` candidate sort to deterministic `O(n log k)` selection, where `k=max_cancel_requests_per_bar`.

- measured before: approximately `254.2 events/s`;
- measured after: `265.68 events/s`;
- improvement: approximately `+4.5%` on the identical workload;
- final state digest remained `759f6d3e2deb93d3b5a526a1168324e40ba9db1533c389d2baab58b7529a9e0f`.

## Memory and state verdict

- maximum traced peak across recorded workloads: `59,681 bytes`;
- ordinary active/no-op checkpoint: approximately `0.8 KiB`;
- cleanup checkpoint with bounded retry state: `1813 bytes`;
- no state growth proportional to processed history was detected;
- no trading semantic was changed for speed;
- status-every-bar remains opt-in because auditability has an explicit performance cost.
