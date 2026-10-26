# Intermediate audit after rounds 03–04

## Decision

**Continue.** UTC normalization and feature-time changes are deterministic, compatible and improve no-look-ahead enforcement.

## Changes and invariants

- Non-datetime timestamp values fail with stable `TypeError`.
- Every feature activity, expiry and snapshot query is normalized through the aware-UTC boundary.
- Feature publication later than the completed bar close is rejected.
- Equivalent timezone offsets produce one canonical feature snapshot/cache identity.

## Public contracts

No field, method, import path, event order, Module API callback or checkpoint schema changed. Only invalid temporal inputs fail earlier.

## Evidence

- Full repository suite: **328 passed**.
- Coverage total: `TOTAL 3242 statements, 382 missed; 1038 branches, 246 partial; 84% combined`.
- Component Ruff: pass.
- strict mypy: pass across 25 core files.
- compileall: pass.
- Test peak RSS: **68,320 KiB**.
- Event loop median: **2.2894 s**, 87,359 calls/s.
- Multi-timeframe median: **2.3377 s**, peak RSS **97,400 KiB**, checkpoint **1169 bytes**.

## Determinism / no-look-ahead / checkpoint

- Both benchmark run identities repeat exactly.
- Feature timestamps cannot exceed the completed-bar close boundary.
- Equivalent aware timezones normalize identically.
- Checkpoint/restore/replay suites remain green.

## Compatibility

Aware timestamps in UTC or any fixed/zone offset remain accepted. Existing module feature lifetimes remain unchanged.

## GitHub workflow status

Remote v0.4.1 head remains green. Candidate v0.4.2 is still local; self-hosted CI is pending publication and runner-label verification.

## Residual risks

HTF calendar/DST behavior remains covered by existing tests; later stress rounds will repeat future-data invariance over larger workloads.
