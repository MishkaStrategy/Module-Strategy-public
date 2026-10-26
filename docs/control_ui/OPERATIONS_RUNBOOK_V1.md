# Control UI Operations & Recovery Runbook v1

Status: operational documentation for Draft PR #371 / Issue #370. This runbook describes the accepted local/PAPER Control UI surface. It does not authorize Live trading, external deployment or private exchange access.

## 1. Safety boundary

The Control Plane is a local projection/orchestration layer. It does not replace Market Data, Trading Runner, Paper Execution, Research Runner, Strategy Factory, Strategy Audit, Results Aggregator, Signal Bot or System Acceptance ownership.

Non-negotiable boundaries:

- PAPER/local-only;
- Control API bind remains loopback-only;
- no Live toggle, private exchange credentials, signed private requests or real order placement;
- no arbitrary shell/script execution;
- no arbitrary file/log path reads or `tail`-style browser input;
- browser tokens remain memory-only;
- authenticated HTTP/SSE clients do not use ambient cookies or cache authenticated responses;
- Control API responses are `Cache-Control: no-store`;
- reads must not initialize/migrate owner persistence or claim owner leases;
- unsupported operations fail closed rather than being simulated in UI state.

## 2. Installation

From repository root:

```bash
python -m pip install -e 'applications/control_plane[interface]'
```

For Control Plane development/validation:

```bash
python -m pip install -e 'applications/control_plane[dev,interface]'
```

The dedicated clean-install workflow proves the built `[interface]` wheel on Python 3.12 and 3.13. Optional project owners remain lazy; a missing owner must produce an actionable typed failure when its feature is used, not crash base import/startup.

Web Console development:

```bash
cd web/market-data-console
npm ci --ignore-scripts --no-audit --no-fund
```

Use the committed lockfile.

## 3. Expected local topology

Default Control Plane:

```text
http://127.0.0.1:8765
```

Default existing Market Data API bridge:

```text
http://127.0.0.1:8000
```

Accepted owner adapters use explicit loopback URLs or server-side owner attachments. Remote TLS/reverse-proxy deployment is outside this scope.

## 4. Important environment variables

Core server/client variables:

```text
MODULAR_CONTROL_HOST
MODULAR_CONTROL_PORT
MODULAR_CONTROL_URL
MODULAR_CONTROL_READER_TOKEN
MODULAR_CONTROL_OPERATOR_TOKEN
MODULAR_CONTROL_REQUEST_TIMEOUT_SECONDS
MODULAR_CONTROL_LOG_LEVEL
```

Market Data bridge:

```text
MODULAR_CONTROL_MARKET_DATA_URL
MODULAR_CONTROL_MARKET_DATA_READER_TOKEN
```

Other owner attachments are configured server-side. Never place owner secrets, Telegram tokens/chat IDs or private paths into browser URLs, screenshots or evidence.

## 5. Validate configuration

```bash
modular-control-api --check
```

Expected high-level properties include:

- `status=ok`;
- `mode=PAPER`;
- `remote_bind_allowed=false`;
- capability booleans for configured owner surfaces/authentication;
- System Resources enabled.

`--check` reports capability state, never bearer values.

## 6. Start the local Control Plane

```bash
modular-control-api
```

Default bind is `127.0.0.1:8765`.

The standalone server must not silently construct a second Trading Runner/Paper Execution/System Acceptance owner. Missing owner attachments fail typed/actionably.

## 7. Initial health checks

```bash
modular-control status --json
modular-control health --json
modular-control system resources --json
```

Health and durable owner lifecycle are separate concepts. A component health projection does not replace its native state.

## 8. Read commands

### Market Data

```bash
modular-control dataset list --json
modular-control dataset show DATASET_ID --json
modular-control dataset validate DATASET_ID --json
modular-control dataset validate DATASET_ID --deep --json
```

### Strategy Factory

```bash
modular-control strategy list --json
modular-control strategy show RUN_ID --json
```

### Backtests

```bash
modular-control backtest list --json
modular-control backtest show JOB_ID --json
modular-control backtest result JOB_ID --json
```

### Research

```bash
modular-control research experiments --json
modular-control research experiment EXPERIMENT_ID --json
modular-control research runs EXPERIMENT_ID --json
modular-control research run RUN_ID --json
```

### PAPER runtime reads

```bash
modular-control paper status --json
modular-control paper orders --json
modular-control paper blocked-scopes --json
```

