# Breakout–Return v1.0.0 — development and audit report

## Scope

A standalone strategy module for Modular Strategy Core v0.4.0. The module detects a confirmed breakout and a later controlled return into the frozen reference range. It publishes setups, signals and immutable features, and intentionally does not own entry, stop, target or execution logic.

## Architecture

- Public class: `strategy_modules.breakout_return.BreakoutReturnModule`.
- Module API: v1 draft revision 3; no kernel changes.
- Reference inputs:
  - self-contained rolling high/low window; or
  - an exact required namespaced range feature declared in the instance manifest.
- Outputs:
  - `breakout_return.active_setup`;
  - `breakout_return.confirmed`;
  - `SetupEvent` and `Signal`.
- State machine: `SCANNING -> WAITING_RETURN -> CONFIRMED/REJECTED/EXPIRED/INVALIDATED`.
- State: monotonic extrema queues, stream binding, last-event signature, active setup and immutable feature revision.
- Financial comparisons: `Decimal(str(value))` at decision boundaries.

## Three completeness rounds

### Completeness round 1 — functional

Checked both breakout directions, body/close confirmation, strict boundary equality, breakout buffer, normalized return depth, inclusive depth boundaries, TTL and feature/rolling reference modes.

Findings and fixes:

1. The first draft included execution concerns. Orders, risk, stop and target duties were removed; the module now emits pattern facts only.
2. Active setup metadata needed the evolving breakout extreme. Added explicit extreme tracking and confirmation payload fields.
3. Feature-mode range changes were ambiguous. Added `invalidate` and `keep_snapshot` policies.

Result: all public inputs, outputs, states and terminal reasons are implemented and testable independently.

### Completeness round 2 — technical/integration

Checked ModuleManifest, parameter schema, dependency planning, feature ownership, setup transitions, JSON state, restart behavior, duplicate events and multi-instance isolation.

Findings and fixes:

1. A callable manifest was valid at runtime but less clear for static typing. Replaced it with an instance-specific `ModuleManifest` built during construction.
2. Immutable features cannot be updated in place. Added deterministic feature revisions and exact deletion of the previous feature ID.
3. Standard logging `extra` attempted to use the reserved `module` field. Renamed it to `module_name`.
4. Direct state restore accepted a side that contradicted breakout direction. Added semantic state validation.

Result: the module connects through the existing registry without a kernel branch or migration layer.

### Completeness round 3 — lifecycle/scenario

Executed creation, validation, registration contract, warm-up, breakout, extension, return, rejection, expiry, gap, duplicate, stop/restart/restore and continuation scenarios.

Findings and fixes:

1. Restart with a fresh FeatureStore could lose the active feature. Added deterministic republishing when the current revision is absent.
2. Gap bars could otherwise seed and detect in one operation. Gap handling now invalidates/reset first and only seeds warm-up.
3. State validation was expanded for stream binding, last signature, extrema order/bounds, revision, reference chronology and active sequence.

Result: happy path, edge paths and failure paths are explicit. Incomplete-bar validation remains the core/data-feed contract rather than duplicated module logic.

## Ten optimization rounds

### Round 1 — algorithm correctness

- Excluded the current bar from rolling reference construction.
- Required a later bar for return confirmation.
- Made breakout comparisons strict and return-depth boundaries inclusive.
- Confirmed contrarian side mapping: above→SELL, below→BUY.
- Added regression tests for no-lookahead, equality and same-bar prevention.

Validation: 58 local unit/regression tests passed.

### Round 2 — audit 1

Audit areas: architecture, temporal ordering, Decimal boundaries, feature ownership, setup transitions and no-lookahead.

Findings fixed:

- zero-valued configured thresholds were incorrectly passed to a positive-only Decimal validator;
- immutable active features could expose a stale breakout extreme;
- reserved logging fields could fail when INFO logging was enabled.

Validation: full local suite passed; regression tests added for threshold and feature revision behavior.

### Round 3 — performance

- Replaced repeated `max`/`min` scans with monotonic deques.
- Rolling update is amortized O(1); memory is bounded by `lookback_bars`.
- Benchmark: 30,000 bars, lookback 1,000:
  - optimized module: 1.062396 s;
  - naive extrema rescans: 2.413045 s;
  - measured ratio: 2.27× in favor of optimized module.

Validation: extrema were compared against a naive reference over 400 deterministic pseudo-random bars.

### Round 4 — audit 2

Audit areas: hot path, deterministic identity, repeat processing, gap boundaries and multi-instrument use.

Findings fixed:

- active feature replacement now deletes only the previous revision rather than a broad key;
- deterministic setup IDs include symbol, timeframe, direction and UTC close time;
- one instance now rejects symbol/timeframe switching instead of silently contaminating state.

Validation: full local suite passed; parallel BTC/ETH instances remained independent.

### Round 5 — resilience

- Added exact duplicate idempotency and contradictory-duplicate rejection.
- Added out-of-order rejection.
- Added gap invalidation and rolling warm-up reset.
- Added range missing/change invalidation.
- Added JSON-safe state snapshot and restart continuation.

Validation: full local suite passed.

### Round 6 — audit 3

Audit areas: partial/corrupt state, restart, missing FeatureStore state and stream consistency.

Findings fixed:

- state restore now rejects invalid side/direction, extreme geometry, sequence/revision, timestamps, stream binding and malformed extrema;
- missing active feature after restart is republished deterministically;
- duplicate replay after restore does not mutate state or duplicate signals.

