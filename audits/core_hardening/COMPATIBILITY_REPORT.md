# Core compatibility report

## Frozen baseline

The public surface was captured directly from the verified core 0.4.0 wheel included in integration artifact `8467238152`. The snapshot covers Module API classes, manifests, feature/checkpoint/event DTOs, order/fill/position DTOs, configuration objects, registry and engine signatures.

The candidate test permits only optional additive fields/parameters. Every original field, method and default remains present and compatible.

## Compatible additive changes

- `DataConfig.max_base_history=None` — optional bounded module view; default preserves full history.
- `FeatureStore(max_features=None)` — optional capacity guard; default preserves prior behavior.
- `ModuleRegistry(max_config_bytes=None)` — optional safe YAML limit; default preserves v0.4.0 unbounded behavior.
- Reconciliation DTOs/interfaces — additive imports only.
- Structured journal fields — additive while legacy keys remain.

## Corrected existing semantics

`InstrumentSpec.contract_size` already existed publicly but was not applied consistently. Candidate 0.4.1 applies it to sizing, notional, fees, realised/unrealised PnL and risk. This is a correctness fix for non-unit contracts, not a DTO change.

## Existing analytical modules

Validated against candidate 0.4.1 using their real packages/source from integration PR #8:

| Module | Version | API | State round-trip | Planner |
|---|---:|---:|---:|---:|
| Session Range | 0.1.0 | 1.0 | Pass | Pass |
| Breakout–Return | 1.0.0 | 1.0 | Pass | Pass |
| FVG | 0.1.0 | 1.0 | Pass | Pass |
| Liquidity Sweep | 0.1.0 | 1.0 | Pass | Pass |
| Market Regime | 1.0.0 | 1.0 | Pass | Pass |

Combined required HTFs: 15m, 4h and 1h. No feature producer conflict or dependency cycle was found.

## Planned executor modules

A test-only harness proves the existing public path:

`Confirmed Setup → EntryPlan feature → Protection/Target features → RiskDecision feature + OrderRequest → Fill → Position → ClosePositionRequest → Exit`.

The harness is not packaged and is not represented as a production implementation. The actual four parallel branches were not visible through the connected GitHub installation during this run, so their final cross-audit remains required.
