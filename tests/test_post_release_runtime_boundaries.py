from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from modular_core import (
    Candle,
    DataConfig,
    OrderRequest,
    OrderType,
    RiskConfig,
    Side,
)
from modular_core.adapters import SimulationClock
from modular_core.data import timeframe_to_seconds, validate_candles
from modular_core.readiness import ReadinessView, calculate_readiness
from modular_core.risk import FixedFractionalSizer
from modular_core.series import ReadOnlySeries


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(index: int = 0) -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _readiness(**overrides: object) -> ReadinessView:
    values: dict[str, object] = {
        "is_ready": False,
        "available_bars": {"5m": 2, "15m": 0},
        "required_bars": {"5m": 2, "15m": 1},
        "missing_requirements": ("15m:0/1",),
    }
    values.update(overrides)
    return ReadinessView(**values)  # type: ignore[arg-type]


def test_readiness_view_freezes_detached_consistent_evidence() -> None:
    available = {"5m": 2, "15m": 0}
    required = {"5m": 2, "15m": 1}
    missing = ["15m:0/1"]

    view = ReadinessView(False, available, required, missing)  # type: ignore[arg-type]
    available["5m"] = 0
    required["15m"] = 0
    missing.clear()

    assert dict(view.available_bars) == {"5m": 2, "15m": 0}
    assert dict(view.required_bars) == {"5m": 2, "15m": 1}
    assert view.missing_requirements == ("15m:0/1",)
    with pytest.raises(TypeError):
        view.available_bars["5m"] = 3  # type: ignore[index]


@pytest.mark.parametrize("value", [1, "yes", None, object()])
def test_readiness_view_requires_exact_bool(value: object) -> None:
    with pytest.raises(TypeError, match="is_ready must be bool"):
        _readiness(is_ready=value)


@pytest.mark.parametrize("field", ["available_bars", "required_bars"])
@pytest.mark.parametrize("value", [None, (), "5m", object()])
def test_readiness_view_requires_count_mappings(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=field):
        _readiness(**{field: value})


@pytest.mark.parametrize(
    ("field", "mapping", "error"),
    [
        ("available_bars", {"": 0}, ValueError),
        ("available_bars", {" 5m": 0}, ValueError),
        ("available_bars", {1: 0}, TypeError),
        ("available_bars", {"5m": True}, TypeError),
        ("available_bars", {"5m": 1.0}, TypeError),
        ("available_bars", {"5m": -1}, ValueError),
        ("required_bars", {"5m": True}, TypeError),
        ("required_bars", {"5m": -1}, ValueError),
    ],
)
def test_readiness_view_rejects_invalid_counts(
    field: str, mapping: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _readiness(**{field: mapping})


def test_readiness_view_requires_matching_keys() -> None:
    with pytest.raises(ValueError, match="same keys"):
        _readiness(available_bars={"5m": 2})


def test_readiness_view_rejects_forged_missing_and_ready_flags() -> None:
    with pytest.raises(ValueError, match="missing_requirements"):
        _readiness(missing_requirements=())
    with pytest.raises(ValueError, match="is_ready"):
        _readiness(is_ready=True)


def test_calculate_readiness_produces_self_consistent_snapshot() -> None:
    view = calculate_readiness(
        base_timeframe="5m",
        base_bars=(_bar(0), _bar(1)),
        timeframe_bars={"15m": ()},
        warmup_bars={"5m": 2},
        required_timeframes=("15m",),
    )

    assert not view.is_ready
    assert dict(view.available_bars) == {"5m": 2, "15m": 0}
    assert dict(view.required_bars) == {"5m": 2, "15m": 1}
    assert view.missing_requirements == ("15m:0/1",)


@pytest.mark.parametrize("value", [True, 5, object()])
def test_calculate_readiness_rejects_non_string_base_timeframe(
    value: object,
) -> None:
    with pytest.raises(TypeError, match="base_timeframe must be str"):
        calculate_readiness(
            base_timeframe=value,  # type: ignore[arg-type]
            base_bars=(),
            timeframe_bars={},
            warmup_bars={},
        )


def test_calculate_readiness_rejects_noncanonical_boundaries() -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        calculate_readiness(
            base_timeframe=" 5m",
            base_bars=(),
            timeframe_bars={},
            warmup_bars={},
        )
    with pytest.raises(TypeError, match="base_bars must be a sequence"):
        calculate_readiness(
            base_timeframe="5m",
            base_bars=iter(()),  # type: ignore[arg-type]
            timeframe_bars={},
            warmup_bars={},
        )
    with pytest.raises(TypeError, match="timeframe_bars must be a mapping"):
        calculate_readiness(
            base_timeframe="5m",
            base_bars=(),
            timeframe_bars=(),  # type: ignore[arg-type]
            warmup_bars={},
        )
    with pytest.raises(TypeError, match="must be a sequence"):
        calculate_readiness(
            base_timeframe="5m",
            base_bars=(),
            timeframe_bars={"15m": iter(())},  # type: ignore[dict-item]
            warmup_bars={},
        )
    with pytest.raises(ValueError, match="duplicates"):
        calculate_readiness(
            base_timeframe="5m",
            base_bars=(),
            timeframe_bars={},
            warmup_bars={},
            required_timeframes=("15m", "15m"),
        )


def test_read_only_series_validates_snapshot_bounds_types() -> None:
    source = [1, 2, 3]
    assert tuple(ReadOnlySeries(source, 3, start=1)) == (2, 3)

    with pytest.raises(TypeError, match="source must be a sequence"):
        ReadOnlySeries(iter(source))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="start must be int"):
        ReadOnlySeries(source, start=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="end must be int or None"):
        ReadOnlySeries(source, 2.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 5, [], object()])
