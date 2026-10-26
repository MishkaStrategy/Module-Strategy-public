# Modular Risk Supervisor

Fail-closed, PAPER-only system safety owner for runtime admission and continuation risk.

## Ownership boundary

Risk Supervisor owns only the system-level safety verdict, durable halt latches, guard counters, replay identities, and its audit trail. It consumes factual aggregates and source-state identities from existing owners; it does **not** size trades, generate or rewrite plans, maintain orders/fills/positions, calculate portfolio accounting, mutate the Trading Runner lifecycle/kill-switch, or replace Paper Execution/reconciliation state.

The factual evidence contract carries:

- session-start, current, and peak equity;
- aggregate exposure notional (gross leverage is derived from factual exposure/current equity);
- market-data and consumer-progress timestamps;
- reconciliation blocked-scope/loss counts;
- execution attempt/rejection and cancel attempt/failure counts;
- observed mean absolute slippage and fill-quality aggregates;
- checkpoint-integrity status;
- disk-free/resource evidence;
- transport health and observed execution latency;
- immutable source-state IDs for the component snapshots used by the decision.

There is no second portfolio, OMS, broker, Runner lifecycle, or sizing state machine.

## Safety state and guards

The public state contract is `HEALTHY`, `DEGRADED`, `HALT`, or `MANUAL_REVIEW`. Every decision is timestamped in UTC and carries stable guard codes, source-state identities, snapshot identity, and a canonical SHA-256 decision hash.

The configured guard set covers evidence freshness, session loss, peak-to-current drawdown, exposure/notional and gross leverage, market-data staleness, consumer stalls, reconciliation block/loss, rejection rate, cancel failure rate, slippage, fill quality, checkpoint integrity, resource pressure, transport health, and execution latency. Mandatory breaches latch HALT and cannot be cleared merely by presenting a later green snapshot.

A fresh/uninitialized owner, unavailable supervisor, corrupt persisted state, stale evidence, replay identity collision, out-of-order snapshot, or clock anomaly fails closed for new risk.

## Durability, replay, and operator control

The owner persists the latest authoritative factual snapshot when one exists, the latest decision, latched guard codes, monotonic evaluation sequence, per-guard breach counters, snapshot replay records, authenticated operator-command replay records, and append-only audit rows behind a checksum-protected repository payload.

Exact duplicate factual snapshots are idempotent; the same snapshot ID with different content fails closed without mutating accepted state. Restarts restore durable HALT and counters exactly, and `decision(now=...)` re-evaluates time-sensitive freshness so an old permissive snapshot cannot remain permissive after it becomes stale.

Operator `HALT`/`RECOVER` commands are HMAC-authenticated, runtime-bound, UTC-timestamped, replay-safe, durable, and audited. Emergency HALT can be latched and restored even before the first factual snapshot, without fabricating evidence; the resulting current decision remains both operator-halted and uninitialized. Recovery is impossible until a real authoritative snapshot exists and satisfies the configured guards, and a later healthy snapshot alone does not clear a mandatory or operator latch.

A bounded deterministic restart soak exercises 128 consecutive authoritative snapshots with exact duplicate replay checks and owner reconstruction every 16 decisions. It proves monotonic sequence, stable decision hashes, exact replay identity, durable audit state, and zero synthetic breach drift without wall-clock waiting. This component-level deterministic soak is deliberately separate from the later physical 2h/24h system soak tracked by #369.

## PAPER admission boundary

`RiskSupervisedPaperAdmission` is a read-only composition boundary. It asks the authoritative supervisor for the current decision and delegates the unchanged plan to the already-existing owner boundary only when the action is allowed.

- `open_position`, `add_to_position`, and `reverse_position` require an explicit current `HEALTHY` decision;
- missing/throwing/invalid supervisor state fails closed before downstream side effects;
- `reduce_position`, `close_position`, cancel, replacement, and protection-repair actions remain available so HALT cannot trap existing exposure;
- unknown actions fail closed;
- the boundary never toggles Trading Runner's kill-switch and never recalculates guards.

The real PAPER acceptance test composes the existing `TradingRunner -> RiskSupervisedPaperAdmission -> RouterExecutionAdapter -> PaperExecutionCoordinator -> BrokerSimulator` path and proves HALT blocks new exposure before Router OCC/Paper/broker effects while leaving Runner lifecycle and kill-switch ownership unchanged. A second real-broker scenario opens exposure while healthy, moves the owner to HALT, and flattens the factual position through a supported full-quantity reduce-only action while HALT remains latched.

The accepted Paper `close_position` intent/Core contract contradiction found during this E2E is tracked separately as #953 / Draft PR #954; #379 does not hide or absorb that Paper-owner defect.

## PAPER-only boundary

This package contains no live execution adapter, private credentials, private-exchange client, or exchange kill-switch API. Signal Bot/Control Plane/observability may project or notify on the authoritative decision but cannot authorize or recalculate a state transition.
