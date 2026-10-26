# Unified Observability v1

Status: product integration contract for Issue #378.

Unified Observability is the read-only PAPER operational truth surface consumed by the Control Plane Web Dashboard and `modular-control` CLI. Component owners remain authoritative for execution, lifecycle, risk, reconciliation, strategy admission, persistence, recovery, and notification delivery.

Observability does not create a second runtime state machine, trading ledger, scheduler, risk engine, persistence migration, or browser-owned truth.

## HTTP endpoint

```text
GET /api/control/v1/observability/overview
```

The endpoint uses the existing Control Plane reader authorization boundary and returns one versioned `UnifiedObservabilitySnapshot`:

- fixed `mode=PAPER`;
- one `UnifiedOperationalOverview`;
- the exact sorted 12-component set;
- backend-owned active alerts from the single Observability `AlertEngine`.

The component set is:

```text
backtest_research
dataset_registry
market_data
paper_execution
portfolio_position
risk_sizing
risk_supervisor
signal_bot
strategy_audit_factory
system_acceptance_recovery
system_resources
trading_runner
```

Health values are:

```text
healthy
degraded
blocked
unavailable
```

Global health is deterministic and conservative:

```text
unavailable > blocked > degraded > healthy
```

A green whole-system summary is therefore impossible while any required component is unavailable, blocked, or degraded.

## Truth and freshness boundary

A component may be `healthy` only when the attached source contract proves the health axis represented by that component. Readability alone is not sufficient.

Examples:

- a reachable Market Data API and readable Registry do **not** prove market-data freshness, last completed event, or gap state; without a native freshness source, `market_data` is `unavailable`;
- a readable Backtest job store and readable Research experiment store do **not** prove that work is not stuck; without a native progress/stuck source, `backtest_research` is `unavailable`;
- a readable Strategy Factory/Audit store does **not** prove accepted strategy identity or admission integrity; without a native admission source, `strategy_audit_factory` is `unavailable`;
- missing Risk Supervisor, Risk Sizing, Portfolio/Position, Trading Runner, or Paper Execution authoritative telemetry remains `unavailable` rather than being inferred from adjacent components.

The Dataset Registry projection may use the accepted Market Data registry-health fact because that legacy contract explicitly reports Registry status. Signal Bot may use its accepted delivery-health projection. System resource collection reports measurement availability only; safety policy remains owned by authoritative risk/capacity owners.

Native #378 sources always take precedence over legacy derived projections.

## Freshness and stuck semantics

The backend `modular_observability` package owns freshness/stuck policy and deterministic evaluation. Control Plane and browser code do not recalculate thresholds.

Each component view includes:

- source `observed_at` when authoritative evidence exists;
- `evaluated_at`;
- `age_seconds`;
- `stale`;
- optional `progress_at` / `progress_age_seconds`;
- `stuck`;
- opaque SHA-256 `state_ref`;
- stable `reason_codes`;
- optional bounded public correlation identity;
- bounded public scalar details;
- immutable observation hash.

All timestamps must be timezone-aware. Control Plane normalizes to UTC. The Web contract rejects timezone-naive timestamp strings.

## Alerts

Alert policy is owned by the backend `AlertEngine`. Web and CLI never derive severity, deduplication, cooldown, or remediation from component state.

The transport exposes current active alert snapshots containing:

- stable condition `alert_id`;
- component;
- `warning` or `critical` severity;
- stable reason codes;
- backend-selected `next_action`;
- evaluation timestamp;
- current source observation hash;
- optional public correlation ID.

Current active-alert state is distinct from immutable alert transition events. Updating a current view does not rewrite a historical transition event identity.

## Privacy and transport bounds

The public contract is deliberately narrow:

- no bearer tokens, credentials, private host paths, raw topology, arbitrary owner payloads, or nested private state;
- state identities are opaque SHA-256 digests;
- at most 32 public detail fields;
- detail strings are bounded and reject control characters;
- integer details must remain within the shared JSON safe-integer boundary so Python and JavaScript represent the same exact value;
- non-finite numbers are rejected;
- response shapes reject unknown fields.

Owner summary/detail fields are never copied wholesale into Unified Observability.

## Optional backend composition

`modular-control-plane` keeps the base observability backend optional so importing or clean-installing the lightweight Control Plane package does not initialize the runtime observability stack.

When `modular_observability` is installed, the standard `create_server()` composition creates one process-local read-only `ObservabilityControlService` and one transient backend `AlertEngine`.

When the optional backend is not installed, the Control Plane still starts. The unified endpoint returns typed fail-closed `503 CONTROL_OBSERVABILITY_NOT_ATTACHED`. Other backend contract/composition failures are not swallowed as “optional”; they remain startup/read failures.

## CLI

```bash
modular-control system observability
modular-control system observability --json
```

Exit codes:

| Code | Meaning |
| ---: | --- |
| `0` | unified health is `healthy` |
| `2` | unified health is `degraded` |
| `3` | unified health is `blocked` or `unavailable` |
| `4` | transport/configuration/contract error |

The CLI prints backend-provided reason codes and `next_action` values. It does not recreate alert policy.

## Web Dashboard

The Overview page consumes Unified Observability for:

- whole-system health;
- component health badges;
- Risk Supervisor / Risk Sizing / Position visibility;
- Market Data and Strategy/Audit health;
- Backtest/Research health separately from durable Backtest job lifecycle;
- current operator alerts and backend remediation actions.

Legacy Control Plane `/health` reads remain only for feature-specific detail that has not migrated to the unified contract. If Unified Observability is unavailable, the Dashboard does not use legacy green state to render a global healthy summary.

## Acceptance

The real loopback browser workflow installs the accepted observability backend, starts the real Control Plane and Market Data services, and verifies both product surfaces against the same local endpoint:

1. authenticated `GET /api/control/v1/observability/overview` becomes ready;
2. real `modular-control system observability --json` returns a valid PAPER snapshot and the expected non-zero fail-closed exit when required owners are intentionally absent;
3. Playwright reads the same endpoint through the Dashboard and verifies unavailable owner surfaces are shown as unavailable, not green;
4. browser storage remains empty and generated local tokens are absent from evidence logs.

The final project-wide 2h/24h physical soak is outside this integration and remains owned by the separate system acceptance workstream.
