from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from modular_core import PositionSide

from modular_strategy_position_exit_management import (
    ManagementAction,
    PositionExitReason,
    PositionManagementStatus,
)


def test_public_status_mapping_rejects_resource_unbounded_decimal():
    status = PositionManagementStatus(
        position_id="position-1",
        setup_id=None,
        symbol="BTCUSDT",
        timeframe="5m",
        side=PositionSide.LONG,
        quantity=Decimal("1e4097"),
        entry_price=Decimal("100"),
        initial_risk_per_unit=Decimal("5"),
        current_r=Decimal("1"),
        favorable_extreme=Decimal("105"),
        stop_loss=Decimal("95"),
        take_profit=None,
        completed_partial_indices=(),
        pending_partial_index=None,
        trailing_active=False,
        breakeven_applied=False,
        action=ManagementAction.NONE,
        reason=PositionExitReason.POSITION_MONITORED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        diagnostics={},
    )

    with pytest.raises(ValueError, match="supported Decimal precision or exponent range"):
        status.as_mapping()
