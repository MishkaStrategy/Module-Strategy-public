# Entry Model — Integration Contract

## Ownership and namespaces

- Input owner: canonical setup producer/adapter.
- Input key: `setup.confirmed`.
- Output owner: `entry_model`.
- Output keys: `entry_model.plan`, `entry_model.rejection`.
- Entry Model never mutates upstream state and never calls downstream modules.

## Input Feature envelope

```json
{
  "key": "setup.confirmed",
  "source_module": "<producer>",
  "created_at": "timezone-aware ISO-8601",
  "symbol": "BTCUSDT",
  "timeframe": "5m",
  "schema_version": "1",
  "feature_id": "stable source identifier",
  "valid_until": "ISO-8601 or null",
  "value": { "...": "canonical setup payload" }
}
```

Feature `created_at` must not be before payload `confirmed_at`. The feature must be visible in the Core Feature Store on the completed current bar.

## Canonical setup payload schema 1

Required:

- `setup_id`: nonblank string;
- `side`: `buy` or `sell`;
- `confirmed_at`: timezone-aware ISO-8601 datetime;
- `confirmation_price` or legacy alias `reference_price`: finite positive Decimal-compatible value.

Optional:

- `status`: default `confirmed`; only `confirmed` is planned;
- `symbol`: nonblank and equal to current stream symbol; otherwise Feature/current symbol fallback applies;
- `entry_kind`: supported entry-kind string;
- `extreme_price`, `entry_price`, `trigger_price`, `limit_price`: finite positive Decimal-compatible values;
- `entry_cutoff_at`: timezone-aware ISO-8601 datetime;
- `metadata`: JSON-compatible mapping;
- `entry_hint`: mapping with `kind`/`entry_kind`, price fields and `cutoff_at`.

Unknown top-level or `entry_hint` fields fail closed.

## EntryPlan schema 2

```json
{
  "plan_id": "entry-<32 hex>",
  "schema_version": "2",
  "module_version": "0.2.0",
  "setup_id": "...",
  "setup_fingerprint": "64 lowercase hex",
  "config_fingerprint": "64 lowercase hex",
  "symbol": "BTCUSDT",
  "side": "buy",
  "entry_kind": "limit_retracement",
  "order_type": "limit",
  "created_at": "ISO-8601 UTC",
  "setup_confirmed_at": "ISO-8601 UTC",
  "expires_at": "ISO-8601 UTC",
  "ttl_bars": 36,
  "price": "96.18",
  "trigger_price": null,
  "limit_price": "96.18",
  "post_only": true,
  "rationale": "setup_hint or configured_default",
  "metadata": {}
}
```

All financial values are canonical decimal strings. `plan_id` is recomputed and validated when `EntryPlan.from_feature_value` reads schema 2; mutation of execution semantics is rejected.

Legacy EntryPlan schema 1 remains readable and is promoted to a validated schema-2 object with explicit legacy fingerprints. New outputs are schema 2 only.

## Entry kinds and required geometry

- `market`: no price fields.
- `limit_confirmation`: limit equals confirmation.
- `limit_retracement`: requires directional extreme; BUY extreme below confirmation, SELL above.
- `limit_explicit`: requires `entry_price`; BUY limit cannot be above confirmation, SELL cannot be below.
- `stop_market`: requires trigger; BUY trigger cannot be below confirmation, SELL cannot be above.
- `stop_limit`: requires trigger and limit or configured offset; BUY limit not below trigger, SELL not above.

## Rejection schema 2

```json
{
  "schema_version": "2",
  "module_version": "0.2.0",
  "setup_id": "string or null",
  "reason": "entry_model.<reason>",
  "details": "safe diagnostic text",
  "created_at": "ISO-8601 UTC",
  "symbol": "BTCUSDT",
  "source_fingerprint": "64 lowercase hex",
  "config_fingerprint": "64 lowercase hex"
}
```

Typed reasons:

- `entry_model.feature_view_unavailable`
- `entry_model.data_gap`
- `entry_model.duplicate_event`
- `entry_model.out_of_order_event`
- `entry_model.conflicting_event`
- `entry_model.invalid_setup_payload`
- `entry_model.unsupported_setup_schema`
- `entry_model.setup_not_confirmed`
- `entry_model.symbol_mismatch`
- `entry_model.feature_causality_violation`
- `entry_model.future_setup`
- `entry_model.stale_setup`
- `entry_model.duplicate_setup`
- `entry_model.source_feature_conflict`
- `entry_model.setup_revision_conflict`
- `entry_model.entry_kind_not_allowed`
- `entry_model.required_price_missing`
- `entry_model.invalid_price_geometry`
- `entry_model.expired_at_creation`
- `entry_model.invalid_timeframe`
- `entry_model.internal_error`

## Time semantics

- Core supplies completed bars.
- Decision and output creation time is current bar close.
- Setup confirmation may not be in the future or after feature publication.
- Age uses exact subsecond Decimal seconds.
- Expiry is current bar close plus bar duration × `ttl_bars`, capped by setup cutoff.
- Future bars never alter an emitted plan.

## Lifecycle and replay

- Exact bar duplicate: idempotent skip.
- Changed same-timestamp bar: typed data error.
- Older bar: typed out-of-order data error.
- Exact source/setup replay: no second plan.
- Same source ID with changed content: rejection.
- Same setup ID with changed canonical content: rejection independent of input order.
- Data gap: fail closed without consuming a valid setup, permitting controlled retry.
- Checkpoint preserves replay and cursor semantics.

## Dependency planner expectation

A producer of `setup.confirmed` must exist before execution. Planner order is:

`canonical setup producer → entry_model → plan consumer(s)`.

No hard-coded order or Core strategy branch is required.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
