# Cross-component fault and recovery acceptance

Issue: #399

## Ownership boundary

This layer is validation-only. It does not own runtime state and does not add a state machine.

Authoritative state remains with the existing component owners. Risk Supervisor owns PAPER safety state and guard decisions; Trading Runner owns runner lifecycle and committed work; Paper Execution owns order/fill execution state; Market Data owns market-data progress; System Recovery coordinates owner-native restore contracts; Control Plane remains a read/control projection rather than a factual runtime owner.

The fault-injection harness under `applications/system_acceptance/tests/` is test-only. Its injectors disturb inputs, timing, SQLite access, process/transport continuity, response delivery, recovery ordering, or bounded queues. They never compute an owner transition.

The cross-component tests under `applications/system_acceptance/tests/` are also test-only. They import accepted owner packages and verify the reviewed matrix against real public contracts. The normal System Acceptance owner suite may module-skip tests when optional cross-component packages are not installed; the dedicated #399 workflow installs the required accepted owner surfaces and requires those tests to pass. `modular_system_acceptance.fault_recovery` deliberately does not duplicate Risk Supervisor, Market Data, Paper Execution, Core, or observability state machines.

## Machine-readable contracts

`applications/system_acceptance/fault_recovery_matrix.v1.json` is the reviewed fault matrix. It covers:

- process termination immediately before and after an authoritative commit/checkpoint boundary;
- stale/interrupted Market Data;
- delayed/out-of-order owner evidence;
- SQLite busy/locked/WAL contention;
- disk pressure/read-only behavior;
- corrupt or truncated owner state;
- wall-clock jumps;
- Signal Bot unavailability;
- Control Plane restart;
- partial outage with wrong recovery ordering;
- operator retry after a lost response;
- bounded-queue resource pressure.

A fault class may contain more than one scenario when the failure window changes semantics. Process termination therefore has distinct pre-commit and post-commit scenarios rather than collapsing both crash windows into one synthetic state.

Each scenario names the target owner, primitive injector, expected owner behavior, Risk Supervisor expectation, observability expectation, owner-native recovery action, forbidden side effects, factual comparison fields, and sanitized evidence schema.

`applications/system_acceptance/fault_recovery_residual_risks.v1.json` is a separate machine-readable coverage register. Every matrix scenario has exactly one entry. The register describes **system-level recovery evidence**, not merely whether an individual owner contract has a green test.

A green owner-contract assertion proves only the contract it executes. A scenario is promoted from residual only when the same scenario actually executes the full continuous -> fault -> owner-native recovery -> declared factual-state equality path. If that path is incomplete, the scenario remains `residual_not_fully_reproduced` even when individual Risk Supervisor or observability semantics are directly verified in fast CI.

On the current #399 candidate the deterministic coverage is:

- `owner_backed_fast`: 1 scenario (`acceptance_sqlite_busy_wal`);
- `composed_contract_fast`: 7 scenarios (`runner_process_kill`, `runner_process_kill_before_checkpoint`, `market_data_stale_interrupt`, `out_of_order_owner_evidence`, `signal_bot_unavailable`, `control_plane_restart`, `operator_retry_after_timeout`);
- `residual_not_fully_reproduced`: 5 scenarios requiring supported-host physical/system acceptance beyond the executed deterministic fast-CI paths.

The process-kill scenarios use a real OS child process on the eligible Linux `[self-hosted, fast]` runner. Both paths begin from an authoritative Core broker state with one pending PAPER market order and a durable Trading Runner checkpoint/journal attestation. `PaperBarExecutor` durably writes its native pending fence before entering the venue boundary. For the pre-commit window, the child announces that boundary and is terminated with `SIGKILL` before Core broker mutation; recovery proves the durable broker still has no fill/position mutation and replays the pending bar exactly once. For the post-commit window, the child processes the bar, persists the authoritative Core broker snapshot through `AtomicJsonStateRepository`, then is killed before `PaperBarExecutor` can record the processed identity; recovery observes the owner mutation and reconciles it as already applied instead of replaying the financial effect. In both windows the same fault scene requires Risk Supervisor `HALT/transport_unhealthy`, unified observability `TRADING_RUNNER=UNAVAILABLE/owner_unavailable`, owner-native recovery, duplicate-fence replay, durable Runtime checkpoint/journal restoration, and exact equality of order count, fill count, position revision, and Runner checkpoint sequence to an uninterrupted reference. No hostname routing, privileged host mutation, or synthetic process state is used.

The SQLite scenario executes an uninterrupted real `SystemAcceptanceOwner` reference, holds a competing `BEGIN IMMEDIATE` writer against the owner's WAL-mode `SQLiteAcceptanceStore`, proves the blocked owner request leaves no run or mutation, releases the lock, reopens through the authoritative store/owner API, retries the same START identity, verifies WAL sidecars and `PRAGMA integrity_check`, and compares the matrix-declared factual state to the uninterrupted reference. No synthetic recovery state is used.

