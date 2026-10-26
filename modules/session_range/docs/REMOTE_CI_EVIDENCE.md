> Historical report; physical runner identifiers are pseudonymized in this export. This is not new-root acceptance.

# Remote CI Evidence — Session Range 0.2.0

## Scope

This document supersedes any `PENDING SELF-HOSTED CI` wording retained in earlier point-in-time local audit notes.

- Repository: `mishkacher/Module-Strategy`
- Base branch: `integration/modular-strategy-v0.5.0`
- Base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Candidate branch: `modules/session-range-hardening`
- Draft PR: #26
- Audited source/CI head: `deccce1005dea76edecf258563319843ae4be9b6`
- Self-hosted workflow run: `29846633473`
- Runner: `archived-darwin-runner`, macOS, ARM64
- Managed interpreters: CPython 3.12 and CPython 3.13

## Quality matrix

Both interpreter jobs completed successfully.

| Gate | Python 3.12 | Python 3.13 |
|---|---:|---:|
| Tests | 139 passed | 139 passed |
| Failures/errors/skips | 0/0/0 | 0/0/0 |
| Statement coverage | 97% | 97% |
| Coverage threshold | 96% pass | 96% pass |
| Ruff lint | pass | pass |
| Ruff format | pass | pass |
| strict mypy | pass | pass |
| compileall | pass | pass |
| root/cross-module tests | pass | pass |
| integration smoke | pass | pass |
| wheel and sdist | pass | pass |
| Twine validation | pass | pass |
| clean install and `pip check` | pass | pass |
| entry-point smoke | pass | pass |

The repository integration smoke produced the same deterministic result on both interpreters: 514 events, 159 features, 2 setups, 1 signal, the expected `session_range`, `market_regime` and `fvg` entry points, and status `ok`.

## Security and supply chain

The `security-and-sbom` job completed successfully.

- Bandit: 0 findings across 1,371 source lines.
- detect-secrets: 0 findings.
- pip-audit: 56 dependencies audited, 0 known vulnerabilities.
- `pip check`: pass.
- CycloneDX SBOM: 58 components.
- Evidence checksum manifest: generated.

Artifact:

- Name: `session-range-security`
- GitHub artifact digest: `sha256:35318de4e492b83bf1825e4294a94a48d8d962b5cc04565b460a521fdaf39ef8`

## Performance

The `performance` job completed successfully with CPython 3.13.14.

| Workload | Throughput | Median time | Checkpoint | Restore median |
|---:|---:|---:|---:|---:|
| 1,000 | 66,197 events/s | 0.0151 s | 1,144 bytes | 57.9 µs |
| 10,000 | 68,934 events/s | 0.1451 s | 1,145 bytes | 57.4 µs |
| 50,000 | 69,026 events/s | 0.7244 s | 1,158 bytes | 59.2 µs |
| 250,000 | 68,150 events/s | 3.6684 s | 1,466 bytes | 89.1 µs |
| 1,000,000 | 67,114 events/s | 14.9000 s | 1,157 bytes | 94.0 µs |

Artifact:

- Name: `session-range-performance`
- GitHub artifact digest: `sha256:c5183a2dc76005a12e62a846cfde6a3cabd3b18e71077826154a83ea7eda0bc3`

## Quality artifacts

- Python 3.12 artifact digest: `sha256:4e2ce478a83422b6e43ce16070abc9a2b93d920b79b6556ca24ae97675f263d7`
- Python 3.13 artifact digest: `sha256:02b3a9598b0b638c1a1454e36904533e2986193f33337d0b1b91e19d1e1751da`

Each quality artifact contains JUnit XML, coverage text and JSON, integration-smoke JSON, wheel/sdist checksums, Twine output and built distributions.

## Remote diff audit

The PR diff is restricted to:

- `modules/session_range/**`;
- `.github/workflows/session-range-hardening.yml`.

No transport, overlay, bootstrap, repair or diagnostic files remain. Core and neighboring module source files are unchanged.

GitHub-hosted legacy Core/integration workflows failed before meaningful test execution because hosted capacity was unavailable. Their failure is infrastructure-only; the equivalent root and cross-module suites passed in both pinned self-hosted quality jobs.

## Decision

`ALL MODULE-SCOPED READINESS GATES PASSED`

The PR remains Draft and unmerged by project policy. Manual integration review is the next allowed action.