# Intermediate audit after rounds 25–26

## Decision
**Continue.** Structured failure evidence and self-verifying export artifacts were added without changing the existing journal keys or run-identity contract.

## Changes
- Skip records now expose stable `reason_code` and optional `reason_detail` while retaining the legacy `reason` value.
- Kernel error records/events now include `error_type` and `message` while retaining the legacy repr field.
- Export bundles now contain replay verification, environment/dependency evidence and SHA-256 checksums.
- JSON evidence is emitted with stable key ordering.

## Invariants affected
- Audit evidence remains append-only.
- Run identity remains environment-independent.
- Evidence files verify their own integrity without including the checksum file in its own manifest.
- A controlled module exception is journalled only when the existing non-strict error policy is selected.

## Public contracts
No public field or import was removed or renamed. Additions are backward compatible. The `RunManifest.created_at` field remains intentionally wall-clock evidence and is excluded only from the reproducibility assertion, not from the artifact.

## Evidence
- Full suite: **189 tests passed**.
- Branch coverage: **83%**.
- `compileall`: passed.
- Export replay verification: passed.
- Every exported artifact digest was recalculated by regression test.
- Repeated runs produced equal metrics, replay result and normalized event stream.

## No-look-ahead / determinism
No market-data visibility or event ordering behavior changed. Determinism checks explicitly preserve the legitimate difference in run-manifest creation timestamp while comparing the causally relevant event payloads.

## Restart/replay
The export invokes the same public replay verifier used by recovery evidence. Invalid sequence/hash chains remain visible as a failed `replay.json`, rather than being silently accepted.

## Regression risks
- External consumers that deserialize journal rows into closed schemas must tolerate additive keys.
- Environment evidence is intentionally host-specific and must not be used as the research run identifier.

## Residual risks
The environment fingerprint currently records the kernel runtime and direct PyYAML version, not a complete transitive SBOM. That is addressed in rounds 27–28.
