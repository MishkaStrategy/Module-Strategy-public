# Entry Model — Post-Error-Fix Baseline

Date: 2026-07-21

This baseline records the independently reproduced state after Error Discovery fixes and the successful self-hosted release-gate run. It is the comparison point for future Entry Model changes.

| Field | Baseline |
|---|---|
| Repository | `mishkacher/Module-Strategy` |
| Remote integration base | `integration/modular-strategy-v0.5.0` |
| Base commit | `5e03b933207a99940244634f49a756d27888a5c3` |
| Prior remote component commit | `ca2d2d2437b238f3b2a57be7d03f1f6df0e5013d` |
| Working branch | `modules/entry-model-hardening` |
| Audited code/evidence head | `d699e8c2f677b19ab744b5442d0e743a3dd1a179` |
| Component version | `0.2.0` |
| Core | `0.4.0` |
| Module API | `1.0` |
| Plan/rejection schema | `2` |
| State schema | `2` |
| Local Python | `3.13.5` |
| Self-hosted Python | `3.12.13`, `3.13.14` |
| Local tests | `324 passed`; `324 passed` under `python -O` |
| Self-hosted tests | Python 3.12: `324/0/0/0`; Python 3.13: `324/0/0/0` (tests/failures/errors/skipped) |
| Coverage | 950/962 lines = `98.75%`; 347/352 branches = `98.58%`; rounded report `99%` |
| Ruff check / format | PASS / PASS on Python 3.12 and 3.13 |
| strict mypy | PASS, 11 source files |
| compileall / shell syntax | PASS / PASS |
| Bandit | PASS |
| detect-secrets | PASS, zero candidates |
| pip-audit | PASS, 69 dependencies, zero known vulnerabilities |
| SBOM | CycloneDX 1.6, 71 components |
| wheel / sdist / Twine | PASS / PASS / PASS |
| extracted sdist verification | PASS; resolver, test helpers and PEP 561 marker present |
| clean wheel-only install | PASS with real Core 0.4.0 |
| public import / entry point | PASS / exactly `entry_model` |
| checkpoint/restart | PASS |
| no-look-ahead / future invariance | PASS through full and optimized-runtime suites |
| 50k plans | median `8.038005 s`, `6,220.45 plans/s` |
| 50k restart at 25k | median `8.165004 s`, digest identical to uninterrupted |
| 250k plans | median `40.811005 s`, `6,125.80 plans/s` |
| 1m empty events | median `8.429573 s`, `118,629.97 events/s` |
| traced peak, 20k plans | `17,074,795 bytes` |
| normalized max RSS | `68,704 KiB` on macOS ARM64 |
| checkpoint size at bound | `3,110,382 bytes` (`3,130,384` for late six-digit setup IDs) |
| restore latency | median `0.063146 s`, 20 runs |
| Wheel SHA-256 | `aceeae51d08c9bdebbbec5ec593c7f025891bf32965f726cd9b252d5e9195e0c` |
| Sdist SHA-256 | `509dab624a58d7fa9f223813a21c3e683d4b14e1a2c25037493c441ab4d082ab` |
| Self-hosted CI | `entry-model-ci` run `29835220983`: all seven jobs PASS |
| Test artifacts | `8497521917` (3.12), `8497852230` (3.13) |
| Security artifact | `8497550551` |
| Distribution artifact | `8498041650` |
| Performance artifact | `8498237319` |

No Core or neighboring module source was changed. The module still consumes only canonical `setup.confirmed` and publishes deterministic `entry_model.plan` or `entry_model.rejection` outputs.
