# Intermediate audit after rounds 27–28

## Decision
**Continue, with one remote gate pending.** Static quality, clean packaging, local security scans and SBOM generation are green. The network-backed vulnerability lookup could not run locally because DNS was unavailable, so it is mandatory in Draft-PR CI before final acceptance.

## Changes
- Cleared all Ruff findings across core, tests and benchmarks.
- Cleared strict mypy errors across all 26 core source files.
- Declared PyYAML typing stubs and a separate security tool extra.
- Replaced optimizer-removable runtime asserts with explicit fail-closed checks.
- Exposed incomplete checkpoint rollback as a hard failure.
- Added clean wheel installation, static security, secret scanning, reproducible SBOM and checksum gates.
- Extended core CI with Python 3.12/3.13 quality gates and a networked security job.

## Public contracts
No import path, DTO field, method name or existing default changed. Type annotations were narrowed internally. New optional dependency groups and CI jobs are additive.

## Evidence
- Full suite: **190 tests passed**.
- Branch coverage: **83%**.
- Ruff: **0 findings** (baseline: 20).
- strict mypy: **0 errors** (baseline: 23).
- `compileall`: passed.
- Wheel build: passed.
- Clean virtualenv install and `pip check`: passed.
- Installed-wheel smoke backtest: 3 bars, 5 events, deterministic run ID `run-8bde4bb21fc5c566`.
- Bandit: 0 findings after removal of seven runtime asserts and one swallowed rollback failure.
- detect-secrets: 0 candidates on the tracked core surfaces.
- CycloneDX SBOM: generated and validated; 2 runtime-environment components.
- SHA-256 evidence manifest: generated.

## Backward compatibility
The removal of runtime asserts only makes corrupted internal state fail consistently under optimized Python. Existing valid flows are unchanged. Checkpoint rollback keeps the original behavior when rollback succeeds and now fails closed when rollback itself is incomplete.

## No-look-ahead / determinism / restart
No market visibility or ordering semantics changed. Clean-wheel smoke retains the deterministic run identity. Checkpoint failure evidence is stronger and no longer hides a potentially split module state.

## Regression risks
- CI security tooling adds installation time and relies on external vulnerability services.
- Consumers relying on invalid internal orders with missing normalized prices now receive explicit runtime failures rather than undefined behavior.

## Residual risks
- Local `pip-audit` result is explicitly **not available** due DNS failure. The security CI job must pass before the final verdict.
- The generated local SBOM covers the clean core wheel environment; downstream module packages require the integration-level SBOM gate.
