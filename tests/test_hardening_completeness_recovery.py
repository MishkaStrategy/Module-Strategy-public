from __future__ import annotations

from datetime import datetime, timezone
import math

import pytest

from modular_core.config import DataConfig, RiskConfig
from modular_core.data import timeframe_to_seconds
from modular_core.instruments import ExchangeRulesAdapter, InstrumentRuleError, InstrumentSpec
from modular_core.models import (
    CancelRequest,
    Candle,
    ClosePositionRequest,
    OrderRequest,
    OrderType,
    PositionUpdateRequest,
    Side,
)
from modular_core.risk import FixedFractionalSizer, RiskError

UTC = timezone.utc


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_close_position_rejects_nonfinite_quantity(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ClosePositionRequest(quantity=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_close_position_rejects_nonfinite_fraction(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ClosePositionRequest(fraction=value)


@pytest.mark.parametrize("field", ["stop_loss", "take_profit"])
def test_position_update_rejects_nonfinite_prices(field: str) -> None:
    values = {"stop_loss": 90.0, "take_profit": 110.0}
    values[field] = math.nan
    with pytest.raises(ValueError, match="finite"):
        PositionUpdateRequest(**values)


def test_cancel_request_rejects_blank_selector() -> None:
    with pytest.raises(ValueError, match="cancel selector"):
        CancelRequest(order_id="   ")


def test_zero_timeframe_is_rejected() -> None:
    assert timeframe_to_seconds("0m") is None
    with pytest.raises(ValueError, match="timeframe amount"):
        Candle(datetime(2026, 1, 1, tzinfo=UTC), 1, 1, 1, 1, timeframe="0m")


def test_data_config_bounds_history() -> None:
    assert DataConfig(max_base_history=10).max_base_history == 10
    with pytest.raises(ValueError, match="max_base_history"):
        DataConfig(max_base_history=0)


@pytest.mark.parametrize(
    "field", ["tick_size", "quantity_step", "min_quantity", "min_notional", "contract_size"]
)
def test_instrument_rejects_nonfinite_values(field: str) -> None:
    values = {"symbol": "BTCUSDT", field: math.nan}
    with pytest.raises(ValueError, match="finite"):
        InstrumentSpec(**values)


def test_instrument_rejects_inconsistent_bounds() -> None:
    with pytest.raises(ValueError, match="max_quantity"):
        InstrumentSpec(symbol="X", min_quantity=2, max_quantity=1)
    with pytest.raises(ValueError, match="max_notional"):
        InstrumentSpec(symbol="X", min_notional=20, max_notional=10)


def test_quantity_is_revalidated_after_notional_cap() -> None:
    adapter = ExchangeRulesAdapter(
        InstrumentSpec(
            symbol="X",
            quantity_step=1,
            min_quantity=2,
            max_notional=150,
        )
    )
    with pytest.raises(InstrumentRuleError, match="below instrument minimum after cap"):
        adapter.normalize_quantity(3, price=100)


def test_contract_size_affects_sizing_and_notional_cap() -> None:
    sizer = FixedFractionalSizer(
        RiskConfig(default_risk_fraction=0.01, max_risk_fraction=0.02, max_notional_multiple=10)
    )
    request = OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, stop_loss=90)
    assert sizer.size(
        request, equity=10_000, reference_price=100, contract_size=10
    ) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "name,value", [("equity", math.nan), ("reference_price", math.inf), ("contract_size", 0)]
)
def test_risk_sizer_rejects_invalid_boundaries(name: str, value: float) -> None:
    request = OrderRequest(side=Side.BUY, order_type=OrderType.MARKET, stop_loss=90)
    kwargs = {"equity": 10_000.0, "reference_price": 100.0, "contract_size": 1.0}
    kwargs[name] = value
    with pytest.raises(RiskError):
        FixedFractionalSizer(RiskConfig()).size(request, **kwargs)
