> Historical snapshot preserved during the 2026-10-02 release-readiness audit. Current plan: `ROADMAP.md` and `.github/HQ_CRITICAL_PATH.md`.

# Full Project Integration Ledger

Status: **ACTIVE — LATE FULL-RC / PRE-OPERATIONAL PAPER READINESS**

This is the current human-readable integration ledger for
`MishkaStrategy/Module-Strategy`. GitHub live refs, Issues, PRs and CI remain the
execution source of truth. Historical checkpoint detail is preserved in earlier
ledger blobs and dated acceptance documents; it must not be confused with the
current live phase.

Current HQ sequencing is documented in `docs/hq/ROADMAP_2026-08-12.md`.

```yaml
schema_version: 5
repository: MishkaStrategy/Module-Strategy
integration_branch: integration/modular-strategy-full-rc
integration_head_observed: 574cde76c28ccea64d926b65bd21d186a0287a81
base_branch: main
main_head_observed: 6c71af9333a650f79c3a3c64bfe43a2f39ba2bb5
paper_only: true
live_execution: prohibited
current_integration_phase: late_full_rc_pre_operational_paper_readiness
hq_tracking_issue: 373
roadmap_reconciliation_issue: 712
control_ui:
  issue: 370
  pr: 371
  head: 15a4bad6eba603df3ee2fd6c99f014e7ce843b1b
  status: accepted_draft_parent_unmerged
system_recovery:
  issue: 377
  status: advanced_open
  latest_core_oms_issue: 627
  latest_core_oms_pr: 656
  latest_integrated_head: 574cde76c28ccea64d926b65bd21d186a0287a81
validation:
  issue: 369
  final_24h_soak: pending
storage_retention:
  issue: 390
  backup_dependency_issue: 542
  status: advanced_open
history_policy:
  force_push: prohibited
  rebase_accepted_history: prohibited
  history_preservation: required
release_actions:
  merge_main: prohibited_without_explicit_authorization
  mark_ready: prohibited_without_explicit_authorization
  auto_merge: prohibited
  tag: prohibited
  stable_github_release: prohibited
  package_publish: prohibited
  deployment: prohibited
  external_market_data_bind: prohibited
  private_exchange_access: prohibited
  live_execution: prohibited
```

## Current stage

The project is no longer primarily assembling isolated modules. The accepted
full-RC contains the paper/research executable chain and the major platform
owners needed to operate it: historical Market Data and Dataset Registry,
Backtest, Research Runner, Results Aggregator, Strategy Factory, independent
Strategy Audit, Decision Router, Paper Execution, Trading Runner, Signal Bot,
System Acceptance and System Recovery substrate.

The current objective is **durable operator-safe PAPER readiness**, not feature
expansion. Remaining work is system-level recovery, storage/evidence reliability,
observability, independent runtime safety, final exact-head acceptance, futures
realism closure, release lifecycle, security and supported operations.

## Current pull-request topology

- PR #371 is the accepted Control UI integration parent against
  `integration/modular-strategy-full-rc`. It remains Draft/open/unmerged by
  policy. Its accepted head is
  `15a4bad6eba603df3ee2fd6c99f014e7ce843b1b`; live full-RC
  `574cde76c28ccea64d926b65bd21d186a0287a81` is its exact merge base and the
  parent is 0 behind at the current checkpoint.
- PR #656 (`recovery/core-paper-oms-627`) is merged. Its merge commit is the
  current full-RC head `574cde76c28ccea64d926b65bd21d186a0287a81` and Issue #627 is completed.
- PR #325 remains validation-only and must not be merged.
- Historical integration PRs remain evidence. They are not current product bases
  unless a live Issue/PR explicitly designates them.
- `main` and full-RC are divergent. Any future integration into `main` requires a
  separate conflict/ancestry audit and explicit owner authorization.

## Control UI / GAP-003 checkpoint

Issue #370 records all Control UI phases 0–10 as implemented and accepted on the
Draft parent. Exact-head acceptance covers the typed loopback Control Plane,
Dashboard, `modular-control`, Market Data, Backtest, Research, Evidence, PAPER
Runtime lifecycle/reconciliation, System Acceptance/soak, Signal Bot,
structured events, System Resources/Capacity, browser E2E, privacy/security,
Test Architecture and routing/policy gates.

HQ treats the **implementation and exact-head acceptance criteria as satisfied**
on Draft #371. This does not authorize merging #371 or closing unrelated system
gaps. New UI feature work requires a new explicit owner-backed requirement.

## System Recovery / GAP-004 checkpoint

Accepted recovery progression now includes:

1. fail-closed System Recovery manifest/snapshot/restore substrate;
2. System Acceptance + Research owner recovery;
3. Core/Paper checkpoint + matching Paper journal recovery;
4. Trading Runner runtime/operator/checkpoint/hash-chained-journal recovery;
5. Core paper OMS factual state recovery from #627 / merged #656, including cash,
   orders, pending membership, duplicate client-order fences, sequence counters,
   active position, pending-close state and config/instrument identity.

