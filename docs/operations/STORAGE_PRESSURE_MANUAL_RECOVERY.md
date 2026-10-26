# Storage-pressure manual recovery runbook

Status: **operator recovery contract**  
Parent: #390 / GAP-013  
Tracking: #532  
Canonical owner inventory: `docs/operations/STORAGE_OWNERSHIP_INVENTORY.md`

## Purpose

This runbook defines what an operator may do when storage pressure, filesystem
failure, or GitHub Actions quota prevents normal progress and automatic cleanup
cannot proceed safely.

It does **not** grant new deletion authority. The storage owner and immutable
identity remain authoritative. Unknown ownership, unknown reconstructibility,
missing backup proof, or stale cleanup evidence means **STOP — DO NOT DELETE**.

## Universal recovery order

For every storage-pressure incident, use this order:

1. stop or allow the owner to fail closed before new durable side effects;
2. identify the exact storage owner and pressure surface;
3. collect a read-only inventory from the owner or platform API;
4. classify protected identities before considering reclaimable identities;
5. prefer owner-native transient/cache cleanup over durable deletion;
6. if destructive cleanup is required, prove its exact identity and bounded
   scope before the first mutation;
7. perform one bounded operation;
8. re-inventory authoritative state from scratch;
9. verify the original blocked operation rather than weakening its gate;
10. record the complete recovery evidence and return maintenance tooling to its
    least-privileged steady state.

Never resume deletion from an old positional offset, an old directory listing,
an mtime scan, or a stale digest.

## Immediate STOP conditions

Do not continue destructive recovery when any of the following is true:

- the storage owner is unknown;
- an identity is classified as `immutable-lineage`, `immutable-evidence`,
  `durable-recovery`, `durable-audit`, or `unresolved` and no narrower owner
  contract authorizes disposition;
- current/recovery/checkpoint state cannot be distinguished from superseded
  state;
- a required backup/archive has not been integrity-verified and restore-tested;
- a cleanup selector returns malformed, ambiguous, duplicate, or unexpected
  identities;
- the current inventory differs from the reviewed destructive batch;
- required count, byte, rate-budget, or pressure ceilings are exceeded;
- a filesystem path escapes the owner root or traverses a symbolic link where
  the owner forbids it;
- another writer/cleanup operation makes the reviewed state stale;
- the post-cleanup state cannot be verified independently;
- continuation would require deleting accepted release/full-RC/security/heavy
  evidence or registered market-data lineage.

Escalate the unresolved owner/identity to HQ or the owning component instead of
inventing a generic cleanup rule.

## Scenario A — self-hosted runner disk pressure

### Source of truth

Use the structured capacity result from `scripts/runner_preflight.sh` and the
role thresholds in `.github/RUNNER_LABELS.md`.

Current accepted disk tiers are:

| Capability | High-water warning below | Critical failure below |
| --- | ---: | ---: |
| `fast` | 2 GiB | 1 GiB |
| `docker` | 15 GiB | 10 GiB |
| `macos` | 12 GiB | 8 GiB |
| `backtester` | 30 GiB | 20 GiB |

`high-water` is an early warning. `critical` remains fail-closed before project
installation.

### Allowed recovery

1. Preserve the failed/blocked job identity and its preflight capacity line.
2. Determine whether another job is active on the same shared infrastructure.
3. Reclaim only CI-infrastructure-owned transient state through accepted
   pre/post-job cleanup boundaries:
   - completed run workspaces;
   - run-scoped temporary directories;
   - explicitly reconstructible dependency/build caches;
   - owner-validated stale scratch state.
4. Preserve external corpora, registered datasets, owner databases,
   checkpoints, accepted evidence, and any unknown directory even when they
   share the same physical disk.
5. Re-run the preflight. Do not start project installation until the role is no
   longer critical.
6. Record the post-recovery state as `healthy` or `high-water`; a remaining
   high-water state is not equivalent to a critical failure, but it is an
   operational warning that must remain observable.

### Forbidden recovery

Do not use a broad `find ... -delete`, age-only directory sweep, host-wide cache
purge, Docker system-wide deletion, or filesystem cleanup that crosses an owner
boundary merely to make the preflight green.

## Scenario B — GitHub Actions artifact quota/storage pressure

### Read-only diagnosis first

1. Confirm the failure is artifact storage/quota transport rather than a
   substantive test/build/security failure.
2. Inventory Actions artifacts read-only.
3. Protect:
   - successful accepted exact-head evidence;
   - current/in-progress evidence;
   - release/full-RC/security/heavy evidence;
   - any identity whose acceptance role is unresolved.
4. For failed/cancelled diagnostics, use only the accepted metadata selector:
   completed run, allowed conclusion, exact artifact/run identity, age cutoff,
   non-expired metadata, and bounded count/bytes.

A successful run or an artifact that is merely old/large is not a cleanup
candidate by itself.

### Reviewed bounded mutation

A destructive maintenance batch must have all of these before the first DELETE:

- an exact read-only inventory from the same selector;
- deterministic oldest-first ordering by `(created_at, artifact_id)`;
- an explicit count ceiling and byte ceiling;
- exact first/last artifact identities;
- a canonical SHA-256 over the complete reviewed batch metadata;
- fresh GitHub core rate-limit budget sufficient for the batch plus reserve;
- bounded retry/backoff for transient transport failures;
- paced mutation requests;
- a one-shot authorization whose code/selector ancestry cannot silently drift.

If the fresh batch digest differs from the reviewed digest, **STOP before the
first DELETE**. Re-run read-only planning and review the new identity.

### Interrupted or partial mutation

If a maintenance job terminates after any possible mutation:

