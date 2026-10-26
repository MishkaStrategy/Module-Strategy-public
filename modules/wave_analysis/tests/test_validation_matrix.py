from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from modular_core import Candle

from tests.support import market_view
from wave_analysis import (
    PivotKind,
    PivotStatus,
    WaveAnalysisConfigurationError,
    WaveAnalysisModule,
    WaveAnalysisStateError,
)
from wave_analysis.analysis import classify_wave, extension_metrics, retracement_metrics
from wave_analysis.config import WaveAnalysisConfig, canonical_decimal, timeframe_seconds, to_decimal
from wave_analysis.enums import WaveClassification, WaveDirection
from wave_analysis.models import PivotPoint, TimeframeState, WaveLeg
from wave_analysis.pivots import _append_confirmed, _basis_down, _basis_up, _set_provisional, reversal_threshold

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def candle(index: int, price: float = 100.0) -> Candle:
    return Candle(
        NOW + timedelta(minutes=index),
        price,
        price + 1,
        price - 1,
        price,
        100,
        "BTCUSDT",
        "1m",
    )


def baseline_snapshot() -> tuple[WaveAnalysisModule, dict[str, object]]:
    module = WaveAnalysisModule(timeframes=("1m",), primary_timeframe="1m", left_bars=1, right_bars=1)
    history = [candle(i, 100 + i) for i in range(3)]
    for index in range(len(history)):
        module.on_bar(market_view(tuple(history[: index + 1]), {"1m": tuple(history[: index + 1])}, index))
    return module, copy.deepcopy(dict(module.snapshot_state()))


@pytest.mark.parametrize(
    "call",
    [
        lambda: to_decimal(True, "x"),
        lambda: to_decimal(object(), "x"),
        lambda: to_decimal("NaN", "x"),
        lambda: timeframe_seconds(1),
        lambda: timeframe_seconds("base"),
        lambda: WaveAnalysisConfig(timeframes=(1,), primary_timeframe="1m"),  # type: ignore[arg-type]
        lambda: WaveAnalysisConfig(timeframes="1m", primary_timeframe="1m"),  # type: ignore[arg-type]
        lambda: WaveAnalysisModule(timeframes="1m", primary_timeframe="1m"),  # type: ignore[arg-type]
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe=1),  # type: ignore[arg-type]
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", left_bars=0),
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", feature_snapshot_limit=0),
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", minimum_price_movement=Decimal("-1")),
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", atr_multiplier=Decimal("0")),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",), primary_timeframe="1m", impulse_efficiency_threshold=Decimal("2")
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",),
            primary_timeframe="1m",
            minimum_atr_threshold=Decimal("2"),
            maximum_atr_threshold=Decimal("1"),
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",), primary_timeframe="1m", left_bars=10, right_bars=10, maximum_retained_bars=16
        ),
        lambda: WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", publish_diagnostics=1),  # type: ignore[arg-type]
        lambda: WaveAnalysisConfig(
            timeframes=("1m",), primary_timeframe="1m", optional_market_regime_feature="invalid"
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",), primary_timeframe="1m", optional_market_regime_feature="market regime.current"
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",),
            primary_timeframe="1m",
            impulse_atr_threshold=Decimal("2"),
            expansion_atr_threshold=Decimal("1"),
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",),
            primary_timeframe="1m",
            range_efficiency_threshold=Decimal("0.4"),
            exhaustion_efficiency_threshold=Decimal("0.3"),
        ),
        lambda: WaveAnalysisConfig(
            timeframes=("1m",),
            primary_timeframe="1m",
            exhaustion_efficiency_threshold=Decimal("0.7"),
            impulse_efficiency_threshold=Decimal("0.6"),
        ),
    ],
)
def test_config_validation_matrix(call) -> None:
    with pytest.raises(WaveAnalysisConfigurationError):
        call()


def mutate_tf(snapshot: dict[str, object], **changes: object) -> dict[str, object]:
    result = copy.deepcopy(snapshot)
    timeframes = dict(result["timeframes"])
    item = dict(timeframes["1m"])
    item.update(changes)
    timeframes["1m"] = item
    result["timeframes"] = timeframes
    return result


