# Round 05 — strict event-envelope inputs

- Goal: reject ambiguous event identity and payload values before persistence or replay.
- Baseline: booleans/floats could pass as sequence numbers; blank optional IDs were accepted; non-mapping payloads were deep-frozen without enforcing the public contract.
- Change: exact positive integer sequence validation, nonblank validation for present correlation/causation/symbol IDs, and mapping-only payload validation.
- Tests: invalid sequence type matrix, blank optional IDs and non-mapping payload.
- Determinism: canonical event identity now excludes ambiguous coercions.
- Compatibility: valid existing event envelopes are unchanged.
