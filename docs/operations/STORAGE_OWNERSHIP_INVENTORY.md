# Storage ownership, durability, and protected identity inventory

Status: **Active fail-closed operational contract**  
Parent: #390 / GAP-013  
Tracking: #518, #542  
Canonical review base: `integration/modular-strategy-full-rc`

See also: [BACKUP_DEPENDENT_PRUNING_ADMISSION.md](BACKUP_DEPENDENT_PRUNING_ADMISSION.md).

## Purpose

This document inventories storage by authoritative owner and durability semantics. It is an input to
capacity, retention, pruning and recovery policy. It does **not** authorize deletion.

The project is identity-first: age, size, extension, pathname or filesystem location is never
sufficient evidence that data is safe to remove. A storage identity is pruneable only when its owner
contract explicitly proves the concrete identity is disposable/reconstructible and the applicable
retention safety gates pass.

Unknown ownership or retention semantics are fail-closed: **do not delete**.

## Classification vocabulary

| Class | Meaning | Automatic cleanup default |
| --- | --- | --- |
| `immutable-lineage` | Source/normalized/registered data or manifest required to reproduce a registered dataset | **forbidden** |
| `immutable-evidence` | Accepted run/release/audit/result evidence bound to immutable identity | **forbidden** |
| `durable-recovery` | State required to resume/reconcile an in-progress or accepted owner lifecycle | **forbidden** |
| `durable-audit` | Mutation/audit/event history required by an owner contract | **forbidden** |
| `reconstructible-cache` | Derived local state with an authoritative source and proven deterministic rebuild path | owner policy required |
| `temporary` | Atomic temp, staging, lock, scratch, build, or run-isolated workspace state | validated owner/infra cleanup only |
| `ci-diagnostic` | GitHub Actions diagnostics associated with a workflow result | only accepted bounded selector/authorization |
| `unresolved` | Storage exists but concrete retention/reconstruction semantics are not proven | **forbidden** |

## Protected identity rule

The following remain protected independently of physical path:

- accepted full-RC/release/security/heavy evidence and exact run/commit binding;
- registered market-data identity `dataset_id + dataset_version + dataset_hash`;
- immutable raw/normalized/derived source lineage referenced by a registered descriptor/manifest;
- Research experiment/run/result/final-test state needed for restart, fencing, replay or evidence;
- System Acceptance run, mutation/audit and recovery identities;
- current Core/Paper checkpoint, journal, OMS/accounting/order/position and reconciliation state;
- Trading Runner runtime/operator/checkpoint/hash-chained journal state;
- current Market Data job/lease/repair/control-plane state;
- Results Registry revision/entity/evidence/audit state;
- Strategy Factory checkpoint/decision/idempotency/protected artifacts;
- Strategy Audit evidence/decision chain;
- Signal Bot delivery/outbox/cursor/update-offset/operator replay state.

A cleanup implementation must select identities through owner semantics, not discover them through
`find`, filename suffix, pathname, mtime or a generic quota sweep.

## Canonical inventory

### 1. Market Data immutable local storage

**Owner:** Historical Market Data Ingestion  
**Primary sources:**
`modules/market_data_ingestion/src/market_data_ingestion/storage.py`,
`modules/market_data_ingestion/src/market_data_ingestion/registry.py`

| Surface | Class | Automatic deletion |
| --- | --- | --- |
| raw / normalized / registered derived partitions | `immutable-lineage` | **forbidden while referenced/registered** |
| manifests / Dataset Registry identities | `immutable-lineage` | **forbidden** |
| checkpoints / runs | `durable-recovery` / `durable-audit` | **forbidden without owner lifecycle rule** |
| cancellations / quarantine / repair audits | `durable-audit` | **forbidden without explicit owner disposition** |
| staging / locks | `temporary` | owner recovery/stale-lock rule only |

The Registry is not a cache of files. Deleting Registry rows can destroy the committed identity even
if data bytes remain.

### 2. Market Data application/control-plane mutable state

**Owner:** Market Data application/control plane  
**Accepted recovery surface:** `jobs.sqlite3` and `control-plane.sqlite3`.

