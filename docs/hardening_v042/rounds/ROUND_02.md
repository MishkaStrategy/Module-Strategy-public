# Round 02 — strict configuration types

- Goal: reject runtime coercion of integers/floats into boolean and integer configuration fields.
- Baseline: dataclass type annotations are not runtime validators; `1`, `0` and `60.5` could enter typed config fields.
- Change: explicit boolean validation for execution/engine switches and exact positive-integer validation for interval/history capacities.
- Tests: constructor matrix for invalid runtime types plus full repository regression.
- Compatibility: valid YAML/Python values are unchanged; only invalid coercions fail earlier.
