# Changelog

## 0.4.2 — error-discovery hardening

### Fixed

- atomic module restore now rolls back the module that mutates before raising;
- durable JSON state loads are deeply immutable;
- unsupported checkpoint schemas fail closed;
- checkpoint integrity requires a valid SHA-256 digest and uses constant-time comparison;
- reconciliation detects divergent client-order identities and requires safe halt.

### Validation

- five new failing-before-fix regression tests;
- 297 repository tests passed;
- statement coverage 87.90%, branch coverage 71.93%;
- wheel/sdist, Twine metadata and wheel-only checkpoint/restore smoke passed.

## 0.4.1 — core hardening candidate

### Added

- bounded base-history and state/event/config size ceilings;
- checkpoint integrity and atomic restore;
- reconciliation foundations and deterministic evidence;
- executor-chain contract harness and load benchmarks.

### Fixed

- non-finite validation boundaries;
- event sequence/replay continuity;
- take-profit/GTD/OHLC ambiguity semantics;
- stale reduce-only orders;
- `contract_size` sizing, fee, risk and PnL accounting;
- cross-module setup ownership.

### Compatibility

Public Module API 1.x draft contracts and import paths remain available.

## Unreleased — analytical integration candidate

### Breakout–Return hardening

- added strict direct-constructor validation for integer, Decimal and optional feature-contract parameters;
- added optional reference feature producer, timeframe, schema-version and maximum-age guards without changing existing defaults;
- bound feature-mode ranges to an optional `reference_feature_source_module` so a later foreign producer with the same key cannot replace the intended causal range;
- included configured producer identity in the deterministic configuration fingerprint and fail-closed checkpoint compatibility contract;
- added configuration fingerprints and an internally versioned payload-v2 checkpoint with legacy payload-v1 migration;
- repaired the active payload-v1 -> payload-v2 -> second-restart lifecycle-counter chain;
- made restore fully atomic and strengthened unknown-key, extrema-horizon, stream-binding, deterministic-ID and geometry checks;
- canonicalized numeric duplicate signatures so equivalent representations such as `100`, `100.0` and `Decimal("100.00")` replay identically;
- added bounded diagnostic counters and explicit stale/schema reason codes;
- profiled and optimized the event hot path by precomputing Decimal thresholds, parsing each bar once and caching timeframe duration;
- added dedicated Python 3.12/3.13 self-hosted Breakout–Return CI, PEP 561 package metadata, an entry point, clean-wheel smoke and evidence artifacts;
- added adversarial, property-style, producer-collision, hash-seed, corrupted-state, restart, no-look-ahead and packaging tests;
- documented the core exact-version checkpoint migration limitation without changing the core.

### Added

- `SessionRangeModule` 0.1.0 as an independent DST-aware, gap-safe entry-point package;
- `MarketRegimeModule` 1.0.0 as an independent trend/range and volatility feature producer;
- FVG plugin 0.1.0 with strict completed three-candle detection, lifecycle features and PEP 561 typing metadata;
- production-grade `BreakoutReturnModule` 1.0.0 with rolling-range and feature-range modes;
- stateful Breakout–Return `ARMED -> CONFIRMED/REJECTED/EXPIRED/INVALIDATED` lifecycle;
- immutable active-setup feature revisions and one-bar confirmation features;
- standalone `LiquiditySweepModule` 0.1.0 under `strategy_modules`;
- confirmed swing-high/swing-low liquidity levels without look-ahead;
- closed-bar bullish/bearish sweep features, formal setup lifecycle and optional signals;
- deterministic JSON-compatible state and restart-aware feature republishing across stateful modules;
- `configs/integration.example.yaml` connecting all five modules through public Module API contracts;
- dependency regression proving `session_range.completed` places Session Range before Breakout–Return;
- deterministic NY range -> breakout -> return smoke with a no-look-ahead truncation check;
- Python 3.12/3.13 all-module CI with editable install, `pip check`, compileall, Ruff, tests, coverage, wheel build and clean wheel-only smoke;
- persisted smoke JSON, JUnit XML, coverage reports and wheel artifacts;
- final cross-module audit and evidence-based release checklist.

### Fixed

