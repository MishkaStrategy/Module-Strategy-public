# Historical Market Data Ingestion architecture

## Repository audit result

The canonical integration surface is the staged v0.6 integration branch, while frozen
Core 0.4.2 remains authoritative for generic contracts. Existing Core `Candle` is a
consumer-facing completed OHLCV bar, not a raw acquisition schema. Existing Backtest
Engine data loading is retained as a consumer boundary. Results Aggregator already owns
an experiment/results registry and a narrower dataset-record schema; this component
therefore owns a detailed ingestion manifest and exposes an adapter to that existing
record rather than creating a second experiment registry.

The existing `scripts/pretest_support/dataset.py` validates a previously downloaded
`download_summary.csv` plus a BTCUSDT 5m file. It is safe to reuse as downstream
acceptance evidence but is not a resumable exchange downloader.

## Component diagram

```mermaid
flowchart LR
    SF[Strategy Factory] --> R[MarketDataRequest]
    CLI[market-data CLI] --> R
    R --> O[Ingestion Orchestrator]
    O --> A[Exchange Adapter]
    A --> OA[Official Archive]
    A --> REST[Official REST]
    O --> RAW[Immutable Raw]
    RAW --> N[Normalizer]
    N --> V[Validation]
    V --> REP[Repair boundary]
    V --> D[Derived Timeframes]
    REP --> V
    D --> H[Manifest + Merkle Hash]
    H --> REG[Dataset Registry]
    REG --> DD[DatasetDescriptor]
    DD --> BT[Backtest Engine]
    DD --> RR[Research Runner]
    DD --> RA[Results Aggregator adapter]
```

## Download sequence

```mermaid
sequenceDiagram
    participant U as User/Component
    participant O as Orchestrator
    participant A as Adapter
    participant S as Storage
    participant V as Validator
    participant R as Registry
    U->>O: MarketDataRequest
    O->>A: capabilities + exact instrument resolution
    O->>A: deterministic DownloadPlan
    loop each chunk
        O->>S: lookup confirmed raw
        alt raw exists
            S-->>O: immutable payload + descriptor
            O->>A: restore typed records from raw
        else missing
            O->>A: acquire typed records
            O->>S: atomic raw publication
        end
        O->>S: checkpoint + SQLite record spool
    end
    O->>V: timestamp/identity ordered stream
    V-->>O: report + exact deduplicated stream
    O->>S: streaming normalized/derived partitions
    O->>S: immutable manifest
    O->>R: atomic dataset + request registration
    R-->>U: DatasetDescriptor
```

## State machine

```mermaid
stateDiagram-v2
    CREATED --> VALIDATING_REQUEST
    VALIDATING_REQUEST --> DISCOVERING_CAPABILITIES
    DISCOVERING_CAPABILITIES --> RESOLVING_INSTRUMENT
    RESOLVING_INSTRUMENT --> PLANNING
    PLANNING --> DOWNLOADING
    DOWNLOADING --> VERIFYING_RAW
    DOWNLOADING --> PAUSED
    VERIFYING_RAW --> NORMALIZING
    NORMALIZING --> VALIDATING_DATA
    VALIDATING_DATA --> REPAIRING
    VALIDATING_DATA --> BUILDING_DERIVED
    VALIDATING_DATA --> HASHING
    VALIDATING_DATA --> DATASET_REJECTED
    REPAIRING --> VALIDATING_DATA
    BUILDING_DERIVED --> HASHING
    HASHING --> REGISTERING
    REGISTERING --> COMPLETED
    REGISTERING --> COMPLETED_WITH_WARNINGS
    PAUSED --> DOWNLOADING
```

## Data lifecycle

```mermaid
flowchart TB
    Source --> Raw[Raw: exact bytes, immutable, checksum]
    Raw --> Normalized[Normalized: UTC, strict schema, exact dedup]
    Normalized --> Derived[Derived: complete UTC groups only]
    Raw --> Manifest
    Normalized --> Manifest
    Derived --> Manifest
    Manifest --> Descriptor
```

## Ownership rules

- Raw responses remain evidence and are never silently edited.
- Exact duplicates may be removed from normalized data only with retained evidence.
- Conflicting duplicates reject the dataset.
- Missing OHLCV is never interpolated.
- Registered versions are immutable.
- Consumers receive a descriptor and never cause a hidden update.

## Bounded-memory execution

Source adapters may materialize at most one configured chunk. The orchestrator writes
typed records to a strict SQLite spool, validates an ordered cursor in one pass, stages
accepted rows in bounded batches and streams monthly CSV publication. Derived OHLCV is
aggregated from the accepted ordered stream. Parquet remains a separate release gate
because `pyarrow` is not installed in the current environment.

## Version-preserving operations

`update`, `backfill` and `repair` publish a new immutable dataset version. Repair can:

- redownload a damaged normalized range;
- rebuild dependent OHLCV timeframes locally;
- restore metadata snapshots from raw exchange-info evidence;
- fill exact unresolved fixed-interval gaps for traded, mark, index, premium and
  open-interest streams.

Funding-rate events are not assigned a synthetic fixed cadence.
