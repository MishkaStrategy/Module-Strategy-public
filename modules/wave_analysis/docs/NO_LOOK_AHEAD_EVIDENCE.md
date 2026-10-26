# No-Look-Ahead and Non-Repainting Evidence

RC2 verifies point-in-time behavior through deterministic prefix/property tests.

- A fractal pivot is unavailable until its configured right-side bars have closed.
- `pivot_time` records the extremum; `confirmed_at` records the first legal availability time.
- Target timeframe candles whose close time exceeds the base decision time are rejected as incomplete.
- Confirmed pivot history remains byte/digest equivalent when later future bars are appended.
- Provisional revisions are separately identified and can be disabled.
- Gaps create independent segments; structure, ratios, hypotheses, and detector direction do not read pre-gap pivots.
- Recovery candles warm the detector but publish no active analysis until recovery completes.
- Same-bar hard invalidation is applied before an Elliott-like hypothesis can be published active.
- Continuous and checkpoint/restart processing produce the same digest.

Dedicated CI runs property, segment-isolation, concurrency, and feature-lifecycle suites with Hypothesis installed explicitly.
