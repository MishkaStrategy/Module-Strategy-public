from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from modular_core import (
    AtomicJsonStateRepository,
    Candle,
    ClosePositionRequest,
    EngineConfig,
    InstrumentSpec,
    Journal,
    OrderRequest,
    OrderType,
    PositionSide,
    Side,
)
from modular_core.broker_state import (
    BrokerStateSnapshot,
    capture_broker_state,
    restore_broker_state,
    restore_broker_state_from_repository,
    save_broker_state,
)
from modular_core.execution import BrokerSimulator
from modular_core.serialization import to_jsonable

_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _config() -> EngineConfig:
    return EngineConfig(one_active_order=False)


def _instrument(symbol: str = "BTCUSDT") -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=0.1,
        quantity_step=0.001,
        min_quantity=0.001,
        min_notional=1.0,
        contract_size=1.0,
        price_precision=1,
        quantity_precision=3,
    )


def _broker(
    *,
    config: EngineConfig | None = None,
    instrument: InstrumentSpec | None = None,
    run_id: str = "broker-state-test",
) -> BrokerSimulator:
    return BrokerSimulator(
        config or _config(),
        Journal(run_id=run_id),
        instrument or _instrument(),
    )


def _bar(offset: int, *, open_price: float = 101.0) -> Candle:
    timestamp = _AT + timedelta(minutes=5 * offset)
    return Candle(
        timestamp=timestamp,
        open=open_price,
        high=open_price + 4.0,
        low=open_price - 2.0,
        close=open_price + 1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _nontrivial_broker() -> BrokerSimulator:
    broker = _broker()
    entry = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
            stop_loss=90.0,
            take_profit=130.0,
            client_order_id="entry-1",
        ),
        source_module="state-test",
        created_at=_AT,
        current_index=0,
        mark_price=100.0,
    )
    assert entry is not None
    assert entry.id == "ord-00000001"
    broker.process_bar(_bar(1), 1)
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.id == "pos-00000001"
    assert broker.journal.fills[-1].fill_id == "fill-0000000001"

    reduce_order = broker.submit(
        OrderRequest(
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=0.25,
            price=120.0,
            reduce_only=True,
            position_side=PositionSide.LONG,
            client_order_id="reduce-1",
        ),
        source_module="state-test",
        created_at=_AT + timedelta(minutes=6),
        current_index=1,
        mark_price=102.0,
    )
    assert reduce_order is not None
    assert reduce_order.id == "ord-00000002"
    assert broker.schedule_close(
        ClosePositionRequest(reason="recovery-close", fraction=0.5),
        timestamp=_AT + timedelta(minutes=7),
        current_index=1,
        source_module="state-test",
    )
    return broker


def _mutable_state(snapshot: BrokerStateSnapshot) -> dict[str, Any]:
    value = to_jsonable(snapshot.to_state())
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _assert_pristine(broker: BrokerSimulator) -> None:
    assert broker.portfolio.cash == broker.config.initial_cash
    assert broker.portfolio.position is None
    assert broker.orders == []
    assert broker.pending_orders == []
    assert broker.pending_close is None


def test_atomic_broker_state_reopen_preserves_identity_and_deterministic_continuation(
    tmp_path: Path,
) -> None:
    source = _nontrivial_broker()
    repository = AtomicJsonStateRepository(tmp_path / "broker-state")
    saved = save_broker_state(repository, "paper-broker", source)

    reopened_repository = AtomicJsonStateRepository(tmp_path / "broker-state")
    restored = _broker(run_id="broker-state-restored")
    loaded = restore_broker_state_from_repository(
        reopened_repository,
        "paper-broker",
        restored,
    )
    assert loaded.to_state() == saved.to_state()
    assert capture_broker_state(restored).to_state() == saved.to_state()
    assert restored.portfolio.position is not None
    assert restored.portfolio.position.id == "pos-00000001"
    assert [order.id for order in restored.pending_orders] == ["ord-00000002"]
    assert restored.pending_close is not None
    assert restored.pending_close.fraction == 0.5

    duplicate = restored.submit(
        OrderRequest(
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=0.1,
            price=125.0,
            reduce_only=True,
            position_side=PositionSide.LONG,
            client_order_id="entry-1",
        ),
        source_module="state-test",
        created_at=_AT + timedelta(minutes=8),
        current_index=1,
        mark_price=103.0,
    )
    assert duplicate is None
    assert restored.journal.skips[-1]["reason"] == "duplicate_client_order_id"

    trade = restored.close_position(
        timestamp=_AT + timedelta(minutes=10),
        price=110.0,
        reason="manual-recovery-close",
        index=2,
    )
    assert trade is not None
    assert restored.portfolio.position is None
    assert restored.pending_close is None
    assert restored.journal.fills[-1].fill_id == "fill-0000000002"

    next_order = restored.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=0.5,
            client_order_id="entry-2",
        ),
        source_module="state-test",
        created_at=_AT + timedelta(minutes=11),
        current_index=2,
        mark_price=110.0,
    )
    assert next_order is not None
    assert next_order.id == "ord-00000003"
    restored.process_bar(_bar(3, open_price=111.0), 3)
    assert restored.portfolio.position is not None
    assert restored.portfolio.position.id == "pos-00000002"
    assert restored.journal.fills[-1].fill_id == "fill-0000000003"


