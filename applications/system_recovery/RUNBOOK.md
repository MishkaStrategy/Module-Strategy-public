# System Recovery Operator Runbook

This runbook is the operator procedure for GAP-004 / Issue #377 on the PAPER/research platform.
It composes owner-native persistence. It does not replace owner databases, journals, checkpoints,
idempotency ledgers, registered-source identities, or reconciliation state machines.

## Safety boundary

- PAPER/research only. Do not use this procedure to enable Live/private-exchange execution.
- `main`, release, deployment, and private exchange credentials are outside this runbook.
- A recovery backup is successful only after `recovery-manifest.json` is durably published and its
  SHA-256 is recorded in trusted recovery evidence outside the package.
- A package without that terminal manifest, with an unknown manifest digest, or with any failed
  owner verification is **not** proof of recoverability and must not authorize destructive pruning.
- System Recovery never stores raw credentials, Telegram bot tokens, Control Plane bearer tokens,
  exchange credentials, signing/HMAC keys, or host-specific topology in the manifest.
- Owner-native protected payload bytes may contain factual private non-secret identifiers already
  persisted by that owner. Those bytes stay inside the protected backup boundary and must never be
  projected into sanitized evidence.

## Accepted owner inventory

The owner remains authoritative for the meaning and validation of every surface below.
System Recovery only snapshots/restores the accepted bytes.

| Owner | Required recovery surface | Post-restore authority |
| --- | --- | --- |
| Registered immutable sources | External `REFERENCE` identity only; source bytes are not copied into the mutable backup package | Owner/registry dataset and artifact hash validation |
| System Acceptance / Research | Owner SQLite state plus protected immutable research artifacts required by the accepted recovery proof | Owner schema/integrity and artifact identity checks |
| Core / Paper Execution | Core checkpoint/state repository, matching Paper JSONL journal, and accepted BrokerSimulator/OMS factual state | Core/Paper checkpoint, journal attestation, OMS identity and reconciliation gates |
| Trading Runner | Runtime state, operator lifecycle/idempotency state, checksummed runtime checkpoint, hash-chained runtime journal | Trading Runner checkpoint/journal verification and operator replay fences |
| Market Data | `jobs.sqlite3` containing jobs, lease history/fencing and repair-confirmation authority; `control-plane.sqlite3` containing token generation/fingerprints and HMAC audit history | Market Data SQLite integrity, idempotency, lease generation, repair replay and audit-chain verification |
| Results Registry | Owner-native Results Registry SQLite/backup state with revision, entity/verified state hashes and append-only audit history | Results Registry owner verification and duplicate/revision semantics |
| Strategy Factory | Run checkpoint, decisions JSONL, idempotency SQLite ledger, protected request artifact and protected manifest artifact | `StrategyFactoryRun.restore_existing`, decision-chain verification and idempotency ledger |
| Strategy Audit | `SQLiteAuditStore` evidence/decision history; signing/HMAC key remains process-supplied and is not backup state | SQLite/audit-chain integrity plus signature verification with separately supplied key |
| Signal Bot | Single owner SQLite database used by signal state/outbox/cursors/preferences/update offset/counters and operator idempotency/audit ledger | Signal lifecycle duplicate fence, monotonic cursors/offsets and operator replay/recovery-required semantics |

Ephemeral caches, temporary files, downloaded/rebuildable working data, secrets, and host-specific
paths are not recovery payloads.

## 1. Before backup

1. Identify the exact accepted full-RC source commit and platform release identity. Never create
   evidence from an unreviewed or ambiguous head.
2. Select a **new empty directory** for this backup attempt. Reusing a prior package root is
   forbidden. A non-empty root fails closed.
3. Resolve each owner source path locally, but expose only the relative `SnapshotSpec.source`,
   logical ID, owner, recovery class, snapshot kind, and relative restore target to System Recovery.
   Absolute host paths are operator input only and must not enter evidence.
