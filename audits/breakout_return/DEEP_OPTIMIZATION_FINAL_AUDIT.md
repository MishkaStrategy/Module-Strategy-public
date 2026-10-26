# Breakout–Return deep optimization and final audit

## 1. Scope and source of truth

Module: `strategy_modules.breakout_return.BreakoutReturnModule`.

Base branch: `integration/modular-strategy-v0.5.0`.

Base commit: `5e03b933207a99940244634f49a756d27888a5c3`.

Working branch: `modules/breakout-return-v1.1.0-hardening`.

Draft PR: #18.

Core: Modular Strategy Core `0.4.0`.

Module API: `1.0 draft`, revision 3.

Public package import: `strategy_modules.breakout_return:BreakoutReturnModule`.

The audited public class is a compatibility-preserving hardening layer over the already integrated v1 state machine. It inherits the original trading algorithm and narrows configuration, serialization, restore, diagnostics and domain invariants. No second trading algorithm, kernel branch or alternative Module API was introduced.

The module manifest deliberately remains `version="1.0.0"` and `state_schema_version="1"`. The core currently performs exact module-version/schema matching before calling module-level restore. Bumping those manifest values would make the module's internal payload-v1 migration unreachable. This core limitation is documented separately in `CORE_CHANGE_REQUEST.md`; core was not changed in this PR.

## 2. Baseline

Baseline evidence was captured in `DEEP_OPTIMIZATION_BASELINE.md`.

Baseline repository evidence:

- integration PR #8 green at the base commit;
- 478 repository/module tests passed in the recorded integration artifact;
- root coverage: `88.265306%`;
- Breakout–Return tests: 63;
- Breakout–Return `module.py` coverage: `89.46%`;
- Python CI matrix: 3.12 and 3.13;
- deterministic all-module smoke run ID: `run-a913a83ae17411b4`.

Baseline local performance on CPython 3.13.5:

| Events | Runs | Median | Throughput | Peak memory | Checkpoint | Restore median |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 5 | 0.070854 s | 14,114 events/s | 21,942 B | 390 B | 13.78 µs |
| 50,000 | 5 | 4.465197 s | 11,198 events/s | 457,708 B* | 446 B | 15.12 µs |

`*` The baseline memory measurement included a caller-owned growing visible-bar list. The final benchmark uses one-bar `MarketView.base_bars`, so the module's bounded memory is measured directly.

## 3. Three completeness rounds

### Completeness round 1 — functional

Verified:

- BUY and SELL symmetry;
- strict breakout boundaries;
- inclusive return-depth boundaries;
- body and close confirmation modes;
- rolling and feature reference modes;
- rejection, expiration, invalidation and reset paths;
- no signal on the breakout bar;
- no recalculation of a past decision from a future suffix.

Findings:

1. Direct integer configuration accepted `bool`/wrong numeric types in some paths.
2. Financial parameters could lose identity when a high-precision `Decimal` was converted into public float parameters.
3. Feature-specific filters could be configured while rolling mode silently ignored them.
4. Feature range IDs accepted whitespace-surrounded identifiers.

Resolution:

- strict type/range validation;
- exact public-parameter round-trip validation;
- feature-only parameter dependency validation;
- trimmed identifier contract;
- regression tests for every defect.

Result: clean.

### Completeness round 2 — technical and integration

Verified:

- real `StrategyModule`, `ModuleManifest`, `MarketView` and `ModuleDecision`;
- Feature Store point-in-time lookup;
- dependency planner contract;
- import-path and entry-point loading;
- immutable outputs and feature revisions;
- reason-code namespaces;
- JSON checkpoint payload;
- configuration fingerprint;
- payload-v1 migration;
- real BacktestEngine lifecycle.

Findings:

