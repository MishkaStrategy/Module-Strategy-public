# Core paper OMS recovery boundary (#627)

This document records the bounded recovery contract introduced for GAP-004 Issue #627.

## Owner and storage

`modular_core.execution.BrokerSimulator` remains the single authoritative owner for simulated paper orders, portfolio state, deterministic broker identities, and pending-close state. Recovery does **not** create a second OMS, broker, accounting ledger, or database.

The durable state carrier is `BrokerStateSnapshot` schema version `1`. Persistence uses the existing Core `StateRepository` contract; the canonical file-backed implementation is `AtomicJsonStateRepository`. Callers choose the validated repository key and deployment-owned directory.

## Snapshot contents

The snapshot contains only factual state required for deterministic continuation:

- complete `EngineConfig` identity;
- complete `InstrumentSpec` identity;
- portfolio cash and active position;
- complete submitted order history retained by `BrokerSimulator`;
- exact non-terminal pending-order membership;
- the complete used `client_order_id` duplicate fence;
- order, position, and fill sequence counters;
- active-position entry/exit fills needed for remaining quantity, fee, and PnL continuation;
- pending close request state.

Core `Journal`/event history is intentionally excluded from the broker snapshot. `Journal` already owns a separate durable `EventStore` and deterministic event-sequence contract. Clean-host recovery must restore the matching journal/event stream separately rather than duplicating it inside broker state.

## Fail-closed restore

`restore_broker_state()` validates and prepares the entire snapshot before mutating the target owner. Restore requires a pristine `BrokerSimulator` constructed with the exact persisted `EngineConfig` and `InstrumentSpec`.

The restore contract rejects, before applying state:

- unknown/future or partial top-level schema;
- config or instrument identity mismatch;
- duplicate or non-contiguous order identities/sequences;
- deterministic order IDs that disagree with their sequence;
- pending membership inconsistent with order terminal state;
- used client-order IDs inconsistent with complete order history;
- impossible active-position/order/fill relationships;
- active position/fill IDs ahead of persisted sequence counters;
- position quantity inconsistent with persisted entry/exit fills;
- reduce-only pending orders that conflict with the active position;
- pending close without an active position;
- a non-pristine restore target.

The apply phase has a rollback guard for unexpected assignment failure. No audit events, fills, orders, fees, or venue side effects are emitted by restore itself.

## Deterministic continuation

After successful restore, the next broker-generated identities continue from the persisted sequence counters. Previously used client-order IDs remain fenced, including IDs belonging to terminal orders. Replaying the same client-order identity therefore does not create a second order.

## System Recovery boundary

The broker state file must be included in System Recovery alongside the matching durable Core/Paper event/journal state required by the owning runtime. System Recovery manifest hashing and no-overwrite restore provide file-integrity and clean-host identity checks around the Core state repository.

This slice does not authorize destructive pruning until the combined Core OMS + Paper clean-host proof in #627 is accepted and #542 is updated.

## Non-goals

- no Live/private exchange adapter state or credentials;
- no Trading Runner persistence changes;
- no cloud backup provider;
- no second OMS or event journal;
- no direct merge to `main`;
- no pruning/deletion authorization from this document alone.
