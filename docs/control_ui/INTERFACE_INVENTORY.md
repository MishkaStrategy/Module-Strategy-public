# Control UI — interface inventory and target architecture

Status: Phase 0 baseline for Issue #370

## Exact repository identity

- Repository: `MishkaStrategy/Module-Strategy`
- Audited development base: `integration/modular-strategy-full-rc`
- Exact audited full-RC head: `3b10b16f14197cf670c572bcc0a75a4c9fc03d5f`
- `main` at audit time: `1029c315c40dc51abb2c075fbb766c53f645242b`
- Validation PR #325 is a non-merge acceptance record. This work does not modify or merge it.
- Control UI owner Issue: #370
- Working branch: `control-ui/unified-control-plane-foundation`

This document records the live interface/control inventory before project-wide UI implementation. It is intentionally additive: existing CLIs, APIs, durable stores and domain state machines remain authoritative until an adapter is proven equivalent.

## Product boundary

The target product has two operator surfaces over one application control layer:

```text
React Dashboard
      │ HTTP / SSE
      ▼
Unified Control Plane
      │
      ├── Market Data adapter
      ├── Backtest adapter
      ├── Research adapter
      ├── Strategy adapter
      ├── Paper Runtime adapter
      ├── Acceptance adapter
      ├── Signal Bot adapter
      └── System / Evidence adapter
```

```text
modular-control CLI
      │
      └──────────────► same service contracts / Control Plane
```

The browser is a projection of durable/server state. It is never the owner of runtime truth. No browser or CLI endpoint may receive an arbitrary shell command, Python expression, script path or `shell=true` request.

The whole Control UI remains paper/research-only and loopback-only by default. Live trading, exchange private keys, signed private requests and real order placement are outside this scope.

## Existing interface inventory

### 1. Market Data Web Console

Location: `web/market-data-console`.

Current stack:

- React 19;
- TypeScript;
- Vite;
- TanStack Query;
- Vitest;
- Playwright with real local FastAPI/Vite processes.

Current operator surfaces include:

- API version and health;
- Dataset Registry list/detail;
- immutable dataset identity and lineage;
- artifacts;
- durable jobs;
- measured progress;
- warnings/errors;
- event timeline and authorized SSE.

Security behavior already worth preserving:

- bearer value is kept only in React memory;
- it is not stored in local/session storage or query keys;
- API and development server use loopback addresses;
- local storage paths are projected as opaque managed identities by the backend.

Current UX/architecture gaps for the project-wide product:

- navigation is Market-Data-only;
- `App.tsx` carries routing, layout and many page components in one file;
- routing is a small custom hash router rather than a reusable project navigation layer;
- Overview shows Market Data API state rather than the whole system;
- status presentation is local to this UI and does not yet define project-wide semantics;
- no Backtest, Strategy, Research, Paper Runtime, Acceptance, Signal Bot or System pages exist;
- no shared project component boundary/design-system package has been formalized.

Decision: **reuse and evolve this application**. Do not replace it with a parallel dashboard.

### 2. Market Data application Control Plane

Location: `modules/market_data_ingestion/src/market_data_application`.

This is the strongest existing implementation pattern and the reference implementation for the unified layer. It already provides:

- transport-neutral application services;
- strict versioned Pydantic contracts;
- FastAPI and Typer over the same application instance;
- canonical request hashing and actor-scoped idempotency;
- durable SQLite jobs created before execution;
- append-only ordered job events;
- measured determinate/indeterminate progress;
- cancellation without conflating cancellation and failure;
- worker leases, fencing and stale-owner recovery;
- restart recovery/reconciliation;
- bounded REST event replay and SSE replay by exclusive event cursor;
- artifacts;
- reader/operator/admin authorization;
- bounded local Control Plane security and audit records;
- loopback-only server policy;
- secret-safe token records and public projections.

The Market Data domain `RunState` remains authoritative for ingestion phases. It must **not** become the global state enum for Backtest, Research or Paper Runtime.

Decision: the unified Control Plane composes/adapts this application; it does not move its Registry, job store or domain execution logic into a new package.

### 3. Backtest Engine

Primary CLI: `modular-backtest` in `src/modular_backtest/cli.py`.

Current commands:

- `validate-data`;
- `run`;
- `inspect`;
- `compare`.

Strengths:

- deterministic engine/result contracts;
- registered and streaming dataset support;
- result inspection and A/B comparison primitives;
- recovery/scalability infrastructure;
- explicit resource-limit failures and structured result directories.

Control gap:

