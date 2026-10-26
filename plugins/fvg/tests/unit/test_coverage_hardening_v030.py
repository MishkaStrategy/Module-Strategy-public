from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from conftest import FakeCandle, bullish_triplet
from modular_strategy_fvg import FvgConfig
from modular_strategy_fvg.analysis import (
    average_candle_range,
    average_true_range,
    calculate_metrics,
    candle_body,
    candle_direction,
    candle_range,
    higher_timeframe_alignment,
    simple_average,
    true_range,
    utc_session,
    zone_overlap_fraction,
    zones_have_opposite_overlap,
)
from modular_strategy_fvg.detector import detect_zone
from modular_strategy_fvg.filters import (
    _bounded_trace,
    _ema,
    _median,
    _price_bounds,
    _volume,
    evaluate_gap_size_filter,
    evaluate_price_filter,
    evaluate_volume_filter,
)
from modular_strategy_fvg.models import FvgDirection, FvgFilterDecision, FvgZone


def _zone_and_metrics() -> tuple[FvgZone, object, object, object, object]:
    a, b, c = bullish_triplet()
    config = FvgConfig(timeframes=("15m",))
    zone = detect_zone(a, b, c, config)
    assert zone is not None
    metrics = calculate_metrics(
        zone=zone,
        a=a,
        b=b,
        c=c,
        history=(a, b, c),
        reference_price=Decimal("100"),
        tick_size=Decimal("1"),
        atr_period=2,
        average_range_period=3,
        volume_ratio=Decimal("1"),
    )
    return zone, metrics, a, b, c


