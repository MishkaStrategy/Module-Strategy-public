# Migration notes

## Entry Model feature transition

The adapter accepts canonical `entry_model.plan` and the legacy alias
`entry_model.entry_plan`. The payload schema is still validated against the
feature envelope; the alias does not permit schema coercion.

## Position actions

ADD, REDUCE, CLOSE and REVERSE plans include an explicit `position_intent` and
expected position version. Executors consuming an earlier plan shape must be
updated to validate this field before actioning a plan.

## Scaling decisions

REBALANCE now preserves explicit `direction`. Risk-increasing actions require a
risk request/reservation identifier; risk-reducing actions reject one. Upstream
producers relying on implicit rebalance direction must migrate.

## Canonical identifiers

Identifier text is trimmed and NFC-normalized. Control/format characters,
oversized values and normalized-key collisions are rejected. Systems that used
visually equivalent decomposed Unicode IDs should normalize before publication.
