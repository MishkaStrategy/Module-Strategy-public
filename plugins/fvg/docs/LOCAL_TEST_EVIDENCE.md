# Local Test Evidence — FVG 0.2.1

Date: 2026-07-21. Runtime: Python 3.13.5. Core: exact `modular-strategy-core==0.4.0` wheel. Module API: v1 draft revision 3.

## Final local gates

```text
compileall: PASS
Ruff: All checks passed
strict mypy: Success, 5 source files
pytest: 237 passed
branch-aware coverage: 96% (gate 95%)
wheel: modular_strategy_fvg-0.2.1-py3-none-any.whl
sdist: modular_strategy_fvg-0.2.1.tar.gz
pip check: PASS
```

Machine evidence:

- `artifacts/hardening/final/local_quality_gates_pass.log`;
- `artifacts/hardening/final/coverage_pass.json`;
- `artifacts/hardening/final/clean_install.log`;
- `artifacts/hardening/final/distribution_checksums.sha256`;
- `artifacts/hardening/final/benchmark_results.json`;
- `artifacts/hardening/final/local_security_scan.json`.

The first final-gate attempt found two unused imports in the newly added hardening test file. It is preserved in `local_quality_gates.log`. The imports were removed and the entire gate was rerun; only the `_pass` evidence is the accepted result.

## Test composition

- 237 collected pytest cases;
- real-Core integration: 5;
- explicit regression file: 7;
- Hypothesis property nodes: 3 with 150 generated examples;
- 30 post-error executable hardening-round cases;
- remaining unit, edge, negative, migration and adversarial cases: 192.

## Clean-wheel verification

A newly created virtual environment installed:

1. PyYAML 6.0.3 from the local wheelhouse;
2. exact Core 0.4.0 wheel;
3. only the built FVG 0.2.1 wheel.

It then passed:

- `pip check`;
- public import;
- PEP 561 marker lookup;
- entry-point discovery;
- ModuleRegistry load;
- bullish FVG smoke;
- FeatureStore visibility;
- checkpoint/restore equality.

## Determinism and restart

The final benchmark's 10,000-event midpoint restart produced identical uninterrupted and restarted SHA-256 digests:

`7e3a301243cdeea6c5b9efdf7d3cb05c841dc017af8b0dd7bd41936620de80a9`

Checkpoint at split: 1,343 bytes. Measured restore latency: 216,961 ns. Restore-distribution median over 250 iterations: 88,298 ns.

## Security evidence

Local source scan found:

- no private-key/token/access-key signatures;
- no bare `except`;
- no broad `Exception`/`BaseException` handlers in source.

Bandit, detect-secrets, pip-audit and CycloneDX are configured in the self-hosted GitHub workflow because those tools were unavailable in the offline local environment.

## Python versions

Python 3.13.5 was exercised locally. Package metadata and strict mypy target Python 3.12. Exact runtime evidence for Python 3.12 must come from GitHub/self-hosted integration where that interpreter is available.