The Market Data scenario executes both an uninterrupted ingestion path and a real interrupted/resumed path through the accepted `IngestionService`. The faulted path persists an authoritative PAUSED checkpoint with completed and pending chunks, projects stale Market Data as `UNAVAILABLE/source_stale`, latches Risk Supervisor HALT with `market_data_stale`, and proves a risk-increasing PAPER plan is blocked without Core broker mutation. Recovery restores completed raw chunks instead of reacquiring them, finishes pending chunks, resolves the registered Parquet dataset through the fail-closed reader, presents fresh dataset identity back to Risk Supervisor, clears the safety latch only through signed operator `RECOVER`, and executes a real PAPER market order/fill using the registered Kline stream. Final checkpoint cursor, registered dataset id/version/hash, Core order sequence, and Core fill sequence match the uninterrupted reference path.

The out-of-order scenario composes the accepted Risk Supervisor, `RiskSupervisedPaperAdmission`, `PaperExecutionCoordinator`, and Core broker. A non-monotonic snapshot returns `MANUAL_REVIEW` with `snapshot_out_of_order` while the persisted Risk sequence and Core broker snapshot remain unchanged. A newer authoritative snapshot is then accepted, a real reduce-only PAPER close executes through the accepted admission boundary, and the declared order/fill/position/Risk factual fields match the uninterrupted reference path.

The Signal Bot scenario executes an uninterrupted Signal Bot reference and a faulted path through the production `TelegramNotifier` plus the authoritative WAL-mode `SQLiteStateStore`. The faulted notifier targets a deliberately bound but non-listening local HTTPS endpoint, so the production `urllib` transport reaches the OS and receives a real TCP connection refusal without privileged network mutation or external-service dependency. The owner persists the signal as `SEND_FAILED`, keeps exactly one outbox identity and its monotonic market cursor, records zero `SENT` notifications, and unified observability projects `SIGNAL_BOT=UNAVAILABLE/owner_unavailable`; the matrix correctly leaves Risk Supervisor `HEALTHY` because notification delivery is not a PAPER safety owner. Recovery closes and reopens the same owner store, waits past the bounded retry fence, resumes through `SignalEngine.retry_pending`, records exactly one `SENT` notification, and proves a second retry is empty. `signal_outbox_count`, `signal_cursor`, and `notification_count` exactly match the uninterrupted reference. This is evidence for #399's required Signal Bot unavailable/notification-delivery-failure class; it does not claim a literal upstream Telegram or host-NIC partition was reproduced.

The lost-response operator-retry scenario starts from a real PAPER market order/fill baseline. It commits a signed Risk Supervisor emergency HALT, simulates loss of the response by replaying the identical command ID, proves only one operator-HALT transition exists, and proves a subsequent risk-increasing PAPER open is blocked before Paper/Core side effects. The declared order/fill/Risk/operator-request factual fields match the single-apply reference path.

The Control Plane restart scenario executes after the browser acceptance has created a real durable System Acceptance owner run. Exact-head fast CI captures that owner projection and the unified typed observability projection through both loopback HTTP and `modular-control`, sends real `SIGKILL` to the separate Control Plane OS process, proves the loopback transport is unavailable, and requires the browser to show fail-closed read errors without a cached system summary. The owner process and SQLite store remain authoritative and running throughout. CI then starts a new Control Plane process on the same loopback port and proves HTTP, CLI, and browser all re-read the same acceptance run identity/status/phase/active time/restart count/identity plus unchanged observability health/reason/alert semantics. Dynamic collection timestamps and host resource values are deliberately not treated as owner identity. Control Plane performs no recovery transition and remains only a read/control projection.

`physical_required` is independent from verification status. The five remaining residuals are kept explicit because their current matrix claims require supported-host/system conditions not truthfully reproduced by the deterministic fast-CI proofs. They are not converted into synthetic PASS results.

## Authoritative safety and observability semantics

Risk Supervisor states and guard codes are validated by importing `SafetyState` and `RiskGuardCode` from `modular_risk_supervisor`; the System Acceptance schema does not maintain a copy. The real process-kill composition additionally requires the process-outage Risk decision inside the same fault scene before recovery proceeds.

Component names and health states are validated by importing `Component` and `ComponentHealth` from `modular_observability`. Generic missing owner truth uses the backend's actual `owner_unavailable` reason. A stale Market Data source is projected as `UNAVAILABLE` with `source_stale`. The process-kill composition reads the accepted `UnavailableSource` projection for Trading Runner after the child has actually been killed. The Signal Bot transport-failure composition uses the same accepted `UnavailableSource` projection for `Component.SIGNAL_BOT` while preserving the matrix's healthy Risk expectation.

Control Plane restart is not represented as a fake observability component. While its own transport is down, the matrix records an out-of-band `control_plane_transport` expectation; once it returns, the read projection is rebuilt from authoritative owners.

Some invalid authoritative inputs are fail-closed requests without a persisted owner transition. In particular, out-of-order evidence and future-clock evidence return `MANUAL_REVIEW` for that evaluation but do not overwrite the last accepted Risk Supervisor state. If the last accepted state was healthy, truthful current-state observability remains healthy and must not invent a persistent blocked alert for the rejected input.