- the current root CLI invokes the engine synchronously and directly;
- there is no project-wide durable Backtest job aggregate/API;
- browser closure/reconnect semantics therefore do not yet exist for a future Backtest page;
- root CLI error/output semantics are not the same contract as Market Data v1.

Decision: retain `modular-backtest` for compatibility. Add a typed Backtest adapter and durable project job orchestration around the existing engine; never make the browser construct shell commands.

### 4. Research Runner

Location: `research/experiment_runner`.

The local scheduler already provides:

- durable SQLite experiment/run state;
- bounded thread/process execution;
- leases, session identity and fencing;
- heartbeat and expired-lease recovery;
- retry/resume policies;
- deterministic artifact verification before success publication;
- cancellation with a publication guard;
- explicit failed, canceled, corrupted and blocked outcomes.

Decision: Research does not need a second scheduler. The Control Plane needs a projection/control adapter over the existing repository/scheduler and must map its states to common external status semantics while retaining Research-specific phase/run detail.

### 5. Strategy Factory / strategy governance

Location: `research/strategy_factory` and `research/strategy_audit_validation`.

Existing control elements include:

- persistent run checkpoints;
- `RunControl.status/cancel/resume`;
- append-only decision log with chain-head consistency checks;
- promotion/admission/governance artifacts;
- canonical candidate/composition identity and audit evidence.

Decision: canonical accepted strategy remains immutable in the UI. Parameter changes create a new candidate/configuration. The Control Plane must not add an endpoint that edits the accepted baseline in place.

### 6. Trading Runner / Paper Runtime

Location: `applications/trading_runner` plus `modules/paper_execution`.

Existing runtime semantics include:

- a strict lifecycle machine covering configuration, initialization, recovery, reconciliation, start, running/degraded, pause, kill-switch, stop, failure and disposal;
- checkpoint and journal infrastructure;
- market validation/queue boundaries;
- paper-only routing and execution integration;
- auditable runtime events.

The lifecycle machine is richer than a generic UI status model and must remain authoritative.

Decision: expose a narrow typed runtime adapter with safe lifecycle actions (`start`, `stop`, later `graceful_restart`/`recover` where the underlying contract proves them). Do not fabricate a Live mode toggle.

### 7. Signal Bot / Telegram

Location: `src/signal_bot`.

Existing control/observability includes:

- orchestrator health;
- SQLite state;
- persisted Telegram update offset;
- allowed/admin chat boundaries;
- notification enable/disable controls;
- signal inspection and control loop recovery behavior.

Decision: UI/API may expose configured/not-configured state, health, bounded recent events and an explicitly safe notification test action. Telegram token values must never be returned, logged, emitted as events or rendered into CI evidence.

### 8. Results, evidence and logs

`modules/results_aggregator` already contains versioned schemas/storage for runs, configurations, datasets, experiments, metrics, artifacts, audit evidence and structured logs. Multiple modules also own authoritative journals/checkpoints/evidence directories.

Decision: the unified UI should query existing registries/journals through typed adapters rather than scan arbitrary filesystem paths supplied by a browser. Artifact/log access must be allowlisted and rooted.

### 9. Acceptance and soak

The repository has substantial executable acceptance, heavy validation, soak/recovery workflows and exact-head CI evidence, but there is not yet one local application-level Acceptance service matching the desired Dashboard lifecycle.

Decision: build an Acceptance adapter/service only after inventorying which checks can run as local durable jobs versus which remain GitHub/CI governance evidence. Browser closure must never terminate a local soak. GitHub validation-only PR semantics must not be exposed as a generic merge/release button.

## Duplication found

The project does **not** primarily suffer from duplicated trading logic. It suffers from duplicated **operator boundaries**:

- several direct CLIs construct module-specific services;
- each module names lifecycle states independently;
- health/status/result/error projections are module-specific;
- durable execution exists in Market Data and Research but not yet as one project-facing job catalog;
- UI status vocabulary is currently local to Market Data;
- logs/evidence live behind multiple module-specific stores and directories.

The correct remediation is adapters plus common contracts, not a rewrite of internal engines.

## Common external status semantics

A global status represents the operator-facing lifecycle class, while `phase` preserves the underlying module state verbatim.

Initial common durable-job states:

```text
QUEUED
STARTING
RUNNING
PAUSING
PAUSED
STOPPING
SUCCEEDED
FAILED
CANCELLED
RECOVERING
BLOCKED
```

Rules:

- `FAILED` is never used for an explicit cancellation;
- `CANCELLED` is terminal;
- adapter-specific states such as Market Data `DOWNLOADING`, Research `CORRUPTED`, Strategy Factory stages and Trading Runner `DEGRADED`/`KILL_SWITCH_ACTIVE` remain available as `phase`/component detail;
- a mapping must be explicit and tested per adapter;
- unknown future domain states fail closed rather than silently being labeled `RUNNING`.

