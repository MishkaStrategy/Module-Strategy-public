# Decision Router & Trade Plan Composer — Remote Checkpoint

Status: **BASE-SYNCHRONIZED AND CORE 0.4.2-COMPATIBLE — DRAFT CHECKPOINT**

This document records the verified Router checkpoint after a normal,
history-preserving integration-base update plus an independent compatibility
check against the accepted Core 0.4.2 candidate. It does not authorize merge,
release or live execution.

## Repository state

- Base branch: `integration/modular-strategy-v0.5.0`
- Latest integration commit incorporated:
  `b4860c94dafb1d398cb36088d16c32bfcd065fc4`
- Working branch: `modules/trade-plan-router-v0.5`
- Base-sync merge checkpoint:
  `d51ad56fe88d07fed0f1794240466a7d284e2712`
- Draft PR: `#95`
- Module package version: `0.1.0`
- Comparison to incorporated integration commit: `behind_by=0`

## Verified validation gate

GitHub Actions run `30060866166` passed with:

- **349 passed**, no failures, errors or skips;
- Ruff: **PASS**;
- strict MyPy: **PASS** across 13 production source files;
- branch-aware coverage: **90.3750%**;
- hard coverage threshold: **90%**, not lowered;
- exact pinned Entry Model, Protection & Target, Position Scaling and Risk
  Sizing contract checks.

## Verified final gate

GitHub Actions run `30060866133` passed:

- production security and public type-surface audits: **PASS**;
- Bandit and `pip-audit`: **PASS**;
- executable golden registry: **30/30**;
- deterministic replay across four timezone/hash-seed profiles: **PASS**;
- serialization fuzz: **5,000 cases, PASS**;
- risk-reduction fuzz: **5,000 cases, PASS**;
- concurrency and soak stress: **PASS**;
- two Core wheel builds: **byte-identical**;
- two Router wheel builds: **byte-identical**;
- clean install, import, uninstall, reinstall and `pip check`: **PASS**.

Verified Router wheel pair SHA-256:

`9412e4142e02375017f487d3248780b28f9f1b1da2e8a8f1578ac78cc1d4d741`

## Core 0.4.2 compatibility

GitHub Actions run `30060866128` passed against exact Core candidate
`04f6de01e5337883256299dd20effdbedd5ac73e`:

- installed Core version: `0.4.2`;
- installed Router version: `0.1.0`;
- full Router suite: **349 passed**;
- Ruff and strict MyPy: **PASS**;
- branch-aware Router coverage: **90%**;
- Core/Router dependency check: **PASS**;
- clean Core 0.4.2 + Router wheel installation/import: **PASS**.

Compatibility wheel SHA-256 values:

- Core: `392d1c0fc1b91118d87cafef3999f094342e7476d0fcf98be3a2bb44027eb9c1`;
- Router: `6a914faeb8b6bc3d4e6d4a380bf82f3d52388749255f5cae894cf17a0d6a4247`.

This is an independent compatibility result. It does not replace integration
validation and does not merge Core PR #96.

## Base-sync cross-checks

The current workflow checkpoint also passed:

- core fast CI `30060866146`;
- FVG fast CI `30060866178`;
- session-range fast CI `30060866132`;
- market-regime fast CI `30060866137`;
- runner-routing policy `30060866174`.

## Downstream execution boundary

The package exports `DownstreamPlanEnvelope` as the explicit handoff contract
for Paper Execution and other executors. The envelope preserves canonical JSON,
payload SHA-256, plan/content/audit identity, validity timestamps and exact
ordered string `ExecutionPrecondition` values.

A downstream executor must reject unsupported actions and stale validity/OCC
state. It must not convert Router string/SHA-256 evidence into incompatible
integer state versions or recalculate side, quantity, risk, stop, targets or
sequencing.

## Runner isolation

Router validation, final and Core compatibility workflows use dedicated
isolated checkouts with pinned contracts, bounded install retries/timeouts and
compact report-only artifacts.

## Historical transport

The corrupted base64/gzip recovery path has been removed. Production source and
the complete active test suite are normal tracked files. Failed recovery and
baseline reports are not part of validation or release.

## Safety

PR #95 remains Draft. No merge, force-push, history rewrite, release tag,
GitHub Release, package publication, live-trading activation or auto-merge is
authorized by this checkpoint.
