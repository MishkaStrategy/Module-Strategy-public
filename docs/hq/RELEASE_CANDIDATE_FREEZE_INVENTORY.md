# Release Candidate Freeze Inventory

**Snapshot:** candidate `release-manifest-387-v2`, HEAD `127c483659fbb3acdfc90ba39ecb3af2b4c69616`; observed 2026-10-25 17:03 Novosibirsk with `git status --porcelain=v1 --untracked-files=all`. This is a path/status inventory, not a source review or release approval.

**Counts:** the default directory-collapsed porcelain view has 129 entries; exact `--untracked-files=all` expands this to 3,217 paths total: 66 tracked modified and 3,151 untracked, including 3,068 under `graphify-out/`; 83 non-Graphify untracked paths; 149 non-Graphify dirty paths. Tracked diff summary: 66 files, 6,462 insertions / 439 deletions (untracked file contents are not in this summary). Classifications: 86 locally evidenced slices, 38 other unverified changes, 4 report artifacts, and 21 uncertain retained-content paths. Graphify output is excluded from the path table but remains dirty and was preserved.

**Classification rule:** “locally evidenced” means only that an HQ record documents a local slice/test; it does not verify the entire dirty file, exact tree as a whole, or release acceptance. All other candidate source/contract/tooling paths are excluded pending review. ROADMAP rev. 734 records the protocol document’s #1190 STOP/plan-only notice; the rest of that dirty protocol diff remains unverified. The two #395 Core lifecycle transaction-contract/coordinator paths remain limited to 19 focused synthetic tests and Sol’s narrow review (ROADMAP rev. 750); no canonical provider/backend, owner-native instance-to-order/position binding, or Runner/Paper effect wiring exists. The four new locally evidenced Results Aggregator paths fix #250 audit-evidence package/model/schema alignment; the 12 focused tests establish only this local contract slice, not independent-auditor trust, production registry sources, #250 acceptance, or PAPER. The completed security scan `04288d47-d12e-45c3-9e97-edb83c9fa76b` reviewed an earlier candidate snapshot and does not cover these four later paths; it was partial and excluded the 21 uncertain retained-content paths and generated Graphify output. Two uncertain paths were opened by delegated reviewers before classification surfaced; no values were returned in their reports or files changed, and the other 19 remained unopened. No real non-secret Option 3 controller/runner, key-custody, or evidence-store references or assigned external owners/admins are available; related trust gates stay fail-closed. “Uncertain retained user content” is preserved and excluded until the owner classifies it. No candidate files were deleted, checked out, reset, staged, or committed.

## Path inventory

### Locally evidenced slice (scope-limited; candidate-only) (86)

