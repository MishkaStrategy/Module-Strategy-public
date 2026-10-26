# Registered Funding Adapter V1

This slice is the first Backtest-side consumer of the accepted Market Data funding owner contract from Issue #417.

It does not move Market Data ownership into Backtest and does not close GAP-008 / Issue #376.

## Source identity

Backtest adapts one exact registered Market Data funding selection into `RegisteredFundingSource`.
The public identity is bound to:

- `dataset_id`;
- `dataset_version`;
- immutable `dataset_hash`;
- symbol;
- acquisition coverage with exact `[start,end)` semantics;
- normalized `event_count`;
- immutable raw `source_record_count`;
- canonically ordered raw lineage with logical artifact IDs, source kinds, SHA-256 values, counts and intervals.

Storage URIs, raw source URLs and credentials are not part of Backtest evidence.

The adapter consumes the Market Data owner reader and does not register, download, migrate or mutate datasets.

## Settlement semantics

Each accepted funding event has a stable zero-based ordinal in the immutable source, UTC timestamp, rate and owner-provided mark price.

A funding event without owner mark-price evidence is rejected. V1 deliberately does **not** substitute candle close, last trade or an inferred price.

For positive position quantity `q`, funding mark price `p`, funding rate `r` and direction `d` (`+1` long, `-1` short):

`cashflow = -(q * p * r * d)`

Therefore a positive funding rate debits longs and credits shorts.

`FundingSettlementLedger` is bound to the source identity and advances a compact `next_ordinal` cursor. A replay of an already-settled immutable event is a no-op; a missing ordinal, altered replay or snapshot from another source fails closed. The snapshot records source identity, next ordinal and cumulative funding cashflow so restart and uninterrupted settlement can be compared exactly.

## Coverage semantics

Owner acquisition coverage remains `[start,end)` throughout Backtest. An event exactly at `coverage_end_exclusive` is invalid. V1 does not inherit the stale inclusive-end behavior from superseded PR #385.

No fixed eight-hour funding cadence is invented. Completeness comes from Market Data immutable raw lineage counts and acquisition intervals.

## V1 non-goals

This slice does not yet:

- wire funding cashflows into the canonical `BacktestEngine`/runner equity journal;
- provide registered mark-price or index-price history beyond mark price carried by funding events;
- substitute a valuation source when a funding event has no mark price;
- change execution/mark/last valuation semantics;
- publish funding assumptions into Results Aggregator or Research Runner;
- run canonical A/B or full-corpus futures-realism acceptance;
- close Issue #376.

Those remain subsequent GAP-008 slices. Main-runner wiring must preserve same-timestamp funding-before-bar ordering, exactly-once restart behavior and immutable owner provenance before it can become an accepted futures-realism path.
