from __future__ import annotations

import pytest
from modular_core import PositionSide

from modular_strategy_position_exit_management import PositionExitManagementModule
from modular_strategy_position_exit_management.models import (
    ManagementAction,
    PositionExitReason,
)


def _metadata(
    *,
    max_holding_seconds: int | None = 30_000,
    no_progress_after_seconds: int | None = 600,
    minimum_mfe_r: str | None = "0.5",
) -> dict[str, object]:
    result: dict[str, object] = {}
    if max_holding_seconds is not None:
        result["router_max_holding_seconds"] = max_holding_seconds
    if no_progress_after_seconds is not None:
        result["router_no_progress_after_seconds"] = no_progress_after_seconds
    if minimum_mfe_r is not None:
        result["router_no_progress_minimum_mfe_r"] = minimum_mfe_r
    return result


def test_no_progress_uses_post_fill_mfe_and_exact_bar(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(
        side=PositionSide.LONG,
        entry_price=100,
        stop_loss=95,
        entry_index=0,
        metadata=_metadata(),
    )

    fill_bar = candle_factory(0, high=110, close=109)
    fill_decision = module.on_bar(view_factory(0, candle=fill_bar, position=position))
    assert fill_decision.closes == ()

    first = candle_factory(1, high=101, close=100)
    first_decision = module.on_bar(view_factory(1, candle=first, position=position))
    assert first_decision.closes == ()

    trigger = candle_factory(2, high=102, close=100)
    decision = module.on_bar(view_factory(2, candle=trigger, position=position))

    assert decision.closes[0].reason == "no_progress"
    assert decision.diagnostics["lifecycle_mfe_r"] == pytest.approx(0.4)
    assert decision.diagnostics["bars_since_fill"] == 2
    assert decision.features[0].value["reason"] == PositionExitReason.NO_PROGRESS_EXIT.value
    assert decision.features[0].value["action"] == ManagementAction.NO_PROGRESS_EXIT.value


def test_no_progress_threshold_is_strictly_less_than(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(
        entry_price=100,
        stop_loss=95,
        entry_index=0,
        metadata=_metadata(),
    )

    module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    module.on_bar(view_factory(1, candle=candle_factory(1, high=101), position=position))
    decision = module.on_bar(
        view_factory(2, candle=candle_factory(2, high=102.5), position=position)
    )

    assert decision.closes == ()
    assert decision.diagnostics["lifecycle_mfe_r"] == pytest.approx(0.5)


def test_max_hold_uses_authoritative_minus_one_boundary(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(
        entry_index=0,
        metadata=_metadata(
            max_holding_seconds=1_200,
            no_progress_after_seconds=None,
            minimum_mfe_r=None,
        ),
    )

    for index in (0, 1, 2):
        decision = module.on_bar(
            view_factory(index, candle=candle_factory(index), position=position)
        )
        assert decision.closes == ()

    decision = module.on_bar(view_factory(3, candle=candle_factory(3), position=position))
    assert decision.closes[0].reason == "max_hold"
    assert decision.diagnostics["bars_since_fill"] == 3
    assert decision.features[0].value["reason"] == PositionExitReason.MAX_HOLD_EXIT.value
    assert decision.features[0].value["action"] == ManagementAction.MAX_HOLD_EXIT.value


def test_lifecycle_metadata_is_ignored_without_explicit_opt_in(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule()
    position = position_factory(
        entry_index=0,
        metadata=_metadata(
            max_holding_seconds=300,
            no_progress_after_seconds=None,
            minimum_mfe_r=None,
        ),
    )

    module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    decision = module.on_bar(view_factory(1, candle=candle_factory(1), position=position))

    assert decision.closes == ()


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(
            max_holding_seconds=301,
            no_progress_after_seconds=None,
            minimum_mfe_r=None,
        ),
        _metadata(
            no_progress_after_seconds=600,
            minimum_mfe_r=None,
        ),
        _metadata(
            no_progress_after_seconds=None,
            minimum_mfe_r="0.5",
        ),
        _metadata(
            no_progress_after_seconds=600,
            minimum_mfe_r="NaN",
        ),
        _metadata(
            max_holding_seconds=600,
            no_progress_after_seconds=1_200,
            minimum_mfe_r="0.5",
        ),
    ],
)
def test_malformed_lifecycle_metadata_fails_closed(
    metadata,
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(entry_index=0, metadata=metadata)

    with pytest.raises(ValueError):
        module.on_bar(view_factory(0, candle=candle_factory(0), position=position))


def test_no_progress_cannot_be_adopted_after_fill_without_mfe_history(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(entry_index=0, metadata=_metadata())

    with pytest.raises(ValueError, match="cannot initialize no-progress lifecycle"):
        module.on_bar(view_factory(1, candle=candle_factory(1), position=position))


def test_lifecycle_state_round_trip_preserves_mfe_history(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    source = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(
        side=PositionSide.LONG,
        entry_price=100,
        stop_loss=95,
        entry_index=0,
        metadata=_metadata(),
    )

    source.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    source.on_bar(view_factory(1, candle=candle_factory(1, high=101, close=100), position=position))

    restored = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    restored.restore_state(source.snapshot_state())
    decision = restored.on_bar(
        view_factory(2, candle=candle_factory(2, high=102, close=100), position=position)
    )

    assert decision.closes[0].reason == "no_progress"
    assert decision.diagnostics["lifecycle_mfe_r"] == pytest.approx(0.4)


def test_no_progress_short_uses_favorable_low(
    position_factory,
    candle_factory,
    view_factory,
) -> None:
    module = PositionExitManagementModule(honor_position_lifecycle_metadata=True)
    position = position_factory(
        side=PositionSide.SHORT,
        entry_price=100,
        stop_loss=105,
        entry_index=0,
        metadata=_metadata(),
    )

    module.on_bar(view_factory(0, candle=candle_factory(0, low=90, close=91), position=position))
    module.on_bar(view_factory(1, candle=candle_factory(1, low=99, close=100), position=position))
    decision = module.on_bar(
        view_factory(2, candle=candle_factory(2, low=98, close=100), position=position)
    )

    assert decision.closes[0].reason == "no_progress"
    assert decision.diagnostics["lifecycle_mfe_r"] == pytest.approx(0.4)
