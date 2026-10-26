from __future__ import annotations

import math
from decimal import Decimal

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule
from modular_strategy_position_exit_management.module import (
    _partial_transport_quantity,
)


def _initialized_runtime(module, view_factory, candle_factory, position_factory):
    position = position_factory(quantity=1.0, stop_loss=95)
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=101, low=99, close=100),
            position=position,
        )
    )
    runtime = module._state.active_position
    assert runtime is not None
    return position, runtime


def test_near_full_partial_does_not_collapse_to_full_close(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": "0.99999999999999999"}]
    )
    position = position_factory(quantity=1.0, stop_loss=95)
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=106, low=99, close=105),
            position=position,
        )
    )

    assert decision.closes
    assert decision.closes[0].quantity == math.nextafter(1.0, 0.0)
    assert decision.closes[0].quantity < position.quantity
    active = module.snapshot_state()["active_position"]
    assert active["pending_partial_target_quantity"] == "0.00000000000000001"


def test_near_full_partial_retry_also_preserves_positive_residual(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": "0.99999999999999999"}]
    )
    original = position_factory(quantity=1.0, stop_loss=95)
    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=106, low=99, close=105),
            position=original,
        )
    )
    remaining = original.quantity - first.closes[0].quantity
    assert remaining > 0

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=position_factory(quantity=remaining, stop_loss=95),
        )
    )

    assert retry.closes
    assert retry.closes[0].quantity is not None
    assert 0 < retry.closes[0].quantity < remaining
    assert retry.closes[0].quantity == pytest.approx(remaining - 1e-17, rel=1e-12)


@pytest.mark.parametrize(
    ("current", "requested", "target"),
    [
        (Decimal("sNaN"), Decimal("1"), Decimal("0")),
        (Decimal("Infinity"), Decimal("1"), Decimal("0")),
        (Decimal("1"), Decimal("Infinity"), Decimal("0")),
        (Decimal("1"), Decimal("0"), Decimal("1")),
    ],
)
def test_partial_transport_rejects_unrepresentable_values(
    current: Decimal,
    requested: Decimal,
    target: Decimal,
) -> None:
    assert _partial_transport_quantity(current, requested, target) is None


def test_partial_request_rejects_missing_position_defensively(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    _, runtime = _initialized_runtime(module, view_factory, candle_factory, position_factory)

    with pytest.raises(RuntimeError, match="requires an open position"):
        module._partial_exit_request(
            view_factory(1, candle=candle_factory(1), position=None),
            runtime,
            Decimal("1"),
            {},
        )


def test_partial_request_rejects_missing_pending_target_defensively(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position, runtime = _initialized_runtime(module, view_factory, candle_factory, position_factory)
    runtime.pending_partial_index = 0
    runtime.pending_partial_target_quantity = None

    with pytest.raises(RuntimeError, match="requires target quantity"):
        module._partial_exit_request(
            view_factory(1, candle=candle_factory(1), position=position),
            runtime,
            Decimal("1"),
            {},
        )


def test_partial_request_confirms_nonpositive_residual_defensively(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position, runtime = _initialized_runtime(module, view_factory, candle_factory, position_factory)
    runtime.pending_partial_index = 0
    runtime.pending_partial_target_quantity = Decimal(str(position.quantity))

    result = module._partial_exit_request(
        view_factory(1, candle=candle_factory(1), position=position),
        runtime,
        Decimal("1"),
        {},
    )

    assert result is None
    assert runtime.completed_partial_indices == {0}
    assert runtime.pending_partial_index is None
    assert runtime.pending_partial_target_quantity is None


def test_reconcile_tolerance_requires_observed_quantity_reduction(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    _, runtime = _initialized_runtime(module, view_factory, candle_factory, position_factory)
    runtime.pending_partial_index = 0
    runtime.pending_partial_target_quantity = Decimal("0.99999999999999995")
    diagnostics: dict[str, object] = {}

    module._reconcile_partial(1.0, diagnostics)
    assert runtime.pending_partial_index == 0
    assert runtime.completed_partial_indices == set()

    module._reconcile_partial(math.nextafter(1.0, 0.0), diagnostics)
    assert runtime.pending_partial_index is None
    assert runtime.completed_partial_indices == {0}
    assert diagnostics["partial_completed_index"] == 0


def test_subnormal_partial_fails_closed_when_no_positive_float_request_exists(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    minimum_positive_float = 5e-324
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position = position_factory(
        quantity=minimum_positive_float,
        entry_price=1e-300,
        stop_loss=5e-301,
        take_profit=None,
    )

    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(
                0,
                open_=1e-300,
                high=2e-300,
                low=5e-301,
                close=2e-300,
            ),
            position=position,
        )
    )

    assert decision.closes == ()
    assert decision.diagnostics["partial_transport_unrepresentable"] is True
    active = module.snapshot_state()["active_position"]
    assert active["completed_partial_indices"] == ()
    assert active["pending_partial_index"] is None
    assert active["pending_partial_target_quantity"] is None
