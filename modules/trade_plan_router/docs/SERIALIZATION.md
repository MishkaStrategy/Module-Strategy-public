# Canonical serialization boundary

`TradePlanSerializer` is the safe JSON boundary for an already composed
`ExecutableTradePlan` or `RoutingResult`. It serializes domain objects to
canonical UTF-8 JSON and validates untrusted serialized mappings without
constructing arbitrary Python objects.

## Public API

```python
from modular_strategy_trade_plan_router import TradePlanSerializer

serializer = TradePlanSerializer(max_payload_bytes=4 * 1024 * 1024)
plan_json = serializer.dumps_plan(plan)
result_json = serializer.dumps_result(result)
plan_mapping = serializer.loads_plan_mapping(plan_json)
result_mapping = serializer.loads_result_mapping(result_json)
```

The `loads_*_mapping` methods return deeply immutable primitive mappings. They
do not recreate dataclasses, import classes named by the payload, execute hooks,
or treat the payload as evidence that an order was placed or filled.

## Validation rules

The boundary fails closed on:

- payloads above the configured byte limit;
- invalid UTF-8 or unpaired Unicode surrogates;
- duplicate JSON object keys;
- `NaN`, positive infinity, and negative infinity;
- cyclic, excessively deep, or excessive-node canonical structures;
- unknown or missing top-level and nested fields;
- non-NFC or control-character domain text;
- noncanonical UTC timestamps and Decimal strings;
- unsupported verdict, action, side, order-type, or reason-code values;
- invalid order geometry, protection allocation, sizing, OCC, or plan intervals;
- noncanonical ordering or duplicate evidence rows;
- mismatched `content_hash`, `audit_hash`, `plan_id`, or `result_id`.

A payload is rejected even when an attacker recomputes syntactically valid
hashes around structurally invalid nested content. Hash verification supplements
schema and invariant validation; it never replaces it.

## Canonical identity

Serialization uses the same bounded canonical representation as the domain
models. Equivalent mappings produce the same bytes regardless of dict insertion
order, timezone environment, or Python hash seed. Accepted routing-result
identity contains compact plan identity (`plan_id`, `content_hash`, and
`audit_hash`); rejected-result identity contains a canonical rejection-evidence
hash.

## Compatibility

The current serializer accepts only plan/result schema version `1`. Schema
migration must be implemented as an explicit, separately tested adapter. The
serializer does not silently drop unknown fields or infer a newer/older schema.