@pytest.mark.parametrize(
    "mutator",
    [
        lambda s: {**s, "last_base_open_time": 1},
        lambda s: {**s, "last_base_open_time": "not-date"},
        lambda s: {**s, "last_base_open_time": "2026-01-01T00:00:00"},
        lambda s: {k: v for k, v in s.items() if k != "publications"},
        lambda s: {**s, "schema_version": "999"},
        lambda s: {**s, "config_signature": "bad"},
        lambda s: {**s, "bound_symbol": ""},
        lambda s: {**s, "last_base_fingerprint": ""},
        lambda s: {**s, "timeframes": []},
        lambda s: {**s, "timeframes": {}},
        lambda s: {**s, "last_base_fingerprint": None},
        lambda s: mutate_tf(s, timeframe="5m"),
        lambda s: mutate_tf(s, symbol="ETHUSDT"),
        lambda s: mutate_tf(s, processed_bars=0),
        lambda s: mutate_tf(s, last_fingerprint=None),
        lambda s: mutate_tf(s, last_fingerprint=""),
        lambda s: mutate_tf(s, last_open_time=(NOW + timedelta(days=1)).isoformat()),
        lambda s: mutate_tf(s, initial_high={"price": "100", "time": NOW.isoformat(), "index": -1}),
        lambda s: mutate_tf(s, initial_low={"price": "100", "time": NOW.isoformat(), "index": -1}),
        lambda s: mutate_tf(s, tr_seed=["1"] * 20),
        lambda s: mutate_tf(s, atr_values=["1"] * 200),
    ],
)
def test_state_corruption_matrix(mutator) -> None:
    module, snapshot = baseline_snapshot()
    with pytest.raises((WaveAnalysisStateError, ValueError, TypeError)):
        module.restore_state(mutator(snapshot))


def make_leg(index: int, direction: WaveDirection, start: str, end: str) -> WaveLeg:
    a, b = Decimal(start), Decimal(end)
    change = b - a
    return WaveLeg(
        f"w{index}",
        "BTCUSDT",
        "1m",
        direction,
        f"p{index}",
        f"p{index + 1}",
        NOW + timedelta(minutes=index),
        NOW + timedelta(minutes=index + 1),
        NOW + timedelta(minutes=index + 2),
        a,
        b,
        change,
        change / a,
        1,
        60,
        abs(change),
        Decimal("2"),
        Decimal("100"),
        Decimal("1"),
        Decimal("0.5"),
        Decimal("0.5"),
        Decimal("0"),
        abs(change),
        change,
        change / Decimal("60"),
        None,
        WaveClassification.IMPULSE,
        Decimal("0.8"),
        Decimal("0.8"),
        "confirmed",
        (),
        {},
        "cfg",
        {},
    )


@pytest.mark.parametrize(
    "expected,kwargs",
    [
        (
            WaveClassification.EXPANSION,
            dict(
                atr_size=Decimal("3"),
                path_efficiency=Decimal("0.8"),
                relative_volume=Decimal("2"),
                amplitude=Decimal("10"),
                previous_same=None,
                previous_leg=None,
            ),
        ),
        (
            WaveClassification.EXHAUSTION,
            dict(
                atr_size=Decimal("2"),
                path_efficiency=Decimal("0.2"),
                relative_volume=Decimal("1"),
                amplitude=Decimal("10"),
                previous_same=None,
                previous_leg=None,
            ),
        ),
        (
            WaveClassification.RANGE_LEG,
            dict(
                atr_size=Decimal("0.5"),
                path_efficiency=Decimal("0.1"),
                relative_volume=Decimal("0.5"),
                amplitude=Decimal("1"),
                previous_same=None,
                previous_leg=None,
            ),
        ),
        (
            WaveClassification.CONTRACTION,
            dict(
                atr_size=Decimal("1"),
                path_efficiency=Decimal("0.5"),
                relative_volume=Decimal("1"),
                amplitude=Decimal("5"),
                previous_same=make_leg(0, WaveDirection.UP, "100", "120"),
                previous_leg=None,
            ),
        ),
        (
            WaveClassification.IMPULSE,
            dict(
                atr_size=Decimal("2"),
                path_efficiency=Decimal("0.8"),
                relative_volume=Decimal("1.5"),
                amplitude=Decimal("8"),
                previous_same=None,
                previous_leg=None,
            ),
        ),
        (
            WaveClassification.CORRECTION,
            dict(
                atr_size=Decimal("1"),
                path_efficiency=Decimal("0.4"),
                relative_volume=Decimal("0.6"),
                amplitude=Decimal("8"),
                previous_same=None,
                previous_leg=make_leg(1, WaveDirection.UP, "100", "120"),
            ),
        ),
        (
            WaveClassification.UNCLASSIFIED,
            dict(
                atr_size=Decimal("0.8"),
                path_efficiency=Decimal("0.3"),
                relative_volume=Decimal("0.4"),
                amplitude=Decimal("19"),
                previous_same=None,
                previous_leg=make_leg(2, WaveDirection.UP, "100", "120"),
            ),
        ),
    ],
)
def test_classification_matrix(expected: WaveClassification, kwargs: dict[str, object]) -> None:
    config = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m")
    assert classify_wave(config=config, **kwargs)[0] is expected  # type: ignore[arg-type]