4. Reach an owner-consistent capture point before invoking the package service:
   - SQLite entries use the SQLite online backup API and do not require file-copy quiescence.
   - Multi-file owner sets still require an owner-defined pause/checkpoint boundary so a checkpoint,
     journal, protected artifacts, and idempotency state represent one accepted factual point.
   - Paper/Core/Trading state must not be captured while an uncontrolled mutation can make their
     checkpoint/journal/OMS identities disagree.
   - If an owner cannot reach a consistent capture point, do not create a terminal package.
5. Confirm required process-supplied secrets are available through the normal secret channel for
   later owner verification. Never copy them into the package.
6. Ensure the backup destination has enough free capacity. If capacity is unknown or insufficient,
   stop before any retention/pruning action.

## 2. Create the backup package

Use the typed System Recovery service with explicit owner-approved requests:

```python
from system_recovery import SnapshotRequest, create_backup_package

package = create_backup_package(
    backup_root=backup_root,
    source_commit=accepted_source_commit,
    platform_release_id=accepted_release_id,
    requests=owner_approved_requests,
)
trusted_manifest_sha256 = package.manifest_sha256
```

The service:

- preflights every request and source before the first payload mutation;
- sorts entries by canonical `logical_id`;
- rejects duplicate logical IDs/restore targets and unsafe/missing/symlink/special-file sources;
- never copies `REFERENCE`, cache, secret/private, or reconstructable-immutable bytes into mutable
  payloads;
- snapshots regular files and SQLite using the existing atomic/no-clobber primitives;
- verifies every embedded payload;
- publishes `recovery-manifest.json` last;
- cleans payloads created by the current attempt if a later snapshot or manifest publication fails.

Do not treat the directory as a backup until the call returns successfully.

## 3. Record trusted recovery evidence

Record outside the package, in the project-approved immutable/sanitized evidence channel:

- exact source commit;
- platform release identity;
- `manifest_sha256`;
- package logical owner/entry IDs if useful;
- backup verification result and timestamp;
- exact CI/acceptance head used for the recovery contract.

Do **not** record:

- absolute user/home/temp paths;
- hostname, runner machine name, IP or private topology;
- raw market-data rows;
- raw owner payload content;
- credentials, tokens, HMAC/signing keys;
- Telegram/private chat identifiers or other owner-private values.

The manifest is intentionally path-sanitized and contains only relative source/restore paths plus
checksums/byte sizes for embedded payloads.

## 4. Verify a package before restore

Load the package only with the separately recorded trusted manifest digest:

```python
from system_recovery import load_backup_manifest

manifest = load_backup_manifest(
    backup_root=backup_root,
    expected_manifest_sha256=trusted_manifest_sha256,
)
```

Loading fails closed when:

- the manifest digest differs;
- JSON is malformed or non-canonical;
- the package contains undeclared files;
- an embedded payload is missing, extra, symlinked, special, wrong-sized, or checksum-mismatched.

`REFERENCE` entries are not copied and therefore require their owner-native external verification.
Before runtime admission, verify every registered dataset/artifact through its canonical registry
identity, expected SHA-256/schema/version, and accepted immutable-source contract. Recovery manifest
identity does not replace that source-owner check.

## 5. Restore to a clean host/root

1. Provision the supported software/dependency environment for the exact accepted source/release.
2. Create a new empty restore root. Do not repair an unknown partial root in place.
3. Load and verify the package using the trusted manifest digest.
4. Verify required registered immutable sources separately as described above.
5. Restore the complete embedded manifest in one composition call:

```python
from system_recovery import restore_manifest_entries

paths = restore_manifest_entries(
    backup_root=backup_root,
    restore_root=clean_restore_root,
    manifest=manifest,
    expected_source_commit=accepted_source_commit,
    expected_platform_release_id=accepted_release_id,
)
```

The composition preflights every payload and destination before factual publication. It admits only:

- all declared restorable targets absent; or
- all declared targets already present with exact manifest identity for an idempotent repeat restore.

A mixed/partial root fails closed. On a late failure, only files published by the current attempt are
eligible for rollback; matching files created by another actor are not deleted.

## 6. Reopen owners and reconcile before new PAPER exposure

