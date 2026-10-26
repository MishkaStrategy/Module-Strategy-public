# System Acceptance — Production Service Bootstrap V1

Owner Issue: #369  
Bootstrap Issue: #854  
Implementation PR: #855

## Purpose

`ProductionSystemAcceptanceProcess` is the bounded process host for the already-accepted durable System Acceptance owner. It does not add another Market Data, Backtest, Research, Trading or soak state machine.

The deployment/service manager constructs real authoritative owner callbacks/ports in code and supplies only typed bootstrap inputs:

- `SystemAcceptanceBootstrapConfig`;
- `SystemAcceptanceProductionBindings`;
- `CheckpointedSoakExecutor`.

`build_production_system_acceptance_process(...)` converts those deployment bindings into the complete `TrustedProductionPhaseAdapters` composition and then owns only process composition:

1. `SQLiteAcceptanceStore`;
2. the complete typed acceptance executor registry;
3. `SystemAcceptanceOwner`;
4. `SystemAcceptanceDaemon`;
5. staged `PinnedSystemAcceptanceHttpServer` on loopback;
6. `SystemAcceptanceService` lifecycle.

A generic CLI that dynamically imports deployment objects or accepts commands/argv is intentionally not provided. Such a surface would bypass the trusted deployment boundary established by #510/#474.

## Trusted deployment boundary

`SystemAcceptanceBootstrapConfig.identity` is the server-owned `TrustedAcceptanceIdentity`:

```text
full_rc_sha
dataset_id
dataset_digest
strategy_id
strategy_digest
config_digest
```

For the accepted production chain:

- `dataset_digest` is the authoritative registered Market Data `descriptor.dataset_hash`;
- `strategy_digest` is the verified immutable Strategy Factory `StrategyPackage.package_hash`;
- `config_digest` is the deployment-owned fingerprint of the frozen Backtest/runtime acceptance configuration and is not replaced by a narrower Core or candidate configuration hash.

`SystemAcceptanceProductionBindings` contains only typed deployment-owned callbacks/ports required by the accepted production adapters: environment/final verification providers, registered-dataset resolver, frozen Backtest A/B/determinism/recovery bindings, the already-prepared Research owner identity, and read-only PAPER runtime smoke probe. Building the process constructs every adapter against the same trusted identity.

The browser/Control Plane may not provide filesystem paths, storage roots, commands, argv, owner objects, Research repositories, output directories, credentials, live mode, or a full-RC SHA.

The HTTP owner pins `full_rc_sha` from the trusted deployment identity and pins `mode="PAPER"` server-side.

## Private configuration

The state path and reader/operator bearer tokens are deployment-private. They are excluded from `SystemAcceptanceBootstrapConfig.__repr__`. Owner callbacks/ports are likewise excluded from `SystemAcceptanceProductionBindings.__repr__` where they could reveal runtime objects.

Reader and operator tokens must be distinct and contain 16..4096 characters. The HTTP server is loopback-only; the bootstrap deliberately exposes no configurable network host.

The deployment must generate/store tokens outside browser state and must not log them.

## Lifecycle

`ProductionSystemAcceptanceProcess.start()` starts durable owner progress before accepting Control Plane requests. Repeated start while running is idempotent through `SystemAcceptanceService`.

`close()` stops accepting HTTP mutations, stops the owner daemon, joins the daemon with the existing bounded service contract, and closes SQLite. It is safe to call repeatedly.

If process startup fails, the process closes the service and SQLite before re-raising. A closed process is terminal and must not be restarted; construct a new process instance from the same durable state instead.

On process restart, the new `SystemAcceptanceDaemon` calls `SystemAcceptanceOwner.recover_after_restart()` and resumes the same durable acceptance run/attempt semantics from the same SQLite state. Deployment must provide the same trusted candidate identity and the same frozen owner bindings; it must not switch candidate/configuration after an uncertain completion.

## Staged soak policy

The current acceptance plan is deliberately staged.

### Stage 1 — baseline 2h PAPER acceptance

Deployment uses:

```text
allow_soak_24h = false
START.enable_soak_24h = false
START.enable_soak_72h = false
```

`SOAK_2H` is already part of the baseline `AcceptanceRunSpec.phases`; no shell sleep, workflow timer or browser session owns its progress. `CheckpointedSoakExecutor` advances only from persisted healthy owner heartbeats and excludes downtime/stale heartbeat gaps from active soak time.

### Stage 2 — fresh final 24h PAPER acceptance

Do not mutate, retry or extend the completed Stage 1 run into a 24h run.

After Stage 1 is terminally accepted, start a **new acceptance run with a new request identity** using the same accepted candidate/frozen bindings and deployment policy:

```text
allow_soak_24h = true
START.enable_soak_24h = true
START.enable_soak_72h = false
```

The fresh run executes its own complete validation graph and its own baseline `SOAK_2H` before `SOAK_24H`, as defined by the existing owner contract. This is intentionally separate from the completed Stage 1 run; no prior 2h checkpoint is reused as 24h progress.

`enable_soak_72h=true` is rejected by this bootstrap for the current plan even if the browser requests it.

## Operator integration

A deployment/service-manager module constructs the actual owner callbacks/ports and passes them as `SystemAcceptanceProductionBindings`. The bootstrap factory constructs all trusted adapters against the pinned candidate:

```python
process = build_production_system_acceptance_process(
    config=bootstrap_config,
    bindings=production_bindings,
    soak=checkpointed_soak,
)
process.start()
```

The service manager may use `process.running` and `process.address` for local supervision and must call `process.close()` during shutdown.

The process host is not evidence by itself. Runtime truth remains the durable System Acceptance store and authoritative phase-owner proofs. GitHub Actions, browser state, logs and service-manager process state are not substitutes for the owner snapshot.

## Safety invariants

- PAPER-only; no live authorization path.
- Loopback HTTP only.
- Exact full-RC identity pinned server-side.
- All production adapters are constructed against one deployment-owned trusted identity.
- Distinct reader/operator credentials.
- No browser-owned filesystem/command/runtime fields.
- No dynamic command or arbitrary import bootstrap.
- No second Research/Trading/Backtest/soak state machine.
- No GitHub Actions runtime truth.
- No 72h stage in the current plan.
- Restart resumes/reverifies the same durable candidate rather than switching configuration.
