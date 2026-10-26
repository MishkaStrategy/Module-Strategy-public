# Modular Strategy v0.6 — Paper Execution & Venue Reconciliation checkpoint

Status: PUBLISHED FROM AUTHORITATIVE COMPONENT AND ALL-MODULES INTEGRATION EVIDENCE

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Starting integration checkpoint: `73bbec9c99b697ef354e65f4fe8bc81427fdb308`
- Integration-ready Paper source and live head: `57283323c38255c1a259df42a1e1388dcd260483`
- Resolved source head: `c367b8affeed05b45f4aefaf27522752b534d87a`
- Ordinary two-parent merge candidate: `fc66829d24594e914e0ab90307bdcdc6e309c8e0`
- Shared normalizer compatibility repair: `267ff5783046aac0ecd6d4866d401ecf8e00d580`

The merge candidate preserves the exact Decision Router checkpoint as its first parent and the complete Paper Execution history plus explicit shared-file resolutions as its second parent. No merge targeted `main` or frozen Core.

## Live-head classification

The Paper Execution live head is the exact integration-ready evidence SHA. No post-evidence product, test, workflow or documentation commits exist.

## Shared-file conflict resolution

Two shared paths diverged from the current central integration branch:

1. `.github/workflows/integration-ci.yml` — the Paper branch carried a component-era full-integration copy. Integration retained the exact central workflow rather than replacing central ownership.
2. `scripts/normalize_sdist.py` — both central and Paper had independently hardened implementations.

The final shared normalizer is a backward-compatible security superset:

- retains central `normalized_sdist_bytes()` and explicit destination support;
- supports Paper's in-place `normalize_sdist(source, epoch=...)` call;
- rejects symlink/non-regular archive inputs;
- rejects negative epochs with the Paper-compatible error contract;
- enforces canonical safe member paths, duplicate rejection, member-count and uncompressed-size limits;
- rejects special archive members;
- writes atomically, fsyncs the file and destination directory;
- retains deterministic metadata and gzip normalization.

Paper's four root normalizer regression tests were executed locally against the exact combined implementation: **4 passed**. Paper product code and public execution contracts were not changed during integration.

## Paper-only and authority boundaries

- only paper execution is accepted;
- no credential, signing, private REST/WebSocket, live adapter or live feature flag exists;
- frozen Core remains authoritative for broker simulation, orders, fills and positions;
- no second broker, OMS, accounting ledger, event bus or position store was introduced;
- Paper executes already-approved plans and does not choose signal, direction, quantity, risk, stop or target;
- unsupported reversal remains typed-rejected pending an approved close-before-open contract.

## Router boundary

Paper consumes the canonical `DownstreamPlanEnvelope` and preserves:

- canonical Router JSON;
- payload, content and audit hashes;
- `plan_id`, validity interval and typed action;
- exact ordered string `ExecutionPrecondition` values;
- OCC evidence without coercion into incompatible integer state versions.

Open, add, reduce and close are supported. Factual position identity, side, quantity and time are validated. Tampering, stale validity, OCC missing/duplicate/mismatch, replay conflict and plan-ID conflict fail closed. Identical replay is idempotent.

## Durable transaction, recovery and OCO

The accepted transaction ordering is:

`validated plan → execution.intent_recorded → Core venue side effects → pre-acceptance orchestration → plan.accepted`

The module provides:

- compensation and scope blocking after failed acceptance persistence;
- market and guarded Decimal/float precision boundaries;
- fresh timestamped venue marks for deferred protection;
- verified baseline plus factual add-fill protection;
- synthetic OCO built only from submitted Core orders;
- cancellation state committed only after positive Core confirmation;
- fail-closed residual, late-fill, cancel-failure and missing-state handling;
- SHA-256-attested recovery schema v4 with v1-v3 migration;
- persisted idempotency, scopes, order projections, OCO/protection state and event fingerprints;
- exact Core EventStore attestation and transactional restore;
- non-decreasing journal and per-order timestamps.

## Permanent component gate

Workflow `paper-execution-ci`, run `30099907565`, head `57283323c38255c1a259df42a1e1388dcd260483`: **SUCCESS** on Python 3.12 and 3.13.

Each lane passed:

- exact Decision Router checkout at `f779e599b361a7b860506980de6b5ae8d0cd1f01`;
- dependency installation and `pip check`;
- compileall, Ruff and strict MyPy;
- paper-only network/security scan and Bandit;
- 263 Paper Execution tests, zero failures/errors/skips;
- exact branch-aware coverage gate at 85%;
- reproducible wheel/sdist, Twine and clean Core + Paper wheel installation/import.

Component artifacts:

- Python 3.12 artifact `8599160913`, digest `sha256:e2da36aee88b39247264b8317a1fa82d678ab113171feede5553c61c35d9314d`;
- Python 3.13 artifact `8599160593`, digest `sha256:433001bc0f03ebdaeae8c733af7696556fdd3932d9e8180dabac8348fedddb49`.

## All-modules integration gate

Workflow `integration-full-ci`, run `30099802813`, exact Paper head: **SUCCESS** on Python 3.12 and 3.13.

Per lane:

- root/Core/contract: 248 passed;
- Session Range: 84 passed;
- Market Regime: 80 passed;
- Paper Execution: 263 passed;
- FVG: 70 passed;
- total: 745 tests per lane / 1,490 across the matrix;
- zero failures, errors or skips;
- strict MyPy: 32 source files, no issues;
- root coverage: 88.27%;
- Paper combined branch-aware coverage: 85.0360%;
- release-candidate build, Twine, reproducibility and clean-wheel integration smoke: PASS.

Additional exact-head checks `30099763982` and `30099763960`: **SUCCESS**.

Integration artifacts:

- Python 3.12 artifact `8599124528`, digest `sha256:bf8893ae5fc73144a7db82718b2e2dfbaa9213c4fb9094c72103b947ce77ad22`;
- Python 3.13 artifact `8599124328`, digest `sha256:f5c2301ad36e2ca4c910efe19c86ec0379096349c39d5581126cc71d53f72eec`.

Reproducible Paper artifacts:

- wheel `d3c47362c3b128a3e80f9e0cf023645f425a168f3e91bd01f035c8349fa8e09b`;
- sdist `1cb3d3eed50fc10b8444b803383fe97353d5abf867e2fffab8cc04075edd6061`.

## Current limitation

The unavailable `[self-hosted, fast]` queue prevented a redundant rerun on the complete current central head. This checkpoint does not invent such a run. Full release-candidate exact-head testing of the complete integrated chain remains mandatory before any merge to `main`.

## Safety state

- PR #168 remains open and Draft;
- paper-only boundary remains enforced;
- no force-push, rebase or history rewrite;
- no merge to `main` or frozen Core;
- no tag, release, package publication or live execution.

## Next executable component

Position & Exit Management must be located and exact-head classified before integration. Its integration must preserve Paper's factual-fill and reconciliation authority, enforce exit priority over additions, and avoid creating a second position or execution state store.
