# Handoff: Trading Runner clean-room reconstruction

- component_slug: `trading-runner-reconstruction`
- repository: `mishkacher/Module-Strategy`
- base_branch: `integration/modular-strategy-v0.6.0-rc`
- exact_base_sha: `c2df6430234ed13d2cbdc08cf109274a1e737c1d`
- canonical_working_branch: `runtime/trading-runner-reconstruction-v0.6`
- canonical_draft_pr: `#197`
- verified_candidate_head: `8c4aae125ac2a8bd0d2e2f5c07cd252779918f6b`
- documentation_checkpoint_parent: `aef54f54510b603b8eef2527ba319de4dcb6321f`
- status: `ROUNDS 1-5 PASS / INTERMEDIATE AUDITS 1-2 PASS / FINAL AUDIT PASS / EXPLICIT INTEGRATION AUTHORIZATION REQUIRED`
- checkpoint_utc: `2026-07-26T08:35:00Z`

## Source boundary

The original verified Trading Runner source ending at `513726d89dca5a196b5f267c1462e41c1db7f4d2` and its bundle are unavailable. Historical PR #93 remains blocked evidence only. This branch is a transparent clean-room implementation and must never be described as recovered source, byte identity or history identity.

## Objective and boundaries

The completed candidate is a deterministic, paper-only runtime orchestrator over the accepted Modular Strategy v0.6 contracts. It reuses Core, Router, Paper Execution and Position & Exit boundaries without introducing strategy rules or a parallel plan, order, position or portfolio engine.

It does not load private trading credentials and exposes no live-execution path.

## Round result

- Baseline/error discovery: complete.
- Round 1 — correctness and causality foundation: PASS.
- Round 2 — Core pipeline and market-data boundary: PASS.
- Intermediate Audit 1: PASS.
- Round 3 — durable factual routing, journal, checkpoint and startup recovery: PASS.
- Round 4 — health, watchdog, notification isolation and bounded shutdown: PASS.
- Intermediate Audit 2: PASS.
- Round 5 — crash-safe application composition, Core bridge, packaging and central v0.6 acceptance: PASS.
- Final Audit: PASS.

## Completed capabilities

### Runtime and causality

- strict immutable paper-only configuration and canonical hash;
- deterministic lifecycle state machine and exact Core dependency-plan boundary;
- checkpoint restore → factual replay → Paper reconciliation → readiness ordering;
- canonical Router envelope validation and unchanged forwarding;
- duplicate plan idempotency and same-ID/different-content rejection;
- persisted kill switch, cancellation retry and safe reset;
- deterministic module startup and reverse shutdown.

### Market and Core pipeline

- immutable Core `Candle` market-event envelope;
- explicit market/source/instrument/timeframe/freshness/order/gap policy;
- bounded no-drop critical market queue;
- deterministic Core `DependencyPlan`/`MarketView` pipeline;
- same-candle feature propagation and gap invalidation;
- rejection of direct pre-Router execution intents;
- transactional history, feature, index and module-state rollback;
- post-success market cursor commit;
- protective market flow in degraded and kill-switch states.

### Durable factual routing and recovery

- Runner-owned append-only Core `EventStore` journal with SHA-256 chain;
- canonical Core `OrderEvent` and `Trade` factual envelopes;
- crash-consistent factual routing into downstream Position & Exit/portfolio consumers;
- duplicate/conflict protection and restart replay;
- checksummed Core `StateRepository` checkpoint;
- transactional restore of cursors, queues, pipeline, features, modules and namespaced runtime metadata;
- strict configuration/module-graph identity checks;
- narrow safe journal prefix-extension verification;
- deterministic reconciliation for every configured portfolio/instrument scope;
- fresh checkpoint after successful startup reconciliation;
- real Router → Paper adapter and actual Paper report compatibility tests.

### Final market cycle and Core bridge

- durable Paper bar intent before venue mutation;
- atomic Paper processed/pending metadata replacement with rollback on persistence failure;
- conservative reconciliation of ambiguous Paper crash windows;
- persisted stages from event receipt through pre/post-dispatch factual routing and final cursor commit;
- accepted freshness/gap decision persisted for restart;
- prepared plan ID/hash persisted and revalidated before dispatch;
- durable Core journal cursor with exact batch reoffer until commit;
- factual drain both before and after plan dispatch;
- duplicate completed market event restores the original plan/no-plan identity;
- missing or conflicting completion evidence blocks recovery;
- completion-to-checkpoint journal crash window closed;
- public cycle, Core bridge and metadata package exports.