def test_metric_early_returns_and_pivot_helpers() -> None:
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT")
    assert retracement_metrics(state) is None
    assert extension_metrics(state) is None
    state.wave_legs = [make_leg(0, WaveDirection.UP, "100", "110")]
    assert extension_metrics(state) is None
    state.wave_legs.append(make_leg(1, WaveDirection.UP, "110", "120"))
    assert retracement_metrics(state) is None

    config = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", pivot_method="atr_zigzag", atr_period=2)
    assert reversal_threshold(state, config, Decimal("100")) is None
    bar = type("Bar", (), {"low": Decimal("90"), "high": Decimal("110"), "close": Decimal("100")})()
    assert _basis_down(bar, config) == Decimal("90")
    assert _basis_up(bar, config) == Decimal("110")


def test_provisional_and_confirmed_dedup_helpers() -> None:
    config = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", pivot_method="percentage_zigzag")
    state = TimeframeState(timeframe="1m", symbol="BTCUSDT")
    changed = _set_provisional(
        state,
        config,
        kind=PivotKind.HIGH,
        pivot_time=NOW,
        observed_at=NOW,
        price=Decimal("100"),
        bar_index=0,
        threshold=Decimal("1"),
    )
    assert changed is True
    assert (
        _set_provisional(
            state,
            config,
            kind=PivotKind.HIGH,
            pivot_time=NOW,
            observed_at=NOW,
            price=Decimal("100"),
            bar_index=0,
            threshold=Decimal("1"),
        )
        is False
    )
    pivot = PivotPoint(
        "p",
        "BTCUSDT",
        "1m",
        PivotKind.HIGH,
        NOW,
        NOW,
        Decimal("100"),
        0,
        PivotStatus.CONFIRMED,
        config_fingerprint=config.signature,
    )
    assert _append_confirmed(state, pivot, config) is True
    assert _append_confirmed(state, pivot, config) is False


def test_unknown_nested_state_field_is_rejected_as_state_error() -> None:
    module, snapshot = baseline_snapshot()
    corrupted = mutate_tf(snapshot, unexpected=1)
    with pytest.raises(WaveAnalysisStateError):
        module.restore_state(corrupted)


def test_invalid_dto_invariants_are_rejected() -> None:
    with pytest.raises(ValueError):
        PivotPoint(
            "p",
            "BTCUSDT",
            "1m",
            PivotKind.HIGH,
            NOW,
            NOW - timedelta(minutes=1),
            Decimal("100"),
            0,
            PivotStatus.CONFIRMED,
        )
    with pytest.raises(ValueError):
        PivotPoint(
            "p",
            "BTCUSDT",
            "1m",
            PivotKind.HIGH,
            NOW,
            NOW,
            Decimal("-1"),
            0,
            PivotStatus.CONFIRMED,
        )
    leg = make_leg(0, WaveDirection.UP, "100", "110")
    payload = dict(leg.to_mapping())
    payload["confidence"] = "2"
    with pytest.raises(ValueError):
        WaveLeg.from_mapping(payload)


def test_long_wave_marks_truncated_metrics_and_reduces_confidence() -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        maximum_retained_bars=16,
        maximum_retained_pivots=16,
        maximum_retained_waves=15,
    )
    prices = [100.0, 94.0]
    prices.extend(100.0 + index for index in range(1, 41))
    prices.extend([132.0, 128.0])
    history: list[Candle] = []
    for index, price in enumerate(prices):
        item = Candle(
            NOW + timedelta(minutes=index),
            price,
            price + 1,
            price - 1,
            price,
            100,
            "BTCUSDT",
            "1m",
        )
        history.append(item)
        module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))
    legs = module.snapshot_state()["timeframes"]["1m"]["wave_legs"]
    assert legs
    truncated = [item for item in legs if item["metadata"]["path_truncated"]]
    assert truncated
    assert all(Decimal(item["confidence"]) <= Decimal("0.5") for item in truncated)
    assert all("path_truncated_metrics" in item["reason_codes"] for item in truncated)


def test_decimal_signed_zero_has_one_canonical_configuration_identity() -> None:
    assert canonical_decimal(Decimal("-0.000")) == "0"
    positive = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", minimum_absolute_movement=Decimal("0"))
    negative = WaveAnalysisConfig(timeframes=("1m",), primary_timeframe="1m", minimum_absolute_movement=Decimal("-0"))
    assert positive.signature == negative.signature


def test_rc1_truncated_negative_excursions_migrate_to_zero() -> None:
    leg = make_leg(0, WaveDirection.UP, "100", "110")
    payload = dict(leg.to_mapping())
    payload["metadata"] = {"path_truncated": True, "segment_id": 0}
    payload["maximum_adverse_excursion"] = "-2"
    payload["maximum_favorable_excursion"] = "-1"

    restored = WaveLeg.from_mapping(payload)

    assert restored.maximum_adverse_excursion == 0
    assert restored.maximum_favorable_excursion == 0


def test_non_truncated_negative_excursion_remains_rejected() -> None:
    leg = make_leg(0, WaveDirection.UP, "100", "110")
    payload = dict(leg.to_mapping())
    payload["maximum_adverse_excursion"] = "-2"
    with pytest.raises(ValueError):
        WaveLeg.from_mapping(payload)
