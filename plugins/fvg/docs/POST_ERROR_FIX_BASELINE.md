# Post-Error-Fix Baseline — FVG 0.2.1

This baseline was established after completing `ERROR_DISCOVERY_AUDIT.md` and before the subsequent completeness/hardening rounds.

| Item | Result |
|---|---|
| Remote base | `integration/modular-strategy-v0.5.0` |
| Remote base commit | `5e03b933207a99940244634f49a756d27888a5c3` |
| Preserved local RC commit | `452a7e319b2eecf923e7666c9306c6c093c7a3f4` |
| Error-fix commits | `0a3a85e`, `dafe80b`, `0f25800` plus hot-path follow-up pending documentation commit |
| Module | `modular-strategy-fvg==0.2.1` |
| Core | `modular-strategy-core==0.4.0` |
| Module API | v1 draft revision 3 |
| State schema | 3; reads schemas 1/2/3 |
| Feature schema | 1 |
| Python | 3.13.5 local runtime; metadata/mypy target 3.12 |
| Tests | 207 passed |
| Coverage | 96% branch-aware; gate 95% |
| Ruff | PASS |
| strict mypy | PASS |
| compileall | PASS |
| wheel/sdist | PASS |
| PEP 561 marker | PASS |
| clean install | PASS |
| `pip check` | PASS |
| public import / entry point | PASS |
| checkpoint/restore | PASS |
| no-look-ahead / replay | PASS |
| Typical active-zone checkpoint | 1,001 bytes in the five-run functional probe |
| One measured restore | 189,393 ns in the five-run functional probe environment |
| Peak Python allocation | median 846,322 bytes in the 5k/15k full-history comparison harness |
| Hot-path median | 5,207.54 calls/s for 5,000 bars and 15,000 calls |
| Comparison to local RC 0.2.0 | −4.05%, inside the 5% gate |
| GitHub CI | Pending for current hardening branch |

The comparison harness intentionally retains full history and includes two exact duplicate calls per bar. A separate bounded rolling-history benchmark is used for standard/extended workload and memory evidence.