The accepted GAP-004 proof covers job idempotency, lease history/fencing/generation, one-time repair
authorization, token generation/fingerprint and HMAC audit history.

**Class:** `durable-recovery` + `durable-audit`.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default**. Active/current lease, repair, security and audit
state does not become disposable merely because a verified backup exists.

### 3. Research experiment repository and protected artifacts

**Owner:** Research Runner  
**Sources:**
`research/experiment_runner/src/modular_research_runner/_repository_impl.py`,
`research/experiment_runner/src/modular_research_runner/artifacts.py`

The restart-safe SQLite repository owns experiment registration/state, jobs, attempts/leases/fencing,
results, activation and final-test grants. The artifact store owns immutable checksum-bound evidence.

**Class:** `durable-recovery` + `immutable-evidence`.  
**Recoverability:** **proven** by owner-native clean-root recovery.  
**Automatic deletion:** **forbidden** unless Research defines a lifecycle-aware archival/retention
contract preserving replay, grant, result and evidence identities.

### 4. System Acceptance

**Owner:** System Acceptance  
**Source:** `applications/system_acceptance/src/modular_system_acceptance/storage.py`

The SQLite store owns run state, checks, mutation log, audit events/payloads and recovery state.

**Class:** `durable-recovery` + `durable-audit` + immutable acceptance evidence metadata.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden** without owner retention/archive rules preserving accepted run
and audit integrity.

### 5. Core / Paper Execution

**Owners:** Core + Paper Execution / Venue Reconciliation

Accepted persistent/recovery surface now includes:

- integrity-bound Core/Paper checkpoint state through the accepted Core state repository;
- matching Paper JSONL journal/event history;
- Core paper `BrokerSimulator` / OMS factual state: cash, complete orders, pending membership,
  client-order duplicate fence, deterministic sequence counters, active position, pending-close state,
  configuration and instrument identity;
- owner-native reconciliation gates before new PAPER exposure.

**Class:** `durable-recovery` + `durable-audit`.  
**Recoverability:** **proven** for the accepted factual owner surfaces.  
**Automatic deletion:** **forbidden by default**. Current/open/pending OMS state, reconciliation
identity, duplicate fences and required journals/audit state are protected. Backup verification is
not a superseded-state lifecycle rule.

### 6. Trading Runner

**Owner:** Trading Runner

Accepted durable surface uses the existing Core state/event contracts and includes runtime state,
operator lifecycle/idempotency state, checksummed runtime checkpoint and hash-chained runtime journal.

**Class:** `durable-recovery` + `durable-audit`.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default**. Current safety/kill-switch/operator replay and
checkpoint/journal identities remain protected until Trading Runner defines a tested superseded-state
retention rule.

### 7. Results Registry

**Owner:** Results Aggregator / Results Registry

Owner-native SQLite recovery preserves revision, entity state hash, verified state hash,
package/run/evidence identity, annotations, role state and append-only audit history.

**Class:** `durable-recovery` + `durable-audit` + evidence identity.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default** without an owner lifecycle/archive contract.

### 8. Strategy Factory

**Owner:** Strategy Factory

Accepted surface includes run checkpoint, decisions JSONL, SQLite idempotency ledger, protected
request artifact and protected manifest artifact.

**Class:** `durable-recovery` + `durable-audit` + protected evidence.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default** until Factory defines a lifecycle rule that proves
specific historical identities are no longer required for replay/evidence.

### 9. Strategy Audit

**Owner:** Strategy Audit

`SQLiteAuditStore` owns durable evidence and append-only signed decision history. Signing/HMAC key
material remains process-supplied secret state and is not copied into recovery payloads.

**Class:** `durable-audit` + immutable evidence.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default**; audit-chain retention is owner policy.

### 10. Signal Bot

**Owner:** Signal Bot

The accepted owner SQLite surface contains signal lifecycle/outbox state, delivery duplicate fences,
cursors, notification preferences, Telegram update offset, capacity counters and operator
idempotency/audit state. Tokens and allowlist configuration remain outside recovery payloads.

**Class:** `durable-recovery` + `durable-audit`.  
**Recoverability:** **proven**.  
**Automatic deletion:** **forbidden by default** until Signal Bot proves a concrete terminal/history
retention rule that cannot re-enable duplicate side effects.

