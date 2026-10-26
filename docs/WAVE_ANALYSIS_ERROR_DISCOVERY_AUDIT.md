# Wave Analysis RC2 — Baseline and Error Discovery Audit

Date: 2026-07-22  
Repository: `mishkacher/Module-Strategy`  
Base: `integration/modular-strategy-v0.5.0`  
Base commit: `5e03b933207a99940244634f49a756d27888a5c3`  
Working branch: `modules/wave-analysis`  
Starting remote head: `de010e52f344ea7c7eeb82cbe2eb85f8769badda`  
Pull request: `#33`

## Baseline

The published branch contained module version 0.1.0, 32 tests, 92% statement coverage, clean Ruff/strict-mypy/compile/wheel evidence, and median 5,000-bar throughput near 2,825 bars/s. PR #33 was open and Draft.

A later local RC1 candidate contained 88 tests and stronger packaging/load evidence but had not been published to PR #33. The deep pass therefore used the remote branch as publication baseline and the local RC1 candidate as recoverable implementation input.

## Material defects found

1. Derived structure, ratios, hypotheses, and part of fractal direction could cross a data-gap segment.
2. Mandatory recovery blocked detector warm-up instead of blocking only publication.
3. Persistent snapshots could accumulate in Core Feature Store.
4. Stale persistent facts could remain visible during post-gap recovery in non-Core adapters.
5. Checkpoint validation accepted several logically contradictory states.
6. DTO constructors did not uniformly reject non-finite or internally inconsistent geometry.
7. Truncated retained paths could generate negative MAE/MFE.
8. Same-bar hypothesis invalidation occurred after construction and could briefly publish an invalid active hypothesis.
9. Same-instance duplicate delivery/checkpoint reads were not serialized.
10. Several local tests and the chunked benchmark used a shortened compatibility `MarketView` constructor rather than the real Core API.
11. Historical CI test jobs omitted Hypothesis, causing collection/runtime failure while four non-test jobs passed.

Every corrected defect is covered by regression, property, lifecycle, or corruption tests.
