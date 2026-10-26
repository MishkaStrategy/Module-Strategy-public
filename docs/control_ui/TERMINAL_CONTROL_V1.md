# Terminal Control v1 — current parity

Status: implemented on Draft PR #371. `modular-control` is the scriptable terminal interface to the same loopback typed Control Plane used by the React Dashboard.

## Boundary

The CLI must not initialize owner engines merely to inspect or mutate them. It talks to the loopback Control Plane and uses accepted owner-native contracts.

The client rejects unsafe transport configuration and keeps bearer credentials out of rendered errors. Reader operations use `MODULAR_CONTROL_READER_TOKEN`; accepted mutations use `MODULAR_CONTROL_OPERATOR_TOKEN` plus owner idempotency request IDs.

No command may enable Live/private-exchange/signed-private-request/real-order-placement capability. No command accepts arbitrary shell, cwd, file path, log path or `tail` input.

## Read commands

System monitoring:

```text
modular-control status [--json]
modular-control health [component] [--json]
modular-control system resources [--json]
```

Market Data:

```text
modular-control dataset list [--limit N] [--offset N] [--json]
modular-control dataset show DATASET_ID [--version N] [--json]
modular-control dataset validate DATASET_ID [--version N] [--deep] [--json]
```

Strategy Factory:

```text
modular-control strategy list [--limit N] [--offset N] [--json]
modular-control strategy show RUN_ID [--json]
```

Backtests:

```text
modular-control backtest list [--limit N] [--offset N] [--json]
modular-control backtest show JOB_ID [--json]
modular-control backtest result JOB_ID [--json]
```

Research Runner:

```text
modular-control research experiments [--limit N] [--offset N] [--json]
modular-control research experiment EXPERIMENT_ID [--json]
modular-control research runs EXPERIMENT_ID [--limit N] [--offset N] [--json]
modular-control research run RUN_ID [--json]
```

PAPER runtime observation:

```text
modular-control paper status [--json]
modular-control paper orders [--limit N] [--offset N] [--json]
modular-control paper blocked-scopes [--json]
```

Durable System Acceptance status:

```text
modular-control acceptance status [--json]
```

Signal Bot status:

```text
modular-control notifications status [--json]
```

Structured operational events:

```text
modular-control events [--cursor CURSOR] [--limit N]
                       [--event-type TYPE ...]
                       [--severity SEVERITY ...]
                       [--json]
```

Events are bounded owner projections with opaque owner cursors. They are not filesystem log access.

Immutable evidence:

```text
modular-control evidence overview [--json]
modular-control evidence audit-decisions [--limit N] [--offset N] [--json]
modular-control evidence audit-evidence [--limit N] [--offset N] [--json]
modular-control evidence results [--limit N] [--offset N] [--json]
```

## Accepted owner mutations

### PAPER lifecycle

```text
modular-control paper-lifecycle start --request-id REQUEST_ID [--json]
modular-control paper-lifecycle stop  --request-id REQUEST_ID [--json]
modular-control paper-lifecycle kill  --request-id REQUEST_ID [--json]
modular-control paper-lifecycle reset --request-id REQUEST_ID [--json]
```

These are Trading Runner owner-native PAPER-only mutations. Reuse a request ID only for replay of the identical operation. Runtime truth is reread from the owner projection; the mutation receipt is not a second state machine.

### Durable System Acceptance

Start:

```text
modular-control acceptance start
  --request-id REQUEST_ID
  --dataset-id DATASET_ID
  --dataset-digest DATASET_DIGEST
  --strategy-id STRATEGY_ID
  --strategy-digest STRATEGY_DIGEST
  --config-digest CONFIG_DIGEST
  [--soak-24h]
  [--soak-72h]
  [--json]
```

Checkpoint-safe mutations:

```text
modular-control acceptance stop RUN_ID --request-id REQUEST_ID [--json]
modular-control acceptance cancel RUN_ID --request-id REQUEST_ID [--json]
modular-control acceptance retry RUN_ID --request-id REQUEST_ID [--json]
```

The Acceptance owner remains browser-independent and durable. CLI exit does not stop the run.

### Signal Bot test notification

```text
modular-control notifications test --request-id REQUEST_ID [--json]
```

The notification content is fixed by the owner. The CLI does not accept arbitrary message bodies or Telegram credentials.

## Semantics

- `--json` emits the same versioned transport models used by the Control API.
- Human output is intended for SSH/recovery diagnostics.
- `status`/`health` retain health exit codes: `0=HEALTHY`, `2=DEGRADED`, `3=UNAVAILABLE`.
- transport/configuration/API errors return `4` with bounded safe fields only.
- list pagination is bounded; invalid pagination fails before transport.
- reader commands do not initialize/migrate owner storage or claim owner leases.
- owner mutations require operator auth and owner-backed idempotency/audit semantics.
- uncertain transport outcome is not proof of failure or success; retry only with the same request ID for the same operation.

## Security and dependency intent

The terminal client remains lightweight. Monitoring and mutations are routed through Control Plane clients rather than direct imports of owner runtime/process internals.

System resource output is deliberately privacy-bounded and excludes hostname, username, environment, command line, working directory, filesystem paths, network inventory and per-process identities.

## Intentionally deferred

- Positions are not projected until an authoritative owner position contract exists.
- Storage capacity forecast is pending the #390 → #370/#371 owner-backed System Resources extension; unknown physical thresholds remain unknown.
- System Recovery operator commands are pending a complete accepted #377 owner contract and must not be fabricated in Control Plane.
- A separate interactive TUI is not justified yet; Web Dashboard plus scriptable CLI cover graphical and headless operation until measured operator workflows prove a gap.