1. Direct restore could accept structurally valid state from incompatible parameters without a fingerprint.
2. Restored extrema were not fully constrained to the restored rolling horizon.
3. A feature without expiry could remain eligible indefinitely.
4. Optional upstream timeframe/schema expectations were not expressible.
5. Root packaging lacked a Breakout–Return entry point and PEP 561 marker at the initial hardening baseline.

Resolution:

- deterministic configuration fingerprint;
- strict extrema horizon/current-bar validation;
- optional age, timeframe and schema guards;
- root entry point and `py.typed` packaging;
- contract tests with the real registry and engine.

Result: clean locally.

### Completeness round 3 — operational lifecycle

Verified lifecycle:

`constructor -> validation -> registry -> start -> warm-up -> processing -> output -> rejection/gap -> checkpoint -> restart -> restore -> continuation -> finish`.

Adversarial scenarios:

- exact duplicate;
- numeric-equivalent duplicate;
- conflicting duplicate;
- out-of-order event;
- symbol/timeframe switch;
- data gap;
- corrupted/partial/oversized state;
- incompatible payload version;
- incompatible configuration fingerprint;
- malformed active setup;
- invalid extrema;
- invalid stream binding;
- repeated event after restore;
- two independent instances;
- restart in a long workload.

Findings:

1. Duplicate signatures were representation-sensitive in the original v1 baseline.
2. Active-path diagnostics reported the previous sequence on several terminal/waiting paths.
3. Restore could parse an invalid timeframe after beginning live-state assignment.
4. Counter totals could contradict active/terminal lifecycle state.
5. Persisting duplicate/restore observation counters made checkpoint snapshots differ after an otherwise idempotent replay.

Resolution:

- canonical numeric signatures;
- current-sequence diagnostics;
- pre-validation of derived timeframe state;
- lifecycle counter invariants;
- causal checkpoint counters that exclude replay/restore observation side effects;
- atomic failure regression tests.

Result: clean locally.

## 4. Thirty substantive rounds

| # | Goal | Implementation/evidence | Result |
|---:|---|---|---|
| 1 | Formula correctness | Re-derived above/below breakout and return-depth formulas; direct boundary tests. | Clean |
| 2 | Decimal and symmetry | BUY/SELL mirrored workloads, exact/inclusive bounds, NaN/inf/type rejection. | Clean |
| 3 | Completed-bar semantics | Decision time tied to `close_time`; breakout bar cannot confirm return. | Clean |
| 4 | No look-ahead | Current bar excluded from rolling reference; truncated and future-suffix invariance tests. | Clean |
| 5 | Input feature contract | Namespaced required feature, symbol/timeframe point-in-time selection. | Clean |
| 6 | Stale/schema/timeframe input | Added optional max-age, schema and timeframe guards with structured reasons. | Clean |
| 7 | Output contract | Immutable feature values, exact active revision deletion, typed ModuleDecision. | Clean |
| 8 | Identifiers/reasons | Deterministic setup/feature IDs and namespaced reason codes; whitespace IDs rejected. | Clean |
| 9 | Configuration strictness | Bool/float integer misuse, unknowns, empty strings and invalid parameter dependencies rejected. | Clean |
| 10 | Config identity | Canonical financial values and SHA-256 configuration fingerprint added to state. | Clean |
| 11 | Lifecycle state | Explicit scanning/waiting transitions and bounded monotonic queues reviewed. | Clean |
| 12 | Counters/diagnostics | Bounded counters, current sequence and lifecycle-total invariants. | Clean |
| 13 | Checkpoint schema | Internal payload v2, exact keys, canonical Decimal strings, bounded state. | Clean |
| 14 | Restore/migration | Payload-v1 migration, incompatible config/version rejection, atomic restore validation. | Clean |
| 15 | Duplicate semantics | Canonical `100 == 100.0 == Decimal("100.00")` duplicate identity. | Clean |
| 16 | Gap/order/replay | Conflicting duplicate, out-of-order, stream switch, gap reset and post-restore replay tests. | Clean |
| 17 | Core contract | Real core DTOs and BacktestEngine used; no simplified substitute implementation. | Clean |
| 18 | Registry/packaging | Import path, module entry point and PEP 561 marker; clean-wheel discovery smoke. | Clean |
| 19 | Upstream compatibility | Session Range feature contract remains producer-agnostic and declared in manifest. | Clean |
| 20 | Downstream compatibility | One-bar confirmation feature remains same-bar visible to later planned modules. | Clean |
| 21 | Hot-path profiling | Profiled Decimal/signature/window path before optimization. | Clean |
| 22 | Incremental performance | Single OHLCV conversion per event, cached factors/timeframe, O(1) monotonic queues. | Improved |
| 23 | Smoke/standard load | Five-run 1,000 and 50,000 event benchmarks with median/min/max/memory. | Clean |
| 24 | Extended/stress load | 250,000 three-run restart workload and 1,000,000 event restart stress. | Clean |
| 25 | Observability | Fixed counters for gap, duplicate, setup terminals, restore and feature republish. | Clean |
| 26 | Diagnostic safety | Structured non-secret messages; no arbitrary key accumulation or hidden I/O. | Clean |
| 27 | Static quality | Ruff clean, strict mypy clean, compileall clean, local package coverage 91%. | Clean |
| 28 | Wheel/clean install | Wheel built, installed in clean venv, `pip` dependencies resolved, entry point and `py.typed` verified. | Clean |
| 29 | Final lifecycle/replay | 135 local tests; deterministic semantic state with interrupted/restarted workload. | Clean |
| 30 | Integration readiness | Docs, config examples, benchmark, CCR, Draft PR and release blockers reviewed. | Conditional |

