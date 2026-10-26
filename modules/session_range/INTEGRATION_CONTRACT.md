# Integration Contract — Session Range 0.2.0

## Ownership

Session Range owns local-session boundary resolution, OHLCV accumulation, completion validation, feature publication, diagnostics, and its private checkpoint. Core owns event ordering, Candle/MarketView construction, Feature Store, dependency planning, lifecycle calls, and checkpoint orchestration.

## Input DTOs

The module accepts the actual Core `MarketView`. Fields used:

```text
current: Candle
  timestamp: aware datetime (bar open)
  close_time: aware datetime (derived by Core from timeframe)
  open, high, low, close, volume: finite numeric values
  symbol: non-empty string
  timeframe: Core-recognized positive interval

data_gap: bool
features: FeatureView | None
```

Only `current` is used for calculation. Historical bars in the view are not read, proving incremental no-look-ahead behavior.

## Output DTO

```text
ModuleDecision
  features: tuple[Feature, ...]
  skips: tuple[str, ...]
  diagnostics: mapping[str, JSON-compatible object]
```

No orders, signals, risk decisions, or portfolio mutations are produced.

## Feature contract

### `session_range.active`

- source module: `session_range`
- created_at: current candle close time
- valid_until: equal to created_at
- symbol/timeframe: copied from stream
- schema_version: `1`
- invalidate_on_gap: true
- metadata: `{session, complete: false}`

### `session_range.completed`

- source module: `session_range`
- created_at: exact session end UTC
- valid_until: instant before next enabled session start, but never before created_at
- symbol/timeframe: copied from stream
- schema_version: `1`
- invalidate_on_gap: true
- metadata: `{session, session_date, complete: true}`

Value JSON shape:

```json
{
  "session": "ny_first_4h",
  "session_date": "2026-07-20",
  "timezone": "America/New_York",
  "start_utc": "2026-07-20T04:00:00+00:00",
  "end_utc": "2026-07-20T08:00:00+00:00",
  "start_local": "2026-07-20T00:00:00-04:00",
  "end_local": "2026-07-20T04:00:00-04:00",
  "open": 100.0,
  "high": 105.0,
  "low": 99.0,
  "close": 104.0,
  "midpoint": 102.0,
  "width": 6.0,
  "width_decimal": "6.0",
  "width_pct": 0.06,
  "volume": 123.0,
  "bar_count": 48,
  "complete": true
}
```

## Reason codes

```text
session_range.duplicate_bar
session_range.conflicting_duplicate
session_range.out_of_order_bar
session_range.unknown_or_incomplete_interval
session_range.data_gap
session_range.missing_session_start
session_range.boundary_misaligned
session_range.minimum_bars_not_met
session_range.minimum_range_not_met
session_range.maximum_range_exceeded
session_range.invalid_dst_boundary
session_range.stream_limit_exceeded
```

Session-scoped reasons may be returned as `{reason}:{session_name}`. In default mode they are skips plus diagnostics. In strict mode invalid bar-contract paths raise controlled `ValueError` after diagnostics/logging.

## Temporal contract

- Local interval is half-open `[start, end)`.
- First candle open must equal session start.
- Subsequent candle open must equal prior close time.
- Candle must be fully contained.
- Completion occurs only when current candle close equals session end.
- Future bars and historical recomputation are not used.

## State contract

Schema 2 top-level JSON-compatible keys:

```text
schema_version
module_version
config_fingerprint
active
last_bars
terminal_tokens
last_completed
```

Restore is atomic. Unknown keys, unsupported version, missing schema-v2 fingerprint, mismatched fingerprint, excessive collection size, unknown session, boundary mismatch, duplicate active token, active/cursor mismatch, invalid terminal scope, or malformed completed feature reject the complete restore.

## Dependency planner

The producer declares `produced_features=("session_range.active", "session_range.completed")`. A downstream module declaring `required_features=("session_range.completed",)` must be ordered after Session Range by the existing planner. No manually hard-coded engine order is required.

## Backward compatibility

Feature keys and payload schema remain compatible with 0.1.0. State schema 1 is migrated. Exact completed `feature_id` strings change to include escaped timeframe; consumers must treat IDs as opaque.
