# FVG 0.2.1 Integration Contract

## Ownership

FVG owns detection, active-zone lifecycle, FVG diagnostics and FVG checkpoint state. Core owns visibility, orchestration, registry, dependency planning, FeatureStore and persistence transport. Downstream modules own trading decisions and execution.

## Inputs

Exact public DTO: Core `MarketView` with completed Core-compatible candles. The module reads `base_bars`, `current`, configured `timeframe_bars`, `index` and `data_gap`.

Constraints:

- timezone-aware open/close times;
- finite positive OHLC and non-negative volume;
- valid OHLC geometry;
- matching symbol/timeframe;
- `bar.close_time <= view.current.close_time`;
- ordered A/B/C; consecutive by default;
- one module instance per symbol.

## Outputs

Core `ModuleDecision` containing immutable `Feature`, exact `FeatureDelete`, stable reason strings and diagnostics.

Feature keys and schema 1:

- `fvg.active_zone`;
- `fvg.zone_created`;
- `fvg.zone_invalidated`.

Strict formulas:

- bullish: `C.low > A.high`, zone `[A.high, C.low]`;
- bearish: `C.high < A.low`, zone `[C.high, A.low]`.

## State schema 3

JSON-compatible state stores module/config versions, canonical config fingerprint, bound symbol, per-timeframe cursor/current fingerprint/two history fingerprints/barrier, and sorted active zones. Restore is atomic. Schemas 1 and 2 migrate with a fail-closed causal barrier.

## Planner order

FVG has no required feature. A consumer must declare `required_features=("fvg.active_zone",)`, causing the planner to place FVG first. A newly created zone is available to downstream processing on the same completed orchestration bar.

## Compatibility

Core 0.4.x required. Feature schema and feature keys are unchanged from 0.1.0/0.2.0. Valid prior UTC zone IDs remain byte-compatible. No Core change request exists.
