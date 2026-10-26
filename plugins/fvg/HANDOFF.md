# FVG 0.3.0 — Handoff

## Current status

**Development complete; exact-head release validation green; integration merge pending separate authorization.**

- Repository: `mishkacher/Module-Strategy`
- Integration base: `integration/modular-strategy-v0.5.0`
- Pinned Core/base SHA: `b4860c94dafb1d398cb36088d16c32bfcd065fc4`
- Component branch: `modules/fvg-hardening`
- Authoritative Draft PR: `#27`
- Fully validated FVG source/checkpoint SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`
- Package/public API version: `0.3.0`
- State schema: `4`; readers migrate schemas `1`, `2` and `3`
- Public import and entry point: `modular_strategy_fvg:FvgModule`

The current branch may contain later documentation or shared-root marker commits. Determine its exact tip from PR #27. No post-validation commit is allowed to change FVG runtime or tests without new evidence.

## Component responsibility

The FVG module is an analytical producer. It:

- detects strict confirmed three-candle bullish and bearish FVGs on completed bars;
- publishes deterministic active-zone and lifecycle features;
- evaluates confirmed zones with typed filter traces and metrics;
- publishes causal pre-FVG candidates and deterministic resolution outcomes;
- maintains bounded single-symbol state with duplicate/history conflict detection;
- checkpoints and atomically restores schema-4 state.

It does not size positions, create orders, manage execution, mutate portfolio state or implement downstream entry/risk/protection behavior.

## Materialized runtime

The active implementation includes split init, state, restore, on-bar, runtime, processing, feature-boundary and support layers. `module_restore.py` and `module_onbar.py` are present and active. Adapter boundaries canonicalize candle identity, UTC timestamps, history, evaluation and feature construction.

Important contracts:

- no look-ahead or use of incomplete confirmation candles;
- strict UTC/timeframe validation;
- full OHLCV duplicate identity and immediate retained-history conflict detection;
- deterministic lifecycle contact, fill, invalidation and expiry transitions;
- causal pre-FVG evaluation with persistent candidates;
- strict schema-1/2/3 to schema-4 migration;
- atomic restore and bounded checkpoint size.

## Authoritative evidence

Release workflow: `fvg-0.3.0-release-validation`
Run: `30036881793`
Exact SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`

Python 3.12 and 3.13 both passed all 17 release gates:

- canonical source and clean source snapshot;
- compileall, Ruff and strict MyPy;
- runtime, full and optimized-runtime tests;
- branch-aware coverage `>=95%`;
- benchmark, memory and restart digest checks;
- reproducible wheel/sdist and clean installation;
- Bandit, detect-secrets, pip-audit and CycloneDX 1.6 SBOM.

Per interpreter: `572/572` full tests and `7/7` runtime tests passed with failures/errors/skips `0/0/0`. Coverage was 95% over 3,273 statements and 1,498 branches.

Artifacts:

- Python 3.12: `8575668567`
- Python 3.13: `8575665269`
- formatter: `8575610563`

Cross-version distribution SHA-256:

- wheel: `6dc1a19fa653d962d27832237c01b9a52049e23bd0704fcaddf47004f8db685d`
- sdist: `2110e6b0f939af7a8e9155294151092530eeff088718857cbb2de166f028331a`

Post-cleanup focused validation of branch head `0ac504881b696199a55ac4621489918f6f00b9f5` passed compile, Ruff, strict MyPy and `572/572` tests in run `30074288827`; artifact `8589234852`.

## Integration state

The branch is not yet an ancestor-descendant continuation of the current integration base. A no-push dry-run ordinary merge was executed and aborted:

- run: `30059979281`
- artifact: `8584177590`
- exactly one conflict: `.github/workflows/fvg-module-ci.yml`
- deterministic resolution: retain the component branch's validated isolated FVG focused workflow
- no conflict or content change under `plugins/fvg/**`

## Exact next action

Only after direct integration-owner authorization:

1. create an ordinary two-parent merge of `b4860c94...` into `modules/fvg-hardening`;
2. resolve only `.github/workflows/fvg-module-ci.yml` by retaining the validated component version;
3. confirm no unexpected FVG source/test changes;
4. run the focused Python 3.12 FVG gate on the resulting exact head;
5. keep PR #27 Draft until separately authorized for integration review.

Do not force-push, rebase, rewrite history, merge PR #27, tag, release, publish a package or declare live-trading readiness without separate explicit authorization.
