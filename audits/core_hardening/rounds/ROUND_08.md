# Round 08 — feature deletion, expiry and cache semantics

- Feature deletion selectors now reject ambiguous blank values.
- Expiry requires timezone-aware timestamps.
- Expiry/deletion continue to invalidate cached snapshots deterministically.
