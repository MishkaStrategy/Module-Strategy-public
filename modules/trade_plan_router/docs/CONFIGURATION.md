# Configuration reference

Configuration schema version is `1`; unknown fields fail closed.

| Field | Default | Meaning |
|---|---:|---|
| `long_short_policy` | `reject_all` | policy for opposite-side candidates |
| `multi_setup_policy` | `highest_priority` | policy for same-side competing setups |
| `existing_position_policy` | `reject` | add/reverse/hedge behavior |
| `pending_order_policy` | `reject` | duplicate/opposite pending-order behavior |
| `active_plan_policy` | `reject_new_exposure` | global behavior when opaque active plan IDs exist |
| `strict_mode` | `true` | reject unknown released payload fields |
| `require_portfolio_snapshot` | `false` | require external snapshot feature |
| `allow_risk_reducing_when_locked` | `true` | permit explicit reduce/close through locks |
| `max_decision_age_seconds` | `86400` | maximum decision age |
| `max_snapshot_age_seconds` | `300` | maximum portfolio snapshot age |
| `plan_ttl_seconds` | `300` | Router plan lifetime |
| `max_portfolio_risk_fraction` | `0.03` | Router risk-fraction cap |
| `max_symbol_notional_fraction` | `3` | symbol notional / equity cap |
| `max_entry_candidates` | `1000` | resource limit per package |
| `max_decisions_per_type` | `2000` | protection/sizing/scaling feature limit |
| `max_portfolio_positions` | `5000` | snapshot position limit |
| `max_pending_orders` | `10000` | snapshot pending-order limit |
| `max_targets_per_plan` | `100` | target-count limit |
| `*_priorities` | `{}` | explicit integer rank maps |
| `warning_escalation` | `false` | reject otherwise accepted plans with warnings |
| `audit_level` | `full` | `minimal` or `full`; observability only |

Priority keys are stripped, NFC-normalized, length bounded, control-character
free, and unique after normalization. Numeric limits reject booleans, NaN,
infinity, negative values, and uncontrolled coercion.

See `config.example.yaml` for a complete example.

`active_plan_ids` currently provide only opaque portfolio-level identifiers;
they do not include instrument, ownership, status, or version metadata. The
safe default therefore rejects OPEN, ADD, REVERSE, and replacement actions
while any active plan exists. `allow_multiple` is an explicit portfolio policy
for runtimes that reconcile active plans outside this module. REDUCE and CLOSE
remain permitted and emit an audit warning because they lower existing risk.
