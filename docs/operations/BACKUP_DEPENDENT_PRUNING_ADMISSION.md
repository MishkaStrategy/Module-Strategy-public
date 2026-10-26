# Backup-dependent pruning admission

Status: **Fail-closed operational contract**  
Parent: #390 / GAP-013  
Tracking: #542  
Recovery authority: #377 / `applications/system_recovery`

## Purpose

This contract defines the evidence that a future retention rule must possess before it may use a
backup/restore claim as part of the safety argument for destructive pruning.

It does **not** implement deletion, choose retention periods, compact owner databases, authorize a
filesystem sweep, or make recovered state disposable.

The central rule is:

> **Recoverable is necessary for backup-dependent pruning, but recoverable is not pruneable.**

A concrete durable identity may be deleted only when both independent gates are satisfied:

1. **Recovery gate:** the exact protected identity is covered by accepted, identity-bound backup,
   integrity verification and isolated clean-root restore evidence; and
2. **Lifecycle gate:** the owning component explicitly proves that this concrete identity is no
   longer current/non-terminal/audit-required/lineage-required and may be discarded under a tested
   owner retention rule.

Failure or uncertainty at either gate means **DENY / do not prune**.

## Canonical recovery dependency

The only platform recovery substrate for this policy is `applications/system_recovery`, accepted by
GAP-004 / #377. GAP-013 must not create a second backup manifest, generic archive format, SQLite copy
service or recovery database.

The accepted recovery contract provides:

- exact source-commit and platform-release identity;
- recovery classes and canonical logical IDs/relative paths;
- deterministic, manifest-last backup package publication;
- canonical manifest SHA-256 recorded outside the package;
- payload SHA-256/byte-size verification and closed package inventory;
- path traversal, symlink and special-file rejection;
- SQLite online backup + integrity verification and hardened regular-file snapshot publication;
- strict digest-bound package loading;
- all-entry clean-root restore preflight and no-clobber publication;
- owner-native reopen/integrity/replay/reconciliation tests;
- exact-repeat restore idempotency;
- fail-closed corrupt/incomplete/wrong-identity handling;
- sanitized evidence and explicit exclusion of credentials/host topology.

A directory, archive or copied database that did not pass that accepted owner/recovery contract is
not a verified backup for this policy.

## Admission decision

A retention implementation that depends on backup must evaluate these gates in order and stop at the
first failure.

### Gate 0 — owner and candidate identity

The retention rule must obtain the candidate from an owner-defined lifecycle/retention API or an
explicitly reviewed identity set. Discovery by pathname, extension, `find`, mtime or generic age
alone is forbidden.

Required candidate identity fields are owner-specific, but the decision must bind enough immutable
identity to distinguish the exact object/state generation under consideration.

If ownership or identity is unknown: **DENY**.

### Gate 1 — protected-current-state exclusion

The owner must prove the candidate is not any of the following:

- current runtime/checkpoint/reconciliation state;
- pending/in-progress/uncertain mutation or operation state;
- active lease/fence/recovery authority;
- current idempotency or duplicate-side-effect fence;
- required audit/event/journal chain;
- registered immutable lineage or evidence still referenced by an accepted identity;
- current strategy/run/package/evidence identity required for restart or reproducibility.

Backup age never satisfies this gate.

If the owner cannot prove terminal/superseded lifecycle state: **DENY**.

### Gate 2 — exact recovery binding

The pruning decision must reference accepted recovery evidence for the exact owner state class. At a
minimum record:

- owner;
- protected source identity or source generation;
- accepted source commit/platform release;
- recovery package manifest SHA-256;
- logical recovery entry identity/identities needed to reconstruct the owner;
- verification result;
- isolated restore-smoke result;
- exact recovery CI/acceptance head or accepted recovery revision.

A backup merely existing somewhere is insufficient.

Missing/stale/ambiguous recovery evidence: **DENY**.

### Gate 3 — integrity and isolated restore smoke

Before the retention decision is admitted:

1. load the package using the separately trusted expected manifest SHA-256;
2. verify the closed manifest/payload inventory and checksums;
3. restore into a new/isolated owner root or database, never over live owner state;
4. reopen through the owner-native persistence API;
5. run the owner integrity/replay/reconciliation checks required by that owner;
6. prove the recovered factual identity equals the protected source identity needed by the retention
   rule.

Malformed, partial, future/incompatible, checksum-mismatched or wrong-identity recovery data:
**DENY**.

### Gate 4 — owner lifecycle disposal authority

Even after successful recovery smoke, the owner must have an explicit tested retention rule that
states why this **specific candidate identity** is disposable.

Examples of evidence that may form part of an owner rule, but are never sufficient by themselves:

- superseded checkpoint generation;
- terminal job/attempt with no live lease/fence;
- immutable artifact no longer referenced by any protected run/evidence/lineage identity;
- completed audit segment whose mandatory retained chain/summary remains owner-verifiable.

If no such owner rule exists, the candidate remains protected: **DENY**.

### Gate 5 — bounded destructive plan

A future deletion implementation must produce a deterministic immutable plan before mutation,
including exact candidate identities and bounded count/bytes. The plan must reject traversal,
symlinks, special files, root escape and undeclared targets.

It must not reinterpret recovery evidence as permission to delete unrelated owner state.

### Gate 6 — post-decision audit evidence

Every backup-dependent pruning attempt must record a sanitized decision record containing:

