# Local Test Evidence — Protection & Target 1.1.1

## Environment

- CPython 3.13.5;
- Core 0.4.0;
- Module API 1.0;
- pytest 9.0.2;
- coverage 7.13.3;
- Ruff 0.15.22;
- mypy 1.20.2.

Python 3.12 is a required self-hosted CI matrix target and was not available as a local interpreter in this sandbox.

## Final source-tree gates

```text
compileall: passed
Ruff format: 22 files formatted
Ruff check: passed
strict mypy: 9 source files, no issues
pytest: 232 passed, 0 failed/errors/skipped
coverage: 93.3380% combined, gate 90%
Bandit: 0 findings
detect-secrets: 0 findings
CycloneDX SBOM: generated
```

Coverage totals:

- statements: 1,008;
- missing statements: 53;
- branches: 418;
- partial branches: 42;
- combined line/branch coverage: 93.3380%.

## Test inventory

| Suite | Cases |
|---|---:|
| Real-Core integration | 9 |
| Calculation and financial geometry | 42 |
| Configuration and manifest | 30 |
| Input and fingerprint contracts | 37 |
| Module behavior and causality | 20 |
| Optimized runtime | 2 |
| Error-discovery adversarial hardening | 22 |
| Post-error optimization rounds | 21 |
| State, corruption and migration | 48 |
| Self-hosted workflow contract | 1 |
| **Total** | **232** |

## Installed-wheel evidence

The final 1.1.1 wheel was installed with Core 0.4.0 and PyYAML into a newly created product virtual environment. The module import path was confirmed inside that environment's `site-packages`.

Passed:

- `pip check`;
- distribution/package version identity;
- public import;
- Python entry point discovery;
- `ModuleRegistry.discover_entry_points()`;
- complete 232-test suite against the installed wheel;
- installed-package coverage gate, 93%.

Test-runner libraries were supplied separately without installing the source package editable into the product environment.

## Security evidence

- no runtime network, filesystem, exchange, secret or environment-variable access;
- no production assertions, random UUIDs, direct system-clock reads or broad exception swallowing;
- Bandit and detect-secrets are clean;
- reproducible CycloneDX SBOM generated;
- local `pip-audit` was attempted but public vulnerability-index access was unavailable. It is a mandatory self-hosted CI gate and is not reported as locally passed.

## Temporal and replay evidence

- FeatureView timestamp must equal completed-bar close;
- future structural and entry features cannot alter the current decision;
- exact duplicates are idempotent;
- changed bar, feature or gap context is rejected;
- out-of-order and stream-switch events are rejected;
- schema-2 restore is signed and transactional;
- compatible 1.1.0 schema-2 state restores into 1.1.1;
- schema-1 migration cannot verify same-bar replay and therefore fails closed until the next completed bar;
- deterministic replay and million-event restart stress passed.
