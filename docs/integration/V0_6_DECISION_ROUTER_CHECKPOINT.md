# Modular Strategy v0.6 — Decision Router checkpoint

Status: PUBLISHED FROM AUTHORITATIVE ROUTER, FINAL-GATE AND CORE 0.4.2 EVIDENCE

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Starting integration checkpoint: `7731a43461c67e032b4088758ebfcf1d762df552`
- Validated Router source/workflow/docs checkpoint: `a81cfba9aea705895a28f410d8e301b97749e6eb`
- Router evidence publisher and live source: `f779e599b361a7b860506980de6b5ae8d0cd1f01`
- Resolved source head: `87ff1386cf8edd4ce6f76a0bc13b6c33d865a98e`
- Ordinary two-parent merge candidate: `c8cb8932ed1287c3cd521b98d47ed8b99ce01bd4`
- Candidate after central-ownership cleanup: `ad5f5d088ee0b7b3ac3ab0823eea2ecd9d3226ab`

The merge candidate preserves the exact Position Scaling checkpoint as its first parent and the complete Router history plus explicit integration-resolution commits as its second parent. No merge targeted `main` or frozen Core.

## Live-head classification

The live Router head is the exact evidence publisher SHA. There are no post-evidence product, test, workflow or documentation commits to classify.

## Shared-file conflict resolution

The Router lineage had diverged from the current central integration branch in shared repository governance and CI files. Integration retained current central ownership and excluded those historical branch copies:

- `.github/WORKFLOW_RULES.md`;
- `.github/workflows/ci.yml`;
- `.github/workflows/fvg-module-ci.yml`;
- `.github/workflows/integration-ci.yml`;
- `.github/workflows/market-regime.yml`;
- `.github/workflows/runner-routing-policy.yml`;
- `.github/workflows/session-range-ci.yml`;
- `docs/project/CI_TRIGGER_ROLLOUT.md`;
- `AGENTS.md`;
- `docs/AI_CHAT_TASK_TEMPLATE.md`;
- `docs/project/RUNNER_ROUTING_MIGRATION.md`;
- `scripts/audit_runner_routing.py`.

The obsolete one-shot `.github/workflows/recover-trade-plan-router-tests.yml` was also excluded. The final candidate diff contains only Router-owned product, tests, documentation, reports and the two dedicated permanent Router gates:

- `.github/workflows/decision-router-core-v042-compatibility.yml`;
- `.github/workflows/decision-router-final-gate.yml`.

Router product code and public contracts were not modified during conflict resolution.

## Authoritative validation gate

GitHub Actions run `30061212253`: **SUCCESS**.

- Router tests: 349 passed; no failures, errors or skips;
- Ruff: PASS;
- strict MyPy across 13 production source files: PASS;
- branch-aware coverage: 90.3750%, hard gate 90%;
- compile and dependency checks: PASS.

Pinned upstream contracts used by the gate:

- Entry Model: `08ee5bc55680d907de2c5ac7959b590eb8f80a22`;
- Protection & Target: `d03e290e57287f53847f4b93238fbd3722b2e39b`;
- Position Scaling: `eaedbdb730b8435197c355c8c69906fe6e2436e7`;
- Risk Sizing: `11419b106fb8eb37ee0a3e0b5570c7c110d75fab`.

## Authoritative final gate

GitHub Actions run `30061212239`: **SUCCESS**.

- production static security and public type-surface audit: PASS;
- Bandit and `pip-audit`: PASS;
- executable golden registry: 30/30;
- deterministic replay across four timezone/hash-seed profiles: PASS;
- serialization fuzz: 5,000 cases, PASS;
- risk-reduction fuzz: 5,000 cases, PASS;
- concurrency and soak stress: PASS;
- two Core wheel builds: byte-identical;
- two Router wheel builds: byte-identical;
- clean wheel installation, import, uninstall, reinstall and `pip check`: PASS.

Reproducible Router wheel SHA-256:

`9412e4142e02375017f487d3248780b28f9f1b1da2e8a8f1578ac78cc1d4d741`

## Core 0.4.2 compatibility

GitHub Actions run `30061212286`: **SUCCESS** against validated Core 0.4.2 exact head `04f6de01e5337883256299dd20effdbedd5ac73e` and the four pinned upstream contracts.

- installed Core version: 0.4.2;
- installed Router version: 0.1.0;
- full Router suite: 349 passed;
- Ruff and strict MyPy: PASS;
- branch-aware coverage gate: PASS at 90%;
- dependency check and clean Core + Router wheel installation/import: PASS.

Compatibility wheel SHA-256:

- Core: `392d1c0fc1b91118d87cafef3999f094342e7476d0fcf98be3a2bb44027eb9c1`;
- Router: `6a914faeb8b6bc3d4e6d4a380bf82f3d52388749255f5cae894cf17a0d6a4247`.

Additional exact checkpoint cross-checks succeeded in runs `30061212346`, `30061212251`, `30061212255`, `30061212303` and `30061212312`.

## Downstream execution boundary

`DownstreamPlanEnvelope` is the canonical Router handoff. Paper Execution must preserve:

- canonical Router JSON and payload SHA-256;
- `plan_id`, `content_hash` and `audit_hash`;
- typed action and validity interval;
- exact ordered string `ExecutionPrecondition` values;
- action-capability and validity checks.

A downstream executor must not recalculate direction, quantity, risk, stops, targets or sequencing, and must not translate Router string/SHA-256 OCC evidence into incompatible integer state versions. Unsupported reversal remains a typed rejection until reversal sequencing is explicitly implemented.

## Current limitation

The unavailable `[self-hosted, fast]` queue prevented a redundant rerun on the complete current central head. This checkpoint does not invent such a run. Full release-candidate exact-head Python 3.12/3.13 and complete Router → Paper Execution cross-contract tests remain mandatory before any merge to `main`.

## Safety state

- PR #168 remains open and Draft;
- no force-push, rebase or history rewrite;
- no merge to `main` or frozen Core;
- no tag, release, package publication or live execution.

## Next executable component

Paper Execution & Venue Reconciliation:

- canonical PR: #98;
- branch: `execution/paper-venue-reconciliation`;
- integration-ready evidence SHA: `57283323c38255c1a259df42a1e1388dcd260483`;
- live head and any post-evidence delta must be rechecked;
- shared `.github/workflows/integration-ci.yml` changes must not replace the central version;
- the Router adapter must consume the canonical `DownstreamPlanEnvelope`, preserve all hashes/OCC evidence and typed-reject unsupported reversal.
