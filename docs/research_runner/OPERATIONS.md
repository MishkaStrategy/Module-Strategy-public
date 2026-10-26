# Research Runner operations

## State and resume

Use one SQLite database per research workspace. The repository rejects symlink database
paths, uses foreign keys and `BEGIN IMMEDIATE`, and records every run transition in an
append-only event table. A run claim has one owner and an expiry. The scheduler extends
leases while work is active and may recover expired claims after a process or machine
failure.

Successful run directories are verified on resume by default. A missing output directory
changes the run to `CORRUPTED`; it is never silently executed again under the same run ID.

## Worker modes

`thread` is the compatibility default and accepts ordinary programmatic config factories.
`process` uses the Python spawn context so behavior is compatible with macOS and Windows.
The adapter, config factory, run function, job parameters, and metadata must be pickle-safe.
Research specifications cannot name or import callables.

The current release is local-only. GitHub Actions validates the package but is not a remote
Research Runner transport. Cross-machine execution requires a separately versioned transport
contract and durable claim protocol.

## Validation workflows

Research Runner workflows use the movable `[self-hosted, fast]` role and pinned uv-managed
Python environments so the same gates can run on heterogeneous macOS, Linux, or Windows fast
runners.

- ordinary PR synchronization runs the focused Python 3.12 component gate;
- `full-integration` runs the Python 3.12/3.13 component matrix and centralized repository
  integration matrix;
- `release-validation` runs the complete suite under `python -O`, builds wheel and source
  distribution twice, canonicalizes source archives, compares SHA-256, and performs a clean
  source-distribution installation and metadata/CLI smoke test;
- `security-validation` runs Bandit over production source and release tooling, scoped secret
  detection, strict runtime dependency audit, CycloneDX SBOM generation, and checksummed
  evidence publication;
- `heavy-validation` runs deterministic high-volume and durable-state checks at stress scale
  `5` when triggered from a pull request. Manual `workflow_dispatch` may provide a different
  scale.

Heavy validation remains on the fast role because it validates orchestration, SQLite state,
and deterministic planning rather than running historical market backtests. Historical
strategy workloads belong to the qualified backtest execution layer.

## Evidence handling

Every label-triggered workflow checks out the exact pull-request head. Release, security, and
heavy artifacts record the candidate head or are bound to it by the GitHub Actions artifact
metadata. Security evidence includes SHA-256 checksums; release evidence includes JUnit,
installed metadata, and reproducible package hashes.

Do not reuse evidence after the feature head changes. Re-run the relevant gates and update the
Draft PR evidence to the new exact head.

## Final-test procedure

Complete train, activate and complete validation, activate and complete test/OOS, then issue
a final grant for the chosen hashes. Activation consumes the grant atomically with job
registration. Do not issue a replacement grant to add candidates after seeing final results;
that would be a new experiment with a new specification and lineage.

## Release procedure

1. Confirm the integration base has not advanced or merge it explicitly before validation.
2. Run focused, full-integration, release-validation, security-validation, and
   heavy-validation on one exact feature head.
3. Verify artifacts, checksums, JUnit counts, package hashes, and exact-head metadata.
4. Confirm the PR remains mergeable and has no unresolved review threads, change requests, or
   outstanding comments.
5. Keep the PR Draft until a human architecture, methodology, security, and release review is
   complete.
6. Never enable auto-merge for this release candidate.
