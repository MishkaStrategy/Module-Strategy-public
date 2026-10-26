# Signal Bot Operator Control Contract V1

Issue #383 owns the safe Signal Bot observation and test-notification boundary required by Control UI.

## Ownership

Signal Bot remains authoritative for Telegram delivery, runtime health, notification preferences and owner persistence. Control Plane must never:

- construct `TelegramNotifier` or `TelegramBotClient`;
- introspect objects that retain Telegram token-bearing URLs;
- open or migrate the Signal Bot SQLite database;
- read or return Telegram bot tokens or chat IDs;
- accept an arbitrary notification message body;
- create a second notification/idempotency state machine.

The V1 owner implementation is split into:

- `SignalBotOperatorControl` — typed owner query/mutation service;
- `SignalBotControlLedger` — durable idempotency/status/audit tables in the **owner** Signal Bot SQLite database;
- `TelegramTestNotificationSender` — private adapter that sends one fixed owner-defined text;
- `SignalBotControlHttpServer` — authenticated loopback-only transport for another local process such as Control Plane.

No trading, order or portfolio mutation is present.

## Public Python contract

```python
from signal_bot import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    SignalBotControlHttpServer,
    SignalBotControlLedger,
    SignalBotControlStatus,
    SignalBotOperatorControl,
    TelegramTestNotificationSender,
    TestNotificationRequest,
    TestNotificationResult,
    TrustedSignalBotOperator,
)
```

`TestNotificationRequest` contains only:

- bounded `request_id` (1..128 ASCII alphanumeric plus `._:-`);
- typed `TrustedSignalBotOperator` (1..128 ASCII alphanumeric plus `._:@-`).

There is no message/reason/chat/token field.

The fixed test text is owner code:

```text
Module Strategy Signal Bot test notification
```

## Safe status projection

`SignalBotControlStatus` exposes only:

- `configured`;
- `reachable`;
- `notifications_enabled`;
- `status` (`ok`, `degraded`, otherwise `unavailable`);
- bounded non-negative `cycles`;
- bounded non-negative `signals_sent`;
- `last_success_at`;
- `last_error_type`.

`configured` is explicit owner truth. It is not inferred from endpoint reachability.

`notifications_enabled` is obtained from the already-owned Signal Bot state/preferences callback for the configured target chat. The chat ID itself is never projected.

`reachable` is nullable last-known Telegram test-transport state maintained by the owner ledger:

- successful test -> `true`;
- network/unreachable test failure -> `false`;
- HTTP rate-limit/rejection proves the endpoint was reachable -> `true`;
- no accepted test evidence yet / owner status unavailable -> `null`.

V1 does not introspect Telegram client objects and does not claim that local `/health` reachability proves Telegram reachability.

The health callback is allowlisted field-by-field. Raw `errors_by_symbol`, injected token/chat/path fields and unknown health status strings are ignored.

## Durable idempotency

The ledger uses the same Signal Bot-owned SQLite database file, but separate owner tables:

- `signal_bot_operator_requests`;
- `signal_bot_operator_audit`;
- `signal_bot_operator_status`.

Control Plane never constructs this ledger. The running Signal Bot daemon owns it.

Writes use SQLite `BEGIN IMMEDIATE`, WAL and a process lock. This serializes competing owner instances/connections before a request is claimed.

The first accepted request is persisted as `IN_PROGRESS` and audited as `ACCEPTED` **before** Telegram send is attempted.

Replay semantics:

- first request -> one authoritative send attempt;
- exact completed success -> original completion timestamp/result with `replayed=true`; no send;
- exact completed failure -> same safe terminal error; no send;
- concurrent duplicate while first request is `IN_PROGRESS` -> `RECOVERY_REQUIRED`; no second send;
- process restart with completed request -> replay; no second send;
- process restart with `IN_PROGRESS` -> `RECOVERY_REQUIRED`; no send;
- same request ID with a different trusted operator -> `IDEMPOTENCY_CONFLICT`; no send.

If Telegram may have received the message but durable success persistence fails, the request remains `IN_PROGRESS`. Retrying the same request ID therefore fails closed as `RECOVERY_REQUIRED` rather than sending again.

V1 retains at most 2048 request identities and never evicts them automatically. Capacity exhaustion fails with `OWNER_FAILURE`; it does not forget an old request ID and risk a duplicate side effect.

## Error taxonomy

Public errors are deterministic and do not chain raw transport/storage exceptions:

- `INVALID_REQUEST`;
- `NOT_CONFIGURED`;
- `UNREACHABLE`;
- `RATE_LIMITED`;
- `REJECTED`;
- `IDEMPOTENCY_CONFLICT`;
- `RECOVERY_REQUIRED`;
- `OWNER_FAILURE`.

Telegram HTTP 429 maps to `RATE_LIMITED`; other 4xx transport rejection maps to `REJECTED`; 5xx/network/timeout/connection failures map to `UNREACHABLE`; unknown owner failures map to `OWNER_FAILURE`.

Raw Telegram URLs can contain the bot token. Those exception strings/causes are never returned, persisted in the mutation record, projected in status, or copied into audit.

## Owner audit

Authenticated/typed owner requests produce durable structured audit rows with only:

- monotonic `event_id`;
- timestamp;
- fixed operation `TEST_NOTIFICATION`;
- request ID;
- trusted operator subject;
- safe result code/state.

No message body, Telegram token, chat ID, URL, filesystem path, environment value or raw exception is stored.

Conflict, uncertain replay, accepted, success, terminal failure and completed replay attempts are durably audited. Persisted audit rows are validated again before being returned; corrupt/private arbitrary row values fail closed as `RECOVERY_REQUIRED`.

## Authenticated owner HTTP transport

The optional owner transport exposes exactly:

```text
GET  /control/v1/status
POST /control/v1/test-notification
```

It binds only to `127.0.0.1`, `localhost` or `::1`. Remote bind such as `0.0.0.0` is rejected.

The transport requires two distinct downstream bearer credentials:

```text
SIGNAL_BOT_CONTROL_READER_TOKEN
SIGNAL_BOT_CONTROL_OPERATOR_TOKEN
```

They must be configured together. The daemon does not start the control transport when they are absent.

Optional transport location:

```text
SIGNAL_BOT_CONTROL_HOST=127.0.0.1
SIGNAL_BOT_CONTROL_PORT=8081
```

The reader token can call only the status endpoint. It receives HTTP 401 on the mutation endpoint. The operator token can call the mutation endpoint.

The POST body is bounded to 4096 bytes and must be exactly:

```json
{"request_id":"bounded-request-id"}
```

Additional `message`, `operator`, `chat_id`, path, command or arbitrary fields are rejected. The HTTP owner constructs the trusted operator identity itself as `control-plane:local-operator`; browser/request JSON cannot overwrite it.

The current Control Plane operator authorizer also resolves authenticated operator requests to a local operator identity, so V1 preserves the currently available attribution granularity without accepting an untrusted browser identity.

HTTP responses contain only safe typed status/result/error fields. Authorization headers/tokens are never echoed or logged by the owner handler.

## Runtime composition

In normal daemon mode the Signal Bot CLI, and only the Signal Bot owner process, composes the control boundary when both downstream control credentials are set:

1. existing `SQLiteStateStore` owns Signal Bot state;
2. `SignalBotControlLedger` attaches owner control tables to that same owner database;
3. `SignalBotOperatorControl` receives the live `orchestrator.health` callback and live notification-preference callback;
4. `TelegramTestNotificationSender` wraps the already-owned `TelegramBotClient` and target chat privately;
5. `SignalBotControlHttpServer` exposes the narrow loopback transport.

`--once` does not start the operator HTTP transport.

## Control UI handoff

After owner acceptance, Control UI #371 can implement a typed HTTP adapter without opening Signal Bot SQLite or importing Telegram objects.

The downstream adapter must:

- validate a loopback-only base URL with no credentials/query/fragment;
- keep owner reader/operator credentials separate from browser-visible data;
- require Control Plane reader scope for status and operator scope for mutation;
- send only the typed request ID to the fixed owner endpoint;
- map owner error codes without exposing response internals;
- use the existing Control Plane trusted operator identity model;
- keep `test_notification_available=false` unless the authenticated owner control endpoint is explicitly configured;
- never infer `configured=true` from the existence of the endpoint itself.

Recommended separate Control Plane downstream settings are owner-specific Signal Bot control URL/reader token/operator token values; browser bearer credentials should not simply be forwarded as owner credentials.

## Known V1 limitations

- `reachable` is last-known test-transport evidence, not continuous Telegram polling.
- owner request IDs are retained up to the fixed capacity; there is no archive/compaction operation in V1.
- operator attribution matches the current local Control Plane identity granularity rather than an external multi-user identity provider.
- the existing public Signal Bot `/health` remains read-only and separate; it is not upgraded into an unauthenticated mutation surface.
