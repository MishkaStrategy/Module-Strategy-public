# Paper Runtime lifecycle mutation audit — Phase 6b

Status: **owner contract accepted; typed Control UI integration implemented in Draft PR #547 and pending exact-head acceptance**.

Control UI Issue: #517  
Parent Control UI Issue: #370  
Parent Draft PR: #371  
Trading Runner owner dependency: #381  
Accepted owner PR: #465  
Accepted owner exact head: `47524a400ed927cf722551d42dd009c153802e6c`

## Scope

Phase 6b exposes only the four PAPER lifecycle operations accepted by Trading Runner:

- `START`;
- `STOP`;
- `ACTIVATE_KILL_SWITCH`;
- `RESET_KILL_SWITCH`.

The Control Plane does not call raw lifecycle methods directly and does not maintain a lifecycle state machine or idempotency database. Runtime truth continues to come from the existing read-only Paper Runtime projection.

## Accepted owner guarantees

`TradingRunnerOperatorLifecycle` is the authoritative mutation boundary. It owns:

- bounded `request_id` and trusted operator identity;
- durable idempotency records in the existing Trading Runner `RuntimeStateStore`;
- exact successful/failed replay;
- same request ID with different operation/operator conflict;
- process-local concurrency serialization plus the native Trading Runner lifecycle lock;
- fail-closed `IN_PROGRESS` recovery markers;
- owner audit in the existing Trading Runner journal;
- fixed owner-defined kill-switch reason, never a browser-supplied reason;
- PAPER-only enforcement;
- deterministic errors: `INVALID_REQUEST`, `INVALID_TRANSITION`, `RECOVERY_REQUIRED`, `IDEMPOTENCY_CONFLICT`, `OWNER_FAILURE`, `PAPER_ONLY`.

The accepted owner contract also no-ops a newly identified request when the requested authoritative state is already reached, without repeating native effects. Exact request replay returns the original receipt with `replayed=true`.

## Control Plane boundary

The downstream adapter accepts only an HTTP loopback base URL and distinct server-side reader/operator credentials. It forwards an allowlisted operation using a body containing exactly:

```json
{"request_id":"<bounded-id>"}
```

The browser-facing mutation body is also closed and contains only:

```json
{"schema_version":1,"request_id":"<bounded-id>"}
```

Published Control Plane routes:

- `POST /api/control/v1/paper/start`
- `POST /api/control/v1/paper/stop`
- `POST /api/control/v1/paper/kill-switch/activate`
- `POST /api/control/v1/paper/kill-switch/reset`

A reader credential cannot mutate. The Control Plane operator credential is required and is never forwarded as the owner operator identity. The downstream owner constructs the trusted operator subject from its authenticated server-side context.

No browser field exists for operator identity, reason, command, filesystem path, execution mode or arbitrary payload.

## Capability projection

`GET /api/control/v1/paper/lifecycle-capability` reports only whether the owner lifecycle transport is attached and that it is PAPER-only:

```json
{"schema_version":1,"available":true,"paper_only":true}
```

This is composition capability, not runtime state. The existing Paper Runtime projection remains authoritative for current `native_state` and kill-switch state. The legacy `lifecycle_mutations_available=false` field inside the Phase 6a runtime summary is intentionally not repurposed as a second mutation-state source.

## Web and CLI

Dashboard `#/paper` uses the global memory-only Control operator token. It exposes Start, Stop, Activate kill switch and Reset/recover only when the lifecycle owner capability is attached. Every operation requires an explicit confirmation.

The Dashboard generates a bounded request ID for a new operation. If a retryable transport/owner error occurs, the same request ID is retained for `Retry same idempotency request`; browser state never decides whether the side effect happened. After every success it invalidates and rereads the authoritative Paper Runtime projection.

CLI parity is exposed as:

```text
modular-control paper-lifecycle start --request-id <id>
modular-control paper-lifecycle stop --request-id <id>
modular-control paper-lifecycle kill --request-id <id>
modular-control paper-lifecycle reset --request-id <id>
```

The CLI forwards the exact request ID and displays the owner receipt, including replay status and previous/new native state.

## Security and privacy

The integration remains PAPER-only and loopback-only. It adds no Live toggle, private exchange key input, signed exchange request, arbitrary command, shell execution, browser path, free-form kill reason or persistent browser credential storage.

Owner/downstream tokens are redacted by construction and never appear in mutation results. Raw transport exceptions and owner internals are mapped to bounded Control API errors.

## Acceptance evidence required before merge into parent

PR #547 must pass exact-head gates proving:

- Python adapter/API/client tests;
- reader-denied/operator-accepted mutation auth;
- all four operation mappings;
- exact duplicate replay preservation without a Control Plane ledger;
- same-request conflict/error mapping from owner taxonomy;
- malformed and oversized input/response rejection;
- downstream URL/credential fail-closed validation;
- browser reader/operator scope separation;
- explicit confirmation and disabled states;
- real-loopback browser flow through `TradingRunnerOperatorLifecycle` and `TradingRunnerLifecycleHttpServer`;
- Start -> `running` -> exact duplicate replay;
- kill activation -> exact duplicate replay with stable completion receipt -> reset;
- stop -> `stopped`;
- privacy canaries and generated browser credentials absent from UI/log evidence;
- strict TypeScript, Vitest, build, Ruff and clean-install gates.

Restart/recovery and concurrent-duplicate owner semantics remain additionally covered by the accepted #381 owner tests; Control Plane does not reimplement them.
