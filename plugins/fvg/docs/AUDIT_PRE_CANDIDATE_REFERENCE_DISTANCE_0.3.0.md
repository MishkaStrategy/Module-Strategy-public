# Pre-FVG Candidate Reference-Distance Audit

Status: **LOCAL PASS — awaiting complete 0.3.0 runtime materialization**

## Exact local checkpoint

- Commit: `96b689f193f1c8bc963b8a7fee109e176ada6711`
- Parent checkpoint: `9a74b13d2a499cf631e07009e7d3c4d693a4d949`
- Runtime: Python 3.13.5, Core 0.4.0

## Checkpoint mutation audit

Two real schema-4 snapshots were generated: one with an active confirmed zone and one with an unresolved pre-FVG candidate. The audit applied 965 structural mutations including scalar type replacement, removed keys, unexpected keys, duplicate rows and wrong nested container types.

Results:

- raw restore exceptions: 0
- rejected mutations that changed committed state: 0
- accepted mutations: only fields whose nullability is supported by the public CandleLike contract, plus the intentional legacy omission of `processed_at`

One impossible runtime state was accepted: an unresolved candidate with `metrics.distance_from_reference = null`. The pre-FVG evaluator always supplies a positive effective reference price, so this metric is always computed for a real candidate.

## Fix

`FvgPreCandidate` now requires `distance_from_reference` to be present. Other optional metrics remain nullable because custom candles may legally omit `open`, and a valid candle may have zero range.

## Validation

- targeted model/state regression: PASS
- full normal suite: **734 tests, PASS**
- optimized runtime suite (`python -O`): PASS
- branch-aware coverage: **95%**, required gate PASS
- compileall: PASS
- `git diff --check`: PASS

## Error classification

- Implementation error: yes — impossible provisional diagnostic state accepted by the model
- Test error: no
- Workflow/configuration error: no
- Runner used: no
- Reason: the mutation, fix and full gate were completed locally
- Workflow/run: none

No merge, tag, release, package publication or live-trading action is authorized by this evidence record.
