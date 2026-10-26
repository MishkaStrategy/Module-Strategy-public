# Automatic Strategy Builder / Strategy Factory — GitHub checkpoint

## Repository identity

- Repository: `MishkaStrategy/Module-Strategy`
- Draft PR: `#218`
- Working branch: `research/automatic-strategy-builder`
- Exact base branch: `integration/modular-strategy-v0.6.0-rc`
- Exact base SHA: `9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8`
- Package: `strategy_factory`
- Safety boundary: research and paper admission only; live trading remains forbidden.

## Materialized scope

The branch contains a deterministic Strategy Factory application with:

- immutable contracts and canonical SHA-256 identities;
- UTC market-data validation, gap/duplicate/order checks and dataset fingerprints;
- instrument profiles, regimes, trusted Module Catalog and Strategy Grammar;
- bounded hypothesis, architecture and parameter-space generation;
- separate `create`, `adapt`, `improve` and `validate` policies;
- Research Runner staged planning, submission, execution, promotion, resume and `FinalTestGrant` integration;
- Results Aggregator publication, Pareto selection and baseline-aware promotion;
- walk-forward, robustness, one-time holdout and simplicity selection;
- hash-bound external-audit contract and fail-closed unavailable-auditor port;
- hash-verified paper-only Strategy Packages;
- durable artifacts, checkpoints, idempotency, budgets and decision logs;
- Backtest Engine configuration and execution adapter;
- Risk Request Composer closing the v0.6 `risk.request` integration boundary.

## Verified quality checkpoint

Permanent self-hosted CI run `30367563503` passed on Python 3.12 and 3.13:

- compile, Ruff and strict MyPy;
- Strategy Factory: 589 tests;
- Risk Request Composer: 6 tests;
- acceptance: 25/25;
- branch coverage: 91%;
- Bandit and tracked-source safety;
- six wheel builds and clean-wheel smoke.

Hardening run `30367564002` passed:

- 41 fault/corruption tests;
- 10 × 4 restart/resume/idempotency checks;
- bounded performance and memory gates;
- paper-only and final-audit fail-closed boundaries.

Extended restart soak run `30367810860` passed:

- 100 cold processes;
- 400/400 crash/restart/idempotency checks;
- deterministic hash seeds 1–100;
- 79.718079167 seconds under a 1,800-second bound.

The real publication chain reaches `REGISTERED`; identical replay returns `DUPLICATE`.

## Compatibility fixes

- Research Runner explicitly accepts the official Backtest Engine evidence files `environment.json` and `SHA256SUMS.txt` while preserving closed inventory validation.
- Results Aggregator explicitly supports Core export versions `0.4.0` and `0.4.2`; unknown patch versions remain rejected.

## Clean full-RC integration checkpoint

Draft validation PR `#282` preserves the ordinary source merge from PR `#218` while keeping `main` and the frozen Core branch unchanged.

The clean candidate now includes:

- the independently owned `strategy-audit-port-v1` implementation already accepted in full RC;
- content-addressed Factory audit envelopes backed by authoritative Dataset Registry lineage and Results Aggregator package hashes;
- strict candidate, configuration, experiment, period, evidence-hash and reference identity checks;
- paper-only execution assumptions and decimal-string risk-policy transport;
- a bridge that canonicalizes the independent Auditor decision before Factory decoding;
- a real SQLite/HMAC Auditor restart-and-replay E2E test;
- hardening and 100-process soak workflows that install and exercise the real independent Auditor.

Exact transport transaction `30508142594` passed Ruff, strict MyPy, the real SQLite/HMAC E2E, the complete Factory suite and the branch-coverage gate. Its self-removing product commit is `57053a1d1210e19c24f8673cb6f5e26cf81389e4`; temporary writer files are absent from that tree.

Exact matrix at `b0751d923b2e5698f587c2e16fb404c6074d303e` passed Factory Python 3.12/3.13 including seven-wheel clean-install smoke, Core Python 3.12/3.13, Fast, actionlint, runner routing, environment policy, Research Runner, Results Aggregator and the 100-process restart soak. The hardening correctness, corruption and restart suites also passed. Its original storage benchmark used absolute wall-clock limits and therefore measured managed-runner filesystem latency rather than only product overhead.

The hardening workflow remains on the canonical `[self-hosted, fast]` capability. Durable artifact and SQLite workloads are measured against equivalent direct reference implementations on the same temporary filesystem, with fail-closed overhead-ratio limits of 2.5 and independent runaway wall-clock bounds. Run `30511374283` measured artifact overhead at `1.2925811985`, SQLite overhead at `0.9065520002`, artifact product time at `15.0394 s`, SQLite product time at `105.2811 s` and peak traced memory at `733587` bytes; all portable I/O and memory gates passed.

That run failed only because the 10,000-operation canonical CPU timer was executed under `tracemalloc` and reported `33.1118 s` against the unchanged `30 s` limit. Timing and memory instrumentation are now separated: the 10,000-operation CPU timing remains bounded at `30 s`, while a separate 1,000-operation canonical sample runs under `tracemalloc` before the durable workloads. No acceptance threshold was raised.

The remaining integration gate is a fresh synchronize-triggered matrix on the current clean candidate. Full RC must not move until every required exact-head job, including calibrated hardening, is green.

## Release-gate state

The module is **not release-ready** and this PR must remain Draft. Remaining blockers:

- issue #250 must deliver an independently owned Strategy Audit & Validation component;
- paper-runtime admission E2E must use that real auditor;
- a longer soak must run under the intended staging/deployment topology;
- the final independent release audit must pass.

The first two historical blockers above are implemented in the clean full-RC candidate, but they are not accepted until the fresh exact-head integration matrix completes successfully.

## Continuation rule

Resume from this file, issue #250, Draft PR #218 and Draft validation PR #282. Preserve exact base identity and ownership boundaries. Do not create a second backtest engine, results registry, risk engine, execution engine or live adapter. Do not merge, tag, release, deploy or enable live trading without explicit authorization and a passed release gate.
