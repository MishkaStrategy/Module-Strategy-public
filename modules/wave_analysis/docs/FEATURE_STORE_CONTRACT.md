# Feature Store Contract

Schema version: `1`.

Produced keys:

- `wave_analysis.confirmed_pivots`
- `wave_analysis.provisional_pivot`
- `wave_analysis.wave_legs`
- `wave_analysis.current_wave`
- `wave_analysis.structure`
- `wave_analysis.impulse_score`
- `wave_analysis.correction_score`
- `wave_analysis.retracement`
- `wave_analysis.extension`
- `wave_analysis.invalidation`
- `wave_analysis.hypotheses`
- `wave_analysis.primary_hypothesis`
- `wave_analysis.mtf_context`
- `wave_analysis.confidence`

Confirmed-pivot and wave-history snapshots are bounded by `feature_snapshot_limit` and use stable exact Feature IDs. A new persistent snapshot supersedes the previous snapshot through `FeatureDelete`, preventing unbounded Core Feature Store growth.

Persistent snapshots are marked `invalidate_on_gap=True`. On an explicit gap the module also emits exact deletes for adapters that apply module decisions without Core's pre-module gap invalidation.

Every feature includes symbol/timeframe identity, point-in-time `created_at`, schema version, deterministic metadata, and active-segment diagnostics where relevant. Provisional output has separate IDs/status/revisions and never mutates a previously confirmed pivot.
