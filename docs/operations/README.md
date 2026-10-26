# Operations contracts

Current fail-closed storage/recovery contracts:

- [Storage ownership inventory](STORAGE_OWNERSHIP_INVENTORY.md)
- [Backup-dependent pruning admission](BACKUP_DEPENDENT_PRUNING_ADMISSION.md)
- [Single-host PAPER topology proposal](SINGLE_HOST_PAPER_TOPOLOGY_PROPOSAL.md)
- [System Recovery operator runbook](../../applications/system_recovery/RUNBOOK.md)

These documents define ownership, recoverability and admission boundaries. They do not grant generic
deletion authority, change runtime semantics or authorize Live/private-exchange operation.

## Known loopback listeners — partial input to #400

This inventory records component defaults and explicit bind checks from the current source. It is
not an accepted single-host deployment topology, host-firewall policy, service-manager
configuration or release acceptance. Loopback binding constrains direct socket access; it does not
choose host firewall rules or authorize a reverse proxy.

| Surface | Default endpoint | Bind contract and source |
| --- | --- | --- |
| Control Plane API | `127.0.0.1:8765` | Host/port are configurable with `--host`/`--port` or `MODULAR_CONTROL_HOST`/`MODULAR_CONTROL_PORT`; configured hosts are validated as loopback (`127.0.0.1`, `localhost` or `::1`). [Server defaults and validation](../../applications/control_plane/src/modular_control_plane/server.py); [Control UI runbook](../control_ui/OPERATIONS_RUNBOOK_V1.md). |
| Market Data API | `127.0.0.1:8000` | Host/port are configurable with `--host`/`--port` or `MDI_API_HOST`/`MDI_API_PORT`; configured hosts are validated as loopback (`127.0.0.1`, `localhost` or `::1`). [API defaults and validation](../../modules/market_data_ingestion/src/market_data_application/server.py); [Control UI runbook](../control_ui/OPERATIONS_RUNBOOK_V1.md). |
| Signal Bot health and metrics | `127.0.0.1:8080` | The CLI accepts `--health-host` and `--health-port` overrides; it does not enforce loopback. Its deployment guide says to keep it on loopback unless an authenticated reverse proxy is explicitly configured; the health server itself does not enforce that policy. [CLI](../../src/signal_bot/cli.py), [health server](../../src/signal_bot/health.py), [deployment guide](../signal_bot/DEPLOYMENT.md). |
| Signal Bot operator control | `127.0.0.1:8081` | Host/port are configurable with `--control-host`/`--control-port` or `SIGNAL_BOT_CONTROL_HOST`/`SIGNAL_BOT_CONTROL_PORT`; the authenticated control server rejects non-loopback hosts. [CLI](../../src/signal_bot/cli.py), [control server](../../src/signal_bot/operator_http.py). |
| Stage-1 Mac owner transports | Trading Runner `127.0.0.1:8780`; System Acceptance `127.0.0.1:8781`; Research Runner `127.0.0.1:8782` | These are fixed by the typed Stage-1 Mac service-manager composition; the System Acceptance host verifies its bound address is loopback. This composition is one Stage-1 deployment path, not the complete product topology. [Service manager](../../scripts/system_acceptance_stage1_service_manager.py), [Stage-1 host](../../scripts/system_acceptance_stage1_host.py), [owner bootstrap](../system_acceptance/PRODUCTION_SERVICE_BOOTSTRAP_V1.md). |

Issue #400 remains open. The table does not settle the supported host/runtime matrix, host firewall
or proxy configuration, complete process ownership and restart order, persistent-data locations
and permissions, release-lock installation, recovery integration, upgrade/rollback, or clean-host
smoke acceptance. Those require the remaining owner-backed and candidate-bound inputs in the
[critical path](../../.github/HQ_CRITICAL_PATH.md).
