# Market Regime Error Discovery Audit

Date: 2026-07-21

Component: `market_regime`

Local source under audit: Market Regime 1.2.0 local release candidate restored from the immutable release archive.

Remote integration target:

- repository: `mishkacher/Module-Strategy`;
- integration base: `integration/modular-strategy-v0.5.0`;
- base commit: `5e03b933207a99940244634f49a756d27888a5c3`;
- current Draft PR: `#29`;
- working branch: `modules/market-regime-hardening`;
- superseded Draft PR: `#17`, version 1.1.0;
- current candidate version: 1.2.0.

The immutable local archive was copied and its SHA-256 was verified before extraction:

`49a3b2ccf34df17719d762c62d708f97317a748749edbaae2be431bcc2ac9138`

## Audit method

The audit started before any new optimization round. It covered repository placement, package metadata, source and source-distribution builds, clean installation, public import and entry-point discovery, configuration parsing, state/restart behavior, deterministic benchmarks, formatting, typing, Bandit, secret scanning, remote synchronization and GitHub Actions execution on the user's self-hosted Mac runner.

The module was tested locally against factual Modular Strategy Core 0.4.0 artifacts. Repository-wide execution remains a mandatory remote gate and is never represented as locally passed.

## Confirmed component defects

| ID | Severity | Area | Symptom | Root cause | Reproducer | Fix | Regression evidence | Recheck |
|---|---|---|---|---|---|---|---|---|
| MR-ERR-001 | Medium | repository/package integration | `test_entry_point_declared_in_project_metadata` failed after placing the module at `modules/market_regime` | a test retained the standalone bundle path `module/pyproject.toml` instead of resolving the package root | run `pytest tests/test_integration.py` from the repository-target layout | resolve `pyproject.toml` from the module root | integration test executes in repository-target layout | PASS |
| MR-ERR-002 | High | source distribution | a fresh sdist omitted `tests/conftest.py` and `tests/fixture_v1_real_checkpoint.json`; its own tests failed during import | setuptools automatic discovery did not include support files and JSON fixture | build sdist, extract it, run its tests | add explicit `MANIFEST.in` coverage for source, tests, fixtures, docs, examples and benchmarks | isolated sdist member test and extracted-sdist suite | PASS |
| MR-ERR-003 | Medium | configuration contract | a timeframe containing more than Python's integer digit limit leaked built-in `ValueError` | `int(match.group(1))` was not translated to the component exception | `MarketRegimeConfig(timeframe=f"{'9' * 5000}w")` | raise `MarketRegimeConfigurationError` | oversized-timeframe regression | PASS |
| MR-ERR-004 | Low | formatting/quality gate | Ruff lint passed but formatter gate reported nine files | previous RC did not include formatter proof | `ruff format --check src tests benchmarks` | apply formatting without semantic changes | full suite and deterministic digests unchanged | PASS |
| MR-ERR-005 | Medium | optimized-Python/security | Bandit reported 13 runtime `assert` statements that disappear under `python -O` | type narrowing and invariants depended on `assert` | Bandit plus AST scan | explicit invariant checks and validated type narrowing | production-assert regression and Bandit zero findings | PASS |
| MR-ERR-006 | High | duplicate/order transactionality | rejecting conflicting repeated target data advanced base cursor before raising | base cursor committed before target identity validation | process T/B1, present conflicting T/B2, compare checkpoint | commit base cursor only after target validation succeeds | transactional rejection and corrected-retry regression | PASS |
| MR-ERR-007 | Low | example formatting | broadened release gate found `examples/integration_smoke.py` non-compliant | examples were omitted from formatter scope | `ruff format --check src tests benchmarks examples` | format example and include examples in all quality gates | final full suite and deterministic evidence | PASS |

## GitHub and CI findings

| ID | Severity | Area | Finding | Root cause / evidence | Fix or status |
|---|---|---|---|---|---|
| MR-CI-001 | High, external | shared Core/integration workflows | Core and integration jobs repeatedly fail before exposing executable test steps | Actions API returns empty step lists; no component test failure is available | OPEN outside module scope; no checks disabled and no paid fallback introduced |
| MR-CI-002 | Medium | runner selection | original module workflow used `ubuntu-latest` and consumed hosted minutes | workflow policy predated the self-hosted runner | FIXED: self-hosted-only selection with macOS/ARM64 fail-closed preflight |
| MR-CI-003 | Medium | workflow hardening | original workflow lacked concurrency cancellation, explicit timeouts and manual separation for heavy workloads | incomplete workflow policy | FIXED: concurrency, timeouts, cleanup, retention and `workflow_dispatch` heavy jobs |
| MR-CI-004 | High | Python provisioning on self-hosted macOS | Python 3.12 reached runner and checkout, then failed in `actions/setup-python@v5` before tests | downloaded macOS CPython packages are non-relocatable and require writable `/Users/runner/hostedtoolcache`; runner executes as a local service account | FIXED IN WORKFLOW: remove `setup-python`, resolve exact locally installed `python3.12`/`python3.13`, validate minor version and create isolated per-job venv; remote recheck pending runner availability |
| MR-CI-005 | Low | publication quality scope | first split publication of rounds 1–30 contained unused imports and examples were absent from fast checks | large test file was split after local audit | FIXED: Ruff-clean split tests and compile/Ruff/format/Bandit coverage for examples |

## Tool and environment limitations

- Python 3.13.5 was available locally and used for the final component audit.
- Python 3.12 was not available in the execution container and remains a mandatory self-hosted gate.
- The runner's actual installed Python inventory will be reported by the new local-interpreter resolver when the queued job executes.
- `pip-audit` was installed locally, but vulnerability lookup could not reach the advisory service because DNS resolution was unavailable. This remains `NOT RUN`, not PASS.
- The GitHub connector can inspect/write repository content and Actions metadata, but cannot enumerate custom runner labels or initiate a new `workflow_dispatch` run.

## Post-fix and publication result

- effective published module suite: 151 tests, PASS locally;
- statement coverage: 93%, gate 90%;
- Ruff lint: PASS;
- Ruff format including examples: PASS;
- strict mypy: PASS, 9 source files;
- compileall: PASS;
- Bandit: PASS, zero findings;
- detect-secrets: one reviewed non-secret historical configuration fingerprint fixture;
- wheel and sdist: PASS;
- Twine metadata: PASS;
- extracted-sdist suite: PASS;
- clean wheel install and `pip check`: PASS;
- public import, entry point, ModuleRegistry and checkpoint roundtrip: PASS;
- deterministic replay and future-data invariance: PASS;
- Draft PR #29 created from a clean integration base without force push;
- PR scope restricted to `.github/workflows/market-regime.yml` and `modules/market_regime/**`;
- merge, tag and release: NOT PERFORMED.

## Error-discovery verdict

All confirmed component defects and all actionable module-workflow defects are fixed and regression-protected. Market Regime 1.2.0 is conditionally ready for integration. The remaining blockers are external runner availability/local Python 3.12 installation, shared Core/integration workflow infrastructure, remote heavy-job dispatch and advisory-network access.