- combined shared `CHANGELOG.md`, `pyproject.toml` and package namespace changes without rewriting audited module logic;
- corrected invalid Market Regime integration example value `volatility_baseline_period=4` to the validated minimum `5`;
- updated root CI to install external modules required by the normal cross-module contract test;
- restricted root coverage measurement to `src` so external packages retain their own dedicated coverage gates;
- replaced outdated README installation, test-count and next-stage information with the actual integrated state.

### Validation

Baseline integration evidence snapshot `bdab3624ceff6df6ce2b1f2a1780a41efdee2ffb`:

- 244 root/core + Breakout–Return + Liquidity Sweep + integration tests passed;
- 84 Session Range tests passed;
- 80 Market Regime tests passed;
- 70 FVG tests passed;
- 478 total tests, 0 failures/errors/skips;
- root statement coverage 88.265306% (`3633 / 4116`, gate 85%);
- deterministic smoke: 514 events, 159 features, 2 setups, 1 confirmed SELL, run id `run-a913a83ae17411b4`;
- core, Session Range, Market Regime, FVG and all-module workflows passed on supported Python versions;
- wheels built and installed in a clean virtualenv before repeating the smoke.

Latest Breakout–Return evidence is recorded in `docs/ERROR_DISCOVERY_AUDIT.md`, `docs/FINAL_AUDIT.md` and the Draft module PR.

### Compatibility

- kernel code still imports no concrete strategy module;
- modules use Core 0.4.x / Module API 1.x draft contracts;
- existing Breakout–Return YAML remains valid and optional feature guards default to disabled;
- producer-bound feature lookup is opt-in and keeps the original producer-agnostic default fingerprint;
- Breakout–Return keeps manifest version `1.0.0` and state schema `1` so existing core checkpoints reach its payload-v1 migration path;
- external modules retain independent entry points and package versions;
- no stable application version or Module API freeze is declared.

### Release status

Production/live release is blocked. The integrated modules currently provide market context, patterns, setups and signals but no complete audited Entry/Risk/Exit chain or production paper/live/reconciliation adapters. Do not create `v1.0.0` or a production GitHub Release until `RELEASE_CHECKLIST.md` is closed.

## 0.4.0

### Added

- finite numeric validation and automatic UTC normalization;
- persistent trigger state for stop-limit/take-profit-limit orders;
- conservative configurable same-bar stop-limit policy;
- cached feature snapshots and indexed event-tail reads;
- deeply immutable feature/event payloads;
- durable JSONL EventStore and replay integrity verifier;
- atomic JSON StateRepository;
- versioned module checkpoints and terminal KernelCheckpoint;
- resolved module parameters in RunManifest and run identity;
- strict module configuration validation;
- Python 3.12/3.13 GitHub Actions matrix;
- 81 tests and four engineering audit reports.

### Fixed

- reduce-only order fee double counting;
- stop-limit orders forgetting a prior trigger;
- identical run IDs for different module parameters;
- acceptance of NaN/Infinity in market and ledger boundaries.

### Compatibility

Module API remains v1 draft. Existing stateless v0.3 modules continue to work. Stateful modules may opt into checkpoint restore by overriding `snapshot_state` and `restore_state`.

## 0.3.0

### Added

- ModuleManifest, YAML/import/entry-point loader and dependency planner;
- Feature Store with expiry, deletion and gap invalidation;
- formal Setup Registry and transition validation;
- InstrumentSpec and purpose-aware ExchangeRulesAdapter;
- market/limit/stop-market/stop-limit/take-profit order DTOs;
- GTC, IOC, FOK and GTD;
- post-only, reduce-only, trigger/limit price and close-position fields;
- partial fills, average fill price and remaining quantity;
- fill-based position ledger, optional scale-in and partial exits;
- EventEnvelope with correlation/causation and JSONL export;
- RunManifest and deterministic SHA-256 run identity;
- DataFeed, ExecutionAdapter, EventStore, Clock and StateRepository boundaries;
- DST-aware sessions and calendar-aligned timeframe buckets;
- warm-up/readiness and automatic HTF generation from manifests;
- extension contracts for funding, borrow, margin, liquidation, latency, spread, liquidity, order book, paper/live, optimizer, Monte Carlo and distributed research;
