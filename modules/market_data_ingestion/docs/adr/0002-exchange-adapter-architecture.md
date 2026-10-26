# ADR-0002: Capability-first exchange adapters

**Status:** Accepted for local implementation checkpoint

Adapters publish only implemented and contract-tested capabilities. An adapter maturity
value cannot be `production` while required methods, source families or acceptance tests
are absent. Instrument resolution is exact and market-scoped; fuzzy substitution and
Spot/Futures fallback are prohibited.

The fake adapter proves that orchestration, storage and validation are not hard-coded to
Binance. The Binance adapter advertises only contract-tested source families: Spot klines
and metadata; USD-M perpetual klines, funding events, mark/index/premium price klines,
open-interest history and metadata. Its maturity remains `tested`, not `production`, until
live official-network acceptance, Python 3.12, static-analysis and Parquet gates pass.
