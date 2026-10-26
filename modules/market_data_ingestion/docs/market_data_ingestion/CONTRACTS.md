# Contract specification — package 0.4.0

The package defines strict serializable dataclasses for:

- `MarketDataRequest`, `ExchangeReference`, `MarketReference`;
- `InstrumentReference`, `InstrumentMetadata`;
- `ExchangeCapabilities`, `DataTypeCapability`;
- `DownloadPlan`, `DownloadChunk`, `DownloadCheckpoint`;
- `RawArtifactDescriptor`, `NormalizedPartitionDescriptor`;
- `GapDescriptor`, `DuplicateDescriptor`, `ValidationIssue` and
  `DataValidationReport`;
- per-stream `DataTypeValidationReport`;
- `RepairPlan`, `RepairResult`;
- `DatasetManifest`, `DatasetDescriptor`, `DatasetRegistration`;
- `IngestionResumeSpec`, `IngestionRunManifest`, `IngestionRunResult`;
- `StructuredIngestionError`.

Normalized records are separate by semantics:

- `KlineRecord` for traded OHLCV;
- `PriceKlineRecord` for mark, index and premium OHLC without traded volume;
- `FundingRateRecord` for point events;
- `OpenInterestRecord` for interval observations;
- `InstrumentMetadataRecord` and `ExchangeMetadataRecord` for normalized snapshots.

Canonical hashing supports dataclasses, enums, UTC datetimes, paths and JSON scalar
values only. Non-string mapping keys, arbitrary Python objects, NaN and infinity fail
closed. Normalized CSV uses an exact ordered schema for every data family. SQLite staging
uses strict JSON plus an independent identity hash; pickle and dynamic imports are not
used.

## Binance USD-M funding timestamp normalization

The Binance USD-M funding adapter preserves the provider REST payload byte-for-byte as
immutable raw lineage. Normalized funding settlement time is distinct from that raw
provider timestamp.

For bar-based consumers, the canonical funding settlement boundary is an exact UTC-hour
boundary. The adapter may normalize only a positive provider timestamp jitter of
`+1..47 ms` when the raw event is unambiguously mapped to its scheduled provider funding boundary and the canonical result is that exact UTC-hour boundary. This
rule is provider-specific and is not nearest-candle rounding. Exact UTC-hour timestamps
remain unchanged. Negative offsets, offsets larger than 47 ms, non-hour timestamps and
other ambiguous values fail closed.

Each REST acquisition/restore publishes deterministic normalization evidence containing
the policy ID (`binance-usdtm-hour-boundary-jitter-v2`), exact-boundary count, normalized-jitter count, maximum observed positive
jitter and the fact that the raw `fundingTime` remains preserved in the immutable raw
payload. Adapter identity changes when this normalization contract changes, so a
reprocessed corpus registers as a new immutable dataset version/hash rather than
reinterpreting an existing dataset in place.

## Identity layers

- Request hash: canonical user request content, excluding the request ID.
- Execution hash: request plus adapter, storage, normalization, quality and resource
  settings.
- Raw artifact ID: checksum plus exchange/market/instrument/data type/range/source and
  adapter version.
- Partition ID: normalized file checksum.
- Dataset hash: content/lineage hash independent of registry version and dataset ID.
- Registry identity: dataset ID plus immutable integer version.

## Compatibility policy

- Additive optional fields require a minor schema revision and explicit default.
- Meaning changes or required-field changes require a new major contract version.
- Resume specifications and checkpoints reject unsupported versions.
- Frozen Core contracts are not modified by this package.
- Consumers must pin dataset ID, version and dataset hash.
- Adapter-version or normalization-version incompatibility blocks resume/repair rather
  than silently reinterpreting prior raw data.
