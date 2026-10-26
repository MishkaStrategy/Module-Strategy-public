# Market Regime 1.2.0 — Independent Final Audit

Date: 2026-07-21

Component: `market_regime`

Remote integration target: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`

Published working branch: `modules/market-regime-hardening`

Draft PR: `#29`

## Source of truth

The final GitHub Actions correction was performed only after reading the repository's current `AGENTS.md` and `.github/WORKFLOW_RULES.md` from the default branch. Those files require:

- fast jobs on `[self-hosted, Linux, X64, fast]`;
- heavy jobs on `[self-hosted, macOS, ARM64, backtest]`;
- complete OS, architecture and workload labels;
- `workflow_dispatch` as the default and only trigger for heavy work;
- `pytest -m "not slow"` for fast tests and `pytest -m slow` for heavy tests.

Earlier PR descriptions, remembered runner behavior and the obsolete mixed workflow were not treated as authoritative.

## Audit scope

The audit covers:

- package metadata, public imports and entry point;
- Core 0.4.0 contracts and neighbouring-module compatibility;
- strict configuration and timeframe normalization;
- completed-bar semantics and point-in-time correctness;
- duplicate, conflicting duplicate, out-of-order and gap barriers;
- checkpoint schema 2, schema-1 migration and restart continuation;
- bounded state, checkpoint size, restore latency and traced memory;
- wheel, sdist, clean installation and package metadata;
- Ruff, strict MyPy, compileall, Bandit, detect-secrets and SBOM evidence;
- fast/heavy workflow routing and exact-commit verification;
- accidental unrelated changes.

## Component defect status

Seven original component defects were fixed and regression-protected:

1. repository-target packaging test path;
2. incomplete sdist test and fixture contents;
3. leaked raw `ValueError` for oversized timeframe integers;
4. incomplete formatter compliance;
5. runtime invariants implemented with removable `assert` statements;
6. non-transactional base cursor mutation after rejected target data;
7. omitted examples from the formatter gate.

Publication and CI work additionally resolved:

- obsolete duplicated legacy tests;
- missing `SkipReason.STALE_TARGET` in the published tree;
- missing `setuptools` for packaging regression tests;
- missing state-schema documentation in the source distribution;
- SBOM checksum evidence drift;
- ambiguous self-hosted runner routing;
- fast tests running on the Mac instead of the VDS;
- temporary `-k` exclusions instead of registered pytest markers.

No unresolved critical, high or medium defect is known within Market Regime runtime responsibility.

## Final source and package gates

| Gate | Result |
|---|---|
| Full local Market Regime suite | 151 passed |
| Statement coverage | 93% |
| Coverage gate | 90% |
| Ruff lint | PASS |
| Ruff format | PASS |
| strict MyPy | PASS |
| compileall | PASS |
| Bandit | PASS |
| Wheel | PASS |
| Source distribution | PASS |
| Twine metadata | PASS |
| Clean wheel installation | PASS |
| `pip check` | PASS |
| Public import | PASS |
| Entry-point discovery | PASS |
| Core registry load | PASS |
| Schema migration and restart | PASS |
| CycloneDX SBOM | PASS |

`pip-audit` was not completed locally because its advisory service was unreachable. This remains an external security gate and is not reported as passed.

## Determinism and performance evidence

Recorded evidence before the remote heavy rerun:

- 50,000 events × 3, median throughput approximately 10,235.64 events/s;
- 100,000-event workflow configuration includes midpoint checkpoint/restart;
- 250,000 continuous and midpoint-restored streams previously produced matching output and state digests;
- checkpoint size approximately 3,511 bytes;
- midpoint restore latency approximately 0.371 ms;
- 50,000-event traced peak allocation approximately 59,495 bytes;
- state remains bounded and checkpoint size remains below 16 KiB.

The interrupted one-million-event attempt remains explicitly unaccepted.

## Final workflow architecture

The obsolete mixed `.github/workflows/market-regime.yml` was removed.

### Automatic fast workflow

File: `.github/workflows/market-regime-ci.yml`

- triggers: pull request and push to `modules/market-regime-hardening`;
- runner: `[self-hosted, Linux, X64, fast]`;
- timeout: 20 minutes per matrix job;
- Python: 3.12 and 3.13, sequential;
- compileall;
- Ruff lint and format check;
- strict MyPy;
- `pytest -m "not slow"`;
- public import and entry-point smoke;
- fast wheel build;
- JUnit and wheel artifacts;
- no heavy benchmark, stress or security audit.

The first corrected VDS run was `market-regime-fast-ci` run `29856769598`. Both Python 3.12 and Python 3.13 jobs passed on workflow-tested head `03363c0b69c152cada753a59a0e7d8280b0b25f5` before this documentation-only update.

### Manual heavy workflow

File: `.github/workflows/market-regime-heavy.yml`

- only trigger: `workflow_dispatch`;
- runner for every job: `[self-hosted, macOS, ARM64, backtest]`;
- required input: exact expected branch SHA;
- each job fails if checked-out `HEAD` differs from the requested SHA;
- full repository, neighbouring-module and Market Regime suites;
- explicit `pytest -m slow` selection;
- coverage and JUnit evidence;
- wheel, sdist, Twine, clean install, `pip check`, PEP 561 and entry-point checks;
- Bandit, detect-secrets, networked `pip-audit`, CycloneDX SBOM and checksums;
- 50,000-event repeated benchmark;
- 100,000-event restart benchmark;
- 250,000-event continuous and restart comparison;
- memory tracing and bounded-state evidence;
- multiple `PYTHONHASHSEED` determinism comparison;
- exact SHA, run ID and runner metadata artifacts;
- finite timeouts and cleanup;
- no commit, push, merge, tag or release operation.

## Pytest classification

The `slow` marker is registered in `modules/market_regime/pyproject.toml`.

The 10,000-event checkpoint-size plateau test is explicitly marked `@pytest.mark.slow`. Static validation of previously generated 50,000- and 250,000-event evidence remains fast because it only reads and verifies bounded JSON artifacts rather than running the workloads again.

## Default-branch dispatch blocker

GitHub requires a manually dispatched workflow to exist in the default branch. The full heavy workflow exists in `modules/market-regime-hardening`, but not yet in `main`.

A separate infrastructure-only Draft PR `#49` was created from `infra/market-regime-heavy-dispatch` into `main`. It adds only `.github/workflows/market-regime-heavy.yml` as a dispatch registration point. It contains no Market Regime source, tests, benchmarks, documentation or trading logic.

PR `#49` must be reviewed and merged manually before the heavy workflow can be dispatched for the Market Regime branch. It was not merged automatically.

## Remaining blockers

Draft PR `#29` must remain Draft until:

1. infrastructure Draft PR `#49` is reviewed and merged manually;
2. `market-regime-heavy-audit` is dispatched with ref `modules/market-regime-hardening` and the exact current head SHA;
3. full-test, packaging/security and performance jobs pass on the Mac `backtest` runner;
4. all expected evidence artifacts are present and refer to the same SHA;
5. networked `pip-audit` completes successfully or is explicitly retained as an external blocker;
6. shared Core and integration workflows execute real test steps successfully.

## Verdict

**CONDITIONALLY READY — EXTERNAL CI BLOCKER**

Market Regime 1.2.0 source, tests, packaging, deterministic local evidence, fast VDS CI and workflow routing satisfy the available checks. Final manual integration review remains blocked until the default-branch dispatch registration is merged manually and the complete heavy audit passes on the exact final head.
