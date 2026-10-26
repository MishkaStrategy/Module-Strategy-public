# Audit after optimization round 2

## Scope

Independent review of data invariants, timestamp semantics, order triggering, partial fills, fee accounting and existing regression coverage after rounds 1–2.

## Validation performed

- Full pytest suite: **66 passed**.
- Python bytecode compilation: passed.
- Statement coverage: recorded with `coverage.py`; baseline and new regression paths executed.
- Manual state-machine trace for market, limit, stop-market, stop-limit, IOC/FOK, partial entry and reduce-only flows.

## Findings fixed

1. **Non-finite numeric contamination — high severity.** `NaN`/`inf` could enter candles, configuration and ledger records. Boundary models now reject non-finite values.
2. **Mixed timezone storage — high severity.** Aware non-UTC timestamps could be retained. Kernel boundary datetimes are now normalized to UTC.
3. **Stop-limit trigger loss — high severity.** A stop-limit order previously forgot that it had triggered when its limit was not filled in the same bar. Trigger state now persists in the order ledger.
4. **Optimistic same-bar stop-limit fill — medium severity.** The default now defers execution until a later bar because OHLC data cannot establish trigger-before-limit ordering. Explicit `fill` policy remains available.
5. **Reduce-order fee double counting — medium severity.** Exit-order fee totals previously included an allocated entry fee. Order snapshots now report only fees charged by that order; trade fees still correctly include allocated entry and exit fees.
6. **Audit visibility — low severity.** Order snapshots now expose `fees_paid`, trigger timestamp and trigger index.

## Remaining risks for later rounds

- Engine dispatch scans accumulated order/trade journals and should use queue/cursor APIs.
- No checkpoint/replay integrity verification yet.
- Module state is not persistable for restart recovery.
- Event payloads are shallowly immutable and need canonical deep freezing.
- Configuration loading needs stronger type coercion and unknown-key rejection.

## Decision

Rounds 1–2 are accepted. No unresolved correctness defect was found in the newly changed paths.
