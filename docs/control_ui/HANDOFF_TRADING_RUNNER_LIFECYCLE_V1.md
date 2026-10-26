# Control UI Handoff — Trading Runner Lifecycle V1

Owner Issue: #381  
Owner PR: #465  
Control UI parent: #371

This handoff defines the adapter contract. The final accepted SHA and exact-head CI evidence are recorded in Issue #381 / PR #465 only after the current owner candidate is green.

## Public Python owner contract

```python
from modular_trading_runner import (
    LifecycleMutationError,
    LifecycleMutationErrorCode,
    LifecycleMutationRequest,
    LifecycleMutationResult,
    LifecycleOperation,
    TradingRunnerLifecycleHttpServer,
    TradingRunnerOperatorLifecycle,
    TrustedOperatorIdentity,
)
```

Operations:

```text
START
STOP
ACTIVATE_KILL_SWITCH
RESET_KILL_SWITCH
```

A successful `LifecycleMutationResult` returns:

```text
request_id
operator
operation
previous_state
new_state
config_hash
completed_at
replayed
```

## Persistence

`TradingRunnerOperatorLifecycle` stores request-id records through the existing Trading Runner `RuntimeStateStore` under the runtime-owned lifecycle namespace. Control Plane must not create a second persistence store or lifecycle state machine.

`IN_PROGRESS` is persisted before the native side effect. Terminal success/failure is persisted afterward. Completed duplicates replay the original result. Persisted uncertain `IN_PROGRESS` requests after restart fail closed as `RECOVERY_REQUIRED` and do not execute the side effect again.

## Idempotency and concurrency

- exact duplicate START/STOP/KILL/RESET: replay, no second native side effect;
- same request ID with different operation/operator: `IDEMPOTENCY_CONFLICT`;
- duplicate kill activation does not cancel orders again or rewrite the original activation timestamp/reason;
- native Trading Runner lifecycle lock remains authoritative;
- HTTP deployment uses one `TradingRunnerLifecycleHttpServer`, one owner instance and one dedicated asyncio owner loop per runtime.

Control Plane must **not** instantiate multiple independent `TradingRunnerOperatorLifecycle` writers against the same runtime persistence. If future multi-process writers are required, the owner persistence contract must first gain an authoritative transactional/CAS primitive.

## Audit

The owner appends `operator.lifecycle` to the existing Trading Runner audit journal. Fields are bounded to:

```text
operation
resource
request_id
operator
result
previous_state
new_state
config_hash
replayed
error_code
```

Timestamp is event metadata. No browser reason, bearer token, exchange secret, filesystem path, environment dump, argv/cmdline or raw exception is written.

An `ACCEPTED` audit record is required before the native side effect. Audit failure returns `RECOVERY_REQUIRED` without performing an unaudited mutation.

## Error taxonomy

```text
INVALID_REQUEST
INVALID_TRANSITION
RECOVERY_REQUIRED
IDEMPOTENCY_CONFLICT
OWNER_FAILURE
PAPER_ONLY
```

Raw owner/storage/audit exceptions are suppressed at the public boundary.

## HTTP owner transport

Loopback-only owner endpoint with distinct downstream reader/operator credentials. Mutation authorization accepts only the operator credential.

```text
POST /lifecycle/v1/start
POST /lifecycle/v1/stop
POST /lifecycle/v1/kill-switch/activate
POST /lifecycle/v1/kill-switch/reset
```

Body is exactly:

```json
{"request_id":"bounded-id"}
```

`operator`, `reason`, `command`, `path` and arbitrary fields are rejected. The owner assigns the trusted operator identity. Responses are `Cache-Control: no-store`.

## Control Plane adapter requirements

Control Plane may add typed operator routes only by forwarding to the owner transport. It must:

- keep downstream owner credentials server-side;
- preserve reader/operator browser authorization separation;
- generate/validate bounded request IDs;
- forward no arbitrary payload other than request ID;
- map owner error taxonomy without raw exception text;
- read current runtime state from the existing owner read projection, not from mutation receipts;
- keep all UI controls PAPER-only.

## Dashboard/CLI capability unlocked after acceptance

Runtime workspace may expose:

- Start;
- Stop;
- Activate Kill Switch;
- Reset/Recover Kill Switch.

Each action requires explicit confirmation and displays the request ID/replay result. Disabled states must derive from the authoritative runtime projection and owner error semantics rather than a browser-side state machine.

## Required Control UI E2E

```text
Start -> RUNNING -> duplicate Start -> same result / one native start
Stop -> STOPPED -> duplicate Stop -> one native shutdown
Activate Kill -> duplicate retry -> one authoritative activation / one cancellation
Reset -> RUNNING -> duplicate retry -> one reset
reader token -> mutation denied
operator token -> mutation allowed
uncertain transport retry -> same request ID -> replay or RECOVERY_REQUIRED, never duplicate side effect
owner restart -> persisted request semantics preserved
```
