from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import FakeCandle, bullish_triplet, candle
from modular_strategy_fvg import FvgConfig
from modular_strategy_fvg.models import FvgDirection, FvgFilterDecision, FvgPreStatus
from modular_strategy_fvg.pre_fvg import (
    ProvisionalCandle,
    _lower_bar_clock,
    _pre_filter_applies,
    _skipped,
    aggregate_provisional_c,
    evaluate_pre_fvg,
    make_pre_candidate_id,
)

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def _config(**overrides: object) -> FvgConfig:
    parameters: dict[str, object] = {
        "timeframes": ("15m",),
        "pre_fvg_enabled": True,
        "pre_fvg_evaluation_offset": "5m",
    }
    parameters.update(overrides)
    return FvgConfig.from_parameters(**parameters)


def _forming_bullish_rows() -> tuple[object, object]:
    return (
        candle(6, timeframe="5m", open_=106, high=110, low=105, close=107),
        candle(7, timeframe="5m", open_=107, high=112, low=106, close=111),
    )


def _aggregate() -> ProvisionalCandle:
    return aggregate_provisional_c(
        lower_bars=_forming_bullish_rows(),
        symbol="BTCUSDT",
        target_timeframe="15m",
        target_open_time=START + timedelta(minutes=30),
        cutoff_time=START + timedelta(minutes=40),
    )


def test_provisional_candle_and_aggregation_happy_path() -> None:
    provisional = _aggregate()
    assert provisional.timestamp == START + timedelta(minutes=30)
    assert provisional.close_time == START + timedelta(minutes=45)
    assert provisional.open == Decimal("106")
    assert provisional.high == Decimal("112")
    assert provisional.low == Decimal("105")
    assert provisional.close == Decimal("111")
    assert provisional.volume == Decimal("2")
    assert provisional.symbol == "BTCUSDT"
    assert provisional.timeframe == "15m"


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": 1},
        {"symbol": " BTCUSDT"},
        {"timeframe": ""},
        {"timeframe": "15M"},
        {"timestamp": START + timedelta(minutes=31)},
        {"close_time": START + timedelta(minutes=44)},
        {"open": 1},
        {"open": Decimal("NaN")},
        {"open": Decimal("0")},
        {"low": Decimal("120")},
        {"open": Decimal("120")},
        {"close": Decimal("120")},
        {"volume": 1},
        {"volume": Decimal("-1")},
    ],
)
def test_provisional_candle_rejects_invalid_contracts(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_aggregate(), **changes)


class _MissingClock:
    pass


class _BrokenClock:
    @property
    def timestamp(self) -> datetime:
        raise RuntimeError("broken")

    close_time = START


def test_lower_bar_clock_fail_closed() -> None:
    with pytest.raises(TypeError):
        _lower_bar_clock(_MissingClock(), "row")
    with pytest.raises(ValueError):
        _lower_bar_clock(_BrokenClock(), "row")


@pytest.mark.parametrize(
    "arguments",
    [
        {"lower_bars": "bad"},
        {"symbol": 1},
        {"symbol": " BTCUSDT"},
        {"target_timeframe": 15},
        {"target_timeframe": "15M"},
        {"target_open_time": START + timedelta(minutes=31)},
        {"cutoff_time": START + timedelta(minutes=30)},
        {"cutoff_time": START + timedelta(minutes=45)},
        {"lower_bars": ()},
    ],
)
def test_aggregate_rejects_invalid_boundaries(arguments: dict[str, object]) -> None:
    values: dict[str, object] = {
        "lower_bars": _forming_bullish_rows(),
        "symbol": "BTCUSDT",
        "target_timeframe": "15m",
        "target_open_time": START + timedelta(minutes=30),
        "cutoff_time": START + timedelta(minutes=40),
    }
    values.update(arguments)
    with pytest.raises((TypeError, ValueError)):
        aggregate_provisional_c(**values)  # type: ignore[arg-type]


