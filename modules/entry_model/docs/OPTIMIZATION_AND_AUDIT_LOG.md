# Entry Model — 30 Optimization Rounds and 15 Audits

All rounds were evidence-producing: each changed code, added tests, strengthened a contract, measured behavior, or produced release evidence. The common invariants checked after every pair were no look-ahead, deterministic identity, atomic checkpoint/restore, bounded memory, Core API compatibility, and unchanged module responsibility.

## Rounds

| Round | Objective | Concrete work and result |
|---:|---|---|
| 1 | Decimal mathematics | Added high-precision local Decimal contexts and extreme-value cases for retracement/offset calculations. |
| 2 | BUY/SELL geometry | Property and parameterized symmetry tests cover all six entry kinds, equality boundaries and zero-distance rejection. |
| 3 | Completed-bar causality | Plans use only `MarketView.current.close_time` and already-visible features; future setup timestamps fail closed. |
| 4 | Future invariance | Truncated and extended engine runs prove later bars do not mutate an already-created plan. |
| 5 | Input schema | Setup schema 1 has explicit allowed fields, nested `entry_hint` schema and finite positive price validation. |
| 6 | Producer substitutability | Real dependency-planner tests use a minimal canonical producer; missing `setup.confirmed` is rejected pre-run. |
| 7 | Output immutability | Schema-2 `EntryPlan` is frozen/slotted; nested metadata is normalized; public payload is plain JSON-compatible data. |
| 8 | Output identity | Deterministic plan/rejection IDs cover canonical execution semantics and reject payload mutation during parsing. |
| 9 | Strict configuration | Bool/int ambiguity, unknown/unsupported entry kinds, empty allow-list and unsafe ranges are rejected. |
| 10 | Config fingerprint | Canonical sorted configuration payload makes semantically equal configurations identical across ordering and hash seeds. |
| 11 | Bounded state | O(1) OrderedDict lookup/eviction keeps processed setup and source feature state at configured limits. |
| 12 | Lifecycle counters | Added bounded structured counters for plans, rejections, gaps, duplicates and conflicts without retaining history. |
| 13 | Versioned checkpoint | State schema 2 contains config identity, stream identity, cursor, records and counters in JSON-compatible form. |
| 14 | Atomic restore | Corrupt, partial, duplicate, unknown, oversized, wrong-schema and wrong-config snapshots cannot partially mutate live state. |
| 15 | Duplicate semantics | Exact duplicate bars/features/setups are idempotent; output is not republished after restore. |
| 16 | Conflict and ordering | Conflicting duplicates/revisions fail closed; out-of-order and changed same-timestamp bars raise typed data errors. |
| 17 | Real Core API | Tested actual Core 0.4.0 `StrategyModule`, `Manifest`, `FeatureView`, `ModuleDecision`, checkpoint and engine journal. |
| 18 | Registry and planner | Tested public import path, package entry point, config loading, automatic producer→entry→consumer order and feature expiry. |
| 19 | Upstream contract | Documented exact canonical setup schema and proved producer replacement through feature contract only. |
| 20 | Downstream contract | Typed `EntryPlan.from_feature_value` supports downstream consumption; schema-1 read compatibility is retained. |
| 21 | Profiling | cProfile identified canonical conversion/hashing and object allocation as dominant costs; no I/O or history scan exists. |
| 22 | Hot-path optimization | Reused fingerprints, used early exits/O(1) state, and avoided binary-float financial semantics. |
| 23 | Standard/extended load | Deterministic 1k, 50k and 250k workloads measure throughput, digest stability, checkpoint size and bounded state. |
| 24 | Stress/restart load | 1m empty-event stress and mid-workload snapshot/restore compare output digests and continuation behavior. |
| 25 | Diagnostics | Structured diagnostics expose seen/planned/rejected/duplicate/conflict counts and bounded-state sizes. |
| 26 | Safe errors | User/data errors map to typed reason codes; unexpected failures fail closed without secrets or raw object dumps. |
| 27 | Static/package quality | Ruff, strict mypy, compileall, coverage gate, `pip check`, wheel and sdist are required release gates. |
| 28 | Clean installation | Wheel-only environment verifies Core dependency, public import, entry-point discovery, plan smoke and checkpoint smoke. |
| 29 | Integration readiness | Real same-bar producer→Entry Model→consumer engine pipeline and no-look-ahead truncation are release tests. |
| 30 | Bundle readiness | Handoff, integration contract, manifests, checksums, final audit and post-unpack rebuild/test verification are produced. |

