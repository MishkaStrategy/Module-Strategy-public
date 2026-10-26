# Risk & Position Sizing — deep optimization and audit report

## Final scope

- Base branch: `integration/modular-strategy-v0.5.0`.
- Base commit: `5e03b933207a99940244634f49a756d27888a5c3`.
- Working branch: `modules/risk-sizing`.
- Baseline module commit: `e21afcbaf0f6004fd2cb7870bd84331617bc95fb`.
- Version: `1.0.0 -> 1.1.0`.
- Core: `0.4.0`; Module API `1.0` draft.
- Core files changed: none.
- Core Change Requests: none.

## Three completeness rounds

### Completeness 1 — functional

Covered approved/rejected BUY and SELL calculations, symmetry, Decimal precision, arbitrary quantity steps, exact boundaries, zero distance, invalid geometry, min/max quantity/notional, contract size, leverage, margin, portfolio risk, position count, NaN/infinity, extremely small/large bounded values and structured rejection reasons.

Findings fixed: non-strict direct integer config, zero instrument maxima, excessive Decimal resource values, setup correlation loss and strict nested-field validation.

Result: clean.

### Completeness 2 — technical/integration

Exercised real `StrategyModule`, `ModuleManifest`, `MarketView`, `ModuleDecision`, `FeatureStore`, `ModuleRegistry`, dependency planner, core checkpoint helpers, `BacktestEngine`, core final sizer, entry-point discovery, independent wheel and clean installation.

Findings fixed: restore incompatibility with core-frozen tuples; missing freshness/schema/symbol checks; missing standalone packaging; output lacked typed deserialization.

Result: clean.

### Completeness 3 — lifecycle/operations

Exercised constructor, strict configuration, registration, same-bar processing, approved/rejected output, missing/stale/future/expired input, exact and conflicting duplicate, malformed payload, gap flag, stream change, bounded cleanup, checkpoint, corrupted/partial/oversized restore, restart continuation, separate instances, producer-risk-consumer pipeline and finish through a real engine run.

Findings fixed: malformed unsupported event repeated indefinitely; stream changes retained old caches; setup ownership in the downstream contract required a child setup.

Result: clean.

## Thirty substantive rounds

| Round | Objective and implemented result | Verification |
|---:|---|---|
| 1 | Rechecked fixed-fractional formulas, `contract_size`, actual risk and notional invariants. | Unit and randomized invariants. |
| 2 | Hardened BUY/SELL symmetry, inclusive risk boundaries and arbitrary Decimal step flooring. | Boundary matrix; Audit 1. |
| 3 | Enforced completed-bar causality: request time must equal current close time. | stale/future tests. |
| 4 | Made decision features same-bar ephemeral and proved future candle invariance. | no-look-ahead tests; Audit 2. |
| 5 | Added strict schema-1 request DTO, unknown-field rejection and deep immutable metadata. | contract/adversarial tests. |
| 6 | Enforced feature schema, expiry and symbol consistency; required dependency remains explicit. | real FeatureStore/planner; Audit 3. |
| 7 | Preserved `setup_id`; added typed request/nested DTO mapping round-trips. | DTO round-trip tests. |
| 8 | Added `RiskDecision.from_mapping`, strict status/approved/reason consistency and immutable output. | downstream contract tests; Audit 4. |
| 9 | Hardened boolean/integer/range/dependent configuration rules. | invalid configuration matrix. |
| 10 | Added exact config fingerprint and rejected Decimal values that collide in float-valued core run identity. | config fingerprint regression; Audit 5. |
| 11 | Added stream-symbol binding, bounded counters and deterministic state ordering. | stream and state tests. |
| 12 | Made malformed unsupported events idempotent and retained bounded event barriers. | duplicate invalid-event regression; Audit 6. |
| 13 | Upgraded state schema to 2; added atomic schema/config/counter validation. | corrupted/partial state matrix. |
| 14 | Fixed real core tuple restore, deterministic oversized-state trimming and checkpoint helper compatibility. | capture/restore integration; Audit 7. |
| 15 | Hardened exact event, republished payload and conflicting-ID semantics. | duplicate/conflict tests. |
| 16 | Covered stale/out-of-order, gap invariance and replay after restore. | ordering/gap/replay tests; Audit 8. |
| 17 | Verified real API classes and automatic producer -> risk ordering with no kernel branch. | registry/planner integration. |
| 18 | Executed real producer -> risk flow through `BacktestEngine`. | engine integration; Audit 9. |
| 19 | Added downstream consumer harness preserving setup causality via child setup. | producer-risk-consumer-order test. |
| 20 | Proved matching core `FixedFractionalSizer` preserves approved explicit quantity. | core compatibility test; Audit 10. |
| 21 | Profiled before optimization; identified Decimal precision and decision validation hot spots. | `cProfile` evidence. |
| 22 | Cached immutable precision/geometry/step ratios and introduced one-pass minimum/internal derived decision construction. | full regression plus benchmark; Audit 11. |
| 23 | Ran 1k/50k/100k/250k pure workloads with five-run medians. | deterministic digest and throughput. |
| 24 | Ran 1m pure stress, 250k checkpointed full events twice, peak memory and restore timing. | byte-identical checkpoint; Audit 12. |
| 25 | Expanded structured counters, cache sizes, event labels and binding constraints. | diagnostics tests. |
| 26 | Hardened safe errors, non-serializable metadata/payloads, identifier and constraint types. | adversarial tests; Audit 13. |
| 27 | Added standalone wheel, entry point, PEP 561 marker, package metadata and dependency bounds. | wheel inspection/import. |
| 28 | Added Ruff, strict mypy, branch coverage, compileall, `pip check` and clean wheel-only smoke. | quality gates; Audit 14. |
| 29 | Updated complete contracts, temporal/state semantics, examples, changelog, benchmark and limitations. | documentation/code review. |
| 30 | Repeated full suite, integration pipeline, deterministic load and release-blocker review. | final gate; Audit 15. |

