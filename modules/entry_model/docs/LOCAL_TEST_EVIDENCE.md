# Entry Model — Local Test Evidence

## Environment

- Local runtime: CPython 3.13.5
- Declared compatibility: Python >=3.12
- Python 3.12 executable in this container: not available
- Core: modular-strategy-core 0.4.0 local wheel
- Module: modular-strategy-entry-model 0.2.0
- Module API: 1.0

## Final source-tree gates

### Compile

`python -m compileall -q src tests examples scripts benchmarks`

Result: PASS.

### Ruff

`ruff check src tests benchmarks examples scripts`

Result: PASS — All checks passed.

### Strict mypy

`mypy --strict src/modular_modules/entry_model`

Result: PASS — no issues in 11 source files.

### Tests

`pytest -q`

Result: **316 passed in 2.21 s**.

The suite includes unit, edge, negative, regression, property-based, adversarial, real-Core integration, dependency planner, entry-point, replay, gap, duplicate/conflict, out-of-order, multi-instance, checkpoint/restore, corrupted-state, no-look-ahead and future-invariance tests.

### Coverage

`coverage run -m pytest -q`

Result: 316 passed.

`coverage report --fail-under=90`

Result: **99% total**; 961 statements, 12 missed, 350 branches, 5 partial branches. Gate 90% passed.

### Build

`python -m build`

Result: PASS.

Artifacts:

- `modular_strategy_entry_model-0.2.0-py3-none-any.whl`
- `modular_strategy_entry_model-0.2.0.tar.gz`

### Dependency check

`python -m pip check`

Result: PASS — no broken requirements.

## Clean offline wheel installation

A new virtual environment was created without system site packages. It installed only local wheel files:

1. local PyYAML 6.0.3 wheel reconstructed from the installed local distribution for offline dependency resolution;
2. retained Core 0.4.0 wheel;
3. newly built Entry Model 0.2.0 wheel.

Results:

- installation: PASS;
- `pip check`: PASS;
- public import: PASS;
- distribution version: 0.2.0;
- manifest version/API/state: 0.2.0 / 1.0 / 2;
- entry-point discovery: `entry_model` found and loadable;
- minimal Decimal plan: price `96.18`;
- schema-2 plan parsing: PASS;
- checkpoint/restore equality: PASS;
- real Core BacktestEngine example: PASS.

## Benchmark and load evidence

- quick workloads: five runs;
- standard 50,000-plan workload: five runs;
- extended 250,000-plan workload: three runs;
- stress 1,000,000 empty events: three runs;
- restart 50,000-plan workload: three runs;
- output digest equality required across every repeated workload;
- benchmark wall time: 355.83 seconds;
- machine-readable report: `artifacts/benchmark_results.json`;
- profile: `artifacts/profile_5000.txt`.

## Evidence files

- `artifacts/compileall.txt`
- `artifacts/ruff.txt`
- `artifacts/mypy.txt`
- `artifacts/pytest.txt`
- `artifacts/coverage_final.txt`
- `artifacts/build.txt`
- `artifacts/pip_check.txt`
- `artifacts/clean_install.txt`
- `artifacts/clean_smoke.txt`
- `artifacts/benchmark_results.json`
- `artifacts/profile_5000.txt`

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.

## Post-unpack archive verification

The ZIP was extracted to a new directory. `CHECKSUMS.sha256` and `FILES_MANIFEST.json` were verified, source compiled, wheel/sdist rebuilt from `module/`, and a new venv installed the unpacked Core/Entry Model wheels. Result: `pip check` passed, all 316 tests passed, and clean-wheel smoke passed.
