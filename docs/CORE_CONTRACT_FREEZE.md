# Core 0.4.2 Contract Freeze

Status: **FROZEN FOR MODULE INTEGRATION**

This document freezes the public contracts of Modular Strategy Core 0.4.2 for the
first project-wide module integration. It is not a package release, tag or merge
authorization.

## Accepted evidence

The accepted technical evidence head is
`686442152bb415f4d6628b2190b590b8acf9c555` in Draft PR #164.
Authoritative run `30073884923` passed every executable gate on Python 3.12 and
3.13:

- 981 Core/root tests per interpreter;
- 84 Session Range, 80 Market Regime and 70 FVG tests per interpreter;
- 1,215/1,215 total tests per interpreter;
- failures/errors/skips: 0/0/0;
- branch-aware coverage: 88%;
- contract snapshot verification from source, installed wheel and installed
  normalized sdist;
- byte-identical wheel
  `5209eaa2628b4f89cd26daf6fdb6b20c730c7c9bcaf9a285e5f727f087b0cd99`;
- byte-identical normalized sdist
  `781debf6e3f05f2687c8c3f7ba963f38b0ba17442e0e729c6a7e20dd2b63408d`.

Issue #103 and the canonical Core handoff record this evidence and the freeze
change gate.

## Change gate

After this freeze, Core source and public contracts may change only when all of
the following are true:

1. a failing integration or recovery scenario reproduces the defect;
2. the affected module/Core ownership boundary is documented;
3. a focused regression is added before or with the fix;
4. the complete Core Python 3.12/3.13 quality matrix remains green;
5. the contract snapshot is intentionally updated and the compatibility impact
   is recorded.

Convenience changes, speculative abstractions, new trading behavior and
coverage-only refactors are deferred until after integration.

## Frozen public surfaces

The machine-readable snapshot is `docs/contracts/core-v0.4.2.json` and is
enforced by `tests/test_core_contract_freeze.py`.

Frozen categories:

- module lifecycle: `StrategyModule`, `ModuleDecision`, `MarketView`;
- module discovery: `ModuleManifest`, `DependencyPlan`, `ModuleRegistry`;
- feature/setup evidence: `Feature`, `FeatureDelete`, `SetupEvent`,
  `SetupRecord`, `SetupTransition`;
- execution requests and snapshots: `OrderRequest`, `CancelRequest`,
  `PositionUpdateRequest`, `ClosePositionRequest`, `OrderSnapshot`,
  `PositionSnapshot`;
- event and persistence evidence: `EventEnvelope`, `EventStore`,
  `KernelCheckpoint`, `ModuleState`, `RunManifest`, `StateRepository`;
- reconciliation: `ExecutionStateSnapshot`, `ReconciliationReport`,
  `reconcile_execution_state`;
- market context: `Candle`, `TimeframeSpec`, `SessionDefinition`,
  `ReadinessView`;
- enum values for side, position side, order type, time in force, order status,
  setup status and reason codes;
- the complete package-level `modular_core.__all__` export list.

## Ownership boundaries

Core owns:

- immutable DTO validation and canonicalization;
- deterministic module lifecycle and dependency planning;
- feature/setup state;
- backtest execution primitives and event evidence;
- checkpoint, state and replay contracts;
- generic exchange-rule normalization and reconciliation interfaces.

Core does not own:

- strategy rules or signal generation;
- Router conflict resolution or trade-plan composition;
- Paper Execution orchestration and venue-specific order lifecycle;
- Trading Runner process orchestration;
- research experiment scheduling or result ranking;
- live venue access.

The canonical Router `DownstreamPlanEnvelope` remains an external upstream
contract. Paper Execution must adapt that envelope without recalculating side,
risk, quantity, stop, target, sequencing or OCC/hash evidence.

## Compatibility policy

Backward-compatible additions require a new contract-snapshot entry and full
integration evidence. Removing or renaming exports, DTO fields, enum values or
method parameters is breaking and requires explicit project approval.

Persisted schema evolution must be versioned and either migrated
transactionally or rejected fail-closed. Silent coercion and best-effort
reinterpretation are not permitted.

## Integration entry conditions

The Core entry conditions are complete:

- Engine construction/factory/input boundaries passed the full matrix;
- the snapshot test passed from source, wheel and normalized sdist;
- Session Range, Market Regime and FVG contract suites remained green;
- the integration harness passed deterministic lifecycle, failure isolation,
  event replay and checkpoint state scenarios;
- issue #103 and the Core handoff record the accepted evidence SHA.

Planned Core development is stopped. The next project action is to wait for
Paper Execution readiness and then begin module integration against this frozen
contract.
