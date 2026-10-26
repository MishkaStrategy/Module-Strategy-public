# Codex Integration — Market Regime 1.2.0

## Repository target

- repository: `mishkacher/Module-Strategy`;
- base branch: `integration/modular-strategy-v0.5.0`;
- working branch: `modules/market-regime-hardening`;
- Draft PR: `#29`;
- module path: `modules/market_regime`.

## Integration boundaries

Integrate only the Market Regime component and its dedicated workflow. Do not modify Core or unrelated modules to simplify integration.

Allowed paths in this PR:

- `.github/workflows/market-regime.yml`;
- `modules/market_regime/**`.

Public import and entry point:

```python
from market_regime import MarketRegimeModule
```

`modular_strategy.modules / market_regime`

## Required verification order

1. Check out the exact Draft PR head.
2. Install the repository development dependencies.
3. Install `modules/market_regime` in editable mode without replacing Core.
4. Run compileall, Ruff lint/format and strict mypy.
5. Run the complete Market Regime suite and coverage gate.
6. Run Core and all integrated-module suites.
7. Build Core and Market Regime wheels and the Market Regime sdist.
8. Install only the built wheels into a clean environment.
9. Verify `pip check`, public import, entry point and Core registry loading.
10. Verify schema-1 migration, checkpoint/restore, deterministic replay and future-data rejection.
11. Run the checked-in benchmark workloads and compare output/state digests.
12. Review the PR diff for unrelated changes.

## Commands

From the repository root:

```bash
python -m pip install -U pip
python -m pip install -e '.[dev]'
python -m pip install -e './modules/market_regime[dev]' --no-deps
python -m pip check
python -m compileall -q modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks
ruff check modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks modules/market_regime/examples
ruff format --check modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks modules/market_regime/examples
(cd modules/market_regime && mypy --strict src/market_regime)
coverage run --source=market_regime -m pytest modules/market_regime/tests
coverage report --fail-under=90
python -m build modules/market_regime
```

Then run the root integration suites and the heavy jobs defined in `.github/workflows/market-regime.yml`.

## Contract invariants

- feature keys remain `market_regime.current`, `market_regime.trend`, and `market_regime.volatility`;
- feature schema remains 1;
- state schema remains 2 with schema-1 migration;
- no future target bar may produce a feature;
- rejected duplicate/out-of-order target data must not mutate the checkpoint;
- restart continuation must preserve exact feature and final-state digests;
- checkpoint size must remain bounded;
- diagnostics configuration must not affect semantic output;
- no orders, signals, setups or portfolio mutations may be introduced.

## CI policy

Use the self-hosted workflow as written. Do not automatically replace it with GitHub-hosted runners when the runner is offline. The workflow validates macOS/ARM64 at runtime and keeps heavy jobs behind `workflow_dispatch`.

## Completion rule

Keep PR #29 Draft until the exact remote head has green Python 3.12/3.13 fast jobs, full repository validation, packaging/security and performance evidence. Do not merge, tag or publish a release without explicit user approval.
