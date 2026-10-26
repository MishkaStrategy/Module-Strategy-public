# Entry Model — Post-Error Hardening Addendum

Date: 2026-07-21

This addendum supplements `OPTIMIZATION_AND_AUDIT_LOG.md`. It is not counted as an additional numbered optimization round and does not replace the existing 30 rounds or 15 even-round audits.

## Resumption and Error Discovery

After the original hardening sequence, the GitHub resumption session repeated Error Discovery before accepting the release candidate. Eleven reproducible findings were tracked in `ERROR_DISCOVERY_AUDIT.md` and either fixed or correctly classified.

The post-error sequence added four evidence-producing regression areas without changing trading semantics:

- explicit optimized-runtime geometry validation, proving runtime behavior does not depend on `assert`;
- exact Python 3.12/3.13 resolution and isolated self-hosted virtual environments;
- source-distribution and checksum-workflow contracts;
- platform-aware RSS normalization for truthful macOS memory evidence.

## Final repeated evidence

Audited code/evidence head: `d699e8c2f677b19ab744b5442d0e743a3dd1a179`.

- Local: 324 normal tests and 324 tests under `python -O`.
- Self-hosted Python 3.12: 324 tests, 0 failures, 0 errors, 0 skipped.
- Self-hosted Python 3.13: 324 tests, 0 failures, 0 errors, 0 skipped.
- Coverage: 950/962 lines = 98.75%; 347/352 branches = 98.58%.
- Ruff, format, strict mypy, compileall and shell syntax: PASS.
- Bandit, detect-secrets, online pip-audit and CycloneDX SBOM: PASS.
- Wheel, sdist, Twine, distribution verification, checksums and clean wheel-only installation: PASS.
- 50k continuous and restart digests: identical.
- 250k extended digest: unchanged from the independently verified release candidate.
- Corrected normalized max RSS: 68,704 KiB.
- Self-hosted release run `29835220983`: all seven jobs PASS.

## Artifacts

- Python 3.12 tests: `8497521917`.
- Python 3.13 tests: `8497852230`.
- Security: `8497550551`.
- Distributions: `8498041650`.
- Performance: `8498237319`.

## Decision

The post-error re-execution confirms the original hardening decisions against the newer implementation and real Core 0.4.0. No unresolved critical or high Entry Model defect remains. The final documentation-only head must pass the same self-hosted workflow before the Draft PR is considered documentation-complete. No merge, tag or release is authorized by this addendum.
