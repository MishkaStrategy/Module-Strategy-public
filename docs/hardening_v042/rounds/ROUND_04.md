# Round 04 — feature-time and completed-bar invariants

- Goal: make every feature lifetime operation use the same aware-UTC boundary and prove future features are rejected.
- Baseline: `Feature.is_active()` could accept a naive timestamp when `valid_until` was absent; expiry relied on incidental datetime comparison; equivalent timezone offsets missed snapshot cache reuse.
- Change: feature activity, expiry and snapshot queries normalize through `utc_datetime` before filtering or cache lookup.
- Tests: naive timestamp rejection, equivalent-offset cache identity, and engine rejection of a feature created one microsecond after the completed bar close.
- No-look-ahead: feature publication remains bounded by `bar.close_time`; no future fact becomes visible.
- Compatibility: aware timestamps in any timezone remain accepted and normalize to UTC.
