# Migration Notes — 0.1.0 to 0.2.0

## Compatibility

- Public import path and entry-point name are unchanged.
- Feature keys and feature value schema remain unchanged.
- Constructor shape remains `SessionRangeModule(sessions, strict_data_validation=False)`.
- Core compatibility is widened from exactly 0.4.0 to `>=0.4,<0.5` without importing private core APIs.
- Module state schema changes from `1` to `2`.

## State migration

Schema-v1 checkpoints are accepted. On restore, completed feature IDs are recomputed using the v0.2 identity including timeframe. The v1 record must contain a valid `session_date` in metadata or value. Schema-v2 checkpoints require the canonical configuration fingerprint and reject unknown top-level keys.

A checkpoint created under a different session definition, timezone, boundaries, weekdays, range filters, active-emission flag, DST fold, or strict-data-validation setting is rejected. This is intentional fail-closed behavior.

## Identifier change

Old completed ID:

```text
session-range-completed:{session}:{symbol}:{session_date}
```

New completed ID:

```text
session-range-completed:{session}:{escaped_symbol}:{escaped_timeframe}:{session_date}
```

Downstream code must treat `feature_id` as opaque and use `feature.key`, `symbol`, `timeframe`, and metadata for semantic selection.

## Operational change

A new stream is rejected with reason `session_range.stream_limit_exceeded` after 1,024 tracked symbol/timeframe streams. Configure separate engine/module instances when intentionally processing a larger universe.
