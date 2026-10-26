> Historical report; physical runner identifiers are pseudonymized in this export. This is not new-root acceptance.

# Entry Model — Independent Final Hardening Audit

Date: 2026-07-21

## Independent clean-state scope

The final Entry Model 0.2.0 tree was reconciled against integration base `5e03b933207a99940244634f49a756d27888a5c3`, tested against the real Core 0.4.0 wheel, packaged, installed from the produced wheel, and audited on the self-hosted macOS ARM64 runner. The final documentation-inclusive head is `822ee85604df5add7a9b0a594ae4a7f91694e971`.

## Error Discovery outcome

Eleven findings were reproduced and fixed or correctly classified:

- stale remote implementation versus the newer verified local release candidate;
- optimization-removable runtime assertion in financial geometry validation;
- incomplete handoff/package layout;
- incomplete source-distribution contents;
- formatter violations;
- unsuitable hosted workflow;
- external hosted-runner failures before steps;
- failed Python 3.12/3.13 provisioning on self-hosted macOS ARM64;
- missing checksum output directory;
- Darwin RSS bytes mislabeled as KiB;
- unmatched MANIFEST glob.

No unresolved critical or high Entry Model code defect remains.

## Exact final GitHub evidence

Documentation-inclusive self-hosted `entry-model-ci` run `29840317277` completed successfully on exact head `822ee85604df5add7a9b0a594ae4a7f91694e971`.

All seven jobs passed:

- Fast Quality / Python 3.12;
- Fast Quality / Python 3.13;
- Full Test / Python 3.12;
- Full Test / Python 3.13;
- Security / Python 3.13;
- Packaging / Python 3.13;
- Performance / Python 3.13.

The checkout log confirms the PR merge-ref combined head `822ee856…` with base `5e03b933…` on runner `archived-darwin-runner`, macOS ARM64.

## Tests and coverage

- Python 3.12: 324 tests, 0 failures, 0 errors, 0 skipped.
- Python 3.13: 324 tests, 0 failures, 0 errors, 0 skipped.
- Full optimized-runtime suites pass under `python -O`.
- Lines: 950/962 = 98.75%.
- Branches: 347/352 = 98.58%.
- Ruff check and format-check: PASS.
- strict mypy: PASS on 11 source files.
- compileall and shell syntax: PASS.

## Security

Final security artifact `8500209309`, digest `sha256:d66620ab524cd4626e3fc114982e962644c5f3d83fc60ca524875b980fc915f4`, was downloaded and independently inspected.

- detect-secrets: zero candidates.
- pip-audit: no known vulnerabilities in audited third-party dependencies.
- Local project packages were correctly reported as non-PyPI packages and skipped by the external vulnerability database.
- CycloneDX SBOM generated successfully.

## Packaging

Final distribution artifact `8500402806`, digest `sha256:bed3a2cd47a37457f7d753d7f9002cc4191830cad4ad89296d9db6fcad175868`, was downloaded and independently rehashed.

- Wheel SHA-256: `ee549e6f6935cf39889bc8d540ea34cb886f20a72d55b7a78356236ec26c7e53`.
- Sdist SHA-256: `59220db1503a31746816619e56eef195297bf9d62a619860a17cf6dc1364995a`.
- Published checksum manifest matches the independently calculated hashes.
- Wheel metadata requires Python >=3.12 and Core >=0.4,<0.5.
- Exactly one module entry point exists: `entry_model = modular_modules.entry_model:EntryModelModule`.
- Sdist includes `MANIFEST.in`, Python resolver, `py.typed`, test support files, final audit documents, and the post-error addendum.
- Independent clean install with Core 0.4.0 passed `pip check`, public import, entry-point load, and state schema 2 snapshot/restore.

## Performance

Final performance artifact `8500547878`, digest `sha256:db634ebb41251dc6ca9c396c2e756ff1d583196adb34c66976af6c46cf8a0613`, was downloaded and inspected.

Environment: Python 3.13.14, macOS ARM64.

- 50k plans: median 8.131984 s, 6,148.56 plans/s.
- 250k plans: median 40.750140 s, 6,134.95 plans/s.
- 1m empty events: median 8.177349 s, 122,289.01 events/s.
- Restarted 50k digest equals uninterrupted digest: `39a9293cdd14ba19007d349738ed02164891cbfb3a38b0d344537c4067446211`.
- 250k digest remains `d3a5971648e73d2f800cc6c835c3585f59d701d55a340df4b1aafdfcabf78087`.
- State remains bounded at 10,000 processed and 10,000 source-feature records.
- Tracemalloc peak: 17,074,745 bytes.
- Corrected normalized max RSS: 65,696 KiB.
- Restore median: 0.061859 s over 20 runs.

## Scope and integration dependency

Core and neighboring module source remain unchanged by this PR. Module API 1.0 and state schema 2 are preserved.

The complete executable strategy still depends on a separately owned canonical producer or adapter for `setup.confirmed`, plus downstream Protection/Target, Risk/Sizing, execution and position-management modules. Entry Model must not absorb those responsibilities.

GitHub-hosted Core and integration workflows can still fail before steps because of external hosted-runner infrastructure; those failures are not represented as Entry Model failures.

## Final verdict

**ENTRY MODEL 0.2.0 IS READY FOR INTEGRATION.**

This verdict applies to the Entry Model component on exact head `822ee85604df5add7a9b0a594ae4a7f91694e971`. It is not a declaration that the entire trading strategy is production-ready.

The pull request remains Draft. No merge, tag, release, force push or automatic integration is authorized.