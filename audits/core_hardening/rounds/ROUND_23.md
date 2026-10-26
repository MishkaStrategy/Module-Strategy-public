# Round 23 — bounded module-visible history

Added optional `DataConfig.max_base_history`. Default behavior remains unchanged. When enabled, the engine uses a bounded deque for the base history exposed to modules; HTF history continues to use `TimeframeSpec.max_history`.

A preflight rejects a capacity smaller than any module's declared base-timeframe warm-up requirement.
