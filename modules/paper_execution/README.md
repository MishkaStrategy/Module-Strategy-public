# Paper Execution & Venue Reconciliation

Paper-only execution and venue-reconciliation orchestration over the existing Modular Strategy Core contracts.

## Safety boundary

- `execution_mode` accepts only `paper`.
- No exchange API client, private REST, private WebSocket, request signing, API-key loading, live adapter or live feature flag exists in this package.
- Core remains authoritative for broker simulation, orders, fills and positions; this module does not introduce a second broker, OMS, event bus, accounting ledger or position store.
- The module executes approved plans. It does not generate signals, choose direction, size risk or alter Router stop/target semantics.
- Reversal remains typed-rejected until an explicitly approved close-before-open contract exists.

## Canonical Router boundary

The public Router adapter accepts canonical `DownstreamPlanEnvelope` objects and preserves:

- canonical payload JSON and payload/content/audit hashes;
- validity interval and plan identity;
- ordered string OCC evidence without coercing it into local integer `StateVersions`;
- open, add, reduce and close action semantics;
- upstream position identity and factual position checks for add/reduce/close.

Tampered, expired, stale-OCC, duplicate-OCC, mismatched-position and unsupported reversal inputs fail closed before venue side effects. Identical replay remains idempotent, while an existing plan ID with different content is rejected.

## Execution transaction

The durable acceptance order is:

`validated plan -> execution.intent_recorded -> Core venue side effects -> plan.accepted`

`plan.accepted` is never written before requested venue side effects complete. A venue rejection writes terminal failure evidence. A failed acceptance commit compensates submitted orders and blocks the affected portfolio/instrument scope for reconciliation.

## Market and deferred-protection boundary

Plan acceptance compares the plan market with venue metadata through an explicit compatibility allowlist. Unknown or cross-market combinations fail closed before submission.

Deferred Stop/Target activation requires an injected `VenueMarkProvider`. `VenueMarkSnapshot` contains the exact execution instrument, a positive mark/last price, UTC observation time and a non-blank evidence source. The deterministic paper contract requires `observed_at` to equal the fill or retry event time. Missing, stale, future, invalid or cross-instrument evidence blocks activation. Average fill price is never substituted for current venue evidence.

For Router add-to-position plans, a factual `RouterPositionProvider` confirms the existing Core position identity, side, quantity and observation time. Deferred protection covers the verified baseline position plus factual add fills and survives schema-v4 recovery.

## Synthetic OCO safety

OCO groups are built only from orders actually accepted by Core and are registered before `plan.accepted`.

- A full sibling fill requests cancellation of remaining siblings through Core.
- A sibling is marked canceled only after Core confirms `cancel > 0`.
- Duplicate fill events are idempotent and do not repeat cancellation.
- Cancel failure, missing sibling projection or a late fill/cancel race blocks the scope for reconciliation without recording false canceled state.
- Because the Core paper venue has no amend/replace path, partial-fill residual protection cancels the unsafe sibling and blocks the scope until protection is reconciled instead of risking an over-close.

## Reconciliation and ownership

Order and position reconciliation are fail closed. Safe orphan-projection repair is permitted only for one unique non-terminal order whose portfolio, instrument and intent role are proven by the canonical snapshot identity tag. Caller-supplied scope metadata alone is insufficient.

## Recovery and timestamp invariants

The coordinator implements Core `snapshot_state()` / `restore_state()` and can be captured with Core `ModuleState` helpers. Recovery schema v4 is canonical and SHA-256 attested. It contains plan idempotency, order projections, portfolio/instrument scopes, blocked scopes, orchestration state, local event sequence, last order-event timestamp and an attestation of the injected Core `EventStore`.

Restore is transactional: all records and invariants are validated before coordinator state changes. Schema v1-v3 checkpoints migrate forward. Journal and per-order timestamps must be non-decreasing; stale events are rejected without mutation, including after restart. Restored scopes reject new plans until order and position reconciliation succeeds.

## Verification gates

`.github/workflows/paper-execution-ci.yml` is the permanent component-owned Python 3.12/3.13 gate. It pins the canonical Router contract and runs compileall, Ruff, strict MyPy, paper-only security checks, the complete Paper test suite with branch coverage, deterministic distribution reproducibility and clean-wheel installation.

`.github/workflows/integration-ci.yml` runs the all-modules integration matrix. Coverage commands use Bash `pipefail`, two-decimal precision and `--fail-under=85`; piped output cannot hide a failed threshold.
