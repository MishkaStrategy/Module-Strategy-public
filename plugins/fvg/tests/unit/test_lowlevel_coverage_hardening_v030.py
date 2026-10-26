from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, getcontext
from types import SimpleNamespace

import pytest

from conftest import START, bullish_triplet
from modular_strategy_fvg import FvgConfig
from modular_strategy_fvg.analysis import calculate_metrics
from modular_strategy_fvg.decimal_utils import (
    _has_fvg_arithmetic_settings,
    canonical_decimal_text,
    deterministic_decimal,
    parse_canonical_decimal_text,
)
from modular_strategy_fvg.detector import (
    _decimal_identity,
    _optional_candle_attribute,
    _required_candle_attribute,
    _validate_candle,
    _validate_candle_clock,
    as_decimal,
    candle_fingerprint,
    detect_zone,
    make_zone_id,
    validate_history_bars,
)
from modular_strategy_fvg.evaluator import _status_for, _target_skipped, evaluate_fvg_window
from modular_strategy_fvg.models import (
    FvgDirection,
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgFilterTrace,
    FvgZone,
)
from modular_strategy_fvg.models_base import (
    _finite_float,
    _require_canonical_mapping_keys,
    _require_canonical_symbol,
)
from modular_strategy_fvg.timeframes import (
    as_utc,
    canonical_timeframe,
    is_period_aligned,
    parse_canonical_utc_datetime,
    period_close,
    period_open,
    timeframe_delta,
    timeframe_seconds,
)

UTC = timezone.utc


class _ClockBar:
    def __init__(
        self,
        *,
        timestamp: datetime = START,
        close_time: datetime | None = None,
        symbol: object = "BTCUSDT",
        timeframe: object = "15m",
        open_: object = 95,
        high: object = 100,
        low: object = 90,
        close: object = 95,
        volume: object = 1,
    ) -> None:
        self.timestamp = timestamp
        self.close_time = close_time if close_time is not None else timestamp + timedelta(minutes=15)
        self.symbol = symbol
        self.timeframe = timeframe
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class _Explodes:
    @property
    def bad(self):
        raise RuntimeError("boom")


class _BadTz(tzinfo):
    def utcoffset(self, dt):
        raise OverflowError("bad offset")

    def dst(self, dt):
        return None


@pytest.mark.parametrize("value", [None, object(), [], True])
def test_as_decimal_rejects_noncanonical_types(value: object) -> None:
    with pytest.raises(TypeError):
        as_decimal(value)  # type: ignore[arg-type]


def test_decimal_identity_and_invalid_values() -> None:
    assert as_decimal("1.25") == Decimal("1.25")
    assert _decimal_identity(1) == "i:1"
    assert _decimal_identity(1.5).startswith("f:")
    assert _decimal_identity(Decimal("1.20")) == "d:1.2"
    with pytest.raises(ValueError):
        as_decimal("bad")
    with pytest.raises(ValueError):
        as_decimal(Decimal("NaN"))
    with pytest.raises(ValueError):
        _decimal_identity(float("inf"))


def test_attribute_boundaries() -> None:
    with pytest.raises(TypeError, match="must provide"):
        _required_candle_attribute(object(), "x", "missing")
    assert _optional_candle_attribute(object(), "x", "missing") is None
    with pytest.raises(ValueError, match="cannot be read"):
        _required_candle_attribute(_Explodes(), "x", "bad")
    with pytest.raises(ValueError, match="cannot be read"):
        _optional_candle_attribute(_Explodes(), "x", "bad")


