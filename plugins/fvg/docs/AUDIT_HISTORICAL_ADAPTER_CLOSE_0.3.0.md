# Historical Candle Adapter Close-Time Audit

Status: **LOCAL PASS — awaiting complete 0.3.0 runtime materialization**

## Exact local checkpoint

- Commit: `eddfce5f5585d351a7476a5b90b98b3beae52047`
- Parent audited checkpoint: `4c63179153791c82e07da02dab75941c747a3728`
- Runtime: Python 3.13.5, Core 0.4.0

## Confirmed defect

During historical fingerprint validation, a replacement custom `CandleLike` was first passed through the canonical fingerprint validator and then its `close_time` property was read directly again. If that adapter property failed on the later read, the raw adapter exception escaped instead of being classified as a historical data conflict.

A deterministic reproducer made `close_time` fail after the fingerprint reads. Before the fix, `FvgModule.on_bar()` emitted a raw `RuntimeError`. The module state remained intended to be atomic, but the public error contract was violated.

## Fix

The later `close_time` access now uses the canonical required-attribute boundary and UTC normalizer. Adapter property failures are converted into the existing `FvgDataConflictError` path. The working state is committed only after validation, so the failure remains atomic.

## Regression evidence

- New regression: `test_historical_adapter_close_failure_is_canonical_and_atomic`
- Targeted regression suite: PASS
- Full normal suite: **727 tests, PASS**
- Full optimized runtime suite (`python -O`): PASS
- Branch-aware coverage gate: **PASS at 95% or higher**
- Compileall: PASS
- `git diff --check`: PASS

## Error classification

- Implementation error: yes — boundary exception containment
- Test error: no
- Workflow error: no
- Local platform limitation: no
- Runner limitation: no
- Network error: no

## GitHub runner

- Remote runner used: **No**
- Reason: the defect and regression were fully reproduced and validated locally; the active remote entrypoint is still intentionally held on the 0.2.1 baseline during runtime synchronization.
- Workflow/run: none

This document is evidence only. It does not authorize merge, tag, release, package publication or live trading.
