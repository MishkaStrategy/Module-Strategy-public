# Conflict resolution

## Candidate order

The stable default order is based on:

1. portfolio policy priority;
2. strategy priority;
3. setup priority;
4. module priority;
5. side priority;
6. candidate priority;
7. signal availability time;
8. decision creation time;
9. canonical setup ID;
10. canonical candidate ID.

No dictionary/set iteration order, random UUID, random seed, filesystem order,
or system clock is used as a tie-breaker.

## Policies

- Long/Short: `reject_all`, `higher_priority`, `earliest_signal`, or
  `freshest_signal`.
- Multiple setups: `highest_priority` or `reject`.
- Existing positions: `reject`, `allow_same_side_add`,
  `close_then_reverse`, or `hedge`.
- Pending orders: `reject`, `replace_same_setup`, or `cancel_opposite`.
- Active plans: `reject_new_exposure` or `allow_multiple`.

Priority maps exist at portfolio, strategy, setup, module, and side levels.
Changing a semantic policy changes the semantic configuration fingerprint and
therefore the plan identity.

## Position semantics

ADD, REDUCE, CLOSE, and REVERSE plans carry an explicit `PositionIntent` with
position ID, current side/quantity, and expected version. Multiple eligible
positions are rejected as ambiguous unless an explicit Scaling decision targets
one position.

A reversal contains deterministic close-before-open sequencing and subtracts
only the explicitly targeted position when projecting post-close exposure.
Hedge mode does not subtract existing exposure.

Risk-reducing REDUCE/CLOSE operations can bypass locks when
`allow_risk_reducing_when_locked` is enabled. They never reserve additional risk.
ADD and rebalance-increase require approved risk evidence.

Portfolio snapshots expose active plans only as opaque IDs. Without symbol,
owner, status, or version metadata, the Router cannot safely infer whether a
new plan replaces or conflicts with an existing one. The default
`reject_new_exposure` policy therefore blocks OPEN, ADD, REVERSE, and replacement
actions whenever `active_plan_ids` is non-empty. An explicit `allow_multiple`
policy permits them and records a warning. REDUCE and CLOSE actions remain
allowed with an `active_plan_risk_reduction_override` warning.

## Action priority for standalone exits

An approved scale-out/rebalance-decrease has deterministic
`reduce-before-open` priority. The scaling decision remains the selected
candidate, while all Entry candidates are recorded as deferred losers in
canonical `(setup_id, candidate_id)` order. This prevents input ordering or the
presence of a compatible Entry bundle from converting a risk-reducing plan into
a new-entry plan.

The standalone path fails closed for ambiguous target positions, stale state,
missing ownership, excess reduction quantity, conflicting active orders, or an
invalid scaling contract. It may bypass active locks only when the explicit
`allow_risk_reducing_when_locked` policy is enabled, and the override is emitted
as a warning.