### 11. GitHub Actions artifacts

**Owner:** workflow owner + HQ/release acceptance policy  
**Source:** `scripts/actions_artifact_retention.py` and accepted #390/#509 policy.

GitHub Actions artifacts are not a homogeneous class:

- successful accepted release/full-RC/security/heavy evidence: `immutable-evidence`, **protected**;
- current/in-progress evidence: protected;
- completed `failure`/`cancelled` diagnostic artifacts may be eligible only through the accepted
  selector, age cutoff, exact run metadata, bounded count/bytes and explicit authorization;
- success/skipped/neutral/active/recent artifacts are not selected by that cleanup contract.

The accepted failed/cancelled diagnostic cleanup is **backup-independent** and is not evidence that
runtime/data owners are pruneable.

### 12. Self-hosted runner workspace, temp and bounded caches

**Owner:** CI infrastructure  
**Sources:** `.github/RUNNER_LABELS.md`, `scripts/runner_preflight.sh`

**Class:** `temporary` / `reconstructible-cache`.

Runner pressure telemetry is role-based and currently preserves the accepted free-space floors:

- `fast`: high-water <2 GiB, critical <1 GiB;
- `docker`: high-water <15 GiB, critical <10 GiB;
- `macos`: high-water <12 GiB, critical <8 GiB;
- `backtester`: high-water <30 GiB, critical <20 GiB.

Project source, external corpora, current evidence and owner persistence are never scratch merely
because they share the same disk.

### 13. Build/package/test scratch

**Owner:** producing workflow/job  
**Class:** `temporary` / `reconstructible-cache` unless promoted into accepted immutable evidence.

Run-isolated virtual environments, build output, test caches and temporary reports may be reclaimed
only through their validated job/workspace lifecycle. Once an artifact becomes accepted immutable
evidence, it leaves the scratch class.

## Recovery state after GAP-004

GAP-004 / #377 is closed completed. The accepted System Recovery substrate now provides:

- deterministic manifest-last backup packages;
- source/release and logical owner identity binding;
- trusted manifest SHA-256 plus embedded payload checksums/sizes;
- closed inventory and traversal/symlink/special-file rejection;
- regular-file and SQLite snapshot integrity;
- isolated clean-root whole-manifest restore and exact repeat restore;
- owner-native reopen/integrity/replay/reconciliation proofs for the accepted PAPER/research owners;
- sanitized recovery evidence.

This changes the old inventory state from “recovery unresolved” to **recoverability proven** for the
listed factual owner surfaces. It does **not** change their deletion default.

## Deletion decision matrix

| Condition | Default decision |
| --- | --- |
| Registered dataset/raw/normalized/manifest lineage | DENY |
| Current owner checkpoint/runtime/reconciliation/OMS state | DENY |
| Current lease/fence/idempotency/duplicate-side-effect authority | DENY |
| Required journal/audit chain | DENY |
| Accepted System Acceptance / audit history | DENY |
| Research/Results/Strategy evidence without owner archival contract | DENY |
| Successful accepted GitHub Actions evidence | DENY |
| Unknown owner/reconstructibility/lifecycle state | DENY |
| Verified backup exists but owner has no disposal lifecycle rule | DENY |
| Stale lock proven stale by owner | owner cleanup allowed |
| Atomic temp/staging proven abandoned by owner recovery logic | owner cleanup allowed |
| Run-isolated workspace/build scratch after job boundary | bounded infra cleanup allowed |
| Completed failed/cancelled CI artifact satisfying accepted selector + reviewed destructive boundary | explicitly authorized batch only |

## Backup-dependent pruning dependency

For durable state, verified recovery and pruning authority are separate gates.

A future backup-dependent destructive rule must satisfy
[BACKUP_DEPENDENT_PRUNING_ADMISSION.md](BACKUP_DEPENDENT_PRUNING_ADMISSION.md), including:

1. exact owner/candidate identity;
2. proof the candidate is not current/non-terminal/audit/lineage state;
3. accepted package identity and trusted manifest digest;
4. isolated restore + owner-native reopen/integrity smoke;
5. owner-specific lifecycle rule declaring that exact candidate disposable;
6. deterministic bounded path-safe deletion plan;
7. sanitized decision/audit evidence.

