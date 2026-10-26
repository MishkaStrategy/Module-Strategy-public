# Entry Model — Error Discovery Audit

Date: 2026-07-21

## Scope and provenance

- Repository: `mishkacher/Module-Strategy`.
- Integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.
- Stale remote component branch inspected: `modules/entry-model-v0.2.0` at `ca2d2d2437b238f3b2a57be7d03f1f6df0e5013d`.
- Hardening branch: `modules/entry-model-hardening`.
- Authoritative local RC archive SHA-256: `5b77225898c41a959149e026f55f86ec466e4d4c21eac5c560acc788349d3fbe`.
- Real Core used for retest: Core 0.4.0 wheel from successful integration-ci artifact for base commit `5e03b933…`.

## Findings

| ID | Severity | Component | Symptom | Root cause | Reproducer | Fix | Regression evidence | Status |
|---|---|---|---|---|---|---|---|---|
| EM-EDA-001 | High | Git/local synchronization | Remote branch had 152 tests/91% coverage while local RC had 316 tests/99%; every inspected source blob differed. Using the remote branch would discard newer correctness hardening. | Local RC was completed after the earlier GitHub publication. | Blob comparison plus local/archive manifests. | Created `modules/entry-model-hardening` from the current integration base and selected the verified local RC as source of truth. | Full post-transfer suite, archive SHA-256 and branch diff restricted to Entry Model/workflow paths. | Fixed and published. |
| EM-EDA-002 | High | Financial geometry validation | `validate_retracement_geometry` relied on `assert setup.extreme_price is not None`; under `python -O` the guard disappeared and behavior changed to an incidental `TypeError`. | Runtime validation incorrectly used an optimization-removable assertion. | Bandit B101 and direct call with missing extreme under optimized Python. | Replaced the assert with explicit `ValueError("required price missing: extreme_price")`. | `test_retracement_geometry_missing_extreme_fails_without_assert_dependency`; full suite passes under normal and `python -O`. | Fixed. |
| EM-EDA-003 | Medium | Handoff/package layout | `pyproject.toml` expected `modules/entry_model/tests`, but archive tests and examples were outside `module/`; standard package commands could not discover them. | Release-bundle layout and repository target layout were different without an executable materialization step. | `pytest`, Ruff and compileall from `module/` reported missing `tests`; build warned about missing examples. | Materialized tests/examples into the actual package tree used for GitHub integration. | 324 tests collected from package root; examples included in sdist. | Fixed. |
| EM-EDA-004 | Medium | Source distribution | sdist contained `test_*.py` but omitted `tests/support.py` and `tests/__init__.py`, so the extracted source distribution could not run its own suite. | Implicit setuptools file discovery was insufficient. | `tar -tzf` inspection of the RC sdist. | Added explicit `MANIFEST.in` and `scripts/verify_distribution.py`; the CI Python resolver is also explicitly included. | Extracted sdist verification, full package suite and distribution contract tests pass. | Fixed. |
| EM-EDA-005 | Low | Formatting gate | Two tests failed `ruff format --check`. | RC used lint checking but its final tree was not fully format-checked. | `ruff format --check src tests benchmarks scripts`. | Applied Ruff formatting without semantic changes. | Format gate passes on source and extracted sdist. | Fixed. |
| EM-EDA-006 | Medium | GitHub workflow | Workflow hardcoded `ubuntu-latest`, lacked concurrency, timeout, manual trigger, security/SBOM and reliable cleanup; it also referenced benchmark artifacts it never generated. | Workflow predated the self-hosted runner policy and expanded release gates. | Static workflow review. | Replaced with self-hosted macOS/ARM64 quality, full-test, packaging, security and performance jobs; added concurrency, timeouts, paths, diagnostics, artifact retention and cleanup. | Workflow syntax/static contract and self-hosted runs with real step logs. | Fixed. |
| EM-EDA-007 | External blocker | GitHub-hosted Actions | PR #21 jobs failed before any first step and returned no logs (`BlobNotFound`). Core and integration jobs failed identically. | GitHub-hosted provisioning/usage availability, not a demonstrated Entry Model code failure. | Runs `29811574750`, `29811574745`, `29811574650`. | Moved the component workflow to the configured self-hosted runner and did not count the old runs as passed. | Self-hosted jobs reach checkout and produce real logs/artifacts. | Superseded; retained as historical evidence. |
| EM-EDA-008 | Medium | Self-hosted Python provisioning | Entry Model run `29829771279` failed for both Python 3.12 and 3.13 inside `actions/setup-python@v5`, before dependency installation or any component test. The runner exposed only system Python 3.14.6. | The workflow assumed `setup-python` could provision both requested interpreters on this macOS beta ARM64 runner. | Jobs `88631425936` and `88631425974`; both failed at step 4 while checkout and diagnostics passed. | Added `scripts/resolve_ci_python.sh`: use an already installed exact interpreter when available, otherwise install the requested version through isolated `uv`; every job then creates a dedicated venv. Removed reliance on `setup-python`. | Resolver contract tests; run `29832198549` passed Fast Quality and Full Test on Python 3.12.13 and 3.13.14. | Fixed. |
| EM-EDA-009 | Medium | Packaging workflow | Run `29832198549` successfully built and verified wheel/sdist and passed clean wheel-only installation, but failed while redirecting checksums into `modules/entry_model/reports/distribution-checksums.sha256`. | Each job receives a clean checkout; the packaging job had not created `modules/entry_model/reports` before the redirect. | Packaging job `88644762122`, step `Checksums`. | Create the reports directory in the checksum step before writing the file. | `test_packaging_workflow_creates_reports_directory_before_checksums`; full local 324-test suite and workflow YAML gate pass. | Fixed locally and published; CI rerun required. |
| EM-EDA-010 | Medium | Performance evidence | Successful macOS benchmark artifact reported `max_rss_kib=75,595,776`, which would imply roughly 72 GiB despite a small process. | `resource.getrusage().ru_maxrss` is bytes on Darwin but KiB on Linux; the raw macOS value was mislabeled as KiB. | Performance artifact `8496687145` from run `29832198549`. | Added platform-aware `normalize_max_rss_kib`: Darwin bytes are converted to KiB, other supported Unix values are treated as KiB; reject negative values. | Synthetic Darwin/Linux regression tests and a real short memory measurement. | Fixed locally and published; corrected performance artifact required. |
| EM-EDA-011 | Low | Source distribution quality | Setuptools emitted `no files found matching '*.yml' under directory 'examples'` during every build. | `MANIFEST.in` requested an unused `.yml` pattern while the project contains `.yaml` examples only. | Local build log after EM-EDA-009. | Removed the unmatched `.yml` glob without changing package contents. | Packaging contract and warning-free wheel/sdist build pass. | Fixed. |

