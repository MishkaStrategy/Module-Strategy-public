# Research Runner final architecture audit

Date: 2026-07-24

## Scope

This audit covers the public specification and result schemas, deterministic planning and scheduling, phase promotion, durable SQLite state and corruption handling, Backtest Engine delegation, Results Aggregator ingestion, CLI and package surfaces, and release, security, integration and recovery workflows.

## Strict-boundary hardening completed

- Specification decoding rejects implicit scalar, collection, integer and enum coercions and exposes contract-level `SpecificationError` failures.
- Backtest Engine outcomes and runtime models reject bool-as-integer, string-as-boolean and null-as-string coercions.
- Durable job, failure and result payloads are validated after canonical decoding and fail closed with `StateRepositoryError` when stored state is malformed.
- Result envelopes validate identifiers, seeds, engine ownership, metadata, artifact references, timestamps and schema versions before publication to Results Aggregator.
- Percentage split parsing rejects NaN and Infinity values.
- Focused CI retains Ruff diagnostics when lint fails, and heavy validation retains JUnit plus the complete pytest log on every outcome.

## Coordinated remediation completed

The release-blocking findings recorded on Draft PR #119 were closed by the remediation beginning at code head `816a5d62a90a60028c6c28742c120684658f6434` and by the independent post-remediation hardening below.

### P0 — lease fencing and stale completion

- Every scheduler invocation creates a unique scheduler session identity.
- Every durable claim receives an opaque per-claim lease token and a claim revision.
- `heartbeat()`, `succeed()`, `fail()` and active-claim cancellation require the complete worker/session/token/expected-revision tuple in one SQLite transaction.
- Lease expiry is checked at the durable mutation boundary; an expired claim cannot heartbeat, complete, fail or cancel.
- Heartbeat renewal increments both the durable run revision and claim revision, invalidating the previous authorization tuple.
- The `run_claims` ownership table and cleanup trigger do not expose claim tokens through ordinary run records.
- A two-scheduler regression reuses the same visible worker identity after expiry/recovery and proves that late heartbeat, completion and cancellation from the expired claim are rejected while the replacement claim succeeds exactly once.

### P1 — bounded and race-safe output verification

- The Backtest Engine adapter writes an atomic Research Runner sidecar outside the authoritative Engine output directory.
- The sidecar binds requested run ID, engine run ID, resolved output directory, processed-bar count, restart-verification status and the exact artifact inventory.
- Every artifact is checked by safe relative path, unchanged regular-file identity, size and SHA-256 through one file descriptor.
- Verification rejects file replacement or symlink substitution between inspection and open.
- Explicit ceilings limit each artifact to 8 GiB, total output to 64 GiB, the inventory to 100,000 artifacts and the sidecar to 16 MiB.
- Manifest-declared per-file and aggregate sizes are revalidated before output hashing.
- Missing, extra, modified, duplicate, oversized, special-file and symlink artifacts fail closed.
- Keeping the sidecar outside Engine output preserves Results Aggregator neutrality.

### P1 — cancellation and heartbeat

- Cancellation and durable publication are linearized through one publication guard.
- Pending work and every active durable claim transition to `CANCELED` before scheduler return.
- A late engine future cannot publish success or failure after cancellation.
- `heartbeat()` independently rejects non-positive and boolean lease durations before durable mutation.
- Active-future and heartbeat-boundary regressions are included.

## Independent post-remediation hardening

A second boundary review found and closed four adjacent fail-closed gaps:

1. **Output resource ceilings and race safety.** The initial sidecar bounded file count and manifest bytes but not per-file or aggregate output bytes, and file inspection was split across path operations. Verification now enforces byte ceilings and binds type, identity, size and digest checks to one descriptor.
2. **Retry and resume policies.** Direct construction rejects bool-as-integer, non-finite values, untyped retry categories and invalid boolean flags. Retry methods validate public arguments, and exponential backoff caps safely on overflow.
3. **Scheduler runtime construction.** Scheduler options reject non-finite, boolean or string poll intervals, incorrect policy objects, non-callable callbacks/factories and invalid executor-mode values before repository or executor side effects.
4. **Atomic SQLite schema lifecycle.** Python `sqlite3.Connection.executescript()` performs implicit transaction commits. The previous schema creation and v1-to-v2 migration therefore could not substantiate their transactional guarantee. Research Runner now parses complete SQL statements and executes them individually through the already active `BEGIN IMMEDIATE` transaction. Subclass-owned claim schema installation runs through a schema-extension hook in that same transaction. Fault-injection regressions prove that both a new repository schema and a v1-to-v2 migration roll back completely, including `repository_meta`, `run_claims`, triggers and schema-version state.

These changes remain inside orchestration, persistence and validation boundaries. They do not alter strategy logic, execution simulation, accounting, financial metrics or Results Aggregator ownership.

## State schema and migration

