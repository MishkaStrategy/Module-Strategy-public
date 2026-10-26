# Round 09 — canonical run/config fingerprints

- Goal: ensure semantically equivalent configurations receive one run identity and invalid non-finite values cannot enter hashes.
- Baseline: `-0.0` and `0.0` produced different fingerprints; nested NaN/Infinity were serialized as non-standard JSON tokens.
- Change: finite-float validation, signed-zero canonicalization and JSON `allow_nan=False` defense in depth.
- Tests: mapping-order plus signed-zero invariance and nested non-finite matrix.
- Determinism: equivalent configuration objects now produce identical `config_hash` and `run_id`.
- Compatibility: ordinary finite values retain canonical JSON hashing; previously invalid non-finite configurations now fail closed.
