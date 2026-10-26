# Reason codes

| Code | Meaning |
|---|---|
| `accepted` | Plan passed all required checks. |
| `missing_entry_decision` | No usable entry candidate. |
| `missing_protection` | Required protection decision absent. |
| `missing_sizing` | Required sizing decision absent. |
| `sizing_rejected` | Upstream sizing explicitly rejected. |
| `stale_decision` / `future_decision` | Decision violates `as_of` timing. |
| `portfolio_state_stale` | Snapshot is missing when required or too old. |
| `long_short_conflict` | Opposite candidates cannot be resolved by policy. |
| `setup_conflict` | Multiple setup candidates remain ambiguous. |
| `position_conflict` | Existing positions conflict with the requested action. |
| `pending_order_conflict` | Pending orders conflict with the requested action. |
| `active_plan_conflict` | Opaque active plan IDs block new exposure under the configured policy. |
| `duplicate_decision` | Duplicate IDs or incompatible duplicate evidence. |
| `risk_limit_exceeded` | Portfolio risk or reservation cap exceeded. |
| `exposure_limit_exceeded` | Symbol notional cap exceeded. |
| `portfolio_locked` / `instrument_locked` / `strategy_locked` | Active lock blocks the action. |
| `circuit_breaker_active` / `emergency_state_active` | Emergency control blocks risk. |
| `daily_loss_limit_active` / `drawdown_limit_active` | Loss control blocks risk. |
| `invalid_scaling` | Scaling action, direction, quantity or linkage invalid. |
| `incompatible_decisions` | Upstream decisions disagree semantically. |
| `unsupported_policy` / `unsupported_schema` | Versioned contract is unsupported. |
| `plan_invariant_failed` | Final executable-plan invariant failed. |
| `invalid_input` | Malformed payload or invalid primitive. |
| `input_limit_exceeded` | Configured resource bound exceeded. |
| `stale_plan` | Downstream preconditions no longer match current state. |
| `replacement_required` / `cancellation_required` | Pending-plan transition required. |
