from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_core import (
    Candle,
    InMemoryEventStore,
    InMemoryStateRepository,
    Journal,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
    Trade,
)

from modular_trading_runner.core_bridge import (
    ConservativePaperBarReconciliation,
    CoreJournalFactualSource,
    CoreVenueEvidence,
)
from modular_trading_runner.cycle import PaperBarRecoveryDecision
from modular_trading_runner.market_data import MarketEventEnvelope
from modular_trading_runner.routing import FactualEventKind

NOW = datetime(2026, 7, 26, 12, 5, tzinfo=UTC)


def market_event() -> MarketEventEnvelope:
    candle = Candle(
        timestamp=NOW - timedelta(minutes=5),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketEventEnvelope(
        event_id="market-1",
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=1,
        available_at=candle.close_time,
        received_at=candle.close_time,
        candle=candle,
    )


def order_snapshot() -> OrderSnapshot:
    return OrderSnapshot(
        id="order-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=NOW,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id="client-1",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
        fees_paid=0.05,
        time_in_force=TimeInForce.GTC,
    )


def trade() -> Trade:
    return Trade(
        side=PositionSide.LONG,
        quantity=1.0,
        entry_time=NOW - timedelta(minutes=5),
        exit_time=NOW,
        entry_price=100.0,
        exit_price=105.0,
        gross_pnl=5.0,
        fees=0.1,
        net_pnl=4.9,
        exit_reason="take_profit",
        position_id="position-1",
    )


class Pending:
    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]:
        return (order_snapshot(),)


class Positions:
    def position_snapshot(self, mark_price: float):
        del mark_price
        return None


def test_core_venue_evidence_uses_public_snapshots_and_event_cursor() -> None:
    store = InMemoryEventStore()
    journal = Journal(run_id="core-run", event_store=store)
    journal.record_order(
        OrderEvent(timestamp=NOW, event="filled", order=order_snapshot()),
        symbol="BTCUSDT",
    )
    evidence = CoreVenueEvidence(
        event_store=store,
        pending_orders=Pending(),
        positions=Positions(),
    ).snapshot_evidence(market_event())
    assert evidence["core_event_sequence"] == 1
    assert evidence["symbol"] == "BTCUSDT"
    assert len(evidence["pending_orders"]) == 1


def test_core_journal_factual_source_decodes_and_persists_cursor() -> None:
    store = InMemoryEventStore()
    journal = Journal(run_id="core-run", event_store=store)
    journal.record_order(
        OrderEvent(timestamp=NOW, event="filled", order=order_snapshot()),
        symbol="BTCUSDT",
    )
    journal.record_trade(trade(), symbol="BTCUSDT")
    repository = InMemoryStateRepository()
    source = CoreJournalFactualSource(
        run_id="runner-1",
        core_run_id="core-run",
        portfolio_id="portfolio-1",
        instruments=("BTCUSDT",),
        config_hash="a" * 64,
        event_store=store,
        state_repository=repository,
    )
    events = tuple(source.events_for(market_event()))
    assert tuple(item.kind for item in events) == (
        FactualEventKind.ORDER,
        FactualEventKind.TRADE,
    )
    source.commit(events)
    assert source.cursor == 2

    recovered = CoreJournalFactualSource(
        run_id="runner-1",
        core_run_id="core-run",
        portfolio_id="portfolio-1",
        instruments=("BTCUSDT",),
        config_hash="a" * 64,
        event_store=store,
        state_repository=repository,
    )
    assert tuple(recovered.events_for(market_event())) == ()
    recovered.commit(())
    assert recovered.cursor == 2


def test_uncommitted_factual_batch_is_stable_for_retry() -> None:
    store = InMemoryEventStore()
    journal = Journal(run_id="core-run", event_store=store)
    journal.record_trade(trade(), symbol="BTCUSDT")
    source = CoreJournalFactualSource(
        run_id="runner-1",
        core_run_id="core-run",
        portfolio_id="portfolio-1",
        instruments=("BTCUSDT",),
        config_hash="a" * 64,
        event_store=store,
        state_repository=InMemoryStateRepository(),
    )
    first = tuple(source.events_for(market_event()))
    second = tuple(source.events_for(market_event()))
    assert second == first
    assert source.cursor == 0
    source.commit(second)
    assert source.cursor == 1


def test_conservative_reconciliation_never_guesses_changed_state() -> None:
    policy = ConservativePaperBarReconciliation()
    event = market_event()
    assert (
        policy.resolve(event=event, before={"v": 1}, current={"v": 1})
        is PaperBarRecoveryDecision.RETRY
    )
    assert (
        policy.resolve(event=event, before={"v": 1}, current={"v": 2})
        is PaperBarRecoveryDecision.BLOCKED
    )