## Repeated gates after fixes

- 324 tests passed on CPython 3.13.5 against real Core 0.4.0.
- `python -O -m pytest`: 324 tests passed; source validation does not depend on runtime assertions.
- Coverage: 99% (962 statements, 12 missed; 352 branches, 5 partial), gate 90%.
- Ruff check and format-check: pass.
- strict mypy: pass, 11 source files.
- compileall: pass.
- `bash -n scripts/resolve_ci_python.sh`: pass.
- workflow YAML and five-job contract: pass.
- Bandit: pass, zero findings after EM-EDA-002.
- detect-secrets: zero candidates.
- wheel and sdist: warning-free build and Twine checks pass.
- source distribution includes `resolve_ci_python.sh`; distribution verifier passes.
- clean wheel install passed in self-hosted run `29832198549` before the unrelated checksum redirect failure.
- Self-hosted Python 3.12 artifact: 321 tests, 0 failures/errors/skips, 98.75% line coverage and 98.58% branch coverage before the three new CI/evidence regressions.
- Self-hosted Python 3.13 artifact: same 321/0/0/0 and coverage before the three new regressions.
- Online security artifact: detect-secrets 0 candidates; pip-audit 69 dependencies and 0 vulnerabilities; valid CycloneDX SBOM with 71 components.
- Performance throughput/digests from artifact `8496687145` are usable, but its RSS field is explicitly invalidated by EM-EDA-010 and must be regenerated.

## Error-discovery verdict

No unresolved critical/high Entry Model code defect remains in the locally tested tree. A final self-hosted rerun must confirm EM-EDA-009 through EM-EDA-011, generate corrected package/performance artifacts and provide the exact successful remote head for the independent final audit.
