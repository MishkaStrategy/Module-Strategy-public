# FVG CI checkpoint policy

## Focused validation

Every pull-request update that changes `plugins/fvg/**` runs `fvg-fast-ci` on Python 3.12. The focused job uses an isolated workspace and checks installation, compilation, Ruff, strict MyPy and the complete FVG test suite. JUnit and a concise pytest log are retained.

## Full release checkpoint

The Python 3.12/3.13 release workflow is checkpoint-only. It is enabled only for a reviewed full-integration checkpoint and keeps the component PR in Draft state unless an integration owner changes that state.

The release matrix contains 17 gates:

1. canonical source;
2. clean source snapshot;
3. compileall;
4. Ruff format;
5. Ruff lint;
6. strict MyPy;
7. runtime tests;
8. full tests;
9. branch-aware coverage of at least 95%;
10. optimized-runtime tests;
11. benchmark, restart and memory evidence;
12. reproducible wheel and sdist;
13. clean wheel and sdist installation;
14. Bandit;
15. detect-secrets;
16. pip-audit;
17. CycloneDX 1.6 SBOM.

## Runner and formatting safety

FVG and pinned-Core checkouts use separate workspaces. Temporary environments and evidence are unique to each run. Cleanup is bounded to workflow-owned FVG paths.

Formatting uses pinned Ruff 0.12.0 in check-only mode. CI publishes formatting diagnostics; source corrections are reviewed commits.

## Validation base and Core

Pinned Core:

```text
b4860c94dafb1d398cb36088d16c32bfcd065fc4
```

Temporary validation can use the historical common ancestor below only to obtain an executable merge ref:

```text
5e03b933207a99940244634f49a756d27888a5c3
```

The evidence records both values separately. The common ancestor never replaces the pinned Core checkout.

## Completed release evidence

Workflow `fvg-0.3.0-release-validation`, run `30036881793`, validated exact FVG SHA:

```text
6ad19db34e80b4f9c19f4d2dd4752b376e36feec
```

Python 3.12 and Python 3.13 both passed all 17 gates.

Per interpreter:

- runtime tests: 7/7;
- full tests: 572/572;
- failures, errors and skips: 0;
- branch-aware coverage: 95% over 3,273 statements and 1,498 branches;
- optimized tests, restart digest, packaging, clean installation, security, dependency audit and SBOM: passed.

Artifacts:

- Python 3.12: `8575668567`;
- Python 3.13: `8575665269`;
- formatter: `8575610563`.

Distribution SHA-256:

- wheel: `6dc1a19fa653d962d27832237c01b9a52049e23bd0704fcaddf47004f8db685d`;
- sdist: `2110e6b0f939af7a8e9155294151092530eeff088718857cbb2de166f028331a`.

## Post-cleanup evidence

Branch head `0ac504881b696199a55ac4621489918f6f00b9f5` passed focused run `30074288827`:

- install, compile, Ruff and strict MyPy: passed;
- FVG tests: 572/572;
- artifact: `8589234852`;
- artifact digest: `sha256:cac74e6ff6a9ae91657977c357ab1ac78ebfb9eb36653043891991e1e966d62a`.

The same head passed Core focused run `30074288873`.

## Integration dry run

Run `30059979281`, artifact `8584177590`, simulated the integration-base merge in an isolated workspace and then discarded it.

The result contained exactly one conflict: `.github/workflows/fvg-module-ci.yml`. The reviewed resolution is to retain the validated isolated FVG focused workflow. No other conflict remained.

The component is development-complete. The remaining integration-base ancestry merge requires separate direct authorization, followed by a focused exact-head check. PR #27 remains Draft and unmerged.
