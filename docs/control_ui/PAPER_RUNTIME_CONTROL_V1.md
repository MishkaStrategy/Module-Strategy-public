# Paper Runtime Control v1

## Scope

Paper Runtime Control projects the **already-owned live paper runtime** into the unified Control UI and, after acceptance of owner Issue #381, exposes a narrow owner-native lifecycle mutation adapter.

It does not create a second Trading Runner, broker, OMS, order ledger, position store, lifecycle state machine, idempotency ledger or recovery authority.

The ownership chain remains:

`Trading Runner -> lifecycle / supervision / kill-switch / recovery / lifecycle idempotency + audit`

`Paper Execution + Core -> plans / orders / fills / reconciliation / positions`

`Control Plane -> bounded typed adapter and operator-facing projection only`

This surface is PAPER-only. Live trading, private exchange credentials, signed private requests and remote Control Plane binding are outside the contract.

## Read projection attachment

`PaperRuntimeControlService` is attached explicitly to an existing runtime host. The standalone `modular-control-api` process does not discover or auto-bootstrap a Trading Runner from files or browser input.

When no runtime is attached, read routes return the typed error `CONTROL_PAPER_RUNTIME_NOT_ATTACHED`. This is intentional: a 503 is truthful and prevents the Control Plane from silently creating a second runtime.

`modular-control-api --check` continues to report `paper_runtime_embedded_only=true`; no runtime path or exchange secret is accepted or emitted.

## Owner query ports

### Trading Runner read projection

`ReadOnlyTradingRuntimeQuery` accepts only an existing `TradingRunner`, plus an optional `TradingRuntimeService` that must own the same runner.

Projected fields include runtime/portfolio/strategy identity, native lifecycle state, fixed paper mode, market/timeframe/instruments, configuration hash, bounded kill-switch status and queue counters. Free-form kill reason, component exception detail, journal payloads, filesystem paths and credentials are excluded.

### Paper Execution read projection

`ReadOnlyPaperExecutionQuery` accepts only an existing `PaperExecutionCoordinator` and projects bounded native lifecycle, order accounting and blocked reconciliation scopes. Raw journal/event payloads, reconciliation evidence, plan payloads and private metadata are excluded.

## Positions

`position_projection_available=false` remains deliberate.

Core/Paper Execution does not yet expose the required safe no-argument authoritative position list contract for this surface. Control UI does not invent a second position store and does not ask the browser for mark prices to manufacture runtime truth.

## Owner-native lifecycle mutation boundary

Owner Issue #381 / PR #465 accepted `TradingRunnerOperatorLifecycle` and `TradingRunnerLifecycleHttpServer` at owner exact head `47524a400ed927cf722551d42dd009c153802e6c`.

Supported operations are exactly:

- `START`
- `STOP`
- `ACTIVATE_KILL_SWITCH`
- `RESET_KILL_SWITCH`

The owner owns durable idempotency, exact replay, same-ID conflicts, concurrency serialization, restart/recovery behavior, trusted operator attribution and audit. The Control Plane must not duplicate those semantics.

A mutation receipt is historical evidence for that request. Current runtime state must still be read from the authoritative Paper Runtime projection.

## Control Plane lifecycle transport

The downstream adapter accepts only an HTTP loopback base URL and distinct bounded reader/operator owner tokens configured server-side.

Environment configuration:

- `MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_URL`
- `MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_READER_TOKEN`
- `MODULAR_CONTROL_TRADING_RUNNER_LIFECYCLE_OPERATOR_TOKEN`

Partial configuration fails closed. A non-loopback/decorated URL or identical owner credentials is rejected.

The adapter forwards only a bounded request identity. Browser operator identity, reason, command, path, shell input, mode selection or arbitrary execution payload are not accepted.

## Control Plane API

Reader routes:

