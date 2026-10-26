# Funding Runtime V2

This slice integrates the accepted registered funding owner contract into Backtest execution state without changing the Core engine API.

It is a bounded substrate for GAP-008 / Issue #376. It does **not** yet make funding configurable from the canonical YAML/CLI runner and it does not close the full futures-realism gap.

## Ownership and execution boundary

- Market Data remains the owner of registered funding datasets and immutable lineage.
- Backtest consumes one `RegisteredFundingSource` from the accepted owner adapter.
- `FundingAwareBrokerSimulator` subclasses the existing Core `BrokerSimulator` and is intended to be supplied through the existing `execution_factory` hook.
- No second engine, portfolio, journal, checkpoint store or execution authority is introduced.
- Core APIs and contracts are unchanged.

## Settlement ordering

For a funding event whose timestamp equals a processed candle open, settlement happens before `BrokerSimulator.process_bar`.

Therefore an order that first becomes fill-eligible on that candle does not retroactively create funding exposure at the same boundary. The funding event sees the position state that existed immediately before bar execution.

A funding event that falls between processed candle-open boundaries fails closed rather than being shifted to another timestamp.

## Flat periods and wider owner coverage

The owner dataset may cover a superset of the requested candle interval.

Before the first processed bar only, older funding events are consumed as flat without journal emission so the event stream does not contain timestamps older than `run.started`. After runtime start, missing a funding boundary is fatal.

A funding event on an active runtime boundary is always consumed exactly once, including when the account is flat. Flat settlements have zero quantity, notional and cashflow and are journaled as `portfolio.funding`.

## Decimal accounting and Core cash projection

Funding notional and cashflow are computed with `Decimal` using:

`effective_quantity = position_quantity * contract_size`

`cashflow = -(effective_quantity * mark_price * funding_rate * direction)`

where direction is `+1` for long and `-1` for short.

Core currently owns portfolio cash as a finite `float`. V2 therefore applies one explicit deterministic projection at the accounting boundary:

1. convert current finite float cash through `Decimal(str(current_cash))`;
2. add the exact Decimal funding cashflow;
3. convert the result back to finite float;
4. fail closed on a non-finite projection.

The exact Decimal cashflow remains in the funding journal payload and funding ledger state.

## Restart semantics

The funding source and `FundingSettlementLedger` are fields of the broker object itself.

The existing scalable direct-runtime snapshot already serializes the authoritative broker inside `ScalableRuntimeState`. Therefore the accepted design has one natural place for source identity, next ordinal and cumulative funding cashflow and does not require a sidecar state store.

Focused V2 tests verify deterministic ledger snapshot/restore together with portfolio cash continuation and exactly-once settlement. They intentionally do not use a plain `pickle.dumps(BrokerSimulator)`: the normal in-memory Core journal contains an `RLock`, while the production direct-runtime snapshot uses `StreamingEventStore` plus the Backtest custom runtime pickler/reducer. Runner-level continuous-vs-restarted acceptance through that real snapshot path remains the next wiring slice.

## Evidence

Each active runtime settlement emits one deterministic `portfolio.funding` event containing:

- model version;
- owner dataset ID/version/hash;
- owner source identity SHA-256;
- immutable event key and ordinal;
- timestamp, rate and owner mark price-derived notional;
- position ID/side/quantity when present;
- instrument contract size;
- exact Decimal cashflow encoded as text.

`funding_state()` exposes only sanitized owner identity and ledger state. Storage paths, source URLs and credentials are not included.

## Fail-closed conditions

V2 rejects at least:

- source/instrument symbol mismatch;
- non-positive or non-finite contract size;
- processed candle outside owner funding coverage;
- missed or non-aligned funding boundary after runtime start;
- event sequence gaps or events differing from immutable owner source;
- non-positive active position quantity;
- non-finite portfolio cash projection.

## Non-goals / next slice

The next GAP-008 Backtest slice must wire this broker through the canonical runner and publication contract. It must also:

- bind a separate application run identity to the funding source identity so baseline and funding-enabled publications cannot collide;
- publish `funding_model`, source identity and deterministic settlement evidence;
- prove continuous == restarted through the actual registered streaming/direct runtime snapshot route;
- preserve same-timestamp funding-before-bar ordering in runner acceptance tests.

Registered mark/index price readers, mark-vs-last valuation scenarios, Results/Research assumption publication and full-corpus A/B realism acceptance remain later Issue #376 work.
