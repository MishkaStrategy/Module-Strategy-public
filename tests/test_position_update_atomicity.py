from datetime import datetime, timezone

from modular_core import EngineConfig, PositionSide, PositionUpdateRequest
from modular_core.audit import Journal
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_core.models import Position

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _broker() -> BrokerSimulator:
    journal = Journal(run_id="run-test")
    broker = BrokerSimulator(
        EngineConfig(),
        journal,
        InstrumentSpec(symbol="X", tick_size=10),
    )
    broker.portfolio.position = Position(
        id="pos-1",
        entry_order_id="ord-1",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
        stop_loss=80,
        take_profit=120,
    )
    return broker


def test_rejected_target_normalization_does_not_partially_change_stop() -> None:
    broker = _broker()
    changed = broker.update_position(
        PositionUpdateRequest(stop_loss=94, take_profit=101),
        timestamp=NOW,
        source_module="risk",
        mark_price=100,
    )
    assert changed is False
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == 80
    assert broker.portfolio.position.take_profit == 120


def test_valid_update_commits_both_normalized_prices_together() -> None:
    broker = _broker()
    changed = broker.update_position(
        PositionUpdateRequest(stop_loss=94, take_profit=119),
        timestamp=NOW,
        source_module="risk",
        mark_price=100,
    )
    assert changed is True
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == 90
    assert broker.portfolio.position.take_profit == 110
