# Control UI Handoff — Signal Bot Control V1

Owner Issue: #383  
Owner PR: #469  
Control UI parent: #371

Final accepted SHA and exact-head CI runs are recorded only after the current owner candidate is green.

## Safe read contract

The owner projects only:

```text
configured
reachable
notifications_enabled
status
cycles
signals_sent
last_success_at
last_error_type
```

No Telegram token, chat ID, request/base URL, notifier/client internals, filesystem path or raw owner exception is returned.

## Mutation contract

`TestNotificationRequest` contains a bounded `request_id` and a trusted owner-assigned operator identity. The message is a fixed predefined owner string; browser/API callers cannot supply arbitrary notification text.

Durable SQLite owner records provide:

- exact duplicate replay;
- same request ID / different operator conflict;
- cross-instance concurrency serialization with `BEGIN IMMEDIATE`;
- persisted `IN_PROGRESS` uncertain-result semantics;
- restart replay without a second send;
- durable bounded audit.

## Error taxonomy

```text
INVALID_REQUEST
NOT_CONFIGURED
UNREACHABLE
RATE_LIMITED
REJECTED
IDEMPOTENCY_CONFLICT
RECOVERY_REQUIRED
OWNER_FAILURE
```

Telegram HTTP/token-bearing URL exceptions are reduced to this taxonomy. Raw URL, token, chat ID and exception text are not propagated.

## Owner HTTP transport

Loopback-only authenticated transport with distinct downstream reader/operator credentials:

```text
GET  /control/v1/status
POST /control/v1/test-notification
```

Status requires the reader credential. Test notification requires the operator credential. POST body is exactly:

```json
{"request_id":"bounded-id"}
```

The owner assigns `control-plane:local-operator`; browser input cannot provide operator identity or message body.

## Control Plane adapter requirements

Control Plane must consume the owner transport, not `TelegramNotifier`, `TelegramBotClient`, or Signal Bot SQLite directly. It must keep downstream credentials server-side and preserve browser reader/operator authorization separation.

The adapter may expose typed status and test-notification actions only. Notification settings mutation remains unavailable unless a separate owner-native contract is introduced.

## Dashboard/CLI capability unlocked after acceptance

Notifications workspace may show:

```text
Telegram
Status
Configured
Reachable
Notifications enabled
Last success
Last error
```

and expose `Test notification` with explicit confirmation/request ID. Exact duplicate retry must render as replay and must not send a second Telegram message.

## Required E2E

```text
reader status -> safe projection
reader token -> test mutation denied
operator test -> success
same request retry -> replay / one send
owner restart -> same request replay / no second send
rate-limit/rejected/unreachable -> deterministic safe error
malformed/arbitrary message body -> rejected
```
