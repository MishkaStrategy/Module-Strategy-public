# Modular Strategy v0.6 Integration Ledger

Status: STAGED INTEGRATION / FAIL-CLOSED

## Canonical base

- Repository: `mishkacher/Module-Strategy`
- Integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core branch: `core/integration-contract-freeze-v0.4.2`
- Frozen Core PR: #164
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Core contract: `0.4.2`
- Module API major: `1`
- Authoritative Core workflow: `core-v042-quality-ci` run `30074333636`

The integration branch must not absorb changes to Core public contracts unless a reproduced integration or recovery defect is recorded with a focused regression and a complete replacement Python 3.12/3.13 evidence matrix.

## Component ledger

| Layer | Component | Canonical PR | Canonical branch | Validated/evidence SHA | Disposition before integration |
|---|---|---:|---|---|---|
| Core | Modular Core 0.4.2 contract freeze | #164 | `core/integration-contract-freeze-v0.4.2` | `cc7eb494c27eb0fc48dd16026b704bade9f6de3e` | CANONICAL BASE |
| Analytics | Session Range | #26 | `modules/session-range-hardening` | audited runtime `71fae8500a4aeb6cafba3c6aae13020b95d3e3be`; accepted workflow-only head `0ffbc5aa409e7628fff39fe1e80f0db77fc102a3` | INTEGRATED AND LOCALLY GREEN |
| Analytics | Market Regime | #29 | `modules/market-regime-hardening` | fast-tested `4d6a751608466305cf09caf25ecd59515ca2cb49`; accepted workflow-only head `55946d26f6b95c5d0a2b5c8ed9b0dbbd6786f938` | INTEGRATED AND LOCALLY GREEN |
| Analytics | FVG 0.3.0 | #27 | `modules/fvg-hardening` | runtime `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`; docs/config `85d30d96928c9e016919ae5c369959b0b93c5e56` | INTEGRATED WITH CORE 0.4.2 ADAPTER FIXES; LOCALLY GREEN |
| Analytics | Liquidity Sweep | #16 | `modules/liquidity-sweep-deep-optimization` | code `0f7ea73e5926332b4cc9597012dd81a867e8b43a`; evidence `0df1bc4760056acc8573939cff2696b2c0193e9c` | PRE-FLIGHT REQUIRED |
| Analytics | Breakout–Return | #18 | `modules/breakout-return-v1.1.0-hardening` | tested `88ab6a3a76987c54cdb34bc0094e5cd49a4149d6`; accepted docs/workflow head `49bc941ee65a64f87fdbf714127996b1f3982388` | INTEGRATED AND LOCALLY GREEN |
| Adapter | Canonical Setup Adapter | #47 | `modules/canonical-setup-adapter` | tested `67f20e042c5301fe3228998d61d04d9c524517a8`; accepted workflow-only head `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb` | INTEGRATED WITH CURRENT-RUFF COMPATIBILITY FIXES; LOCALLY GREEN |
| Decision | Entry Model | #24 | `modules/entry-model-hardening` | audited `08ee5bc55680d907de2c5ac7959b590eb8f80a22`; accepted workflow-only head `d08ca2957dff1fc697665762e91cc6c297b43ab0` | INTEGRATED WITH DECIMAL PRECISION FIX; LOCALLY GREEN |
| Decision | Protection & Target | #12 | `modules/protection-target` | tested `d03e290e57287f53847f4b93238fbd3722b2e39b`; accepted workflow-only head `02f3a2239e6edce2eb3e92b558925f5ca436da76` | INTEGRATED WITH CURRENT-RUFF/POLICY FIXES; LOCALLY GREEN |
| Decision | Position Scaling | #23 | `modules/position-scaling-hardening` | tested `eaedbdb730b8435197c355c8c69906fe6e2436e7` | PRE-FLIGHT REQUIRED |
| Decision | Risk & Position Sizing | #43 | `modules/risk-sizing-position-adjustments` | tested `11419b106fb8eb37ee0a3e0b5570c7c110d75fab` | PRE-FLIGHT REQUIRED |
| Routing | Decision Router | #95 | `modules/trade-plan-router-v0.5` | evidence `f779e599b361a7b860506980de6b5ae8d0cd1f01` | CORE 0.4.2 COMPATIBILITY EVIDENCE EXISTS |
| Execution | Paper Execution & Reconciliation | #98 | `execution/paper-venue-reconciliation` | integration-ready `57283323c38255c1a259df42a1e1388dcd260483` | AUTHORIZED INTEGRATION CANDIDATE |
| Management | Position & Exit Management | #35 | `modules/position-exit-management-hardening` | verified `b4f414b264f8c4cf1d14d0ebcda83fe0bb1d8dd7` | PRE-FLIGHT REQUIRED |
| Runtime | Trading Runner | #93 | `runtime/trading-runner` | remote branch not yet equal to verified local source | BLOCKED |
| Research | Research Runner | #119 | `research/experiment-runner` | final `97b4cf512b816a24042f07f40314d010fde666a7` | READY, HUMAN APPROVAL REMAINS |
| Research | Results Aggregator | #94 | `research/results-aggregator` | product head `05a616e87bb5cc10a420588b1480deec3dec7792` | CI WORKFLOW PR #154 REQUIRED |

