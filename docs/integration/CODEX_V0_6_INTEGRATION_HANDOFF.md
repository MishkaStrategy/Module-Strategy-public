# Modular Strategy v0.6 — Codex Integration Handoff

## Authority and safety

This document prepares the executable integration continuation. It does not authorize a merge to `main`, a release, package publication, live execution, auto-merge, force-push, rebase, or history rewrite.

Maximum authorized runtime scope: deterministic paper execution.

## Canonical integration target

- Repository: `mishkacher/Module-Strategy`
- Integration branch: `integration/modular-strategy-v0.6.0-rc`
- Draft PR: `#168`
- Frozen Core branch: `core/integration-contract-freeze-v0.4.2`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Integration branch was created directly from that SHA.

Before making any change, fail unless:

```bash
test "$(git rev-parse core/integration-contract-freeze-v0.4.2)" = "cc7eb494c27eb0fc48dd16026b704bade9f6de3e"
git switch integration/modular-strategy-v0.6.0-rc
git status --short
git merge-base --is-ancestor cc7eb494c27eb0fc48dd16026b704bade9f6de3e HEAD
```

The worktree must be clean. Do not use PR `#8` as the integration source or final merge candidate.

## Selected integration candidates

Use the following branches and evidence identities. Re-read live PR metadata before integrating each component. If a branch tip moved, classify the delta before accepting it.

| Order | Component | Canonical PR | Branch | Authorized product/evidence identity | Current observed PR head | Disposition |
|---:|---|---:|---|---|---|---|
| 1 | Session Range | #26 | `modules/session-range-hardening` | audited `71fae8500a4aeb6cafba3c6aae13020b95d3e3be` | `0ffbc5aa409e7628fff39fe1e80f0db77fc102a3` | inspect post-audit delta; analytical |
| 2 | Market Regime | #29 | `modules/market-regime-hardening` | fast-CI `4d6a751608466305cf09caf25ecd59515ca2cb49` | `55946d26f6b95c5d0a2b5c8ed9b0dbbd6786f938` | conditional; external heavy-gate history must be classified |
| 3 | FVG | #27 | `modules/fvg-hardening` | release `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`; focused docs checkpoint `85d30d96928c9e016919ae5c369959b0b93c5e56` | `85d30d96928c9e016919ae5c369959b0b93c5e56` | ready; known workflow conflict |
| 4 | Breakout–Return | #18 | `modules/breakout-return-v1.1.0-hardening` | tested `88ab6a3a76987c54cdb34bc0094e5cd49a4149d6` | `49bc941ee65a64f87fdbf714127996b1f3982388` | inspect documentation-only/post-test delta |
| 5 | Canonical Setup Adapter | #47 | `modules/canonical-setup-adapter` | exact tested `67f20e042c5301fe3228998d61d04d9c524517a8` | `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb` | inspect post-test delta |
| 6 | Entry Model | #24 | `modules/entry-model-hardening` | validated `08ee5bc55680d907de2c5ac7959b590eb8f80a22` | `d08ca2957dff1fc697665762e91cc6c297b43ab0` | inspect post-validation delta |
| 7 | Protection & Target | #12 | `modules/protection-target` | validated `d03e290e57287f53847f4b93238fbd3722b2e39b` | `02f3a2239e6edce2eb3e92b558925f5ca436da76` | inspect post-validation delta |
| 8 | Risk & Position Sizing | #43 | `modules/risk-sizing-position-adjustments` | validated `11419b106fb8eb37ee0a3e0b5570c7c110d75fab` | `ea3579b0159ee9269f9213c4b698730153f356ac` | inspect post-validation delta |
| 9 | Position Scaling | #23 | `modules/position-scaling-hardening` | validated `eaedbdb730b8435197c355c8c69906fe6e2436e7` | `d5cb7975dda0beba113ec32971384b4bc0e88ed8` | inspect post-validation delta |
| 10 | Decision Router | #95 | `modules/trade-plan-router-v0.5` | validation publisher `f779e599b361a7b860506980de6b5ae8d0cd1f01` | same | ready; must preserve `DownstreamPlanEnvelope` exactly |
| 11 | Paper Execution | #98 | `execution/paper-venue-reconciliation` | exact integration-ready `57283323c38255c1a259df42a1e1388dcd260483` | same | ready; paper-only |
| 12 | Position & Exit Management | #35 | `modules/position-exit-management-hardening` | verified connector head `b4f414b264f8c4cf1d14d0ebcda83fe0bb1d8dd7` | `8ba3396068efd0ba2252c3b488be5d296f4f441a` | inspect post-verification delta |
| 13 | Backtest Engine | #55 | `engine/modular-backtest` | tested product `2198df1559d91a7ad2982a86bab4ddde152eb6fe` | `acbf6fc12d7342e46c0cb1a069f44f0ed76428e9` | non-mergeable PR; classify workflow-only and product deltas |
| 14 | Results Aggregator | #94 | `research/results-aggregator` | validated tree `ae2cd815ac05453a9b7dbba5f5fbdace90db8447`; branch commit `05a616e87bb5cc10a420588b1480deec3dec7792` | same | ready after CI-gate ownership is reconciled |
| 15 | Research Runner | #119 | `research/experiment-runner` | final publication `97b4cf512b816a24042f07f40314d010fde666a7` | same | ready, human architecture approval remains release gate |
| 16 | Trading Runner | #93 | `runtime/trading-runner` | verified local-only `94df38c8a328326d482ef3c814fda38c16c962fc` | remote `bac69562cce00ac68e45582eebfc522933541a86` | BLOCKED until remote tree is synchronized and proven equivalent |