## 5. Fifteen audits after every second round

### Audit 1 — after rounds 1–2

Invariant: mathematical symmetry and Decimal boundary correctness.

Finding: high-precision Decimal could not be represented identically in the public float parameter mapping.

Severity: High for deterministic run identity.

Fix: reject non-round-trippable values before startup; add regression.

Decision: continue.

### Audit 2 — after rounds 3–4

Invariant: causal availability.

Finding: no new look-ahead defect; existing previous-window ordering is correct.

Evidence: same-bar rejection, truncated stream and future-suffix invariance.

Decision: continue.

### Audit 3 — after rounds 5–6

Invariant: explicit upstream feature contract.

Finding: unbounded producer features could remain eligible and schema/timeframe assumptions were implicit.

Severity: Medium.

Fix: optional age/schema/timeframe filters with namespaced reason codes and no default behavior change.

Decision: continue.

### Audit 4 — after rounds 7–8

Invariant: immutable and deterministic outputs.

Finding: whitespace-surrounded `range_id` could create restart-unstable identity.

Severity: Medium.

Fix: require trimmed nonblank identifier; regression test.

Decision: continue.

### Audit 5 — after rounds 9–10

Invariant: configuration is validated and fingerprintable before execution.

Finding: feature-only options were silently irrelevant in rolling mode.

Severity: Medium.

Fix: parameter dependency validation and canonical fingerprint payload.

Decision: continue.

### Audit 6 — after rounds 11–12

Invariant: every ARMED setup is either active or terminal exactly once.

Finding: restored counters could contradict active lifecycle state; active diagnostics could show the prior sequence.

Severity: High for auditability.

Fix: lifecycle equation and current-sequence diagnostics.

Decision: continue.

### Audit 7 — after rounds 13–14

Invariant: restore is atomic and configuration-compatible.

Finding: derived timeframe parsing occurred too late; core exact manifest checks block an honest public schema/version bump.

Severity: High for restart safety; external blocker for version migration.

Fix: pre-validate timeframe and all new invariants before base restore; create `CORE_CHANGE_REQUEST.md`; do not change core.

Decision: continue with unchanged manifest version.

### Audit 8 — after rounds 15–16