## Explicitly superseded or non-product PRs

- PR #8: historical integration/evidence branch; not a v0.6 merge candidate.
- PR #165: superseded freeze-candidate branch; PR #164 is canonical.
- Temporary transport, export, diagnostic and artifact-only PRs must never be merged into the integration branch.

## Required component pre-flight

Each component must pass, against the exact current integration head:

1. manifest load and entry-point discovery;
2. declared feature inputs/outputs and dependency-plan validation;
3. no duplicate Core DTO or private Core import;
4. configuration and public DTO schema validation;
5. deterministic duplicate/out-of-order behavior;
6. checkpoint snapshot/restore and corrupted-state rejection;
7. closed-bar/no-look-ahead contract;
8. Python 3.12 and 3.13;
9. wheel and normalized-sdist build where applicable;
10. clean, non-editable installation and public-entry-point smoke.

## Integration order

1. Core freeze.
2. Session Range.
3. Market Regime.
4. FVG.
5. Liquidity Sweep.
6. Breakout–Return.
7. Canonical Setup Adapter.
8. Entry Model.
9. Protection & Target.
10. Position Scaling and Risk Sizing.
11. Decision Router.
12. Paper Execution & Reconciliation.
13. Position & Exit Management.
14. Trading Runner.
15. Research Runner and Results Aggregator.

A layer may advance only after the exact integration head has green evidence. No batch merge is allowed when a failure cannot be attributed to a single component.

## Executable acceptance packs

### Analytical chain

- no signal before completed bar;
- no HTF look-ahead;
- identical input produces identical normalized output;
- restart at selected boundaries preserves output;
- duplicate event does not duplicate setup;
- missing or cyclic dependencies fail startup;
- same-bar producer-to-consumer propagation follows dependency order.

### Paper execution chain

- normal limit entry;
- partial fill;
- cancellation;
- expired plan;
- identical duplicate plan;
- same ID with conflicting content;
- stop and target inside one candle;
- synthetic OCO;
- restart between entry fill and protection installation;
- lost acknowledgement;
- duplicate fill event;
- stale mark price;
- market mismatch;
- corrupted checkpoint recovery;
- reconciliation after state divergence.

## Analytics-to-execution contract freeze

The following boundary must be frozen before end-to-end integration:

`ConfirmedSetup -> EntryPlan -> ProtectionPlan -> RiskDecision -> ExecutableTradePlan -> DownstreamPlanEnvelope`

Required evidence includes field presence, Decimal/string units, UTC timestamps, validity interval, market identity, idempotency key, setup/config/version provenance, OCC ordering and typed rejection taxonomy.

Paper Execution must preserve Router canonical payload/content/audit hashes and must not recalculate direction, quantity, risk, stops, targets or sequencing.

## Final dry-run gate

Before any merge to `main`, the release-candidate head must:

- build all release-candidate wheels and normalized sdists;
- install them into empty Python 3.12 and 3.13 environments without editable installs;
- load components only through public entry points;
- run deterministic analytical replay;
- run paper execution replay;
- restart at multiple controlled boundaries;
- restore checkpoints and reconcile factual execution state;
- compare normalized events, plans, orders, fills, positions and equity;
- publish tested SHA, workflow run IDs, artifact IDs/digests, test counts, coverage, limitations and rollback procedure.

## Integration checkpoints

### Session Range — 2026-07-24T15:53:02Z

