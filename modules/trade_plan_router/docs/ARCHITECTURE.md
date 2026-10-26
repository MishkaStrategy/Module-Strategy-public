# Architecture

## Data flow

```text
Entry decisions ───────────────┐
Protection decisions ──────────┤
Risk/Sizing decisions ─────────┼─> DecisionNormalizer
Scaling decisions (optional) ──┤          │
PortfolioSnapshot ─────────────┘          v
                                  DecisionValidator
                                          │
                                          v
                                   ConflictResolver
                                          │
                                          v
                              PortfolioConstraintEvaluator
                                          │
                                          v
                                  TradePlanComposer
                                          │
                         ExecutableTradePlan / RoutingResult
```

## Boundaries

The domain service is deterministic and side-effect free. All time enters as
`RouterInputPackage.as_of`; no system clock is read. Decimal values are used for
prices, quantities, notional, risk, allocations, and leverage. Input and output
models are frozen dataclasses, normalize UTC timestamps, and validate their own
invariants.

`TradePlanRouterModule` is a thin stateless adapter around the domain service.
It reads Module API features from a `MarketView`, converts them into the domain
package, and publishes immutable result features. It does not own execution or
portfolio state.

## Components

- `DecisionNormalizer`: strict schema adapters for the actual upstream payloads.
- `DecisionFreshnessValidator`: future/stale decision and snapshot checks.
- `DecisionValidator`: decision completeness, ownership, linkage, duplicate,
  and compatibility checks.
- `ConflictResolver`: stable policy ordering and typed loser evidence.
- `PortfolioConstraintEvaluator`: positions, pending orders, locks, risk,
  exposure, scale, reversal, and cancellation semantics.
- standalone risk-reduction route: validates one explicit decrease decision,
  resolves target-position ownership, applies `reduce-before-open`, and bypasses
  Entry/Protection/Sizing composition without bypassing OCC or portfolio locks.
- `TradePlanComposer`: immutable intents, OCC preconditions, hashes, and audit.
- canonical layer: NFC-normalized bounded canonical serialization with cycle,
  depth, and node-budget protection.
- `TradePlanSerializer`: exact primitive JSON schema validation and deterministic
  ID/hash verification without arbitrary object construction.

## Deterministic identity

`ExecutableTradePlan.plan_id` and `content_hash` derive from semantic content.
Audit-only fields are represented separately by `audit_hash`, so changing audit
verbosity does not change trading semantics. `RoutingResult.result_id` uses a
compact identity containing the plan IDs/hashes or canonical rejection evidence.
Equivalent input collection permutations are sorted before identity generation.
For standalone risk reduction, the scaling decision remains the selected
candidate even when matching Entry data is present for ownership fallback. This
prevents an informational Entry from changing the primary plan identity.

## Concurrency model

The Router is stateless and thread-safe for independent immutable input
packages. It does not reserve risk or mutate the snapshot. Plans carry expected
portfolio, position, pending-order, risk-reservation, configuration, and input
versions. Execution Engine must reject a plan when any required version or
precondition is stale.

## State and failure model

`TradePlanRouterModule` is stateless. `snapshot_state()` returns an empty mapping
and `restore_state()` accepts only an empty mapping. Expected business failures
are returned as typed rejected routing results. Unexpected internal exceptions
are not converted into `NO_ACTION`; they remain visible to the runtime.

## Module lifecycle and observability

The adapter inherits no-op `on_start`, `on_order_event`, `on_trade`, and
`on_finish` hooks from Module API because it owns no mutable trading state.
Constructor parameters are deep-frozen and detached from caller-owned nested
mappings. The manifest publishes the complete configuration schema, including
policy enums and defaults, so Module Registry validation and direct
construction enforce the same public surface.

Every routing attempt emits one structured completion record. The record
contains correlation/result ID, plan ID when accepted, portfolio, strategy,
setup and instrument identity, verdict, primary reason code, module and policy
versions, configuration hash, severity, and measured latency. Timing and log
metadata are observability-only and never enter plan or result hashes.