Validation: state round-trip and negative restoration scenarios passed.

### Round 7 — public API quality

- Kept constructor parameters YAML-compatible and explicit.
- Made required feature dependency instance-specific.
- Kept all execution actions empty by contract.
- Namespaced every produced feature and reason.
- Added enums, frozen configuration and typed public exports.

Validation: manifest/config tests passed.

### Round 8 — audit 4

Audit areas: replaceability, dependency planning and downstream interoperability.

Findings fixed:

- confirmation feature is valid on exactly the confirmation close, making it visible to later modules in the same deterministic pipeline;
- active and confirmed payloads now contain stable, documented fields;
- feature-mode producer ordering is enforced through `required_features`.

Validation prepared: four real-engine integration tests cover producer→detector→consumer ordering, lifecycle, checkpoint and YAML loading.

### Round 9 — tests and observability

- Added 48 unit tests and 11 regression cases (59 local tests total).
- Added four BacktestEngine integration tests.
- Added structured diagnostics for scanning, arm, wait, confirm, reject, expire, gap and reference invalidation.
- Added standard-library transition logging and benchmark script.
- Local module statement coverage: 91%; critical algorithm/config paths have direct tests.

Validation: full local suite passed.

### Round 10 — audit 5

Audit areas: architecture, algorithm, state, failure handling, performance, API, documentation and packaging.

Findings fixed:

- moved module-specific fixtures under `tests/breakout_return` to avoid replacing the core test fixture file;
- removed unused imports and compiled all new source/test/example/benchmark files;
- added scoped Ruff and mypy CI checks without linting unrelated legacy code;
- the first repository CI run found three strict typing defects in generic checkpoint integer conversion; added a field-aware integer parser and a regression test for non-integer state.

Validation: ten consecutive pre-CI local full-suite runs passed, each 58/58; final typed-state regression increased the local set to 59/59.

## Intermediate audit summaries

### Audit after round 2

- Findings: zero-threshold Decimal defect, stale immutable feature, reserved logging field.
- Resolution: fixed and regression-tested.
- Decision: accepted.

### Audit after round 4

- Findings: broad feature replacement risk, stream contamination risk, deterministic identity requirements.
- Resolution: revision IDs, exact deletion, instance binding and deterministic setup IDs.
- Decision: accepted.

### Audit after round 6

- Findings: semantically corrupt state could pass structural parsing; FeatureStore is not part of module state.
- Resolution: strict semantic restore checks and active-feature republishing.
- Decision: accepted.

### Audit after round 8

- Findings: downstream same-bar semantics and feature dependency ordering required explicit proof.
- Resolution: one-bar confirmation feature and real-engine producer/consumer integration test.
- Decision: accepted; repository CI later confirmed the integration contract.

### Audit after round 10

- Findings: test fixture path initially conflicted with the repository root fixture; lint/type checks were absent from CI.
- Resolution: isolated fixture tree and scoped Ruff/mypy steps.
- Decision: accepted after the repository CI finding below was corrected and re-verified.

## Final audit

### Architecture

Passed locally and in repository CI. No kernel strategy branch, cyclic dependency or hidden execution ownership was introduced.

### Trading and temporal logic

Passed locally and through the real BacktestEngine suite. Closed-bar only, previous-window reference, no same-bar return, strict breakout equality, inclusive return depth and explicit direction mapping are regression-tested.

### Data and state

Passed locally and in CI. Duplicate, contradictory duplicate, out-of-order, gap, restart, corrupt state and multi-instance cases are covered.

### Tests and performance

- Unit tests: 48 passed.
- Regression tests: 11 passed.
- Real-core integration tests: 4 passed.
- Full repository pytest suite: 144 passed on Python 3.12 and 144 passed on Python 3.13 (81 baseline + 63 new).
- Ten consecutive pre-CI local runs: passed; final local set: 59/59.
- Local module statement coverage: 91%; repository coverage gate >=85% passed on both Python versions.
- Benchmark: 2.27× versus naive extrema rescanning in the recorded workload.

### Configuration and documentation

Passed locally. All parameters, ranges, outputs, failure reasons, examples and conscious limitations are documented.

### Repository validation status

The first draft-PR CI run passed compilation and Ruff but exposed three strict-mypy errors where generic checkpoint values were converted with `int(object)`. Publication remained blocked. The module was corrected by adding a strict typed integer parser that accepts only integer/string checkpoint encodings, rejects booleans/floats, and reports field-specific errors. A corrupt-state regression case for `sequence=1.5` was added.

Final GitHub Actions validation on Python 3.12 and 3.13 passed all steps:

- dependency installation;
- `compileall`;
- scoped Ruff;
- strict mypy for the module package;
- full repository pytest;
- coverage threshold `>=85%`;
- wheel build.

No critical or major final-audit findings remain.

## Known limitations

1. The core/data feed must provide completed candles; the module does not duplicate raw-stream candle-completion checks.
2. Entry, stop, target, sizing and busy-position policy are intentionally external.
3. One module instance handles one symbol/timeframe.
4. OHLC input cannot reveal intrabar path; the detector uses closed-bar semantics only.
5. Rolling reference is a technical range; session/HTF ranges should use feature mode.

## Final verdict

**ГОТОВ К ИНТЕГРАЦИИ.**
