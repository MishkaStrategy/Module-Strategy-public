# FVG 0.3.0 — Authoritative synchronization status

Status: **REMOTE RELEASE VALIDATION COMPLETE; INTEGRATION MERGE AWAITS SEPARATE AUTHORIZATION**

This document supersedes the former transport and in-progress validation checkpoints. The active GitHub branch contains the complete FVG 0.3.0 runtime, tests, packaging and evidence.

## Repository context

- Repository: `mishkacher/Module-Strategy`
- Base branch: `integration/modular-strategy-v0.5.0`
- Current pinned Core/base SHA: `b4860c94dafb1d398cb36088d16c32bfcd065fc4`
- Historical common ancestor used only for validation merge refs: `5e03b933207a99940244634f49a756d27888a5c3`
- Authoritative FVG branch: `modules/fvg-hardening`
- Draft component PR: `#27`
- Fully validated exact FVG SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`
- Active package/module version: `0.3.0`
- Active state schema: `4`
- Active public entry point: `modular_strategy_fvg:FvgModule`

Use PR #27 to determine the current documentation tip. Later documentation/shared-root commits must not be confused with a new runtime version.

## Materialized runtime

The active import graph includes:

- split init, state, restore, on-bar, runtime, processing and support layers;
- canonical feature-builder and duplicate-signature adapter boundaries;
- confirmed evaluator, typed filter traces and rich metrics;
- deterministic lifecycle state and events;
- causal pre-FVG evaluation, persistent candidates and resolution events;
- strict schema-1/schema-2/schema-3 to schema-4 migration;
- deterministic wheel/sdist normalization scripts;
- complete 0.3.0 regression, adversarial and coverage-hardening suites.

`module_restore.py` and `module_onbar.py` are present and active. Package metadata, public `__version__`, manifest and `VERSION` marker target 0.3.0.

## Authoritative exact-head release validation

Workflow: `fvg-0.3.0-release-validation`
Run: `30036881793`
Exact SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`

Python 3.12 and Python 3.13 both passed all 17 gates:

1. canonical source;
2. clean source snapshot;
3. compileall;
4. Ruff format;
5. Ruff lint;
6. strict MyPy;
7. runtime tests;
8. full tests;
9. branch-aware coverage `>=95%`;
10. optimized-runtime tests;
11. benchmark/restart/memory evidence;
12. reproducible wheel and sdist;
13. clean wheel/sdist installation;
14. Bandit;
15. detect-secrets;
16. pip-audit;
17. CycloneDX 1.6 SBOM.

Per interpreter:

- runtime tests: `7/7`;
- full tests: `572/572`;
- failures/errors/skips: `0/0/0`;
- branch-aware coverage: 95% over 3,273 statements and 1,498 branches;
- restart digest matched uninterrupted digest;
- packaging, clean install, security, dependency audit and SBOM passed.

Artifacts:

- Python 3.12: `8575668567`, digest `sha256:9c87c793a50c6157795f8f12462a81447c6798a234295282dab339f8e21fb7a6`
- Python 3.13: `8575665269`, digest `sha256:d387a5e52a3c18ea3a2baf66c8700cdd091a5242ffcb8e0c0ef7952f5680fd07`
- formatter: `8575610563`, digest `sha256:f4baf5423fd8d66174cbf35db36012d37804a1560226104b4b1bd57c42266d25`

Cross-version distribution SHA-256:

- wheel: `6dc1a19fa653d962d27832237c01b9a52049e23bd0704fcaddf47004f8db685d`
- sdist: `2110e6b0f939af7a8e9155294151092530eeff088718857cbb2de166f028331a`

## Post-cleanup focused validation

Branch head `0ac504881b696199a55ac4621489918f6f00b9f5` was validated after removing the temporary ancestry workflow.

- workflow: `fvg-fast-ci`
- run: `30074288827`
- install, compile, Ruff and strict MyPy: success
- complete FVG suite: `572/572`
- failures/errors/skips: `0/0/0`
- artifact: `8589234852`
- artifact digest: `sha256:cac74e6ff6a9ae91657977c357ab1ac78ebfb9eb36653043891991e1e966d62a`

The same head passed `core-fast-ci` run `30074288873`, including root tests with coverage and core wheel build. Concurrent integration/market-regime jobs failed before creating any steps and produced no code-failure evidence.

## Integration ancestry status

The FVG branch and current integration base still have separate ancestry. A controlled no-push ordinary-merge dry run was completed and aborted:

- run: `30059979281`
- artifact: `8584177590`
- artifact digest: `sha256:86280aee11090b2b6c3256eb98afe399c814dc2730c8cdbfb338512cff4f046b`
- exactly one conflict: `.github/workflows/fvg-module-ci.yml`
- validated resolution: retain the isolated FVG focused workflow from the component branch
- no other conflicts and no changes under FVG runtime/test paths after resolution

## Remaining action

There is no remaining FVG development or release-gate blocker. The sole repository action is an ordinary two-parent integration-base merge into `modules/fvg-hardening`, resolving the one workflow conflict as documented, followed by a focused exact-head check.

That merge requires separate direct authorization. No force-push, rebase, history rewrite, merge of PR #27, tag, release, package publication or live-trading action is authorized by this checkpoint.
