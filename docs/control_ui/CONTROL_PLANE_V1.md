# Modular Strategy Control Plane v1

Status: foundation contract for Issue #370 / Draft PR #371.

This document describes the additive local Control Plane introduced for the unified Dashboard and terminal management surfaces. It is intentionally read-only in the current slice. Typed mutation endpoints are added only when an authoritative module adapter has proven idempotency, audit and recovery behavior.

## Safety boundary

The Control Plane is:

- `PAPER` only;
- loopback-only by default;
- a projection/orchestration layer, not a trading engine;
- unable to receive arbitrary shell commands or script paths;
- unable to place real exchange orders or use private exchange credentials;
- designed so browser state is never authoritative runtime state.

`modular-control-api` rejects non-loopback bind hosts. Remote access/TLS/reverse-proxy/trusted-proxy authentication is a separate deployment scope and is not enabled by this package.

## Processes

The initial runtime topology is:

```text
Browser / modular-control
          │
          ▼
modular-control-api :8765
          │
          └── Market Data health adapter
                    │ loopback HTTP
                    ▼
          market-data-api :8000
```

The Market Data adapter intentionally reads the already-running Market Data API instead of creating a second Market Data application/worker. This avoids duplicate executors, recovery loops and lease ownership.

Other project components are marked required but remain `UNAVAILABLE` with `reason=adapter_not_registered` until their typed adapters are implemented. This is deliberate fail-closed behavior, not a fake healthy placeholder.

## Installation

From the repository:

```bash
python -m pip install -e 'applications/control_plane[interface]'
```

The lightweight contract/client package itself depends only on Pydantic. FastAPI/httpx/uvicorn are kept in the `interface` extra so importing `modular_control_plane` for shared contracts does not initialize the web or component stacks.

## Configuration

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODULAR_CONTROL_HOST` | `127.0.0.1` | Control API bind host; loopback values only. |
| `MODULAR_CONTROL_PORT` | `8765` | Control API TCP port. |
| `MODULAR_CONTROL_MARKET_DATA_URL` | `http://127.0.0.1:8000` | Existing Market Data API base URL; loopback HTTP only. |
| `MODULAR_CONTROL_MARKET_DATA_READER_TOKEN` | unset | Server-side reader credential for Market Data. Never returned by the Control API. |
| `MODULAR_CONTROL_READER_TOKEN` | unset | Optional bearer token protecting Control API read endpoints and used by `modular-control`. |
| `MODULAR_CONTROL_REQUEST_TIMEOUT_SECONDS` | `2.0` | Bounded Market Data health request timeout. |
| `MODULAR_CONTROL_LOG_LEVEL` | `info` | Uvicorn log level. |
| `MODULAR_CONTROL_URL` | `http://127.0.0.1:8765` | Base URL used by `modular-control`. |
| `MODULAR_CONTROL_TIMEOUT_SECONDS` | `2.0` | CLI monitoring timeout. |

Secrets must be supplied through the process environment or another deployment secret mechanism. Do not place them in Git, URLs, screenshots, evidence or browser storage.

## Start and validate

Validate configuration without starting a server:

```bash
modular-control-api --check
```

The output reports only configuration state, for example token `configured` booleans. It never prints bearer values.

Start the local server:

```bash
modular-control-api
```

The default bind is `127.0.0.1:8765`. A request to bind `0.0.0.0`, LAN addresses or hostnames is rejected.

## Read-only HTTP API

All current endpoints are under `/api/control/v1`.

### Version

```text
GET /api/control/v1/version
```

Returns the contract version, fixed `PAPER` mode and `remote_access_allowed=false`.

### Whole-system health

```text
GET /api/control/v1/health
```

Returns a `SystemOverview` containing:

- global health status;
- checked timestamp;
- fixed `PAPER` mode;
- sorted component health projections.

### Component health

```text
GET /api/control/v1/components/{component}/health
```

Known component identifiers:

```text
market_data
backtest
research
strategy
paper_runtime
acceptance
signal_bot
evidence
```

Unknown component identities return a typed actionable 404. Internal component failures are projected as bounded safe errors; raw exception messages are not returned.

## Common health semantics

Health is separate from durable job lifecycle.

```text
HEALTHY
DEGRADED
UNAVAILABLE
```

The unified health projection does not overwrite a component's native lifecycle/state machine. Adapter-specific state belongs in component detail/phase contracts.

## Common durable job semantics

The foundation contract reserves:

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

`FAILED` and `CANCELLED` are distinct terminal outcomes. Future adapters must map native states explicitly and fail closed on unknown states.

## Terminal interface

Current commands:

```bash
modular-control status
modular-control status --json
modular-control health
modular-control health market_data
modular-control health paper_runtime --json
```

Exit codes:

| Code | Meaning |
| ---: | --- |
| `0` | requested health is `HEALTHY` |
| `2` | requested health is `DEGRADED` |
| `3` | requested health is `UNAVAILABLE` |
| `4` | transport/configuration/API error |

The monitoring CLI is intentionally implemented with a lightweight standard-library HTTP client. `status` does not import FastAPI, Market Data, Backtest, Research, Trading Runner, Signal Bot or Strategy Factory.

## Dashboard integration

The existing `web/market-data-console` now acts as the base for the unified Dashboard shell.

During Phase 2/3 migration:

- `/overview` and `/system` read the new Control Plane;
- Market Data `/datasets`, `/artifacts` and `/jobs` retain their existing API/SSE implementation inside the unified product shell;
- future module routes display their real adapter health and explicitly state `Control contract pending` when not integrated;
- no fake start/stop buttons are shown for unimplemented mutations;
- Control and Market Data reader values remain separate in-memory browser inputs during the compatibility phase.

Vite proxies:

```text
/api/control/*  -> 127.0.0.1:${MODULAR_CONTROL_PORT:-8765}
/api/*          -> 127.0.0.1:${MDI_E2E_API_PORT:-8000}
```

Both remain loopback-only.

## Troubleshooting

### Dashboard says `CONTROL_PLANE_UNAVAILABLE`

1. Run `modular-control-api --check`.
2. Start `modular-control-api` on the configured loopback port.
3. Run `modular-control status --json`.
4. If reader authentication is enabled, ensure the same `MODULAR_CONTROL_READER_TOKEN` is supplied to the CLI or entered into the Dashboard's in-memory Control reader field.

### Market Data is `UNAVAILABLE`

1. Verify `market-data-api` is running.
2. Verify `MODULAR_CONTROL_MARKET_DATA_URL` points to its loopback port.
3. If Market Data reader authentication is enabled, configure `MODULAR_CONTROL_MARKET_DATA_READER_TOKEN` server-side.
4. Read Market Data API logs. The Control Plane intentionally does not echo response bodies or secret-bearing transport errors.

### Other components say `adapter_not_registered`

This is expected until their typed adapter is implemented. The Dashboard must not infer readiness or offer mutations from module presence alone.

## Current non-goals

This foundation does not yet provide:

- Backtest start/cancel/results orchestration;
- Paper Runtime start/stop/restart/recover;
- Research experiment mutations;
- Strategy candidate creation/promotion;
- Acceptance/soak mutations;
- Telegram notification mutation;
- unified SSE/event journal;
- remote deployment/authentication;
- live trading.

Those are subsequent phases of Issue #370 and must reuse authoritative component state instead of duplicating it.
