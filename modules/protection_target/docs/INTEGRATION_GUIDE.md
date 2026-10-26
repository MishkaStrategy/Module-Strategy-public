# Integration Guide — Protection & Target 1.1.1

## Target

- repository: `mishkacher/Module-Strategy`;
- base: `integration/modular-strategy-v0.5.0`;
- component path: `modules/protection_target`;
- workflow destination: `.github/workflows/protection-target-ci.yml`;
- existing Draft PR before hardening: #12.

## Public contract

- import: `modular_strategy_protection_target:ProtectionTargetModule`;
- entry point group/name: `modular_strategy.modules / protection_target`;
- input: `entry_model.entry_plan`, schema `1`;
- outputs: `protection_target.plan`, `protection_target.rejected`;
- state schema: `2`;
- Core compatibility: `modular-strategy-core>=0.4,<0.5`;
- required timeframes: none;
- warm-up: none.

## Files

Copy the complete module tree into `modules/protection_target`. Copy the audited workflow from this source checkout's `.github/workflows/protection-target-ci.yml` to the repository workflow path.

Do not modify:

- Core public DTOs;
- `StrategyModule`;
- `ModuleManifest`;
- Feature Store;
- dependency planner;
- event ordering;
- neighboring module source.

No Core Change Request is required.

## Verification commands

```bash
python -m pip install -e '.[dev]'
python -m pip install -e './modules/protection_target[dev]' --no-deps
python -m compileall -q modules/protection_target/src modules/protection_target/tests modules/protection_target/benchmarks
ruff format --check modules/protection_target/src modules/protection_target/tests modules/protection_target/benchmarks
ruff check modules/protection_target/src modules/protection_target/tests modules/protection_target/benchmarks
mypy --strict modules/protection_target/src/modular_strategy_protection_target
coverage run --branch --source=modules/protection_target/src/modular_strategy_protection_target -m pytest -q modules/protection_target/tests
coverage report --fail-under=90
python -m build --wheel --sdist modules/protection_target
```

Then run the repository Core and cross-module integration suites. The self-hosted workflow matrix must pass on Python 3.12 and 3.13 before the Draft PR can be considered integration-ready.

## Migration

- 1.0.0 schema-1 checkpoints are accepted only with the exact legacy configuration fingerprint;
- migrated state is emitted as schema 2;
- the same historical candle cannot be safely replayed immediately after schema-1 migration because no event fingerprint existed; advance to the next completed candle;
- compatible 1.1.0 schema-2 checkpoints restore into 1.1.1 and are re-emitted with module version 1.1.1.

## Known boundaries

Tick-size rounding, position sizing, order creation, execution, reconciliation and open-position management remain owned by downstream modules/adapters.
