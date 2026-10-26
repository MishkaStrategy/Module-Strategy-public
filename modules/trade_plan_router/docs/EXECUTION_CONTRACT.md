# Execution and stale-plan contract

An `ExecutableTradePlan` is a deterministic instruction for the next execution
layer. It is not an exchange order and is not evidence of a fill.

## Mandatory executor checks

Before translating intents into exchange requests, the Execution Engine must
recheck every plan precondition, including:

- plan validity interval and expiry;
- expected portfolio snapshot version;
- expected position-state and exact target-position version;
- expected pending-order state version;
- expected risk-reservation version;
- configuration/input evidence versions represented by plan hashes;
- instrument/strategy availability and lock/circuit-breaker state;
- remaining quantity, precision, risk, exposure, and protection validity.

A mismatch must produce a stale-plan outcome and must not be repaired by silently
changing Router semantics. The caller may obtain a new portfolio snapshot and
rerun the Router with a new explicit `as_of`.

## Sequencing

The plan records deterministic sequencing such as cancellation-before-replace,
protection-before-entry, reduce-before-open, or close-before-reverse. The
Execution Engine may implement retries and exchange-specific order conversion,
but it must preserve the plan's semantic order and reduce-only constraints.

## Position changes

`ADD_TO_POSITION`, `REDUCE_POSITION`, `CLOSE_POSITION`, and
`REVERSE_POSITION` carry a `PositionIntent` with exact position ID, version,
side, and factual current quantity. A reduction cannot exceed that quantity.
A full reduction is represented as close. Reversal closes the exact opposite
position before opening new exposure unless an explicit hedge policy applies.

## Failure boundary

The Execution Engine must not infer that a plan was executed merely because it
was serialized, logged, or delivered twice. Idempotent exchange execution needs
its own downstream idempotency key and state transition contract keyed by the
Router's stable plan identity.

## Portfolio content precondition

In addition to `portfolio_version`, every executable plan requires
`portfolio_content_hash`. The value must equal the immutable snapshot hash used
when the Router composed the plan and must also appear in the plan's
`input_hashes`. The Execution Engine must reject the plan as stale if either the
version or factual content hash differs. This protects against a buggy state
publisher that reuses a version while changing balances, positions, pending
orders, locks or risk state.

The executor must treat all mandatory OCC precondition kinds as unique. A
missing, duplicate or field-inconsistent precondition makes the plan invalid;
it must not choose one duplicate value or silently repair the payload.
