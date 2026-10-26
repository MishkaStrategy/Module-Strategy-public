# Trading Runner Operational Events HTTP V1

Issue #391 requires Control Plane to consume owner-backed structured events without reading arbitrary component files, databases or process output. `TradingRunnerOperationalEventsHttpServer` is the reader-only transport for the V1 Trading Runner source.

## Boundary

- loopback bind only: `127.0.0.1`, `localhost` or `::1`;
- downstream reader bearer credential required;
- no mutation endpoint exists;
- response uses `Cache-Control: no-store`;
- owner query object is already composed against the authoritative `RuntimeEventJournal`;
- Control Plane never receives a filesystem path, database path, journal object or raw event payload.

Endpoint:

```text
GET /events/v1/query
```

## Query parameters

Only these parameters are accepted:

- `cursor` — one opaque owner cursor;
- `limit` — one integer, 1..200;
- repeated `event_type` — only sanitizer-backed owner event types;
- repeated `severity` — `INFO`, `WARNING`, `ERROR`.

Unknown parameters are rejected. In particular there is no `path`, `file`, `tail`, `command`, `shell`, `argv` or raw-query surface. The complete request target is bounded to 4096 characters and parsed with a bounded field count.

## Response

The response contains only the sanitized `StructuredOperationalEventPage` contract:

```text
source
events[]
next_cursor
has_more
scanned_count
omitted_count
retention_limit
truncated
```

Each event contains:

```text
event_id
source
resource
timestamp
event_type
severity
state
payload
cursor
```

The payload is the owner query's scalar-only field allowlist. The transport does not reconstruct or expose raw owner payloads.

## Errors

- missing/wrong reader credential → HTTP 401 `UNAUTHORIZED`;
- malformed/unknown filter → HTTP 400 `INVALID_REQUEST`;
- malformed/wrong-stream/ahead cursor → HTTP 400 `INVALID_CURSOR`;
- corrupt/unavailable owner journal → HTTP 503 `SOURCE_UNAVAILABLE`.

Raw owner/storage exception text is never serialized.

## Control Plane handoff

Control Plane may register this fixed loopback owner endpoint and keep its downstream reader credential server-side. The Dashboard/CLI can request source/event-type/severity/resource filters through the typed Control Plane adapter; Control Plane may apply source/resource filtering over registered adapters but must not translate those fields into file paths.

For a future multi-owner Logs & Events view, Control Plane retains one opaque cursor per registered source and merges already-sanitized event pages deterministically. It must not create a replacement persistent event journal merely to merge streams.
