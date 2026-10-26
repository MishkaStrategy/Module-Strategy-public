from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core import Candle, Feature, FeatureView, MarketView, SetupStatus
from strategy_modules.breakout_return import (
    BreakoutReturnModule,
    BreakoutReturnStateError,
)

START = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _bar(
    *,
    minutes: int = 5,
    open_price: float = 101.0,
    high: float = 104.0,
    low: float = 100.5,
    close: float = 103.0,
) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=minutes),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _range_feature(
    *,
    source_module: str,
    created_at: datetime,
    high: float,
    low: float,
    feature_id: str,
) -> Feature:
    return Feature(
        key="range.current",
        source_module=source_module,
        created_at=created_at,
        value={"high": high, "low": low, "range_id": feature_id},
        symbol="BTCUSDT",
        timeframe="5m",
        feature_id=feature_id,
    )


def _view(
    features: tuple[Feature, ...],
    *,
    current: Candle | None = None,
) -> MarketView:
    selected = current or _bar()
    return MarketView(
        base_bars=[selected],
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=False,
        features=FeatureView(features, selected.close_time),
    )


def test_source_contract_selects_configured_producer_not_latest_foreign_feature() -> None:
    intended = _range_feature(
        source_module="session_range",
        created_at=START + timedelta(minutes=5),
        high=100.0,
        low=90.0,
        feature_id="intended-range",
    )
    later_foreign = _range_feature(
        source_module="foreign_range",
        created_at=START + timedelta(minutes=9),
        high=200.0,
        low=190.0,
        feature_id="foreign-range",
    )
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="session_range",
    )

    decision = module.on_bar(_view((intended, later_foreign)))

    assert decision.setups[0].status is SetupStatus.ARMED
    assert decision.setups[0].metadata["reference_source"] == (
        "feature:range.current:session_range"
    )
    assert decision.setups[0].metadata["reference_id"] == "intended-range"


def test_source_contract_treats_other_producers_as_missing() -> None:
    foreign = _range_feature(
        source_module="foreign_range",
        created_at=START + timedelta(minutes=5),
        high=100.0,
        low=90.0,
        feature_id="foreign-range",
    )
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="session_range",
    )

    decision = module.on_bar(_view((foreign,)))

    assert decision.skips == ("breakout_return.reference_missing",)
    assert not decision.setups


def test_bound_producer_loss_invalidates_active_setup_under_invalidate_policy() -> None:
    intended = _range_feature(
        source_module="session_range",
        created_at=START + timedelta(minutes=5),
        high=100.0,
        low=90.0,
        feature_id="intended-range",
    )
    foreign = _range_feature(
        source_module="foreign_range",
        created_at=START + timedelta(minutes=10),
        high=100.0,
        low=90.0,
        feature_id="foreign-range",
    )
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="session_range",
        range_change_policy="invalidate",
    )

    armed = module.on_bar(_view((intended,)))
    invalidated = module.on_bar(
        _view(
            (foreign,),
            current=_bar(
                minutes=10,
                open_price=99.0,
                high=100.0,
                low=97.0,
                close=98.0,
            ),
        )
    )

    assert armed.setups[0].status is SetupStatus.ARMED
    assert invalidated.setups[0].status is SetupStatus.INVALIDATED
    assert invalidated.setups[0].reason == "breakout_return.reference_missing"
    assert invalidated.skips == ("breakout_return.reference_missing",)
    assert invalidated.feature_deletes


def test_keep_snapshot_policy_ignores_foreign_replacement_after_arm() -> None:
    intended = _range_feature(
        source_module="session_range",
        created_at=START + timedelta(minutes=5),
        high=100.0,
        low=90.0,
        feature_id="intended-range",
    )
    foreign = _range_feature(
        source_module="foreign_range",
        created_at=START + timedelta(minutes=10),
        high=200.0,
        low=190.0,
        feature_id="foreign-range",
    )
    module = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="session_range",
        range_change_policy="keep_snapshot",
    )

    armed = module.on_bar(_view((intended,)))
    confirmed = module.on_bar(
        _view(
            (foreign,),
            current=_bar(
                minutes=10,
                open_price=99.0,
                high=100.0,
                low=97.0,
                close=98.0,
            ),
        )
    )

    assert armed.setups[0].status is SetupStatus.ARMED
    assert confirmed.setups[0].status is SetupStatus.CONFIRMED
    assert confirmed.setups[0].metadata["reference_id"] == "intended-range"
    assert confirmed.signals


def test_source_contract_is_strict_feature_only_configuration() -> None:
    with pytest.raises(ValueError, match="reference_feature_source_module"):
        BreakoutReturnModule(reference_feature_source_module="session_range")
    with pytest.raises(ValueError, match="nonblank trimmed"):
        BreakoutReturnModule(
            reference_mode="feature",
            reference_feature_source_module=" session_range",
        )


def test_source_contract_is_declared_and_part_of_run_identity() -> None:
    unrestricted = BreakoutReturnModule(reference_mode="feature")
    session_range = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="session_range",
    )
    foreign_range = BreakoutReturnModule(
        reference_mode="feature",
        reference_feature_source_module="foreign_range",
    )

    assert "reference_feature_source_module" not in unrestricted.parameters
    assert session_range.parameters["reference_feature_source_module"] == ("session_range")
    assert "reference_feature_source_module" in session_range.manifest.parameters_schema
    assert unrestricted.config_fingerprint != session_range.config_fingerprint
    assert session_range.config_fingerprint != foreign_range.config_fingerprint

    with pytest.raises(BreakoutReturnStateError, match="configuration fingerprint"):
        foreign_range.restore_state(session_range.snapshot_state())
