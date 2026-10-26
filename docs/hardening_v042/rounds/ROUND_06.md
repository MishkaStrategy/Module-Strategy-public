# Round 06 — strict checkpoint and state inputs

- Goal: reject ambiguous checkpoint/state types before restore or compatibility checks.
- Baseline: boolean/float/string indices were accepted or failed incidentally; module state payloads were not required to be mappings; schema versions could be coerced from integers or contain surrounding whitespace.
- Change: exact integer index validation, mapping-only module-state payloads, canonical schema declarations, and no persisted schema coercion.
- Tests: invalid index matrix, invalid payload, noncanonical supported schema and integer persisted schema.
- Recovery: malformed state fails before any module mutation.
- Compatibility: correctly serialized v0.4 checkpoints already use integer indices, mapping payloads and string schema versions.
