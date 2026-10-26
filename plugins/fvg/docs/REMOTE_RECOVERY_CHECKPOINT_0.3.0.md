# FVG 0.3.0 — Remote recovery checkpoint

Status: **RECOVERY COMPLETE; RELEASE GATES GREEN; INTEGRATION AUTHORIZATION PENDING**

## Authoritative context

- Repository: `mishkacher/Module-Strategy`
- Component branch: `modules/fvg-hardening`
- Authoritative Draft PR: `#27`
- Integration base and pinned Core SHA: `b4860c94dafb1d398cb36088d16c32bfcd065fc4`
- Fully validated FVG exact SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`
- Package/API version: `0.3.0`
- State schema: `4`

The former incomplete transport state is resolved. The split runtime, restore and on-bar layers are present and active. This checkpoint replaces all earlier residual-validation checkpoints.

## Recovered and hardened scope

- strict confirmed three-candle FVG detection;
- causal pre-FVG evaluation and candidate resolution;
- deterministic zone lifecycle publication;
- strict UTC, timeframe and candle-adapter boundaries;
- canonical historical/new-index/evaluator/feature identity handling;
- immediate duplicate and retained-history conflict detection;
- strict schema 1/2/3 to schema 4 migration;
- atomic schema-4 restore and bounded deterministic checkpoints;
- reproducible package normalization, clean install and supply-chain evidence;
- complete regression, adversarial and coverage-hardening suites.

## Authoritative release evidence

Workflow `fvg-0.3.0-release-validation`, run `30036881793`, validated exact SHA `6ad19db...` on Python 3.12 and 3.13.

Both interpreters passed all 17 release outcomes:

- canonical and clean source;
- compileall, Ruff and strict MyPy;
- runtime, full and optimized tests;
- branch-aware coverage at or above 95%;
- benchmark, memory and restart digest;
- reproducible wheel/sdist and clean installation;
- Bandit, detect-secrets, pip-audit and CycloneDX SBOM.

Per interpreter: `572/572` full tests and `7/7` runtime tests passed with no failures, errors or skips.

Artifacts:

- Python 3.12: `8575668567`
- Python 3.13: `8575665269`
- formatter: `8575610563`

Distribution SHA-256:

- wheel: `6dc1a19fa653d962d27832237c01b9a52049e23bd0704fcaddf47004f8db685d`
- sdist: `2110e6b0f939af7a8e9155294151092530eeff088718857cbb2de166f028331a`

## Post-cleanup confirmation

Focused run `30074288827` validated branch head `0ac504881b696199a55ac4621489918f6f00b9f5` after temporary ancestry tooling was removed:

- install, compile, Ruff and strict MyPy passed;
- all `572/572` FVG tests passed;
- artifact `8589234852`.

The same head passed Core focused run `30074288873`.

## Integration dry run

Run `30059979281`, artifact `8584177590`, simulated an ordinary merge of the current integration base and then discarded the workspace result.

It found exactly one conflict: `.github/workflows/fvg-module-ci.yml`. The reviewed resolution is to retain the validated isolated FVG focused workflow. No other conflict remained and no FVG runtime/test resolution was required.

## Exact next action

The component requires no further recovery or hardening work. After separate direct authorization, record the current integration base as ordinary merge ancestry, resolve the single workflow conflict as documented, and run the focused FVG gate on the resulting exact head.

PR #27 remains Draft and unmerged. No release, tag, package publication or live-trading declaration is authorized by this checkpoint.
