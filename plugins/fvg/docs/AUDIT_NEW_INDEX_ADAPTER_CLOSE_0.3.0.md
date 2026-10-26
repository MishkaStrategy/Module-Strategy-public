# New-Index Candle Adapter Close-Time Audit

Status: **LOCAL PASS — awaiting complete 0.3.0 runtime materialization**

## Exact local checkpoint

- Commit: `68e7be97d991b64293d217e5efb4222c024c45c8`
- Parent boundary checkpoint: `eddfce5f5585d351a7476a5b90b98b3beae52047`
- Runtime: Python 3.13.5, Core 0.4.0

## Confirmed defect

The incremental index finder read `series[start].close_time` directly. A custom `CandleLike` whose property became unreadable after the earlier validation reads could therefore emit a raw `RuntimeError` from `_new_indexes()`.

## Fix

The index scan now obtains `close_time` through the canonical required-attribute boundary, verifies the datetime type, and normalizes it through the UTC validator before comparing it to the persisted cursor. The module continues to use copy-on-write working state, so a failed adapter remains atomic.

## Validation

- New regression: `test_new_index_adapter_close_failure_is_canonical_and_atomic`
- Targeted historical/new-index adapter regressions: PASS
- Trigger candle property-failure matrix: 9 fields × 29 thresholds, raw exceptions: 0
- Historical replacement property-failure matrix: raw exceptions: 0
- Pre-FVG lower-stream property-failure matrix: raw exceptions: 0
- Full normal suite: PASS
- Full optimized suite (`python -O`): PASS
- Branch-aware coverage gate: PASS at 95% or higher
- Compileall: PASS
- `git diff --check`: PASS

## Error classification

- Implementation error: yes — direct adapter property read bypassed the canonical boundary
- Test error: no
- Workflow/configuration error: no for the implementation; one earlier local coverage command used a relative import path and was rerun correctly
- Runner used: no
- Workflow/run: none

No merge, tag, release, package publication or live-trading action is authorized by this evidence record.