| Path | Status | Evidence / disposition |
|---|---:|---|
| `applications/control_plane/src/modular_control_plane/paper_api.py` | `M` | ROADMAP rev. 580: bounded read-only owner-snapshot projection only; no owner reader attached. |
| `applications/control_plane/src/modular_control_plane/paper_positions.py` | `??` | ROADMAP rev. 580: bounded read-only owner-snapshot projection only; production owner source absent. |
| `applications/control_plane/src/modular_control_plane/recovery_profile_api.py` | `??` | ROADMAP rev. 577 / 581: bounded/authenticated recovery API tests; service remains unconfigured. |
| `applications/control_plane/src/modular_control_plane/recovery_profiles.py` | `??` | ROADMAP rev. 588: named-profile interface; no trusted persistence/effect adapter. |
| `applications/control_plane/src/modular_control_plane/server.py` | `M` | ROADMAP rev. 577 / 588: recovery-router/default-unconfigured behavior only. |
| `applications/control_plane/src/modular_control_plane/server_launcher.py` | `M` | ROADMAP rev. 730: install guidance slice; local candidate only. |
| `applications/control_plane/tests/test_paper_positions_api.py` | `??` | ROADMAP rev. 580: local API projection contract evidence; not #933 acceptance. |
| `applications/control_plane/tests/test_paper_positions_contract.py` | `??` | ROADMAP rev. 580: local bounded-projection tests; not production owner evidence. |
| `applications/control_plane/tests/test_recovery_profile_api.py` | `??` | ROADMAP rev. 577 / 581: focused synthetic API tests; default service unavailable. |
| `applications/control_plane/tests/test_server_launcher.py` | `M` | ROADMAP rev. 730: focused launcher test and local Control Plane suite; no release acceptance. |
| `applications/system_acceptance/src/modular_system_acceptance/production_composition.py` | `M` | ROADMAP rev. 737: covered by synthetic composition/identity tests only; no real owner loading or acceptance callback. |
| `applications/system_acceptance/tests/test_production_composition.py` | `M` | ROADMAP rev. 737: one of three focused files; combined suite 33 passed, no Backtest/acceptance callback. |
| `applications/trading_runner/src/modular_trading_runner/__init__.py` | `M` | ROADMAP rev. 746: exports standalone Lifecycle core; no production owner composition or effect integration. |
| `applications/trading_runner/src/modular_trading_runner/market_data.py` | `M` | RELEASE_CHECKLIST rev. 721 / TRACEABILITY rev. 698: provided-envelope freshness check only; no trust/current-owner proof. |
| `applications/trading_runner/src/modular_trading_runner/ports.py` | `M` | ROADMAP rev. 727: GLOBAL kill-switch port slice only. |
| `applications/trading_runner/src/modular_trading_runner/runtime.py` | `M` | ROADMAP rev. 727: GLOBAL kill-switch slice only; remaining dirty hunks are not certified. |
| `applications/trading_runner/src/modular_trading_runner/strategy_lifecycle.py` | `??` | ROADMAP rev. 746: durable Lifecycle owner core and command records; callbacks are injected, no production composition or atomic effect fence. |
| `applications/trading_runner/tests/test_market_data.py` | `M` | RELEASE_CHECKLIST rev. 721: focused freshness tests (13); candidate-only. |
| `applications/trading_runner/tests/test_runtime.py` | `M` | ROADMAP rev. 727: local GLOBAL kill-switch test evidence; not per-instance #395 acceptance. |
| `applications/trading_runner/tests/test_strategy_lifecycle.py` | `??` | ROADMAP rev. 746: six synthetic focused tests; not #395 acceptance or Paper/Core effect evidence. |
| `modules/market_data_ingestion/src/market_data_application/api.py` | `M` | ROADMAP rev. 736: authentication-provider validation exercised by the five focused API tests using in-process TestClient, FakeExchangeAdapter, and temporary storage only; no remote endpoint. |
| `modules/market_data_ingestion/src/market_data_application/control_plane_runtime.py` | `M` | ROADMAP rev. 736: hardened auth configuration validation exercised with synthetic keys/bootstrap values and tmp_path; no durable owner/control-plane DB created. |
| `modules/market_data_ingestion/src/market_data_application/server.py` | `M` | ROADMAP rev. 736: startup auth-mode and exactly-one-provider paths exercised; `--check` does not start uvicorn, and missing auth is rejected before app construction. |
| `modules/market_data_ingestion/tests/auth_helpers.py` | `??` | ROADMAP rev. 736: local test helper contains only a synthetic admin token and authenticator used by in-process API tests. |
| `modules/market_data_ingestion/tests/test_application_catalog.py` | `M` | ROADMAP rev. 736: included in five-file focused suite, 30 passed; FakeExchangeAdapter and tmp_path stores. |
| `modules/market_data_ingestion/tests/test_application_inspection.py` | `M` | ROADMAP rev. 736: included in five-file focused suite, 30 passed; synthetic datasets/repairs and tmp_path storage. |
| `modules/market_data_ingestion/tests/test_application_security.py` | `M` | ROADMAP rev. 736: included in five-file focused suite, 30 passed; synthetic bearer tokens and tmp_path storage. |
| `modules/market_data_ingestion/tests/test_application_server.py` | `M` | ROADMAP rev. 736: included in five-file focused suite, 30 passed; FakeExchangeAdapter job in tmp_path, TestClient, and `--check`; no remote service. |
| `modules/market_data_ingestion/tests/test_application_transports.py` | `M` | ROADMAP rev. 736: included in five-file focused suite, 30 passed; FakeExchangeAdapter, temporary stores, and in-process TestClient. |
| `modules/market_data_ingestion/scripts/benchmark_control_plane_slo.py` | `M` | ROADMAP rev. 741: shutdown ResourceWarning is counted in the characterization verdict; targeted local regression passed. This is not a production SLO acceptance. |
| `modules/market_data_ingestion/tests/test_control_plane_slo_benchmark.py` | `M` | ROADMAP rev. 741: four focused tests passed; synthetic shutdown warning fails the zero-warning verdict, API/CLI reads preserve registry state; temporary storage, fake adapter, in-process TestClient only. |
| `modules/paper_execution/tests/composition_owner_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/results_aggregator/schemas/strategy-audit-evidence-package.schema.json` | `M` | ROADMAP rev. 754: local contract fix removes an invalid per-metric-version uniqueness constraint while preserving unique metric IDs; not #250 acceptance. |
| `modules/results_aggregator/src/modular_results_registry/evidence_package.py` | `M` | ROADMAP rev. 754: builder and immutable model reject duplicate dataset/metric IDs; hashes and versions remain aligned by metric definition. |
| `modules/results_aggregator/src/modular_results_registry/schemas/strategy-audit-evidence-package.schema.json` | `M` | ROADMAP rev. 754: packaged runtime schema kept byte-identical to the release schema; bundled-schema test passes. |
| `modules/results_aggregator/tests/test_evidence_package_validation_extended.py` | `M` | ROADMAP rev. 754: focused serialization/schema and duplicate-ID regressions; 12 selected tests pass, candidate-only. |
| `modules/paper_execution/tests/disposable_executor_lifecycle_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/paper_allocation_commit_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/paper_allocation_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/test_composition_contract.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/test_composition_owner.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/test_disposable_executor_lifecycle_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/test_paper_allocation_commit_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `modules/paper_execution/tests/test_paper_allocation_reference.py` | `??` | ROADMAP rev. 596: local synthetic composition/lifecycle/allocation reference tests only; not production composition or #396 acceptance. |
| `research/strategy_factory/docs/ms5a_bounded_profile_protocol.md` | `M` | ROADMAP rev. 734: explicit #1190 STOP / `--plan-only` notice only; the rest of this file’s dirty protocol diff remains unverified. |
| `research/strategy_factory/src/strategy_factory/ms5a_profile_executor.py` | `M` | ROADMAP rev. 733: STOP guard only; the rest of this large dirty file is not thereby verified. |
| `research/strategy_factory/tests/test_ms5a_budget_probe_contract.py` | `M` | ROADMAP rev. 733: included in the two-module 188-test local result; candidate-only. |
| `research/strategy_factory/tests/test_ms5a_profile_executor_contract.py` | `M` | ROADMAP rev. 733: STOP-entry regression and synthetic module result; no owner/workload run. |
| `scripts/deterministic_executor_manager.py` | `??` | ROADMAP rev. 725: candidate-only reference manager; synthetic tests, no real backend/trust. |
| `scripts/external_publication_ledger.py` | `??` | TRACEABILITY rev. 697–698: expiry recheck and targeted suite (68 tests/24 subtests); production trust remains open. |
| `scripts/system_acceptance_stage1_lifecycle.py` | `??` | ROADMAP rev. 737: lifecycle order/rollback tests replace host, owner, HTTP and materializer; no deployment or real host startup. |
| `scripts/system_acceptance_stage1_service_manager.py` | `M` | ROADMAP rev. 737: tests patch server/host/owner effects; real service manager and data paths remain unverified. |
| `scripts/system_acceptance_stage1_systemd.py` | `??` | ROADMAP rev. 737: tests use dummy executable and `tmp_path`; no `systemctl` or host unit activation. |
| `src/modular_core/__init__.py` | `M` | ROADMAP rev. 750: Core lifecycle transaction-contract/coordinator exports; covered only by the narrow candidate synthetic suite, not production atomicity. |
| `src/modular_core/lifecycle.py` | `??` | ROADMAP rev. 750: canonical Core transaction Protocol/coordinator contract; provider/backend and Runner/Paper effects are absent. |
| `src/signal_bot/adapters.py` | `M` | ROADMAP rev. 735: current adapter diff exercised by mocked Binance/Telegram transport tests; no live endpoint call or credential read. |
| `src/signal_bot/cli.py` | `M` | ROADMAP rev. 735: current CLI diff exercised with temporary config and mocked state/clients/threads; no production configuration or `.env` read. |
| `src/signal_bot/config.py` | `M` | ROADMAP rev. 735: current config diff exercised with temporary YAML fixtures only. |
| `src/signal_bot/health.py` | `M` | ROADMAP rev. 735: current health diff exercised with mocked server plus ephemeral loopback-only HTTP tests; no external bind. |
| `tests/signal_bot/test_adapters.py` | `M` | ROADMAP rev. 735: changed tests pass; Binance/Telegram `urlopen` calls are patched. |
| `tests/signal_bot/test_cli.py` | `M` | ROADMAP rev. 735: changed tests pass; env values are synthetic and clients/state/threads are patched. |
| `tests/signal_bot/test_config_and_adapter.py` | `M` | ROADMAP rev. 735: changed tests pass; config is temporary YAML under pytest tmp_path. |
| `tests/signal_bot/test_control.py` | `M` | ROADMAP rev. 735: changed tests pass; Telegram transport is patched and SQLite state is under tmp_path. |
| `tests/signal_bot/test_health.py` | `M` | ROADMAP rev. 735: changed tests pass; server construction is mocked/local; no external request. |
| `tests/signal_bot/test_health_coverage_regression.py` | `M` | ROADMAP rev. 735: changed tests pass; HTTP test binds only loopback on an ephemeral port and closes server in `finally`. |
| `tests/test_deterministic_executor_manager.py` | `??` | ROADMAP rev. 725: synthetic reference-core tests; unresolved crash ambiguity remains fail-closed. |
| `tests/test_external_publication_ledger.py` | `??` | TRACEABILITY rev. 698: targeted ledger suite (68 tests/24 subtests); not CI/production acceptance. |
| `tests/integration/test_system_acceptance_stage1_lifecycle.py` | `??` | ROADMAP rev. 737: synthetic lifecycle test, included in 33 passes; host/materializer/HTTP effects are patched. |
| `tests/integration/test_system_acceptance_stage1_systemd.py` | `??` | ROADMAP rev. 737: synthetic unit-install test uses `tmp_path`; dummy executable is not run. |
| `web/market-data-console/e2e/paper.spec.ts` | `M` | ROADMAP rev. 738: current E2E diff typechecks only; no browser run. Existing lifecycle path is gated by an explicit test flag. |
| `web/market-data-console/e2e/serve_paper.py` | `M` | ROADMAP rev. 738: static review confirms synthetic in-memory Runner/positions and loopback-only bind; server/browser fixture was not launched. |
| `web/market-data-console/src/paper/PaperRuntimePage.tsx` | `M` | TRACEABILITY rev. 739: exact-lock full frontend tests/typecheck/build cover the local position filter/export slice; no production owner reader or #933/#396 acceptance, browser E2E, or usability/accessibility evidence. |
| `web/market-data-console/src/paper/client.test.ts` | `M` | ROADMAP rev. 738: included in focused 35-test suite with stubbed fetch; not live-data acceptance. |
| `web/market-data-console/src/paper/client.ts` | `M` | ROADMAP rev. 738: bounded read-only position request exercised with stubbed fetch; no production owner fetch. |
| `web/market-data-console/src/paper/contracts.ts` | `M` | ROADMAP rev. 738: bounded complete-snapshot parser tested locally; no authoritative owner data read. |
| `web/market-data-console/src/paper/positions.test.ts` | `??` | ROADMAP rev. 738: position filtering/export cases included in focused 35-test run; fixtures are synthetic. |
| `web/market-data-console/package-lock.json` | `M` | ROADMAP rev. 739: exact npm lock installed offline from local cache in a disposable copy; full 122-test suite, typecheck/build and E2E TypeScript check passed. No Linux-target receipt. |
| `web/market-data-console/package.json` | `M` | ROADMAP rev. 739: scripts/dependencies exercised against the exact lock in a disposable offline install; no target-host reconstruction. |
| `web/market-data-console/src/dashboard/DashboardApp.tsx` | `M` | ROADMAP rev. 738: Recovery route and reader-auth wiring rendered/tested locally; no live Control Plane. |
| `web/market-data-console/src/dashboard/navigation.ts` | `M` | ROADMAP rev. 738: Recovery route active/navigation mapping covered by local UI test and type/build. |
| `web/market-data-console/src/recovery/RecoveryPage.tsx` | `??` | ROADMAP rev. 738: truthful read-only capability display tested; default configuration remains unavailable, no recovery effects. |
| `web/market-data-console/src/recovery/client.test.ts` | `??` | ROADMAP rev. 738: synthetic capability/auth/error contract cases included in focused 35-test run. |
| `web/market-data-console/src/recovery/client.ts` | `??` | ROADMAP rev. 738: GET-only client and sanitized errors covered by local stubbed-fetch tests; no real endpoint. |
| `web/market-data-console/src/recovery/contracts.ts` | `??` | ROADMAP rev. 738: fail-closed configuration contract parser covered locally; no trust/owner readiness source. |
| `scripts/platform_release_manifest.py` | `??` | ROADMAP rev. 745: full 328-case local manifest contract suite and focused Ruff check/format passed under candidate `.venv`; this is structural candidate evidence only, not trusted source/lock/release acceptance. |
| `tests/test_platform_release_manifest.py` | `??` | ROADMAP rev. 745: all 328 tests passed under the candidate Python 3.12 environment with declared `jsonschema` extra; synthetic local contract suite, not CI or trusted release acceptance. |

### Other unverified candidate change (exclude from freeze pending review) (38)

| Path | Status | Evidence / disposition |
|---|---:|---|
| `.github/workflows/market-data-ingestion-ci.yml` | `M` | Dirty candidate configuration/input; exclude pending review and provenance mapping. |
| `applications/control_plane/uv.lock` | `??` | Dependency lock input; target-bound resolver provenance and full selected profile reconstruction remain open. |









| `modules/market_data_ingestion/uv.lock` | `??` | Dependency lock input; target-bound resolver provenance and full selected profile reconstruction remain open. |
| `modules/paper_execution/src/paper_execution/_composition_contract.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `profiles/component-inventory.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `pyproject.toml` | `M` | Dirty candidate configuration/input; exclude pending review and provenance mapping. |
| `research/strategy_factory/src/strategy_factory/ms5a_budget_probe.py` | `M` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `schemas/external-execution-receipt-v1.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/external-publication-receipt-v1.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/platform-compatibility-coverage-policy-v1.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/platform-release-manifest-v2.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/platform-release-manifest-v3.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/protected_controller_policy.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/results-registry-compatibility-policy-v1.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `schemas/web-console-release-contract-v1.schema.json` | `??` | Candidate release contract/profile input; structural/local tests do not establish trusted approval or release acceptance. |
| `scripts/candidate_evidence_bridge.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/compatibility_material_set.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/external_admission_ledger.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/external_execution_publisher.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/external_publication_receipt.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/fixed_workload_planner.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/platform_dependency_inputs.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/protected_admission_controller.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/release_evidence_handoff.py` | `M` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/release_obligation_report.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/validated_release_facts.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/verify_external_execution_receipt.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |
| `scripts/web_console_release_contract.py` | `??` | Untracked/modified release or execution tooling; no exact current-path verification is recorded here. |