System health is a different axis from job lifecycle. It uses explicit health states and must not be inferred only from the presence of a running job.

## Common event envelope direction

The project event stream should normalize presentation metadata without rewriting source journals. A future versioned envelope should carry at least:

```text
schema_version
event_id / source_cursor
component
resource_type
resource_id
operation
event_type
status
phase
timestamp
correlation_id
request_id
payload
```

Requirements:

- source ordering/cursor identity is preserved;
- replay is bounded;
- reconnect resumes after an exclusive cursor;
- duplicate presentation is suppressed by stable identity;
- payloads are size-bounded and typed/allowlisted;
- no bearer values, credentials, private filesystem roots or raw market payloads are emitted.

## Target package boundary

Phase 1 introduces an additive lightweight package under `applications/control_plane` with no dependency on heavyweight strategy/backtest initialization for simple health/status operations.

Initial responsibilities:

- versioned common contracts;
- component/adapter protocols;
- deterministic adapter registry;
- project health/overview aggregation;
- explicit `PAPER` mode identity;
- safe conversion of adapter failures into bounded operator-facing errors.

It will **not** initially own:

- Market Data Registry/storage/jobs;
- Research scheduler state;
- Trading Runner lifecycle state;
- Backtest engine execution semantics;
- Strategy governance decisions;
- Telegram secrets;
- arbitrary filesystem or process execution.

These remain behind typed adapters.

## Compatibility plan

Keep the following working while the unified UI is introduced:

- `market-data`;
- `market-data-v1`;
- `market-data-api` and existing `/api/v1` behavior;
- `web/market-data-console` current Market Data flows;
- `modular-backtest`;
- `modular-signal-bot`;
- module-specific Research/Strategy Factory entry points;
- existing CI and component contracts.

New project-wide contracts receive their own versioned namespace. Migration is additive until parity tests prove replacement safe.

## UX audit baseline

### Navigation

The Market Data sidebar is clear for one slice but cannot scale as a flat list to the whole product. The unified shell needs grouped domains with persistent PAPER identity and a compact global health summary.

### Information density

Existing pages are appropriately dense and table-oriented. Preserve that direction; avoid decorative trading visuals and large empty cards.

### State visibility

The current console exposes job state/progress/events well. The unified shell should make System, Runtime, active jobs, Acceptance and warnings visible from every primary screen.

### Error handling

Market Data already exposes structured error code/message/correlation ID. Project-wide errors should add an operator action/remediation field without leaking raw internal exceptions by default.

### Responsive behavior

Desktop remains the full operational surface. Mobile monitoring must prioritize Overview, alerts, Paper Runtime, positions, Acceptance and emergency safe stop/recovery actions; complex Backtest configuration can remain desktop-first.

### Accessibility

The existing use of semantic tables, labels and alert roles is a useful base. The unified design system must additionally standardize focus visibility, keyboard navigation, non-color status cues, reduced motion and responsive side navigation.

## TUI decision

Do **not** create a dedicated TUI in Phase 1. A fast structured `modular-control` CLI plus a responsive Web Dashboard cover recovery, SSH, automation and normal operation with less duplicated presentation code. Revisit a TUI only if operational tests demonstrate a recurring interactive terminal workflow that the CLI cannot serve efficiently.

## Implementation sequence

1. Phase 0 — this inventory and contract/UX gap baseline.
2. Phase 1 — common Control Plane contracts, adapter registry and aggregate system health.
3. Phase 2 — refactor the existing Web Console into a reusable Dashboard shell without losing Market Data behavior.
4. Phase 3 — mount Market Data as the first adapter-backed product area and establish compatibility tests.
5. Phase 4 — durable Backtest orchestration/results/A-B comparison.
6. Phase 5 — Strategy/Research governance, experiments and evidence.
7. Phase 6 — Paper Runtime lifecycle/signals/plans/orders/fills/positions/recovery.
8. Phase 7 — local Acceptance/soak orchestration and evidence projection.
9. Phase 8 — `modular-control` command tree over the same services.
10. Phase 9 — Signal Bot, System, Logs and Evidence operational surfaces.
11. Phase 10 — profiling, security, accessibility and real-backend E2E hardening.

## Phase 0 verdict

The repository already contains enough durable and recovery-safe infrastructure to build the requested product without creating a second trading platform. The main architectural task is to establish a small, stable project-facing contract layer and adapters, then evolve the existing Market Data Console into the unified shell.

No cross-module contract break is required for the first implementation slice.