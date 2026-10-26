# Modular Strategy v0.6 — Risk & Position Sizing checkpoint

Status: PUBLISHED FROM PREVIOUSLY COMPLETED LOCAL GREEN GATE

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core branch: `core/integration-contract-freeze-v0.4.2`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Previous canonical checkpoint: `a9d33d6afcbe5dc616423ca6cb697a1ae9263046`
- Integration head before Risk publication: `828b67f4509481c8e7cfba4f9a92b904fe884d67`
- Validated Risk product SHA: `11419b106fb8eb37ee0a3e0b5570c7c110d75fab`
- Accepted live Risk head: `ea3579b0159ee9269f9213c4b698730153f356ac`
- Ordinary two-parent merge candidate: `3755afa5a03ed90a62f6cf26d1e1d85e760453ca`

The merge candidate has the exact pre-publication integration head as its first parent and the exact accepted Risk source as its second parent. It was created through GitHub merge commit semantics on a temporary candidate branch; it was never merged into `main` or frozen Core.

## Accepted post-validation delta

The validated SHA to live-head delta is exactly one workflow-only commit. It changes `.github/workflows/risk-sizing-ci.yml` to the repository workload-role routing `[self-hosted, fast]`. It does not change product source, component tests, packaging metadata or public contracts.

## Integration repairs

Two integration-only repairs were reconstructed from the completed local handoff:

1. A test-only compatibility shim provides `FakeFeatureView.get_all()` semantics required by frozen Core 0.4.2. Product code and contracts are unchanged.
2. Two Git-reported Markdown trailing-whitespace defects in `docs/RISK_SIZING_CI_CHECKPOINT.md` were normalized. No prose or evidence claim was changed.

Temporary bootstrap, transport and executor files used during the connector recovery were removed from the final candidate tree.

## Completed validation evidence

The previous local integration run completed before publication was blocked by GitHub write permission. Its recorded evidence is authoritative for this checkpoint:

- Python 3.12 and 3.13: PASS;
- compileall: PASS;
- Ruff lint and format: PASS;
- strict MyPy: PASS;
- Risk Sizing tests: 195 passed;
- branch coverage: 96%;
- root regression suite: 1,244 passed on both interpreters;
- Session Range: 139 passed on both interpreters;
- Market Regime: 151 passed on both interpreters;
- FVG: 572 passed on both interpreters;
- Canonical Setup Adapter: 65 passed on both interpreters;
- Entry Model: 325 passed on both interpreters;
- Protection & Target: 232 passed on both interpreters;
- wheel and source distribution build: PASS;
- Twine checks: PASS;
- clean non-editable installation: PASS;
- `risk_sizing` public entry-point smoke: PASS.

The original local merge object was not present in the remote object database because its push failed with `403 Write access to repository not granted`. The published candidate reconstructs the same product source and the documented integration-only repairs without changing Risk behavior.

## Remote runner limitation

A redundant exact-head Actions rerun was prepared on `[self-hosted, fast]`, but the available self-hosted queue did not assign a runner and no job reached its first step. This document does not claim a new remote test run. Publication relies on the already completed local Python 3.12/3.13 gate above. Full release-candidate exact-head Actions, security/SBOM and end-to-end chain checks remain mandatory before any merge to `main`.

## Safety state

- PR #168 remains open and Draft.
- No force-push, rebase or history rewrite is authorized or used.
- No merge to `main` or frozen Core is authorized or used.
- No tag, release, package publication or live execution is authorized or used.

## Next executable component

Position Scaling:

- validated SHA: `eaedbdb730b8435197c355c8c69906fe6e2436e7`;
- accepted live head: `d5cb7975dda0beba113ec32971384b4bc0e88ed8`;
- live delta: exactly one workflow-only commit;
- conflict preview on top of the Risk product candidate: mergeable without conflicts;
- required integration repair: update stale Position Scaling documents that still describe PS-CCR-001 as open, while the final audit records it closed by Risk Sizing 1.2.0. This repair must remain documentation-only and must not change product contracts.