Mandatory safety breaches are different: they persist a latched `HALT`. Healthy evidence alone does not clear that latch; `recovery_required` remains authoritative until accepted operator recovery. The recovery-order matrix scenario therefore expects `HALT`, not `MANUAL_REVIEW`. The Market Data recovery proof explicitly exercises this latch before signed `RECOVER` returns the owner to HEALTHY.

## Deterministic acceptance algorithm

A scenario may be promoted from residual to system-level deterministic PASS only when fast CI executes all of the following against the real accepted owners:

1. Run the uninterrupted reference path and read only the scenario's declared authoritative factual fields.
2. Inject the deterministic fault without mutating owner state directly.
3. Read affected owners through existing authoritative contracts; missing truth remains unavailable rather than inferred.
4. Require the real Risk Supervisor decision state/guard where safety-relevant.
5. Require the real unified observability projection or, for Control Plane self-unavailability, the explicitly out-of-band transport expectation.
6. Recover through the owner-native path, preserving original request identity for idempotent retry contracts.
7. Re-read the same authoritative factual fields and compare them with the uninterrupted reference path.
8. Fail on every declared forbidden side effect, including duplicate financial effects, partial owner mutation, cursor/revision regression, fabricated success, or unbounded queue growth.
9. Emit sanitized evidence only from that actually executed path.

If steps 1-9 are incomplete, the scenario remains in the residual-risk register. A unit test may validate serialization and digest functions with fixtures, and a cross-component test may validate authoritative owner state/guard/observability semantics, but neither is recovery evidence by itself. CI does not publish fixture digests as recovery evidence.

## CI topology

`.github/workflows/system-fault-recovery-ci.yml` is an exact-head deterministic gate on `[self-hosted, fast]`, as required by repository runner policy for non-backtest fault/recovery work. It:

- verifies exact candidate checkout;
- validates the generic matrix/residual schema and primitive injectors, including distinct pre-commit and post-commit crash windows;
- installs the accepted Market Data Parquet surface, Risk Supervisor, Paper Execution, Trading Runner, and unified observability packages before executing the cross-owner acceptance tests;
- verifies matrix state/guard/component/health/reason expectations against those authoritative packages;
- executes the real Trading Runner child-process `SIGKILL` pre/post owner-commit recovery paths with durable Core broker state, Runner checkpoint/journal attestation, Risk/observability fail-closed assertions, and continuous-vs-recovered factual equality;
- executes the real Signal Bot production-transport TCP-refusal path, owner outbox reopen/retry, unavailable observability assertion, duplicate-delivery fence, and continuous-vs-recovered factual equality;
- executes the System Acceptance storage pressure rollback proof and the owner-backed WAL lock/reopen factual-equality proof;
- executes the composed Market Data interruption/resume, lost-response retry, and out-of-order recovery paths through real accepted owner boundaries;
- reuses the accepted Risk Supervisor PAPER system E2E;
- reports exact `owner_backed_fast`, `composed_contract_fast`, and residual counts without emitting synthetic recovery evidence.

The exact-head `market-data-web-console-e2e` gate supplies the Control Plane restart scene that requires a real browser: it verifies the same candidate checkout, keeps the durable System Acceptance owner alive, kills and rebinds the actual loopback Control Plane process, and performs the HTTP/CLI/Web assertions described above. This is a joint gate with the matrix workflow, not a second recovery coordinator.

A green workflow means the validation layer, executed owner-backed/composed scenarios, owner-contract assertions, and residual classification are internally consistent on the exact head. It does **not** turn the five remaining physical/system residuals into deterministic evidence. Non-empty residual coverage remains blocking evidence for any physical/system scenario that would otherwise be claimed as recovered.

The existing owner workflows remain authoritative for their full component suites. This cross-component gate does not replace them.

## Observability dependency

Issue #378 and its Control UI/observability slice were accepted before this history-preserving branch sync. The Control Plane restart scene therefore exercises the accepted unified observability backend and the same typed read-only Web/CLI/loopback surface; it does not reconstruct component health in the browser or the #399 validation layer.

## Physical/system residual risk

The residual register now carries five scenarios whose claimed supported-host/system failure is not truthfully reproduced by the executed deterministic paths: real filesystem exhaustion/read-only remount, torn durable checkpoint writes, host wall-clock stepping, simultaneous supported-host multi-service outage/recovery ordering, and sustained whole-runtime queue saturation.

The process-kill scenarios, Signal Bot notification-delivery scenario, and Control Plane restart/rebind scenario are no longer in this residual set: exact-head fast CI sends real `SIGKILL` to the Trading Runner child at both native PaperBar owner windows, uses the production Signal Bot HTTPS transport against a real OS TCP refusal, and separately sends `SIGKILL` to the actual Control Plane before same-port loopback rebind and Web/CLI owner re-read. The five remaining residuals stay explicit rather than being approximated with synthetic evidence. They require the appropriate supported-host/system acceptance path. The final physical 2-hour/24-hour soak is not run or claimed by this scope.

Strategy Audit/#933/#873/#866, release-manifest governance, and PR #325 are also outside this work.
