# Post-Error-Fix Baseline — Protection & Target 1.1.1

## Identity

- local branch: `modules/protection-target-hardening`;
- parent local RC commit: `63e41a4366ec52efc2fbd518ab25692b8511c83b`;
- remote source branch before publication: `modules/protection-target` at `789680102439fc5b1eed34e2d950cd06be9e0fa8`;
- package version: 1.1.1;
- Core: 0.4.0;
- Module API: 1.0;
- feature schema: 1;
- state schema: 2;
- Python tested locally: 3.13.5.

## Quality baseline

The first clean post-error run, before the additional post-error hardening suite, produced:

- 186 tests passed;
- coverage gate 90% passed;
- Ruff format/check passed;
- strict mypy passed for nine runtime files;
- compileall passed;
- Bandit zero findings;
- detect-secrets zero findings;
- wheel/sdist/twine checks passed;
- clean wheel installation, `pip check`, import, entry point and Registry loading passed.

After adding the dedicated post-error adversarial suite, the working baseline contains 208 tests. Final coverage and packaging measurements are recorded after the 30 rounds.

## Performance baseline

2,000 unique EntryPlans, five traced runs on Python 3.13.5:

- minimum: 1.086198 s;
- median: 1.107916 s;
- maximum: 1.110188 s;
- throughput: 1,805.19 plans/s;
- peak traced memory: 0.573168 MiB;
- checkpoint median: 235,551 bytes;
- restore median: 5.193 ms.

This is the comparison point for subsequent post-error optimization. Correctness changes are not permitted to alter plan geometry or deterministic identifiers for valid inputs.

## CI baseline

- previous remote 1.0.0 commit had successful core, integration and module workflows;
- 1.1.1 had not yet been pushed when this baseline was recorded;
- Python 3.12 and macOS ARM64 verification remain GitHub self-hosted CI gates.