Issue #377 remains open. The next correct step is a fresh owner inventory after
#656 and one complete clean-host platform restore/reconciliation/deterministic
continuation proof. Unknown or unclaimed mutable state is not implicitly
recoverable.

## Storage / GAP-013 checkpoint

Issue #390 remains open. Accepted work already includes storage ownership and
protected identity inventory, self-hosted disk telemetry, reduced successful CI
artifact pressure, storage-pressure failure contracts, manual recovery guidance,
and bounded Actions diagnostic pruning work.

Remaining work is owner-specific and must not be duplicated:

- complete Actions artifact semantic migration under #646 and bounded children;
- complete backup-dependent pruning only through accepted System Recovery owner
  proofs under #542/#377;
- finish thresholds/capacity policy and destructive-cleanup negative tests;
- address weak/contended fast-runner reliability under #633 without weakening
  substantive tests;
- keep accepted/release evidence protected and fail closed on ambiguous ownership.

## HQ execution order

The current order is intentionally conservative:

1. **#712 governance reconciliation** — this ledger, the HQ roadmap and live
   Issue handoffs;
2. **GAP-004 #377 + #542** — close the complete clean-host recovery boundary and
   only then reconsider backup-dependent pruning for exact covered owners;
3. **GAP-013 #390** — finish storage/evidence reliability and runner policy;
4. **GAP-005 #378 + GAP-006 #379** — authoritative observability plus independent
   runtime Risk Supervisor;
5. **GAP-002 #369** — final exact-head validation, short soak, then 24h PAPER
   soak on the stabilized safety/recovery candidate;
6. **GAP-008 #376/#478** — canonical full-corpus historical vs funding vs
   funding+mark A/B acceptance with registered owner data;
7. **GAP-009 #387 + GAP-014 #393 + operations #400** — release identity,
   migration/rollback, whole-system security and one supported deployment
   topology;
8. only then evaluate an immutable platform release and an eventual
   conflict-checked path toward `main`.

Portfolio/multi-symbol #396 remains deferred behind its explicit safety,
monitoring, release and strategy-lifecycle dependencies.

## Canonical strategy identity

Historical comparison evidence remains immutable:

- candidate: `strategy-candidate-51659c6ed582f2843357bd33`;
- composition hash:
  `51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9`;
- composition: `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`;
- semantic digest:
  `63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb`;
- trades: `438`;
- final equity: `15266.31855203826`;
- net return: `0.526631855203826`;
- profit factor: `1.328366621740268`;
- maximum closed-equity drawdown: `0.1164174394297347`;
- win rate: `0.3515981735159817`.

Futures-aware evidence must use new immutable run identities and must not rewrite
this historical baseline.

## Registered dataset identity

Canonical registered historical data remains:

- dataset: `binance_usdt-m-perpetual_btcusdt_5m`;
- version: `1`;
- dataset hash:
  `49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5`;
- source URI: `dataset://binance_usdt-m-perpetual_btcusdt_5m/v1`;
- rows: `721597`;
- relation: `PROVEN_BY_EXACT_FRAME_AND_RESULT_PARITY`.

Raw/normalized market bytes, private local storage paths, credentials and private
runner topology must not be published as public acceptance evidence.

## Historical checkpoint preservation

The Optimization Waves 1–2 and earlier full-RC history remains valid historical
evidence but is no longer the current phase. Detailed historical state is
preserved in:

- Git blob `ce57e69c1e420150f015f76b7027397ce3c05825`;
- Git blob `5416e0aec61167561d9e5cb1cd6d0647007d5db6`;
- `docs/integration/FULL_RC_ACCEPTANCE_CHECKPOINT_2026-07-31.md`;
- `docs/integration/FULL_RC_ACCEPTANCE_CHECKPOINT_2026-08-04.md`.

Do not use those older SHAs as live bases without rechecking the current branch.

## Exact-head acceptance policy

Any final acceptance claim must bind evidence to the exact tested head. A later
commit invalidates the previous exact-head acceptance for affected surfaces.
Startup-only infrastructure failures, expected skips, cancelled superseded runs
and queued jobs remain separately classified; none is silently counted as a
substantive PASS.

The final 24h GAP-002 soak should be executed only after the recovery and runtime
safety boundaries stop moving. Intermediate 2h/engineering soaks are useful but
do not close GAP-002.

## Safety boundary

This checkpoint is research and PAPER runtime only. It is not a claim of
profitability, production readiness or Live-trading fitness. Missing identity,
lineage, checksum, independent admission, recovery, risk-supervisor or safety
evidence must stop progression rather than fall back to mutable aliases,
unverified backup assumptions or permissive execution.
