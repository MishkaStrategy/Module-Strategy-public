# Entry Model 0.2.0 — Local Release Report

## Verdict

`LOCAL RELEASE CANDIDATE — READY FOR CODEX INTEGRATION`

This is a module release candidate, not a complete strategy, production/live release or exchange execution implementation.

## Local source base

- Standalone package root: `module/` in the release bundle
- Core: locally retained 0.4.0 wheel
- Module API: 1.0
- Runtime: Python 3.13.5; static/declaration target Python 3.12+
- Remote repositories and remote CI are outside this local release process

## Public contract

- Input: `setup.confirmed` schema 1
- Outputs: `entry_model.plan`, `entry_model.rejection` schema 2
- Import: `modular_modules.entry_model`
- Entry point: `modular_strategy.modules:entry_model`
- State schema: 2

## Local work

- restored and verified the established Entry Model contract against the actual local Core wheel;
- implemented strict Decimal configuration and canonical identities;
- built a 316-case unit/integration/regression/property/adversarial suite;
- verified real Core registry, dependency planner, BacktestEngine, Feature Store lifetime and checkpoint helpers;
- completed three completeness rounds, 30 optimization rounds and 15 audits;
- profiled and load-tested through 250,000 planning events and 1,000,000 empty events;
- built wheel/sdist and passed clean-wheel smoke;
- prepared a self-contained Codex handoff bundle.

## Final gates

- Tests: 316 passed
- Coverage: 99% (gate 90%)
- Ruff: clean
- strict mypy: clean
- compileall: clean
- pip check: clean
- Clean wheel install/import/entry point/plan/checkpoint smoke: passed
- Deterministic restart digest: identical to uninterrupted 50,000-event workload
- Bounded state: 10,000 + 10,000 records at long-load completion

## Performance summary

- 50,000 plans: median 12.101039 s, 4,131.88/s
- 250,000 plans: median 61.914522 s, 4,037.82/s
- 1,000,000 empty events: median 11.951375 s, 83,672.38/s
- 50,000 plans with restart: median 12.160613 s, identical digest
- Peak tracemalloc: 17,072,481 bytes
- Max RSS: 74,596 KiB
- Checkpoint: 3,110,382 bytes
- Median restore: 0.052208 s

## Known integration risk

A canonical `setup.confirmed` producer/adapter is required. Strategy-specific confirmation keys are intentionally not parsed inside this generic module. No Core change is required.

## Runtime scope

Python 3.13.5 passed all local runtime gates. Python 3.12 runtime was unavailable; package metadata declares `>=3.12`, Ruff targets py312 and strict mypy uses Python 3.12 semantics.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
