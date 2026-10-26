# System Recovery

`system_recovery` is the platform-level backup/restore coordination substrate for GAP-004 / Issue
#377. It composes owner-native durability; it does not own Market Data, Research, Strategy, Paper
Execution, Trading Runner, Results Registry, Strategy Audit, Signal Bot, or Control Plane factual
state.

The operator procedure is in [RUNBOOK.md](RUNBOOK.md).

## Recovery classes

Every declared surface is classified before snapshot work:

- `reconstructable_immutable_source` — registered datasets/source identities that remain external to
  the mutable backup package and are revalidated by their authoritative registry contract;
- `protected_immutable_evidence` — exact owner evidence bytes required for recovery/audit;
- `required_configuration` — explicitly declared non-secret configuration required to reconstruct
  the same runtime identity;
- `mutable_runtime_state` — durable factual state that cannot be reconstructed safely from immutable
  inputs alone;
- `ephemeral_cache` — rebuildable/transient bytes, forbidden from backup payloads;
- `secret_private` — credentials/private secret material, forbidden from backup payloads.

Owner-native protected payloads can contain factual private non-secret identifiers already persisted
by that owner. Those bytes remain inside the protected backup boundary. They are not projected into
the manifest or sanitized recovery evidence.

## Accepted owner recovery inventory

The exact-head System Recovery suite covers the current factual PAPER/research owner set:

- **System Acceptance / Research** — owner SQLite state plus required protected immutable research
  artifacts;
- **Core / Paper Execution** — Core checkpoint/state repository, matching Paper JSONL journal, and
  accepted BrokerSimulator/OMS factual state, including duplicate fences and reconciliation state;
- **Trading Runner** — runtime/operator state, checksummed runtime checkpoint, and hash-chained
  runtime journal;
- **Market Data** — `jobs.sqlite3` for job/idempotency, lease history/fencing and repair authority,
  plus `control-plane.sqlite3` for token generation/fingerprints and HMAC audit history;
- **Results Registry** — owner-native SQLite state preserving revision, entity/verified state hashes,
  evidence/package/run identity and append-only audit history;
- **Strategy Factory** — run checkpoint, decisions JSONL, idempotency SQLite ledger, protected
  request artifact and protected manifest artifact;
- **Strategy Audit** — `SQLiteAuditStore` evidence/decision history and append-only chain. Signing
  material remains process-supplied and is not backup state;
- **Signal Bot** — the single owner SQLite database used by signal lifecycle/outbox/cursors/
  preferences/update offset/counters and operator idempotency/audit state.

Registered immutable dataset/artifact sources are represented as `REFERENCE` entries and are not
copied into mutable backup payloads. Their hashes/schema/version remain authoritative in the source
owner/registry contract and must be checked before runtime admission.

## Snapshot and package contracts

The accepted substrate provides hardened single-entry snapshots:

- canonical logical IDs and relative POSIX source/restore paths only;
- fail-closed path traversal, symlink, special-file and destination-collision handling;
- regular-file snapshot with no-follow source open, source and temporary-file identity checks,
  durable same-directory no-clobber publication and rollback on post-publication failure;
- SQLite snapshot through the SQLite backup API plus `PRAGMA integrity_check`, URI-safe filenames,
  source/temp identity checks and the same no-clobber publication boundary;
- checksum/byte-size verification of every embedded payload;
- explicit rejection of cache, secret/private and reconstructable-immutable copy requests.

`create_backup_package(...)` adds the deterministic package-level commit point:

- requires a fresh empty package root;
- validates all owner-approved `SnapshotRequest` sources, logical IDs and restore targets before the
  first payload mutation;
- canonicalizes request order by `logical_id`;
- snapshots/verifies every embedded entry while leaving `REFERENCE` entries external;
- publishes canonical `recovery-manifest.json` **last**;
- returns the manifest SHA-256 for recording in trusted external recovery evidence;
- cleans payloads created by the current attempt if a later snapshot or manifest publication fails.

A directory without the terminal canonical manifest is not a successful backup package.

`load_backup_manifest(...)` requires the separately recorded expected manifest SHA-256, rejects
malformed/non-canonical manifest bytes, verifies the package is a closed inventory with no undeclared
files, and re-verifies every embedded payload before returning a manifest for restore admission.

## Restore composition contracts

The accepted restore substrate provides:

- source-commit/platform-release identity binding;
- complete embedded-payload preflight before the first factual publication;
- destination path/no-clobber identity preflight;
- only two accepted destination states: all declared targets absent, or all present with exact
  manifest identity for an idempotent repeat restore;
- fail-closed rejection of mixed/partial roots;
- canonical manifest-order restore through the existing single-entry primitive;
- rollback limited to files proven to have been published by the current attempt;
- preservation of a matching target that appears concurrently but was not published by this attempt;
- external/non-copied `REFERENCE` semantics.

The public `snapshot_entry(...) -> RecoveryEntry` and `restore_snapshot_entry(...) -> Path`
contracts remain available for focused owner-native composition and tests.

## Owner-native post-restore admission

Restored bytes are not sufficient by themselves. Before new PAPER exposure, owners reopen and apply
their existing verification/replay contracts. Accepted tests cover, among other properties:

- checkpoint/journal/module/config identity and Core/Paper open-state reconciliation;
- Trading Runner journal/checkpoint verification, kill-switch persistence and operator idempotency;
- Market Data SQLite integrity, job replay, lease generation, consumed repair authorization and
  HMAC audit-chain verification;
- Results Registry revision/state hashes, audit integrity and duplicate registration semantics;
- Strategy Factory decision-chain/artifact verification and batch/event/external-operation replay;
- Strategy Audit evidence/decision-chain integrity plus signature revalidation with separately
  provisioned signing material;
- Signal Bot duplicate-delivery fences, pending continuation, monotonic cursor/update-offset state and
  fail-closed uncertain operator requests.

Duplicate/stale/out-of-order replay must not create new orders, events, jobs, notifications,
revisions, or other external side effects.

## Security and evidence boundary

Recovery manifests/evidence contain only canonical identifiers, relative paths, checksums/sizes and
accepted source/release identity. Do not publish absolute user paths, hostnames, runner names, private
topology, raw market data, raw owner payload values, credentials, bot tokens, bearer tokens, or
HMAC/signing keys.

Secrets required by an owner for runtime/signature/audit validation are provisioned separately via the
normal secret channel after restore. System Recovery never turns them into backup state.

## Retention and storage pressure

System Recovery does not implement destructive retention policy. #390/#542 own capacity thresholds,
retention automation and pruning. The recovery-side rule is fail closed:

- unknown/unverified recoverability = `do not prune`;
- failed/interrupted backup with no terminal manifest = `do not prune`;
- manifest/payload verification failure = `do not prune`;
- insufficient or unknown backup capacity = `do not prune`;
- state outside an explicitly accepted owner recovery boundary = `do not prune` when backup is the
  safety argument.

Backup-independent diagnostic GitHub Actions pruning remains a separate #542 boundary.

## Non-goals

System Recovery does not:

- create a second owner truth store, outbox, accounting ledger, OMS, journal or state machine;
- copy registered immutable sources, caches or secrets merely to make recovery appear complete;
- define cloud-provider/deployment topology;
- authorize Live/private-exchange state;
- grant generic pruning authority;
- replace final GAP-002 system/soak acceptance.
