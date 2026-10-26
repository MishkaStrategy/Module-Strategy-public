from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wave_analysis.enums import (
    HypothesisPattern,
    HypothesisStatus,
    PivotKind,
    PivotStatus,
    StructureState,
    SwingLabel,
    WaveClassification,
    WaveDirection,
)
from wave_analysis.models import (
    BarRecord,
    PivotPoint,
    ProvisionalPivot,
    StructureSnapshot,
    WaveHypothesis,
    WaveLeg,
)

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def valid_bar() -> BarRecord:
    return BarRecord(
        NOW, NOW + timedelta(minutes=1), Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"), Decimal("1")
    )


def valid_pivot() -> PivotPoint:
    return PivotPoint(
        "p1",
        "BTCUSDT",
        "1m",
        PivotKind.HIGH,
        NOW,
        NOW + timedelta(minutes=1),
        Decimal("102"),
        0,
        PivotStatus.CONFIRMED,
        0,
        (),
        "cfg",
        {"segment_id": 0},
    )


def valid_provisional() -> ProvisionalPivot:
    return ProvisionalPivot(
        "p1",
        "r1",
        "BTCUSDT",
        "1m",
        PivotKind.HIGH,
        NOW,
        NOW + timedelta(minutes=1),
        Decimal("102"),
        0,
        1,
        Decimal("1"),
        PivotStatus.PROVISIONAL,
        "cfg",
    )


def valid_leg() -> WaveLeg:
    return WaveLeg(
        "w1",
        "BTCUSDT",
        "1m",
        WaveDirection.UP,
        "p1",
        "p2",
        NOW,
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=2),
        Decimal("100"),
        Decimal("110"),
        Decimal("10"),
        Decimal("0.1"),
        1,
        60,
        Decimal("11"),
        Decimal("2"),
        Decimal("100"),
        Decimal("1"),
        Decimal("0.8"),
        Decimal("0.8"),
        Decimal("1"),
        Decimal("10"),
        Decimal("10"),
        Decimal("0.1666666667"),
        None,
        WaveClassification.IMPULSE,
        Decimal("0.8"),
        Decimal("0.8"),
        "confirmed",
        (),
        {},
        "cfg",
        {"segment_id": 0},
    )


def valid_structure() -> StructureSnapshot:
    return StructureSnapshot(
        "1m",
        StructureState.BULLISH,
        SwingLabel.HIGHER_HIGH,
        StructureState.BULLISH,
        StructureState.BULLISH,
        True,
        True,
        False,
        Decimal("99"),
        Decimal("0.8"),
        (),
        NOW + timedelta(minutes=2),
        0,
    )


def valid_hypothesis() -> WaveHypothesis:
    return WaveHypothesis(
        "h1",
        HypothesisPattern.IMPULSE_12345,
        WaveDirection.UP,
        "local",
        {"1": "w1"},
        NOW,
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=2),
        Decimal("0.8"),
        Decimal("0.8"),
        Decimal("0.7"),
        Decimal("95"),
        1,
        HypothesisStatus.ACTIVE,
        (),
        ("w1",),
        {"segment_id": 0},
    )


@pytest.mark.parametrize(
    ("factory", "mutate"),
    [
        (valid_bar, lambda m: m.update(close_time=(NOW - timedelta(minutes=1)).isoformat())),
        (valid_bar, lambda m: m.update(open="0")),
        (valid_bar, lambda m: m.update(volume="-1")),
        (valid_bar, lambda m: m.update(high="98")),
        (valid_pivot, lambda m: m.update(confirmed_at=(NOW - timedelta(minutes=1)).isoformat())),
        (valid_pivot, lambda m: m.update(price="0")),
        (valid_pivot, lambda m: m.update(status=PivotStatus.PROVISIONAL.value)),
        (valid_provisional, lambda m: m.update(observed_at=(NOW - timedelta(minutes=1)).isoformat())),
        (valid_provisional, lambda m: m.update(revision=0)),
        (valid_provisional, lambda m: m.update(status=PivotStatus.CONFIRMED.value)),
        (valid_leg, lambda m: m.update(end_time=(NOW - timedelta(minutes=1)).isoformat())),
        (valid_leg, lambda m: m.update(duration_bars=0)),
        (valid_leg, lambda m: m.update(price_change="9")),
        (valid_leg, lambda m: m.update(price_change="-10")),
        (valid_leg, lambda m: m.update(direction=WaveDirection.DOWN.value, price_change="10")),
        (valid_leg, lambda m: m.update(confidence="2")),
        (valid_leg, lambda m: m.update(status="provisional")),
        (valid_structure, lambda m: m.update(confidence="2")),
        (valid_structure, lambda m: m.update(invalidation_price="0")),
        (valid_structure, lambda m: m.update(segment_id=-1)),
        (valid_hypothesis, lambda m: m.update(end_time=(NOW - timedelta(minutes=1)).isoformat())),
        (valid_hypothesis, lambda m: m.update(rule_score="2")),
        (valid_hypothesis, lambda m: m.update(invalidation_price="0")),
    ],
)
def test_serialized_dtos_fail_closed(
    factory: Callable[[], object], mutate: Callable[[dict[str, object]], None]
) -> None:
    value = factory()
    mapping = deepcopy(dict(value.to_mapping()))  # type: ignore[attr-defined]
    mutate(mapping)
    with pytest.raises(ValueError):
        type(value).from_mapping(mapping)


@pytest.mark.parametrize(
    ("factory", "field", "bad_value"),
    [
        (valid_pivot, "metadata", []),
        (valid_pivot, "reason_codes", "not-a-sequence"),
        (valid_pivot, "bar_index", True),
        (valid_structure, "break_detected", 1),
        (valid_hypothesis, "wave_labels", []),
        (valid_hypothesis, "supporting_wave_ids", "w1"),
    ],
)
def test_serialized_dto_shape_helpers_reject_ambiguous_types(
    factory: Callable[[], object], field: str, bad_value: object
) -> None:
    value = factory()
    mapping = deepcopy(dict(value.to_mapping()))  # type: ignore[attr-defined]
    mapping[field] = bad_value
    with pytest.raises(ValueError):
        type(value).from_mapping(mapping)
