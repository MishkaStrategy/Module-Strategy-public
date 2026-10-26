from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from modular_core import Candle

from tests.support import market_view
from wave_analysis import WaveAnalysisModule, WaveAnalysisStateError

START = datetime(2026, 6, 1, tzinfo=UTC)


def rich_snapshot() -> tuple[WaveAnalysisModule, dict[str, object]]:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
        elliott_enabled=True,
        hypothesis_min_confidence="0",
    )
    history: list[Candle] = []
    prices = (100, 90, 110, 95, 120, 100, 130, 105, 140, 110, 150, 115, 160)
    for index, price in enumerate(prices):
        item = Candle(
            START + timedelta(minutes=index),
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
    return module, copy.deepcopy(dict(module.snapshot_state()))


def mutate_timeframe(snapshot: dict[str, object], mutate: Callable[[dict[str, object]], None]) -> dict[str, object]:
    result = copy.deepcopy(snapshot)
    timeframes = dict(result["timeframes"])
    item = dict(timeframes["1m"])
    mutate(item)
    timeframes["1m"] = item
    result["timeframes"] = timeframes
    return result


def set_value(key: str, value: object) -> Callable[[dict[str, object]], None]:
    return lambda item: item.__setitem__(key, value)


def corrupt_bar_duration(item: dict[str, object]) -> None:
    bars = list(item["bars"])
    first = dict(bars[0])
    first["close_time"] = first["timestamp"]
    bars[0] = first
    item["bars"] = bars


def reverse_bars(item: dict[str, object]) -> None:
    item["bars"] = list(reversed(item["bars"]))


def exceed_bar_capacity(item: dict[str, object]) -> None:
    bars = list(item["bars"])
    item["bars"] = (bars * ((4097 + len(bars) - 1) // len(bars)))[:4097]


def exceed_pivot_capacity(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    item["confirmed_pivots"] = (pivots * ((257 + len(pivots) - 1) // len(pivots)))[:257]


def exceed_wave_capacity(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    item["wave_legs"] = (waves * ((256 + len(waves) - 1) // len(waves)))[:256]


def exceed_hypothesis_capacity(item: dict[str, object]) -> None:
    hypotheses = list(item["hypotheses"])
    item["hypotheses"] = (hypotheses * ((6 + len(hypotheses) - 1) // len(hypotheses)))[:6]


def duplicate_pivot(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    pivots.insert(1, copy.deepcopy(pivots[0]))
    item["confirmed_pivots"] = pivots


def future_pivot_segment(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    metadata = dict(first["metadata"])
    metadata["segment_id"] = 1
    first["metadata"] = metadata
    pivots[0] = first
    item["confirmed_pivots"] = pivots


def pivot_beyond_history(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    first["bar_index"] = item["processed_bars"]
    pivots[0] = first
    item["confirmed_pivots"] = pivots


def wrong_pivot_identity(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    first["symbol"] = "ETHUSDT"
    pivots[0] = first
    item["confirmed_pivots"] = pivots


def wrong_pivot_fingerprint(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    first["config_fingerprint"] = "wrong"
    pivots[0] = first
    item["confirmed_pivots"] = pivots


def unordered_pivots(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    second = dict(pivots[1])
    second["bar_index"] = first["bar_index"]
    pivots[1] = second
    item["confirmed_pivots"] = pivots


def decreasing_pivot_confirmation(item: dict[str, object]) -> None:
    pivots = list(item["confirmed_pivots"])
    first = dict(pivots[0])
    first["confirmed_at"] = item["last_open_time"]
    pivots[0] = first
    item["confirmed_pivots"] = pivots


def duplicate_wave(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    waves.insert(1, copy.deepcopy(waves[0]))
    item["wave_legs"] = waves


def disconnected_wave(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    second = dict(waves[1])
    second["start_pivot_id"] = "not-the-previous-end"
    waves[1] = second
    item["wave_legs"] = waves


def wrong_wave_identity(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    first = dict(waves[0])
    first["timeframe"] = "5m"
    waves[0] = first
    item["wave_legs"] = waves


def wrong_wave_fingerprint(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    first = dict(waves[0])
    first["config_fingerprint"] = "wrong"
    waves[0] = first
    item["wave_legs"] = waves


def future_wave_segment(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    first = dict(waves[0])
    metadata = dict(first["metadata"])
    metadata["segment_id"] = 1
    first["metadata"] = metadata
    waves[0] = first
    item["wave_legs"] = waves


def decreasing_wave_confirmation(item: dict[str, object]) -> None:
    waves = list(item["wave_legs"])
    first = dict(waves[0])
    first["confirmed_at"] = item["last_open_time"]
    waves[0] = first
    item["wave_legs"] = waves


def wrong_provisional_identity(item: dict[str, object]) -> None:
    provisional = dict(item["provisional"])
    provisional["symbol"] = "ETHUSDT"
    item["provisional"] = provisional


def wrong_provisional_fingerprint(item: dict[str, object]) -> None:
    provisional = dict(item["provisional"])
    provisional["config_fingerprint"] = "wrong"
    item["provisional"] = provisional


def provisional_beyond_history(item: dict[str, object]) -> None:
    provisional = dict(item["provisional"])
    provisional["bar_index"] = item["processed_bars"]
    item["provisional"] = provisional


def wrong_structure_segment(item: dict[str, object]) -> None:
    structure = dict(item["structure"])
    structure["segment_id"] = 1
    item["structure"] = structure


def wrong_hypothesis_rank(item: dict[str, object]) -> None:
    hypotheses = list(item["hypotheses"])
    first = dict(hypotheses[0])
    first["alternative_rank"] = 2
    hypotheses[0] = first
    item["hypotheses"] = hypotheses


def wrong_hypothesis_segment(item: dict[str, object]) -> None:
    hypotheses = list(item["hypotheses"])
    first = dict(hypotheses[0])
    metadata = dict(first["metadata"])
    metadata["segment_id"] = 1
    first["metadata"] = metadata
    hypotheses[0] = first
    item["hypotheses"] = hypotheses


def duplicate_hypothesis_id(item: dict[str, object]) -> None:
    hypotheses = list(item["hypotheses"])
    if len(hypotheses) == 1:
        second = copy.deepcopy(dict(hypotheses[0]))
        second["alternative_rank"] = 2
        hypotheses.append(second)
    else:
        second = dict(hypotheses[1])
        second["hypothesis_id"] = dict(hypotheses[0])["hypothesis_id"]
        hypotheses[1] = second
    item["hypotheses"] = hypotheses


def wrong_hypothesis_degree(item: dict[str, object]) -> None:
    hypotheses = list(item["hypotheses"])
    first = dict(hypotheses[0])
    first["degree"] = "5m"
    hypotheses[0] = first
    item["hypotheses"] = hypotheses


@pytest.mark.parametrize(
    "mutation",
    [
        set_value("timeframe", "5m"),
        set_value("publications", -1),
        set_value("symbol", "ETHUSDT"),
        set_value("bar_offset", 1),
        set_value("gap_events", 1),
        lambda item: (item.__setitem__("gap_blocked", True), item.__setitem__("lifecycle", "warming_up")),
        lambda item: (
            item.__setitem__("gap_blocked", True),
            item.__setitem__("lifecycle", "gap_blocked"),
            item.__setitem__("recovery_count", 17),
        ),
        set_value("published_feature_ids", {"not_namespaced": ""}),
        corrupt_bar_duration,
        reverse_bars,
        exceed_bar_capacity,
        exceed_pivot_capacity,
        exceed_wave_capacity,
        exceed_hypothesis_capacity,
        set_value("atr", "NaN"),
        set_value("previous_close", "0"),
        duplicate_pivot,
        future_pivot_segment,
        pivot_beyond_history,
        wrong_pivot_identity,
        wrong_pivot_fingerprint,
        unordered_pivots,
        decreasing_pivot_confirmation,
        duplicate_wave,
        disconnected_wave,
        wrong_wave_identity,
        wrong_wave_fingerprint,
        future_wave_segment,
        decreasing_wave_confirmation,
        wrong_provisional_identity,
        wrong_provisional_fingerprint,
        provisional_beyond_history,
        wrong_structure_segment,
        wrong_hypothesis_rank,
        wrong_hypothesis_segment,
        duplicate_hypothesis_id,
        wrong_hypothesis_degree,
    ],
)
def test_checkpoint_semantic_invariants_are_fail_closed(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    module, snapshot = rich_snapshot()
    corrupted = mutate_timeframe(snapshot, mutation)
    with pytest.raises(WaveAnalysisStateError):
        module.restore_state(corrupted)


def test_partial_base_event_identity_is_fail_closed() -> None:
    module, snapshot = rich_snapshot()
    snapshot["last_base_fingerprint"] = None
    with pytest.raises(WaveAnalysisStateError, match="base event identity is partially saved"):
        module.restore_state(snapshot)
