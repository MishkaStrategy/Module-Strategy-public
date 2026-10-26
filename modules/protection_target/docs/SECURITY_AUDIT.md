# Security Audit — Protection & Target 1.1.1

## Static source checks

- Bandit recursive source scan: zero findings;
- detect-secrets scan of runtime source, tests and package metadata: zero findings;
- no credentials, API keys, exchange endpoints, subprocess execution, network access or filesystem I/O exist in runtime code;
- production runtime source contains no `assert`, random UUID, system-clock read or broad `except Exception` handler.

## Dependency surface

Runtime dependency surface is intentionally limited to:

```text
modular-strategy-core>=0.4,<0.5
```

A reproducible CycloneDX JSON SBOM was generated and validated locally.

`pip-audit` was attempted against the clean local environment. If `round27_security_summary.json` reports `pip_audit_completed=false`, the audit was blocked by unavailable public vulnerability-index access in the sandbox. That status is explicitly **not** treated as a passing dependency vulnerability audit; self-hosted CI must complete it.

## Threat boundaries

The module:

- has no network or exchange I/O;
- does not load external files;
- does not read secrets or environment variables;
- does not place orders or mutate portfolio state;
- bounds input identifiers, nested fingerprints, Decimal shapes, diagnostics, processed state and counters;
- verifies checkpoint integrity with SHA-256 and semantic validation;
- fails closed on nondeterministic feature content and unverifiable legacy duplicate replay.
