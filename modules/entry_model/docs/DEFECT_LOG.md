# Entry Model — Local Defect Log

| ID | Severity | Defect | Resolution | Regression evidence |
|---|---|---|---|---|
| EM-001 | High | Plan identity omitted execution-relevant values and could collide | Schema-2 canonical plan identity covers prices, expiry, flags, config and setup fingerprints | plan identity and mutation tests |
| EM-002 | High | Conflicting revisions could emit a plan depending on input order | Inputs are grouped by `setup_id`; any multi-fingerprint group fails closed before planning | order-permutation tests |
| EM-003 | High | Reused source `feature_id` with changed content was treated as duplicate | Source fingerprint is persisted and compared; changed content becomes `source_feature_conflict` | source conflict tests |
| EM-004 | High | Restore could accept state produced under different semantics | State schema 2 requires exact config fingerprint | incompatible-config tests |
| EM-005 | High | Incomplete validation could partially mutate live state | Restore parses all fields into temporary structures and swaps atomically | atomic-restore tests |
| EM-006 | Medium | Oversized state was silently trimmable | Restore rejects state beyond configured bound | oversized-state tests |
| EM-007 | Medium | Nested metadata could retain mutable containers | Recursive JSON-safe normalization freezes DTO state and emits plain JSON values | immutability tests |
| EM-008 | Medium | Subsecond staleness could be lost by integer truncation | Age comparison uses exact Decimal seconds | subsecond stale tests |
| EM-009 | Medium | Feature publication could precede setup confirmation | Explicit publication/confirmation causality validation | causality tests |
| EM-010 | Medium | Stream changes and out-of-order replay were not checkpoint-bound | Symbol, timeframe, cursor and bar signature are versioned state | stream/restart tests |
| EM-011 | Medium | Anonymous malformed inputs could share rejection IDs | Deterministic source fingerprint participates in rejection identity | malformed duplicate tests |
| EM-012 | Medium | DTO/state mappings were not uniformly plain JSON data | Public serialization emits dict/list/string/number/bool/null only | JSON round-trip tests |
| EM-013 | Medium | Bool values could pass integer configuration fields | Explicit bool rejection for integer fields | config type tests |
| EM-014 | Low | Legacy parser diagnostics were ambiguous | Exact legacy schema/order validation and messages | legacy compatibility tests |
| EM-015 | Low | Hot path repeated canonical work | Source/setup fingerprints are reused and runtime mapping checks use `collections.abc` | profile and benchmark evidence |

No open critical or high-severity module defect remains in the local release candidate.
