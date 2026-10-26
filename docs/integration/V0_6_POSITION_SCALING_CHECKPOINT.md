# Modular Strategy v0.6 — Position Scaling checkpoint

Status: PUBLISHED FROM AUTHORITATIVE COMPONENT AND CROSS-MODULE EVIDENCE

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Starting integration checkpoint: `fca725632b94bfbf7ab6e402dc97cb451cba3c03`
- Validated Position Scaling SHA: `eaedbdb730b8435197c355c8c69906fe6e2436e7`
- Accepted live Position Scaling head: `d5cb7975dda0beba113ec32971384b4bc0e88ed8`
- Ordinary two-parent merge candidate: `f88ee337703dd85aded3346db14810155b3e4a22`

The merge candidate has the exact published Risk checkpoint as its first parent and the accepted Position Scaling head as its second parent. It was created only on `integration-candidate/v0-6-position-scaling`; it was never merged to `main` or frozen Core.

## Accepted post-validation delta

Validated-to-live comparison:

- status: ahead by exactly one commit;
- behind: zero commits;
- changed file: `.github/workflows/position-scaling-hardening-ci.yml` only;
- diff: one addition, five deletions;
- purpose: workload-role routing to `[self-hosted, fast]` and removal of stale physical-runner comments/assertions.

No product source, tests, package metadata or public contract changed after the validated SHA.

## Integration documentation repair

The component's final audit and Draft PR already recorded PS-CCR-001 as closed by Risk & Position Sizing 1.2.0, while four earlier documents still described it as open. Integration corrected only those stale documents:

- `CORE_CHANGE_REQUESTS.md` now lists PS-CCR-001 as closed;
- `CORE_CHANGE_REQUEST.md` preserves the historical request and records the implemented typed contract and closure evidence;
- `docs/INTEGRATION_READINESS.md` now reflects the typed planner → Risk → approval path;
- `LOCAL_RELEASE_REPORT.md` now reflects the authoritative 251-test exact-head gate, security/SBOM evidence and closed blocker.

Product code and runtime contracts were not changed during integration.

## Authoritative component evidence

Workflow `position-scaling-hardening-ci`, run `29867118592`: **SUCCESS**.

Python 3.12 with integration Core:

- compileall, Ruff lint/format and strict MyPy: PASS;
- Position Scaling tests: 251 passed;
- branch-aware coverage: 90%, gate 90%;
- wheel and source distribution: PASS;
- clean-wheel installation, `pip check`, imports, two entry points, registry and checkpoint smoke: PASS;
- artifact digest: `sha256:7f07db4b58cfa83c1b109eb45566958a2337adfeed96b953c2dd598882049c09`.

Python 3.13 with hardened Core 0.4.1:

- all Python 3.12 gates: PASS;
- repository-root compatibility: 244 passed;
- Session Range: 84 passed;
- Market Regime: 80 passed;
- FVG: 70 passed;
- Bandit, detect-secrets and pip-audit: PASS;
- CycloneDX SBOM: PASS;
- deterministic benchmark and bounded-memory evidence: PASS;
- artifact digest: `sha256:7f46b0d79b09d2107a036071f6638c546ffed5919467629f9984906c75b4243a`.

## PS-CCR-001 closure evidence

Pinned cross-module run `29866061235`: **SUCCESS**.

Verified chain:

`position_scaling.plan → risk.request → risk.decision → position_scaling.approved`

Pinned source:

- Position Scaling: `b338264e872bab99d48acb63304f041aa1a182db`;
- Risk Sizing: `b2393048d09ab345bdbfe3addc0fea20be685c8a`.

Observed quantities:

- requested: `0.25`;
- Risk-approved: `0.250`;
- final approved: `0.25`.

Typed request, typed decision and legacy metadata agreed exactly; no planner, Risk or approval skips occurred. Artifact digest: `sha256:2e3174bf46cc20783cb55fa1a9450c0f81f16c6550237e04e7685d89b63a5682`.

PS-CCR-001 is closed.

## Current limitation

The unavailable `[self-hosted, fast]` queue prevented a redundant full current-integration-head rerun. This checkpoint does not invent such a run. Full release-candidate exact-head Python 3.12/3.13, recovery, packaging and end-to-end execution checks remain mandatory before any merge to `main`.

## Safety state

- PR #168 remains open and Draft;
- no force-push, rebase or history rewrite;
- no merge to `main` or frozen Core;
- no tag, release, package publication or live execution.

## Next executable component

Decision Router:

- canonical branch: `modules/trade-plan-router-v0.5`;
- authoritative evidence SHA: `f779e599b361a7b860506980de6b5ae8d0cd1f01`;
- live-head and post-evidence delta must be rechecked before construction of an integration candidate;
- any shared Core or integration workflow conflicts must preserve the central Core-owned versions unless the component adds a proven conditional extension.