### Supervision

- critical/non-critical health registry with readiness and liveness;
- stale-heartbeat and consecutive-failure semantics;
- watchdog activation through the existing persisted kill-switch boundary;
- activation and checkpoint retry without duplicate kill-switch side effects;
- bounded notification side channel isolated from trading decisions;
- explicit notification failure/drop evidence and counters;
- bounded graceful shutdown and forced-stop fallback.

## Final verification evidence

### Component gate

Exact candidate `8c4aae125ac2a8bd0d2e2f5c07cd252779918f6b`, workflow run `30194713646`:

- Python 3.12: PASS;
- Python 3.13: PASS;
- compileall: PASS;
- Ruff format/lint: PASS (`36 files`);
- strict MyPy: PASS (`19 source files`);
- Trading Runner suite: `110 passed` per version, no failures/errors/skips;
- sdist/wheel build: PASS;
- clean wheel install/public import: PASS with Core `0.4.2`;
- paper-only static boundary: PASS.

Artifacts:

- Python 3.12: ID `8629736129`, digest `sha256:509b15b2acb0887a2bf362ca41866a3852a8336448031fbcdd4ead2217bffa5d`;
- Python 3.13: ID `8629732844`, digest `sha256:08b98deefad542f9c0ba8b215d243db7560baa9057bc35bc02a47aaf034c172d`.

### Central v0.6 executable acceptance

Same exact candidate, workflow run `30194713641`:

- Python 3.12: PASS;
- Python 3.13: PASS;
- `661` recorded JUnit tests per version, no failures/errors/skips;
- frozen Core, Setup → Entry, cross-layer/intrabar/manifest, Trading Runner, complete Paper Execution and Position & Exit profile: PASS;
- full package installation and dependency check: PASS;
- Trading Runner strict MyPy, import smoke, build and `twine check`: PASS;
- exact environment/JUnit/package SHA-256 evidence: uploaded.

Artifacts:

- Python 3.12: ID `8629736641`, digest `sha256:05332152aab2c848f3cc680283d8193178fddbe20ba666b59b73bb36e60fd0cd`;
- Python 3.13: ID `8629736613`, digest `sha256:ce77a5302af7b59d4d01fc419396642b69bfc5afbbb3c616efa42f6ad9520508`.

## Reports

- `docs/trading_runner/ROUND_2_INTERMEDIATE_AUDIT.md`;
- `docs/trading_runner/reconstruction-round-3.md`;
- `docs/trading_runner/reconstruction-round-4.md`;
- `docs/trading_runner/intermediate-audit-2.md`;
- `docs/trading_runner/reconstruction-round-5.md`;
- `docs/trading_runner/final-audit.md`;
- `docs/trading_runner/RECONSTRUCTION_ACCEPTANCE_MATRIX.md`;
- `docs/integration/V0_6_EXECUTABLE_ACCEPTANCE_MATRIX.md`.

## Open findings and residual boundaries

- No open CRITICAL or HIGH finding remains inside the audited paper-only boundary.
- The candidate is clean-room code and not the unavailable historical implementation.
- Signal Bot is optional and outside the current executable-chain gate.
- Production/live behavior, private credentials and deployment are intentionally absent and unauthorized.
- Long-duration infrastructure soak is deployment-stage work and does not justify adding a live path to this component.

## Exact next action

Development work for the accepted Trading Runner paper-only scope is complete.

The next action is an explicit human decision whether to authorize integration of Draft PR #197. Until that decision:

1. keep PR #197 Draft and unmerged;
2. do not auto-merge, tag, release, deploy or publish packages;
3. do not add credentials or live execution;
4. preserve exact candidate/evidence references above;
5. if any source file changes, rerun both the component gate and central v0.6 acceptance on the new exact head before reconsidering integration.

## Do not repeat

Do not search again for the lost bundle unless a new concrete storage location is supplied. Do not write reconstruction code into PR #93. Do not introduce parallel plan, order, position or portfolio contracts. Do not enable live execution, merge, auto-merge, tag, release, deploy or publish packages without explicit authorization. Do not rerun completed gates without a material source, dependency or environment change.
