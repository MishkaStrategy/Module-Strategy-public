# Round 10 — parameter-schema preflight

- Goal: reject malformed module configuration schemas before registry loading or module construction.
- Baseline: unknown types, non-boolean `required`, inverted bounds, duplicate enums and invalid defaults could be stored in a manifest and fail later or be silently accepted.
- Change: manifest-time validation of parameter names/rule mappings, supported types, required flag, finite bounds, bound order, enum values and defaults.
- Tests: eight adversarial schema cases plus the full real-module suite.
- Config safety: malformed schema fails before dependency planning or constructor side effects.
- Compatibility: all existing audited module schemas pass unchanged.