Do not integrate Signal Bot, Wave Analysis, Liquidity Sweep, or other optional analytical components in the first executable chain unless the integration ledger is intentionally expanded and the same staged gates are applied.

## Required method

The component branches diverged from frozen Core. For example, Decision Router and frozen Core share merge base `5e03b933207a99940244634f49a756d27888a5c3`, while the two tips have independently advanced. Do not copy a branch tree wholesale over the integration branch.

Use ordinary history-preserving merges, one component at a time:

```bash
git fetch origin --prune
git switch integration/modular-strategy-v0.6.0-rc
git pull --ff-only origin integration/modular-strategy-v0.6.0-rc

git merge --no-ff --no-commit <selected-ref>
# inspect every conflict and the complete staged diff
git diff --check
git diff --cached --stat
git diff --cached --name-status
# run the component pre-flight below
git commit -m "integrate(<component>): stage validated candidate onto Core 0.4.2"
git push origin integration/modular-strategy-v0.6.0-rc
```

If a current branch tip contains unclassified changes after its authoritative evidence SHA, first create a temporary local ref at the evidence SHA and integrate that immutable SHA. Do not silently treat a newer branch tip as validated.

Do not use squash merges for component integration. Preserve parents and provenance. Do not force-push.

## Conflict policy

1. Core contracts, DTO ownership, checkpoint semantics, lifecycle and integration harness from frozen Core win unless a reproduced integration defect proves a focused Core change is necessary.
2. Component-owned source/tests/docs under its namespace normally win when they do not duplicate Core ownership.
3. Workflow conflicts must be resolved against current repository runner policy: movable role labels such as `[self-hosted, fast]` or `[self-hosted, backtest]`; no physical runner identity routing.
4. Never weaken tests, coverage gates, strict typing, security gates, exact-head assertions, paper-only restrictions, or fail-closed behavior merely to make integration green.
5. Router owns plan composition and canonical evidence. Paper Execution must consume `DownstreamPlanEnvelope` without recalculating side, quantity, risk, stop, target, ordering, hashes, or OCC evidence.
6. Core remains the authoritative broker simulation, order/fill/position and persistence substrate. Paper Execution must not introduce a second broker, position ledger, or event bus.
7. Trading Runner is integrated last and only after its remote source equals the verified local checkpoint or a newer independently validated checkpoint.