## Audits after every second round

| Audit | Focus | Defects/findings and disposition | Decision |
|---:|---|---|---|
| 2 | Math/geometry | Canonical Decimal and equality semantics verified; invalid geometry remains rejected. | Continue |
| 4 | Temporal causality | Future publication/setup and extended-future invariance covered; no system clock in module. | Continue |
| 6 | Input/dependencies | Strict schema and canonical producer dependency verified through real planner. | Continue |
| 8 | Output/identity | Plan-ID collision and mutation risks closed with schema-2 identity validation. | Continue |
| 10 | Configuration | Stable canonical fingerprint and strict bool/range validation established. | Continue |
| 12 | Runtime state | State bounded and counters non-historical; no O(history) hot path. | Continue |
| 14 | Restore safety | Atomicity, bounds, version and config compatibility proven adversarially. | Continue |
| 16 | Gap/duplicate/order | Exact duplicates idempotent; conflicts/out-of-order fail closed and survive restart. | Continue |
| 18 | Core integration | No Core changes; registry, entry point, dependency plan and Feature Store lifetime verified. | Continue |
| 20 | Neighbor contracts | Generic canonical feature boundary retained; strategy-specific adapter remains external. | Continue |
| 22 | Performance | Profile-guided changes retained semantics and bounded memory; benchmark gate prepared. | Continue |
| 24 | Load/restart | Deterministic workload digests and bounded checkpoint established across long runs. | Continue |
| 26 | Observability | Reason codes/counters are structured, bounded and safe; no secret-bearing logging. | Continue |
| 28 | Packaging | Static gates and clean-wheel smoke defined; Python 3.12 runtime unavailable locally and documented. | Continue |
| 30 | Release candidate | Bundle structure, contracts, evidence and archive verification required before final verdict. | Final audit |

## Compatibility and residual risks

- Core 0.4.x / Module API 1.0 public interfaces are used without source changes.
- Public import path is `modular_modules.entry_model`.
- Plan schema 1 remains readable; new output is schema 2.
- State schema 1 is intentionally not silently migrated because it lacks source/config identity; Codex must start clean or provide an explicit migration.
- A canonical `setup.confirmed` producer/adapter is an integration responsibility, not a Core change.
- Python 3.12 runtime execution was not possible in this container; Ruff/mypy target 3.12 and Python 3.13.5 runtime gates are local evidence.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.


## Detailed audit evidence

The following evidence was rechecked at each audit boundary. Earlier audits used the then-current growing regression set; the final repeated gate contains 316 passing cases and 99% coverage. Benchmark values shown are the final reproducible measurements of the unchanged audited semantics.

### Audit after round 2

- **Focus:** Decimal mathematics and BUY/SELL geometry.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 4

- **Focus:** completed-bar timing, causality and future invariance.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 6

- **Focus:** input schema and declared dependencies.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 8

- **Focus:** output DTO, immutability and deterministic identity.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 10

- **Focus:** configuration types, ranges and fingerprint.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 12

- **Focus:** bounded state and lifecycle counters.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 14

- **Focus:** checkpoint schema and atomic restore.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 16

- **Focus:** gaps, duplicates, conflicts and event ordering.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 18

- **Focus:** real Core API, registry, planner and Feature Store.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 20

- **Focus:** upstream/downstream substitution contracts.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 22

- **Focus:** profile-guided hot path and complexity.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 24

- **Focus:** standard, extended, stress and restart loads.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 26

- **Focus:** observability, typed reasons and safe diagnostics.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 28

- **Focus:** static quality, packaging and clean installation.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

### Audit after round 30