A successful backup alone never authorizes deletion.

## Remaining GAP-013 ownership/retention work

The recovery dependency is now resolved, but #390 still owns broader policy/implementation work:

1. owner-specific lifecycle/retention rules for any durable identity proposed for pruning;
2. high-water/critical-water and quota policy where concrete owner limits remain unknown;
3. low-disk/quota/read-only/interrupted-cleanup negative tests;
4. deterministic bounded cleanup implementations only for owner-approved disposable classes;
5. idempotent cleanup audit evidence and path/symlink safety;
6. reconciliation of the accepted capacity forecast/System Resources consumer without creating a
   second observability state machine.

Until a narrow owner rule proves one of those deletion cases, durable factual state stays protected.

## Capacity / observability boundary

The accepted Control UI System Capacity surface consumes canonical owner evidence read-only and does
not grant deletion authority, invent a synthetic global watermark or claim unsupported time-to-full.
Its parent #371 remains under separate HQ disposition.

GAP-013 must consume that projection rather than introduce another capacity state machine.

## Source map

Primary current-tree and accepted owner sources include:

- `applications/system_recovery/README.md` and `applications/system_recovery/RUNBOOK.md`;
- Market Data storage/registry and application persistence modules;
- Research experiment repository and artifact store;
- System Acceptance storage;
- Core checkpoint/event/broker-state persistence;
- Paper Execution recovery/reconciliation owner contracts;
- Trading Runner durable stores/checkpoint/journal owner contracts;
- Results Registry owner persistence;
- Strategy Factory persistence/idempotency/artifact contracts;
- Strategy Audit `SQLiteAuditStore`;
- Signal Bot state/operator-ledger persistence;
- `scripts/actions_artifact_retention.py`;
- `.github/RUNNER_LABELS.md` and `scripts/runner_preflight.sh`.

Historical Issues/PRs explain acceptance chronology; current-tree code and current owner documentation
remain authoritative.

## Source-defined runtime roots (partial input to #400)

The table records roots that current source or owner configuration actually names. It is a
component-level map, not a deployment layout: there is no product-wide private root, service account,
mount plan, ACL, or `chmod` policy in these sources. “No mode enforcement” means the component does
not set Unix mode bits for the root; access still depends on the operating system and process
identity. Paths without a code-enforced mode rule must be assigned suitable ownership and access
controls by the eventual host deployment contract.