File restoration alone is not runtime admission. Reopen each owner through its native contract and
run the accepted integrity/replay checks.

At minimum:

- Core/Paper: verify checkpoint/module/config identity, matching journal attestation and factual OMS
  state. Reconcile pending/open orders and position state. Keep `recovery_pending`/equivalent gate
  closed until the venue projection and Paper projection agree.
- Trading Runner: verify runtime checkpoint + hash-chained journal; preserve kill-switch/emergency
  state and operator idempotency replay fences.
- Market Data: verify both SQLite databases, job idempotency, lease generation/expiry semantics,
  consumed repair authorization, token generation/fingerprint state and HMAC audit chain.
- Results Registry: verify SQLite/FK/audit integrity, revision/state hashes, package/run/evidence
  identity and duplicate registration semantics.
- Strategy Factory: restore the run, verify decision-chain head/artifacts, then verify batch/event and
  external-operation idempotency before resume.
- Strategy Audit: verify evidence + append-only decision chain and revalidate historical signatures
  using the separately provisioned key.
- Signal Bot: verify terminal duplicate-delivery fences, pending outbox state, cursors/update offset,
  counters and operator ledger. An uncertain in-progress operator request stays
  `RECOVERY_REQUIRED`; do not blindly resend.

Duplicate, stale, and out-of-order inputs must remain no-op/rejected according to owner contracts.
Only after all required owners pass can new PAPER exposure be admitted.

## 7. Repeat restore

An exact repeat restore of the same trusted package into a complete matching root is an idempotency
check. It must produce the same factual bytes and must not create new orders, events, jobs,
notifications, revisions, or operator side effects.

If any target differs or only part of the declared root exists, stop and allocate a new clean root.
Do not force overwrite.

## 8. Corruption, mismatch, rollback, and irrecoverable state

Treat any of the following as a recovery failure:

- missing or unknown trusted manifest digest;
- source commit / platform release mismatch;
- unsupported manifest/schema/component version;
- missing/extra/corrupt backup payload;
- wrong registered-source identity/hash;
- owner-native integrity, journal, audit-chain, signature, checkpoint or reconciliation failure;
- partial restore root;
- rollback/cleanup durability failure;
- missing required owner backup from the accepted inventory.

Response:

1. Keep PAPER execution halted; create no new exposure or external side effect.
2. Preserve the failed package and sanitized diagnostics for investigation. Do not mutate the only
   backup copy while debugging.
3. Discard/retire the failed restore root as contaminated; retry only into a new clean root after the
   cause is understood.
4. If another accepted backup package exists, verify it from its trusted manifest digest before use.
5. If no accepted package/source combination can reconstruct an owner, classify the owner state as
   irrecoverable. Escalate to HQ/operator decision; do not fabricate/reseed factual state.
6. Never use a recovery failure as permission to weaken integrity gates, replay fences, or tests.

## 9. Retention and storage pressure

System Recovery defines the correctness boundary, not the retention scheduler. Capacity thresholds,
retention automation, interrupted-cleanup handling, and destructive pruning policy remain owned by
#390/#542.

Until those policies explicitly accept an owner boundary:

- unknown/unverified recoverability = `do not prune`;
- a failed/interrupted backup with no terminal manifest = `do not prune`;
- a package whose manifest/payload verification fails = `do not prune`;
- insufficient/unknown destination capacity = stop backup and `do not prune`;
- owner state outside accepted recovery evidence = `do not prune` when backup is the safety claim.

Diagnostic GitHub Actions pruning that is independent of owner backups remains a separate #542
boundary. System Recovery does not grant generic pruning authority.

## 10. Closure evidence

For a GAP-004 recovery acceptance record, include only sanitized identifiers and exact-head evidence:

- source/release identity;
- trusted manifest SHA-256;
- owner verification result summary;
- clean-root/repeat-restore result;
- reconciliation/idempotency result;
- CI run IDs and accepted PR/head.

Never include the package's local absolute path, host identity, credentials, private owner values, or
raw market-data payloads.
