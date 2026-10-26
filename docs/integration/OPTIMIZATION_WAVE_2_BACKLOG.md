# Optimization Wave 2 — Production SLO and Monitored Components

## Scope boundary

Wave 2 starts only after the Optimization Wave 1 Draft integration candidate is reviewed. This backlog does not authorize production-code changes without a measured bottleneck, and it does not authorize live trading, deployment or private exchange credentials.

## Priority 0 — acceptance prerequisites

### Publish the canonical executable strategy composition

Create one versioned, executable and testable configuration for:

`FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`

Acceptance evidence must include:

- exact module identities and versions;
- YAML/config schema validation;
- immutable source/dataset identity;
- execution timeframes and completed-candle rules;
- fees, funding, slippage and gap policy;
- legacy/streaming A/B digests;
- direct/restarted equivalence;
- fills, trades, fees, funding, final equity and drawdown;
- runtime and peak RSS;
- one-command reproduction instructions.

Do not substitute the negative PR #298 research baseline.

### Establish repository governance

For `main` and `integration/modular-strategy-full-rc`:

- confirm branch protection or repository rulesets;
- require pull requests and independent approval;
- prohibit force pushes and branch deletion;
- require conversation resolution;
- select stable exact-head required checks only after measuring runner availability and check names;
- keep deployment/live authorization outside the integration workflow.

## Priority 1 — production-scale SLOs

Define component-specific SLOs with measured baselines, alert thresholds and evidence retention for:

- Backtest Engine/Core: bars/s, peak RSS, snapshot bytes, restart time, digest equality and fail-closed guard latency;
- Registered Dataset reader: time-to-first-batch, partition-open count, batch-size invariance, suffix-resume latency and close-on-cancel;
- Historical Ingestion: rows/s, peak RSS, spool bytes, checksum reread share, promotion latency, cleanup time and recovery time;
- Test Architecture: collection time, execution time, flake rate, unmapped-file count and runner consumption;
- CI/CD: queue time, job time, cache restore/save time, duplicate exact-SHA runs, artifact count/bytes and retry cause.

SLOs must be tied to named runner profiles and reproducible corpus identities.

## Priority 1 — ingestion follow-up

- Profile the final typed-checksum reread without weakening the checksum trust boundary.
- Evaluate streaming or fused validation/promotion designs with identical typed digest and crash semantics.
- Explain and reduce the observed approximately 11.256% RSS increase.
- Run the optimized path over the 12,968,018-row, 3,865-partition corpus when a safe, reproducible backtester runner is available.
- Add crash, tamper, idempotency and cleanup measurements to the full-corpus evidence.
- Preserve baseline/head paired execution and cache-condition disclosure.

## Priority 1 — recovery and streaming hardening

- Add production-scale registered runtime snapshot support if direct snapshotting of the registered reader becomes necessary; do not conflate it with deterministic cursor replay.
- Measure checkpoint bytes and restart latency across batch sizes 1, 64, 1,024 and 8,192.
- Add fault injection at partition boundaries, Arrow batch boundaries and snapshot/event-sidecar verification steps.
- Verify cancellation and reader closure under timeout, memory guard and application interruption.
- Add long-horizon result-digest and event-order reproducibility checks on the canonical strategy.

## Priority 2 — monitored components

The following components remain `MONITOR` until a measured bottleneck or missing production SLO justifies code changes:

- Research Runner;
- Results Aggregator;
- Paper Execution;
- Trading Runner;
- Signal Bot;
- Trade Plan Router;
- Dataset Registry;
- LocalStorage;
- Market Data API, jobs and control plane;
- Strategy Audit & Validation.

For each component:

1. instrument wall time, CPU, RSS, I/O, queue/backpressure and error rate;
2. define a representative production workload;
3. record a reproducible baseline;
4. set an SLO and a breach threshold;
5. optimize only after a confirmed breach;
6. preserve public APIs and deterministic semantics unless an approved migration exists.

## Priority 2 — CI optimization

- Measure declared versus expanded jobs after Wave 1.
- Quantify exact-SHA duplicate runs and remove only proven duplication.
- Measure cache restore/save overhead before adding caches.
- Confirm ordinary jobs use `fast`, real backtests use `backtester`, and no workflow uses physical runner names.
- Establish retry taxonomy: infrastructure-only retries; substantive failures remain fail-closed.
- Evaluate whether selected owner suites can become required checks without overloading self-hosted runners.
- Keep the `all` suite non-required until duration and capacity data exist.

## Priority 2 — security and packaging

- Make Bandit, detect-secrets, pip-audit, SBOM and package-install evidence consistently content-addressed.
- Verify wheel and sdist clean installs for every publishable package.
- Confirm Twine and metadata checks for release-capable packages.
- Prevent direct PyPI substitution for local monorepo packages.
- Continue zero-tolerance immutable Action pinning unless an exception is explicitly documented and approved.
- Add automated scans for accidental raw datasets, registry bytes, benchmark dumps and local paths.

## Exit criteria for Wave 2

Wave 2 is complete only when:

- the canonical executable strategy composition exists and passes full legacy/streaming/restart equivalence;
- monitored components have production workloads and SLOs;
- only measured SLO breaches result in optimization changes;
- full-corpus ingestion evidence is available or a precise resource blocker is documented;
- repository governance is confirmed;
- CI capacity and required-check choices are evidence-based;
- no live trading, deployment or private credential scope is introduced implicitly.
