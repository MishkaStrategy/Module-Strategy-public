# Trading Runner Lifecycle Owner Transport V1

Issue #381 requires an authenticated boundary between the singleton Trading Runner owner and Control Plane. The transport is `TradingRunnerLifecycleHttpServer`; it does not create a second lifecycle state machine.

## Network and authorization

- bind is restricted to `127.0.0.1`, `localhost` or `::1`;
- reader and operator bearer credentials are required and must be distinct;
- mutation endpoints authorize only the operator credential;
- a reader credential presented to any mutation endpoint receives `401 UNAUTHORIZED` before the owner is called;
- bearer values are never copied into `TrustedOperatorIdentity`, audit, response bodies or object repr;
- responses use `Cache-Control: no-store`.

The owner transport assigns the trusted subject `control-plane:local-operator` by default. Browser JSON cannot provide an operator identity.

## Typed endpoints

Only these POST paths exist:

```text
/lifecycle/v1/start
/lifecycle/v1/stop
/lifecycle/v1/kill-switch/activate
/lifecycle/v1/kill-switch/reset
```

The request body is exactly:

```json
{"request_id":"bounded-id"}
```

Extra fields such as `reason`, `operator`, `command`, `path` or arbitrary payloads are rejected. Body size is bounded to 4 KiB.

Kill-switch reason remains the owner-defined fixed value from the Python lifecycle contract; it cannot be supplied through HTTP.

## Singleton execution

`TradingRunnerLifecycleHttpServer` sends all asynchronous lifecycle mutations to one dedicated owner event loop and one `TradingRunnerOperatorLifecycle` instance. This preserves the V1 invariant that a runtime has one authoritative mutation writer.

The underlying owner performs durable request-id claim/replay and the native `TradingRunner` retains its lifecycle lock. Control Plane must connect to this owner transport; it must not construct an independent `TradingRunnerOperatorLifecycle` against the same persistence.

A bounded transport timeout returns `RECOVERY_REQUIRED`. The in-flight owner coroutine is not converted into a second attempt; a retry with the same `request_id` follows the durable owner replay/uncertain-result rules.

## Error mapping

- malformed/PAPER-only request: HTTP 400;
- reader or invalid credential: HTTP 401;
- idempotency conflict: HTTP 409;
- invalid transition: HTTP 422;
- recovery-required or owner failure: HTTP 503.

Raw owner exceptions are not serialized.

## Control UI adapter

Control Plane should hold a downstream owner reader/operator credential pair separately from browser credentials. Browser reader authorization must never be promoted to the downstream operator credential. The Control Plane operator endpoint forwards only the validated request ID to the fixed owner path and returns the typed owner result/error.

The existing owner read projection remains authoritative for current runtime status; mutation responses are idempotency receipts, not a parallel runtime state projection.
