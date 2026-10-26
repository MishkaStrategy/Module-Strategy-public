# Structured Operational Events v1

Status: **Control UI integration in Draft PR #552; exact-head acceptance pending**.

Control UI Issue: #521  
Parent Control UI Issue: #370  
Parent Draft PR: #371  
Owner dependency: #391  
Accepted owner PR: #471

## Scope

V1 exposes only the accepted Trading Runner structured operational-events source. It is a typed, read-only projection over the owner cursor contract.

Control Plane does **not** create a persistent event journal, inspect Trading Runner databases/log files, tail arbitrary paths, execute shell commands, expose argv/cmdline/environment, or return raw stack traces/external payloads.

## Owner contract

Accepted fields:

- `event_id`
- `source`
- `resource`
- `timestamp`
- `event_type`
- `severity`
- `state`
- bounded scalar `payload`
- opaque `cursor`

Accepted page metadata:

- `next_cursor`
- `has_more`
- `scanned_count`
- `omitted_count`
- `retention_limit`
- `truncated`

Accepted Trading Runner event types:

- `runtime.configuring`
- `runtime.started`
- `runtime.failed`
- `runtime.stopping`
- `runtime.stopped`
- `kill_switch.activated`
- `kill_switch.reset`
- `kill_switch.cancellation_retry`
- `operator.lifecycle`

Severities are `INFO`, `WARNING`, `ERROR`.

## Control Plane transport

Server-side owner configuration:

- `MODULAR_CONTROL_TRADING_RUNNER_EVENTS_URL`
- `MODULAR_CONTROL_TRADING_RUNNER_EVENTS_READER_TOKEN`

The URL must be pathless loopback HTTP. Partial configuration fails closed. The owner token is server-side only and is never projected to the browser/CLI response.

The adapter issues only `GET /events/v1/query` with allowlisted parameters:

- `cursor`
- `limit`
- repeatable `event_type`
- repeatable `severity`

No `path`, `file`, `command`, `tail`, `shell`, `argv` or arbitrary component query exists. Unsupported Control Plane query fields are rejected with a typed `400` before the owner transport is called; their names and values are not echoed in the response.

## Control Plane API

Reader-authenticated route:

```text
GET /api/control/v1/events/trading-runner
```

The Control Plane remains stateless with respect to event persistence. It validates and translates the owner page but does not merge or rewrite owner cursor history.

Error mapping is deterministic:

- unsupported query field -> 400 `CONTROL_EVENTS_INVALID_REQUEST`;
- invalid bounded query -> 400;
- invalid/stale cursor -> 400 with action to restart without cursor;
- unavailable source -> 503 retryable;
- unreachable owner -> 503 retryable;
- owner authentication/configuration failure -> typed 503;
- incompatible owner response -> typed 503.

Raw owner response bodies and transport exception text are not emitted.

## CLI

```text
modular-control events [--cursor <opaque>] [--limit 1..200]
                       [--event-type <accepted-type>]...
                       [--severity INFO|WARNING|ERROR]...
                       [--json]
```

The CLI uses the existing reader credential and preserves the owner cursor unchanged. It does not accept filesystem paths or commands.

## Dashboard

`#/events` is a native Dashboard workspace exposed as **Logs & Events**.

Owner-side filters:

- event type;
- severity;
- cursor pagination.

`Resource` search is explicitly labelled **current page only** because owner #391 does not expose a global resource query in V1. The UI does not pretend that client-side filtering changes owner pagination semantics.

Manual refresh plus bounded 10-second polling are used. No SSE/live stream is invented because the accepted owner contract is query/cursor based.

## Security/privacy boundary

The browser parser fails closed on:

- nested payloads;
- unknown event types;
- invalid source/cursor/event IDs;
- oversized payload strings;
- malformed timestamps;
- oversized responses.

The browser request client also runtime-validates event-type, severity, cursor and page-limit inputs before transport.

The browser displays only typed scalar payload fields. Technical error details, private filesystem paths, secrets and raw owner payloads are discarded by the client error boundary.

## V1 limitations

- Trading Runner is the only accepted source.
- There is no heterogeneous owner cursor merge yet.
- There is no global resource filter in owner V1.
- There is no SSE/live subscription contract.
- Retention is owner-reported; Control Plane does not persist a replacement journal.

These limitations are intentional. Additional sources must first provide equally bounded owner-native contracts.

## Acceptance before parent integration

PR #552 must prove on its exact head:

- adapter URL/auth/query allowlisting;
- cursor and pagination semantics;
- owner retention/truncation projection;
- owner error taxonomy;
- malformed/oversized response fail-closed behavior;
- explicit rejection of raw path/file/tail/shell/command query fields before owner transport;
- reader-authenticated Control Plane API;
- CLI parity;
- strict browser parser/client tests;
- Dashboard route/filter/pagination behavior;
- real-loopback browser flow against `TradingRunnerOperationalEventQuery` + `TradingRunnerOperationalEventsHttpServer`;
- privacy canaries absent from UI/log evidence;
- Ruff, MyPy, pytest, strict TypeScript, Vitest, Playwright and production build.
