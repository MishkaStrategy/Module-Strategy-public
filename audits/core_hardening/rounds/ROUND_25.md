# Round 25 — structured reasons and failures

Skip evidence now contains stable `reason_code` plus optional `reason_detail` while preserving the original `reason` string. Kernel errors now expose `error_type` and `message` alongside the legacy repr field.

Existing consumers of `reason` and `error` remain compatible.
