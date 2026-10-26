# Architecture — Protection & Target 1.1.1

```text
Confirmed Setup
    ↓
Entry Model → entry_model.entry_plan
    ↓
ProtectionTargetModule
    ├── strict EntryPlan contract
    ├── invalidation resolver
    ├── outward stop buffer
    ├── fixed-R / structural target resolver
    ├── geometry and risk-distance gates
    ├── deterministic plan/rejection feature
    └── bounded signed checkpoint
    ↓
Risk & Position Sizing → Order Intent → Execution
```

The module communicates exclusively through public Core types and FeatureStore snapshots. It has no
exchange I/O, market-data loading, direct clock access, portfolio mutation or hidden instrument logic.

One instance owns one symbol/timeframe stream. Module-level locking makes duplicate processing and
checkpoint operations atomic for that instance.
