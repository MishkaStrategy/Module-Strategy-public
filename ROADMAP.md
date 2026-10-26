# Module-Strategy Roadmap

**Status as of 2026-10-27:** local preparation only. The original repository remains private and GitHub Actions remain disabled. A clean-root copy is being assembled locally; its destination and any publication are pending. This document is a planning snapshot, not release acceptance.

## Product scope

Preserve the complete platform: all 26 Python distributions and the Web Console, spanning deterministic Core/backtesting, Market Data, strategy modules and plugins, research and audit, Paper Execution, Trading Runner, Control Plane, System Acceptance and Recovery, Observability, and Risk Supervisor.

The strategy product compares three neutral families—`breakout_return`, `fvg_rt` with explicit FVG-retest semantics, and `liquidity_sweep`—under shared lifecycle and evaluation contracts. Signal Bot remains optional and notification-only; it is not a trading or execution owner.

## Selected behavior and ownership

- **Paper portfolio (#933):** one future canonical owner for portfolio, accounting, and admission. It is not operational until its canonical aggregate/writer, identity, capital and risk limits, authoritative order/position/terminal/reconciliation sources, and populated-state migration are selected and reviewed.
- **Strategy Lifecycle (#395):** Lifecycle state is per strategy instance and generation-bound through the Core SQLite reference. A common fence must cover every relevant Paper mutation. Missing identity, durable ownership, migration, or authoritative reconciliation keeps operations fail-closed. No live trading authority follows.
- **Shared portfolio allocation (#396):** deterministic allocation orders requests by priority, then stable `instanceID`. Positions remain independent. Reserve worst-case commitment and costs before dispatch; transfer fills atomically to exposure; retain reservations through cancellation or lost acknowledgement until an authoritative terminal result or reconciled recovery. If capital is insufficient, reject the whole request; do not partially allocate or auto-queue it.
- **Deployment:** Linux deployment and separate mandatory Darwin acceptance are both required. Exact runtime, packaging, provenance, and environment inputs still need to be frozen and verified against the same candidate.

## Ordered roadmap

1. **Finish the local snapshot.** Select and review source content, keep the original private repository unchanged, and retain Actions disabled. A new-root copy is a new source identity.
2. **Re-establish software evidence.** Validate all 26 distributions, Web Console build inputs, package data, schemas, examples, and lock profiles. Run local synthetic software checks against the selected source during preparation and repeat affected checks after source changes. CI and owner-data execution require their separate gates. Existing CI, grants, and receipts do not qualify a new root history.
3. **Resolve product owner inputs.** Build the selected #933 canonical accounting owner on the approved Core-owned SQLite design, with explicit adapters for identity, capital/risk, order/position/terminal/reconciliation and migration. Keep operational adapters disabled until real authority and configuration are supplied. Integrate #395 generation and mutation fences and #396 shared-pool allocation with that single owner.
4. **Keep research admission closed.** #1190 CP-10 STOP remains in force. #250 is planning-only; any successor must be approved before a separate bounded run and fresh one-shot authorization.
5. **Complete system and release acceptance.** Close recovery, observability, independent risk supervision, drift, security, and exact-candidate qualification; deploy on Linux and complete mandatory Darwin acceptance. No release claim before all required evidence is bound to the final snapshot.

## Closed until separately authorized

SEARCH, HOLDOUT, PAPER execution, soaks, CI/workflow runs, deployment, release, and publication remain closed. Preparation or design does not grant operational authority.