- `GET /api/control/v1/paper/runtime`
- `GET /api/control/v1/paper/orders?limit=<1..200>&offset=<n>`
- `GET /api/control/v1/paper/blocked-scopes`
- `GET /api/control/v1/paper/lifecycle-capability`
- `GET /api/control/v1/components/paper_runtime/health`

The capability response is intentionally small:

```json
{"schema_version":1,"available":true,"paper_only":true}
```

It reports attached composition capability only; it is not a lifecycle-state projection.

Operator-authenticated mutation routes:

- `POST /api/control/v1/paper/start`
- `POST /api/control/v1/paper/stop`
- `POST /api/control/v1/paper/kill-switch/activate`
- `POST /api/control/v1/paper/kill-switch/reset`

The accepted request body is closed:

```json
{"schema_version":1,"request_id":"<bounded-id>"}
```

The reader token cannot mutate. The Control Plane operator token is used only at the local Control Plane boundary; the owner transport has separate server-side credentials and constructs its trusted operator subject after authentication.

## Error semantics

Owner errors are mapped deterministically without raw owner/storage/transport exception text:

- invalid request -> 400;
- PAPER-only violation -> 400;
- idempotency conflict -> 409;
- invalid owner transition -> 422;
- recovery required -> 503, retryable;
- unreachable owner -> 503, retryable;
- owner authentication/configuration failure -> typed 503;
- other bounded owner failure -> typed 503.

On an uncertain/retryable result, the operator must first reread authoritative runtime state and retry the **same request ID** only when the owner semantics say that is safe. The Control Plane does not decide whether a side effect occurred.

## Dashboard

`#/paper` is a native workspace in the existing Dashboard shell. Reader and operator credentials use the same memory-only global controls as the other typed workspaces and are never stored in local/session storage.

The page shows owner runtime/Paper Execution state, paper mode, accepted strategy, market/timeframe/instruments, queue counters, kill-switch state, orders, blocked scopes and explicit position unavailability.

When lifecycle capability is attached and a Control operator credential is present, it exposes:

- Start;
- Stop;
- Activate kill switch;
- Reset / recover.

Every operation requires explicit confirmation. Obvious already-reached native states disable redundant buttons in the browser, but the browser is not authoritative: the owner still performs the final transition/idempotency decision.

A fresh mutation gets a bounded browser-generated request ID. A retryable failure exposes `Retry same idempotency request`, preserving the exact ID. After success the page invalidates and rereads authoritative runtime, capability, orders, blocked scopes and health.

There is no Live toggle.

## CLI

The same adapter is reachable through typed CLI parity:

```text
modular-control paper-lifecycle start --request-id <id>
modular-control paper-lifecycle stop --request-id <id>
modular-control paper-lifecycle kill --request-id <id>
modular-control paper-lifecycle reset --request-id <id>
```

The CLI forwards the exact request identity and prints the bounded owner receipt. It does not add a local idempotency database.

## Acceptance

Before child PR #547 may be accepted into parent Draft PR #371, exact-head checks must prove:

- owner adapter/API/client contract tests;
- all four operation mappings;
- reader denied / operator accepted;
- exact replay preservation without duplicate Control Plane effects;
- error taxonomy and privacy mapping;
- malformed/oversized request and response handling;
- loopback and distinct-credential validation;
- strict TypeScript and browser contract tests;
- explicit confirmation and correct operator/capability disabled states;
- a real-loopback Playwright flow through the accepted `TradingRunnerOperatorLifecycle` and `TradingRunnerLifecycleHttpServer` covering Start, replay, kill, replay, reset and Stop;
- no browser credential persistence;
- no privacy canary or generated credential leakage into UI/log evidence;
- clean-install, formatting, build and relevant repository policy gates.

Owner-native concurrency and process restart/recovery are additionally proven by accepted #381 owner tests and are not reimplemented in the Control Plane.

## Deferred

Authoritative positions remain deferred until their owner contract exists. Structured operational Logs/Events are a separate owner-backed Control UI slice tracked by Issue #521 and must not be implemented here through file tailing or arbitrary paths.