| Owner / surface | Path source or configuration key | Source-defined default | Code-enforced filesystem rule |
| --- | --- | --- | --- |
| Historical Market Data Ingestion — registered data, registry, jobs and artifacts | `ApplicationSettings.storage_root`; CLI `--storage-root` | `./data`, resolved relative to the process working directory | `LocalStorage` creates root/layer directories with requested mode `0750` (subject to umask); rejects path escape and symlink destinations. Atomic data files request `0640`; lock files request `0600`. No owner/service account is assigned in code. Sources: [settings.py](../../modules/market_data_ingestion/src/market_data_application/settings.py), [cli.py](../../modules/market_data_ingestion/src/market_data_application/cli.py), [storage.py](../../modules/market_data_ingestion/src/market_data_ingestion/storage.py). |
| Control Plane — Backtest results and job state | `MODULAR_CONTROL_BACKTEST_OUTPUT_ROOT`; `MODULAR_CONTROL_BACKTEST_STATE_PATH` | `~/.module-strategy/control-plane/backtest-results`; `~/.module-strategy/control-plane/backtests.sqlite3` | Paths are passed to the result writer and SQLite store. No root mode, owner, ACL, or chmod rule is enforced here. Sources: [server.py](../../applications/control_plane/src/modular_control_plane/server.py), [backtest.py](../../applications/control_plane/src/modular_control_plane/backtest.py). |
| Strategy Factory — durable run state and protected artifacts | CLI `--state-root`; Control UI attachment `MODULAR_CONTROL_STRATEGY_FACTORY_STATE_ROOT` | `.strategy-factory-state` for the CLI; Control Plane integration has no default and is disabled when unset | Run state and artifacts are beneath the supplied root; no root mode or owner is set by this CLI/store. The Control Plane reader resolves the configured root and enforces path/run identity constraints. Sources: [cli.py](../../research/strategy_factory/src/strategy_factory/cli.py), [run.py](../../research/strategy_factory/src/strategy_factory/run.py), [artifacts.py](../../research/strategy_factory/src/strategy_factory/artifacts.py), [server.py](../../applications/control_plane/src/modular_control_plane/server.py), [strategy.py](../../applications/control_plane/src/modular_control_plane/adapters/strategy.py). |
| Research Runner — experiment outputs and Control UI state reader | CLI `--output-root`; `MODULAR_CONTROL_RESEARCH_STATE_PATH` for the separate SQLite state attachment | CLI requires an explicit output root; Control UI state path has no default and is disabled when unset | Plan builder rejects a symlink output root. The state reader opens the configured database read-only; neither path has a code-enforced owner or mode policy. Sources: [cli.py](../../research/experiment_runner/src/modular_research_runner/cli.py), [planning.py](../../research/experiment_runner/src/modular_research_runner/planning.py), [server.py](../../applications/control_plane/src/modular_control_plane/server.py), [research.py](../../applications/control_plane/src/modular_control_plane/adapters/research.py). |
| Core / Paper Execution — durable runtime state, checkpoints and event history through Trading Runner | Trusted Trading Runner bootstrap `owner_root`; Core repositories are injected by that owner composition; child paths are fixed as `state/` and `journal/events.jsonl` | No product-wide default owner root; deployment code supplies it | Rejects symlink/non-directory roots and journal paths, and rejects child directories escaping the owner root. No chmod/mode or process-owner policy is set. Sources: [persistence.py](../../applications/trading_runner/src/modular_trading_runner/persistence.py), [bootstrap.py](../../applications/trading_runner/src/modular_trading_runner/bootstrap.py), [checkpoint.py](../../applications/trading_runner/src/modular_trading_runner/checkpoint.py). |
| Core — optional extension-owned durable state repository | `AtomicJsonStateRepository(directory)` constructor input; Trading Runner supplies its fixed `owner_root/state` for the PAPER runtime state repository | No Core-wide default; directory is injected by each owner | Resolves and creates the directory with platform defaults; state keys are constrained to files directly beneath it. No owner/mode/ACL policy is set by Core. Source: [extensions.py](../../src/modular_core/extensions.py). |
| Strategy Audit — append-only evidence and decision ledger | `SQLiteAuditStore(path)` write-side input; Control UI read-side attachment `MODULAR_CONTROL_STRATEGY_AUDIT_STATE_PATH` | No default path in the store or Control Plane attachment; read surface is disabled when unset | Store creates parent directories with platform defaults and sets no file mode/owner; Control UI reader requires an existing regular, non-symlink file and opens it read-only. Sources: [store.py](../../research/strategy_audit_validation/src/strategy_audit_validation/store.py), [query.py](../../research/strategy_audit_validation/src/strategy_audit_validation/query.py), [server.py](../../applications/control_plane/src/modular_control_plane/server.py), [evidence.py](../../applications/control_plane/src/modular_control_plane/adapters/evidence.py). |
| Signal Bot — lifecycle, delivery-deduplication and operator audit state | YAML `state.database` consumed by `load_config()` | `data/signal_bot.sqlite3`, relative to the configuration file directory, then resolved | SQLite stores create parent directories with platform defaults; no symlink rejection, mode, or owner policy is enforced by these stores. Sources: [config.py](../../src/signal_bot/config.py), [cli.py](../../src/signal_bot/cli.py), [state.py](../../src/signal_bot/state.py), [operator_ledger.py](../../src/signal_bot/operator_ledger.py). |
| System Acceptance — runs, checks, audit and recovery state | Deployment composition `build_stage1_system_acceptance_process(state_path=...)`; `SQLiteAcceptanceStore(path)` | No default path is defined by the owner or Stage-1 builder | Bootstrap rejects a symlink state path; store creates parent directories with platform defaults and sets no file mode/owner. Sources: [production_stage1.py](../../applications/system_acceptance/src/modular_system_acceptance/production_stage1.py), [bootstrap.py](../../applications/system_acceptance/src/modular_system_acceptance/bootstrap.py), [storage.py](../../applications/system_acceptance/src/modular_system_acceptance/storage.py). |
| Results Registry — derived-view cache | `DerivedViewCache(directory)` constructor argument | No default; the caller supplies the cache directory | Creates parent components with requested mode `0700` when absent and rejects symlink/non-directory parents and non-regular/symlink targets. No service owner is assigned. Sources: [cache.py](../../modules/results_aggregator/src/modular_results_registry/cache.py), [safe_io.py](../../modules/results_aggregator/src/modular_results_registry/safe_io.py). |
| Results Registry — canonical SQLite metadata and immutable evidence blobs | `SQLiteRegistry(path)` / CLI positional `database`; `ImmutableArtifactStore(root)` | No default in the registry; callers supply database and blob-store root | Registry rejects symlink parents/targets and requests `0700` for newly-created parents; its atomic-write helper defaults to `0600`, while SQLite database-file mode is not explicitly set. Artifact store rejects unsafe paths, creates the temporary blob file with mode `0400`, then changes the published immutable blob to `0444`. Existing deployment owner/group is not set in code. Sources: [storage_lifecycle.py](../../modules/results_aggregator/src/modular_results_registry/storage_lifecycle.py), [cli_parser.py](../../modules/results_aggregator/src/modular_results_registry/cli_parser.py), [safe_io.py](../../modules/results_aggregator/src/modular_results_registry/safe_io.py), [artifact_store.py](../../modules/results_aggregator/src/modular_results_registry/artifact_store.py). |
| Research Runner — immutable run/result artifacts | `AtomicJsonArtifactStore(root)` injected by its owner composition | No default in the artifact store | Rejects symlink roots/parents/targets and unsafe relative paths; creates directories and files with platform defaults (no requested owner or mode bits). Sources: [artifacts.py](../../research/experiment_runner/src/modular_research_runner/artifacts.py), [results.py](../../research/experiment_runner/src/modular_research_runner/results.py). |

