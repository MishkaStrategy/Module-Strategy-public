# Modular Trading Runner

Deterministic paper-only runtime orchestration for the Modular Strategy executable chain.

## Scope

Trading Runner owns lifecycle, dependency-plan execution, validated market-event ingestion, Router plan dispatch, recovery/reconciliation gates, state checkpoints, failure supervision and controlled shutdown.

It does **not**:

- contain entry, exit, sizing, stop-loss or take-profit rules;
- create or modify `ExecutableTradePlan` content;
- implement order, fill, position or portfolio state machines;
- load private exchange credentials;
- expose a live-execution adapter or execution mode.

The only accepted execution mode is `paper`.

## Durable owner stores

Trading Runner uses existing Core persistence formats rather than defining a second storage engine. `TradingRunnerDurableStores.open(owner_root)` provides the canonical owner-local layout:

- `state/` — one `AtomicJsonStateRepository` shared by runtime state (`<runtime_id>`), operator-lifecycle idempotency (`<runtime_id>:operator-lifecycle:v1`) and `RuntimeCheckpointCoordinator` (`trading-runner-checkpoint:<run_id>`);
- `journal/events.jsonl` — one `JsonLinesEventStore` with fsync enabled and normal truncated-tail auto-repair disabled so ordinary recovery verification fails closed.

The owner directory layout and journal filename are fixed. State filenames remain the existing Core state-key mapping; Core rejects path separators and traversal keys, so validated runtime/checkpoint/operator keys cannot select child directories. The fixed `state/` directory and journal file reject symbolic-link substitution. Existing Core size/identity checks remain authoritative.

A successful `TradingRunner.start()` durably records the factual terminal startup state before the `runtime.started` audit event. If a later startup audit write fails, the rollback path compensates the already-persisted runtime state to `failed` on a best-effort basis, avoiding a known-success durable record for a runtime that was rolled back.

System Recovery clean-host evidence for GAP-004 treats the Trading durable set as four linked owner objects: runtime state, operator lifecycle state, runtime checkpoint and hash-chained journal. Recovery verifies checkpoint integrity and journal attestation before applying checkpoint state, preserves kill-switch state, and replays completed operator mutations without duplicate owner side effects.

This does **not** claim recovery of independent Core broker/OMS/venue/portfolio/order state and does not by itself authorize backup-dependent pruning.

`TradingRunnerProvisioningFactory` is the one-shot trusted-deployment boundary for the
**first** durable PAPER owner state. It requires a fixed config, owner root, run identity
and code-owned components factory. It starts and stops one real `TradingRunner`, requires
the canonical `RuntimeCheckpointCoordinator` and `RuntimeRecoveryCoordinator`, verifies
the resulting stopped runtime/checkpoint/hash-chain state, then exercises a real restart
through `TradingRunnerBootstrapFactory`. Only after that restart succeeds does it publish
the strict durable identity manifest. Existing or partial owner roots fail closed and are
never overwritten automatically. The provisioner is a typed Python API, not a generic
CLI, and it does not accept persisted module/class/command names.

`TradingRunnerBootstrapFactory` is the narrow restart boundary for an already-created
owner. It binds a fixed PAPER config and durable identity manifest to the existing
runtime state and hash-chain journal, then asks a deployment-owned typed components
factory for concrete code objects. Durable JSON never selects modules/classes, and the
returned `TradingRunnerOperatorLifecycle` wraps the single reconstructed runner.

## Integration status

Trading Runner is integrated into the canonical staged v0.6 checkpoint:

- branch: `integration/modular-strategy-v0.6.0-rc`;
- exact checkpoint: `bc49db40726ec625d456c6c37265ef63b1c74b75`;
- canonical Draft PR: #168.

Exact post-merge evidence on that checkpoint:

- Trading Runner CI — run `30215827063`: success;
- v0.6 executable acceptance — run `30215827042`: success on Python 3.12 and 3.13 through every executable-chain step.

The implementation is a clean-room reconstruction built from the accepted v0.6 integration contracts. It is not source- or history-identical to the unavailable historical Trading Runner head `513726d`.

## Release boundary

This status means the deterministic **paper-only staged integration** is complete. It does not authorize or claim:

- live exchange connectivity;
- private credentials or request signing;
- production deployment;
- stable package publication, tag or GitHub Release.

Real-venue recovery, kill-switch validation, long-duration staging soak, operations runbooks and release governance remain project-level production/live blockers.
