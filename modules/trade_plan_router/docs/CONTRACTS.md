# Contracts

## Upstream inputs

| Producer | Accepted feature/schema | Router treatment |
|---|---|---|
| Entry Model | `entry_model.plan`, schema `2` | verifies required identity fields and canonical upstream `plan_id` |
| Entry Model legacy | `entry_model.entry_plan`, schema `1` | explicit compatibility normalizer; never inferred from schema `2` |
| Protection & Target | `protection_target.plan`, schema `1` | requires matching candidate/setup, symbol, side, entry, stop, target and time linkage |
| Risk & Position Sizing | `risk.decision`, schema `1` | accepts approved sizing only; validates candidate/protection linkage and risk evidence |
| Position Scaling | `position_scaling.approved`, schema `1` | preserves ADD/REDUCE/REBALANCE and INCREASE/DECREASE semantics |
| Portfolio state | `portfolio.snapshot`, schema `1` | immutable supplied state; no exchange reads |

A known upstream integration mismatch exists: released Entry Model publishes
`entry_model.plan`, while released Protection & Target documents the legacy
`entry_model.entry_plan` input. The Router accepts both feature keys, but does
not claim that this fixes the upstream producer/consumer contract by itself.

## Router package

`RouterInputPackage` contains explicit `as_of`, one immutable
`PortfolioSnapshot`, and tuples of normalized Entry, Protection, Sizing, and
Scaling decisions. It enforces element types, canonical ordering, resource
limits, and compact correlation identity.

## Executable plan

The plan contains:

- schema/module/portfolio/strategy/setup/instrument identity;
- action and side;
- creation, validity, and expiry times;
- expected portfolio, position, order-state, and risk-reservation versions;
- selected candidate and optional target-position intent;
- entry, protection, sizing, and optional scaling linkage;
- cancellation intents and sequencing rules;
- executable preconditions;
- reason codes, warnings, rejected candidates, input hashes, and audit trace;
- configuration, content, audit, and plan hashes.

It contains no exchange order ID, fill, REST response, API key, signature, or
claim that execution occurred.

### Standalone risk reduction

A single approved Position Scaling decision with `direction=decrease` may form
a `REDUCE_POSITION` or `CLOSE_POSITION` plan directly from the immutable target
position snapshot. This path does not require a new Entry, Protection, or Sizing
decision because it does not create exposure or reserve risk. The resulting
entry intent is always `reduce_only`, sizing risk is zero, and no new protection
intent is composed.

The selected candidate is always the scaling decision ID. Any Entry decisions
in the same package are deferred deterministically under the explicit
`reduce-before-open` action policy. A matching current Entry may supply missing
strategy/market ownership, but it never becomes the executable candidate. A
stale or future Entry cannot provide ownership. Redundant current Entry content
is audit-only when the position already contains complete ownership and does
not change `plan_id` or `content_hash`. The plan carries exact target position
ID/version/quantity preconditions so the Execution Engine can reject stale or
over-reducing plans without re-running routing policy.

## Failure model

Expected business failures return a typed rejected `RoutingResult` with one or
more `ReasonCode` values. Invalid public payloads raise typed input/configuration
errors at the normalization boundary and are converted by the Module adapter to
an explicit rejection. Unexpected internal exceptions remain visible; they are
not silently converted to `NO_ACTION`.

## Time, numbers and canonical identity

All timestamps are timezone-aware and canonicalized to UTC; `as_of` is explicit.
Prices, quantities, notional, risk and allocations use `Decimal`. Non-finite
values and booleans in numeric fields are rejected. Canonical serialization
sorts mappings, normalizes enums and Decimal scale, rejects cycles, bounds
nesting/nodes/string sizes, and keeps equivalent input permutations stable.

## Serialized plan/result boundary

`TradePlanSerializer` emits canonical UTF-8 JSON and accepts untrusted JSON only
as a deeply immutable primitive mapping. It enforces exact top-level and nested
schemas, bounded payload size, canonical UTC/Decimal text, action-specific
execution geometry, position OCC, protection/sizing invariants and stable
evidence ordering. It rejects duplicate keys and non-finite JSON constants.

Deserialization re-verifies `content_hash`, `audit_hash`, `plan_id`, and
`result_id` after structural validation. It never imports or constructs an
object named by the payload and never interprets a serialized plan as an
exchange acknowledgement. See [Serialization](SERIALIZATION.md).

## Package and portfolio content identity

`PortfolioSnapshot.version` is an optimistic-concurrency version supplied by the
state owner. It is not accepted as proof that the snapshot content is unchanged.
The Router also computes a canonical `PortfolioSnapshot.input_hash` over all
portfolio balances, locks, risk state, positions, pending orders and active plan
identifiers. Large position/order collections use deterministic per-state
SHA-256 rows so the full supported snapshot can be hashed without increasing the
global canonical node budget.

`RouterInputPackage.correlation_id` binds:

- explicit `as_of`;
- portfolio identity, versions, timestamp and content hash;
- ordered Entry, Protection, Sizing and Scaling input hashes.

Collections with duplicate logical IDs are ordered by `(ID, input_hash)`, never
by arrival order. Equivalent permutations therefore produce the same package
identity, while different content under a reused ID changes the correlation ID.

Every terminal result attaches an `input_inventory` object to the final business
audit event. It records the package correlation ID, portfolio content hash,
per-type counts and cryptographic inventory digests. For collections of at most
256 elements it also includes the canonical decision-ID list; larger lists are
represented by count and digest to preserve bounded audit size. This inventory
is part of the plan/result hash. Two rejected packages with the same reason code
but different factual input therefore cannot share a `result_id`.

## Rejection identity

The public plan/result schema contains at most one `RejectedCandidate` per
`candidate_id`. Multiple validation records caused by duplicate delivery are
coalesced deterministically by unioning reason codes. Their individual details
remain in the audit trace. If duplicate payloads reuse one candidate ID with
incompatible setup ownership, the public rejection uses setup ID `ambiguous`.

Orphan upstream decisions use type-qualified candidate IDs:

- `protection:<decision_id>`;
- `sizing:<decision_id>`;
- `scaling:<decision_id>`.

This prevents cross-type ID collision and preserves safe serializer round trips.
