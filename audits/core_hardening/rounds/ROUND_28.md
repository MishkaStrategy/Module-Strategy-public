# Round 28 — security evidence, SBOM and clean installation

## Change
- Replaced runtime `assert` statements with explicit fail-closed checks.
- Checkpoint rollback failures are now surfaced instead of silently swallowed.
- Added security-tool dependency group.
- Added static security scan, secret detection, reproducible CycloneDX SBOM and artifact checksums.
- Built and installed the wheel in a clean virtual environment and ran `pip check` plus a smoke backtest.

## Local limitation
The vulnerability service could not be reached because the execution environment had no DNS access. This is not reported as a successful vulnerability audit; a mandatory networked `pip-audit` CI gate is added for the Draft PR.
