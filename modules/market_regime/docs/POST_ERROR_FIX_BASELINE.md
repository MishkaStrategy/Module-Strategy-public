# Market Regime Post-Error-Fix Baseline

Date: 2026-07-21

## Identity

- component: Market Regime;
- package/import: `modular-strategy-market-regime` / `market_regime`;
- component version: 1.2.0;
- Core version: 0.4.0;
- Module API major: 1;
- feature schema: 1;
- state schema: 2, with schema-1 migration;
- remote integration base: `integration/modular-strategy-v0.5.0`;
- remote base commit: `5e03b933207a99940244634f49a756d27888a5c3`;
- remote pre-resumption head: `6117c22640d2d6944ee10c45ca3870298f4a81a9`;
- restored local source commit: `00bcae42cae37693169a9ea383a088da6b92785b`;
- post-error-fix code commit: `c6abb45`;
- local branch: `local/market-regime-1.2.0-rc-restored`.

The final remote commit is intentionally not recorded yet because the local baseline has not been published at this point in the workflow.

## Correctness and quality gates

| Gate | Result |
|---|---|
| pytest | 115 passed |
| statement coverage | 91% |
| coverage gate | 90%, PASS |
| Ruff lint | PASS |
| Ruff format check | PASS |
| strict mypy | PASS, 9 source files |
| compileall | PASS |
| Bandit | PASS, zero findings |
| detect-secrets | PASS, zero findings |
| Python 3.13 | PASS, 3.13.5 |
| Python 3.12 | NOT RUN locally; requires CI |
| full repository suite | NOT RUN locally; full checkout unavailable |

The test suite includes configuration, indicator mathematics, temporal/no-look-ahead behavior, state migration and tamper rejection, duplicates and ordering, gap reset, deterministic restart, public contracts, registry/Feature Store integration, packaging and production-source security regressions.

## Packaging baseline

Fresh artifacts built from the post-fix source:

- wheel: `modular_strategy_market_regime-1.2.0-py3-none-any.whl`;
- wheel SHA-256: `94844247cf010d89321f7c6bfb63eb41cf351af0a98d57812779b367f80dd8b5`;
- sdist: `modular_strategy_market_regime-1.2.0.tar.gz`;
- sdist SHA-256: `bf36c9c791c74a4ee443b656eff7441f97f1103303b61063a74522f3bd40e5e8`;
- Twine metadata: PASS;
- sdist support files and checkpoint fixture: present;
- extracted-sdist tests: PASS;
- clean wheel install with Core 0.4.0 and PyYAML 6.0.3: PASS;
- `pip check`: PASS;
- public import: PASS;
- entry-point discovery: PASS;
- ModuleRegistry load: PASS;
- checkpoint/restore smoke: PASS;
- PEP 561 `py.typed`: included.

## Deterministic performance baseline

Environment: Python 3.13.5, current container, single process.

### Throughput, 20,000 events × 2

- median elapsed: 1.8081344255 s;
- median throughput: 11,061.14 events/s;
- maximum checkpoint: 3,438 bytes;
- median verification restore: 0.207 ms;
- output digest: `57533fc7754f557d4365c056371b272bf86f9a861bf4a2b4ed7ac87f229f0b3d`;
- state digest: `6b47fd668dfb4ebe674217146497f079bb668038ad155ae86deccee64146aff0`;
- deterministic outputs/states: true.

### Mid-stream restart, 20,000 events at 10,000

- elapsed: 1.8767969220 s;
- throughput: 10,656.45 events/s;
- checkpoint: 3,438 bytes;
- combined restore time: 0.385 ms;
- output and final-state digests match the continuous stream.

### Traced allocation, 10,000 events

- elapsed under tracing: 4.3164521160 s;
- peak traced allocation: 62,860 bytes;
- checkpoint: 3,438 bytes;
- verification restore: 0.223 ms;
- deterministic outputs/states: true.

These measurements are local engineering baselines, not latency guarantees for another machine.

## Security and dependency baseline

- no production `assert` dependency;
- Bandit: zero findings;
- detect-secrets: zero findings;
- dependency set: Core 0.4.x and its PyYAML dependency;
- `pip check`: clean;
- vulnerability database audit: NOT RUN because the external vulnerability service was unreachable.

## CI baseline

The previous remote head has failed workflow runs that terminated before steps were exposed. No GitHub CI claim is made for `c6abb45` until it is safely published and a new run executes.

Actual self-hosted runner labels have not been retrievable from this environment. Workflow changes must not guess labels.

## Baseline verdict

The local component baseline is clean for all locally executable gates. It is suitable as the reference point for completeness review and resumption hardening, but is not yet integration-ready because repository-wide Python 3.12/3.13 CI and self-hosted execution remain unproven.
