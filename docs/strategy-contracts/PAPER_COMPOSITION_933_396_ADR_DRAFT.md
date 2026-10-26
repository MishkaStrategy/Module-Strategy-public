# PAPER Composition and Portfolio Allocation ADR for #933 / #396

**Status:** PROPOSED; not accepted and not implementation or execution authority.

**Scope:** Proposed aggregate ownership boundary and fail-closed allocation contract for operational PAPER.

**Issues:** [#933 — Paper Execution owner](https://github.com/MishkaStrategy/Module-Strategy/issues/933); [#396 — portfolio acceptance](https://github.com/MishkaStrategy/Module-Strategy/issues/396)

## Decision requested

The owner has selected the future #933 Paper Execution/Core composition as the sole aggregate owner of the complete PAPER portfolio, with #396 implemented only as internal allocation, accounting, and reservation mechanics inside that composition. #396 must not become a separate owner, API, ledger, or competing source of portfolio truth. This ADR and the required extension of official #933 scope remain proposed pending explicit acceptance.

This ADR is a proposed contract for review. It does not approve the contract, extend an issue, enable an adapter, establish release readiness, or authorize PAPER execution. The official #933 scope currently covers Stage-1 provisioning/reconnect and `TradingRunnerComponentsFactory`; it does not specify the complete multi-engine portfolio snapshot or allocator. Extending that scope requires separate agreement and acceptance of the #933 issue contract. The #250 durable signed approved-package predecessor gate remains in force for Stage-1 provisioning.

## Context

The existing Core execution model maintains factual positions, orders, and fills inside individual engine/broker state. The local `PaperCompositionOwner` prototype isolates same-symbol positions by strategy instance, but it creates separately funded brokers, processes submissions by call arrival, and has no shared-cash or reservation ledger. Its reference-only state is not an accepted portfolio owner.

The product needs one composition boundary that coordinates those Core child facts with shared portfolio accounting. A second portfolio API or a parallel accounting store would allow inconsistent membership, position, cash, reservation, and recovery views. Per-broker `initial_cash` must not act as additional spendable shared capital.

## Proposed ownership and authority

| Concern | Proposed authority | Boundary |
|---|---|---|
| Child positions, orders, and fills | Existing Core engine/broker substates, observed and coordinated by the #933 composition | Preserve child facts and their strategy-instance identity. Do not infer a target from symbol, side, or strategy alone. |
| Complete PAPER portfolio and membership | Future #933 Paper Execution/Core composition | Sole aggregate owner, writer, commit/fence, snapshot, and recovery boundary. |
| Shared capital, allocation decisions, and reservations | #396 mechanics inside the #933 composition | Record shared accounting once in the same aggregate state and transaction as the relevant portfolio version. No separate #396 owner/API or ledger. |
| Operator projection | Control Plane, after contract acceptance | Read-only consumer of the one complete owner snapshot; unavailable or stale owner state must not be presented as current. |
| Capital, commitment/cost, ranking-cycle, and terminal/reconciliation policy | `unconfigured` pending explicit owner decisions | No inferred zero, default, fallback, or synthetic production authority. |

Independent positions for different strategies remain valid even when they use the same symbol, including same-side positions. Shared capital is a single pool. Core child state remains the factual source for child positions, orders, and fills; aggregate accounting and reservation facts are owned once by #933.

## Settled policy proposal

The following choices are recorded as selected policy, not as implementation proof:

1. Allocate simultaneous candidate-plan requests only after sorting by fixed priority, then stable strategy-instance ID as the deterministic tie-break. The priority source, direction/value range, and update timing remain `unconfigured`; no Router ordering is substituted.
2. If a complete request exceeds currently available shared capital, reject the whole request. Do not partially admit it or queue it automatically. A strategy may submit again in a later eligible allocation cycle.
3. Before any future dispatch, reserve the owner-approved worst-case commitment plus approved costs in the aggregate state. The actual calculation inputs and their authority remain `unconfigured`.
4. Apply each observed fill atomically: add its factual exposure and transfer the corresponding amount from reservation into exposure in one aggregate commit. Never count the same amount as both free capital and exposure.
5. Keep the unfilled reservation remainder through cancellation and lost/uncertain acknowledgements. Release it only after an authoritative terminal result or an owner-authorized, reconciled recovery establishes the terminal disposition. A Core-local cancelled state alone is insufficient.

These rules do not enable dispatch. Until the unresolved owner inputs below are accepted, the production allocation and dispatch path remains disabled and fails closed.

## Commit, snapshot, and recovery contract

The future composition must provide one serialized writer boundary for membership, child position/order/fill reconciliation, allocation, reservations, protection/close operations, and restore. Every accepted mutation must be fenced against the expected aggregate generation/version and commit once to the same whole-owner state. A process-local mutex alone is not a durable cross-process fence.

If an error occurs after any child state has changed but before the coordinated whole-owner commit completes, the implementation must either roll back the entire mutation or mark the entire aggregate unavailable/recovering. In the latter case it must block complete reads and new exposure until a coordinated recovery establishes one consistent owner state. Partial publication of child changes or accounting is forbidden; no reader may observe a mutation that did not commit across the whole owner.

The read API must publish an immutable complete snapshot for one exact composition scope. It should bind composition identity, owner identity and generation, membership/version, commit-wide state version, snapshot version, observation time, explicit completeness, child positions/orders/fills, and shared accounting/reservation facts. The complete projection must cover aggregate cash, notional, leverage, and exposure, plus per-symbol, per-strategy, and aggregate risk-budget state, using factual owner state and only accepted calculation inputs. This specifies required evidence coverage, not financial values, thresholds, or defaults. A complete empty snapshot may mean no positions only when the owner explicitly proves complete coverage. Stale, partial, unavailable, or recovering state must never be projected as complete.

Recovery must restore or reconcile the entire owner scope atomically: membership, Core child state, shared capital/accounting, reservations, allocation decisions, commit/version fence, and replay/idempotency state. Restored state lineage and writer authority are separate: a new owner that restores a checkpoint must receive a new owner generation/fencing token; the previous writer immediately loses mutation authority, and commands carrying its generation are rejected. A checkpoint restores state lineage, never the prior writer's current authority. If the system cannot establish a consistent whole-owner state, it remains unavailable/recovering and blocks new exposure. Recovery must not publish a partially restored portfolio or release a reservation merely because an acknowledgement is missing.

## Owner inputs that remain `unconfigured`

| Input | Required owner decision before production allocation or dispatch |
|---|---|
| Capital and balance authority | Authoritative source and owner; currency; opening/current balance; definition of spendable cash; treatment of realized and unrealized P&L. |
| Commitment and cost authority | Trusted immutable inputs and calculation policy for contract units, margin/leverage, price, fees/slippage, rounding/dust, caps, and an over-bound fill. |
| Priority and cycle | Priority source, direction and value range, update timing, cycle identity and eligibility, and maximum requests per strategy per cycle. The comparator shape is selected, but these values are not. |
| Lifecycle and cardinality | Stable strategy-instance identity, membership changes, maximum/required cardinality, and the availability/freshness semantics that govern eligibility. |
| Terminal, reconciliation, and recovery | Authoritative terminal source, durable pre-dispatch commit boundary, reconciliation actor/evidence, and reservation continuity/reconstruction across restart or restore. |
| Aggregate risk and control | Per-symbol, per-strategy, and aggregate risk budgets; provenance-backed concentration/correlation policy; aggregate loss/drawdown gates and Risk Supervisor integration; and portfolio suspend/kill semantics. |

Until these decisions are explicitly accepted, all related values remain `unconfigured`. Do not substitute zero balances, unlimited limits, default priority, current timestamps, local Core status, missing acknowledgements, or broker-local cash. Production allocation, reservation-backed dispatch, readiness claims, and #396 acceptance fail closed.

## Permitted work while blocked

Work may cover isolated composition mechanics, immutable scope/version/fencing primitives, complete-snapshot and whole-owner recovery mechanics, synthetic fixtures with explicitly test-only values, conservation/idempotency/failure tests, and disabled production adapters. Tests must not imply that fixture values are approved owner policy. Do not connect a dispatcher, add another ledger or owner API, change Core, or describe the prototype as accepted.

## Acceptance sequence and evidence

1. Record explicit acceptance of the aggregate-owner ADR and agreement to extend #933 beyond its official Stage-1 provisioning/reconnect and `TradingRunnerComponentsFactory` scope. The owner-selected direction above is not itself ADR or issue-scope acceptance.
2. Before #396 portfolio implementation or acceptance, verify that #379 is closed and #380, #387, and #395 are sufficiently accepted. These portfolio predecessors are separate from #250's durable signed approved-package prerequisite for #933 Stage-1 provisioning: satisfying one gate does not satisfy or replace the other.
3. Resolve the `unconfigured` owner inputs with authoritative identities, exact policy values, and recorded approval. Keep allocation, reservation-backed dispatch, readiness, and #396 acceptance fail-closed until those decisions are accepted.
4. Implement the exclusive #933 owner boundary and prove strategy-bound operations, immutable complete snapshots, shared-accounting conservation, stale-version rejection, idempotency, and no double-spend using synthetic fixtures.
5. Prove atomic whole-owner recovery, including partial failures, lost acknowledgements, pending reservations, replay, and unavailable/recovering behavior.
6. Preserve every official #396 deliverable. The live issue lists all twelve as unchecked; this traceability map identifies required evidence only, marks none complete, and adds no threshold or financial value:

| #396 deliverable | ADR evidence required (all remain unproven until accepted) |
|---|---|
| Authoritative Portfolio Coordinator / Capital Allocator boundary | Proposed ownership table and acceptance evidence for the exclusive owner, commit, snapshot, and recovery boundary. |
| Aggregate cash, notional, leverage, and exposure projection from factual owner state | Complete-snapshot projection contract and candidate-bound evidence from factual owner state, using only accepted inputs. |
| Per-symbol, per-strategy, and aggregate risk budgets | Budget-state projection and enforcement evidence; thresholds and financial values remain unconfigured until owner-approved. |
| Concentration and correlated-exposure limits with explicit correlation-data provenance | Accepted policy and provenance-backed concentration/correlation evidence. |
| Aggregate drawdown/daily-loss gates integrated with the independent Risk Supervisor | Accepted aggregate-loss policy and integration evidence. |
| Portfolio kill/suspend gate that does not replace Trading Runner/Paper Execution lifecycle semantics | Accepted portfolio suspend/kill contract and lifecycle evidence. |
| Deterministic allocation under simultaneous candidate plans, including priority/tie rules | Fixed-priority ordering with stable strategy-instance ID tie-break; policy inputs remain unconfigured. |
| Recovery/fencing/idempotency for allocation decisions | Whole-owner commit, fencing, replay, and recovery evidence (items 4–5 above). |
| Immutable allocation evidence linked to strategy and executable-plan identities | Candidate-bound immutable allocation evidence with both identities. |
| Backtest/research parity for portfolio decisions before runtime admission | Decision-parity evidence before runtime-admission consideration. |
| Stress tests for simultaneous losses, stale symbols, partial outages, correlation spikes, and exhausted capital | Evidence for all five named stress cases in ROADMAP §H. |
| Control Plane read projection only after owner contracts are accepted | Read-only projection only after accepted owner contracts and candidate-bound acceptance. |

The read-only Control Plane projection remains downstream of the accepted owner contract. Nesting #396 inside #933 does not reduce or waive any deliverable.
7. Only after owner contract and all required release/acceptance gates are satisfied may production adapters or dispatch be considered. This ADR itself grants no PAPER, soak, CI, deployment, publication, or release authority.

## Non-goals

This draft does not select a supported predecessor or trust root; those remain separate #387/#250 gates. It does not implement portfolio allocation, define financial defaults, change the current #933 issue scope, accept #933 or #396, enable the Control Plane reader/API/UI, or authorize SEARCH, HOLDOUT, PAPER, soak, CI, deployment, publication, push, merge, or release.