- Starting integration head: `567b0a193aa05fb2696b5bfd460be12011a622d2`.
- Audited runtime evidence SHA: `71fae8500a4aeb6cafba3c6aae13020b95d3e3be`.
- Integrated source SHA: `0ffbc5aa409e7628fff39fe1e80f0db77fc102a3`.
- Post-evidence delta: one workflow-only commit changing physical
  `macOS/ARM64` routing to the repository workload role
  `[self-hosted, fast]`; no code, tests, packaging or contract delta.
- Merge commit: `6e5225b418e6bb0b30478bacc5f6c17aec7b5fdc`.
- Conflict: `modules/session_range/pyproject.toml`; resolved manually by
  preserving the component's validated 0.2.0 metadata, Core compatibility
  range, public entry point, strict MyPy, Ruff and pytest configuration.
- Scope review: only `modules/session_range/**` and the permanent
  `.github/workflows/session-range-hardening.yml` gate changed; frozen Core
  source and tests did not change.
- Python 3.12.13: compileall, Ruff lint, Ruff format, strict MyPy, 139 component
  tests, 97% coverage, 5 Core contract/harness tests and `pip check` passed.
- Python 3.13.14: the same gate passed with 139 component tests, 97% coverage
  and 5 Core contract/harness tests.
- Packaging: Core 0.4.2 and Session Range 0.2.0 wheels and sdists built; all
  four passed Twine; clean non-editable wheel installation, `pip check`,
  version checks and `modular_strategy.modules/session_range` entry-point
  discovery passed on Python 3.12 and 3.13.
- Deterministic replay, checkpoint/restore, corrupted-state rejection,
  duplicate/out-of-order handling and no-look-ahead are covered by the
  component suite and passed on both interpreters.
- Not run at this checkpoint: full repository suite, remote exact-head CI,
  component security/SBOM workflow and extended performance benchmark. These
  remain later integration/final-gate responsibilities.
- Known limitation: the component workflow still targets pull requests to the
  historical v0.5 integration branch. It remains available through
  `workflow_dispatch`; central v0.6 exact-head validation is owned by the
  integration gate.
- Next component: Market Regime, beginning with live-head/evidence delta
  classification before any merge.

### Market Regime — 2026-07-24T15:58:00Z

- Starting integration head: `606769a3f20e584c9e15eeff2d478049d0bd9540`.
- Product evidence SHA: `4d6a751608466305cf09caf25ecd59515ca2cb49`.
- Integrated source SHA: `55946d26f6b95c5d0a2b5c8ed9b0dbbd6786f938`.
- Post-evidence delta: two workflow-only commits moving fast, full, security
  and performance jobs to `[self-hosted, fast]`, removing physical OS/arch
  assertions and unnecessary repository-wide serialization. No product code,
  tests, packaging or contract changed after the evidence SHA.
- Merge commit: `4e97b4ddc8ce4f1fe495d453579c3089e8d56957`.
- Merge conflicts: none.
- Integration defect fixed: the component's round-28 repository-policy test
  still required obsolete physical runner labels after the workflow-only
  update. The test now requires all four jobs to use `[self-hosted, fast]` and
  rejects `Linux`, `X64`, `macOS`, `ARM64` and `backtest` routing.
- Scope review: only `modules/market_regime/**` and its three workflow paths
  changed; frozen Core and Session Range source were unchanged.
- Python 3.12.13: compileall, Ruff lint/format, strict MyPy, 151 Market Regime
  tests including slow tests, 93% coverage, 5 Core contract/harness tests,
  139 adjacent Session Range tests and `pip check` passed.
- Python 3.13.14: the same gate passed.
- Packaging: Market Regime 1.2.0 wheel and sdist built and passed Twine.
  Combined clean non-editable installation of Core 0.4.2, Session Range 0.2.0
  and Market Regime 1.2.0 passed `pip check`, version assertions and both
  public entry-point discoveries on Python 3.12 and 3.13.
- Determinism, no-look-ahead, checkpoint/restore, schema migration,
  corrupt-state rejection, cross-hash-seed behavior and extended replay are
  covered by the full component suite and passed on both interpreters.
- Not run at this checkpoint: full repository suite, remote exact-head CI,
  fresh security/SBOM generation and the standalone 250k-event benchmark
  workflow. These remain later integration/final-gate responsibilities.
- Next component: FVG at source/evidence head
  `85d30d96928c9e016919ae5c369959b0b93c5e56`; reconfirm its known workflow
  conflict before resolving it.

### FVG 0.3.0 — 2026-07-24T16:06:24Z