### Evidence

```bash
modular-control evidence overview --json
modular-control evidence audit-decisions --json
modular-control evidence audit-evidence --json
modular-control evidence results --json
```

### Signal Bot status

```bash
modular-control notifications status --json
```

### Structured operational events

```bash
modular-control events --limit 100 --json
```

Optional filters are owner-bounded `--cursor`, repeated `--event-type` and repeated `--severity`. The CLI does not accept filesystem paths, shell commands or arbitrary log files.

## 9. Accepted owner mutations

Mutation commands require the operator token and a caller-supplied bounded `request_id`. Reuse a request ID only to replay the identical owner operation.

### PAPER lifecycle

```bash
modular-control paper-lifecycle start --request-id REQUEST_ID --json
modular-control paper-lifecycle stop --request-id REQUEST_ID --json
modular-control paper-lifecycle kill --request-id REQUEST_ID --json
modular-control paper-lifecycle reset --request-id REQUEST_ID --json
```

Trading Runner remains authoritative. Control Plane receipts/replay are not runtime truth; the factual runtime should be reread after mutation.

### System Acceptance

Status:

```bash
modular-control acceptance status --json
```

Start requires candidate identity fields and optional 24h/72h soak enablement:

```bash
modular-control acceptance start \
  --request-id REQUEST_ID \
  --dataset-id DATASET_ID \
  --dataset-digest DATASET_DIGEST \
  --strategy-id STRATEGY_ID \
  --strategy-digest STRATEGY_DIGEST \
  --config-digest CONFIG_DIGEST \
  --json
```

Owner checkpoint mutations:

```bash
modular-control acceptance stop RUN_ID --request-id REQUEST_ID --json
modular-control acceptance cancel RUN_ID --request-id REQUEST_ID --json
modular-control acceptance retry RUN_ID --request-id REQUEST_ID --json
```

Browser closure must not stop an authoritative Acceptance run.

### Signal Bot test notification

```bash
modular-control notifications test --request-id REQUEST_ID --json
```

The message is owner-defined/fixed. Arbitrary browser/CLI notification bodies are not accepted.

## 10. Browser routes

Accepted routes include:

```text
#/overview
#/system
#/system/resources
#/datasets
#/artifacts
#/jobs
#/strategies
#/backtests
#/research
#/paper
#/acceptance
#/notifications
#/events
#/evidence
```

The browser consumes the same typed Control Plane and does not become a durable owner.

## 11. Current intentionally unavailable surfaces

- Positions are not projected until an authoritative owner position contract exists.
- Unified storage capacity forecast remains pending the #390 → #370/#371 integration slice. Unknown physical watermarks must stay unknown; no deletion authority is implied.
- The owner-native System Recovery procedure and manual operator runbook for #377 are accepted and available. The bounded named recovery-plan/profile service and Control Plane adapter remain open under [#778](https://github.com/MishkaStrategy/Module-Strategy/issues/778); no recovery path or host path may be supplied through the browser. Do not construct a second backup/restore engine in Control Plane. See the [System Recovery runbook](../../applications/system_recovery/RUNBOOK.md).
- Separate TUI remains deferred until measured operator need exists.

## 12. Recovery discipline

After a Control Plane/Web restart:

1. run `modular-control-api --check`;
2. start the loopback API;
3. read `status`, `health`, `system resources` and relevant owner status;
4. confirm durable owner runs/orders/events from their projections rather than browser memory;
5. retry mutations only with the exact original request ID when replaying the same operation;
6. do not infer a successful side effect from a transport timeout alone.

System-level backup/restore is covered by the accepted [System Recovery owner-native manual runbook](../../applications/system_recovery/RUNBOOK.md) for [#377](https://github.com/MishkaStrategy/Module-Strategy/issues/377). This Control UI runbook does not claim the separate bounded named recovery-plan/profile service or Control Plane adapter tracked by [#778](https://github.com/MishkaStrategy/Module-Strategy/issues/778). Keep restore roots and other filesystem paths in the trusted operator environment; browser-supplied paths remain forbidden.

## 13. GitHub acceptance discipline

- exact-head CI evidence applies only to that SHA;
- parent PR #371 remains Draft/open/unmerged until separate HQ/integration authorization;
- `main` is not edited directly;
- validation-only PR #325 must never be merged;
- owner failures, CI transport failures and evidence-publication failures must be classified separately rather than hidden.
