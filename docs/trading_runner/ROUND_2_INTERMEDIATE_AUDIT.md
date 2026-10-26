# Trading Runner reconstruction — Round 2 and Intermediate Audit 1

## Identity

- Repository: `mishkacher/Module-Strategy`
- Accepted base: `integration/modular-strategy-v0.6.0-rc`
- Exact base SHA: `c2df6430234ed13d2cbdc08cf109274a1e737c1d`
- Reconstruction branch: `runtime/trading-runner-reconstruction-v0.6`
- Canonical Draft PR: #197
- Audited code head: `e0be6c08ebdd9900e719f0fd13cdf38db1e7fe6c`
- Fast gate run: `30189653310`

This is a clean-room reconstruction and is not source-, tree- or history-identical to the unavailable historical Trading Runner head `513726d...`.

## Round 2 scope delivered

- immutable market identity configuration bound to market, instruments, sources and base timeframe;
- canonical Core `Candle` market-event envelope with source sequence, availability and receive timestamps;
- strict market/source/instrument/timeframe/closed-candle validation;
- Core-enforced OHLCV finite/geometry validation;
- future, stale, duplicate, identity-conflict, out-of-order and explicit gap handling;
- bounded critical queue with no-drop fail-closed overflow;
- deterministic Core `DependencyPlan` execution over immutable `MarketView` snapshots;
- same-candle feature propagation through a copied Core `FeatureStore`;
- gap-triggered feature invalidation;
- pre-Router rejection of direct order, cancellation, position-update and close intents;
- lifecycle-bound ingestion that blocks pre-start input but continues protective market flow in `DEGRADED` and `KILL_SWITCH_ACTIVE` states;
- public package exports and exact Core 0.4.2 runtime dependency;
- package README and clean wheel installation.

## Independent audit findings

### A1 — market cursor committed before pipeline success

- Severity: **HIGH**
- Risk: a module failure after validation could mark the event processed; retry would be classified as duplicate and skip the pipeline.
- Fix: split validation into non-mutating `inspect()` and post-success `commit()`; service commits ordering evidence only after the transactional pipeline returns.
- Regression: transient module failure, state rollback, same-event retry and successful processing.
- Status: **CLOSED**

### A2 — partial pipeline state survived downstream module failure

- Severity: **HIGH**
- Risk: history/index, published features and earlier module state could advance although the event batch failed.
- Fix: process against copied history and `FeatureStore`; capture every module snapshot; commit pipeline state only after all modules pass; restore module snapshots in reverse order on failure.
- Regression: producer publishes, downstream module fails once, feature/history/index/module state remain unchanged, retry starts at index zero and succeeds once.
- Status: **CLOSED**

### A3 — degraded runtime stopped all market ingestion

- Severity: **HIGH**
- Risk: after a gap or stale event, Position & Exit protection would lose fresh market input even though new entries were correctly blocked.
- Fix: allow ingestion/processing in `RUNNING`, `DEGRADED` and `KILL_SWITCH_ACTIVE`; entry permission remains limited to healthy `RUNNING` with inactive kill switch.
- Regression: degraded runtime accepts market events while entry permission remains false.
- Status: **CLOSED**

### A4 — freshness was measured from local receive time

- Severity: **HIGH**
- Risk: a very old candle received recently could pass freshness validation and enter the strategy pipeline.
- Fix: event age is measured from authoritative `available_at`; `received_at` remains transport/future-skew evidence.
- Regression: delayed old candle with recent receipt is rejected as stale.
- Status: **CLOSED**

### A5 — known duplicate was checked after stale policy

- Severity: **MEDIUM**
- Risk: a delayed exact duplicate could degrade runtime instead of being idempotently ignored.
- Fix: validate identity scope, then resolve known event identity before freshness/order checks.
- Regression: exact known duplicate remains duplicate after the freshness window.
- Status: **CLOSED**

### A6 — transport receipt time polluted source-event identity

- Severity: **MEDIUM**
- Risk: the same source event delivered again with a different local receipt timestamp could be treated as content conflict.
- Fix: canonical source payload hash excludes local `received_at` while retaining event ID, source, market, sequence, availability, metadata and full candle content.
- Regression: same source event with changed receipt timestamp is deduplicated; changed candle content still fails with typed identity conflict.
- Status: **CLOSED**

## Audit invariants verified

- no market event enters the pipeline before an admissible runtime state;
- no invalid/stale/future/out-of-order event reaches modules;
- exact duplicate delivery does not execute modules twice;
- same event ID with changed source content fails closed;
- critical queue overflow preserves all already queued events and degrades runtime;
- gap evidence is explicit, invalidates symbol features and blocks entry permission;
- module order is the exact Core dependency plan;
- features emitted by an earlier module are visible to later modules on the same accepted candle;
- a module cannot bypass Decision Router and Paper Execution with direct execution intents;
- failed pipeline batches do not commit market identity, history, index, features or module snapshot state;
- `DEGRADED` and kill-switch states preserve market flow for protective downstream processing;
- package source contains no live-execution adapter, private credential surface or alternate trading state engine.

## Exact verification evidence

The permanent component gate passed on both Python 3.12 and Python 3.13:

- compileall: PASS;
- Ruff format: PASS (`17 files already formatted`);
- Ruff lint: PASS;
- strict MyPy: PASS (`10 source files`);
- component, Core-contract, Router-contract and market/pipeline regression suite: `50 passed` per Python version;
- wheel and sdist build: PASS;
- package README included in sdist metadata: PASS;
- clean environment installation of Core 0.4.2 and Trading Runner wheel: PASS;
- public import smoke: PASS;
- paper-only static source boundary: PASS.

Artifacts:

- Python 3.12: ID `8628121090`, digest `sha256:a23fdba33533790ab46061584220814fab8bff23aa068e508dd07c22462e01cc`;
- Python 3.13: ID `8628124462`, digest `sha256:c1f9ce00e3290d7f1eb5786fb7c58e53f6e64b58bc94184c9aac1ad64e7a64ed`.

The separately triggered repository-wide `core-ci` again failed in self-hosted `actions/setup-python@v5` before repository installation or tests. It remains classified as runner infrastructure evidence and is not counted as a Trading Runner product failure or success.

## Remaining risks outside Round 2

- accepted market cursor/history/feature/module state are still in-memory and not crash-durable;
- execution, fill and Position & Exit event routing are not implemented;
- event journal chain, checkpoint checksum, corruption detection and restart replay remain absent;
- process/task ownership and cross-domain concurrency remain to be hardened;
- watchdog, notification isolation, operational metrics, forced shutdown and resource escalation remain future rounds;
- no full Router → Paper Execution → Position & Exit end-to-end chain exists yet.

## Intermediate Audit 1 verdict

`PASS — ROUND 2 CONTRACT AND MARKET PIPELINE SCOPE COMPLETE`

No open critical/high finding remains inside the Round 2 acceptance boundary. This does not authorize integration. Round 3 must establish execution/position routing and crash-consistent durable recovery before readiness can advance.
