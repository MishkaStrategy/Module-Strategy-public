# Entry Model — Local Baseline Audit

## Scope and source

- Module: **Entry Model**
- Standalone source root: `src/modular_modules/entry_model`
- Module version: `0.2.0`
- Python used locally: CPython 3.13.5
- Python compatibility declared/static target: `>=3.12`
- Python 3.12 executable: not available in this container
- Core: locally retained `modular-strategy-core 0.4.0` wheel
- Module API: `1.0`
- Remote services were intentionally excluded from this local release process.

The transient source directory from an earlier session was not retained. The module was restored from the already established local contract/context and validated against the retained local Core wheel. The current directory is the baseline snapshot for this local release candidate.

## Public contract baseline

- Import: `modular_modules.entry_model`
- Class: `EntryModelModule`
- Entry point: `modular_strategy.modules:entry_model`
- Required feature: `setup.confirmed`, schema `1`
- Produced features: `entry_model.plan`, `entry_model.rejection`, schema `2`
- State schema: `2`
- Required timeframes: none; TTL uses the completed current-bar interval
- Entry kinds: market, limit confirmation, limit retracement, explicit limit, stop market, stop limit

## First broad restored test baseline

The first broad suite collected 165 cases: 139 passed and 26 failed. The failures exposed real contract defects plus over-constrained test fixtures. Real defects included inconsistent plain-JSON DTO/state representation, unsafe fingerprint failure on unsupported metadata, and anonymous malformed rejection identity collision. Validators were not weakened; invalid fixtures were corrected only where they violated the intended contract, and regression tests were retained.

## Baseline gate state

| Gate | Baseline state |
|---|---|
| Pytest | 139 passed, 26 failed in first broad restored suite |
| Coverage | deferred until suite stabilization |
| Ruff | import-order findings during benchmark creation |
| strict mypy | clean after initial source stabilization |
| compileall | no syntax failures |
| Wheel/clean install | deferred to release-gate stage |
| Benchmark/memory | quick evidence only; final workload deferred |
| Checkpoint | schema 2 existed; adversarial restore coverage incomplete |

## Baseline limitations

- A canonical producer for `setup.confirmed` is required.
- Entry Model does not perform protection, sizing, exchange rounding, execution or position management.
- One mutable instance is bound to one symbol/timeframe stream.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