@pytest.mark.parametrize(
    ("bar", "message"),
    [
        (_ClockBar(symbol=1), "canonical strings"),
        (_ClockBar(timeframe=""), "timeframe is required"),
        (_ClockBar(timeframe="15M"), "timeframe must be canonical"),
        (_ClockBar(close_time=START + timedelta(minutes=5)), "duration"),
        (_ClockBar(timestamp=START + timedelta(minutes=1), close_time=START + timedelta(minutes=16)), "aligned"),
        (_ClockBar(close_time=START), "cannot precede"),
    ],
)
def test_validate_candle_clock_fail_closed(bar: _ClockBar, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _validate_candle_clock(bar, "x")


@pytest.mark.parametrize(
    ("bar", "message"),
    [
        (_ClockBar(high=float("inf")), "finite"),
        (_ClockBar(low=0), "positive"),
        (_ClockBar(low=101), "geometry"),
        (_ClockBar(close=101), "geometry"),
        (_ClockBar(open_=101), "outside"),
        (_ClockBar(volume=-1), "non-negative"),
        (_ClockBar(high=Decimal("NaN")), "finite"),
    ],
)
def test_validate_candle_market_values(bar: _ClockBar, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_candle(bar, "x")


def test_history_validation_all_ordering_failures() -> None:
    a, b, c = bullish_triplet()
    with pytest.raises(TypeError):
        validate_history_bars("bad", symbol="BTCUSDT", timeframe="15m", before=a.timestamp, require_consecutive=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope"):
        validate_history_bars(
            (replace(a, symbol="ETHUSDT"),),
            symbol="BTCUSDT",
            timeframe="15m",
            before=a.close_time,
            require_consecutive=False,
        )
    with pytest.raises(ValueError, match="close before A"):
        validate_history_bars((a,), symbol="BTCUSDT", timeframe="15m", before=a.timestamp, require_consecutive=False)
    overlap = replace(b, timestamp=a.timestamp)
    with pytest.raises(ValueError, match="strictly ordered"):
        validate_history_bars(
            (a, overlap), symbol="BTCUSDT", timeframe="15m", before=c.timestamp, require_consecutive=False
        )
    gap = replace(b, timestamp=a.close_time + timedelta(minutes=15))
    with pytest.raises(ValueError, match="consecutive"):
        validate_history_bars(
            (a, gap),
            symbol="BTCUSDT",
            timeframe="15m",
            before=c.timestamp + timedelta(minutes=15),
            require_consecutive=True,
        )
    with pytest.raises(ValueError, match="tail"):
        validate_history_bars((a,), symbol="BTCUSDT", timeframe="15m", before=c.timestamp, require_consecutive=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbol": 1},
        {"timeframe": ""},
        {"timeframe": "15M"},
        {"direction": "bullish"},
        {"lower": 1},
        {"lower": Decimal("NaN")},
        {"lower": Decimal("0")},
        {"lower": Decimal("2"), "upper": Decimal("1")},
        {"source_c_open_time": START + timedelta(minutes=1)},
    ],
)
def test_make_zone_id_validation(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "direction": FvgDirection.BULLISH,
        "source_c_open_time": START + timedelta(minutes=30),
        "lower": Decimal("100"),
        "upper": Decimal("105"),
    }
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        make_zone_id(**values)  # type: ignore[arg-type]


def test_fingerprint_and_detect_zone_validation() -> None:
    a, b, c = bullish_triplet()
    assert len(candle_fingerprint(a)) == 64
    with pytest.raises(ValueError, match="override"):
        candle_fingerprint(a, a.close_time + timedelta(seconds=1))
    with pytest.raises(TypeError, match="config"):
        detect_zone(a, b, c, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same symbol"):
        detect_zone(a, replace(b, symbol="ETHUSDT"), c, FvgConfig(timeframes=("15m",)))
    with pytest.raises(ValueError, match="same timeframe"):
        detect_zone(a, replace(b, timeframe="5m"), c, FvgConfig(timeframes=("15m",)))
    with pytest.raises(ValueError, match="temporally ordered"):
        detect_zone(a, replace(b, timestamp=c.timestamp), c, FvgConfig(timeframes=("15m",)))
    with pytest.raises(ValueError, match="not configured"):
        detect_zone(a, b, c, FvgConfig(timeframes=("4h",)))


def test_timeframe_and_utc_boundaries() -> None:
    with pytest.raises(TypeError):
        canonical_timeframe(15)  # type: ignore[arg-type]
    for value in ("", "0m", "m15", "01m"):
        with pytest.raises(ValueError):
            canonical_timeframe(value)
    with pytest.raises(ValueError, match="exceeds"):
        timeframe_seconds("999999999999999999999999999d")
    assert timeframe_seconds("2h") == 7200
    assert timeframe_delta("2m") == timedelta(minutes=2)
    with pytest.raises(TypeError):
        as_utc("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        as_utc(datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="represented"):
        as_utc(datetime(2024, 1, 1, tzinfo=_BadTz()))
    with pytest.raises(TypeError):
        parse_canonical_utc_datetime(1)
    for value in ("bad", "2024-01-01T00:00:00", "2024-01-01T00:00:00Z", "2024-01-01 00:00:00+00:00"):
        with pytest.raises(ValueError):
            parse_canonical_utc_datetime(value)
    assert period_open(START + timedelta(minutes=16), "15m") == START + timedelta(minutes=15)
    assert period_close(START, "15m") == START + timedelta(minutes=15)
    assert is_period_aligned(START, "15m")
    assert not is_period_aligned(START + timedelta(minutes=1), "15m")
    monday = datetime(2024, 1, 8, tzinfo=UTC)
    assert period_open(monday + timedelta(days=2), "1w") == monday


def test_decimal_text_and_context_boundaries() -> None:
    with pytest.raises(TypeError):
        canonical_decimal_text(1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        canonical_decimal_text(Decimal("NaN"))
    assert canonical_decimal_text(Decimal("0.000")) == "0"
    with pytest.raises(TypeError):
        parse_canonical_decimal_text(1)
    for value in ("NaN", "+1", "01", "bad"):
        with pytest.raises(ValueError):
            parse_canonical_decimal_text(value)
    assert parse_canonical_decimal_text("1.20") == Decimal("1.20")
    original = getcontext().copy()
    assert not _has_fvg_arithmetic_settings(original) or original.prec == 28

    @deterministic_decimal
    def outer():
        @deterministic_decimal
        def inner():
            return getcontext().prec

        return inner()

    assert outer() == 28


def _zone() -> tuple[FvgZone, object, object, object]:
    a, b, c = bullish_triplet()
    z = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert z is not None
    return z, a, b, c


def test_zone_model_and_state_failures() -> None:
    z, a, b, c = _zone()
    assert z.to_feature_value()["size"] == 5.0
    state = dict(z.to_state())
    assert FvgZone.from_state(state) == z
    for value in (1, " BTCUSDT", "BTC USDT", ""):
        with pytest.raises((TypeError, ValueError)):
            _require_canonical_symbol(value)
    with pytest.raises(TypeError):
        _require_canonical_mapping_keys([], "x")
    with pytest.raises(ValueError):
        _require_canonical_mapping_keys({1: "x"}, "x")
    with pytest.raises(ValueError):
        _finite_float(Decimal("1e999999"), "x")
    with pytest.raises(ValueError):
        _finite_float(Decimal("1e-999999"), "x")
    malformed = dict(state)
    malformed["extra"] = "x"
    with pytest.raises(ValueError):
        FvgZone.from_state(malformed)
    malformed = dict(state)
    malformed["lower"] = 1
    with pytest.raises(ValueError):
        FvgZone.from_state(malformed)
    with pytest.raises(TypeError):
        replace(z, direction="bullish")
    with pytest.raises(TypeError):
        replace(z, lower=1)
    with pytest.raises(TypeError):
        replace(z, created_at="bad")
    with pytest.raises(ValueError):
        replace(z, timeframe="15M")
    with pytest.raises(ValueError):
        replace(z, source_b_open_time=z.source_a_open_time)
    with pytest.raises(ValueError):
        replace(z, lower=Decimal("NaN"))
    with pytest.raises(ValueError):
        replace(z, lower=Decimal("0"))
    with pytest.raises(ValueError):
        replace(z, lower=z.upper)


def test_analysis_input_validation_and_zero_range() -> None:
    z, a, b, c = _zone()
    with pytest.raises(TypeError):
        calculate_metrics(
            zone=object(),
            a=a,
            b=b,
            c=c,
            history=(),
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history="bad",
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )  # type: ignore[arg-type]
    for name, value in (("reference_price", 1), ("tick_size", 1), ("volume_ratio", 1)):
        kw = {"reference_price": None, "tick_size": None, "volume_ratio": None}
        kw[name] = value
        with pytest.raises(TypeError):
            calculate_metrics(zone=z, a=a, b=b, c=c, history=(), atr_period=2, average_range_period=2, **kw)  # type: ignore[arg-type]
    for name, value in (
        ("reference_price", Decimal("NaN")),
        ("tick_size", Decimal("NaN")),
        ("volume_ratio", Decimal("NaN")),
    ):
        kw = {"reference_price": None, "tick_size": None, "volume_ratio": None}
        kw[name] = value
        with pytest.raises(ValueError):
            calculate_metrics(zone=z, a=a, b=b, c=c, history=(), atr_period=2, average_range_period=2, **kw)
    with pytest.raises(ValueError):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history=(),
            reference_price=Decimal("0"),
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history=(),
            reference_price=None,
            tick_size=Decimal("0"),
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history=(),
            reference_price=None,
            tick_size=None,
            volume_ratio=Decimal("-1"),
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError, match="scope"):
        calculate_metrics(
            zone=z,
            a=replace(a, symbol="ETHUSDT"),
            b=b,
            c=c,
            history=(),
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError, match="source times"):
        calculate_metrics(
            zone=z,
            a=replace(a, timestamp=a.timestamp + timedelta(minutes=15)),
            b=b,
            c=c,
            history=(),
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError, match="history scope"):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history=(replace(a, symbol="ETHUSDT"),),
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )
    with pytest.raises(ValueError, match="future"):
        calculate_metrics(
            zone=z,
            a=a,
            b=b,
            c=c,
            history=(
                c,
                replace(c, timestamp=c.timestamp + timedelta(minutes=15)),
            ),
            reference_price=None,
            tick_size=None,
            atr_period=2,
            average_range_period=2,
        )


def test_evaluator_status_and_source_validation() -> None:
    passed = FvgFilterTrace(name="gap_size", decision=FvgFilterDecision.PASSED, reason="ok")
    failed = FvgFilterTrace(name="gap_size", decision=FvgFilterDecision.FAILED, reason="failed")
    unavailable = FvgFilterTrace(name="gap_size", decision=FvgFilterDecision.UNAVAILABLE, reason="missing")
    assert _status_for((failed,))[0] is FvgEvaluationStatus.REJECTED
    assert _status_for((unavailable,))[0] is FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA
    assert _status_for((passed,))[0] is FvgEvaluationStatus.ACCEPTED
    assert _target_skipped("price", FvgConfig().price_filter_target).decision is FvgFilterDecision.SKIPPED
    a, b, c = bullish_triplet()
    with pytest.raises(TypeError):
        evaluate_fvg_window(a=a, b=b, c=c, config=object())  # type: ignore[arg-type]
    bad = SimpleNamespace(
        **{k: getattr(a, k) for k in ("timestamp", "close_time", "open", "high", "low", "close", "volume")},
        symbol=1,
        timeframe="15m",
    )
    with pytest.raises(TypeError, match="a candle"):
        evaluate_fvg_window(a=bad, b=b, c=c, config=FvgConfig(timeframes=("15m",)))
    flat = replace(c, low=99, open=100, close=100)
    result = evaluate_fvg_window(a=a, b=b, c=flat, config=FvgConfig(timeframes=("15m",)))
    assert result.status is FvgEvaluationStatus.REJECTED
    with pytest.raises(ValueError, match="positive"):
        evaluate_fvg_window(a=a, b=b, c=c, config=FvgConfig(timeframes=("15m",)), reference_price=Decimal("0"))
