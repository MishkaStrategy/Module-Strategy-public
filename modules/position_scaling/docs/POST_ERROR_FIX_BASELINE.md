# Post-Error-Fix Baseline

- component: Position Scaling & Adjustment;
- version: 0.2.2 hardening candidate;
- remote integration base: `5e03b933207a99940244634f49a756d27888a5c3`;
- local verified source commit before publication: `b12878d78d45be89168574920381bbcd209c8e96`;
- working branch: `modules/position-scaling-hardening`;
- Core: 0.4.1;
- Module API: 1.0;
- state schema: 4;
- public package: `modular_strategy_position_scaling`;
- entry points: `position_scaling_planner`, `position_scaling_approval`.

## Quality baseline

| Gate | Result |
|---|---|
| pytest | 228 passed |
| branch coverage | 90%, gate 90% |
| Ruff | passed |
| Ruff format | passed |
| strict mypy | passed, 9 source files |
| compileall | passed |
| wheel | built |
| sdist | built |
| clean wheel install | passed with Core 0.4.1 |
| pip check | passed in clean venv |
| public imports | passed |
| entry-point discovery | passed |
| checkpoint/restore | passed |
| deterministic replay | passed |
| real-Core BacktestEngine smoke | passed |
| Bandit | passed |
| detect-secrets | zero findings |
| local pip-audit | not completed: PyPI DNS unavailable |

## Performance baseline

- 50,000 full plan creations: about 21.5k plans/s after profiling-led optimization;
- 250,000 bounded-state events with five restores: about 2.38M events/s;
- 1,000,000 representative O(1) early-rejection events: about 0.71M events/s in the latest saved run;
- traced full-plan peak: 5,820 bytes;
- idle checkpoint: 897 bytes;
- saturated checkpoint: 39,616 bytes;
- saturated median restore: about 2.29 ms.

The baseline is local evidence. GitHub CI status is recorded separately and must never be inferred from this document.
