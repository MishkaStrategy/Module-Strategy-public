# Notifications Control v1 — Signal Bot owner control

Status: implemented on Issue #520 / Draft PR #541; exact-head acceptance required before merge into parent #371.

Related: Control UI #370, accepted Signal Bot owner contract #383 / PR #469.

## Ownership

Signal Bot remains authoritative for scheduler state, durable operator idempotency/recovery, Telegram delivery, fixed test-message semantics and downstream configuration.

Control Plane and Dashboard do **not**:

- open or migrate Signal Bot SQLite state;
- create a second notification/idempotency ledger;
- call Telegram clients or notifier send methods directly;
- read or return Telegram bot tokens, chat IDs or message bodies;
- accept browser/CLI supplied message, operator, chat, token or settings fields;
- expose arbitrary send/settings endpoints.

The browser and CLI supply only a bounded `request_id`. The Signal Bot owner assigns the trusted operator and fixed test message.

## Authoritative owner transport

The accepted owner surface is loopback-only and authenticated with distinct server-side reader/operator credentials:

```text
GET  /control/v1/status
POST /control/v1/test-notification
```

Control Plane configuration:

```text
MODULAR_CONTROL_SIGNAL_BOT_CONTROL_URL=http://127.0.0.1:8081
MODULAR_CONTROL_SIGNAL_BOT_CONTROL_READER_TOKEN=<server-side reader token>
MODULAR_CONTROL_SIGNAL_BOT_CONTROL_OPERATOR_TOKEN=<server-side operator token>
```

The URL must use plain local HTTP and target `127.0.0.1`, `localhost` or `::1` with no credentials, path prefix, query or fragment. Reader/operator credentials must both be present and distinct. They are never forwarded to the browser or CLI.

The owner-control response is bounded to 64 KiB and parsed fail-closed. The accepted status allowlist is:

- `configured`;
- `reachable`;
- `notifications_enabled`;
- `status`;
- `cycles`;
- `signals_sent`;
- `last_success_at`;
- `last_error_type`.

Unknown/private owner fields are rejected rather than silently ignored.

## Legacy health fallback

`MODULAR_CONTROL_SIGNAL_BOT_URL` and the native read-only `/health` adapter remain supported as a compatibility fallback when authenticated owner control is not configured.

When owner control is configured, its authenticated status becomes authoritative for the `signal_bot` Control Plane component health. Control Plane must not combine two competing health truths.

## Public Control Plane API

Reader-authenticated status:

```text
GET /api/control/v1/notifications/status
```

Operator-authenticated fixed test notification:

```text
POST /api/control/v1/notifications/test
Content-Type: application/json

{
  "schema_version": 1,
  "request_id": "dashboard-notification-..."
}
```

`extra="forbid"` contracts reject fields such as `message`, `operator`, `chat_id`, `bot_token`, paths or settings before they can reach the owner transport.

Owner-native error semantics are preserved through sanitized Control Plane errors:

- invalid request — non-retryable;
- not configured — non-retryable;
- unreachable — retryable with the same request identity;
- rate limited — retryable after the owner limit clears;
- rejected — non-retryable;
- idempotency conflict — non-retryable;
- recovery required — fail closed until owner recovery is resolved;
- downstream authentication/configuration errors — fail closed without exposing credentials or raw response bodies.

Control Plane stores no notification request state.

## Dashboard

`#/notifications` is a native workspace in the common Control Dashboard shell. It shows only the owner status allowlist and provides:

- `Test notification` — creates a bounded browser-memory request ID;
- `Replay same request safely` — reuses the exact same request ID so the owner can return its durable replay result.

Reader/operator tokens remain only in React memory and are cleared by reload. Browser transport uses `credentials: "omit"` and `cache: "no-store"`; localStorage, sessionStorage, cookies and IndexedDB are not used for credentials or idempotency state.

There are no message/chat/token/settings inputs in the UI.

## CLI

The existing `modular-control` binary is extended without a second executable:

```text
modular-control notifications status
modular-control notifications status --json
modular-control notifications test --request-id <bounded-id>
modular-control notifications test --request-id <same-id> --json
```

Reader and operator Control Plane credentials continue to come from the existing environment variables:

```text
MODULAR_CONTROL_READER_TOKEN
MODULAR_CONTROL_OPERATOR_TOKEN
```

Existing commands continue to delegate to the legacy CLI dispatcher. The Notifications dispatcher recognizes only a real `notifications` command token after global `--url` / `--timeout` options; a later argument value named `notifications` does not hijack an existing command.

## Real-owner acceptance

The existing `market-data-web-console-e2e` workflow is extended; no new workflow is introduced.

The real loopback chain is:

```text
SignalBotControlLedger
  -> SignalBotOperatorControl
  -> SignalBotControlHttpServer
  -> Control Plane Notifications API
  -> Dashboard
```

The fixture uses a counting sender only as the external side-effect sink. Browser E2E sends a new request and then replays the identical request ID. The workflow requires the final sender count to equal exactly `1`, proving the owner replay did not duplicate the external send.

The same run also verifies:

- exact browser request shape contains only `schema_version` and `request_id`;
- owner-assigned operator is returned;
- replay returns the same request identity with `replayed=true`;
- fixed/private message, token, chat ID and owner paths are absent from the DOM;
- no message/chat input exists;
- browser storage remains empty;
- generated Control Plane and owner credentials are absent from E2E logs.

## Acceptance gates

Before merge, the exact PR head must pass:

- Fast checks;
- Control Plane foundation on Python 3.12 and 3.13 including Ruff, strict MyPy, component tests and wheel/entrypoint smoke;
- clean installed-wheel/default-server acceptance;
- static Web production audit, browser security boundary, TypeScript, unit tests and build;
- actionlint;
- Test Architecture where triggered;
- full real-loopback browser E2E with `send_count == 1`.

Artifact-storage upload failures from the shared quota incident are classified separately only after all substantive repository checks in that job have passed.
