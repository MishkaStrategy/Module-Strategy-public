# Core security and supply-chain report

## Local static results

- Bandit: **0 findings** over `src/modular_core`.
- detect-secrets: **0 candidates** over tracked core/test/benchmark/script/documentation surfaces.
- Runtime `assert` statements used as invariants: removed from production core paths.
- Checkpoint rollback exceptions: no longer silently swallowed.
- Path traversal, oversized state/event/config inputs, deterministic serialization and atomic persistence have adversarial regression coverage.

## Supply chain

- Clean wheel: `modular_strategy_core-0.4.1-py3-none-any.whl`.
- Clean virtualenv installation: pass.
- `pip check`: pass.
- Reproducible CycloneDX JSON SBOM: generated and validated.
- SHA-256 manifest covers wheel and final evidence artifacts.

## Vulnerability audit

The local environment could not resolve the external vulnerability service. This is recorded as `pending_remote_ci`, not as a pass. The Draft-PR security job runs `pip-audit . --strict` with network access and must be green before final acceptance.

## Secrets and live safety

No API key, account credential or exchange secret was added. The work does not enable live trading and does not claim a production-safe live adapter.
