# Downstream execution boundary

## Authority

`modular_strategy_trade_plan_router.models.ExecutableTradePlan` is the only
canonical trade-plan domain contract produced by the Decision Router. Its
schema version, semantic hashes, plan identity, optimistic-concurrency
preconditions, action semantics and sequencing rules are authoritative.

A downstream execution module must not introduce a second model that silently
changes the meaning of a Router plan. Exchange- or venue-specific order models
are permitted only behind an explicit adapter whose input is either:

1. an in-process `ExecutableTradePlan`; or
2. the canonical JSON emitted by `TradePlanSerializer.dumps_plan` and accepted
   by `TradePlanSerializer.loads_plan_mapping`.

The serialized mapping is validation evidence, not permission to recalculate
or repair the plan.

## Router handoff envelope

`DownstreamPlanEnvelope.from_plan` is the supported in-process handoff helper.
It contains:

- the Router schema, plan ID, content hash, audit hash and typed action;
- the exact canonical JSON bytes represented as UTF-8 text;
- a separate SHA-256 digest of that canonical JSON payload;
- the exact ordered `ExecutionPrecondition` values without integer coercion;
- the original `valid_from` and `expires_at` timestamps.

The envelope validates that its identifiers, validity window and preconditions
match the canonical Router payload. `validate_at` rechecks the validity window,
`require_supported_actions` rejects executor capability gaps, and
`precondition_mapping` exposes immutable string-to-string OCC evidence.

The envelope is not a venue order model. A downstream adapter must still parse
the canonical plan and produce its internal order/state-machine values
explicitly. In particular, Router string versions and SHA-256 preconditions
must not be converted into Paper Execution integer `StateVersions`.

## Adapter responsibilities

A downstream adapter may:

- translate Router order types to venue order types;
- derive stable downstream idempotency keys from `plan_id` and intent identity;
- normalize quantity and price only when the venue contract explicitly permits
  it and the normalized value still satisfies every Router precondition;
- expand protection targets into venue-specific reduce-only requests;
- implement retries and reconciliation while preserving semantic order;
- reject an unsupported action or venue capability with a typed result.

A downstream adapter must not:

- change side, action, quantity, stop, target allocation or reduce-only intent;
- recompute risk, leverage, position sizing or portfolio eligibility;
- ignore, merge or choose between duplicate preconditions;
- execute an expired plan or a plan whose portfolio/content/version evidence is
  stale;
- reorder cancellation-before-replace, protection-before-entry,
  reduce-before-open or close-before-reverse rules;
- treat serialization, delivery or journal persistence as proof of execution;
- reuse `plan_id` with different `content_hash` or `audit_hash` evidence.

## Required validation order

Before producing venue requests, the downstream boundary must fail closed in
this order:

1. validate the Router schema and canonical hashes;
2. verify the canonical-payload SHA-256 carried by the handoff envelope;
3. reject unsupported schema versions and actions;
4. recheck `valid_from` and `expires_at`;
5. recheck every unique optimistic-concurrency precondition;
6. verify current portfolio, position, order and risk-reservation versions;
7. verify exact target-position ownership for position-changing actions;
8. verify venue metadata and capability compatibility;
9. translate intents without changing Router semantics;
10. persist a downstream idempotency record before submission;
11. execute and reconcile in the recorded sequencing order.

Any mismatch produces a typed stale-plan, unsupported-plan or venue-rule
rejection. It must not be repaired by mutating the Router payload.

## Versioning

The boundary currently supports Router `schema_version = "1"`. A downstream
adapter must declare the exact Router schema versions it supports. Adding a
field, changing identity material, changing action semantics or changing a
mandatory precondition requires a new schema version or an explicitly backward
compatible extension proven by contract tests.

## Paper Execution integration

Paper Execution may keep internal `ExecutionIntent`, venue metadata and state
machine models. Its external adapter, however, must consume the canonical
Router plan and produce those internal values explicitly. Its internal plan
representation must not be accepted as an alternative upstream contract.

The integration test must cover at least:

- open-position market and limit plans;
- add, reduce and close actions;
- cancellation-before-replace ordering;
- protection and target allocation;
- stale portfolio/content/version rejection;
- duplicate or missing precondition rejection;
- unsupported reversal rejection when the executor lacks reversal support;
- idempotent replay of identical `plan_id`/`content_hash`;
- rejection of identical `plan_id` with different content;
- canonical JSON round-trip before adaptation;
- canonical-payload digest mismatch rejection;
- preservation of Router string OCC values without conversion to integers.