def test_aggregate_rejects_incompatible_or_corrupt_lower_series() -> None:
    ten_minute = FakeCandle(
        START + timedelta(minutes=30),
        START + timedelta(minutes=40),
        112,
        105,
        111,
        timeframe="10m",
    )
    with pytest.raises(ValueError, match="resolution_incompatible"):
        aggregate_provisional_c(
            lower_bars=(ten_minute,),
            symbol="BTCUSDT",
            target_timeframe="15m",
            target_open_time=START + timedelta(minutes=30),
            cutoff_time=START + timedelta(minutes=40),
        )

    mismatch = replace(_forming_bullish_rows()[1], symbol="ETHUSDT")
    with pytest.raises(ValueError, match="series_contract_mismatch"):
        aggregate_provisional_c(
            lower_bars=(_forming_bullish_rows()[0], mismatch),
            symbol="BTCUSDT",
            target_timeframe="15m",
            target_open_time=START + timedelta(minutes=30),
            cutoff_time=START + timedelta(minutes=40),
        )

    with pytest.raises(ValueError, match="not_contiguous"):
        aggregate_provisional_c(
            lower_bars=(
                candle(6, timeframe="5m"),
                candle(8, timeframe="5m"),
            ),
            symbol="BTCUSDT",
            target_timeframe="30m",
            target_open_time=START + timedelta(minutes=30),
            cutoff_time=START + timedelta(minutes=50),
        )

    three_minute = FakeCandle(
        START + timedelta(minutes=30),
        START + timedelta(minutes=33),
        110,
        105,
        107,
        timeframe="3m",
    )
    with pytest.raises(ValueError, match="cannot_reach"):
        aggregate_provisional_c(
            lower_bars=(three_minute,),
            symbol="BTCUSDT",
            target_timeframe="15m",
            target_open_time=START + timedelta(minutes=30),
            cutoff_time=START + timedelta(minutes=40),
        )

    with pytest.raises(ValueError, match="open_unavailable"):
        aggregate_provisional_c(
            lower_bars=(
                FakeCandle(
                    START + timedelta(minutes=30),
                    START + timedelta(minutes=35),
                    110,
                    105,
                    107,
                    timeframe="5m",
                ),
                FakeCandle(
                    START + timedelta(minutes=35),
                    START + timedelta(minutes=40),
                    112,
                    106,
                    111,
                    timeframe="5m",
                ),
            ),
            symbol="BTCUSDT",
            target_timeframe="15m",
            target_open_time=START + timedelta(minutes=30),
            cutoff_time=START + timedelta(minutes=40),
        )


@dataclass(frozen=True)
class _OptionalVolumeCandle:
    timestamp: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    symbol: str = "BTCUSDT"
    timeframe: str = "5m"


def test_aggregate_allows_missing_volume() -> None:
    rows = (
        _OptionalVolumeCandle(
            START + timedelta(minutes=30),
            START + timedelta(minutes=35),
            Decimal("106"),
            Decimal("110"),
            Decimal("105"),
            Decimal("107"),
            None,
        ),
        _OptionalVolumeCandle(
            START + timedelta(minutes=35),
            START + timedelta(minutes=40),
            Decimal("107"),
            Decimal("112"),
            Decimal("106"),
            Decimal("111"),
            Decimal("1"),
        ),
    )
    result = aggregate_provisional_c(
        lower_bars=rows,
        symbol="BTCUSDT",
        target_timeframe="15m",
        target_open_time=START + timedelta(minutes=30),
        cutoff_time=START + timedelta(minutes=40),
    )
    assert result.volume is None


def test_pre_candidate_id_is_canonical_and_deterministic() -> None:
    arguments = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "source_c_open_time": START + timedelta(minutes=30),
        "evaluated_at": START + timedelta(minutes=40),
        "lower": Decimal("100"),
        "upper": Decimal("105"),
    }
    enum_id = make_pre_candidate_id(direction=FvgDirection.BULLISH, **arguments)
    text_id = make_pre_candidate_id(direction="bullish", **arguments)
    assert enum_id == text_id
    assert enum_id.startswith("pre-fvg:")


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": 1},
        {"symbol": " BTCUSDT"},
        {"timeframe": 15},
        {"timeframe": "15M"},
        {"direction": object()},
        {"direction": "sideways"},
        {"lower": 1},
        {"lower": Decimal("NaN")},
        {"lower": Decimal("0")},
        {"upper": Decimal("99")},
        {"source_c_open_time": START + timedelta(minutes=31)},
        {"evaluated_at": START + timedelta(minutes=30)},
        {"evaluated_at": START + timedelta(minutes=45)},
    ],
)
def test_pre_candidate_id_rejects_invalid_contracts(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "direction": FvgDirection.BULLISH,
        "source_c_open_time": START + timedelta(minutes=30),
        "evaluated_at": START + timedelta(minutes=40),
        "lower": Decimal("100"),
        "upper": Decimal("105"),
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        make_pre_candidate_id(**values)  # type: ignore[arg-type]


def test_pre_filter_target_helpers() -> None:
    assert _pre_filter_applies(_config().price_filter_target)
    confirmed = _config(price_filter_target="confirmed").price_filter_target
    assert not _pre_filter_applies(confirmed)
    trace = _skipped("price", confirmed)
    assert trace.decision is FvgFilterDecision.SKIPPED
    assert trace.metadata["event_target"] == "pre_fvg"


def test_evaluate_pre_fvg_disabled_and_boundary_contracts() -> None:
    a, b, _ = bullish_triplet()
    disabled = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(),
        observed_at=b.close_time,
        config=FvgConfig(timeframes=("15m",)),
    )
    assert disabled.status is FvgPreStatus.REJECTED
    assert disabled.reason == "pre_fvg_disabled"

    with pytest.raises(TypeError):
        evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars=(),
            observed_at=b.close_time,
            config=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars="bad",  # type: ignore[arg-type]
            observed_at=b.close_time,
            config=_config(),
        )
    with pytest.raises(ValueError, match="share symbol"):
        evaluate_pre_fvg(
            a=a,
            b=replace(b, symbol="ETHUSDT"),
            lower_bars=(),
            observed_at=b.close_time,
            config=_config(),
        )
    with pytest.raises(ValueError, match="closed by observed_at"):
        evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars=(),
            observed_at=b.timestamp,
            config=_config(),
        )
    with pytest.raises(ValueError, match="not configured"):
        evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars=(),
            observed_at=b.close_time,
            config=_config(timeframes=("4h",)),
        )


