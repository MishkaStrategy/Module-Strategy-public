# Decision Router & Trade Plan Composer

Deterministic orchestration module for the **Модульная Стратегия** project.
It consumes already-computed entry, protection/target, risk-sizing and optional
position-scaling decisions together with an immutable portfolio snapshot. It
returns one typed `ExecutableTradePlan` or one typed rejected routing result.

## Safety boundary

This module **does not**:

- place, cancel or modify exchange orders;
- call exchange REST or WebSocket APIs;
- use API keys or retrieve balances;
- calculate market signals, stops, targets or optimized position sizes;
- treat a composed plan as a confirmed fill.

The Execution Engine remains responsible for rechecking plan preconditions and
translating execution intents into exchange orders.

## Deterministic contract

For equal decisions, portfolio versions, configuration and explicit `as_of`, the
Router produces equal candidate selection, reason codes, plan ID, content hash,
audit trace and serialized output. System time, local timezone, hash seed,
filesystem order, network access and random UUIDs are not inputs.

Every accepted plan carries optimistic-concurrency preconditions for portfolio,
position, order-state, risk-reservation and configuration versions. A downstream
executor must return a typed stale-plan rejection when those preconditions no
longer hold.

`TradePlanSerializer` provides a bounded canonical JSON boundary for plans and
routing results. It validates exact nested schemas, canonical UTC/Decimal text,
order geometry, OCC evidence and all deterministic IDs/hashes without unsafe
object construction.

## Downstream handoff

`DownstreamPlanEnvelope.from_plan(plan)` is the supported handoff helper for
Paper Execution and other downstream executors. It preserves:

- canonical Router JSON and a separate SHA-256 of the delivered payload;
- `plan_id`, `content_hash` and `audit_hash`;
- the typed Router action and validity interval;
- exact ordered string `ExecutionPrecondition` values.

The envelope revalidates canonical payload identity, supports fail-closed action
capability checks, and rechecks the validity window. It is not a venue order
model and does not permit downstream recalculation or conversion of Router
string/SHA-256 OCC evidence into incompatible integer state versions.

```python
from modular_strategy_trade_plan_router import (
    DownstreamPlanEnvelope,
    TradeAction,
)

handoff = DownstreamPlanEnvelope.from_plan(
    plan,
    supported_actions={
        TradeAction.OPEN_POSITION,
        TradeAction.ADD_TO_POSITION,
        TradeAction.REDUCE_POSITION,
        TradeAction.CLOSE_POSITION,
    },
)
handoff.validate_at(execution_time)
preconditions = handoff.precondition_mapping()
```

## Public module features

Inputs:

- `entry_model.plan` (legacy alias: `entry_model.entry_plan`)
- `protection_target.plan`
- `risk.decision`
- optional `position_scaling.approved`
- optional `portfolio.snapshot`

Outputs:

- `trade_plan_router.executable_plan`
- `trade_plan_router.rejected_plan`
- `trade_plan_router.routing_decision`
- `trade_plan_router.conflict_trace`

## Validation

The remote validation gate checks the exact PR head against pinned upstream
contracts, compiles the package, runs Ruff, strict MyPy, the full Router suite
and branch-aware coverage with a hard 90% minimum.

Verified Router checkpoint:

- 349 tests passed;
- Ruff and strict MyPy passed;
- branch-aware coverage: 90.3750%;
- 30/30 executable golden scenarios passed.

The final gate also runs production security/type audits, Bandit, `pip-audit`,
four-profile deterministic replay, 10,000 fuzz cases, concurrency/soak stress,
byte-identical wheel builds and clean install/uninstall/reinstall checks.

A separate fast compatibility gate installs exact validated Core 0.4.2 head
`04f6de01e5337883256299dd20effdbedd5ac73e` together with the pinned upstream
module contracts and Router 0.1.0. Verified run `30060866128` passed all 349
Router tests, Ruff, strict MyPy, the 90% coverage gate, dependency checks and a
clean Core 0.4.2 + Router wheel import. This proves forward compatibility but
does not merge Core PR #96 or replace integration-base validation.

For an equivalent local workspace validation:

```bash
CORE_PROJECT=/path/to/modular_strategy_core-0.4.0 \
CORE_SRC=/path/to/modular_strategy_core-0.4.0/src \
ENTRY_SRC=/path/to/modular_strategy_entry_model-0.2.0/src \
PYYAML_WHEEL=/path/to/PyYAML.whl \
./scripts/local_fast_ci.sh local
```

## Release-gate evaluation

`scripts/release_gate.py` consumes Fast CI, performance, fuzz, stress and
external Heavy Audit evidence. It cannot emit a local release-candidate status
unless a verified integration-branch checkout plus Mac ARM64, strict type,
external security/dependency and full mutation reports all explicitly pass.
Missing evidence remains a named blocker rather than an inferred success.

Documentation:

- [Architecture](docs/ARCHITECTURE.md)
- [Contracts](docs/CONTRACTS.md)
- [Downstream execution boundary](docs/DOWNSTREAM_EXECUTION_BOUNDARY.md)
- [Conflict resolution](docs/CONFLICT_RESOLUTION.md)
- [Configuration](docs/CONFIGURATION.md)
- [Reason codes](docs/REASON_CODES.md)
- [Testing](docs/TESTING.md)
- [Serialization](docs/SERIALIZATION.md)
- [Migration](docs/MIGRATION.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)
- [Module API integration](docs/MODULE_API.md)
- [Execution and stale-plan contract](docs/EXECUTION_CONTRACT.md)
- [Extension guide](docs/EXTENSION.md)
- [Rollback plan](docs/ROLLBACK.md)
- [Final local audit](../../reports/trade_plan_router/FINAL_LOCAL_AUDIT.md)
