# Trading Runner Operator Lifecycle Contract V1

Issue #381 defines the owner-native mutation boundary required before Control UI can expose PAPER runtime lifecycle controls.

## Ownership and scope

`TradingRunnerOperatorLifecycle` belongs to `applications/trading_runner` and delegates lifecycle effects to the existing authoritative `TradingRunner` methods. It does not maintain a second lifecycle state machine, order state, portfolio state, or exchange state.

The boundary is PAPER-only. The underlying `TradingRunner` already rejects non-paper configuration and the operator boundary repeats that fail-closed invariant. There is no Live toggle, private exchange API, credential input, arbitrary command, path, or shell surface.

Supported operations are exactly:

- `START`
- `STOP`
- `ACTIVATE_KILL_SWITCH`
- `RESET_KILL_SWITCH`

Kill activation uses the owner-defined fixed reason `operator_control`. A browser/API caller cannot supply free-form reason or message content.

## Public Python contract

```python
from modular_trading_runner import (
    LifecycleMutationError,
    LifecycleMutationErrorCode,
    LifecycleMutationRequest,
    LifecycleMutationResult,
    LifecycleOperation,
    TradingRunnerOperatorLifecycle,
    TrustedOperatorIdentity,
)
```

`request_id` is a bounded 1-128 character identifier using only ASCII alphanumeric characters plus `._:-`. Trusted operator subjects are bounded to 1-128 characters using ASCII alphanumeric characters plus `._:@-`.

The runtime resource identity used by this boundary is independently restricted to 1-128 ASCII alphanumeric/`._:-` characters. A path-like or free-form `runtime_id` is rejected before it can become an idempotency-store key or operator-audit resource.

`TrustedOperatorIdentity` is intentionally not an authentication token. It is the identity produced by an already-authenticated local owner/Control Plane boundary. Reader/operator authorization remains the responsibility of that trusted adapter; raw browser bearer values must never be copied into this object or into owner audit.

## Durable idempotency

The owner uses the same injected `RuntimeStateStore` already owned by Trading Runner, under a namespaced metadata key:

```text
<runtime_id>:operator-lifecycle:v1
```

The record store contains idempotency metadata only. Native lifecycle truth remains in `TradingRunner`.

Each first request is persisted as `IN_PROGRESS` **before** the native method is invoked. Terminal `SUCCEEDED` or `FAILED` outcome is persisted after the owner call completes.

Exact replay rules:

- same `request_id`, operator and operation + `SUCCEEDED` -> return the original accepted result with `replayed=true`; no native side effect;
- same identity + `FAILED` -> return the same deterministic error class; no native side effect;
- same `request_id` reused with a different operation or trusted operator -> `IDEMPOTENCY_CONFLICT`; no native side effect;
- existing `IN_PROGRESS` -> `RECOVERY_REQUIRED`; no native side effect.

The `IN_PROGRESS` rule is deliberately conservative. If a process or storage failure occurs after a side effect but before terminal result persistence, the owner cannot prove whether the effect happened. It therefore refuses to guess or repeat the effect. Recovery/operator inspection must establish authoritative native state before a new request is issued.

The V1 metadata store is bounded at 2048 records and never evicts prior request identities. Capacity exhaustion fails closed with `OWNER_FAILURE` rather than forgetting an old idempotency key and risking a repeated side effect. A future owner-maintenance contract may archive records only if it preserves equivalent replay/conflict guarantees.

## Duplicate/native state behavior

Beyond exact request replay, owner-native no-op guards prevent duplicate state effects from a newly generated request where the requested state is already authoritative:

- `START` while already `RUNNING` does not invoke `TradingRunner.start()` again;
- `STOP` while `STOPPED`/`DISPOSED` does not invoke shutdown again;
- `ACTIVATE_KILL_SWITCH` while already active does not invoke cancellation again and therefore does not rewrite the original activation timestamp or reason;
- `RESET_KILL_SWITCH` while already inactive does not invoke reconciliation/reset again.

Concurrent requests are serialized by the operator boundary in the same owner process and each native lifecycle call remains serialized by `TradingRunner._lifecycle_lock`.

V1 assumes one authoritative Trading Runner owner process for a runtime. `RuntimeStateStore` does not expose a cross-process compare-and-set/lease primitive, so multiple independent mutation writers are unsupported and must not be created by Control Plane.

## Result

A successful `LifecycleMutationResult` contains only bounded/safe owner metadata:

- request ID;
- trusted operator subject;
- operation;
- runtime ID;
- previous native state;
- new native state;
- immutable configuration hash;
- completion timestamp;
- replay flag.

A replay returns the originally persisted previous/new states and completion timestamp. Current runtime state must still be read from the authoritative runtime projection; replay result is not a replacement runtime state owner.

## Deterministic error taxonomy

- `INVALID_REQUEST` — malformed/badly typed/bounded request contract;
- `INVALID_TRANSITION` — owner-native transition/precondition is rejected;
- `RECOVERY_REQUIRED` — uncertain prior result, stale/corrupt idempotency metadata, unavailable persistence/audit, or runtime recovery/failure state requiring inspection;
- `IDEMPOTENCY_CONFLICT` — request ID reused for a different operation/operator;
- `OWNER_FAILURE` — deterministic owner/internal failure outside a transition conflict;
- `PAPER_ONLY` — non-paper owner configuration is rejected.

Raw owner/storage/audit exception text is not persisted or exposed by the public mutation error. Wrapped exception causes are suppressed so a downstream traceback cannot reveal a private path, credential-bearing URL, token, or other internal detail through the mutation boundary.

## Audit

Every accepted/replayed/conflicted owner request writes to the existing owner `AuditJournal` using event type `operator.lifecycle` and request ID as correlation ID.

The structured payload is limited to:

- `operation`
- `resource`
- `request_id`
- `operator`
- `result`
- `previous_state`
- `new_state`
- `config_hash`
- `replayed`
- optional `error_code`

Timestamp remains journal metadata. The operator payload contains no token, exchange secret, chat ID, filesystem path, environment dump, argv/cmdline, raw stack trace, or browser-supplied reason.

If the owner audit journal is unavailable, the boundary returns `RECOVERY_REQUIRED`. The first `ACCEPTED` audit is required before any native side effect: an audit failure at that point leaves the persisted request fail-closed and does not start/stop/reset/cancel anything without owner attribution.

Existing native lifecycle events continue to be emitted by `TradingRunner`; the operator audit is attribution/idempotency evidence, not a second lifecycle journal.

## Recovery semantics

A completed request survives owner reconstruction when the same durable `RuntimeStateStore` is supplied and replays without executing the native method again.

An uncertain persisted `IN_PROGRESS` request survives restart as a fail-closed recovery marker. The same request ID cannot be used to retry the side effect automatically.

A stale configuration hash or malformed idempotency record prevents operator boundary construction/use with `RECOVERY_REQUIRED`; it is never silently ignored or migrated during a read/mutation attempt.

## Control UI handoff boundary

After owner acceptance, Control UI may add a typed adapter that:

1. authenticates on loopback using the existing Control Plane token model;
2. requires operator scope for mutation and rejects reader scope;
3. constructs `TrustedOperatorIdentity` from trusted auth context, never from arbitrary request JSON;
4. accepts only the allowlisted operation and bounded `request_id`;
5. maps the owner error taxonomy without exposing internal exception text;
6. keeps `127.0.0.1` default bind and PAPER-only capability reporting;
7. confirms destructive Kill/Reset operations in Web/CLI without making browser state authoritative.

The Control Plane must not add its own idempotency database or lifecycle state machine.