- policy/schema version;
- owner;
- candidate identity;
- lifecycle/terminal-state evidence identity;
- source/release identity;
- backup package manifest SHA-256;
- recovery entry identity set;
- backup verification result;
- isolated restore-smoke result;
- owner-native reopen/integrity result;
- exact bounded prune-plan digest;
- final decision: `DENY`, `AUTHORIZED_NOT_EXECUTED`, `EXECUTED`, or `FAILED`;
- if executed, exact success/failure accounting without private host paths.

Do not include credentials, tokens, hostnames, runner names, raw market data, absolute user paths or
raw owner payload values.

## Current owner matrix after GAP-004

The recovery column below reflects accepted #377 evidence. The pruning column is deliberately
separate.

| Owner / surface | Accepted recoverability | Backup-dependent pruning authority |
| --- | --- | --- |
| System Acceptance / Research owner-native state and protected artifacts | **PROVEN** | **DENY by default** until owner lifecycle/archive rule selects a concrete disposable identity |
| Core/Paper checkpoint + journal | **PROVEN** | **DENY by default**; current/reconciliation/audit state remains protected |
| Core paper BrokerSimulator/OMS factual state | **PROVEN** | **DENY by default**; orders/positions/duplicate fences/current sequence state are not made disposable by backup |
| Trading Runner runtime/operator/checkpoint/hash-chained journal | **PROVEN** | **DENY by default**; current safety/operator/replay state remains protected |
| Market Data jobs/lease/repair/control-plane mutable state | **PROVEN** | **DENY by default**; active lease/fence, repair authority, token-generation and audit state require owner lifecycle rules |
| Results Registry | **PROVEN** | **DENY by default**; revision/entity/evidence/audit identity remains owner-protected without a retention contract |
| Strategy Factory | **PROVEN** | **DENY by default**; checkpoint/decision/idempotency/protected artifact lifecycle remains owner-controlled |
| Strategy Audit | **PROVEN** | **DENY by default**; append-only evidence/decision chain remains audit-protected |
| Signal Bot | **PROVEN** | **DENY by default**; duplicate-delivery/outbox/cursor/operator replay state remains owner-controlled |
| Registered immutable dataset/artifact sources | external identity verified via owner registry/manifest; mutable backup copy intentionally absent | **DENY** while referenced/registered; any lineage retirement requires a separate owner contract |
| Failed/cancelled GitHub Actions diagnostics under accepted #509 selector | backup-independent | not governed by this backup-dependent contract; existing bounded CI policy applies |

The matrix therefore advances #542 from “recovery unresolved” to “recovery proven, lifecycle pruning
still owner-gated”. It does **not** grant generic deletion authority.

## Current recovery evidence boundary

The exact GAP-004 parent #377 is closed completed. The final accepted recovery package/composition
surface is present in canonical full-RC after PRs #725 and #727. The System Recovery exact-head suite
runs the accumulated owner-native recovery proofs on Python 3.12 and 3.13 using `[self-hosted, fast]`.

For future pruning decisions, do not bind to historical superseded recovery heads. Bind to the live
accepted recovery contract plus the concrete package manifest digest and owner source identity used
for that retention decision.

## Atomic publication and cleanup requirements

Backup verification must fail closed before destructive mutation. Incomplete package or restore temp
state must never be mistaken for a valid backup.

A future pruning implementation must independently provide:

- owner-root containment;
- no symlink traversal;
- no special-file deletion;
- deterministic bounded target inventory;
- no-clobber/identity re-check immediately before deletion where applicable;
- idempotent interrupted-cleanup recovery;
- exact accounting of deleted/already-missing/failed targets;
- preservation of audit evidence even when cleanup itself fails.

Those deletion mechanics are **not implemented by #542**.

## Capacity and pressure interaction

Low disk, quota exhaustion or backup write failure never weakens these gates.

If capacity is insufficient to create and verify the required backup, the decision is **DENY / do
not prune** for backup-dependent owner data. Storage pressure may trigger an operator escalation or a
separately proven backup-independent cleanup class; it is not permission to delete protected owner
state.

High-water/critical-water thresholds and capacity forecasting remain owned by #390 and the accepted
System Capacity/observability surface. This contract does not invent a second monitor or global
synthetic watermark.

## GitHub Actions diagnostic exception

The failed/cancelled Actions diagnostic cleanup accepted under #509 is backup-independent. It selects
only explicitly eligible completed workflow artifacts through its own metadata, age, identity,
count/byte ceilings and reviewed authorization process.

Successful/current/accepted evidence is not promoted into that diagnostic class and remains
protected.

## Acceptance rule for a future owner pruning implementation

A future owner-specific destructive PR must show, on its exact head:

1. candidate selection comes from the owner lifecycle contract;
2. current/non-terminal/recovery/audit identities are rejected;
3. exact accepted backup identity and trusted manifest digest are verified;
4. isolated restore + owner-native reopen/integrity smoke succeeds before deletion;
5. wrong/stale/corrupt/incompatible/partial recovery evidence denies the plan;
6. deletion target planning is deterministic, bounded and traversal/symlink safe;
7. interrupted/partial cleanup is idempotent and audited;
8. no unrelated owner identity becomes eligible;
9. sanitized audit evidence binds the retention decision to backup and owner identity;
10. exact-head owner tests and repository quality/policy gates pass.

Until all ten conditions are proven for a concrete owner deletion rule: **do not prune**.

## Non-goals

This policy does not:

- delete, compact or truncate any current database, journal, checkpoint or artifact store;
- set a universal TTL;
- make all successfully backed-up data disposable;
- introduce another recovery package/manifest format;
- authorize pruning across owner boundaries;
- change trading, strategy, financial, runtime or reconciliation semantics;
- authorize Live/private-exchange operation;
- merge or deploy `main`.