The Control Plane also accepts `MODULAR_CONTROL_STRATEGY_AUDIT_STATE_PATH` and
`MODULAR_CONTROL_RESULTS_REGISTRY_PATH` as optional evidence-reader database paths; neither has a
source-defined default or a Control Plane filesystem mode/owner policy. The corresponding audit and
registry owners remain responsible for their write paths. Source: [server.py](../../applications/control_plane/src/modular_control_plane/server.py).

This partial map does not define the product's single-host topology. Missing deployment inputs
remain open for #400: final absolute roots, process/service identities, group membership, effective
umask, parent-directory permissions, mounts/filesystem guarantees, backup destinations, and the
installation-specific path wiring for components whose roots are constructor/CLI inputs. Do not
infer these from development defaults or these component rules.

### Durable owners not fully mapped to a deployed root

- Core / Paper is mapped only through the Trading Runner's fixed `owner_root/state` and
  `owner_root/journal` layout; no accepted product deployment binding supplies the actual root.
- Strategy Audit's accepted owner write path is a constructor argument. The environment key above is
  only the optional Control UI read attachment; no source ties it to the writer's configured path.
- System Acceptance's Stage-1 builder receives `state_path`, but no accepted host-level binding
  selects its final absolute value.
- Results Registry's Control UI attachment is optional; the database and immutable blob-store
  write paths are explicit constructor/CLI inputs, and the cache directory has no product
  configuration default. No accepted deployment binding selects their final absolute roots.
- Core extension state directories outside the Trading Runner PAPER binding remain constructor-
  injected; no product-wide default or permission contract chooses their deployed values.

Signal Bot and the listed Market Data, Control Plane Backtest, Strategy Factory and Research Runner
paths are source/config inputs, not proof that their production values or host ACLs are accepted.

## Non-goals

This inventory does not:

- delete, compact or truncate any store;
- set a universal age-based retention period;
- make all backed-up data disposable;
- authorize pruning of successful acceptance evidence or registered lineage;
- expose local production paths or runner hostnames;
- introduce another backup/recovery subsystem;
- change runtime, strategy, dataset, execution, financial, release, deployment or Live behavior.
