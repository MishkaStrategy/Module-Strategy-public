# Changelog

## 1.1.1 — 2026-07-21

### Error-discovery hardening

- replaced production assertions with explicit fail-closed validation that remains active under `python -O`;
- rejected boolean contract identifiers and non-string payload schema versions;
- made public module parameters and nested manifest schema immutable;
- rejected Decimal parameters that cannot be represented as finite Module API numbers;
- bounded configuration strings, checkpoint identities, numeric text and 64-bit counters;
- required exact integer state versions, bounded string module versions and SHA-256 config fingerprints;
- made checkpoint digests strict JSON (`allow_nan=False`) and wrapped recursive/non-JSON state failures;
- made legacy checkpoint duplicate replay fail closed when the historical event fingerprint is unavailable;
- preserved compatible schema-2 restore from module 1.1.0;
- normalized nondeterministic feature-fingerprint failures to `ProtectionTargetDataError`;
- added post-error adversarial, optimized-runtime, hash-seed and immutability regressions.
- completed 231 local tests, 93.34% combined coverage, clean-wheel testing and one-million-event restart stress.

## 1.1.0 — 2026-07-21

### Correctness and safety

- made Decimal serialization independent of ambient Decimal context;
- isolated financial arithmetic in a module-owned deterministic Decimal context;
- rejected non-finite, oversized and unsupported nondeterministic input values;
- required an exact point-in-time FeatureView timestamp;
- detected changed feature/gap context on duplicate bar delivery;
- rejected ambiguous equally-current structural features;
- bounded diagnostic result details;
- introduced state schema 2 with SHA-256 integrity digest and transactional validation;
- added explicit migration from compatible 1.0.0 schema-1 checkpoints.

### Performance and verification

- removed repeated EntryPlan fingerprinting and recursive JSON hashing from the event hot path;
- improved the local five-run 2,000-plan median throughput by approximately 11.3% over 1.0.0;
- kept traced peak memory effectively unchanged and bounded persistent state at 4,096 entries by default;
- added 173 local unit, edge, regression, contract and real-Core integration cases;
- added deterministic replay and one-million-event multi-process restart stress evidence;
- added local release, handoff and integration-contract documentation.

## 1.0.0 — baseline

- initial independent protection and target planning plugin.
