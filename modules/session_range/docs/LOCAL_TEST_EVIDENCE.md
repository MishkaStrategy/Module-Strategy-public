# Local Test Evidence — Session Range 0.2.0

## Executed environment

- CPython 3.13.5
- Modular Strategy Core 0.4.0 wheel
- module source target: Python 3.12+
- candidate code checkpoint before documentation: `e82547da83a6177e923564a96855396976c3d773`

## Commands and results

```bash
python -m compileall -q modules/session_range/src modules/session_range/tests
python -m ruff check modules/session_range/src modules/session_range/tests modules/session_range/benchmarks
python -m ruff format --check modules/session_range/src modules/session_range/tests modules/session_range/benchmarks
MYPYPATH=modules/session_range/typings python -m mypy --strict modules/session_range/src/modular_session_range
python -m coverage run --source=modules/session_range/src/modular_session_range -m pytest -q modules/session_range/tests
python -m coverage report -m --fail-under=96
python -m build modules/session_range
```

Results:

- 139 tests collected and passed;
- 97% statement coverage (`808` statements, `27` missed);
- compileall: pass;
- Ruff lint: pass;
- Ruff format: pass;
- strict mypy: pass;
- wheel and source distribution: pass.

Exact console evidence is under `artifacts/test_evidence/` and machine-readable coverage is `artifacts/coverage.json`.

## Clean wheel installation

A fresh virtual environment was created. Only local PyYAML 6.0.3, Core 0.4.0 wheel and Session Range 0.2.0 wheel were installed. The following passed:

- `pip check`;
- `import modular_session_range`;
- package version 0.2.0;
- `modular_strategy.modules` entry-point discovery and load;
- completed-session processing smoke;
- JSON-compatible checkpoint/restore equality.

## Security evidence

- Bandit 1.9.4: 0 findings after replacing three B101 runtime asserts with explicit fail-closed checks;
- detect-secrets 1.5.0: 0 findings when the intentional benchmark SHA-256 digest artifact is excluded;
- CycloneDX SBOM: generated for the isolated installed Core 0.4.0 + Session Range 0.2.0 environment;
- clean-environment `pip check`: pass;
- `pip-audit`: installed and attempted, but the vulnerability service endpoint could not be resolved from this container; it is not reported as passed;
- no broad `except Exception` in production source.

Evidence is stored under `artifacts/security/`.

## Runtime coverage limitation

Python 3.12 is not installed locally. Static configuration targets Python 3.12, while runtime execution was Python 3.13.5. Python 3.12 and repository-wide tests remain mandatory in CI.
