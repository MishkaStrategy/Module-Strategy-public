# Mimic / GitHub Actions quota remediation postmortem

Date: 2026-08-10

Updated: 2026-08-11 after merged organization storage policy PR #611.

Tracks: #605, #390, #373, `MishkaStrategy/Mimic#82`.

## Executive conclusion

The Mimic incident did not inject Mimic code into Module-Strategy and did not change trading, strategy, dataset, risk, execution, or runtime semantics. The original quota-remediation stream did not move `main`.

The incident exposed a repository-wide GitHub Actions storage problem and triggered storage, retention, runner-routing, fan-out, and evidence-transport changes on `integration/modular-strategy-full-rc`.

Most of the hardening is useful and must remain. The real auditability regression was not the one-day retention value by itself: several acceptance-oriented workflows replaced immutable publication with SHA-256 manifests retained only in Actions logs/job summaries. That made acceptance correctness reproducible but did not preserve the produced evidence outside the transient runner/log surface.

On 2026-08-11, PR #611 (`CI: adopt organization storage policy`) established the organization-wide target architecture on `main`:

- GitHub Actions artifacts are temporary only;
- successful routine/canonical evidence must not depend on Actions artifact storage;
- temporary failure/cancelled diagnostics use one-day Actions artifact retention;
- small canonical manifests belong on the repository evidence surface;
- large or canonical immutable evidence is published directly to a GitHub Release from the producing job;
- `.github/actions/publish-release-evidence` provides exact-commit, non-clobbering Release publication with SHA-256 sidecars.

This supersedes the earlier #605 recommendation to raise Actions artifact retention to 14 days. Durable acceptance must now be achieved by the Release/evidence surfaces, not by extending Actions artifact retention.

Deleted superseded diagnostic artifacts cannot be restored and are not treated as source/product loss.

## Trace and classification

### KEEP

| Change | Reason |
|---|---|
| #392 retention audit / selector library | Persistent workflow is read-only; selector is fail-closed and excludes successful/current evidence. |
| #407 narrow global trigger routing | Reduces unnecessary CI fan-out without changing substantive gates. |
| #410 mandatory policy jobs on `[self-hosted, fast]` | Correct workload routing under the organization hosted-budget policy. The runner change itself is beneficial. |
| #414 Fast Ruff diagnostics failure-only / nonblocking | Diagnostics are not acceptance evidence; substantive Ruff/format/MyPy/tests stay blocking. |
| #425 read-only artifact census | Observability only, no delete authority. |
| #485 / #507 Research workflow consolidation | Reduces empty workflow shells while preserving selected exact-head validation. |
| #527 storage ownership inventory | Makes protected identities explicit and fail-closed. |
| #533 storage-pressure recovery runbook | Preserves STOP-before-delete and least-privilege recovery. |
| #551 artifact-quota policy boundary | Explicitly forbids global quota suppression and keeps canonical publication blocking. |
| #591 / #593 / #596 / #600 / #602 | Owner storage-pressure correctness/recovery hardening; independent of Mimic product code. |
| #611 organization storage policy | Separates temporary Actions diagnostics from canonical Release-backed evidence and removes durable evidence from Actions artifact quota. |

### KEEP WITH GUARDS

| Change | Guard |
|---|---|
| #386 one-shot cleanup | Completed and closed without merge. 618 superseded artifacts were deleted; 17 canonical full-RC artifacts were explicitly protected. Never reuse this branch-selector pattern as blanket deletion authority. |
| #392 legacy delete execution | 49 old failure/cancelled artifacts were deleted. Final persistent workflow is read-only. Keep only the final read-only tooling. |
| #494 / #511 bounded 1-day failure/cancelled cleanup | Destructive history is irreversible. Preserve exact-batch digest fencing, ceilings, rate reserve, one-shot write authorization, and mandatory restoration to `actions: read`. Never broaden eligibility to successful/canonical evidence. |
| Core/Fast failure-only diagnostic uploads | May remain nonblocking, failure/cancelled-only, and one-day retained; they must never be reclassified as acceptance evidence. |