## Fifteen audits after even rounds

| Audit | Main finding | Severity | Resolution and repeat result |
|---:|---|---|---|
| 1 / round 2 | Quantity/tick rules needed exact arbitrary Decimal semantics and fixture alignment. | High | Exact integer-ratio floor/alignment; fixtures corrected; clean. |
| 2 / round 4 | Baseline accepted stale feature state and did not constrain output lifetime. | High | Exact close-time validation and same-bar `valid_until`; clean. |
| 3 / round 6 | Feature schema and request/feature/view symbols could disagree. | High | Explicit schema/symbol checks and structured rejects; clean. |
| 4 / round 8 | `setup_id` was lost and downstream had no typed parser. | High | Preserved ID and added `RiskDecision.from_mapping`; clean. |
| 5 / round 10 | Exact Decimal config could collapse into the same float run identity. | Medium | Reject non-representable parameter precision; clean. |
| 6 / round 12 | Unsupported malformed event was not remembered and could reject repeatedly. | Medium | Envelope fingerprint barrier and duplicate suppression; clean. |
| 7 / round 14 | Core checkpoint deep-freeze converts list state to tuple; restore rejected it. | High | Accept non-string sequences and preserve atomic validation; clean. |
| 8 / round 16 | One instance could switch symbols with old caches. | High | Stream-symbol state binding and explicit reject reason; clean. |
| 9 / round 18 | Existing branch was not proven against the real integration core. | High | Real core wheel, registry, FeatureStore, checkpoint and engine tests; clean. |
| 10 / round 20 | Setup ownership prevents a consumer from ordering against another module's setup directly. | High | Consumer creates module-owned child setup linked to parent; clean. |
| 11 / round 22 | Correctness hardening initially reduced pure throughput below baseline. | Medium | Measured hot-path changes; final 100k median 0.9027 s; clean. |
| 12 / round 24 | Long-run state/memory had no evidence. | Medium | 250k restart workload twice, 1m pure workload, memory/checkpoint measures; clean. |
| 13 / round 26 | Direct runtime construction still accepted some wrong identifier/integer types. | Medium | Strict runtime validation and regressions; clean. |
| 14 / round 28 | Baseline had no independent package or clean install. | Medium | Wheel, entry point, PEP 561, clean install and `pip check`; clean. |
| 15 / round 30 | PR base was `main`, conflicting with integration governance. | High | Retarget PR #9 to integration branch; code/diff remains module-only. |

## Defects fixed

1. Real-core tuple checkpoint restore incompatibility — High.
2. Missing stale/future/expired/schema checks — High.
3. Missing feature/request/view symbol consistency — High.
4. Lost `setup_id` in output — High.
5. Unsafe symbol stream change — High.
6. Cross-module setup ownership misuse in the first downstream harness — High.
7. Repeating invalid unsupported feature — Medium.
8. Exact config/run-identity precision mismatch — Medium.
9. Non-strict direct boolean/integer/identifier fields — Medium.
10. Zero invalid instrument maxima and unknown nested fields — Medium.
11. Shallow metadata immutability and unsupported resource values — Medium.
12. No standalone package/entry point/clean install — Medium.
13. Missing typed decision/request round-trip APIs — Medium.
14. Missing measured memory/restart/load evidence — Medium.
15. Hot-path regression introduced by initial defensive precision work — Medium; optimized without semantic relaxation.

Every defect has a regression or contract test.

## Final quality evidence

- Tests: 120 passed locally on Python 3.13.5.
- Branch-aware module coverage: 96%.
- Ruff: passed.
- strict mypy module gate: passed.
- `compileall`: passed.
- standalone wheel: built.
- clean wheel-only install with integration core: passed.
- `pip check`: passed.
- public import and entry-point discovery: passed.
- deterministic 250k restart workload: repeated final checkpoint byte-identical.
- no-look-ahead: stale/future rejection and future-data invariance tests passed.
- core changes: none.

Python 3.12 and the complete integration-branch suite are verified by GitHub Actions.

## Final independent audit

### Functional and mathematical

Approved quantity is never rounded upward, actual risk never exceeds the effective budget, all relevant caps use `contract_size`, and BUY/SELL geometry is explicit. Public DTO constructors remain strict; the optimized internal constructor is private and receives only values derived from a validated request under bounded Decimal context.

### Temporal and data causality

The module uses only current closed-bar time and point-in-time features. It reads no history, system clock, file, network, broker or future data. Decisions expire on the creating close time.

### State and restart

State is versioned, exact-config-bound, symbol-bound, JSON-compatible, bounded and atomically restored. Invalid state never partially mutates the instance.

### Integration

The real dependency planner orders producer -> risk -> consumer. The consumer must own its order setup and may link it to the upstream setup as a child. The core final sizer remains a separate execution safeguard.

### Performance and memory

No O(history) growth was observed. Full event throughput remains stable from 1k to 50k and across 250k checkpointed events. Caches are bounded. Final pure 100k median is about 10.5% faster than the recorded one-run baseline.

### Residual risks

- No integrated production producer of `risk.request` exists yet.
- No integrated OrderIntent/Exit/paper/live/reconciliation chain exists yet.
- Correlation/group risk requires a portfolio service.
- Module API remains draft until the downstream chain is cross-audited.
- Global durable replay beyond the bounded local cache remains a core/event-store responsibility.

## Verdict

**READY FOR INTEGRATION REVIEW**, not production/live ready. PR #9 targets `integration/modular-strategy-v0.5.0` and remains unmerged until integration CI and repository review are green.
