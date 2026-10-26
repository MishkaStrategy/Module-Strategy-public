# Completeness round 1 — functional lifecycle

## Scope

Order/fill/position/exit paths, numeric invariants, cancellations, partial closes, instrument limits and risk boundaries.

## Confirmed defects and fixes

- Non-finite close quantity/fraction could pass DTO comparisons: fixed with shared finite/positive validation.
- Non-finite position protection prices could pass DTO comparisons: fixed.
- Blank cancellation selectors were accepted: fixed.
- Instrument specifications accepted NaN/inf and inverted min/max limits: fixed.
- Risk sizing accepted NaN/inf equity or reference prices: fixed.
- Direct `close_position` boundary accepted non-finite prices/quantities: fixed.

## Evidence

- Added adversarial regression tests for every defect.
- Existing order, fill, partial-fill, reduce-only, fee and PnL tests remain green.
- Public DTO names, import paths and field layout were preserved.

## Verdict

Functional completeness improved; no public contract break introduced.
