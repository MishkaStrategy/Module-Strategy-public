# Trading Runner reconstruction — Round 1 closure

## Source boundary

The original verified source history ending at `513726d89dca5a196b5f267c1462e41c1db7f4d2` is unavailable. Historical PR #93 contains coordination evidence only and remains blocked. This branch is a transparent clean-room reconstruction from the accepted v0.6 central checkpoint; it does not claim source, tree or history identity with the lost implementation.

## Exact identity

- accepted base branch: `integration/modular-strategy-v0.6.0-rc`;
- exact base SHA: `c2df6430234ed13d2cbdc08cf109274a1e737c1d`;
- reconstruction branch: `runtime/trading-runner-reconstruction-v0.6`;
- canonical Draft PR: #197;
- Round 1 verified code head: `b6fb4d619be9e03058bc7177a3d6422f5a35c0d9`;
- Fast gate run: `30188453678`.

## Existing contracts reused

- Core `ModuleRegistry.validate_and_plan` owns module compatibility and deterministic ordering.
- Core module lifecycle remains `StrategyModule.on_start`, `on_bar`, `on_order_event`, `on_trade`, `on_finish`, `snapshot_state` and `restore_state`.
- Router owns the canonical `DownstreamPlanEnvelope` and plan validity/content evidence.
- Paper Execution owns Router adaptation, OCC validation, order state, fills, OCO, protection, recovery and reconciliation.
- Position & Exit Management remains a Core `StrategyModule`; Runner only routes immutable views and factual events.
- No alternate trade-plan, order, position or portfolio DTO is introduced by Round 1.

## Round 1 implementation

- strict immutable paper-only configuration and canonical hash;
- deterministic lifecycle state machine;
- exact dependency-plan permutation validation;
- mandatory recovery, initial reconciliation and readiness gate before `RUNNING`;
- canonical Router-envelope identity validation and unchanged Paper-boundary forwarding;
- same-plan idempotency and same-ID/different-content fail-closed rejection;
- restart replay through the authoritative Paper Execution idempotency boundary;
- persisted kill switch with retry-pending entry cancellation state;
- cancellation retry on reset and after restart;
- risk-increasing versus risk-reducing dispatch policy;
- persisted configuration/schema/lifecycle compatibility checks;
- graceful shutdown checkpoint, reverse module stop order and terminal-state persistence;
- typed failures and audit-journal hooks.

## Defects closed during the round

1. immutable configuration mappings were not directly JSON serializable for hashing;
2. restored processed plan identities could access a missing local result cache;
3. a malicious dependency plan could substitute or duplicate modules;
4. a false cancellation return was treated as success;
5. kill-switch reset did not retry pending cancellation;
6. restart with an active kill switch did not reapply pending-entry cancellation;
7. persisted state did not bind schema, configuration hash or lifecycle enum;
8. successful shutdown persisted `STOPPING` instead of terminal `STOPPED`;
9. kill-switch scope accepted untyped values;
10. component gate initially stopped at formatting and did not retain complete diagnostics;
11. clean-wheel smoke incorrectly omitted required runtime dependencies.

Every product defect received focused regression coverage.

## Verification evidence

The exact code head `b6fb4d619be9e03058bc7177a3d6422f5a35c0d9` passed the permanent component gate on both Python 3.12 and 3.13:

- compileall: PASS;
- Ruff format: PASS;
- Ruff lint: PASS;
- strict MyPy: PASS, no issues in seven source files;
- component and real-contract suite: `25 passed` per Python version;
- actual Core `ModuleRegistry` dependency test: PASS;
- actual Router `DownstreamPlanEnvelope` contract test: PASS;
- wheel and sdist build: PASS;
- clean wheel installation with declared runtime dependencies: PASS;
- public import smoke: PASS;
- paper-only static source boundary: PASS.

Evidence artifacts:

- Python 3.12 artifact `8627761263`, digest `sha256:26c64f95620b0d2eae4b17931872d3de017f74acfa67f379d34e5ced7b464ba3`;
- Python 3.13 artifact `8627753185`, digest `sha256:94663e2417f9c36707611f2a557bfa896c62360ce246892aff680a8cdad72854`.

The repository-wide `core-ci` did not reach project installation or tests because `actions/setup-python@v5` failed on the self-hosted environment. This is recorded as separate infrastructure evidence and is not represented as a product success or product failure.

## Round 1 verdict

`ROUND 1 COMPLETE — FOUNDATION GATE GREEN`

This is not integration readiness. It authorizes only progression to Round 2 under Draft PR #197.

## Known gaps after Round 1

- actual Core `MarketView` pipeline execution and feature propagation;
- factual market-event validation, freshness and bounded queues;
- execution and position event routing;
- Core checkpoint/event-store adapters and journal-chain verification;
- watchdog, metrics, structured logging and notification isolation;
- forced-shutdown and unclean-restart handling;
- full v0.6 executable-chain acceptance and final integration gate.

## Round 2 entry criteria

Round 2 must add the Core pipeline and market-data boundary without creating strategy logic or alternate state engines. It must close with market-event ordering/freshness/backpressure tests and Intermediate Audit 1.
