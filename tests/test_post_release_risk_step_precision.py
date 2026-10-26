from __future__ import annotations

import pytest

from modular_core import OrderRequest, OrderType, RiskConfig, Side
from modular_core.risk import FixedFractionalSizer


def test_risk_sizer_preserves_sub_e16_quantity_step_precision() -> None:
    request = OrderRequest(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.23456789e-8,
    )
    sizer = FixedFractionalSizer(
        RiskConfig(
            quantity_step=1e-17,
            max_notional_multiple=100.0,
        )
    )

    quantity = sizer.size(request, equity=1_000.0, reference_price=1.0)

    assert quantity == pytest.approx(1.23456789e-8, rel=0.0, abs=1e-17)
    assert quantity > 0.0


def test_risk_sizer_requires_risk_config() -> None:
    with pytest.raises(TypeError, match="config must be RiskConfig"):
        FixedFractionalSizer(object())  # type: ignore[arg-type]


def test_risk_sizer_requires_order_request() -> None:
    sizer = FixedFractionalSizer(RiskConfig())
    with pytest.raises(TypeError, match="request must be OrderRequest"):
        sizer.size(object(), equity=1_000.0, reference_price=100.0)  # type: ignore[arg-type]
