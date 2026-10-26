# Session Range 0.2.0 — Hardening Release Report

## Status

`VERIFIED RELEASE CANDIDATE — SELF-HOSTED CI PASS`

## Scope

Session Range remains an analytical, DST-aware and gap-safe OHLCV feature producer. It emits `session_range.active` and `session_range.completed`; it does not emit entries, orders, risk decisions or position actions.

## Completed evidence

- Core/API compatibility: Core 0.4.0 / Module API 1.0 draft.
- Managed CPython 3.12 and 3.13 on self-hosted macOS ARM64: pass.
- Tests: 139 passed per interpreter, with zero failures, errors or skips.
- Coverage: 97%, above the 96% gate.
- Ruff lint/format, strict mypy and compileall: pass.
- Root and cross-module regression suite: pass.
- Wheel and source distribution: pass on both interpreters.
- Twine package validation: pass.
- Clean wheel-only installation, `pip check`, public import and entry point: pass.
- Bandit: 0 findings across 1,371 source lines.
- detect-secrets: 0 findings after excluding generated evidence and the intentional benchmark digest.
- pip-audit: 56 dependencies audited, 0 known vulnerabilities.
- CycloneDX SBOM: 58 components.
- Self-hosted stress benchmark: approximately 67,114 events/s for 1,000,000 events; checkpoint 1,157 bytes; restore approximately 94 microseconds.
- Interleaved comparison versus 0.1.0: +4.18% at 10k and +4.36% at 50k.
- Error Discovery Audit: all confirmed defects fixed with regression coverage, including optimized-Python fail-closed hardening.
- Final PR diff audit: only `modules/session_range/**` and the module-scoped hardening workflow remain.

## Remote evidence

- Draft PR: #26.
- Audited source/CI head: `deccce1005dea76edecf258563319843ae4be9b6`.
- Full self-hosted verification run: `29846633473`.
- Quality, security/SBOM and performance artifacts were uploaded with SHA-256 digests recorded by GitHub Actions.

Detailed results are documented in `docs/REMOTE_CI_EVIDENCE.md` and `docs/FINAL_AUDIT.md`.

## Release decision

All module-scoped readiness gates are complete. The candidate is ready for manual integration review. It is not a production-live trading strategy release, the PR remains Draft by project policy, and automatic merge is not authorized.