Invariant: replay and duplicate processing are deterministic.

Finding: observation counters changed persisted checkpoints after duplicate/restore despite unchanged trading state.

Severity: Medium.

Fix: persist causal counters only; keep duplicate/restore counters available in live diagnostics.

Decision: continue.

### Audit 9 — after rounds 17–18

Invariant: real-kernel compatibility and replaceable loading.

Finding: root package initially lacked a Breakout–Return entry point and type marker.

Severity: Medium for plugin discovery and typing.

Fix: add entry point, `py.typed`, real-registry and clean-wheel checks.

Decision: continue.

### Audit 10 — after rounds 19–20

Invariant: no hidden coupling to Session Range or a downstream executor.

Finding: none. Required feature key remains configurable; no concrete producer import exists.

Residual risk: downstream Entry/Risk/Exit chain is not yet integrated in the wider project.

Decision: continue.

### Audit 11 — after rounds 21–22

Invariant: no semantic change for speed.

Finding: repeated Decimal/signature work was measurable in the baseline hot path.

Fix: cached thresholds/factors/timeframe and one parsed bar-value object per event.

Result: 50,000-event median improved from 4.465 s to 3.651 s in the final public implementation, approximately 18.2% faster.

Decision: continue.

### Audit 12 — after rounds 23–24

Invariant: bounded memory and deterministic restart on long workloads.

Evidence:

- 50,000 events: median 3.651 s, 13,694 events/s, peak 6,611 B;
- 250,000 with restart: median 4.759 s, 52,531 events/s;
- 1,000,000 with restart: 19.158 s, 52,197 events/s;
- checkpoint remains below 1 KiB in the recorded synthetic workloads;
- semantic state hashes are identical across repeated runs.

Decision: continue.

### Audit 13 — after rounds 25–26

Invariant: diagnostics are bounded, structured and safe.

Finding: no secret/system-time/file/network access; counter key set is fixed.

Fix: causal checkpoint projection separates observability side effects from trading state.

Decision: continue.

### Audit 14 — after rounds 27–28

Invariant: distributable quality.

Local evidence on CPython 3.13.5:

- Ruff: passed;
- strict mypy: passed;
- compileall: passed;
- 135 tests: passed;
- package coverage: 91%, gate 90%;
- wheel built successfully;
- clean virtualenv installation: passed;
- public import, entry point, `py.typed` and real BacktestEngine SELL smoke: passed.

External finding: GitHub Actions runs currently fail before any job step is created, affecting core, integration, Market Regime and Breakout–Return workflows simultaneously. This is repository/Actions infrastructure state, not a test failure, but it prevents the required green CI evidence.

Decision: continue to final audit; retain Draft status.

### Audit 15 — after rounds 29–30

Invariant: integration candidate must not overclaim release readiness.

Finding: module-local engineering gates are clean, but final GitHub CI is not green and the stable core migration contract remains unresolved.

Resolution: PR remains Draft; no merge, tag or production-ready claim; CCR retained.

Decision: conditionally ready, blocked from final integration approval until CI executes successfully.

## 6. Final independent audit

### Functional and trading logic

Passed locally:

- above breakout/return -> SELL;
- below breakout/return -> BUY;
- strict breakout equality;
- inclusive return-depth limits;
- body/close confirmation;
- rejection, expiry and invalidation;
- no order, portfolio or execution ownership.

### Temporal causality and no-look-ahead

Passed locally and supported by the existing cross-module smoke:

- rolling reference excludes current bar;
- feature selection is point-in-time;
- future feature/state suffix does not alter past output;
- breakout bar cannot confirm return;
- return signal appears only after the required return candle closes.

### Configuration and contracts

Passed locally:

- strict enums/integers/numeric values;
- no bool-as-int;
- no NaN/inf;
- exact financial parameter identity;
- namespaced/trimmed keys and identifiers;
- explicit feature-only dependencies;
- deterministic config fingerprint.

