# Compatibility and migration inventory for #387

**Status:** source inventory and proposal only; not an accepted compatibility contract, migration
plan, rollback guarantee, or release candidate. Source facts below were inspected from a separate
clean worktree at exact source commit `127c483659fbb3acdfc90ba39ecb3af2b4c69616`. No schema or
validator introduced by the uncommitted #387 candidate was used as evidence of existing product
behavior.

## Scope and count reconciliation

The Results Registry source tree at that commit contains **16 JSON Schema families**, each with one
repository schema and one Python package resource copy (32 schema files total). The corresponding
`catalog.json` also exists in both locations (2 more JSON resources). Its catalog lists 16 schema
entries. `list_schema_names()` returns 17 resource names because it includes the catalog itself, and
the current package test asserts that resource count; this does not make the catalog a seventeenth
schema family ([source catalog](../../modules/results_aggregator/schemas/catalog.json), [packaged
catalog](../../modules/results_aggregator/src/modular_results_registry/schemas/catalog.json),
[catalog loader](../../modules/results_aggregator/src/modular_results_registry/schema_resources.py#L29),
[resource-count test](../../modules/results_aggregator/tests/test_schema_catalog_integrity.py#L23)).
Accordingly, an expected count of “17 schema families / 34 schema files” does not match the exact
source inventory and must be reconciled rather than padded with an invented family.

The source schema directory is
[`modules/results_aggregator/schemas/`](../../modules/results_aggregator/schemas/); the installed
package resource directory is
[`modules/results_aggregator/src/modular_results_registry/schemas/`](../../modules/results_aggregator/src/modular_results_registry/schemas/).
For each row below, the filename exists at both paths and the listed SHA-256 is the raw-file digest
for both copies. The bytes were compared directly in the clean source worktree; every listed pair was
byte-identical at the inspected commit.

| Schema family | Declared `$id` | SHA-256 for each source and packaged copy |
| --- | --- | --- |
| `artifact-package.schema.json` | `urn:modular-results-registry:artifact-package:1` | `dd9c8fc97b8bc4585d2eb60c0da1a261856b6f4559b03b5af4f2bea128a28d08` |
| `artifact-record.schema.json` | `urn:modular-results-registry:artifact-record:1` | `e3b9d770f61fcbe5de355d989f2c6a2e55b0474ccd7e909d0591e0c6c8f23f06` |
| `audit-evidence-record.schema.json` | `urn:modular-results-registry:audit-evidence-record:1` | `c91ac3c4949708889f35e8243e68c604feec191dbf5f34c04d319171e0d83c5f` |
| `audit-log-event.schema.json` | `urn:modular-results-registry:audit-log-event:1` | `a892b3e5ece36a3e9dd9b358438d198fa16c9772eeaf87d340dd1f132d96b89f` |
| `backup-manifest.schema.json` | `urn:modular-results-registry:backup-manifest:1` | `29ecdaebc8d7b25965f811843e84a508c7697e04a74d6b39e4a7ae922944d2e7` |
| `configuration-record.schema.json` | `urn:modular-results-registry:configuration-record:1` | `8534af92bcf2e08e9875eb11fb680118b7f7afe392e4b5130036ae255fb2d056` |
| `dataset-record.schema.json` | `urn:modular-results-registry:dataset-record:1` | `b508a16690cb91a4a3fa679cce847fd2c7901d14ee31e0763ca13a8e66a4bcb3` |
| `experiment-record.schema.json` | `urn:modular-results-registry:experiment-record:1` | `a0e9ab2d7c977355b0091bdf131e89576090848a3456d8a1dc08942453c6033a` |
| `metric-definition-record.schema.json` | `urn:modular-results-registry:metric-definition-record:1` | `0d16d223f5464417c5d812919fef467aba970793b6d6bd25c1083b1f9acbe6b4` |
| `metric-snapshot.schema.json` | `urn:modular-results-registry:metric-snapshot:1` | `4d963706165c3634035374da5cd700d382c80f1452e13d165d6594ef11cab555` |
| `rebuild-bundle.schema.json` | `urn:modular-results-registry:rebuild-bundle:4` | `deb31205e98fe83294fd26aed2f5c213ae153e4247a70fe4409a3b13ad4e0d05` |
| `registration-package.schema.json` | `urn:modular-results-registry:registration-package:2` | `f23b2ce2521ade91dc9647cd9706cefe04f46e76cd5e49443fc3f9ec4ded6219` |
| `role-decision.schema.json` | `urn:modular-results-registry:role-decision:1` | `b689a94ec6857287be4bf561fcb9db843c47055592cb0b6fb616eaf743da5848` |
| `run-record.schema.json` | `urn:modular-results-registry:run-record:1` | `068a065fc474452c082dd5ae0d708116ed1bad3e23d115eeb768f00601a2e6d1` |
| `strategy-audit-evidence-package.schema.json` | `urn:modular-results-registry:strategy-audit-evidence-package:2` | `223bded172006fb9ec5bae1afdba7aad71735180a1107a2a991ad429460d75c3` |
| `structured-log-event.schema.json` | `urn:modular-results-registry:structured-log-event:1` | `3d3c57309bc9dfcef40fa65c8fbcf3220033bebaf568dc29302898b4705b924d` |

The terminal integer in each `$id` is a declared schema identity. Equal bytes across the two
locations prove source/package copy consistency at this SHA; neither fact establishes compatibility
with another schema version, producer, consumer, persisted record, or release.

## Source-visible readers, writers, and version checks

- `schema_resources.py` loads the packaged `catalog.json`, checks its canonical catalog hash, exact
  field set, sorted and unique paths/IDs, equality between listed paths and packaged resources, and
  each resource's `$id`; `read_schema_bytes`, `read_schema`, and `schema_hash` expose those package
  resources ([catalog validation and resource API](../../modules/results_aggregator/src/modular_results_registry/schema_resources.py#L33)).
  The package exports those APIs ([package exports](../../modules/results_aggregator/src/modular_results_registry/__init__.py#L58)).
  This is catalog/resource integrity checking; it does not itself demonstrate migration compatibility.
- Existing source tests compare bundled resources byte-for-byte with repository schema copies and
  check selected payload contracts ([copy test](../../modules/results_aggregator/tests/test_schema_resources.py#L23),
  [catalog test](../../modules/results_aggregator/tests/test_schema_catalog_integrity.py#L23)). Runtime
  models also validate schema/version identifiers against a string-format contract
  ([identifier validation](../../modules/results_aggregator/src/modular_results_registry/model_common.py#L132)).
  No complete family-by-family producer/consumer compatibility matrix is established here; do not
  infer one from filenames or `$id` values.
- The Results Registry's **SQLite storage schema** is a separate contract from the 16 JSON Schema
  families. Source declares current version 4 and explicitly handles fresh bootstrap plus forward
  paths 1→2, 2→3 and 3→4; the v2→v3 path fails closed if old metrics exist without immutable metric
  definitions ([storage version](../../modules/results_aggregator/src/modular_results_registry/storage_common.py#L14),
  [migration implementation](../../modules/results_aggregator/src/modular_results_registry/storage_schema.py#L10),
  [preflight](../../modules/results_aggregator/src/modular_results_registry/storage_lifecycle.py#L130)).
  These code paths are not proof of safe reverse migration or compatibility with a selected prior
  release.
- Market Data's SQLite dataset registry declares version 1. Version 0 can be adopted after table
  validation; other versions except 1 are rejected, not migrated ([registry version contract](../../modules/market_data_ingestion/src/market_data_ingestion/registry_schema.py#L10),
  [adopt/reject behavior](../../modules/market_data_ingestion/src/market_data_ingestion/registry_schema.py#L79)).
- System Acceptance's SQLite store declares version 1 and rejects any version set other than exactly
  `(1,)` ([acceptance store initialization](../../applications/system_acceptance/src/modular_system_acceptance/storage.py#L25),
  [version check](../../applications/system_acceptance/src/modular_system_acceptance/storage.py#L79)).
- Market Data has a legacy download-config conversion that emits the typed request format and uses
  safe, no-overwrite output handling ([converter entry point](../../modules/market_data_ingestion/src/market_data_application/download_config_migration.py#L42),
  [safe output path](../../modules/market_data_ingestion/src/market_data_application/download_config_migration.py#L354)).
  This is input conversion, not an installed-release upgrade or database rollback contract.

## Component-to-format coverage for the documented PAPER scope

The separate support-profile proposal names the mandatory execution chain, optional Signal Bot,
Research/Backtest, Results Registry, Strategy Factory/Audit, System Acceptance/Recovery, Control
Plane/UI, and 26 Python distributions ([scope and inventory references](PAPER_SUPPORT_PROFILE_DECISION.md#L11)).
The table below is an initial source-backed review of stateful and exchanged formats within that
scope, against source commit `127c483659fbb3acdfc90ba39ecb3af2b4c69616`. Rows are not a complete
owner attestation: the candidate's exact set of 26 distributions is
[`profiles/component-inventory.json`](../../profiles/component-inventory.json#L1). Each listed
distribution and capability still needs an accountable owner to confirm whether it owns additional
persisted or wire formats. `unknown-blocking` is the release disposition where source facts alone do
not establish compatibility with a selected predecessor.

| Owner / scope surface | Exact format and identity facts from source | Source-visible direction and behavior | Release disposition |
| --- | --- | --- | --- |
| Paper Execution recovery snapshot | Root snapshot `schema_version` is string `4`; writer includes exact `module_version`, `configuration_hash`, and canonical `state_hash`. Reader accepts schema strings `1`–`4`; v2–v4 include orchestration state, v3–v4 event fingerprints, v4 timestamps. Reader requires current module version and caller-supplied exact configuration hash ([version sets](../../modules/paper_execution/src/paper_execution/recovery_types.py#L10), [writer](../../modules/paper_execution/src/paper_execution/recovery_snapshot.py#L13), [reader identity checks](../../modules/paper_execution/src/paper_execution/recovery_restore.py#L30)). | `snapshot_execution_state` produces; `restore_execution_state` consumes. Legacy versions are accepted conditionally under the current module/config identity; this does not prove migration across module versions or configs. | `unknown-blocking` |
| Paper Execution protection and orchestration state | Protection writer emits schema `5`; reader accepts `1`–`5`, with version-dependent required fields and default baseline quantity for v1–v4. Orchestration substate writes/reads schema `1` ([protection reader](../../modules/paper_execution/src/paper_execution/protection.py#L235), [writer](../../modules/paper_execution/src/paper_execution/protection.py#L416), [orchestration state](../../modules/paper_execution/src/paper_execution/orchestration_state.py#L33)). | Same component serializes and restores state; source permits historical shape parsing, but no predecessor/module/config binding or end-to-end rollback guarantee is established here. | `unknown-blocking` |
| Paper Execution ↔ Decision Router plan evidence | Router envelope schema string `1`, payload SHA-256 over canonical plan JSON, plan/audit hashes, scope and validity bounds; constructor rejects unsupported schema and mismatched payload hash ([contract](../../modules/paper_execution/src/paper_execution/router_adapter.py#L128)). | Router evidence is accepted by Paper Execution and projected into journal payload; source contract is directional but release-version compatibility is not established. | `unknown-blocking` |
| Trading Runner configuration, runtime checkpoint and operator lifecycle store | Config schema string `1`, exact root allowed/required fields, and execution mode must be `paper` ([config](../../applications/trading_runner/src/modular_trading_runner/config.py#L16)). Checkpoint schema `1` binds run ID, config hash, module graph hash and integrity hash; restore rejects mismatch ([checkpoint](../../applications/trading_runner/src/modular_trading_runner/checkpoint.py#L28)). Operator store schema `1` binds runtime ID/config hash and rejects mismatch ([lifecycle store](../../applications/trading_runner/src/modular_trading_runner/operator_lifecycle.py#L21)). | Runner writes and restores checkpoint/lifecycle payloads; identity mismatch fails closed. Config parser consumes typed config. A source-visible version is not evidence of predecessor compatibility. | `unknown-blocking` |
| Market Data dataset registry, manifests and spool | SQLite registry schema `1` permits validated version `0` adoption and `1`, rejecting other nonzero versions ([registry policy](../../modules/market_data_ingestion/src/market_data_ingestion/registry_schema.py#L10)). Dataset manifest schema version is `2`; package, normalization, validation and hash policy identities are separately declared ([version constants](../../modules/market_data_ingestion/src/market_data_ingestion/version.py#L1)). Spool schema is `2` and rejects a stored different version ([spool](../../modules/market_data_ingestion/src/market_data_ingestion/spool.py#L18)). | Market Data writes manifests/registry/spool and reads descriptors/partitions through manifest and streaming readers. Version 0 registry adoption is source behavior, not a release migration approval; spool has no forward migration in the inspected path. | `unknown-blocking` |
| Research Runner repository and output artifacts | Repository declares a `schema_version` metadata key and contains run claims; output manifest emits `MANIFEST_SCHEMA_VERSION` and parser enforces it ([repository](../../research/experiment_runner/src/modular_research_runner/repository.py#L219), [output manifest](../../research/experiment_runner/src/modular_research_runner/output_manifest.py#L201)). | Research Runner writes run claims and manifests and reads them for resumption/validation. The complete on-disk table/manifest predecessor matrix and owner-state reopen behavior have not been established in this inventory. | `unknown-blocking` |
| Strategy Factory and strategy/module configuration/state | The 26-distribution scope includes Strategy Factory and analytical/strategy modules; source exposes module `state_schema_version` and metric schema identity in adapter-derived records ([adapter mapping](../../research/strategy_factory/src/strategy_factory/adapters.py#L827)). Per-module state/config formats are package-owned and vary; no complete owner-by-owner format enumeration or version transition matrix is established here. | Strategy Factory consumes module-produced state/metrics and emits registration/artifact records; the inspected reference demonstrates a version field, not a common compatibility mechanism. | `unknown-blocking` |
| Strategy Audit validation and governance | Audit contracts include schema `1`; governance SQLite metadata requires exactly `schema_version='1'`; control evidence has a declared schema check ([contracts](../../research/strategy_audit_validation/src/strategy_audit_validation/contracts.py#L46), [store](../../research/strategy_audit_validation/src/strategy_audit_validation/governance_store_base.py#L25), [evidence schema check](../../research/strategy_audit_validation/src/strategy_audit_validation/governance_evidence.py#L48)). | Auditor/owner store writes and validates governance evidence and records. No selected predecessor, migration path, or rollback behavior is proved. | `unknown-blocking` |
| Results Registry | Sixteen JSON Schema families and their paired source/package hashes are preserved in the table above. SQLite store schema is version `4`, with source-defined forward migrations `1→2→3→4` and fail-closed preconditions noted above ([version](../../modules/results_aggregator/src/modular_results_registry/storage_common.py#L14), [migration](../../modules/results_aggregator/src/modular_results_registry/storage_schema.py#L10)). | Registry consumes/writes schema-backed records and its SQLite store. Forward migration code exists; reverse migration and selected-predecessor compatibility are unproven. | `unknown-blocking` |
| Signal Bot state and operator ledger | State SQLite setup inserts schema versions `1` and `2` and creates signal/cursor/preferences/metadata/counter tables; the ledger creates request/audit/status tables ([state](../../src/signal_bot/state.py#L112), [operator ledger](../../src/signal_bot/operator_ledger.py#L52)). The insertion pattern does not establish a single exact version gate or transition semantics. | Signal Bot writes and reads local owner state and operator records; source inspection does not establish migration/reopen compatibility for a prior release. | `unknown-blocking` |
| System Acceptance store and serialized run payload | SQLite schema version `1`; startup requires version tuple exactly `(1,)`. Run payloads are decoded into typed acceptance snapshots ([store version and tables](../../applications/system_acceptance/src/modular_system_acceptance/storage.py#L25), [exact version check](../../applications/system_acceptance/src/modular_system_acceptance/storage.py#L79)). | Acceptance owner writes and reads runs/mutations/audit entries; non-1 schema state is rejected in the inspected path. No release rollback/reopen proof exists. | `unknown-blocking` |
| System Recovery package and snapshot entries | Recovery manifest schema integer `1`, exact source commit and release ID, sorted unique logical IDs/restore targets; entries carry owner/class/kind, relative paths and (for copied bytes) SHA-256/size ([manifest contract](../../applications/system_recovery/src/system_recovery/manifest.py#L34)). Snapshot/restore code handles SQLite and regular-file payloads with integrity checks ([snapshot](../../applications/system_recovery/src/system_recovery/snapshot.py#L52), [restore](../../applications/system_recovery/src/system_recovery/restore.py#L28)). | Recovery writes and verifies a package, then restores selected entries. This establishes package integrity behavior, not semantic compatibility of each owner's restored state or release rollback safety. | `unknown-blocking` |
| Control Plane Backtest store and API/UI wire formats | Backtest SQLite initialization creates the durable jobs table and sets `PRAGMA user_version=1` ([store initialization](../../applications/control_plane/src/modular_control_plane/backtest.py#L400)). Control Plane exposes typed API contracts and connects UI capabilities; a complete endpoint-by-endpoint versioned wire inventory is not established here. | Backtest worker writes/reads durable job records; API/UI exchange typed requests and responses. Schema `1` alone does not establish reopen compatibility or API predecessor support. | `unknown-blocking` |

### Exact provider coverage crosswalk

The following crosswalk carries the review through every provider declared in the `python_distributions`
role and the separate Market Data Console capability in the component inventory
([provider declaration](../../profiles/component-inventory.json#L1)). “Not established” means this
source scan did **not** establish that the component has no persisted or wire format; accountable
owner confirmation is still required. Even where a format is listed, source inspection is not owner
confirmation and does not establish predecessor compatibility. Every row therefore remains
`unknown-blocking` pending owner evidence and an approved predecessor.

| Declared provider | Format coverage found in this inventory | Release disposition |
| --- | --- | --- |
| `modular-control-plane` | Backtest SQLite store (`user_version=1`) and typed API/UI exchanges are covered above; endpoint-by-endpoint wire coverage is incomplete. | `unknown-blocking` |
| `modular-market-data-ingestion` | Dataset registry, dataset manifests and spool are covered above; legacy download-config conversion is also described above. | `unknown-blocking` |
| `modular-observability` | Typed observation and alert contracts carry schema identities and have JSON serialization helpers ([contracts](../../applications/observability/src/modular_observability/contracts.py#L177), [serialization](../../applications/observability/src/modular_observability/serialization.py#L1)); durable ownership and complete wire coverage are not established. | `unknown-blocking` |
| `modular-results-registry` | Sixteen JSON Schema families and SQLite store version 4 with forward migrations are covered above. | `unknown-blocking` |
| `modular-risk-supervisor` | Supervisor state writer/reader uses `state_schema_version="2"` ([state persistence](../../applications/risk_supervisor/src/modular_risk_supervisor/supervisor.py#L604)); transition compatibility and full checkpoint coverage are unestablished. | `unknown-blocking` |
| `modular-session-range` | Module state uses schema `2`; reader accepts `1` and `2`, with v2 fingerprint checks ([state writer](../../modules/session_range/src/modular_session_range/module.py#L353), [reader](../../modules/session_range/src/modular_session_range/module.py#L376)). | `unknown-blocking` |
| `modular-strategy-audit-validation` | Audit contracts, governance SQLite metadata and control-evidence schema check are covered above. | `unknown-blocking` |
| `modular-strategy-core` | Not established; absence of persisted or wire formats is not established, and owner confirmation is required. | `unknown-blocking` |
| `modular-strategy-entry-model` | Serialized module state uses `state_schema_version="2"` and reader requires that version; setup payloads carry a separately checked source schema ([state](../../modules/entry_model/src/modular_modules/entry_model/state.py#L63), [setup](../../modules/entry_model/src/modular_modules/entry_model/setup.py#L47)). | `unknown-blocking` |
| `modular-strategy-factory` | Adapter-derived records expose module state-schema and metric-schema identities; the inventory does not establish a complete per-module format set. | `unknown-blocking` |
| `modular-strategy-fibonacci` | Core adapter declares state schema `2` and checks it when restoring adapter state ([adapter](../../plugins/fibonacci/src/modular_strategy_fibonacci/core_adapter_v4.py#L25)); broader plugin-owned formats are unestablished. | `unknown-blocking` |
| `modular-strategy-fvg` | State writer identifies schema `4` and source defines prior schema identities `3`, `2`, and `1` ([state identities](../../plugins/fvg/src/modular_strategy_fvg/module_shared.py#L96)); this does not establish release migration compatibility. | `unknown-blocking` |
| `modular-strategy-market-regime` | State writer uses schema `2`; restore accepts legacy schema `1` and current `2` ([state contract](../../modules/market_regime/src/market_regime/state.py#L15)). | `unknown-blocking` |
| `modular-strategy-paper-execution` | Recovery snapshots, protection/orchestration state, and accepted Decision Router plan evidence are covered above. | `unknown-blocking` |
| `modular-strategy-position-exit-management` | Serialized state uses schema `2` and the reader requires that identity ([state](../../modules/position_exit_management/src/modular_strategy_position_exit_management/state.py#L275)). | `unknown-blocking` |
| `modular-strategy-position-scaling` | State writer uses schema `4`; restore accepts schema `1`–`4` with version-specific handling ([state](../../modules/position_scaling/src/modular_strategy_position_scaling/state.py#L19)). | `unknown-blocking` |
| `modular-strategy-protection-target` | Module manifest declares state schema `2` ([module declaration](../../modules/protection_target/src/modular_strategy_protection_target/module.py#L88)); source scan did not establish complete persisted-state identity or transition behavior, so owner confirmation remains required. | `unknown-blocking` |
| `modular-strategy-research-runner` | Repository run-claim metadata and output manifests are covered above; complete predecessor and reopen behavior is not established. | `unknown-blocking` |
| `modular-strategy-risk-request-composer` | Module state uses schema `1` and its reader checks that identity ([state writer and reader](../../modules/risk_request_composer/src/modular_strategy_risk_request_composer/module.py#L414)); the module also consumes separately versioned feature inputs. | `unknown-blocking` |
| `modular-strategy-risk-sizing` | Not established; absence of persisted or wire formats is not established, and owner confirmation is required. | `unknown-blocking` |
| `modular-strategy-setup-adapter` | Adapter state uses `state_schema_version="1"`; output/setup contracts also carry schema `1` ([state](../../modules/setup_adapter/src/modular_setup_adapter/state.py#L34), [outputs](../../modules/setup_adapter/src/modular_setup_adapter/outputs.py#L35)). | `unknown-blocking` |
| `modular-strategy-trade-plan-router` | The Decision Router plan-evidence envelope accepted by Paper Execution is covered above; provider-side coverage and release compatibility remain unconfirmed. | `unknown-blocking` |
| `modular-strategy-wave-analysis` | Serialized state uses schema `1` and restore rejects other values ([state](../../modules/wave_analysis/src/wave_analysis/state.py#L14)); the complete persistent feature contract is outside this inventory. | `unknown-blocking` |
| `modular-system-acceptance` | SQLite store version 1 and typed serialized run payloads are covered above. | `unknown-blocking` |
| `modular-system-recovery` | Recovery manifest schema 1 and integrity-checked SQLite/regular-file snapshot entries are covered above. | `unknown-blocking` |
| `modular-trading-runner` | Configuration schema, runtime checkpoint and operator lifecycle store are covered above. | `unknown-blocking` |
| `@module-strategy/market-data-console` (Market Data Console capability) | Console API clients and typed contracts exchange JSON carrying `schema_version: 1` in acceptance, strategy, backtest and event surfaces ([acceptance contracts](../../web/market-data-console/src/acceptance/contracts.ts#L29), [strategy contracts](../../web/market-data-console/src/strategy/contracts.ts#L45), [backtest client](../../web/market-data-console/src/backtest/client.ts#L110), [event contracts](../../web/market-data-console/src/events/contracts.ts#L4)). This is inspected wire evidence; a complete capability-wide endpoint inventory and browser-persisted-state review are not established. | `unknown-blocking` |

### Owner coverage and disposition gate

This matrix is deliberately conservative. It records selected concrete source formats, not a claim
that every persisted file, cache, IPC/API message, configuration, plugin state, or owner boundary
across all 26 distributions has been enumerated. Before candidate acceptance, each declared
distribution and capability must have an owner-confirmed format row or an explicit “no persisted or
wire format” disposition, with producer(s), consumer(s), exact identity, and tested transition
behavior. The owner must then select one of `migrate`, `unchanged-compatible`, `reject`, or
`unknown-blocking` for every predecessor-to-candidate relation. Until that coverage and evidence
exist, all release-level dispositions remain `unknown-blocking`; versions or source presence alone
cannot change them.

## Proposal for the #387 compatibility contract

The candidate should bind each supported schema family to its exact source/package digest, declared
ID, owner, relevant producer and consumer components, and explicit read/write compatibility claims.
Keep JSON payload schemas distinct from SQLite store versions, configuration conversion, and
System Recovery package formats. Require migration declarations to name exact predecessor and
target identities, preconditions, owner authority, state effects, backup/restore boundary, and
post-migration verification. A rollback claim must say whether it is a reversible down-migration or
restore to a predecessor-bound clean root; do not treat process rollback as data rollback.

This is a proposal for the release contract, not an assertion that all those relations are known. The
owner must select the supported predecessor, required compatibility promises, migration policy, and
whether any one-way transition is acceptable. Where current source does not prove semantics, retain
an explicit unknown/blocking declaration instead of inferring compatibility from identical schema
copies, an ID suffix, or a migration function's presence.

For a migrating relation, the rollback declaration's `from_candidate` uses the literal selector
`"self"` to mean the enclosing candidate. The selector is included as ordinary rollback content in
the candidate identity hash; it is not replaced with, or excluded from hashing in favor of, the
candidate digest. This keeps candidate identity acyclic while still binding the rollback claim to
the enclosing candidate. The schema continues to accept a 64-character candidate digest in this
field for pending legacy declarations, but migration validation requires `"self"`.

The local v2 candidate schema requires one coverage row for every source-inventoried Python
distribution and Market Data Console provider. A source-pinned policy derives an exact evidence gate
for each provider, and the profile must map every gate/component pair to an acceptance environment.
`unknown-blocking` rows are valid while pending and carry no inferred format facts. Confirmed
inventories require stable format IDs, producer/consumer IDs, and explicit predecessor-to-target
relations. Confirmation evidence lives outside candidate bytes and binds to a digest of the
normalized claim subject plus an explicit environment and runtime scope, avoiding
candidate-ID/evidence-ID cycles and preventing one environment/runtime pass from standing in for
another. Python scope keys expand each provider's acceptance environment across the profile's exact
Python runtime IDs. The Market Data Console has no runtime ID in the profile (its npm resolver
version is not a runtime declaration), so its runtime scope is explicitly `unknown-blocking` and
cannot support an accepted decision until the profile names the relevant runtime. Every declared
predecessor relation must cover every derived scope, and each subject/scope requires
exactly one passing evidence row. Migrations bind to declared migration and rollback IDs. The Results
Registry source/package schema binding remains a separate
static integrity obligation; candidate flags and free-text assertions cannot create evidence gates
or substitute for typed provider coverage. Candidate owner labels are structural references only;
the external trusted verifier remains the authority for owner attestation and accepted decisions.

The current relation fields still do not resolve predecessor release material: `candidate_id` is
only a string, and `format_id` plus the target descriptor digest do not bind predecessor manifests,
target contract bytes, or the exact shipped artifact. The validator therefore rejects an accepted
format relation until that resolver contract exists. This is an architectural blocker, not completed
predecessor binding. Completion requires a bundle/verifier contract
that, at admission issuance, resolves immutable predecessor and target material and computes/binds
their exact material-set digest in the existing admission `material_digest` (already included in
`admission_digest` and bound by receipts; no additional admission field is needed). The resolver
must recompute and cross-check candidate/source/component identities against the resolved bytes.
Because the predecessor envelope has no top-level `release_id`, the protected registry must
separately establish the relation-label-to-manifest association. The resolver must bind the
predecessor artifact and format contract; for the target,
it must resolve exact source-or-bundle contract and artifact references, verify their bytes, and
check the parsed format identity against the declared producer/consumer and operation. A digest by
itself is insufficient; the trusted verifier must authenticate the predecessor release decision.

One deliberately narrow local draft is a pure material-set validator for a JSON Schema member in a
wheel. Its canonical, closed descriptor stays outside candidate bytes and binds
`compatibility_subject_sha256` plus a `materials` object with exactly five roles: predecessor manifest digest, predecessor component and
wheel reference, predecessor schema member digest/path, target wheel reference, and target schema
member digest/path. The helper accepts caller-supplied byte snapshots only; it performs no fetch,
registry lookup, signing, authentication, or acceptance update. It recomputes candidate/profile and
relation identities, checks exact wheel membership and component identities, and validates each
schema `$id` against the declared format ID. A predecessor `decision: accepted` is only an
unauthenticated manifest claim here; registry binding and authentication remain required. It is a
structural draft and does not authenticate source provenance or a predecessor decision. For this draft binding, `candidate_digest` and
`profile_digest` are SHA-256 of the complete frozen target candidate and profile objects encoded
with the release validator's canonical JSON function; the separate IDs remain their canonical
self-identity hashes. The target admission `source_sha` must also equal the frozen candidate source
commit, and a receipt's `source_sha` claim is compared to the immutable admission.

The draft adapter workload spec is closed over adapter, operation, version, and an explicitly
supplied lowercase `implementation_sha256`; the admission `workload_digest` hashes that spec with
the release validator's canonical JSON function. Selecting and protecting the source-byte digest
remains an external responsibility. This local helper does not prove the supplied implementation
digest's provenance and does not self-hash its own source. Its receipt check compares existing
payload claims to the immutable admission digest but does not verify a signature or provider.

The predecessor envelope currently has no top-level `release_id`. Therefore, even after recomputing
its candidate/profile identities and checking the relation's predecessor candidate/source fields,
the helper cannot authenticate the relation's `release_id` label against those bytes. The association
between that label and the resolved immutable manifest remains an explicit protected-registry
blocker; it must be established by the future external resolver.

**Owner decision (2026-10-20): no supported predecessor release, authenticated acceptance record, or trust root has been selected.** Keep the predecessor-material and compatibility-acceptance gates fail-closed until owners/admins make and provision that decision. The local draft does not imply or nominate a predecessor.

### Unaccepted local resolver proposal

This is a local design proposal only; it is not an approved contract and does not change the current
accepted-path fail-closed behavior. Resolve predecessors only through the protected
`AcceptedReleaseRegistry`, keyed by an owner-approved release identity. The resolver must not follow
candidate-supplied URLs or mutable branch, tag, or `latest` references. It must authenticate the
registry record and establish that its approved release identity maps to the resolved manifest;
then cross-check candidate, source commit, component, and accepted decision bindings against the
relation before trusting any material. The manifest bytes alone cannot authenticate that release
label because the envelope contains no top-level `release_id`.

#### Protected orchestration dependency order

The recommended protected flow is: (1) resolve the owner-approved predecessor and frozen target
snapshots outside candidate control; (2) preflight the exact material and workload; (3) issue
admission from trusted expectations; (4) execute through the existing isolated path; (5) authenticate
the receipt against the immutable admission and protected policy before using it, requiring
`outcome=passed` and all required outputs; (6) call the pure material helper only with the exact
frozen snapshots and verifier-returned payload; (7) emit protected, immutable evidence binding the
candidate, profile, relation and scope to the admission and receipt digests, output digests, workload,
and predecessor provenance; and (8) only then allow the acceptance boundary to require that
authenticated evidence for every required relation.

The current helper checks integrity and schema identity only. It does not establish JSON Schema
semantic compatibility, migration or rollback behavior, or actual runtime use. A Python result or
dataclass is not an authentication boundary, and neither a generic ledger digest nor a signed
receipt alone proves material validation. These checks and evidence issuance must not be called
directly from candidate-controlled `validate_candidate`.

**Owner decision (2026-10-20): no predecessor or trust root has been selected.** Therefore resolver,
admission, and evidence acceptance remain unimplemented and fail-closed; this recommended sequence
is not an implemented architecture.

The resolved predecessor manifest and its exact shipped artifact and format contract must be
immutable, digest-checked, and identity-checked against that record. Resolve the target contract and
declared shipped component artifact from the frozen source or candidate bundle, verify their exact
bytes and declared digests, and verify that the contract belongs to that artifact. Parse both sides
to confirm the typed producer/consumer format and operation. Start with one
explicitly supported typed format adapter; unsupported formats remain blocking. Material digests
must form an acyclic graph: compatibility claims cannot hash their own evidence or receipt. At
admission issuance, bind the normalized compatibility subject, scope, and exact resolved
material-set digest through the existing `material_digest` field; then bind a controller-signed
execution receipt to that immutable admission. The independent verifier checks the receipt against
the admission and resolved materials.

Essential negative tests cover unregistered or unauthenticated predecessors; revoked trust;
release/candidate/source/component mismatches; mutable or candidate-directed locators; missing,
tampered, or substituted manifest, contract, and artifact bytes; unsupported format or operation;
digest cycles; and receipts detached from the claim, scope, or material. Before implementation,
owners and administrators must decide the approved predecessor set, trust root and revocation
policy, authoritative predecessor and target contract/artifact sources, compatibility promises and
runtime/environment scope, approved fixtures, and the protected service that owns registry
resolution and admission/receipt signing.

## Required acceptance evidence

Before this inventory can support a release decision, evidence must be bound to one frozen candidate
and approved profile and must:

1. reconcile the 16 cataloged families against the full supported component inventory, complete the
   owner-by-owner persisted/wire/configuration-format coverage gate above, and resolve the separately
   reported “17 families” count discrepancy without inventing a schema;
2. verify exact source/package copies and IDs from the frozen source and built package artifact;
3. identify the selected predecessor and validate the compatibility declarations against real
   producer/consumer contracts and supported persisted formats;
4. exercise each declared migration from its exact predecessor fixture, including fail-closed
   preconditions, preservation/integrity checks, and restore or rollback behavior on the supported
   environment; and
5. retain sanitized, candidate-bound evidence for migration outcome, post-migration owner reopen,
   reconciliation and resulting state identity.

No clean-release candidate, supported predecessor selection, end-to-end upgrade, rollback or
migration acceptance is established by this source inventory. It is documentation only; it does not
authorize data access, deployment, CI, installation, release, or a claim that #387 is complete.
