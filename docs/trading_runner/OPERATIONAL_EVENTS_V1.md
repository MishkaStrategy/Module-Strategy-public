# Trading Runner Structured Operational Events V1

Issue #391 requires a safe owner-backed operational event query for Control UI Logs & Events. V1 intentionally accepts only one authoritative source: the Trading Runner `RuntimeEventJournal`.

## Ownership

`RuntimeEventJournal` remains the authoritative hash-chained owner event stream. The query layer is stateless and read-only. It does not create a second persistent event journal and does not read paths, log files, component databases or shell output.

Public Python contracts:

```python
from modular_trading_runner import (
    OperationalEventQueryError,
    OperationalEventQueryErrorCode,
    OperationalSeverity,
    StructuredOperationalEvent,
    StructuredOperationalEventPage,
    TradingRunnerOperationalEventQuery,
)
```

## Event model

Every projected event contains:

- `event_id`;
- fixed `source="trading_runner"`;
- bounded owner `resource` supplied when the query is composed;
- UTC `timestamp`;
- allowlisted `event_type`;
- typed severity (`INFO`, `WARNING`, `ERROR`);
- bounded native state when available;
- scalar-only immutable sanitized `payload`;
- opaque source cursor.

The owner query never returns raw owner payloads.

## Accepted V1 event types

V1 projects only explicit sanitizer-backed events:

- `runtime.configuring`;
- `runtime.started`;
- `runtime.failed`;
- `runtime.stopping`;
- `runtime.stopped`;
- `kill_switch.activated`;
- `kill_switch.reset`;
- `kill_switch.cancellation_retry`;
- `operator.lifecycle` when the lifecycle owner contract is present in the composed branch.

All other owner events are omitted. Their owner sequence still advances the cursor, so a caller cannot loop forever over an unsafe/unprojected event.

## Privacy and sanitization

Projection is field-level allowlist, not blocklist.

Examples:

- `runtime.failed` retains only a bounded error **type**; raw exception text is dropped;
- kill-switch events never project the free-form owner `reason`;
- operator lifecycle events retain only typed operation/request/operator/result/native states/config hash/replay/error code;
- nested values, raw external payloads, stack traces, URLs, paths, argv/cmdline, environment values and credentials are never projected.

Payload values are scalar-only: `str | int | float | bool | None`.

## Cursor and pagination

Cursor form is opaque to Control Plane consumers. V1 binds it to the owner run by hashing the private run identifier and coupling that stream token with the owner journal sequence.

Properties:

- cursor is stateless; no Control Plane cursor persistence is required;
- a cursor from another run is `INVALID_CURSOR`;
- a cursor ahead of the current owner stream is `INVALID_CURSOR`;
- unsupported/filtered owner events still advance the cursor;
- each page returns at most 200 projected events;
- each query scans at most 1000 owner events, even when filters are sparse;
- a zero-result page may therefore return `has_more=true`; the caller continues with `next_cursor`;
- V1 owner retention is the existing hard journal limit of 1,000,000 events and performs no compaction, therefore `truncated=false`.

## Owner-side filters

The owner query accepts optional typed filters for:

- allowlisted `event_types`;
- `OperationalSeverity` values.

Empty, duplicate, unknown or incorrectly typed filters fail as `INVALID_REQUEST`. `source` is fixed by the owner implementation and `resource` is fixed by query composition, so those filters are applied by a future multi-owner Control Plane aggregator rather than by reading arbitrary owner locations.

## Error taxonomy

- `INVALID_REQUEST` — invalid limit/filter/resource contract;
- `INVALID_CURSOR` — malformed, wrong-stream or ahead cursor;
- `SOURCE_UNAVAILABLE` — owner journal unavailable, corrupt hash chain or invalid owner event metadata.

Raw storage/journal exception causes are suppressed. Safe error text never includes an owner path, raw payload or credential.

## Heterogeneous merge handoff

Control Plane must treat each owner cursor independently. A future multi-source Logs & Events adapter should:

1. query only registered owner adapters;
2. retain one opaque cursor per source in a stateless aggregate cursor;
3. merge already-sanitized owner events by `(timestamp, source, event_id)` for deterministic display;
4. represent an unavailable source explicitly without substituting file/log reads;
5. never persist a replacement Control Plane journal merely to merge sources.

V1 can be accepted and exposed with only the Trading Runner source. Additional owners require their own safe query contracts rather than weakening this boundary.

## Control UI handoff

After owner acceptance, #371 may add Logs & Events with source/component, event type, severity and resource filters plus bounded cursor pagination. Live refresh/SSE is optional and must consume owner-backed queries; it must not tail files or execute commands.

Forbidden surfaces remain:

```text
path=/some/file.log
tail
cat
shell=True
command=<browser supplied>
```
