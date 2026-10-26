# Trading Runner reconstruction — Intermediate Audit 2

- audit_status: `PASS`
- audited_code_head: `2442a43fe85f8c18f505d7058d72fd8a64b637ca`
- evidence_run: `30192049172`
- audit_scope: durable execution boundary, startup recovery, supervision and shutdown

## Findings and resolutions

### HIGH — nonexistent Core state contract assumed

Earlier Round 3 work assumed a `StateSnapshot` wrapper and incorrect `StateRepository` method signatures. The implementation now uses the exact Core 0.4.2 `StateRepository.save(key, mapping)` / `load(key)` boundary and no parallel state contract.

Resolution evidence: strict MyPy and real Core imports pass on Python 3.12 and 3.13.

### HIGH — partial checkpoint restore could corrupt live in-memory state

A failure late in restore could leave market cursors, queues or module state partially replaced.

Resolution: all checkpoint sections are decoded before mutation; restore snapshots the current validator, queues, pipeline, module states and runtime metadata and rolls every section back on failure. Incomplete rollback fails closed.

### HIGH — exact journal attestation prevented safe factual crash recovery

A crash after factual processing journal evidence but before a new checkpoint created a valid journal extension that an exact attestation rejected.

Resolution: checkpoint attestation now verifies the original prefix hash and permits only a narrow recovery-safe event allowlist. Unknown, market, kill-switch and other stateful extensions remain blocking.

### HIGH — watchdog ignored configured failure threshold

A critical component in the transient `UNHEALTHY` state was included in the trip set, so the first failure could activate the kill switch even when the configured threshold was greater than one.

Resolution: watchdog trips only for `CRITICAL`; readiness can still be false during `UNHEALTHY` without prematurely creating the kill-switch side effect.

### HIGH — failed watchdog activation was incorrectly deduplicated

The failure signature was stored before successful kill-switch activation, preventing retries after an activation error.

Resolution: the signature is committed only after successful activation. Activation errors are journaled and re-raised, and the next watchdog check retries.

### HIGH — failed post-trip checkpoint had no retry path

After successful kill-switch activation, a checkpoint failure left no fresh recovery baseline and subsequent identical checks did nothing.

Resolution: watchdog maintains an explicit pending-checkpoint state. It retries checkpoint capture while preserving exactly-once kill-switch activation.

### MEDIUM — notification audit extensions could block recovery

Notification failure/drop evidence is non-stateful but was treated as an unsafe extension after checkpoint.

Resolution: only `notification.failed` and `notification.dropped` were added to the safe extension allowlist. Watchdog and shutdown events remain blocking unless captured in a fresh checkpoint.

### MEDIUM — startup reconciliation could become ready without a fresh recovered checkpoint

Recovery and Paper reconciliation could succeed, but a crash immediately afterward could restart from the older checkpoint.

Resolution: the startup reconciliation coordinator captures a fresh checkpoint after every configured scope reconciles without blocking findings. Capture failure blocks readiness.

## Regression evidence

The final suite covers:

- journal tampering and unsafe-extension rejection;
- factual routing duplicate/conflict and both crash windows;
- transactional checkpoint restore and rollback;
- real Router→Paper adapter compatibility;
- actual Paper reconciliation reports;
- deterministic startup ordering;
- watchdog threshold, activation retry and checkpoint retry;
- notification overflow/failure isolation;
- graceful and forced shutdown.

Workflow run `30192049172` is green on Python 3.12 and 3.13 with `85 passed` per version, strict MyPy, Ruff, build, clean-wheel and paper-only checks.

## Audit conclusion

No unresolved CRITICAL or HIGH finding remains within the Round 3–4 scope. Round 5 may begin. Integration, merge, release and live execution remain forbidden until the final audit and explicit authorization.