- New repositories use state schema version `2`.
- Version `1` repositories migrate to version `2` only when no run is `RUNNING`.
- Base schema DDL, claim-schema DDL and schema-version mutation commit or roll back together.
- Migration with an active unfenced version-1 lease fails closed; operators must resolve the active state first.
- Unsupported, malformed and non-numeric schema markers remain normalized to `StateRepositoryError`.
- No `executescript()` call remains in the Research Runner schema path.

## Exact code-head validation evidence

All evidence in this section is bound to clean code head `1e8ce02c05963ce60fd08fa22e71d043c9fb6f16`. The only subsequent product-branch change is this audit document; the final publication head must repeat the same exact-head gates before approval.

### Routing and focused gate

- Runner-routing-policy run `30079107984`: success.
- Focused Research Runner run `30079108012`: success through install, compile, Ruff, strict MyPy, complete focused tests, branch-aware coverage, authoritative module paths, wheel smoke and reproducible-wheel enforcement.
- Focused artifact digest: `sha256:75175150ee670ed6f3c04c2222751c1884e7a90e35d32ee6bd626fff13b9a7b4`.

### Full Research Runner matrix

Workflow run `30079209383`: success on Python 3.12 and 3.13.

- `376/376` tests per matrix;
- `0` failures, `0` errors and `0` skips;
- combined line/branch coverage: `92.08764455264759%`;
- statement coverage: `93.66138448707257%` (`3369/3597`);
- branch coverage: `87.83783783783784%` (`1170/1332`);
- authoritative Backtest Engine suite, wheel smoke, clean installation and reproducibility: success;
- reproducible wheel SHA-256: `fd84ac6d397300e2eccd865ff07856196e8e983c8f5918c34902cdde3a5ecafa`;
- artifact digests: Python 3.12 `sha256:9f198a51e6ca8da10afd18b9f5646c39a92bc8ae0226798f2c9d9472f3863696`; Python 3.13 `sha256:3142d8d4a650d184fd389af10ba3f3195f30826869e29c22eb05586c1348ac43`.

### Centralized integration

Workflow run `30079209320`: success on Python 3.12 and 3.13 through installation of core and every integrated module, compile/lint, deterministic smoke, root/contract/regression and external-module suites, all release-candidate wheel builds and clean installation.

Artifact digests: Python 3.12 `sha256:d1975ff37d721729b87781dafbaae87e2a063b6dd5a4999c7dc958c1e7653136`; Python 3.13 `sha256:5cd7913d77d0f3bf53a557d8faabb372bd5c99d4290aae199b5207dcc2af11f5`.

### Security and supply chain

Workflow run `30079308854`: success.

- Bandit: `0` findings and `0` errors over 6,951 lines;
- detect-secrets: `0` candidates;
- pip-audit: `0` known runtime dependency vulnerabilities;
- dependency inventory, CycloneDX SBOM and checksum enforcement: success;
- artifact digest: `sha256:ce26fde608db9955c56ee04cbe92f3fc24b02f0be13f709dab7cff7097423e51`.

### Release and reproducibility

Workflow run `30079405567`: success on Python 3.12 and 3.13.

- optimized suite: `376` tests per version, `0` failures, `0` errors and `3` documented optional cross-branch/import skips;
- two independent wheel and normalized-sdist builds are byte-identical;
- clean installation from sdist, dependency check, package metadata and CLI smoke: success;
- reproducible wheel SHA-256: `fd84ac6d397300e2eccd865ff07856196e8e983c8f5918c34902cdde3a5ecafa`;
- normalized sdist SHA-256: `b83dbb9dd7a7ba5165587d7b31631dcd79b9fcf602017ae8d49194a94c1cf07a`;
- installed metadata: `modular-strategy-research-runner` `0.3.0`, Python `>=3.12`;
- artifact digests: Python 3.12 `sha256:508bc194af994e667ab8834737df89385f5e7c0356d31bb0a4f78b94a633bdd4`; Python 3.13 `sha256:1eb0388ae1dfdcfd9f84295a5a3dba4f10863862a8c744d08aa6f53c9804f45b`.

### Heavy and recovery

Workflow run `30079491169`: success at stress scale `5` through deterministic high-volume and recovery checks. JUnit and complete pytest output were retained.

Artifact digest: `sha256:23b50bf8e915967fcb3f14d6298e34a1c6de54ca9cec9e17b6547e136a62bd85`.

## Architecture conclusion

Research Runner remains correctly bounded as an orchestration layer. It does not duplicate strategy logic, bar/order simulation, accounting or financial metric calculation. Backtests remain delegated to the authoritative Backtest Engine, while result packages remain neutral for the authoritative Results Aggregator.

The P0 stale-worker risk, all coordinated P1 findings, output resource/race gaps, direct policy/scheduler coercion gaps and SQLite transaction-integrity defect are closed with fail-closed runtime enforcement and adversarial regressions.

## Release disposition

Keep PR #119 Draft until the final publication head completes focused/full, centralized integration, release/reproducibility, security and heavy/recovery validation and receives explicit human architecture/release approval. Do not merge, enable auto-merge, mark Ready for review, tag or release without that approval.
