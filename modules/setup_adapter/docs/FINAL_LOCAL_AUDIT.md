# Canonical Setup Adapter 0.1.0 — Final Local Audit

Date: 2026-07-22

## Verified environment

- Python: 3.13.5
- Modular Strategy Core: 0.4.0 wheel
- Session Range: 0.1.0 wheel
- Entry Model: 0.2.0 final exact-head wheel
- Breakout–Return: 1.0.0 from the Core distribution

## Gates

- compileall: PASS
- Ruff lint: PASS
- Ruff format check: PASS
- strict mypy: PASS on all adapter source files
- normal tests: 65 passed
- optimized runtime (`python -O`): PASS
- statement + branch coverage: 96% total, above 90% gate
- real same-bar integration chain: PASS
- no-look-ahead truncated run: PASS
- future-data invariance: PASS
- deterministic plan price for the synthetic SELL scenario: `101.7022`
- bounded replay state and Core checkpoint/restore: PASS

## Component verdict

**CANONICAL SETUP ADAPTER 0.1.0 IS READY FOR LOCAL INTEGRATION TESTING.**

This is a component verdict, not a production-release declaration. No GitHub merge, tag or release is authorized by this local audit.

## Packaging and security evidence

- Wheel and source distribution build: PASS without warnings.
- Wheel metadata and single `setup_adapter` entry point: PASS.
- Sdist completeness for source, tests, examples and handoff documents: PASS.
- Clean wheel-only internal-package installation with locally provisioned PyYAML: PASS.
- `pip check`: PASS.
- Installed-package end-to-end smoke: PASS.
- Bandit: 0 findings across 756 lines of scanned source/test code.
- detect-secrets: 0 candidates.
- CycloneDX SBOM: generated, 6 installed runtime components.
- Online vulnerability-database lookup was attempted but could not complete because the execution container had no DNS access to PyPI. This audit does not misrepresent that external lookup as passed; the adapter introduces no runtime dependency beyond Core 0.4.x.
