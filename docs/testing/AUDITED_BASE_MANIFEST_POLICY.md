# Audited-base manifest policy

The test-architecture comparison must validate the historical audited tree without pretending that head-only suite paths existed in that tree.

The current audited SHA predates `config/test_suites.toml`, so a historical revision-owned manifest is not available. The workflow therefore materializes a **base-compatible projection** from the current validated head manifest: it keeps only suite working directories and test paths that actually exist in the audited worktree, removes genuinely head-only entries from projected groups, and then validates the projected historical tree fail-closed before collecting node IDs.

This projection is not permission to ignore removed tests. The audited worktree is scanned with the projected manifest using the same static inventory gate. Any historical test or fixture that exists outside the projected owner paths remains unmapped and fails the base validation. Existing suite paths are collected from the historical bytes, so any node ID that existed on the audited base but is absent on head is still reported as a removal and fails comparison.

This distinction is required when a pull request introduces a genuinely new isolated component or adds a new test path to an existing component. A head-only suite or path is absent from the projection and therefore contributes zero historical node IDs; the corresponding head IDs appear only as additive evidence.

## Verified test-file relocations

A test may be reorganized into a different file without being semantically removed. Raw pytest node IDs include the file path, so an exact string comparison would otherwise report every test in that file as deleted and re-added.

The `[node_id_relocations]` table in canonical `config/test_suites.toml` is the only relocation authority. It shares the manifest's exact `audited_base_sha`, so a relocation-only change also runs the existing Test Architecture path gate. Mappings are exact source-file-to-target-file relative POSIX `.py` paths: no glob, prefix, directory, traversal, absolute path or same-path mapping is accepted, and target paths must be unique.

A historical node ID is classified as relocated only when all of these conditions hold:

- its source file exactly matches a declared relocation source;
- the source node ID is absent from head and the target node ID is genuinely additive;
- replacing only the file path produces an exact target node ID present in the same suite;
- the complete pytest suffix after `::` — class/function/parameter identity — is byte-identical;
- the manifest audited-base SHA exactly matches both collection reports.

Therefore a mapping cannot hide a deleted or renamed test. If the mapped target is missing, the test suffix changed, a target is ambiguous, the path is unsafe, or the manifest identity is stale, comparison fails closed. Relocated IDs are emitted as separate evidence and are never counted as exact-match IDs. `--require-exact` still rejects relocations; it is used for deterministic repeated head collection where no node ID is allowed to change.

## Verified parameter-ID migrations

The `[node_id_parameter_migrations]` table is a separate, narrowly scoped audited-base exception for a parameter case whose pytest ID changes while its test remains the same. Each entry must use the exact same relative `.py` path and exact class/function identity; only the final bracketed parameter ID may differ. Entries are bound to the same exact `audited_base_sha` as file relocations. A normal audited-base comparison requires each declared source to occur exactly once before, be absent after, and have a target that did not occur before and occurs exactly once after in that same suite. Missing, retained, pre-existing, cross-suite, ambiguous, or unused migration entries fail closed. For `--require-exact`, parameter mappings remain inert only when the complete suite-to-node-ID sets are identical; any changed ID remains a raw difference and fails the exact comparison. Migration counts are reported separately when applied. The existing file relocation table and its suffix-preservation contract remain unchanged.

The sole current migration preserves the audited tampering case after the SQLite audit store advanced from schema 1 to schema 2. `research/strategy_audit_validation/src/strategy_audit_validation/store.py` declares `_SCHEMA_VERSION = 2`, accepts existing versions `None`, `1`, and `2`, and rejects other values in `_initialize`; `research/strategy_audit_validation/tests/test_request_binding_migration.py::test_unsupported_or_incomplete_v2_schema_fails_without_repair` continues to exercise future version `99`. Accordingly, the unchanged tampering assertion in `research/strategy_audit_validation/tests/test_edges.py` uses value `99` to retain the unsupported-future-schema invariant. The migration changes only that historical parameter ID (`'2'` to `'99'`); it does not change the SQL assertion, test body, suite, or audited baseline.

Fail-closed invariants:

- head static inventory contains zero unmapped test and fixture files;
- the audited projection contains only paths that physically exist in the audited worktree;
- audited static inventory also contains zero unmapped test and fixture files;
- selected-suite dependency preflight passes in each revision-specific environment;
- repeated collection on the same head matches exactly;
- audited-base-to-head comparison rejects every unverified removed node ID;
- verified file relocations preserve only exact test/class/parameter suffixes and are reported separately;
- verified parameter-ID migrations preserve only the exact file and test/class identity, are reported separately, and require the audited source to disappear as the target appears in the same suite;
- new head-only suites and paths are additive evidence only;
- no historical source file is copied from head into the audited worktree.

The head versions of collection tooling may be copied into the audited worktree solely to record and compare node IDs consistently. The generated projected manifest is evidence of mapping compatibility, not a claim that the historical commit contained that manifest.

## CI evidence policy

The focused acceptance workflow runs on `[self-hosted, fast]`; it must not fall back to an unqualified self-hosted selector or silently move the audited collection contract to a different runner class. The workflow checks out and explicitly verifies the exact pull-request head before validation.

After collection and comparison, the workflow creates a deterministic run-bound evidence manifest containing the exact head SHA, workflow run identity, relative report paths, byte sizes and SHA-256 hashes for every bounded `reports/test-architecture/**` report. The canonical manifest is printed to the workflow log and its own SHA-256 is recorded in the job summary.

A successful Test Architecture acceptance also requires immutable publication of the manifest and bounded report directory directly to a GitHub Release from the same job through `.github/actions/publish-release-evidence`. The publisher binds the Release to the exact commit SHA, packages directory evidence deterministically, refuses to replace an existing differing asset, and publishes a SHA-256 sidecar. Publication is blocking, is not `continue-on-error`, and is followed by an explicit publication-success gate.

This publication requirement does not weaken or replace the underlying correctness checks: static inventory, dependency preflight, repeated collection, audited-base projection, relocation validation, and no-unverified-removal comparison all remain authoritative and blocking before publication.

GitHub Actions artifact storage is not the durable Test Architecture evidence store. Under the organization storage policy adopted in PR #611, Actions artifacts are temporary failure/cancelled diagnostics with one-day retention, while canonical successful evidence uses the repository evidence/Release surfaces. Exhausted Actions artifact quota must therefore not be handled by replacing durable evidence with mutable logs, nor by extending Actions retention for successful acceptance.

This policy closes Issue #372 without globally skipping missing paths, weakening test ownership, rewriting the audited revision, or replacing immutable acceptance evidence with mutable logs/job summaries.
