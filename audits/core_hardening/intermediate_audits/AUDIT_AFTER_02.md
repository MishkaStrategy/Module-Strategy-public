# Intermediate audit after rounds 01–02

- Changes: stricter data/time boundaries and structured failure reasons.
- Invariants: UTC/closed-bar semantics unchanged; invalid interval declarations now fail before the event loop.
- Public contracts: field and import compatibility preserved; `DataValidationError.reason_code` is additive.
- Tests: full suite green after updating the prior regression to assert earlier fail-closed validation.
- Look-ahead: unchanged and no future-bar access introduced.
- Determinism: improved by rejecting ambiguous stream identity.
- Restart/replay: unaffected.
- Regression risk: callers that passed `0m` now fail at construction rather than later at `close_time`; this is intentional invalid-input hardening.
- Decision: continue.
