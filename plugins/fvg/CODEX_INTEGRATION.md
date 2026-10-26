# Integration Instructions — FVG 0.3.0

## Authority and scope

- Repository: `mishkacher/Module-Strategy`
- Target base branch: `integration/modular-strategy-v0.5.0`
- Current pinned Core/base SHA: `b4860c94dafb1d398cb36088d16c32bfcd065fc4`
- Component branch: `modules/fvg-hardening`
- Authoritative Draft PR: `#27`
- Fully validated FVG exact SHA: `6ad19db34e80b4f9c19f4d2dd4752b376e36feec`
- Package version: `0.3.0`
- State schema: `4`

The FVG module is an analytical producer. Do not add position sizing, protection/target, execution, order submission or portfolio mutation to this component.

Do not merge PR #27, create a tag, publish a package or declare live-trading readiness without separate explicit authorization.

## Validated release evidence

Release workflow run `30036881793` passed all 17 gates on Python 3.12 and 3.13 at exact SHA `6ad19db...`.

Per interpreter:

- `572/572` full tests passed;
- `7/7` runtime tests passed;
- failures/errors/skips `0/0/0`;
- branch-aware coverage 95%;
- Ruff, strict MyPy and compileall passed;
- optimized-runtime tests passed;
- benchmark/restart/memory checks passed;
- normalized wheel and sdist were reproducible;
- clean wheel/sdist installation passed;
- Bandit, detect-secrets, pip-audit and CycloneDX SBOM passed.

Post-cleanup focused run `30074288827` passed install, compile, Ruff, strict MyPy and `572/572` tests on branch head `0ac504881b696199a55ac4621489918f6f00b9f5`.

## Verification commands

Use Python 3.12+ and an isolated environment. The permanent focused workflow uses uv, but equivalent commands are:

```bash
python -m pip install -e .
python -m pip install -e './plugins/fvg[dev]'
python -m pip check
python -m compileall -q plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
ruff format --check plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
ruff check plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts
mypy --config-file mypy.ini --strict plugins/fvg/src/modular_strategy_fvg
python -m pytest -q plugins/fvg/tests
```

For the complete release gate, follow `docs/CI_CHECKPOINT_POLICY.md` and use `.github/workflows/fvg-release-validation.yml`. Do not weaken the 95% branch-coverage threshold or omit security, reproducibility and clean-install gates.

## State migration

- Writer emits schema `4`.
- Reader accepts schemas `1`, `2`, `3` and `4`.
- Legacy payloads are migrated fail-closed and atomically.
- Restored cursor/history/lifecycle/pre-FVG state is validated before commit.
- Duplicate and retained-history conflicts are detected immediately.

## Controlled integration procedure

The current integration branch is not yet an ancestor of the FVG branch. A runner-only dry run (`30059979281`, artifact `8584177590`) proved that an ordinary merge has exactly one conflict:

```text
.github/workflows/fvg-module-ci.yml
```

The reviewed resolution is to retain the component branch version of that file because it is the isolated focused workflow that passed `572/572` tests. No other conflict remains after that resolution.

Only after direct integration-owner authorization:

```bash
git checkout modules/fvg-hardening
git fetch origin integration/modular-strategy-v0.5.0
git merge --no-ff b4860c94dafb1d398cb36088d16c32bfcd065fc4
# Resolve only .github/workflows/fvg-module-ci.yml by retaining the validated component version.
```

Then verify:

1. there are no unmerged paths;
2. no unexpected FVG runtime/test content changed;
3. the merge has two parents and history was not rewritten;
4. the focused FVG Python 3.12 workflow passes on the exact merge head;
5. PR #27 remains Draft unless separately authorized otherwise.

Never use force-push, rebase or history rewrite for this integration.
