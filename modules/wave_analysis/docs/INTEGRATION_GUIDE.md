# Integration Guide

1. Apply the Wave Analysis commit(s) to a checkout based on `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3` or review conflicts against a newer integration head.
2. Install the root Core package and `modules/wave_analysis` package.
3. Discover the `wave_analysis` entry point through `ModuleRegistry`.
4. Load `configs/wave_analysis.example.yaml` or provide per-instrument parameters.
5. Run the full repository suite, module CI, and a strategy-level backtest.

No Core source change is required. The module declares required timeframes, 14 produced features, one optional Market Regime feature, warm-up requirements, parameter schema, and state schema through its manifest.

Use one instance per symbol. A single instance rejects cross-symbol state mixing. Paper/live adapters must preserve completed-bar ordering, pass gap information, apply both Features and FeatureDeletes, and persist module checkpoints atomically.