def test_evaluate_pre_fvg_timing_and_resolution_statuses() -> None:
    a, b, _ = bullish_triplet()
    nonconsecutive = replace(b, timestamp=b.timestamp + timedelta(minutes=15))
    result = evaluate_pre_fvg(
        a=a,
        b=nonconsecutive,
        lower_bars=(),
        observed_at=nonconsecutive.close_time,
        config=_config(),
    )
    assert result.reason == "pre_fvg_ab_not_consecutive"

    closed = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(),
        observed_at=START + timedelta(minutes=45),
        config=_config(),
    )
    assert closed.reason == "pre_fvg_window_already_closed"

    not_due = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(),
        observed_at=START + timedelta(minutes=35),
        config=_config(),
    )
    assert not_due.status is FvgPreStatus.NOT_DUE

    incompatible = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(
            FakeCandle(
                START + timedelta(minutes=30),
                START + timedelta(minutes=40),
                112,
                105,
                111,
                timeframe="10m",
            ),
        ),
        observed_at=START + timedelta(minutes=40),
        config=_config(),
    )
    assert incompatible.reason == "lower_timeframe_resolution_incompatible"

    rows_3m = tuple(
        FakeCandle(
            START + timedelta(minutes=30 + offset),
            START + timedelta(minutes=33 + offset),
            112,
            105,
            111,
            timeframe="3m",
        )
        for offset in (0, 3, 6)
    )
    unreachable = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=rows_3m,
        observed_at=START + timedelta(minutes=40),
        config=_config(),
    )
    assert unreachable.reason == "lower_timeframe_resolution_cannot_reach_evaluation_offset"


def test_evaluate_pre_fvg_geometry_filters_and_candidate() -> None:
    a, b, _ = bullish_triplet()
    unavailable = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=(),
        observed_at=START + timedelta(minutes=40),
        config=_config(),
    )
    assert unavailable.reason == "lower_timeframe_history_unavailable"

    no_gap_rows = (
        candle(6, timeframe="5m", open_=98, high=100, low=95, close=99),
        candle(7, timeframe="5m", open_=99, high=101, low=96, close=100),
    )
    no_gap = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=no_gap_rows,
        observed_at=START + timedelta(minutes=40),
        config=_config(),
    )
    assert no_gap.reason == "pre_fvg_geometry_not_present_at_cutoff"

    filtered = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=_forming_bullish_rows(),
        observed_at=START + timedelta(minutes=40),
        config=_config(min_gap_absolute="100"),
    )
    assert filtered.status is FvgPreStatus.REJECTED

    metric_missing = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=_forming_bullish_rows(),
        observed_at=START + timedelta(minutes=40),
        config=_config(gap_size_mode="atr", min_gap_size="0", atr_period=14),
    )
    assert metric_missing.status is FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA

    candidate = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=_forming_bullish_rows(),
        observed_at=START + timedelta(minutes=40),
        config=_config(),
    )
    assert candidate.status is FvgPreStatus.CANDIDATE
    assert candidate.candidate is not None
    assert candidate.candidate.lower == Decimal("100")
    assert candidate.candidate.upper == Decimal("105")

    skipped = evaluate_pre_fvg(
        a=a,
        b=b,
        lower_bars=_forming_bullish_rows(),
        observed_at=START + timedelta(minutes=40),
        config=_config(
            price_filter_mode="absolute",
            price_filter_min="1",
            price_filter_target="confirmed",
            volume_filter_mode="b_to_sma",
            volume_lookback=1,
            volume_filter_target="confirmed",
        ),
    )
    assert skipped.status is FvgPreStatus.CANDIDATE
    assert skipped.filters[1].decision is FvgFilterDecision.SKIPPED
    assert skipped.filters[2].decision is FvgFilterDecision.SKIPPED

    with pytest.raises(ValueError, match="reference_price"):
        evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars=_forming_bullish_rows(),
            observed_at=START + timedelta(minutes=40),
            config=_config(),
            reference_price=Decimal("0"),
        )