| `tests/integration/test_external_execution_pipeline_contract.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |






| `tests/test_compatibility_material_set.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_external_admission_ledger.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_external_execution_publisher.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_external_execution_receipt.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_fixed_workload_planner.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_protected_admission_controller.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_release_evidence_transport_policy.py` | `M` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `tests/test_web_console_release_contract.py` | `??` | Dirty implementation or test path; no exact current-path/hash verification was located in the cited HQ records. |
| `uv.lock` | `??` | Dependency lock input; target-bound resolver provenance and full selected profile reconstruction remain open. |

### Generated/report artifact (retain; verify provenance and whether it belongs in release evidence) (4)

| Path | Status | Evidence / disposition |
|---|---:|---|
| `modules/market_data_ingestion/reports/dataset-storage-recovery-py312.json` | `??` | Untracked JSON report; no source freeze inclusion decision or evidence-chain verification made in this audit. |
| `modules/market_data_ingestion/reports/dataset-storage-smoke-py312.json` | `??` | Untracked JSON report; no source freeze inclusion decision or evidence-chain verification made in this audit. |
| `modules/market_data_ingestion/reports/market-data-control-plane-storage-py3.12.json` | `??` | Untracked JSON report; no source freeze inclusion decision or evidence-chain verification made in this audit. |
| `modules/market_data_ingestion/reports/shared-jobs-storage-pressure-py3.12.json` | `??` | Untracked JSON report; no source freeze inclusion decision or evidence-chain verification made in this audit. |

### Uncertain retained user content (preserve; exclude pending owner classification) (21)

| Path | Status | Evidence / disposition |
|---|---:|---|
| `.env.example` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `configs/signal_bot.example.yaml` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/control_ui/ACCESSIBILITY_HARDENING_V1.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/control_ui/PERFORMANCE_BASELINE.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/control_ui/SECURITY_HARDENING_V1.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/COMPATIBILITY_MIGRATION_INVENTORY.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/DECLARED_DEPENDENCY_INPUTS.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/EXTERNAL_EXECUTION_BOUNDARY_V1.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/EXTERNAL_EXECUTION_OWNER_ADMIN_DECISION_PACKET.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/ISSUE_387_DELIVERABLE_CROSSWALK.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/LINUX_OWNER_BOOTSTRAP_DECISION_PACKET.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/OPTION3_IMPLEMENTATION_EVIDENCE_CROSSWALK.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/PACKAGING_AUDIT_2026-10-23.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/PAPER_SUPPORT_PROFILE_DECISION.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/PLATFORM_RELEASE_AND_ROLLBACK_V2.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/WEB_CONSOLE_RELEASE_CONTRACT_V1.json` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/release/WEB_CONSOLE_RELEASE_CONTRACT_V1.md` | `??` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/research_runner/SECURITY.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/signal_bot/DEPLOYMENT.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |
| `docs/signal_bot/README.md` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |

| `web/market-data-console/src/paper/paper.css` | `M` | Existing dirty/local documentation, example configuration, or styling; provenance/desired inclusion not established. File contents were not inspected for secret values. |

## Release-gate crosswalk

The categories below follow the installed shipping-and-launch checklist. Generic example thresholds are not imported as project requirements; this crosswalk records only project evidence or explicit gaps.

| Gate category | Project evidence to consult | Freeze/readiness assessment |
|---|---|---|
| Code quality | `ROADMAP.md` revs. 727–733/750; `docs/hq/RELEASE_EVIDENCE_TRACEABILITY.md`; `docs/testing/TEST_ARCHITECTURE.md` | Focused candidate slices have recorded local results, including the narrow #395 coordinator contract (19 synthetic tests); this does not prove provider atomicity or the full dirty tree. The full dirty candidate has no exact-tree full-suite/build/typecheck record. Review and path-to-test mapping remain incomplete. |
| Security | `docs/release/EXTERNAL_EXECUTION_BOUNDARY_V1.md`; `docs/release/EXTERNAL_EXECUTION_OWNER_ADMIN_DECISION_PACKET.md`; `docs/research_runner/SECURITY.md`; HQ revs. 699/731/733/750 | Trusted grant authority, ledger/owner boundary, signing/key custody, provider/runtime trust and full dependency/security audit are not established. No real non-secret Option 3 controller/runner/key-custody/evidence-store references or assigned owners/admins are available; candidate trust/execution gates remain closed. |
| Performance | `docs/control_ui/PERFORMANCE_BASELINE.md`; MDI report paths listed above; `docs/hq/RELEASE_EVIDENCE_TRACEABILITY.md` | Candidate reports/baselines are artifacts, not approved deployment thresholds or exact-source release acceptance. No common operational performance gate is evidenced here. |
| Accessibility | `docs/control_ui/ACCESSIBILITY_HARDENING_V1.md` | Local documentation is present; current exact candidate accessibility validation and acceptance are unverified. No generic axe/Lighthouse result is inferred. |
| Infrastructure | `docs/release/PAPER_SUPPORT_PROFILE_DECISION.md`; `docs/release/PACKAGING_AUDIT_2026-10-23.md`; `docs/release/PLATFORM_RELEASE_AND_ROLLBACK_V2.md`; `docs/hq/DEPENDENCY_INPUTS_BASELINE_127C483.json`; `docs/hq/PAPER_SUPPORT_PROFILE_SELECTION_127C483.json`; HQ revs. 731/778–781 | Linux/VDS deployment and separate Darwin acceptance are selected classes, but host/image/runtime mapping and six typed locks are incomplete (three `uv.lock` files present); exact resolver/index/artifact provenance and clean reconstruction remain open. Baseline inventory added 2026-10-26: report for clean integration `127c483659fbb3acdfc90ba39ecb3af2b4c69616` covers 26 Python distributions plus Web Console package/lock; report SHA-256 `b309a0fba348a0bbd5c109a3a132b0542f9b624e017f252ed12a1876ad573687`, generator SHA-256 `73c582c9f724b5d4ee4998e70706c9c1d491c0e88a7cd1ec0ff6dd5649d7d91b`; clean-source validation passed. Profile-selection fragment added 2026-10-26: deterministic bytes against the same clean commit, 26/26 IDs match the baseline inventory and selected groups match owner choice/source groups: Core `backtest` + `signal-bot`, Market Data `interface` + `parquet`, Control Plane `interface`, System Acceptance `package-runtime-owner`; schema-subset validation passed. Fragment SHA-256 `51d6822fa901f12ae53f40a8626cc29e5c9607d566ee5f26f8ae67a6dc5b4478`, selection digest `e969c8b3e6da445fbf96c389f72f5637d186d114907ef99938c42088d12494e9`. This is selection only; no locks/resolution/reconstruction/provenance/candidate freeze/acceptance. Earlier records' Actions/exact-head observations remain dated historical. Verified read-only GitHub snapshot 2026-10-26: Actions `enabled=false`, `sha_pinning_required=false`; queued/in-progress runs empty; PR #1424/#1427 have no PR-triggered runs or combined status, while #1426 shows only governance SUCCESS and physical Mac SKIPPED with no combined status (partial first-page check response, not a full census). Runner groups #1/default (all, 7), #4/PC (all, 0), #5/MAC (all, 12) are unrestricted; selected #8 has 0 runners; #3/#6/#7 do not select this repository; all listed groups have `restricted_to_workflows=false`. Branch protection and rulesets return 403 with GitHub Pro/public-repository eligibility message while the repository is private; effective branch-rule enforcement is unavailable/unverified. Owner direction: retain private visibility and plan a subscription upgrade; owner/admin handles billing and performs it, Codex does not change the subscription or settings. This is not a completed upgrade or permission to enable Actions. Keep Actions disabled and the gate closed until upgrade is complete and a successful API read verifies protection. Public visibility is not selected. No gate changed. |
| Documentation | Candidate `docs/release/` packets and `research/strategy_factory/docs/ms5a_bounded_profile_protocol.md`; HQ `ROADMAP.md` / `RELEASE_CHECKLIST.md` | Dirty docs are listed and retained; owner intent/classification and exact agreement with frozen code are not established. Protocol prose does not create execution authority. |
| Monitoring / rollback | `docs/release/PLATFORM_RELEASE_AND_ROLLBACK_V2.md`; `scripts/system_acceptance_stage1_systemd.py`; `scripts/system_acceptance_stage1_service_manager.py`; HQ revs. 644/731/737 | Synthetic tests cover ordering and temporary unit generation only; no deployed monitoring/rollback, approved host mapping, runtime trust, or recovery authority is recorded. |
| Rollout / error budget | `docs/release/PLATFORM_RELEASE_AND_ROLLBACK_V2.md`; `docs/control_ui/PERFORMANCE_BASELINE.md`; HQ rev. 729 | No project-approved rollout/error-budget thresholds or production rollout evidence was found in the cited records. Do not borrow generic checklist numbers or infer rollout authorization. |

## Freeze prerequisites

1. Owner-classify the 145 dirty non-Graphify paths into the intended exact candidate, retained local material, generated evidence, or excluded work; preserve excluded user material without cleaning it.
2. Review every included source/config/schema/lock diff and map it to exact tests, lint/type/build results, security/performance/accessibility evidence where applicable, and provenance. Do not treat the focused HQ records as verification of whole files.
3. Resolve the selected 26-distribution profile inputs: six typed Python 3.12/3.13 bootstrap/build/runtime locks, target patches and host/ABI mapping, resolver/index/build-tool identity, artifact source/provenance, and clean reconstruction evidence.
4. Create a clean exact candidate only after classification and review; capture complete exact-head acceptance. The selected HQ snapshot records Actions disabled, and there is no release, deployment, SEARCH/HOLDOUT/PAPER, or workload authority. #1190 remains STOP; #395/#396/#933 remain unaccepted; #250 remains zero robustness/no HOLDOUT.
