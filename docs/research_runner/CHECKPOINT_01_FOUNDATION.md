# Checkpoint 01 — Deterministic Research Foundation

Status: `LOCAL DEVELOPMENT — NOT READY`

## Implemented

- independent package `modular-strategy-research-runner==0.1.0`;
- immutable typed Research Specification foundation;
- strict YAML/JSON loading with unknown-field rejection and `safe_load`;
- dataset-lineage contract and dataset fingerprints;
- type-preserving canonical JSON and SHA-256;
- deterministic experiment, configuration and run IDs;
- semantic seed hierarchy;
- deterministic finite grid search;
- deterministic random search without replacement;
- declarative cross-parameter constraints;
- fixed and percentage chronological splits;
- optional final untouched-test split;
- rolling and anchored walk-forward planning with purge/embargo;
- experiment manifest foundation with code/data/environment provenance;
- atomic checksummed local JSON artifact writes;
- thin adapter to the official Backtest Engine application contract.

## Architecture audit

- second Backtest Engine created: **No**;
- trading logic added: **No**;
- execution simulation added: **No**;
- second metric engine added: **No**;
- network or exchange access added: **No**;
- arbitrary code execution from YAML/JSON added: **No**.

## Local verification

- Research Runner tests: **66 passed**;
- branch-aware package coverage: **93%**;
- compileall: PASS;
- line-length scan at 100 columns: PASS;
- wheel build: PASS;
- clean wheel installation with exact Backtest Engine wheel: PASS;
- `pip check` in isolated environment: PASS;
- public Backtest Engine `RunOutcome` adapter smoke: PASS;
- integration-base baseline: **244 passed**;
- Backtest Engine exact-head focused suite: **113 passed**.

Research Runner wheel SHA-256:

`883363f9af5e902a52c49101d3f031ea20160b37c3c4252ef57f6241e7c0b1b0`

Backtest Engine wheel used for contract smoke:

`13cbbc7608473ffa6a42089395c28db4084ad501db1603ca005ddd57afbdfb42`

## Tooling limitations

Ruff and MyPy executables were not available in the local container and could not be
installed without network access. Their configuration is included, but this
checkpoint does not claim those gates as passed. They remain required before a local
release candidate.

## Next checkpoint

Implement the experiment state machine, all-runs index, atomic checkpoint/resume,
retry classification, cancellation and deterministic local process scheduler.
