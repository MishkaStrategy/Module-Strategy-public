# Changelog

## 0.2.2 — GitHub-resumption hardening

- completed mandatory error discovery and regression fixes;
- strengthened risk feedback, reconciliation, Decimal serialization and state schema 4 integrity;
- added full projected-position RiskRequest evidence and PS-CCR-001;
- added bounded load/restart benchmarks and audit-ready diagnostics;
- retained Core 0.4.x / Module API 1.x compatibility.

## 0.2.1 — error-discovery hardening

### Fixed

- partial risk approvals now reconcile against the approved quantity;
- overfills fail closed in `RECOVERY_REQUIRED`;
- zero-residual strategic reductions are rejected without crashing;
- rebalance safety follows the computed increase/decrease direction;
- plan DTOs enforce action, direction, total-quantity and weighted-average invariants;
- Decimal serialization is context-independent and lossless;
- approval/rejection feedback is restart-safe, idempotent and not masked by unrelated features;
- legacy schema-1/2 pending plans migrate to state schema 3;
- setup and risk-decision identity/schema checks are strict.

### Validation

- 187 tests passed;
- 91% branch-aware coverage, gate 90%;
- Ruff, Ruff format, strict mypy and compileall passed.

## 0.2.0 — local release candidate

### Added

- standalone planner/approval Module API pipeline;
- averaging down, pyramiding, fixed/weighted DCA, scale-out, rebalance and stop-scaling policies;
- typed plan, approved and rejected DTOs;
- versioned state schema 2 with schema-1 migration;
- real Core 0.4.1 FeatureStore, ModuleRegistry and BacktestEngine integration tests;
- bounded checkpoint/restart, partial-fill reconciliation and recovery behavior;
- benchmark, profiling, clean-wheel evidence and Codex handoff documentation.

### Fixed

- bar-duration expiration;
- STOP_SCALING zero-quantity round-trip;
- stop weakening and stale factual-position approval;
- partial-fill stage completion;
- gap/exit action priority;
- planner → risk → approval dependency order;
- deterministic datetime/Decimal canonicalization;
- retry behavior after missing context;
- test isolation from local Core imitations.

### Validation

- 173 tests passed;
- 93% branch-aware coverage;
- Ruff format/lint, strict mypy and compileall passed;
- wheel, sdist and clean installation passed.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
