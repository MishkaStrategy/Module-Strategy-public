# Baseline audit — core hardening v0.4.x

## Source of truth

- Repository: `mishkacher/Module-Strategy`
- Integration base branch: `integration/modular-strategy-v0.5.0`
- Baseline commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Baseline integration PR: #8 (Draft, mergeable, all recorded workflows successful)
- Hardening branch: `core/hardening-v0.4.x`
- Core version: `0.4.0`
- Module API status: draft; public contracts frozen for parallel executor module development.

## Environment

- Local Python: 3.13.5
- Platform: Linux x86_64, glibc 2.41
- Supported CI Python: 3.12 and 3.13
- Core dependencies: PyYAML >= 6.0
- Integrated wheel evidence installed locally: Session Range 0.1.0, Market Regime 1.0.0, FVG 0.1.0.
- Integration artifact smoke run ID: `run-a913a83ae17411b4`.

## Baseline evidence

- Core tests: 81 passed, 0 failed.
- Local core branch coverage: 80% (statement coverage differs from PR's root statement coverage gate).
- PR #8 root statement coverage: 88.265306% (3633 / 4116).
- Baseline test elapsed: 1.41 s; max RSS 52,012 KiB.
- Compileall: passed.
- Wheel build: passed.
- `pip check`: passed after installing verified integration wheels.
- Baseline no-op benchmark: 20,000 bars × 10 modules = 4.3139 s on the local environment.
- Integration smoke artifact: 514 events, 159 features, 2 setups, 1 confirmed SELL signal.

## Baseline release-gate defects

### High

1. Strict mypy does not pass for the core: 17 errors across six files.
2. Repository-wide Ruff on core + core tests does not pass: 17 findings.

### Medium

1. Branch coverage is materially lower than statement coverage and uncovered branches cluster in engine, execution, feature store and dependency planning.
2. The current benchmark is a single sample and records neither median nor peak memory.
3. Core checkpoint integrity is based on identity generation but persisted checkpoint payloads have no independent checksum verification on load.
4. Durable JSONL event persistence has no explicit maximum line/event size guard.

## Public contract inventory (frozen)

- `StrategyModule`
- `ModuleManifest`
- `MarketView`
- `ModuleDecision`
- `Feature`, `FeatureDelete`, `FeatureView`
- dependency planner behavior and feature producer uniqueness
- checkpoint/module-state DTOs and import paths
- event ordering and public event DTOs
- order/fill/position public DTOs
- module loading and compatibility rules

No public-contract change is authorized by this baseline audit.

## Parallel module status

The required branch names for Entry Model, Protection & Target, Risk & Position Sizing and Position & Exit Management were not returned by branch search at baseline time. They are treated as parallel external work, and core public contracts remain frozen regardless of branch visibility.

## Baseline verdict

The integration head is a valid hardening base and is analytically functional, but it does not satisfy the requested hardening release gates because strict static analysis is red and several recovery/security/load controls remain unverified.
