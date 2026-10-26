# Session Range Architecture

## Position in the system

Session Range is an external analytical producer loaded through `modular_strategy.modules`. Core delivers completed `Candle` events in `MarketView`; the module publishes namespaced `Feature` objects in `ModuleDecision`. Dependency planning and Feature Store lifecycle remain owned by Core.

```text
completed Candle / MarketView
        |
        v
SessionRangeModule
  - resolve local session bounds (IANA ZoneInfo)
  - enforce exact start/continuity/containment/end
  - accumulate Decimal OHLCV range incrementally
  - publish active/completed features
  - snapshot bounded deterministic state
        |
        v
ModuleDecision -> Core Feature Store -> downstream feature consumers
```

## Components

- `config.py`: strict session specification parsing, timezone/DST boundary resolution, canonical mappings.
- `state.py`: accumulator, bar cursor, stored completed feature, fixed Decimal context, JSON mappings.
- `module.py`: manifest, event lifecycle, gap/duplicate/order handling, feature construction, checkpoint/restore, pruning and diagnostics.
- `__init__.py`: stable public exports.

## State partitioning

Stream identity is escaped `{symbol}|{timeframe}`. Scope adds session name. Active token adds anchor date. Separate maps track active accumulators, stream cursors, terminal tokens, and last completed feature. Collections are bounded and pruned no more than once per UTC day.

## Concurrency

All processing, snapshot and restore paths share one re-entrant lock. Instances share no mutable global state. Decimal context is private and immutable by convention.

## Failure model

Critical uncertainty fails closed. Default mode returns stable skip reasons and diagnostics; strict mode raises controlled `ValueError` for invalid bar-contract paths. Restore is all-or-nothing.

## Core boundary

No Core source change, private import, concrete adapter, or strategy-specific branch is required.
