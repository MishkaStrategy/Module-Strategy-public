# Control UI documentation index

This directory is the documentation surface for Issue #370 / Draft PR #371.

GitHub code, exact-head CI and accepted owner contracts are authoritative. Historical audit documents remain useful context, but a document describing a formerly blocked phase must not override a later accepted owner contract or current implementation.

## Start here

### Operations and terminal control

- [`OPERATIONS_RUNBOOK_V1.md`](OPERATIONS_RUNBOOK_V1.md) — current local/PAPER startup, operator commands, restart checks and fail-closed boundaries.
- [`TERMINAL_CONTROL_V1.md`](TERMINAL_CONTROL_V1.md) — current `modular-control` read and accepted owner-mutation surface.

### Security / performance / accessibility / observability

- [`SECURITY_HARDENING_V1.md`](SECURITY_HARDENING_V1.md)
- [`PERFORMANCE_BASELINE.md`](PERFORMANCE_BASELINE.md)
- [`ACCESSIBILITY_HARDENING_V1.md`](ACCESSIBILITY_HARDENING_V1.md)
- [`STRUCTURED_OPERATIONAL_EVENTS_V1.md`](STRUCTURED_OPERATIONAL_EVENTS_V1.md)

Historical blocker audits such as `PAPER_RUNTIME_MUTATION_AUDIT.md`, `ACCEPTANCE_CONTROL_AUDIT.md` and `LOGS_CONTROL_AUDIT.md` record why the Control UI originally refused to fabricate owner state. Their blockers have since been resolved by accepted owner-native contracts.

## Component control contracts

- [`BACKTEST_CONTROL_V1.md`](BACKTEST_CONTROL_V1.md)
- [`STRATEGY_CONTROL_V1.md`](STRATEGY_CONTROL_V1.md)
- [`RESEARCH_CONTROL_V1.md`](RESEARCH_CONTROL_V1.md)
- [`EVIDENCE_CONTROL_V1.md`](EVIDENCE_CONTROL_V1.md)
- [`PAPER_RUNTIME_CONTROL_V1.md`](PAPER_RUNTIME_CONTROL_V1.md)
- [`MARKET_DATA_CONTROL_V1.md`](MARKET_DATA_CONTROL_V1.md)
- [`SYSTEM_CONTROL_V1.md`](SYSTEM_CONTROL_V1.md)
- [`NOTIFICATIONS_CONTROL_V1.md`](NOTIFICATIONS_CONTROL_V1.md)

`CONTROL_PLANE_V1.md` and `INTERFACE_INVENTORY.md` describe the earlier foundation/inventory and are not the sole current operational source.

## Current accepted high-level boundaries

The Control UI remains:

- PAPER/local-only;
- loopback-only for accepted HTTP boundaries;
- owner-backed rather than a replacement state machine;
- browser-memory-only for Control/Market Data/operator token inputs;
- bearer-only at the browser transport boundary, without ambient cookies or authenticated response caching;
- unable to execute arbitrary shell commands or read arbitrary filesystem/log paths;
- unable to enable Live/private-exchange/signed-private-request/real-order-placement capability.

## Current accepted surfaces

Dashboard and `modular-control` consume the same typed Control Plane for:

- system/component health and Operational Overview;
- System Resources (CPU/RAM/disk/uptime plus bounded Control Plane process aggregates);
- Market Data datasets/artifacts/jobs;
- Strategy Factory governance;
- durable Backtest control/progress/results/A-B comparison;
- Research Runner read projections;
- immutable Evidence/provenance inspection;
- PAPER runtime/orders/reconciliation plus owner-native idempotent Start/Stop/Kill/Reset;
- durable System Acceptance/soak status plus Start/Stop/Cancel/Retry;
- Signal Bot bounded status plus fixed owner-defined idempotent test notification;
- structured sanitized Trading Runner operational events with owner cursors.

Former owner blockers #381, #382, #383 and #391 are accepted and integrated. They must not be listed as current blockers.

## Intentionally unavailable / future owner-backed work

Do not improvise these in the Control Plane:

- **Positions** remain unprojected until an authoritative position-owner projection exists.
- **Unified storage capacity forecast** remains pending the #390 → #370/#371 integration slice. Existing measured bounds and unknown physical watermarks must be preserved fail-closed; do not invent percentages or deletion authority.
- **System Recovery operator UI/CLI** remains pending an accepted complete owner contract from #377; the Control UI must not create a second backup/restore state machine.
- **Separate interactive TUI** remains deferred until measured operational need exists; Web + scriptable CLI are the accepted interfaces.

## Acceptance terminology

Use precise labels:

- **backend/CLI accepted** only after exact-head Control Plane Python 3.12 + 3.13 foundation gates pass;
- **clean packaging accepted** only after exact-head isolated-wheel Python 3.12 + 3.13 gates pass;
- **static Web accepted** only after production dependency audit, browser security boundary, strict TypeScript, Vitest and production build pass;
- **full browser accepted** only after real loopback Playwright flows pass;
- **owner mutation accepted** only when the authoritative owner contract and mutation E2E are green.

## GitHub governance

- work stays on Control UI child branches / Draft PRs and the parent Draft PR #371 until accepted;
- `main` is not edited directly;
- validation-only PR #325 must never be merged;
- parent #371 is not merged without a separate HQ/integration decision;
- cross-module dependencies are handed off to HQ #373 rather than reimplemented locally.
