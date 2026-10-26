# System Acceptance Soak Executor V1

`CheckpointedSoakExecutor` is the owner-native timing primitive for `SOAK_2H`, `SOAK_24H` and `SOAK_72H`.

## Rule: active time is heartbeat evidence, not poll count or wall time

The executor never computes soak completion from `now - started_at` and never awards a fixed duration merely because the daemon called it once.

Each persisted checkpoint is a health heartbeat. Active time advances only by the actual UTC elapsed interval between two consecutive healthy checkpoints, and only when that interval is within the configured bounded heartbeat window (`slice_seconds`, maximum 300 seconds).

The first healthy observation establishes a baseline and contributes zero. An unhealthy probe, failed probe, stale heartbeat gap, recovery call, stopped daemon, expired owner lease, process crash or host downtime contributes zero active seconds. After any paused/stale/recovery interval, a fresh healthy baseline must be persisted before later healthy elapsed time can be credited.

The System Acceptance owner adds the returned `active_seconds` only when the phase outcome is durably persisted. Therefore a process dying after a health observation but before owner persistence cannot silently credit soak time.

## Checkpoint

Every non-terminal soak slice emits an `AcceptanceCheckpoint` containing only:

- soak phase;
- UTC update timestamp;
- bounded opaque progress token;
- SHA-256 digest.

The token/digest bind:

- acceptance run ID;
- phase;
- completed active milliseconds;
- heartbeat timestamp milliseconds;
- health state (`healthy` or `paused`).

This prevents a persisted timestamp from being altered independently to manufacture active duration. No filesystem path, host identity, environment data, market data or external response is stored in the checkpoint.

A mismatched/tampered token or digest, a digest without a token, or a checkpoint timestamp ahead of the trusted owner clock fails closed with `RECOVERY_REQUIRED`.

## Restart and same-attempt recovery

If the owner process crashes with an `IN_PROGRESS` phase attempt, the durable owner lease/attempt machinery calls `CheckpointedSoakExecutor.recover()` using the same phase attempt identity.

Recovery resumes from the last durably persisted completed-active-time value but deliberately contributes **zero** for the elapsed downtime. The recovery call writes a fresh healthy/paused heartbeat at the current owner time. Only a later timely healthy heartbeat may begin advancing active time again.

A clean next slice within the same soak phase may use a new owner attempt ID, but it still resumes from the current phase checkpoint and cannot jump active time.

## Health semantics

The executor accepts one trusted owner-side health predicate. Browser input cannot define or replace it.

- first `True` after start/recovery/pause: zero active time and a fresh healthy baseline;
- timely `True` after a healthy baseline: credit the actual elapsed interval, bounded by the remaining target;
- `False`: zero active time, paused checkpoint, `SOAK_HEALTH_NOT_READY` warning;
- health probe exception: zero active time, paused checkpoint, `SOAK_HEALTH_PROBE_FAILED` warning;
- healthy heartbeat arriving after the configured maximum gap: zero active time, fresh baseline, `SOAK_HEARTBEAT_STALE` warning;
- non-boolean result: deterministic phase failure.

Warnings are fixed bounded codes. Raw health exception text is discarded.

## Resource samples

An optional trusted resource sampler may add one bounded typed `ResourceSample` per slice. Sampling failure is ignored rather than exposing host exception text; an invalid sampler return fails the phase contract.

## Production targets

Default targets are exactly:

- 2 hours;
- 24 hours;
- 72 hours.

Tests may override targets with smaller bounded values. Production composition must use the defaults unless an explicitly reviewed owner configuration changes the acceptance policy; browser/API payloads do not supply target durations.
