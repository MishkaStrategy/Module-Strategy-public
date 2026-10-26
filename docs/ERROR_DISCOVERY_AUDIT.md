# Error Discovery Audit — Modular Strategy Core 0.4.1 → 0.4.2

## Scope and source identity

- Repository: `mishkacher/Module-Strategy`
- Remote integration base: `integration/modular-strategy-v0.5.0`
- Remote base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Existing hardening branch: `core/hardening-v0.4.x`
- Existing Draft PR: `#13`
- Remote hardening head inspected: `362e11b5876fdab82bbc1f5183fc9630dcdc6187`
- Local release candidate source: Core `0.4.1`, local manifest commit `6e1cfdc0bd261cdedcd2ab34d38b69696940cb71`
- Local audit branch: `core/hardening-v0.4.1-error-audit`

The local RC was preserved before applying any correction. The 0.4.1 package sources were independently matched to the verified CI wheel before this audit.

## Findings

| ID | Severity | Component | Symptom | Root cause | Reproducer | Fix | Verification |
|---|---|---|---|---|---|---|---|
| ED-001 | High | checkpoint/restore | A module that mutates state and then raises during restore remains corrupted after the operation fails. | The failing module was added to the rollback list only after `restore_state` returned successfully. | `test_atomic_restore_rolls_back_module_that_mutates_then_raises` | Track every attempted module before calling `restore_state`; rollback the failing module and all previously restored modules. Surface incomplete rollback as a hard failure. | Regression and full suite pass. |
| ED-002 | Medium | durable state | `AtomicJsonStateRepository.load()` returned mutable nested dictionaries/lists while the in-memory repository returned an immutable snapshot. | Disk state was parsed and returned directly instead of passing through `deep_freeze`. | `test_atomic_json_repository_load_is_deeply_immutable` | Deep-freeze the decoded JSON before returning it. | Regression and full suite pass. |
| ED-003 | Medium | checkpoint compatibility | An unsupported checkpoint schema such as `999` was accepted by `CheckpointManager.load`. | The checkpoint schema was stored but never compared with supported versions. | `test_checkpoint_manager_rejects_unsupported_schema` | Add a backward-compatible `supported_schema_versions=("1",)` constructor option and fail closed before decoding module state. | Regression and full suite pass. |
| ED-004 | Medium | checkpoint integrity | Any 64-character text was accepted as a SHA-256 field, and integrity comparison was not constant-time. | Validation checked only length and load used normal string inequality. | `test_checkpoint_rejects_non_hex_integrity_digest` | Require 64 hexadecimal characters and use `hmac.compare_digest`. | Regression and full suite pass. |
| ED-005 | High | restart/reconciliation | Local and remote states with different client-order IDs could be reported as fully matched. | Reconciliation compared pending order IDs and position only; `client_order_ids` were ignored. | `test_reconciliation_detects_client_order_identity_mismatch` | Compare both client-ID sets, expose additive mismatch fields, and require safe halt on divergence. | Regression and full suite pass. |
| ED-CI-001 | Medium | workflows | Every current workflow uses `ubuntu-latest`; no self-hosted workflow, concurrency cancellation, or macOS portability gate exists. | Existing workflows predate the configured user runner and were optimized for GitHub-hosted Linux. | Static workflow inspection | Prepare a separate self-hosted macOS workflow after runner labels are verified; preserve Linux compatibility workflow. | Pending runner-label verification. |
| ED-QA-001 | Low | repository-wide lint | `ruff check .` reports 22 findings, primarily unused imports in audited module code/tests plus two ambiguous test variable names. | Existing CI intentionally scopes Ruff per component rather than repository-wide. | `ruff check .` | Core PR does not silently edit unrelated module implementations. Core-owned findings will be handled in the hardening rounds; module-owned findings are recorded for their component branches. | Component Ruff passes. |
| ED-QA-002 | Low | formatting | `ruff format --check` would reformat 29 core/hardening files. | No formatter gate was part of the existing published candidate. | `ruff format --check src/modular_core tests/test_hardening_*.py benchmarks/hardening` | Treat as review-noise debt; do not mix a mass reformat with correctness fixes. Add a formatter policy only in a dedicated formatting change. | Accepted residual. |

## Initial test evidence

Before installing external module packages, the root integration test failed because the expected entry points were absent. This was an environment/setup failure, not a kernel defect. The audit environment then installed the real editable packages for Session Range, Market Regime and FVG and repeated the suite.

After the five code fixes:

- full repository suite: **297 passed**;
- statement coverage: **87.90%**;
- branch coverage: **71.93%**;
- combined branch-aware coverage: **84.04%**;
- component Ruff: pass;
- strict mypy: pass across 25 core source files;
- compileall: pass;
- Bandit: 0 findings;
- detect-secrets: 0 candidates;
- wheel and source distribution: built;
- Twine metadata: pass;
- wheel-only clean install and checkpoint/restore smoke: pass;
- installed dependency check: pass.

## GitHub and runner status

The existing PR head workflows were read and confirmed successful on the prior 0.4.1 commit. The connected GitHub API exposes workflow runs/jobs but not the repository self-hosted runner label inventory. Therefore the preferred label set `[self-hosted, macOS, ARM64, module-strategy]` is treated as unverified until a label-aware API or an actual queued job confirms it. No heavy paid-runner workflow is triggered merely to discover labels.

## Verdict

The previous 0.4.1 release candidate was not fully correct for atomic recovery and reconciliation. The confirmed defects are fixed in candidate 0.4.2 with regression coverage. The component may proceed to a new post-error-fix baseline and completeness/hardening cycle.