### REPAIR

| Change | Repair |
|---|---|
| #402 / retention migration | One-day Actions retention is acceptable only for temporary diagnostics. Do not treat Actions artifacts as the durable acceptance store. |
| #410 retention audit wording | Keep the self-hosted runner change, but distinguish storage efficiency from durable acceptance publication. |
| #426 Test Architecture | Keep deterministic run-bound manifest generation; require blocking exact-SHA GitHub Release publication of the bounded evidence directory. |
| #432 Market Data export | Keep exact-head verification and deterministic manifest; require blocking exact-SHA GitHub Release publication. |
| #434 Market Data CLI parity | Keep exact-head manifest; require blocking exact-SHA GitHub Release publication. |
| #434 Market Data download-config migration | Keep exact-head manifest; require blocking exact-SHA GitHub Release publication. |
| #437 Strategy Factory | Keep exact-head manifest and all quality/security/build gates; require blocking exact-SHA GitHub Release publication. |

## Retention and storage decision

The organization policy from #611 is authoritative for this repair:

1. `actions/upload-artifact` is not the durable evidence transport for successful acceptance;
2. temporary failure/cancelled diagnostics may use Actions artifacts with `retention-days: 1`;
3. canonical successful evidence is published outside Actions artifact storage;
4. the repaired acceptance surfaces use `.github/actions/publish-release-evidence` and private draft evidence releases while the candidate is not a product release;
5. the Release asset name is purpose-specific and the publisher prepends repository plus exact commit SHA;
6. the publisher refuses to replace an existing differing asset and writes a SHA-256 sidecar.

Therefore exhausted Actions artifact capacity is no longer an authorized reason to weaken or skip canonical acceptance evidence publication. It is also no longer a blocker for the repaired surfaces once Release-backed publication is present and proven.

The earlier recommendation to raise organization Actions retention from 1 day to 14 days is explicitly withdrawn. Raising Actions retention would recreate pressure in the wrong storage class and contradict #611.

## Evidence transport contract

For the repaired workflows:

1. all substantive quality/security/test/build checks remain blocking;
2. deterministic evidence manifest generation remains blocking and exact-head/run-bound;
3. successful acceptance does not call `actions/upload-artifact`;
4. the produced evidence set is published directly from the same job through `.github/actions/publish-release-evidence`;
5. publication is not `continue-on-error`;
6. a final publication gate asserts the Release publication step outcome is `success`;
7. the Release publisher is exact-commit-bound, deterministic for packaged directory evidence, non-clobbering, and emits a SHA-256 sidecar;
8. failure/cancelled diagnostics remain a separate temporary one-day Actions artifact class.

This preserves the useful deterministic manifests introduced during the quota incident while restoring durable immutable transport without returning successful acceptance to Actions artifact quota.

## Migration boundary

PR #611 was merged to `main` after the current full-RC line had diverged, so its publisher action was not yet present on `integration/modular-strategy-full-rc` when #605 v2 resumed. The #605 repair therefore imports the publisher action and migrates only the five Mimic/quota-remediated acceptance surfaces in scope.

Other pre-existing successful `upload-artifact` sites on full-RC are not silently reclassified or weakened by this repair. Their broader migration to the organization storage policy is a separate integration task; until migrated, existing release/security evidence gates remain blocking rather than being deleted merely to satisfy policy text.

## Non-goals

- no rollback of storage-pressure correctness fixes;
- no restoration of deleted superseded diagnostics;
- no broad increase in artifact production;
- no runtime/product/strategy/dataset changes;
- no direct change to canonical `main` or full-RC outside the Draft PR process;
- no product release/deployment authorization;
- no weakening of exact-head, privacy, security, coverage, package, or deterministic evidence gates;
- no blanket migration of unrelated historical evidence workflows inside #605.
