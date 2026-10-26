# HANDOFF — Market Regime 1.2.0

## Component

- Name: `market_regime`.
- Purpose: deterministic point-in-time classification of trend/range/transition and relative volatility from completed target candles.
- Remote branch: `modules/market-regime-hardening`.
- Draft PR: `#29`.
- Base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.
- Public import: `from market_regime import MarketRegimeModule`.
- Entry point: group `modular_strategy.modules`, name `market_regime`.

## Compatibility

- Modular Strategy Core: `>=0.4,<0.5`, verified locally with `0.4.0`.
- Module API major: `1`.
- Python: `>=3.12`; local final evidence uses Python `3.13.5`.
- Feature schema: `1`, unchanged.
- State schema: `2`, with schema-1 migration.

## Public contract

Required timeframe: configured target timeframe, default `1h`.

Produced features:

- `market_regime.current`;
- `market_regime.trend`;
- `market_regime.volatility`.

The module has no required upstream features. It emits only immutable Core `Feature` objects. It does not emit orders, signals, setups, cancellations, close/update requests or portfolio mutations.

Typed skip reasons:

- `market_regime.empty_view`;
- `market_regime.no_target_bar`;
- `market_regime.not_ready`;
- `market_regime.data_gap_reset`;
- `market_regime.timeframe_gap_reset`;
- `market_regime.duplicate_bar`;
- `market_regime.stale_target`;
- `market_regime.incomplete_bar`.

## State and temporal guarantees

- completed-bar and point-in-time semantics;
- future target candles are rejected before publication;
- exact duplicate events are idempotent;
- conflicting duplicate and out-of-order events fail closed;
- rejected target validation is transactional and does not advance the base cursor;
- explicit data/timeframe gaps reset bounded runtime state;
- one module instance is permanently bound to one symbol;
- checkpoint restore is atomic;
- schema-1 legacy checkpoints are migrated to schema 2;
- continuous and midpoint-restart runs produce identical output/state digests.

## Evidence

- effective published module suite: 151 tests;
- statement coverage: 93%, gate 90%;
- Ruff lint/format, strict mypy, compileall and Bandit: PASS locally;
- wheel, sdist, Twine metadata, extracted-sdist tests and clean install: PASS locally;
- 50,000-event benchmark median: 10,235.64 events/s;
- checkpoint: 3,511 bytes;
- midpoint restore: approximately 0.371 ms;
- peak traced allocation: 59,495 bytes;
- 250,000 continuous/restart digests and counts match.

Detailed evidence is in `artifacts/` and the five audit documents under `docs/`.

## Integration procedure

1. Review Draft PR #29 against `integration/modular-strategy-v0.5.0`.
2. Run self-hosted Python 3.12 and 3.13 fast quality jobs.
3. Dispatch the heavy full-repository, packaging/security and performance jobs.
4. Confirm the full Core and neighboring-module integration suite.
5. Keep the PR Draft until all required checks pass.
6. Do not merge, tag or create a release without explicit user approval.

## Open gates

- self-hosted runner jobs are queued and have not yet executed;
- Python 3.12 requires remote proof;
- full repository suite requires remote proof;
- `pip-audit` requires advisory-network access;
- actual custom self-hosted labels were not exposed by the available connector and were not guessed.

## Verdict

Conditionally ready for integration. Local component evidence is complete; remote CI and repository-wide integration remain mandatory before merge.
