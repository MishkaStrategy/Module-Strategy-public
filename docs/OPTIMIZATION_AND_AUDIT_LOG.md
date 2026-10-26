# Core 0.4.2 — Optimization and Audit Log

Baseline: `docs/POST_ERROR_FIX_BASELINE.md`.

Each round includes a code/test/benchmark or evidence change, a full local test run, component Ruff/mypy, deterministic smoke, and a logical local commit. Audits occur after every second round.

## Round 01

Strict mathematical validators now reject booleans instead of converting them to `0/1`. Full suite, Ruff and mypy passed.

## Round 02

Engine/execution booleans and data interval/history fields now enforce their declared runtime types. Invalid coercions fail before engine construction.


## Audit after rounds 01–02

Approved. Full suite: 321 passed; combined branch-aware coverage: 84%; event-loop median: 2.1060 s; multi-timeframe median: 2.3996 s; deterministic identities and 1,169-byte checkpoints confirmed. See `docs/hardening_v042/audits/AUDIT_AFTER_02.md`.

## Round 03

The shared UTC boundary now rejects non-datetime values deterministically instead of leaking incidental attribute errors. Aware offset timestamps continue to normalize to UTC.

## Round 04

Feature activity, expiry and snapshots now share the same aware-UTC boundary. Equivalent timezone representations reuse the snapshot cache, and future feature publication remains fail-closed at the completed-bar boundary.

## Audit after rounds 03–04

Approved. 328 tests passed; UTC and feature-time no-look-ahead boundaries are clean. See `docs/hardening_v042/audits/AUDIT_AFTER_04.md`.

## Round 05

Event envelopes now enforce exact sequence types, nonblank optional identities and mapping payloads before durable append or replay.

## Round 06

Checkpoint and module-state inputs now enforce exact persisted types and canonical schema versions before any restore mutation or compatibility decision.

## Audit after rounds 05–06

Approved. 341 tests passed; malformed event/checkpoint inputs fail before persistence or restore. See `docs/hardening_v042/audits/AUDIT_AFTER_06.md`.

## Round 07

Reconciliation output is now internally consistent and fail-closed: every mismatch requires safe halt, identifiers are unique/nonblank, and `issue_count` provides an additive deterministic counter.

## Round 08

`PositionSnapshot` remains immutable and now validates identity, side, prices, quantity, time, PnL and fill counters before exposure to modules or reconciliation.

## Audit after rounds 07–08

Approved. 356 tests passed; reconciliation and position snapshot outputs are deterministic and fail closed. See `docs/hardening_v042/audits/AUDIT_AFTER_08.md`.

## Round 09

Run/config fingerprints now canonicalize signed zero and reject nested non-finite numbers, eliminating semantically duplicate or non-standard run identities.

## Round 10

Module parameter schemas now receive complete manifest-time preflight for names, types, required flags, bounds, enums and defaults before registry loading.

## Audit after rounds 09–10

Approved for checkpoint publication. 368 tests passed; config fingerprints and parameter schemas are deterministic and fail closed. See `docs/hardening_v042/audits/AUDIT_AFTER_10.md`.