- Starting integration head: `201776089b48489f98b266942d5e73bb26fba97e`.
- Runtime evidence SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`.
- Integrated docs/config head: `85d30d96928c9e016919ae5c369959b0b93c5e56`.
- Post-runtime delta: temporary ancestry workflow added then removed, root
  pytest marker synchronization and documentation/evidence updates. No FVG
  runtime source changed after the runtime evidence SHA.
- Merge commit: `750321f5b699b2a7bd34a3be640556845b770e86`.
- Git reported no textual conflict, but manual ownership review found a shared
  workflow conflict: the FVG branch reduced Core CI from Python 3.12/3.13 to
  Python 3.12 only. The frozen Core gate and both interpreters were preserved;
  only its runner selector was updated to `[self-hosted, fast]`.
- The validated focused `fvg-module-ci.yml` and release-validation workflow
  were retained. Workflow routing contains no physical machine selectors.
- Core 0.4.2 compatibility fixes:
  - FVG manifest metadata no longer uses unsupported JSON-Schema-only rules or
    nullable union types; `FvgConfig` continues to enforce nonempty unique
    canonical timeframes and nullable decimal semantics at runtime.
  - the adversarial non-Candle test now verifies the frozen Core `MarketView`
    boundary, which rejects noncanonical candle types before module dispatch.
- Three imported trailing-whitespace defects were normalized so
  `git diff --check` passes.
- Python 3.12.13 and 3.13.14: compileall, Ruff lint/format, strict MyPy, 572 FVG
  tests, 290 adjacent Session Range/Market Regime tests, 5 Core
  contract/harness tests and `pip check` passed.
- Branch coverage: 95% on both interpreters using Coverage 7.15.2. The
  component-pinned Coverage 7.13.3 could not start under the macOS privacy
  sandbox because it attempted to list the protected Documents root; canonical
  pytest still ran with all other pinned FVG dev tools.
- Packaging: FVG 0.3.0 wheel and sdist built and passed Twine. Combined clean
  non-editable installation of Core, Session Range, Market Regime and FVG
  passed `pip check`, public entry-point discovery and FVG construction against
  Core 0.4.2 on Python 3.12 and 3.13.
- Not run at this checkpoint: complete root repository suite, remote exact-head
  CI, fresh security/SBOM generation and extended performance benchmark.
- Next component: Breakout–Return. Compare live head
  `49bc941ee65a64f87fdbf714127996b1f3982388` with tested SHA
  `88ab6a3a76987c54cdb34bc0094e5cd49a4149d6` before any merge.

### Breakout–Return — 2026-07-24T16:10:58Z

- Starting integration head: `e2eb84dc91ea0625703b7980540ddcae68122ee1`.
- Tested evidence SHA: `88ab6a3a76987c54cdb34bc0094e5cd49a4149d6`.
- Integrated source head: `49bc941ee65a64f87fdbf714127996b1f3982388`.
- Post-evidence delta: five documentation/evidence-only commits and one
  workflow-only commit moving the permanent gate to `[self-hosted, fast]` and
  removing unnecessary serialization. No executable code, tests, packaging or
  contracts changed.
- Merge commit: `3a0934020405edc826e7814d1fcc3c69dc736f3f`.
- Conflicts: four add/add root documentation paths. Central Core-owned versions
  were retained; the complete component evidence remains under
  `audits/breakout_return/**`. A redundant component-specific
  `docs/FINAL_AUDIT.md` was omitted to keep the shared root namespace free.
- Imported Markdown hard-break whitespace was normalized so
  `git diff --check` passes.
- Scope review: Breakout source/tests, its package entry point, docs/audits,
  benchmark and permanent component workflow changed. Core source and all
  previously integrated module sources were unchanged.
- Python 3.12.13 and 3.13.14: compileall, Ruff, strict MyPy, 131 Breakout tests,
  91% coverage, 5 Core contract/harness tests, 139 Session Range tests, 151
  Market Regime tests, 572 FVG tests and `pip check` passed.
- Packaging: the updated Core 0.4.2 wheel/sdist containing the Breakout module
  and public entry point built and passed Twine. Combined clean non-editable
  installation with Session Range, Market Regime and FVG passed `pip check`,
  four-entry-point discovery and Breakout checkpoint round-trip on Python 3.12
  and 3.13.
- Test-root limitation: pytest must run Market Regime and FVG in separate
  processes because both suites import an unqualified top-level `conftest`.
  Each canonical suite passed separately; a combined single-process collection
  is not supported.
- Not run at this checkpoint: complete root repository suite, remote exact-head
  CI, fresh security/SBOM generation and benchmark stress.
- Next component: Canonical Setup Adapter. Compare live head
  `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb` with tested SHA
  `67f20e042c5301fe3228998d61d04d9c524517a8` before any merge.

### Canonical Setup Adapter — 2026-07-24T16:17:24Z

- Starting integration head: `aa765ef548de8e492b5fc4ad6d2eff48c7761415`.
- Exact tested evidence SHA: `67f20e042c5301fe3228998d61d04d9c524517a8`.
- Integrated source head: `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb`.
- Post-evidence delta: one workflow-only commit replacing physical
  `macOS/ARM64` routing with `[self-hosted, fast]`; no product code, tests,
  package metadata or contracts changed.
- Merge commit: `f0146477f5ca8efd16107f8e365e82c2f398c464` (source parent
  `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb`).
- Merge conflicts: none.
- Integration compatibility fix: five tuple-form multi-type `isinstance`
  checks were converted to Python 3.12+ union form. This is behavior-preserving
  and satisfies the current central Ruff `UP038` gate without changing Core or
  public adapter contracts.
- Scope review: only `modules/setup_adapter/**` and
  `.github/workflows/setup-adapter-ci.yml` changed; frozen Core and all
  previously integrated component sources remained unchanged.
- Python 3.12.13 and 3.13.14: compileall, Ruff lint/format, strict MyPy,
  65 adapter tests, 97% branch coverage, optimized-runtime tests, 1049 root
  Core/Breakout tests, 139 Session Range tests, 151 Market Regime tests,
  572 FVG tests and `pip check` passed.
- The four real-chain adapter tests used Entry Model 0.2.0 from validated head
  `d08ca2957dff1fc697665762e91cc6c297b43ab0` as a temporary detached test
  dependency only. Entry Model was not included in this merge. They verify
  dependency ordering, no premature plan, deterministic same-bar plan and
  future-data invariance.
- Coverage 7.15.2 supplied the 97% evidence. Coverage 7.13.3 in the FVG
  environment reproduced the known macOS privacy-sandbox startup limitation;
  all non-coverage gates passed there before the current coverage environment
  was used.
- Packaging: Setup Adapter 0.1.0 wheel and sdist built without isolation and
  passed Twine. Clean non-editable wheel installation and public
  `modular_strategy.modules/setup_adapter` entry-point construction passed on
  Python 3.12 and 3.13.
- The first isolated environment bootstrap could not download build
  dependencies while DNS was sandboxed. The final build used already
  provisioned local build dependencies; one temporary `wheel` package download
  was authorized, and no repository dependency constraints changed.
- Not run at this checkpoint: remote exact-head CI, fresh security/SBOM
  generation and extended performance benchmark.
- Next component: Entry Model. Compare live head
  `d08ca2957dff1fc697665762e91cc6c297b43ab0` with validated SHA
  `08ee5bc55680d907de2c5ac7959b590eb8f80a22` before any merge.

### Entry Model — 2026-07-24T16:22:44Z

- Starting integration head: `98b970d1179ea3d1c99e55936ab9afef8f5b3001`.
- Validated evidence SHA: `08ee5bc55680d907de2c5ac7959b590eb8f80a22`.
- Integrated source head: `d08ca2957dff1fc697665762e91cc6c297b43ab0`.
- Post-evidence delta: one workflow-only commit moving all five jobs from
  physical `macOS/ARM64` routing to `[self-hosted, fast]` and removing
  unnecessary per-matrix serialization. No product, tests, metadata or
  contracts changed after validation.
- Merge commit: `96ed6395d3f3f7ab6872d7618c37b7857b3888a3`.
- Merge conflicts: none.
- Current-Ruff compatibility: three tuple-form multi-type `isinstance` checks
  were converted to behavior-equivalent Python 3.12+ union form.
- Reproduced integration defect: `decimal_text()` called
  `Decimal.normalize()` under the default 28-digit context. A Hypothesis case
  combining a roughly 1e9 confirmation price with a high-precision
  retracement exposed irreversible price rounding and BUY/SELL asymmetry after
  feature serialization.
- Fix: canonical Decimal serialization now formats the original finite value
  directly in fixed-point form and strips only insignificant trailing zeros.
  A focused regression preserves all digits beyond the default Decimal
  context. The symmetry property constructs and compares its high-precision
  values under an explicit 80-digit local context, preventing the test fixture
  itself from rounding inputs.
- Scope review: only `modules/entry_model/**` and
  `.github/workflows/entry-model-ci.yml` changed; Core and every earlier
  component source remained unchanged.
- Python 3.12.13 and 3.13.14: compileall, Ruff lint/format, strict MyPy,
  325 Entry Model tests, 99% branch coverage and all 325 optimized-runtime
  tests passed.
- Regression matrix on both interpreters: 1049 root Core/Breakout tests,
  139 Session Range tests, 151 Market Regime tests, 572 FVG tests and
  65 Setup Adapter tests passed.
- A parallel coverage attempt briefly produced an invalid 74% Python 3.13
  report because both interpreters shared and erased the same `.coverage`
  file. The Python 3.13 run was repeated sequentially with an isolated
  `COVERAGE_FILE` and produced the canonical 99% result.
- Packaging: Entry Model 0.2.0 wheel and sdist built without isolation and
  passed Twine. Clean non-editable wheel installation, public
  `modular_strategy.modules/entry_model` discovery and construction passed on
  Python 3.12 and 3.13.
- Not run at this checkpoint: remote exact-head CI, fresh security/SBOM and
  performance stress benchmark.
- Next component: Protection & Target. Compare live head
  `02f3a2239e6edce2eb3e92b558925f5ca436da76` with validated SHA
  `d03e290e57287f53847f4b93238fbd3722b2e39b` before any merge.

### Protection & Target — 2026-07-24T16:49:14Z

- Starting integration head: `90d218bcc0c6632fc34073567bd2447b0b4161e3`.
- Validated evidence SHA: `d03e290e57287f53847f4b93238fbd3722b2e39b`.
- Integrated source head: `02f3a2239e6edce2eb3e92b558925f5ca436da76`.
- Post-evidence delta: one workflow-only commit moving the matrix to
  `[self-hosted, fast]`, removing unnecessary serialization and a
  macOS-specific diagnostic. Product code, tests, packaging and contracts did
  not change after validation.
- Merge commit: `409464e061ef6745caa42ee71c01808a56ec059e`.
- Merge conflicts: none.
- Integration fixes: four behavior-equivalent Python 3.12+ union-form
  `isinstance` changes satisfy current Ruff `UP038`; the stale workflow-policy
  test now requires `[self-hosted, fast]` and explicitly rejects the obsolete
  physical `macOS/ARM64` selector.
- Scope review: only `modules/protection_target/**` and
  `.github/workflows/protection-target-ci.yml` changed. Core and all earlier
  component sources remained unchanged.
- Python 3.12.13 and 3.13.14: compileall, Ruff lint/format, strict MyPy,
  232 component tests and 93% branch coverage passed.
- Regression matrix on both interpreters: 1049 root Core/Breakout tests,
  139 Session Range tests, 151 Market Regime tests, 572 FVG tests,
  65 Setup Adapter tests and 325 Entry Model tests passed.
- Packaging: Protection & Target 1.1.1 wheel and sdist built without
  isolation and passed Twine. Clean non-editable wheel installation and public
  `modular_strategy.modules/protection_target` entry-point construction passed
  on Python 3.12 and 3.13.
- Not run at this checkpoint: remote exact-head CI, fresh security/SBOM and
  extended performance stress.
- Next component: Risk & Position Sizing. Compare live head
  `ea3579b0159ee9269f9213c4b698730153f356ac` with validated SHA
  `11419b106fb8eb37ee0a3e0b5570c7c110d75fab` before any merge.

## Fail-closed merge gate

- [x] Canonical Core SHA selected.
- [x] Clean integration branch created from frozen Core.
- [x] Initial Integration Ledger created.
- [ ] Every selected component has a live validated SHA bound to evidence.
- [ ] Component pre-flight is green on the v0.6 integration head.
- [ ] Python 3.12 integration is green.
- [ ] Python 3.13 integration is green.
- [ ] Clean-wheel/sdist installation is green.
- [ ] Deterministic replay is green.
- [ ] Checkpoint/restore and reconciliation are green.
- [ ] No-look-ahead suite is green.
- [ ] Analytics-to-execution contract freeze is green.
- [ ] Paper Execution acceptance pack is green on the combined head.
- [ ] Trading Runner remote source is synchronized and green.
- [ ] Current integration head has its own complete evidence package.
- [ ] Final PR is mergeable and current with its intended base.

Until every unchecked item is closed, this branch is not authorized for merge to `main`, tag, release, package publication or live execution.
