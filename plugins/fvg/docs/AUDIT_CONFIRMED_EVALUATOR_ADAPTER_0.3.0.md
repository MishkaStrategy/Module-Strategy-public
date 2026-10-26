# Confirmed Evaluator Adapter Boundary Audit

Status: **LOCAL PASS — awaiting complete 0.3.0 runtime materialization**

## Exact local checkpoint

- Commit: `9a74b13d2a499cf631e07009e7d3c4d693a4d949`
- Parent checkpoint: `68e7be97d991b64293d217e5efb4222c024c45c8`
- Runtime: Python 3.13.5, Core 0.4.0

## Confirmed defects

The public confirmed evaluator performed direct property reads after the source candles had already passed canonical validation. A custom `CandleLike` could therefore fail on a later read and expose a raw adapter exception from four paths:

- source A `timestamp`;
- source A `symbol`;
- source A `timeframe`;
- source C `close` used as the implicit reference price.

## Fix

Those later reads now use the canonical required-candle-attribute boundary. Adapter property failures are converted into the existing deterministic `ValueError` contract with the source candle and field identified.

## Validation

- Four exact regression scenarios: PASS
- Hostile evaluator property matrix: 3 source candles × 9 fields × 11 failure thresholds = 297 cases
- Raw adapter exceptions after fix: 0
- Full normal suite: **733 tests, PASS**
- Full optimized runtime suite (`python -O`): PASS
- Branch-aware coverage: **95%**, required gate PASS
- Compileall: PASS
- `git diff --check`: PASS

## Error classification

- Implementation error: yes — public boundary containment
- Test error: no
- Workflow/configuration error: no
- Local platform limitation: no
- Runner limitation: no
- Network error: no

## GitHub runner

- Remote runner used: **No**
- Reason: the defect and regression were completely reproduced and validated locally. The remote active entrypoint remains intentionally on the validated 0.2.1 baseline until exact 0.3.0 runtime transport is complete.
- Workflow/run: none

This evidence does not authorize merge, tag, release, package publication or live trading.
