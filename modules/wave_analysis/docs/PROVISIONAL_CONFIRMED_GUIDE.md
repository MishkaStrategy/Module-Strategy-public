# Provisional and confirmed outputs

`ProvisionalPivot` and `PivotPoint` are distinct DTOs. A provisional item carries `revision`, `revision_id`, `observed_at`, threshold and `status=provisional`. A confirmed item carries immutable identity, `pivot_time`, `confirmed_at` and `status=confirmed`.

A stable provisional observation does not increase revision. A changed extreme or threshold does. `publish_provisional=false` suppresses provisional-only Feature Store writes but keeps internal detector tracking.
