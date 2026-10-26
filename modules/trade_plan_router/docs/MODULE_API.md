# Module API integration

## Registration

The wheel publishes one entry point:

```text
[modular_strategy.modules]
trade_plan_router = modular_strategy_trade_plan_router.module:TradePlanRouterModule
```

The package version, `ModuleManifest.version`, and entry-point distribution
version must remain equal. The local Fast CI validates this relationship from
source and again after clean wheel installation.

## Required and optional features

Required manifest features:

- `entry_model.plan`;
- `protection_target.plan`;
- `risk.decision`.

Optional features:

- `entry_model.entry_plan` for explicit legacy schema-1 compatibility;
- `position_scaling.approved`;
- `portfolio.snapshot`.

Produced features:

- `trade_plan_router.executable_plan`;
- `trade_plan_router.rejected_plan`;
- `trade_plan_router.routing_decision`;
- `trade_plan_router.conflict_trace`.

The adapter publishes primitive immutable mappings. It does not publish exchange
orders, order acknowledgements, fills, balances, API credentials, or network
state.

## Lifecycle and state

`TradePlanRouterModule` is stateless. It owns no reservation ledger, active-plan
registry, exchange session, or deduplication database. `snapshot_state()` returns
an empty immutable mapping and `restore_state()` accepts only an empty mapping.
Inherited lifecycle/event hooks are no-ops.

Idempotency is therefore a property of immutable inputs plus configuration, not
of hidden module state. Duplicate delivery produces the same result mapping and
result ID; it does not reserve risk or mutate the supplied portfolio snapshot.

## Configuration

Construction parameters are validated through `RouterConfiguration` and are
deep-frozen. `ModuleManifest.parameters_schema` exposes the supported enum,
boolean, integer, numeric, object, and default constraints. Unknown or invalid
configuration fails before the module becomes ready.

## Runtime call

`on_bar(MarketView)` uses `view.current.close_time` as explicit `as_of`, reads
only supplied features, normalizes the package, routes it, writes one structured
completion log, and returns one `ModuleDecision`. Expected malformed upstream
payloads become typed rejected features; unexpected implementation failures are
not silently converted to `NO_ACTION`.