Known FVG dry-run conflict: `.github/workflows/fvg-module-ci.yml`. Prior reviewed resolution was to retain the component's validated isolated focused workflow. Reconfirm that neither side changed before reusing this resolution.

## Pre-flight after every component

Run from a clean worktree and save exact commands/results in the ledger:

```bash
python3.12 -m compileall -q src modules plugins applications
python3.13 -m compileall -q src modules plugins applications
python3.12 -m pytest -q tests/test_core_contract_freeze.py tests/test_core_module_integration_harness.py
python3.13 -m pytest -q tests/test_core_contract_freeze.py tests/test_core_module_integration_harness.py
```

Then install and test the newly added component on both interpreters using its own `pyproject.toml` and canonical test directory. Run Ruff and strict MyPy with the component's committed configuration. Do not invent exclusions.

At each checkpoint verify:

- frozen Core snapshot still passes;
- package/entry-point discovery is unambiguous;
- no duplicate public DTO or registry implementation appeared;
- no look-ahead or future-data dependency was introduced;
- checkpoint state restores deterministically;
- changed files match the intended component plus explicitly reviewed shared files;
- `git diff --check` is clean.

## Executable-chain acceptance sequence

After layers 5–12 are present, add deterministic cross-module scenarios covering at least:

1. setup confirmation appears only after the confirming bar closes;
2. Entry Model emits one deterministic plan;
3. Protection emits valid side-specific geometry;
4. Risk approves/rejects without changing upstream evidence;
5. Position Scaling partial approval preserves exact quantity semantics;
6. Router emits one canonical `DownstreamPlanEnvelope`;
7. Paper Execution accepts an ordinary limit entry;
8. partial fill and duplicate fill event are idempotent;
9. cancellation and expired plan are terminal and replay-safe;
10. duplicate plan is idempotent;
11. same plan ID with different content fails closed;
12. simultaneous stop/target inside one candle follows the declared conservative policy;
13. synthetic OCO cancels the safe sibling only after factual confirmation;
14. restart between entry and protection installation recovers safely;
15. lost acknowledgement and repeated event reconcile without duplicate venue side effects;
16. stale mark, market mismatch, damaged checkpoint and state divergence block and reconcile fail closed.

## Final integration gates

Only after all selected components are materialized:

- Python 3.12 and 3.13 complete repository suites;
- Ruff and strict MyPy for every package;
- build every wheel and required normalized sdist;
- Twine verification;
- clean isolated installation from built distributions;
- entry-point and public import inventory;
- deterministic analytical replay;
- checkpoint/restart at every ownership boundary;
- Router → Paper cross-contract suite;
- Paper partial-fill, OCO, lost-acknowledgement and reconciliation suite;
- Trading Runner lifecycle, watchdog, kill switch, graceful shutdown and recovery;
- security scans and SBOM;
- exact-head fast CI and justified heavy/recovery jobs;
- evidence artifact digests recorded in `V0_6_INTEGRATION_LEDGER.md`.

PR `#168` must remain Draft until these gates pass on the exact combined head. Do not enable live execution.

## Immediate Codex action

Session Range, Market Regime, FVG, Breakout–Return, Canonical Setup Adapter,
Entry Model and Protection & Target are integrated and locally green.

Session Range:

- source: `0ffbc5aa409e7628fff39fe1e80f0db77fc102a3`;
- merge: `6e5225b418e6bb0b30478bacc5f6c17aec7b5fdc`;
- accepted delta after runtime evidence
  `71fae8500a4aeb6cafba3c6aae13020b95d3e3be`: workflow-only runner-role
  routing;
- Python 3.12/3.13 component, coverage, Core contracts, lint, format, typing,
  packaging, Twine and clean-install entry-point gates: passed;
- full command/results record: `V0_6_INTEGRATION_LEDGER.md`.

Market Regime:

- source: `55946d26f6b95c5d0a2b5c8ed9b0dbbd6786f938`;
- merge: `4e97b4ddc8ce4f1fe495d453579c3089e8d56957`;
- accepted delta after evidence
  `4d6a751608466305cf09caf25ecd59515ca2cb49`: workflow-only runner-role
  routing;
- a stale workflow-policy test was repaired to require `[self-hosted, fast]`
  and reject physical machine labels;
- Python 3.12/3.13 full component including slow tests, coverage, Core
  contracts, adjacent Session Range, lint, format, typing, packaging, Twine
  and combined clean-install entry-point gates: passed.

FVG:

- source: `85d30d96928c9e016919ae5c369959b0b93c5e56`;
- merge: `750321f5b699b2a7bd34a3be640556845b770e86`;
- frozen Core CI retained with both Python 3.12/3.13 jobs and
  `[self-hosted, fast]`; focused FVG workflows retained;
- manifest metadata adapted to Core 0.4.2 without weakening FVG runtime
  validation;
- Python 3.12/3.13 component, branch coverage, adjacent analytics, Core
  contracts, lint, format, typing, packaging, Twine and combined clean-install
  entry-point gates: passed;
- Coverage 7.13.3 macOS privacy-sandbox startup limitation and the separate
  7.15.2 coverage evidence are recorded in the ledger.

Breakout–Return:

- source: `49bc941ee65a64f87fdbf714127996b1f3982388`;
- merge: `3a0934020405edc826e7814d1fcc3c69dc736f3f`;
- accepted post-test delta: docs/evidence and workload-role workflow only;
- central root audit documents were retained; component evidence remains in
  `audits/breakout_return/**`;
- Python 3.12/3.13 component, coverage, all prior analytics, Core contracts,
  lint, typing, packaging, Twine and combined clean-install entry-point gates:
  passed.

Canonical Setup Adapter:

- source: `ed3efeddbcf2a76a5f67e67e497ff9c42457a3cb`;
- merge: `f0146477f5ca8efd16107f8e365e82c2f398c464`;
- accepted post-test delta: one workload-role workflow commit only;
- five current-Ruff `UP038` compatibility findings were repaired with
  behavior-equivalent Python 3.12+ union-form `isinstance` checks;
- Python 3.12/3.13 component, 97% branch coverage, optimized runtime, all prior
  modules, full root suite, lint, format, strict typing, packaging, Twine and
  clean-install entry-point gates passed;
- Entry Model validated head was used only as a detached dependency for the
  adapter's four real-chain tests and was not included in the adapter merge.

Entry Model:

- source: `d08ca2957dff1fc697665762e91cc6c297b43ab0`;
- merge: `96ed6395d3f3f7ab6872d7618c37b7857b3888a3`;
- accepted post-validation delta: workload-role workflow only;
- current-Ruff compatibility was repaired in three `isinstance` checks;
- a reproduced high-precision Decimal serialization defect was fixed without
  changing the public schema, and a focused regression was added;
- Python 3.12/3.13 component, 99% branch coverage, optimized runtime, all prior
  modules, full root suite, lint, format, strict typing, packaging, Twine and
  clean-install entry-point gates passed.

Protection & Target:

- source: `02f3a2239e6edce2eb3e92b558925f5ca436da76`;
- merge: `409464e061ef6745caa42ee71c01808a56ec059e`;
- accepted post-validation delta: workload-role workflow only;
- four current-Ruff compatibility findings and one stale physical-runner
  policy assertion were repaired without changing public contracts;
- Python 3.12/3.13 component, 93% branch coverage, all prior modules, full
  root suite, lint, format, strict typing, packaging, Twine and clean-install
  entry-point gates passed.

The exact next action is to refresh `modules/risk-sizing-position-adjustments`,
compare live head `ea3579b0159ee9269f9213c4b698730153f356ac` with validated SHA
`11419b106fb8eb37ee0a3e0b5570c7c110d75fab`, and classify every intervening
commit before starting the Risk & Position Sizing merge.
