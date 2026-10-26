from __future__ import annotations

from datetime import timedelta

import pytest

from modular_core import Candle
from modular_core.timeframes import (
    TimeframeAggregator,
    TimeframeSpec,
    TimeframeStore,
)


@pytest.mark.parametrize("value", [True, 15, None, object()])
def test_timeframe_spec_requires_string_output_identity(value: object) -> None:
    with pytest.raises(TypeError, match="output_timeframe must be str"):
        TimeframeSpec(3, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", " ", " 15m", "15m "])
def test_timeframe_spec_requires_canonical_output_identity(value: str) -> None:
    with pytest.raises(ValueError):
        TimeframeSpec(3, value)


@pytest.mark.parametrize("value", [True, 1, None, object()])
def test_timeframe_spec_requires_string_timezone(value: object) -> None:
    with pytest.raises(TypeError, match="alignment_timezone must be str"):
        TimeframeSpec(3, "15m", alignment_timezone=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", " ", " UTC", "UTC "])
def test_timeframe_spec_requires_canonical_timezone(value: str) -> None:
    with pytest.raises(ValueError):
        TimeframeSpec(3, "15m", alignment_timezone=value)


def test_timeframe_spec_preserves_supported_custom_names() -> None:
    spec = TimeframeSpec(1, "5m-copy", alignment_timezone="UTC")
    assert spec.output_timeframe == "5m-copy"
    assert spec.alignment_timezone == "UTC"


@pytest.mark.parametrize("value", [True, 15, None, object()])
def test_aggregator_requires_string_output_identity(value: object) -> None:
    with pytest.raises(TypeError, match="output_timeframe must be str"):
        TimeframeAggregator(3, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", " ", " 15m", "15m "])
def test_aggregator_requires_canonical_output_identity(value: str) -> None:
    with pytest.raises(ValueError):
        TimeframeAggregator(3, value)


def test_aggregator_preserves_supported_custom_names() -> None:
    aggregator = TimeframeAggregator(1, "1d-ny", alignment_timezone="UTC")
    assert aggregator.output_timeframe == "1d-ny"
    assert aggregator.alignment_timezone == "UTC"


@pytest.mark.parametrize("value", [None, 1, object(), "specs"])
def test_timeframe_store_requires_spec_iterable(value: object) -> None:
    with pytest.raises(TypeError, match="timeframe specs"):
        TimeframeStore(
            value,  # type: ignore[arg-type]
            expected_base_interval=timedelta(minutes=5),
        )


def test_empty_timeframe_store_still_validates_push_boundary() -> None:
    store = TimeframeStore((), expected_base_interval=timedelta(minutes=5))
    with pytest.raises(TypeError, match="timeframe store requires Candle"):
        store.push(object())  # type: ignore[arg-type]


def test_timeframe_store_accepts_one_shot_spec_iterable() -> None:
    specs = iter((TimeframeSpec(1, "5m-copy"),))
    store = TimeframeStore(
        specs,
        expected_base_interval=timedelta(minutes=5),
    )
    assert tuple(store.snapshot()) == ("5m-copy",)


def test_timeframe_store_pushes_valid_candle_with_custom_output_name(
    bars_factory,
) -> None:
    store = TimeframeStore(
        (TimeframeSpec(1, "5m-copy"),),
        expected_base_interval=timedelta(minutes=5),
    )
    assert store.push(bars_factory([100])[0]) is False
    output = store.snapshot()["5m-copy"][-1]
    assert isinstance(output, Candle)
    assert output.timeframe == "5m-copy"