def test_timeframe_to_seconds_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError, match="timeframe must be str"):
        timeframe_to_seconds(value)  # type: ignore[arg-type]


def test_validate_candles_requires_explicit_config_type() -> None:
    bars = (_bar(),)
    assert validate_candles(bars, None) == list(bars)
    for value in (False, 0, {}, object()):
        with pytest.raises(TypeError, match="config must be DataConfig or None"):
            validate_candles(bars, value)  # type: ignore[arg-type]


def test_validate_candles_rejects_invalid_container_and_elements() -> None:
    with pytest.raises(TypeError, match="iterable of Candle"):
        validate_candles("not candles")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="iterable of Candle"):
        validate_candles(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="contain Candle"):
        validate_candles((_bar(), object()))  # type: ignore[arg-type]


def test_validate_candles_materializes_one_shot_iterable_once() -> None:
    values = iter((_bar(0), _bar(1)))
    assert validate_candles(values, DataConfig()) == [_bar(0), _bar(1)]
    assert list(values) == []


class _BrokenTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01",
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=_BrokenTimezone()),
    ],
)
def test_simulation_clock_rejects_invalid_timestamps(value: object) -> None:
    clock = SimulationClock()
    with pytest.raises((TypeError, ValueError), match="clock timestamp"):
        clock.advance(value)  # type: ignore[arg-type]
    assert clock.now is None


def test_simulation_clock_normalizes_utc_before_monotonicity() -> None:
    clock = SimulationClock()
    clock.advance(datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))))
    assert clock.now == NOW
    clock.advance(NOW)
    assert clock.now == NOW
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance(NOW - timedelta(seconds=1))
    assert clock.now == NOW


def test_risk_sizer_supports_decimal_explicit_quantity_and_config() -> None:
    request = OrderRequest(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1.234"),
    )
    sizer = FixedFractionalSizer(
        RiskConfig(
            max_notional_multiple=Decimal("100"),
            min_notional=Decimal("1"),
            min_quantity=Decimal("0.01"),
            quantity_step=Decimal("0.01"),
        )
    )

    quantity = sizer.size(
        request,
        equity=Decimal("1000"),
        reference_price=Decimal("100"),
        contract_size=Decimal("1"),
    )

    assert quantity == 1.23
    assert isinstance(quantity, float)


def test_risk_sizer_supports_decimal_risk_based_inputs() -> None:
    request = OrderRequest(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        stop_loss=Decimal("90"),
        risk_fraction=Decimal("0.01"),
    )
    sizer = FixedFractionalSizer(
        RiskConfig(
            default_risk_fraction=Decimal("0.005"),
            max_risk_fraction=Decimal("0.02"),
            max_notional_multiple=Decimal("10"),
            quantity_step=Decimal("0.1"),
            min_stop_distance_pct=Decimal("0.01"),
            max_stop_distance_pct=Decimal("0.2"),
        )
    )

    quantity = sizer.size(
        request,
        equity=Decimal("1000"),
        reference_price=Decimal("100"),
    )

    assert quantity == 1.0
