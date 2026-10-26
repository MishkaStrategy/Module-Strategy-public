# Historical Hardening Evidence — FVG 0.2.1

Status: **SUPERSEDED BY FVG 0.3.0 RELEASE VALIDATION**

This report preserves the accepted 0.2.1 hardening evidence. It is not the current release verdict and must not be used to describe the active package, state schema, runtime structure or remaining blockers.

## Historical 0.2.1 verdict

`READY FOR DRAFT PR AND SELF-HOSTED CI`

## Historical completed work

- preserved the local 0.2.0 release candidate;
- compared against the integration base;
- completed the Error Discovery Audit;
- fixed seven confirmed findings with regression tests;
- established the post-error baseline;
- completed three completeness rounds;
- completed 30 post-error hardening rounds and 15 intermediate audits;
- built and clean-installed wheel/sdist;
- added the former self-hosted validation workflow.

## Historical accepted evidence

- 237 tests passed;
- branch-aware coverage 96%, gate 95%;
- Ruff, strict MyPy and compileall passed;
- wheel/sdist, clean import, entry point, registry, FeatureStore and checkpoint/restore passed;
- 50k rolling throughput: 11,136.29 events/s;
- bounded-memory peak: 7,637 bytes;
- restart digest matched;
- local secret/broad-exception scan passed.

## Current authority

The active branch now targets FVG 0.3.0 and state schema 4 with split runtime, lifecycle state, confirmed evaluation, pre-FVG candidates and strict legacy migration. Current status and blockers are recorded in:

- `docs/LOCAL_0.3.0_SYNC_STATUS.md`;
- `docs/REMOTE_RECOVERY_CHECKPOINT_0.3.0.md`;
- the exact-head Python 3.12/3.13 release-gate evidence.

No current release conclusion may be inferred from this historical 0.2.1 report.
