from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_core import Candle, MarketView, PositionSide, PositionSnapshot
from strategy_modules.risk_sizing import InstrumentRiskRules, PortfolioRiskSnapshot

from modular_strategy_risk_request_composer import (
    INSTRUMENT_RULES_FEATURE,
    PORTFOLIO_RISK_FEATURE,
    RuntimeRiskContextError,
    RuntimeRiskContextModule,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _rules(*, contract_size: str = "1") -> dict[str, object]:
    return {
        "quantity_step": "0.001",
        "min_quantity": "0.001",
        "min_notional": "5",
        "tick_size": "0.1",
        "contract_size": contract_size,
        "max_leverage": "3",
    }


def _position(
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: float = 2.0,
    entry_price: float = 100.0,
    stop_loss: float | None = 95.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        id="position-1",
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_time=NOW,
        stop_loss=stop_loss,
        take_profit=120.0 if side is PositionSide.LONG else 80.0,
        tag="test",
        unrealized_pnl=0.0,
    )


def _view(*, close: float = 110.0, position: PositionSnapshot | None = None) -> MarketView:
    bar = Candle(
        timestamp=NOW,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketView(
        base_bars=(bar,),
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=position,
        pending_orders=(),
    )


def _values(module: RuntimeRiskContextModule, view: MarketView) -> dict[str, object]:
    decision = module.on_bar(view)
    return {feature.key: feature.value for feature in decision.features}


def test_flat_portfolio_publishes_typed_zero_snapshot_and_exact_instrument() -> None:
    module = RuntimeRiskContextModule(instrument_rules=_rules())

    values = _values(module, _view())

    instrument = InstrumentRiskRules.from_mapping(values[INSTRUMENT_RULES_FEATURE])
    portfolio = PortfolioRiskSnapshot.from_mapping(values[PORTFOLIO_RISK_FEATURE])
    assert instrument.quantity_step == Decimal("0.001")
    assert portfolio == PortfolioRiskSnapshot()


def test_open_position_uses_current_notional_and_current_stop_risk_geometry() -> None:
    module = RuntimeRiskContextModule(instrument_rules=_rules(contract_size="10"))

    values = _values(module, _view(close=110.0, position=_position()))

    portfolio = PortfolioRiskSnapshot.from_mapping(values[PORTFOLIO_RISK_FEATURE])
    assert portfolio.gross_notional == Decimal("2200")
    assert portfolio.symbol_notional == Decimal("2200")
    assert portfolio.open_risk_amount == Decimal("100")
    assert portfolio.open_positions == 1


def test_short_position_uses_same_absolute_stop_risk_contract() -> None:
    module = RuntimeRiskContextModule(instrument_rules=_rules(contract_size="10"))
    position = _position(side=PositionSide.SHORT, stop_loss=105.0)

    values = _values(module, _view(close=90.0, position=position))

    portfolio = PortfolioRiskSnapshot.from_mapping(values[PORTFOLIO_RISK_FEATURE])
    assert portfolio.gross_notional == Decimal("1800")
    assert portfolio.open_risk_amount == Decimal("100")
    assert portfolio.open_positions == 1


def test_open_position_without_stop_fails_closed() -> None:
    module = RuntimeRiskContextModule(instrument_rules=_rules())

    with pytest.raises(RuntimeRiskContextError, match="no stop_loss"):
        module.on_bar(_view(position=_position(stop_loss=None)))


def test_invalid_or_missing_instrument_rules_fail_at_construction() -> None:
    with pytest.raises(ValueError, match="instrument_rules are required"):
        RuntimeRiskContextModule()
    with pytest.raises(ValueError, match="invalid instrument_rules"):
        RuntimeRiskContextModule(instrument_rules={"quantity_step": "0"})


def test_instrument_configuration_is_immutable_and_normalized() -> None:
    source = _rules()
    module = RuntimeRiskContextModule(instrument_rules=source)
    source["quantity_step"] = "999"

    configured = module.parameters["instrument_rules"]
    parsed = InstrumentRiskRules.from_mapping(configured)
    assert parsed.quantity_step == Decimal("0.001")
    with pytest.raises(TypeError):
        configured["quantity_step"] = "1"  # type: ignore[index]
