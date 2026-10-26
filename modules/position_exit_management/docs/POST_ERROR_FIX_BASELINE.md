# Post-Error-Fix Baseline — Position & Exit Management 0.2.1

## Identity

| Field | Value |
|---|---|
| Remote base | `integration/modular-strategy-v0.5.0` |
| Base commit | `5e03b933207a99940244634f49a756d27888a5c3` |
| Previous remote head | `33777705bfac9ce6990c3ef6b96f23d6782c996f` |
| Local branch | `modules/position-exit-management-hardening` |
| Module version | `0.2.1` |
| Core versions | `0.4.0`, `0.4.1` |
| Module API | `1.0` |
| State schema | `2` |
| Feature schema | `2` |
| Python | `3.13.5` local; 3.12 pending CI |

## Quality gates

| Gate | Baseline result |
|---|---|
| Module tests | 183 passed |
| Repository suite | 244 passed |
| Core 0.4.0 clean-wheel matrix | 183 passed |
| Core 0.4.1 clean-wheel matrix | 183 passed |
| Combined branch-aware coverage | 92.066% (gate 92%) |
| Statement coverage | 93.978% |
| Branch coverage | 87.685% |
| Ruff format | PASS |
| Ruff lint | PASS |
| strict mypy | PASS, 6 source files |
| Bandit | PASS |
| compileall | PASS |
| detect-secrets | no detected secret material in module tree |
| Wheel build | PASS |
| Source distribution | PASS; reproducibility members verified |
| Clean pip check | PASS on Core 0.4.0 and 0.4.1 environments |
| Public import | PASS |
| Entry point | PASS |
| ModuleRegistry | PASS |
| Empty and active checkpoint/restore | PASS |
| Local pip-audit | NOT RUN TO COMPLETION — external DNS unavailable |

## Performance baseline

CPython 3.13.5, Core 0.4.0, BTCUSDT 5m synthetic deterministic workload.

| Workload | Events | Runs | Median throughput | Peak traced memory | Checkpoint | Median restore |
|---|---:|---:|---:|---:|---:|---:|
| Smoke no-op traced | 1,000 | 5 | 13,942.56 events/s | 10,996 B | 815 B | n/a |
| Standard no-op traced | 50,000 | 5 | 13,981.28 events/s | 13,790 B | 816 B | n/a |
| Standard active + restart | 50,000 | 5 | 44,146.69 events/s | untraced | 819 B | 0.068 ms |
| Active + restart traced | 10,000 | 5 | 7,577.90 events/s | 19,311 B | 819 B | 0.256 ms |
| Every-bar status traced | 10,000 | 5 | 3,438.20 events/s | 60,923 B | 819 B | n/a |

The prior local RC standard traced no-op result was 13,772 events/s. The hardened baseline is approximately 1.5% faster while performing stricter input validation and stronger duplicate identity.

## State and causality

- Event fingerprint: fixed 64-character SHA-256 for new events.
- Checkpoint state is independent of event history.
- Cancel attempts are bounded by configuration and identifier length.
- Exact duplicate, conflicting duplicate and out-of-order events are distinct.
- Gap candles cannot modify trailing extrema when price actions are suspended.
- Future position input is rejected before active-state creation.
- Replay after restore is deterministic.

## CI state at baseline

- Existing Draft PR #20 remained on old head `33777705...` at this point.
- Exact-head historical runs on that old head failed before job steps started.
- No self-hosted workflow or confirmed runner labels were discoverable from repository files or the available connector API.
- No new CI result is claimed by this local baseline.
