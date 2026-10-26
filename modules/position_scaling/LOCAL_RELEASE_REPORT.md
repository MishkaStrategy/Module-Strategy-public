# Local Release Report

- component: Position Scaling & Adjustment;
- version: 0.2.2;
- status: hardening complete and ready for staged integration review;
- frozen Core compatibility: 0.4.2 / Module API 1.0;
- authoritative exact-head tests: 251 passed;
- branch-aware coverage: 90%, gate 90%;
- Ruff, format, strict MyPy and compileall: passed;
- wheel, source distribution, clean install, entry points, registry, checkpoint smoke and `pip check`: passed;
- repository-root compatibility: 244 passed;
- Session Range: 84 passed;
- Market Regime: 80 passed;
- FVG: 70 passed;
- Bandit, detect-secrets, pip-audit and CycloneDX SBOM: passed;
- deterministic replay, benchmark and bounded-memory evidence: passed;
- 3 completeness rounds, 30 hardening rounds and 15 paired audits completed;
- PS-CCR-001: closed by Risk Sizing 1.2.0 and pinned cross-module run `29866061235`;
- authoritative component workflow: `position-scaling-hardening-ci`, run `29867118592`, success;
- GitHub CI and artifact evidence are read from Draft PR #23 and are not inferred from local results.

This report does not assert production/live readiness or strategy profitability. Wider execution, persistence, factual-fill reconciliation and operational monitoring remain release-candidate responsibilities.