- **Focus:** complete release bundle and integration readiness.
- **Changes reviewed:** all source/test changes from the preceding pair of rounds; no Core or neighboring module source.
- **Invariants:** one plan or typed rejection per canonical setup, completed-bar causality, deterministic full identity, bounded state, atomic restore, no execution side effects.
- **Public contracts:** Module API 1.0, `setup.confirmed` schema 1, plan/rejection schema 2, state schema 2; no undeclared feature key or import change.
- **Tests:** affected unit/regression/integration/property tests passed before continuation; final repeated suite is 316/316.
- **Coverage:** final branch-aware source coverage 99%, above 90% gate.
- **Benchmark:** final 50k median 12.101039 s; 250k median 61.914522 s; empty 1m median 11.951375 s. No semantic output change across measurements.
- **Memory:** state capped at 10,000 + 10,000 records; peak tracemalloc 17,072,481 bytes; max RSS 74,596 KiB.
- **No-look-ahead:** truncated/future-invariance and causality tests remain green.
- **Determinism:** canonical/hash-seed/replay tests and workload digests remain identical.
- **Checkpoint/restore:** JSON roundtrip, corruption rejection and restart continuation remain green.
- **Backward compatibility:** import/entry point retained; legacy plan schema 1 readable; state schema 1 intentionally not silently migrated.
- **Core compatibility:** actual Core 0.4.0 contracts used; no Core changes.
- **Neighbor compatibility:** generic upstream/downstream feature boundary retained; canonical setup adapter remains explicit integration dependency.
- **Residual risk:** Python 3.12 runtime unavailable locally; integration chain still needs canonical `setup.confirmed` producer.
- **Decision:** clean audit; proceed to the next round pair.

---

## Post-error hardening re-execution — 2026-07-21

The original 30 substantive rounds and 15 even-round audits above were re-executed after Error Discovery fixes rather than accepted solely from the archive. The post-error suite increased from 316 to 319 tests through the optimized-runtime and packaging regressions. Each round retained the same contract/no-look-ahead/checkpoint gates and used the post-error baseline in `POST_ERROR_FIX_BASELINE.md`.

| Round pair | Re-executed evidence | Audit decision |
|---|---|---|
| 1–2 | Explicit non-assert geometry validation; BUY/SELL/Decimal property suite; normal and optimized Python. | Audit 2 PASS. |
| 3–4 | Completed-bar, stale/future timing, truncation and future-invariance tests. | Audit 4 PASS. |
| 5–6 | Strict schema, namespace, producer/source identity and malformed payload adversarial tests. | Audit 6 PASS. |
| 7–8 | Frozen output, canonical payload, deterministic ID and mutation/tamper rejection tests. | Audit 8 PASS. |
| 9–10 | Strict config types/ranges/default dependencies and hash-seed-stable fingerprint tests. | Audit 10 PASS. |
| 11–12 | Lifecycle/counters and bounded 10k+10k state under long continuation. | Audit 12 PASS. |
| 13–14 | State schema, corrupt/partial/oversized/wrong-config snapshots and atomic restore. | Audit 14 PASS. |
| 15–16 | Exact/conflicting duplicates, gaps, out-of-order and restart continuation. | Audit 16 PASS. |
| 17–18 | Real Core 0.4.0 API, Registry, Feature Store, dependency planner and BacktestEngine. | Audit 18 PASS. |
| 19–20 | Canonical upstream producer and typed downstream consumer contract harness. | Audit 20 PASS. |
| 21–22 | Hot-path chunk measurements showed no O(history) degradation; current 50k throughput exceeded the stored RC median while preserving digest. | Audit 22 PASS. |
| 23–24 | 1k, 50k, 250k checkpoint-segmented, 1m empty and restart workloads; digests match prior uninterrupted evidence. | Audit 24 PASS. |
| 25–26 | Diagnostics/reason counters, fail-closed paths, Bandit and secret scan. | Audit 26 PASS. |
| 27–28 | Format, mypy, compile, wheel/sdist, explicit manifest, extracted-sdist tests, clean wheel install, SBOM and workflow hardening. | Audit 28 PASS locally; online vulnerability lookup pending CI. |
| 29–30 | Full lifecycle, distribution verifier, final docs, integration patch preparation and independent audit. | Audit 30 PASS locally; self-hosted CI required before ready-for-review. |

Post-error common evidence: 319 tests, 99% coverage, strict mypy/Ruff/compileall/Bandit pass, deterministic restart, atomic restore, no-look-ahead pass, real Core 0.4.0 compatibility, no Core changes. GitHub workflow execution is intentionally not claimed until the hardening branch run completes.