### State, checkpoint and restart

Passed locally:

- JSON-compatible payload;
- exact keys;
- payload-v1 internal migration;
- incompatible config/version rejection;
- extrema and active geometry validation;
- atomic restore failure;
- stream binding validation;
- causal checkpoint idempotence after duplicate/restore;
- deterministic continuation through a midpoint restart.

### Performance and memory

Final public implementation, CPython 3.13.5:

| Workload | Runs | Median | Min | Max | Throughput | Peak | Checkpoint | Restore |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 5 | 0.078555 s | 0.073120 | 0.084231 | 12,730/s | 6,407 B | 920 B | 46.55 µs |
| 50,000 | 5 | 3.651361 s | 3.551519 | 3.769211 | 13,694/s | 6,611 B | 883 B | 42.95 µs |
| 250,000 + restart | 3 | 4.759120 s | 4.605572 | 5.006399 | 52,531/s | not traced | 871 B | 43.04 µs |
| 1,000,000 + restart | 1 | 19.158167 s | same | same | 52,197/s | not traced | 982 B | 46.80 µs |

The measured state remains bounded by `lookback_bars`; no O(history) recomputation or unbounded event retention was found.

### Packaging

Passed locally:

- wheel build;
- clean venv installation;
- public import;
- `modular_strategy.modules / breakout_return` entry point;
- PEP 561 `py.typed` marker;
- real BacktestEngine smoke.

Python 3.12 remains covered by the baseline integration evidence but the final head cannot yet obtain new 3.12 CI evidence because Actions jobs do not start.

### Backward compatibility

- constructor defaults preserve v1 behavior;
- optional feature guards default to disabled;
- public package import path is unchanged;
- manifest version/schema remain unchanged to preserve current core checkpoint compatibility;
- payload-v1 state is accepted internally;
- no core or neighboring module source was changed.

### Core change request

One CCR exists. It asks the core checkpoint layer to distinguish API compatibility, module semantic version and state migration capability instead of requiring exact module-version/schema equality before invoking module restore.

The request is documented only. It was not implemented in this module PR.

## 7. Defects fixed

High:

1. Configuration identity loss for high-precision Decimal values.
2. Restore from incompatible configuration without fingerprint protection.
3. Incomplete extrema-horizon validation.
4. Non-atomic derived timeframe restoration.
5. Counter lifecycle inconsistent with active setup.
6. Audit diagnostics reporting a previous active sequence.

Medium:

7. Representation-sensitive duplicate identity.
8. Missing stale/schema/timeframe feature guards.
9. Whitespace-surrounded range identity.
10. Noncanonical exponent-style Decimal checkpoint strings.
11. Replay/restore observation counters changing persisted checkpoints.
12. Missing root entry point and `py.typed` evidence.
13. Single-result benchmark without median/min/max/memory/restore/state hashes.

All listed defects have tests or reproducible benchmark evidence.

## 8. Remaining limitations and risks

1. The core/data feed must provide completed candles.
2. OHLC cannot reveal intrabar path.
3. One module instance remains bound to one symbol/timeframe.
4. Entry, stop, target, sizing, execution and exit management are intentionally external.
5. Core-level module-version/state-schema migration remains blocked by the current exact-match checkpoint contract.
6. Final GitHub Actions evidence is unavailable because workflows fail before job steps begin. No assertion is made that the final head has green CI.
7. The wider project still lacks the complete audited Entry/Risk/Execution/Exit production chain.

## 9. Final verdict

**CONDITIONALLY READY / УСЛОВНО ГОТОВ.**

The Breakout–Return module is locally clean for research/backtest integration: functional, no-look-ahead, deterministic, bounded, restart-safe, typed, packaged and benchmarked. It must not be merged or declared fully integration-ready until GitHub Actions executes the final Python 3.12/3.13, full repository and cross-module gates successfully.
