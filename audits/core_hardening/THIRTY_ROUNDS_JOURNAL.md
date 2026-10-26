# Thirty-round core hardening journal

Baseline: integration commit `5e03b933207a99940244634f49a756d27888a5c3`, core 0.4.0.
Candidate: core 0.4.1.

| Round | Area | Verified change / evidence |
|---:|---|---|
| 1 | Data | Finite OHLCV/config/accounting values and UTC-aware timestamps enforced. |
| 2 | Time/aggregation | Duplicate, conflicting, out-of-order, gap and mixed-stream guards strengthened. |
| 3 | Event loop | Event sequence is committed only after successful append; side effects remain ordered. |
| 4 | Determinism/replay | Canonical set serialization and event-gap replay rejection added. |
| 5 | Manifest | Manifest schemas, API versions and parameter bounds validated more strictly. |
| 6 | Dependency/config | Cycles, producer conflicts and explicit safe YAML size limits tested; default v0.4 behavior retained. |
| 7 | Feature lifecycle | Optional capacity, immutable publication and failed-publish identity invariants added. |
| 8 | Feature invalidation | Delete/expiry/gap-reset filters and snapshot cache consistency hardened. |
| 9 | Checkpoint restore | All module states prevalidated; partial restore rolls back atomically. |
| 10 | Persistence | Checkpoint schema/integrity validation and directory fsync strengthened. |
| 11 | Execution | Take-profit trigger direction, trigger rounding and order validation corrected. |
| 12 | Lifecycle/fees | GTD boundary and accumulated reduce-order exit fees corrected. |
| 13 | OHLC ambiguity | Limit entries no longer use pre-entry candle extremes for same-bar brackets. |
| 14 | Gap execution | Short gap-through and configurable conservative same-bar policies regression-tested. |
| 15 | Accounting | Ledger reconciliation, average entry and partial-close invariants expanded. |
| 16 | Order ownership | Stale reduce-only orders are cancelled when their owning position closes. |
| 17 | Instrument rules | Tick/step precision and post-cap quantity/minimum validations strengthened. |
| 18 | Risk foundations | Existing `contract_size` semantics applied to sizing, fees, risk and PnL. |
| 19 | Restart | Immutable execution-state snapshots and deterministic reconciliation reports added. |
| 20 | Safe halt | Duplicate IDs, missing/unknown venue state and mismatches produce explicit halt decisions. |
| 21 | Profiling | cProfile isolated repeated view/readiness/feature/event hot paths. |
| 22 | Performance | Cached closed-bar views/readiness/snapshots reduced median hot-path time ~47%. |
| 23 | Long history | Optional bounded module-visible base history added with warm-up preflight. |
| 24 | Load | 30k-bar, multi-module, multi-timeframe deterministic load gate added. |
| 25 | Observability | Stable reason codes/details and typed kernel error evidence added. |
| 26 | Evidence | Replay report, environment fingerprint and SHA-256 evidence bundle added. |
| 27 | Static quality | Ruff reduced 20→0; strict mypy reduced 23→0 without API breakage. |
| 28 | Security/package | Runtime asserts removed; rollback failure surfaced; clean wheel, scans and SBOM added. |
| 29 | Executor harness | Existing contracts proved through Setup→Plan→Risk→Order→Position→Exit; setup-owner defect fixed. |
| 30 | Integration readiness | Original-wheel contract snapshot and all five integrated-module compatibility gates added. |

No round was counted as analysis-only. Each round has an individual report under `audits/core_hardening/rounds/` and regression evidence under `tests/hardening/`.
