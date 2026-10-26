# Risk Sizing CI checkpoint

Component: Risk & Position Sizing 1.2.0
Branch: `modules/risk-sizing-position-adjustments`
Base: `integration/modular-strategy-v0.5.0`

## Feature Store contract repair

The self-hosted macOS ARM64 run on commit `8461181d5a003af3ae876a8d7b09a513160021d6` reached the real test suite on Python 3.12 and 3.13. Static quality passed. Four failures were isolated to stale test expectations and fixtures:

- manifest expectation still referenced 1.1.0;
- a future Feature Store item was expected to bypass point-in-time filtering;
- an expired fixture violated the Core `Feature` constructor invariant;
- a different-symbol feature was expected to bypass Feature Store stream filtering.

Commit `68a724ce03b89a4195cefd355cc8c21a956319a4` aligns those tests with the real Core Feature Store contract while retaining direct adapter fail-closed tests through an unfiltered test view.

## Projected-position geometry repair

Subsequent exact-head runs exposed three projected-position test failures:

- one partial-approval fixture used `entry_price == stop_loss`;
- derivative evidence used an incorrect weighted-average entry and total risk;
- an exact step-boundary partial approval could be rejected because projected risk was recalculated through a rounded weighted-average price.

Commit `e5d479a3b71d3e4ee1de54562b9e97aace03da97` was created only after a successful full Risk Sizing suite on the self-hosted Linux/X64 VDS runner. It:

- corrects the two fixture geometries;
- calculates projected total risk directly from current-position and delta-order signed risk contributions;
- avoids a Decimal divide/multiply round-trip through weighted-average entry;
- preserves the published weighted-average entry for downstream evidence;
- removes all temporary self-modifying workflows and patch scripts.

## Coverage hardening

The initial exact-head macOS runs passed the complete test suite but reported 89% branch-aware coverage against the required 95% gate. The threshold was not reduced.

Two targeted fail-closed test matrices were added for:

- immutable position-adjustment DTO validation and alias parsing;
- canonical serialization and Decimal edge cases;
- projected-position risk, quantity, notional, leverage and portfolio-capacity branches;
- zero-after-rounding and instrument minimum constraints;
- direct post-rounding risk and leverage guards.

The verified self-hosted coverage-hardening run completed with:

- 195 tests passed;
- 1,142 statements and 478 branches;
- 96% total branch-aware coverage;
- 100% coverage for `adjustment.py` and `adjustment_calculator.py`.

Commit `f7dd827c702488d4f613cb33bd284030e610062c` was published only after that gate passed. Generated JUnit and coverage files were subsequently removed from the branch; evidence remains in GitHub Actions artifacts rather than the source tree.

## Packaging repair

The first exact-head macOS packaging step exposed that the module's repository-oriented `pyproject.toml` referenced `../../src`. A direct wheel could be built from the checkout, but the generated source distribution contained metadata only and could not rebuild its wheel.

The permanent CI packaging path now creates an isolated release staging tree containing the exact audited `strategy_modules.risk_sizing` source and a self-contained `src` package layout. It then verifies:

- Core wheel and source distribution;
- Risk Sizing wheel and source distribution;
- wheel rebuild from the Risk Sizing source distribution;
- clean virtual-environment installation;
- `pip check`;
- public version, entry point and `ModuleRegistry` discovery.

The self-hosted Linux/X64 VDS packaging verification completed successfully and removed its one-shot workflow. Commit `bedc429cd59f28c314c421b6f755b07b2413b74e` records that verified cleanup.

## Final exact-head gate

This documentation-only commit triggers the authoritative self-hosted macOS ARM64 workflow on the clean corrected source tree. Required gates are:

- Python 3.12 and 3.13;
- compileall;
- Ruff lint and format;
- strict mypy;
- full Risk Sizing tests and branch coverage of at least 95%;
- wheel and source distribution, including wheel rebuild from sdist;
- clean installation, `pip check`, public import and entry point;
- Bandit, detect-secrets, pip-audit and CycloneDX SBOM.

Core and repository-wide workflows that terminate before creating steps are tracked as separate CI-infrastructure failures and are not represented as Risk Sizing test results.

The Pull Request remains Draft. No merge, tag or release is authorized until both exact-head macOS jobs are green and the final audit is updated.
