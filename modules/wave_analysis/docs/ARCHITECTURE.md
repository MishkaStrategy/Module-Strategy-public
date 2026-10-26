# Architecture

## Boundary

`wave_analysis` is an independent Module API feature producer. Core owns event ordering, Feature Store mutation, timeframe aggregation, checkpoint orchestration, and execution. The module owns only wave-analysis state and calculations.

## Pipeline

```text
completed MarketView
  -> base/target identity validation
  -> completed-timeframe gate
  -> gap segmentation and recovery gate
  -> pivot detector
  -> provisional revision / confirmed pivot
  -> active-segment wave construction
  -> classification and structure
  -> retracement / extension
  -> optional bounded hypotheses
  -> bounded Feature and FeatureDelete decision
```

## State ownership

One module instance is intentionally bound to one symbol. Each configured timeframe has isolated bars, ATR state, pivots, waves, hypotheses, segment counters, recovery counters, and persistent Feature IDs. Multiple instruments use independent instances.

Public methods that read or mutate runtime state are serialized by an `RLock`. This guarantees atomic duplicate delivery and checkpoint reads for one instance; it does not reorder distinct out-of-order market events.

## Gap semantics

A gap starts a new segment. Historical confirmed objects may remain in bounded audit history, but calculations use only active-segment pivots and waves. The detector warms during mandatory recovery, while externally visible analysis remains blocked. Persistent Feature snapshots are invalidated/deleted at the gap boundary.

## Dependencies

Runtime dependency: Modular Strategy Core `>=0.4,<0.5`. No numerical or data-science framework is required. Decimal is used for financial geometry and JSON-compatible primitive mappings for state.
