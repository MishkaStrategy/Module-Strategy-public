# Mark Valuation / Reference Price Policy V1

This slice implements the next bounded GAP-008 dependency after the accepted Market Data registered mark/index owner reader.

The purpose is to separate **traded-price execution** from **mark-price valuation/reference semantics** without changing the historical baseline by default.

## Policies

`run_backtest()` exposes an explicit programmatic valuation policy:

- `last` — default. Preserves the existing traded-price baseline and accepts no mark source.
- `mark` — requires one immutable `RegisteredMarkPriceSource` adapted from the accepted Market Data `MARK_PRICE` owner contract.

The Core run manifest/run ID remains the existing strategy/config/candle identity. A mark-valued run receives a separate application identity derived from the existing funding-aware application identity plus the immutable mark-source identity. Historical baseline or funding-only result directories therefore cannot collide with mark-valued results.

## Price timing semantics

V1 intentionally uses two mark references per base candle to avoid look-ahead:

- **mark open** — execution-time equity/risk reference while the traded candle is processed;
- **mark close** — post-bar unrealized/equity/position snapshot and new decision/order/update reference.

This distinction matters because Core processes eligible orders before strategy `on_bar()` for the current candle.

## What remains traded OHLC

Mark valuation does **not** replace the execution market:

- market fills remain based on traded candle open plus configured slippage;
- limit fills remain based on traded high/low/open and configured fill policy;
- stop/trigger decisions remain based on traded high/low/open;
- pending position closes remain executed from traded candle open;
- forced end-of-data close remains executed at traded last close.

The mark source only changes valuation/reference semantics. This prevents mark candles from silently becoming an execution simulator.

## Mark-aware portfolio and broker

The application layer composes Core without changing Core APIs:

- `MarkAwarePortfolio` keeps the same Core cash/position ledger but projects unrealized PnL, equity and position snapshots through the currently bound mark reference.
- `MarkAwareBrokerSimulator` binds mark open before traded `process_bar()` and mark close after execution.
- order submission and position-update validation use mark close for their reference checks.
- execution-time fill risk resizing sees mark open through the mark-aware portfolio while actual fill prices remain traded.
- `FundingMarkAwareBrokerSimulator` composes the same behavior with the already accepted funding runtime; funding ownership and settlement order remain unchanged.

## Immutable mark source

`RegisteredMarkPriceSource` is a compact application adapter over the accepted Market Data owner reader. It binds:

- dataset ID/version/hash;
- symbol/exchange/market/timeframe;
- exact selected `[start,end)` run window;
- owner normalized/source row counts;
- immutable source lineage checksums/counts/coverage;
- exactly aligned mark open/close arrays.

Every candle lookup rechecks symbol, timeframe and exact timestamp alignment. The runner verifies first/last/count compatibility before simulation. Missing, shifted, short, long or misaligned mark evidence fails closed before authoritative publication.

## Publication

For `last`, no valuation-specific files or fields are added so the established baseline publication contract remains unchanged.

For `mark`:

- the output directory uses the mark-bound application run ID;
- `application-run.json` records the valuation model, policy, source identity, sanitized owner evidence and explicit execution/reference price semantics;
- `valuation-runtime.json` records the same deterministic application/source identity;
- `artifact-manifest.json` keeps the Core `run_id` and uses the existing optional `application_run_id` extension.

No raw storage path or source URL is published.

## Recovery

The mark source is immutable and index-addressed by candle timestamp; it has no mutable cursor. The broker/portfolio state is serialized by the existing `runtime_snapshot_v1` path. Acceptance requires restarted and uninterrupted result digests, fills and equity to match exactly under mark valuation.

## Acceptance invariants

V1 is accepted only if all of the following hold:

1. `last` remains the default and preserves the established baseline path.
2. mark open/close equal to traded open/close reproduces the exact Core result digest.
3. changing mark close changes valuation deterministically but does not change traded fill prices.
4. execution-time risk reference is mark open, not future mark close.
5. mark source mismatch fails before simulation/publication.
6. funding + mark compose without changing funding ownership or double-applying settlements.
7. direct runtime snapshot restart equals uninterrupted execution.
8. Python 3.12/3.13, full Backtest/Core regressions and policy gates remain green.

## Non-goals

This slice does not:

- use index price as an execution or valuation substitute;
- redesign liquidation/margin semantics;
- change Market Data ownership;
- add live/private exchange APIs;
- add Control UI or Paper Runtime controls;
- propagate futures assumptions into Results Aggregator / Research Runner;
- perform the final full-corpus baseline-vs-realism A/B acceptance.

Those remain subsequent Issue #376 dependencies.