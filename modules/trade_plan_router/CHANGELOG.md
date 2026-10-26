# Changelog

## 0.1.0 — Draft integration candidate

- Materialized the deterministic Decision Router and Module API adapter.
- Added strict upstream schema/linkage/time validation and typed fail-closed
  business rejections.
- Added Long/Short and multi-setup resolution, pending-order policies, existing
  position/reversal/scaling semantics and portfolio risk/exposure checks.
- Added immutable executable plans, canonical IDs/hashes, audit traces and
  optimistic-concurrency preconditions.
- Added explicit position intents and close-before-open reversal sequencing.
- Added standalone reduce-only REDUCE/CLOSE composition from approved scaling
  decrease decisions with exact position OCC and stable scaling-candidate
  identity.
- Hardened cyclic/deep/oversized payload handling, Unicode/log safety and input
  resource limits.
- Added deterministic replay, property/metamorphic/concurrency tests,
  performance profiling, static security checks and reproducible wheels.
- Added bounded canonical JSON serialization/deserialization for plans and
  routing results with exact nested schemas and ID/hash verification.
- Added reproducible package metadata, wheel-content, clean
  uninstall/reinstall, entry-point and dependency lifecycle gates.

### Decimal and canonical boundary hardening

- Made canonical Decimal formatting and trading-domain arithmetic independent
  of process-global Decimal precision and rounding mode.
- Canonicalized Decimal text in OCC preconditions so equivalent numeric scales
  preserve plan and result identity.
- Enforced exact schema-version and feature-envelope types, bounded mapping and
  sequence inputs, and primitive-only text/numeric coercion.
- Rejected arbitrary Python objects, non-finite numbers, naive datetimes,
  malformed mapping keys and unsafe Unicode/control text before hashing.
- Replaced context-sensitive Decimal operations with exact helpers and retained
  Fraction-oracle validation for critical arithmetic.

### Portfolio identity and rejection integrity

- Added an immutable canonical content hash to `PortfolioSnapshot` and carried
  it into package correlation identity, plan input hashes and executor
  `portfolio_content_hash` preconditions.
- Replaced monolithic large-snapshot hashing with deterministic per-position and
  per-order hashes.
- Hardened pending-order status, type, remaining-quantity and price geometry.
- Made duplicate decision ordering stable by `(logical ID, input hash)` and
  coalesced public rejection records while retaining delivery evidence in the
  audit trace.
- Namespaced orphan upstream rejection IDs and exposed orphan Protection,
  Sizing and Scaling decisions instead of silently discarding them.
- Bound Router collections to the global item ceiling and attached a compact
  cryptographic input inventory to accepted and rejected results.

### Downstream handoff contract

- Added `DownstreamPlanEnvelope` and `DownstreamContractError` as public API.
- Bound each handoff to the exact canonical Router JSON with a separate payload
  SHA-256.
- Preserved `plan_id`, `content_hash`, `audit_hash`, validity timestamps and
  exact ordered string OCC preconditions.
- Added fail-closed executor action-capability and validity-window checks.
- Added contract tests for tampering, duplicate/mismatched preconditions,
  idempotent replay and immutable precondition mappings.
- Documented that downstream executors must not coerce Router string/SHA-256
  evidence into incompatible integer state versions.

### Remote reconciliation and validation

- Rebuilt the active test suite as normal tracked source, including an
  executable 30-scenario golden registry.
- Removed obsolete corrupted base64/gzip recovery utilities, failed recovery
  diagnostics and the failed baseline report from the active branch.
- Isolated self-hosted workflow checkouts under dedicated worktrees to prevent
  concurrent module jobs from contaminating Router validation.
- Added bounded dependency-install retries/timeouts and compact evidence
  artifacts that exclude virtual environments and wheel workspaces.
- Incorporated integration commit
  `b4860c94dafb1d398cb36088d16c32bfcd065fc4` through the normal,
  history-preserving merge checkpoint
  `d51ad56fe88d07fed0f1794240466a7d284e2712`; repository comparison reports
  `behind_by=0`.
- Added a cross-platform compatibility gate against validated Core 0.4.2 exact
  head `04f6de01e5337883256299dd20effdbedd5ac73e`.

Latest verified evidence:

- validation run `30060866166`: **349 passed**, Ruff **PASS**, strict MyPy
  **PASS** across 13 source files, branch-aware coverage **90.3750%** with a
  hard 90% threshold;
- final run `30060866133`: security/type audits, Bandit, `pip-audit`, 30/30
  golden scenarios, four-profile deterministic replay, 10,000 fuzz cases,
  concurrency/soak stress, byte-identical wheel pairs and clean
  install/uninstall/reinstall all **PASS**;
- Core 0.4.2 compatibility run `30060866128`: **349 passed**, Ruff/MyPy and 90%
  coverage **PASS**, dependency check and clean Core 0.4.2 + Router wheel import
  **PASS**;
- core, FVG, session-range, market-regime and runner-routing fast checks passed.

Compatibility wheel SHA-256 values:

- Core 0.4.2: `392d1c0fc1b91118d87cafef3999f094342e7476d0fcf98be3a2bb44027eb9c1`;
- Router 0.1.0: `6a914faeb8b6bc3d4e6d4a380bf82f3d52388749255f5cae894cf17a0d6a4247`.

This changelog describes a Draft integration candidate. It does not imply a
GitHub Release, package publication, merge approval or live-trading readiness.