1. do not continue from the last reported index;
2. run a fresh read-only inventory;
3. account explicitly for identities that are already missing;
4. recompute the deterministic next batch and its digest;
5. require a new reviewed one-shot authorization before another mutation;
6. record the actual deleted/already-missing count and bytes from the previous
   operation if they are recoverable from authoritative logs/API state.

A 404 may be treated as idempotent only by the accepted cleanup implementation;
it is not permission to ignore other metadata or transport failures.

### Return to least privilege

After a bounded maintenance operation:

1. run a fresh read-only post-inventory;
2. prove the reviewed digest is no longer executable;
3. remove/replace the write-enabled one-shot workflow so persistent maintenance
   returns to `actions: read` / `contents: read` behavior;
4. keep the maintenance PR Draft/unmerged unless a separate canonical
   integration decision explicitly requires otherwise.

### Verify the blocked producer

Storage recovery is not complete because DELETE succeeded. Re-run only the
previously failed evidence upload/job on the **unchanged exact producer head**
when possible. All substantive gates must remain unchanged and green. Capacity
is considered restored only when the new immutable artifact is actually created
and its identity/digest can be verified.

Do not replace immutable evidence with mutable comments/logs merely to bypass
quota.

## Scenario C — durable local owner store is full or read-only

Examples include Market Data storage/Registry, Research repository/artifacts,
System Acceptance state, and current Core/Paper/Trading recovery state.

### First response

1. Stop new owner mutation or allow the owner to fail closed.
2. Capture the typed storage error, owner identity, exact operation, and current
   durable identity without exposing secrets/private topology.
3. Determine whether the failure is disk exhaustion/quota, read-only
   filesystem, permission denial, or general I/O failure.
4. Consult `STORAGE_OWNERSHIP_INVENTORY.md` and the concrete owner contract.

### Durable deletion precondition

No durable identity may be deleted unless at least one of these is independently
proven:

1. it is deterministically reconstructible from an immutable upstream identity
   and the rebuild path is tested; or
2. a backup/archive exists, its integrity is bound to the exact identity, and a
   restore smoke has passed; or
3. the owner explicitly defines a terminal disposable state and negative/current
   recovery tests prove that disposal cannot remove live or accepted state.

If none applies, free capacity somewhere else or expand/move storage; do not trim
SQLite tables, manifests, checkpoints, or owner directories generically.

### SQLite-specific boundary

A SQLite file is an owner store, not an interchangeable cache. Do not delete
rows, `-wal`, or `-shm` files manually to recover capacity. Use owner-supported
transactions/checkpoint/backup/recovery behavior only. A successful
`integrity_check` does not itself prove that old rows are disposable.

### Immutable file-store boundary

Do not remove registered Market Data partitions/manifests, Research immutable
artifacts, or accepted evidence simply because the source can theoretically be
re-downloaded. Reconstructibility must be proven with the exact identity,
checksum/lineage, and deterministic restore/rebuild contract first.

## Scenario D — automatic pruning cannot safely choose a candidate

If a cleanup policy reaches an unresolved owner, protected identity, conflicting
metadata, oversized oldest item, insufficient API budget, or any other
fail-closed condition:

1. stop the automatic cleanup;
2. preserve the read-only inventory and rejection reason;
3. open/continue a narrow owner issue describing the unresolved identity and
   required proof;
4. do not widen the selector to make the operation pass;
5. prefer capacity expansion, reduced future artifact production, or an
   owner-specific retention design over emergency deletion.

## Minimum recovery evidence record

Every significant recovery operation should leave a GitHub Issue/PR/run record
with enough information to reproduce the decision without revealing private
host topology:

- repository and relevant branch/ref;
- exact code/workflow head;
- storage owner and storage class;
- pressure/quota error and structured pressure state where available;
- read-only inventory totals and cutoff/selector contract;
- protected identities/classes excluded from cleanup;
- for a destructive batch: count, bytes, first/last IDs, canonical batch
  SHA-256, ceilings, rate budget and reserve;
- mutation result: deleted count/bytes and already-missing count/bytes;
- post-cleanup read-only inventory and next-batch identity if non-empty;
- proof that persistent maintenance returned to read-only/least privilege;
- exact blocked producer/job retried afterward and its final result;
- any residual risk or owner gap.

Do not record bearer tokens, credentials, private storage roots, runner
hostnames, physical machine identity, or raw private market data.

## Recovery is complete only when

All applicable conditions hold:

- authoritative state is readable and internally valid;
- pressure is below the owner/platform critical boundary or the blocked external
  quota operation succeeds;
- no protected identity was removed;
- any destructive batch is fully accounted and post-inventoried;
- write-enabled maintenance has been removed or otherwise rendered
  non-executable;
- the original failed producer/gate has been retried successfully without
  weakening its acceptance contract;
- the incident/recovery evidence is sufficient for later review.

If the original operation still fails, recovery remains open even when local
cleanup succeeded.

## Source map

Primary current-tree sources:

- `docs/operations/STORAGE_OWNERSHIP_INVENTORY.md`;
- `.github/RUNNER_LABELS.md`;
- `scripts/runner_preflight.sh`;
- `scripts/actions_artifact_retention.py`;
- Market Data `storage.py` and `registry.py`;
- Research Runner repository/artifact stores;
- System Acceptance durable storage.

Operational batching evidence is tracked under #509/#511. That maintenance
surface does not broaden the steady-state owner permissions described here.

## Non-goals

This runbook does not:

- authorize blanket deletion;
- define retention periods for durable owner stores;
- replace owner-native backup/restore design;
- weaken immutable evidence requirements;
- authorize merge to `main`, release, deployment, private credentials, or live
  execution.
