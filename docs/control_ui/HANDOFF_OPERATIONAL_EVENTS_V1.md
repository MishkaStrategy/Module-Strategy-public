# Control UI Handoff — Structured Operational Events V1

Owner Issue: #391  
Owner PR: #471  
Control UI parent: #371

V1 intentionally accepts one safe source: Trading Runner. Final accepted SHA and exact-head CI evidence are recorded only after the current owner candidate is green.

## Owner query

`TradingRunnerOperationalEventQuery` reads the authoritative in-memory/owner `RuntimeEventJournal` and returns sanitized `StructuredOperationalEventPage` values.

Event contract:

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

Payload is immutable, scalar-only and field-allowlisted per event type. Raw runtime error text and kill-switch reason are explicitly dropped.

## Cursor / pagination

Cursor format is opaque to Control UI:

```text
trr-v1:<hashed-stream-id>:<owner-sequence>
```

The raw run ID is not present. A cursor from another run, malformed cursor or cursor ahead of the owner stream is rejected as `INVALID_CURSOR`.

Page limit is 1..200. At most 1000 owner events are scanned per request. Unsupported/filtered owner events advance the cursor and increment `omitted_count`; therefore a zero-event page may still have `has_more=true`.

The owner journal retains its existing hard limit. Control Plane does not create a second event journal.

## Accepted event types

```text
runtime.configuring
runtime.started
runtime.failed
runtime.stopping
runtime.stopped
kill_switch.activated
kill_switch.reset
kill_switch.cancellation_retry
operator.lifecycle
```

The accepted set may grow only by adding an explicit owner sanitizer and tests.

## Filters

Owner query supports only typed allowlisted:

```text
event_type
severity
```

Control Plane may additionally filter by registered source/resource after receiving sanitized owner pages. These filter values must never become filesystem paths or shell arguments.

## Error taxonomy

```text
INVALID_REQUEST
INVALID_CURSOR
SOURCE_UNAVAILABLE
```

Corrupt/unavailable journal state maps to `SOURCE_UNAVAILABLE`; raw journal/storage exceptions are not exposed.

## Reader-only owner HTTP transport

Control Plane should use the loopback owner endpoint:

```text
GET /events/v1/query
```

Supported query parameters are exactly:

```text
cursor
limit
event_type (repeatable)
severity (repeatable)
```

A downstream reader credential is required. Unknown parameters such as `path`, `file`, `tail`, `command`, `shell` or `argv` are rejected. Responses use `Cache-Control: no-store`.

## Multi-source Control Plane merge

When future safe owner sources are added, Control Plane should remain stateless:

- keep one opaque cursor per registered owner source in the browser/request state;
- fetch bounded sanitized pages independently;
- merge events deterministically by `(timestamp, source, event_id)`;
- preserve each source cursor separately;
- report unavailable sources explicitly;
- do not persist a replacement Control Plane event journal merely for aggregation.

## Dashboard/CLI capability unlocked after acceptance

Logs & Events may expose structured filters for:

```text
component/source
resource
event type
severity
cursor pagination
```

Live refresh/SSE may be added only over the same owner query semantics. No arbitrary file/log path input is allowed.

## Required E2E

```text
reader query -> sanitized events
pagination -> opaque cursor continuation
wrong/malformed cursor -> typed error
unavailable/corrupt source -> SOURCE_UNAVAILABLE
path/command query -> rejected
runtime.failed raw exception -> never visible
kill-switch reason -> never visible
real-loopback query -> same projection as owner Python contract
```