@pytest.mark.parametrize(
    "parameters",
    [
        {"min_gap_absolute": "-1"},
        {"min_gap_fraction": "1"},
        {"min_gap_size": "-1"},
        {"min_gap_size": "2", "max_gap_size": "1"},
        {"tick_size": "0"},
        {"gap_size_mode": "ticks"},
        {"price_filter_lower_fraction": "-0.1"},
        {"price_filter_lower_fraction": "1.1"},
        {"price_filter_min": "0"},
        {"price_filter_min": "2", "price_filter_max": "1"},
        {"price_filter_mode": "absolute"},
        {"price_filter_mode": "anchor_fraction"},
        {"volume_min_ratio": "-1"},
        {"volume_min_ratio": "2", "volume_max_ratio": "1"},
        {"volume_filter_mode": "percentile", "volume_min_ratio": "1.1"},
        {
            "volume_filter_mode": "percentile",
            "volume_min_ratio": "0",
            "volume_max_ratio": "1.1",
        },
        {
            "timeframes": ("5m",),
            "pre_fvg_enabled": True,
            "pre_fvg_evaluation_offset": "5m",
        },
    ],
)
def test_config_numeric_contracts_fail_closed(parameters: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        FvgConfig.from_parameters(**parameters)


@pytest.mark.parametrize(
    "parameters",
    [
        {"direction_filter": object()},
        {"pre_fvg_evaluation_offset": 3},
        {"source_name": 1},
        {"source_name": "   "},
        {"timeframe_overrides": []},
        {"timeframe_overrides": {1: {}}},
        {"timeframe_overrides": {"4h": {}}},
        {"timeframe_overrides": {"15m": []}},
        {"timeframe_overrides": {"15m": {1: "x"}}},
        {"timeframe_overrides": {"15m": {"unknown": 1}}},
    ],
)
def test_config_structural_contracts_fail_closed(parameters: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        FvgConfig.from_parameters(timeframes=("15m",), **parameters)


def test_config_timeframe_override_and_evaluation_requirements() -> None:
    config = FvgConfig.from_parameters(
        timeframes=("15m",),
        timeframe_overrides={
            "15m": {
                "gap_size_mode": "absolute",
                "min_gap_size": "1",
                "volume_filter_mode": "b_to_sma",
                "volume_lookback": 2,
            }
        },
    )
    effective = config.for_timeframe("15m")
    assert effective.gap_size_mode.value == "absolute"
    assert effective.min_gap_size == Decimal("1")
    assert effective.requires_confirmed_evaluation
    assert config.for_timeframe("15m") == effective
    with pytest.raises(TypeError):
        config.for_timeframe(15)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        config.for_timeframe("4h")

    assert FvgConfig(timeframes=("15m",)).requires_confirmed_evaluation is False
    assert FvgConfig.from_parameters(timeframes=("15m",), publish_evaluation_events=True).requires_confirmed_evaluation
    assert FvgConfig.from_parameters(
        timeframes=("15m",), pre_fvg_enabled=True, pre_fvg_evaluation_offset="3m"
    ).requires_confirmed_evaluation


def test_analysis_primitives_and_sessions() -> None:
    a, b, c = bullish_triplet()
    missing_open = FakeCandle(c.timestamp, c.close_time, c.high, c.low, c.close)
    assert candle_range(a) == Decimal("10")
    assert candle_body(a) == Decimal("0")
    assert candle_body(missing_open) is None
    assert candle_direction(a) == "neutral"
    assert candle_direction(b) == "bullish"
    bearish = replace(b, open=b.close + 1)
    assert candle_direction(bearish) == "bearish"
    assert candle_direction(missing_open) == "unknown"
    assert simple_average(()) is None
    assert simple_average((Decimal("1"), Decimal("3"))) == Decimal("2")
    assert true_range(b, None) == Decimal("11")
    assert true_range(b, Decimal("80")) == Decimal("22")
    assert average_true_range((a, b), 2) is None
    assert average_true_range((a, b, c), 2) is not None
    assert average_candle_range((a,), 2) is None
    assert average_candle_range((a, b), 2) is not None
    with pytest.raises(TypeError):
        average_true_range((a, b), 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        average_true_range((a, b), 0)
    with pytest.raises(TypeError):
        average_candle_range((a, b), "2")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        average_candle_range((a, b), 0)
    assert utc_session(0) == "asia"
    assert utc_session(8) == "europe"
    assert utc_session(16) == "us"
    with pytest.raises(TypeError):
        utc_session("1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        utc_session(24)


def test_analysis_metrics_overlap_and_alignment() -> None:
    zone, metrics, a, b, c = _zone_and_metrics()
    assert metrics.gap_size == zone.size
    assert metrics.gap_ticks == zone.size
    assert metrics.gap_atr is not None
    assert metrics.gap_b_range is not None
    assert metrics.gap_average_range is not None
    assert metrics.candle_b_direction == "bullish"
    assert metrics.volume_ratio == Decimal("1")
    assert metrics.distance_from_reference is not None

    no_optional = calculate_metrics(
        zone=zone,
        a=FakeCandle(a.timestamp, a.close_time, a.high, a.low, a.close),
        b=FakeCandle(b.timestamp, b.close_time, b.high, b.low, b.close),
        c=FakeCandle(c.timestamp, c.close_time, c.high, c.low, c.close),
        history=(),
        reference_price=None,
        tick_size=None,
        atr_period=2,
        average_range_period=2,
    )
    assert no_optional.candle_b_body is None
    assert no_optional.impulse_efficiency is None
    assert no_optional.distance_from_reference is None

    overlapping = replace(zone, zone_id="overlap", lower=Decimal("99"), upper=Decimal("106"))
    disjoint = replace(zone, zone_id="disjoint", lower=Decimal("120"), upper=Decimal("130"))
    opposite = replace(
        overlapping,
        zone_id="opposite",
        direction=FvgDirection.BEARISH,
    )
    assert zone_overlap_fraction(zone, overlapping) > 0
    assert zone_overlap_fraction(zone, disjoint) == 0
    assert zones_have_opposite_overlap(zone, opposite)
    assert not zones_have_opposite_overlap(zone, overlapping)
    with pytest.raises(TypeError):
        zone_overlap_fraction(zone, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        zone_overlap_fraction(zone, replace(zone, zone_id="eth", symbol="ETHUSDT"))

    high_a, high_b, high_c = bullish_triplet(timeframe="4h")
    high_zone = detect_zone(high_a, high_b, high_c, FvgConfig(timeframes=("4h",)))
    assert high_zone is not None
    assert higher_timeframe_alignment(zone, (high_zone,)) == (high_zone.zone_id,)
    assert higher_timeframe_alignment(zone, ()) == ()
    with pytest.raises(TypeError):
        higher_timeframe_alignment(zone, "bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        higher_timeframe_alignment(zone, (object(),))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("observed", "minimum", "maximum", "decision"),
    [
        (None, Decimal("1"), Decimal("2"), FvgFilterDecision.UNAVAILABLE),
        (Decimal("0"), Decimal("1"), Decimal("2"), FvgFilterDecision.FAILED),
        (Decimal("3"), Decimal("1"), Decimal("2"), FvgFilterDecision.FAILED),
        (Decimal("1.5"), Decimal("1"), Decimal("2"), FvgFilterDecision.PASSED),
    ],
)
def test_bounded_trace_all_decisions(
    observed: Decimal | None,
    minimum: Decimal | None,
    maximum: Decimal | None,
    decision: FvgFilterDecision,
) -> None:
    assert (
        _bounded_trace(
            name="x",
            observed=observed,
            minimum=minimum,
            maximum=maximum,
            unavailable_reason="missing",
        ).decision
        is decision
    )


@pytest.mark.parametrize(
    "mode",
    ["absolute", "fraction", "bps", "ticks", "atr", "b_range", "average_range"],
)
def test_gap_size_filter_modes(mode: str) -> None:
    _, metrics, *_ = _zone_and_metrics()
    parameters: dict[str, object] = {
        "timeframes": ("15m",),
        "gap_size_mode": mode,
        "min_gap_size": "0",
    }
    if mode == "ticks":
        parameters["tick_size"] = "1"
    trace = evaluate_gap_size_filter(metrics, FvgConfig.from_parameters(**parameters))
    assert trace.name == "gap_size"
    assert trace.decision in {
        FvgFilterDecision.PASSED,
        FvgFilterDecision.UNAVAILABLE,
    }

    high = dict(parameters)
    high["min_gap_size"] = "1000000"
    assert evaluate_gap_size_filter(metrics, FvgConfig.from_parameters(**high)).decision in {
        FvgFilterDecision.FAILED,
        FvgFilterDecision.UNAVAILABLE,
    }


def test_legacy_gap_filter_and_invalid_arguments() -> None:
    _, metrics, *_ = _zone_and_metrics()
    passed = evaluate_gap_size_filter(metrics, FvgConfig(timeframes=("15m",)))
    failed = evaluate_gap_size_filter(
        metrics,
        FvgConfig.from_parameters(timeframes=("15m",), min_gap_absolute="100"),
    )
    assert passed.decision is FvgFilterDecision.PASSED
    assert failed.decision is FvgFilterDecision.FAILED
    with pytest.raises(TypeError):
        evaluate_gap_size_filter(object(), FvgConfig())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_gap_size_filter(metrics, object())  # type: ignore[arg-type]


def test_price_filter_modes_and_match_policies() -> None:
    zone, *_ = _zone_and_metrics()
    disabled = evaluate_price_filter(zone, FvgConfig(timeframes=("15m",)), reference_price=None)
    assert disabled.decision is FvgFilterDecision.SKIPPED

    absolute = FvgConfig.from_parameters(
        timeframes=("15m",),
        price_filter_mode="absolute",
        price_filter_min="99",
        price_filter_max="106",
    )
    assert _price_bounds(absolute, None)[:2] == (Decimal("99"), Decimal("106"))
    for policy in ("midpoint", "overlap", "contained"):
        config = FvgConfig.from_parameters(
            timeframes=("15m",),
            price_filter_mode="absolute",
            price_filter_min="99",
            price_filter_max="106",
            price_filter_match_policy=policy,
        )
        assert evaluate_price_filter(zone, config, reference_price=None).decision is FvgFilterDecision.PASSED

    current = FvgConfig.from_parameters(
        timeframes=("15m",),
        price_filter_mode="current_fraction",
        price_filter_lower_fraction="0.1",
        price_filter_upper_fraction="0.1",
    )
    assert evaluate_price_filter(zone, current, reference_price=None).decision is FvgFilterDecision.UNAVAILABLE
    assert evaluate_price_filter(zone, current, reference_price=Decimal("100")).decision is FvgFilterDecision.PASSED

    anchor = FvgConfig.from_parameters(
        timeframes=("15m",),
        price_filter_mode="anchor_fraction",
        price_filter_anchor="100",
        price_filter_lower_fraction="0.01",
        price_filter_upper_fraction="0.01",
    )
    assert _price_bounds(anchor, Decimal("200"))[2] == Decimal("100")
    assert evaluate_price_filter(zone, anchor, reference_price=None).decision is FvgFilterDecision.FAILED

    with pytest.raises(TypeError):
        evaluate_price_filter(object(), absolute, reference_price=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_price_filter(zone, object(), reference_price=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_price_filter(zone, absolute, reference_price=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evaluate_price_filter(zone, absolute, reference_price=Decimal("NaN"))
    with pytest.raises(ValueError):
        evaluate_price_filter(zone, absolute, reference_price=Decimal("0"))


class _VolumeOnly:
    def __init__(self, volume: object = None) -> None:
        self.volume = volume


@pytest.mark.parametrize(
    "mode",
    ["b_to_sma", "b_to_ema", "b_to_median", "abc_to_sma", "percentile"],
)
def test_volume_filter_modes(mode: str) -> None:
    previous = (_VolumeOnly(1), _VolumeOnly(2), _VolumeOnly(3))
    config = FvgConfig.from_parameters(
        timeframes=("15m",),
        volume_filter_mode=mode,
        volume_lookback=3,
        volume_min_ratio="0",
        volume_max_ratio="1" if mode == "percentile" else "10",
    )
    trace = evaluate_volume_filter(
        a=_VolumeOnly(2),
        b=_VolumeOnly(3),
        c=_VolumeOnly(4),
        previous_bars=previous,
        config=config,
    )
    assert trace.decision is FvgFilterDecision.PASSED


def test_volume_missing_policies_and_helpers() -> None:
    assert _volume(_VolumeOnly()) is None
    assert _volume(_VolumeOnly(2)) == Decimal("2")
    with pytest.raises(ValueError):
        _volume(_VolumeOnly(-1))
    assert _ema([Decimal("1"), Decimal("2")]) > Decimal("1")
    assert _median([Decimal("3"), Decimal("1"), Decimal("2")]) == Decimal("2")
    assert _median([Decimal("4"), Decimal("2")]) == Decimal("3")

    for policy, decision in (
        ("allow", FvgFilterDecision.SKIPPED),
        ("reject", FvgFilterDecision.FAILED),
    ):
        config = FvgConfig.from_parameters(
            timeframes=("15m",),
            volume_filter_mode="b_to_sma",
            volume_lookback=2,
            volume_missing_policy=policy,
        )
        assert (
            evaluate_volume_filter(
                a=_VolumeOnly(),
                b=_VolumeOnly(),
                c=_VolumeOnly(),
                previous_bars=(_VolumeOnly(),),
                config=config,
            ).decision
            is decision
        )

    error = FvgConfig.from_parameters(
        timeframes=("15m",),
        volume_filter_mode="b_to_sma",
        volume_lookback=2,
        volume_missing_policy="error",
    )
    with pytest.raises(ValueError):
        evaluate_volume_filter(
            a=_VolumeOnly(),
            b=_VolumeOnly(),
            c=_VolumeOnly(),
            previous_bars=(_VolumeOnly(),),
            config=error,
        )
    with pytest.raises(TypeError):
        evaluate_volume_filter(
            a=_VolumeOnly(),
            b=_VolumeOnly(),
            c=_VolumeOnly(),
            previous_bars=[],  # type: ignore[arg-type]
            config=FvgConfig(),
        )
    with pytest.raises(TypeError):
        evaluate_volume_filter(
            a=_VolumeOnly(),
            b=_VolumeOnly(),
            c=_VolumeOnly(),
            previous_bars=(),
            config=object(),  # type: ignore[arg-type]
        )


def test_filter_zero_reference_and_disabled_volume() -> None:
    disabled = evaluate_volume_filter(
        a=_VolumeOnly(1),
        b=_VolumeOnly(1),
        c=_VolumeOnly(1),
        previous_bars=(),
        config=FvgConfig(timeframes=("15m",)),
    )
    assert disabled.decision is FvgFilterDecision.SKIPPED

    zero_reference = FvgConfig.from_parameters(
        timeframes=("15m",),
        volume_filter_mode="b_to_sma",
        volume_lookback=2,
        volume_missing_policy="reject",
    )
    trace = evaluate_volume_filter(
        a=_VolumeOnly(1),
        b=_VolumeOnly(1),
        c=_VolumeOnly(1),
        previous_bars=(_VolumeOnly(0), _VolumeOnly(0)),
        config=zero_reference,
    )
    assert trace.reason == "volume_reference_zero"
