> Historical snapshot preserved during the 2026-10-02 release-readiness audit. Current plan: `ROADMAP.md` and `.github/HQ_CRITICAL_PATH.md`.

# Modular Strategy v0.6 release checklist

Status legend:

- `[x]` complete for the canonical staged paper-only v0.6 checkpoint;
- `[~]` accepted for staged integration but not sufficient for a stable production/live release;
- `[ ]` blocking a stable production/live release.

## Canonical identity

- [x] Canonical staging branch: `integration/modular-strategy-v0.6.0-rc`.
- [x] Exact checkpoint: `bc49db40726ec625d456c6c37265ef63b1c74b75`.
- [x] Canonical Draft PR: #168.
- [x] Frozen Core branch: `core/integration-contract-freeze-v0.4.2`.
- [x] Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`.
- [x] Integration history uses ordinary merge commits; no squash, rebase, force-push or history rewrite.

## Source and architecture

- [x] Core remains strategy-independent and does not branch on concrete strategy modules.
- [x] Modules load through public imports and/or `modular_strategy.modules` entry points.
- [x] Duplicate producers, missing dependencies, incompatible manifests and dependency cycles fail preflight.
- [x] Core 0.4.2 public-contract snapshot is machine-readable and enforced in source, wheel and normalized sdist.
- [x] Mandatory paper executable chain is integrated:

  `market data → analytical modules → setup adapter → entry → protection/targets → risk/scaling → router → paper execution → position/exit → trading runner`

- [x] Optional Signal Bot is configuration-controlled and is not a second execution engine.
- [x] Wave Analysis and Liquidity Sweep are independently owned analytical feature producers.
- [~] Module API major remains `1.0 draft`; the Core 0.4.2 snapshot is frozen for v0.6 integration, not yet declared a permanent stable-major API.

## Integrated components

- [x] Session Range.
- [x] Market Regime.
- [x] Fair Value Gap.
- [x] Breakout–Return.
- [x] Wave Analysis.
- [x] Liquidity Sweep.
- [x] Canonical Setup Adapter.
- [x] Entry Model.
- [x] Protection & Target.
- [x] Risk & Position Sizing.
- [x] Position Scaling & Adjustment.
- [x] Decision Router & Trade Plan Composer.
- [x] Paper Execution & Venue Reconciliation.
- [x] Position & Exit Management.
- [x] Backtest Engine.
- [x] Results Aggregator & Experiment Registry.
- [x] Research Runner & Parameter Optimization.
- [x] Trading Runner.
- [x] Optional Signal Bot.

## Configuration and temporal correctness

- [x] Unknown configuration keys and invalid parameter schemas fail closed.
- [x] UTC-aware candles and completed-bar semantics are enforced.
- [x] Duplicate, conflicting, out-of-order and data-gap paths are covered.
- [x] Higher timeframes are generated only from completed base bars.
- [x] Session/DST boundaries are covered.
- [x] Executable acceptance proves no fill on the confirmation candle.
- [x] Analytical modules have point-in-time/no-look-ahead gates.

## State, recovery and reconciliation

- [x] Integrated modules support validated checkpoint capture/restore against frozen Core 0.4.2.
- [x] Checkpoint schema, checksum, corruption, size and transactional-restore controls pass.
- [x] Deterministic replay/restart equivalence passes for Core and component-owned recovery suites.
- [x] Paper Execution persists plan idempotency, order projections, blocked scopes, OCO/deferred-protection state and Core EventStore attestation.
- [x] Restored paper scopes reject new plans until order and position reconciliation succeeds.
- [x] Paper tests cover pending-order/open-position state, stale/duplicate events, lost acknowledgements, partial fills, synthetic OCO and reconciliation failures.
- [x] Trading Runner owns checkpoint sequencing, recovery gates, failure supervision and controlled shutdown.
- [ ] Restart/reconciliation against an actual external exchange account with real pending orders.
- [ ] Restart/reconciliation against an actual external exchange account with a real open position.
- [ ] Long-duration staging soak with process crashes, network partitions and repeated recovery cycles.

## Execution and risk

- [x] Market, limit, stop-market, stop-limit and take-profit lifecycles are tested.
- [x] Partial fills, reduce-only, cancellation, expiry, fees and slippage are tested.
- [x] Same-bar ambiguity and gap-through-stop policies are explicit and tested.
- [x] Contract size, tick/step rounding, notional and accounting invariants pass.
- [x] Entry Model is integrated and cross-audited.
- [x] Protection & Target is integrated and cross-audited.
- [x] Risk & Position Sizing is integrated and cross-audited.
- [x] Position Scaling is integrated and cross-audited.
- [x] Position & Exit Management is integrated and cross-audited.
- [x] Decision Router emits canonical immutable `ExecutableTradePlan` evidence.
- [x] Concrete deterministic paper execution coordinator exists over the Core paper venue.
- [x] Paper venue idempotency, reconciliation and fail-closed blocked-scope behavior are tested.
- [ ] Production live exchange adapter.
- [ ] Private REST/WebSocket authentication and signing boundary.
- [ ] Production kill-switch test against a real external venue.
- [ ] Portfolio-level multi-instrument correlation/exposure service.

## Research and evidence

- [x] Modular Backtest Engine is integrated.
- [x] Results Aggregator and immutable experiment registry are integrated.
- [x] Research Runner supports deterministic planning, persistence, recovery and reproducibility.
- [x] Fast CI uses `[self-hosted, fast]`; historical simulations use `[self-hosted, backtest]`.
- [x] Component and central Python 3.12/3.13 gates are green on the canonical checkpoint.
- [x] Full v0.6 executable acceptance is green on the canonical checkpoint.
- [x] Security scans, dependency audits and SBOM generation are present in component-owned gates.
- [x] Reproducible wheel/sdist procedures and SHA-256 evidence are retained.
- [x] Long component stress gates include restart/recovery and bounded-state checks.
- [ ] Strategy selection approved from untouched out-of-sample evidence for a production deployment.
- [ ] Exchange-specific calibration, funding, latency and liquidity assumptions validated in staging.

## Canonical post-merge evidence

All ordinary post-merge workflows passed on `bc49db40726ec625d456c6c37265ef63b1c74b75`:

- Session Range — `30215827078`;
- FVG — `30215827061`;
- Market Regime — `30215827093`;
- Backtest Engine — `30215827071`;
- Breakout–Return — `30215827031`;
- v0.6 executable acceptance — `30215827042`;
- Protection & Target — `30215827056`;
- Research Runner — `30215827091`;
- Results Aggregator — `30215827064`;
- Liquidity Sweep — `30215827054`;
- Paper Execution — `30215827060`;
- Core CI — `30215827043`;
- Trading Runner — `30215827063`;
- Wave Analysis — `30215827066`.

Post-merge Liquidity evidence:

- Python 3.12: `sha256:801e35335fe37f373bbe71e997ca28e0609facdc9b5eac1d30c42e226fa91f2f`;
- Python 3.13: `sha256:8894ae63e4c2fe551ccc68fe247a08e5cfdfded2e01821dd4ede3d1c04e976d3`.

Release-only, heavy-only and explicit security-dispatch workflows correctly remained skipped when their event conditions were not met.

## Packaging and supply chain

- [x] Compileall, Ruff and strict MyPy gates are green.
- [x] Wheel/sdist build and clean-install checks are present.
- [x] Deterministic sdist normalization has path-safety and idempotency coverage.
- [x] Bandit and component-scoped secret scans are fail closed.
- [x] Dependency vulnerability audits are green on validated candidates.
- [x] CycloneDX SBOMs and artifact checksum manifests are generated.
- [ ] Stable release artifacts built from an approved release commit.
- [ ] Signing/attestation policy for stable release artifacts.

## Operations and documentation

- [x] Component architecture, recovery and validation evidence is documented.
- [x] Canonical staging checkpoint and integration history are recorded in PR #168.
- [x] Paper-only and no-credentials boundaries are explicit.
- [ ] Production paper deployment runbook.
- [ ] Live deployment and rollback runbook.
- [ ] Incident response, exchange outage and degraded-data runbook.
- [ ] Secrets provisioning/rotation procedure.
- [ ] Monitoring, alerting, SLO and long-duration soak evidence.
- [ ] Unified application CLI covering the approved production operations surface.
- [ ] Final stable Module API reference after downstream/live freeze.

## GitHub governance

- [x] Canonical v0.6 integration PR remains open and Draft.
- [x] Frozen Core and `main` remain unchanged by staged component integration.
- [x] Exact-head and post-merge evidence is recorded.
- [ ] Human integration/release review completed.
- [ ] Policy decision to merge the staged integration into a release branch or `main`.
- [ ] Stable version, tag and GitHub Release selected.

## Decision

The deterministic **paper-only v0.6 staged integration is complete and green** at `bc49db40726ec625d456c6c37265ef63b1c74b75`.

A stable production/live release remains blocked by the external live venue adapter, real-venue recovery and kill-switch validation, staging soak, operational runbooks, secrets/monitoring procedures, release approval and stable API/release governance. Do not create a stable tag, GitHub Release, publish production packages or enable live execution until those blockers are closed.