@pytest.mark.parametrize(
    ("config", "instrument", "match"),
    [
        (EngineConfig(one_active_order=True), _instrument(), "config identity"),
        (_config(), _instrument("ETHUSDT"), "instrument identity"),
    ],
)
def test_restore_rejects_wrong_owner_identity_before_mutation(
    config: EngineConfig,
    instrument: InstrumentSpec,
    match: str,
) -> None:
    snapshot = capture_broker_state(_nontrivial_broker())
    target = _broker(config=config, instrument=instrument, run_id="wrong-identity")
    with pytest.raises(ValueError, match=match):
        restore_broker_state(target, snapshot)
    _assert_pristine(target)


def test_broker_state_rejects_future_and_partial_schema() -> None:
    snapshot = capture_broker_state(_nontrivial_broker())

    future = _mutable_state(snapshot)
    future["schema_version"] = "2"
    with pytest.raises(ValueError, match="unsupported broker state schema"):
        BrokerStateSnapshot.from_state(future)

    partial = _mutable_state(snapshot)
    partial.pop("fill_sequence")
    with pytest.raises(ValueError, match="fields mismatch"):
        BrokerStateSnapshot.from_state(partial)


def test_restore_rejects_pending_membership_and_sequence_tamper_before_mutation() -> None:
    snapshot = capture_broker_state(_nontrivial_broker())

    missing_pending_state = _mutable_state(snapshot)
    missing_pending_state["pending_order_ids"] = []
    missing_pending = BrokerStateSnapshot.from_state(missing_pending_state)
    target = _broker(run_id="pending-tamper")
    with pytest.raises(ValueError, match="pending order membership"):
        restore_broker_state(target, missing_pending)
    _assert_pristine(target)

    regressed_sequence_state = _mutable_state(snapshot)
    regressed_sequence_state["order_sequence"] = 1
    regressed_sequence = BrokerStateSnapshot.from_state(regressed_sequence_state)
    target = _broker(run_id="sequence-tamper")
    with pytest.raises(ValueError, match="order sequence counter"):
        restore_broker_state(target, regressed_sequence)
    _assert_pristine(target)


def test_restore_rejects_client_order_and_fill_accounting_tamper() -> None:
    snapshot = capture_broker_state(_nontrivial_broker())

    client_state = _mutable_state(snapshot)
    client_state["used_client_order_ids"] = ["entry-1"]
    client_tamper = BrokerStateSnapshot.from_state(client_state)
    target = _broker(run_id="client-tamper")
    with pytest.raises(ValueError, match="duplicate fence"):
        restore_broker_state(target, client_tamper)
    _assert_pristine(target)

    fill_state = _mutable_state(snapshot)
    assert isinstance(fill_state["position"], dict)
    fill_state["position"]["quantity"] = 9.0
    fill_tamper = BrokerStateSnapshot.from_state(fill_state)
    target = _broker(run_id="fill-tamper")
    with pytest.raises(ValueError, match="quantity disagrees"):
        restore_broker_state(target, fill_tamper)
    _assert_pristine(target)


def test_restore_refuses_non_pristine_target_without_clobbering_existing_state() -> None:
    snapshot = capture_broker_state(_nontrivial_broker())
    target = _broker(run_id="non-pristine")
    existing = target.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=0.25,
            price=90.0,
            client_order_id="existing",
        ),
        source_module="state-test",
        created_at=_AT,
        current_index=0,
        mark_price=100.0,
    )
    assert existing is not None
    before = capture_broker_state(target).to_state()

    with pytest.raises(ValueError, match="restore target"):
        restore_broker_state(target, snapshot)

    assert capture_broker_state(target).to_state() == before
