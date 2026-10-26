from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from modular_core import Candle, MarketView

from conftest import FakeCandle, bearish_triplet, bullish_triplet, candle, monotonic_bars, view_for
from modular_strategy_fvg import FvgConfig, FvgModule, FvgStateError, FvgZone
from modular_strategy_fvg.detector import as_decimal, detect_zone
from modular_strategy_fvg.models import FvgDirection


def valid_zone(**changes: object) -> FvgZone:
    values: dict[str, object] = {
        "zone_id": "zone",
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "direction": FvgDirection.BULLISH,
        "lower": Decimal("100"),
        "upper": Decimal("105"),
        "created_at": datetime(2024, 1, 1, 0, 45, tzinfo=timezone.utc),
        "source_a_open_time": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        "source_b_open_time": datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
        "source_c_open_time": datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
    }
    values.update(changes)
    return FvgZone(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["created_at", "source_a_open_time", "source_b_open_time", "source_c_open_time"])
def test_zone_rejects_naive_times(field: str) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        valid_zone(**{field: datetime(2024, 1, 1)})


@pytest.mark.parametrize(("field", "value"), [("zone_id", ""), ("symbol", ""), ("timeframe", "")])
def test_zone_requires_identity(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="required"):
        valid_zone(**{field: value})


@pytest.mark.parametrize(
    ("lower", "upper", "message"),
    [
        (Decimal("NaN"), Decimal("2"), "finite"),
        (Decimal("1"), Decimal("Infinity"), "finite"),
        (Decimal("0"), Decimal("1"), "positive"),
        (Decimal("-1"), Decimal("1"), "positive"),
        (Decimal("2"), Decimal("2"), "below"),
        (Decimal("3"), Decimal("2"), "below"),
        (Decimal("1e10000"), Decimal("2e10000"), "finite float"),
    ],
)
def test_zone_boundary_validation(lower: Decimal, upper: Decimal, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        valid_zone(lower=lower, upper=upper)


def test_zone_rejects_noncausal_source_times() -> None:
    with pytest.raises(ValueError, match="ordered"):
        valid_zone(source_b_open_time=datetime(2023, 12, 31, 23, 59, tzinfo=timezone.utc))


def test_zone_feature_and_state_mappings_are_immutable() -> None:
    zone = valid_zone()
    with pytest.raises(TypeError):
        zone.to_feature_value()["lower"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        zone.to_state()["lower"] = "1"  # type: ignore[index]


@pytest.mark.parametrize("mutation", ["missing", "unknown", "bad_decimal"])
def test_zone_from_state_rejects_malformed_payload(mutation: str) -> None:
    state = dict(valid_zone().to_state())
    if mutation == "missing":
        state.pop("lower")
    elif mutation == "unknown":
        state["extra"] = "x"
    else:
        state["lower"] = "not-a-decimal"
    with pytest.raises(ValueError):
        FvgZone.from_state(state)


@pytest.mark.parametrize(
    ("value", "error_type"),
    [("not-a-number", ValueError), (object(), TypeError)],
)
def test_as_decimal_rejects_non_decimal_compatible(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="decimal-compatible"):
        as_decimal(value)  # type: ignore[arg-type]


def test_detector_rejects_open_after_close() -> None:
    a, b, c = bullish_triplet()
    fake = FakeCandle(c.close_time, c.timestamp, c.high, c.low, c.close)
    with pytest.raises(ValueError, match="precede"):
        detect_zone(a, b, fake, FvgConfig(timeframes=("15m",)))


@pytest.mark.parametrize(
    ("field", "message"),
    [("symbol", "same symbol"), ("timeframe", "same timeframe")],
)
def test_detector_rejects_blank_contract_fields(field: str, message: str) -> None:
    a, b, c = bullish_triplet()
    values = dict(
        timestamp=c.timestamp,
        close_time=c.close_time,
        high=c.high,
        low=c.low,
        close=c.close,
        symbol=c.symbol,
        timeframe=c.timeframe,
    )
    values[field] = ""
    with pytest.raises(ValueError, match=message):
        detect_zone(a, b, FakeCandle(**values), FvgConfig(timeframes=("15m",)))


def test_detector_rejects_temporal_reordering_even_when_consecutive_disabled() -> None:
    a, b, c = bullish_triplet()
    fake = FakeCandle(a.timestamp - timedelta(minutes=15), a.timestamp, c.high, c.low, c.close)
    config = FvgConfig.from_parameters(timeframes=("15m",), require_consecutive_bars=False)
    with pytest.raises(ValueError, match="temporally ordered"):
        detect_zone(a, b, fake, config)


def test_current_candle_with_invalid_time_geometry_rejected() -> None:
    bad = FakeCandle(
        datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        101,
        99,
        100,
        timeframe="5m",
    )
    with pytest.raises(TypeError, match="base_bars must contain Candle"):
        MarketView((bad,), {}, 0, 1, 1, None, ())  # type: ignore[arg-type]


def test_legacy_restore_fills_missing_duplicate_fingerprint() -> None:
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bullish_triplet()))
    current = source.snapshot_state()
    legacy = {
        "state_schema_version": "1",
        "module_version": "0.1.0",
        "config_fingerprint": source._legacy_config_fingerprint,  # noqa: SLF001
        "last_processed": [
            {
                key: value
                for key, value in row.items()
                if key not in {"bar_fingerprint", "history_fingerprints", "barrier_close_time"}
            }
            for row in current["last_processed"]
        ],
        "zones": current["zones"],
    }
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(legacy)
    decision = restored.on_bar(view_for(bullish_triplet()))
    assert decision.diagnostics["duplicates"] == 1
    assert restored.snapshot_state()["last_processed"][0]["bar_fingerprint"] is not None


def test_inner_conflicting_duplicate_is_detected_during_first_batch() -> None:
    bars = list(bullish_triplet())
    c = bars[-1]
    bars.append(Candle(c.timestamp, c.open, c.high + 1, c.low, c.close, c.volume, c.symbol, c.timeframe))
    module = FvgModule(timeframes=("15m",))
    with pytest.raises(Exception, match="conflicting duplicate"):
        module.on_bar(view_for(bars))
    assert module.bound_symbol is None


@pytest.mark.parametrize("policy", ["raise", "reset_and_skip"])
def test_inner_temporal_regression_paths(policy: str) -> None:
    bars = monotonic_bars(3)
    reordered = [bars[0], bars[2], bars[1]]
    module = FvgModule(timeframes=("15m",), order_violation_policy=policy)
    if policy == "raise":
        with pytest.raises(Exception, match="regressed"):
            module.on_bar(view_for(reordered, base_close_offset_minutes=15))
    else:
        decision = module.on_bar(view_for(reordered, base_close_offset_minutes=15))
        assert "fvg.order_violation_reset:15m" in decision.skips


def test_multi_timeframe_missing_branch_does_not_block_available_series() -> None:
    module = FvgModule(timeframes=("15m", "4h"))
    decision = module.on_bar(view_for(bullish_triplet()))
    assert "fvg.no_data:4h" in decision.skips
    assert decision.features


def test_base_timeframe_series_path() -> None:
    bars = [
        Candle(datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), 95, 100, 90, 95, 1, "BTCUSDT", "5m"),
        Candle(datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc), 96, 102, 91, 97, 1, "BTCUSDT", "5m"),
        Candle(datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc), 106, 110, 105, 107, 1, "BTCUSDT", "5m"),
    ]
    view = MarketView(tuple(bars), {}, 2, 1, 1, None, ())
    decision = FvgModule(timeframes=("5m",)).on_bar(view)
    assert decision.features


def test_bearish_zone_invalidation_path() -> None:
    bars = list(bearish_triplet())
    module = FvgModule(timeframes=("15m",), invalidation_level="full_fill")
    module.on_bar(view_for(bars))
    next_bar = candle(3, open_=99, high=101, low=90, close=99)
    decision = module.on_bar(view_for([*bars, next_bar]))
    assert decision.feature_deletes[0].reason == "fvg_mitigated"


def test_processing_enforces_size_limit_atomically() -> None:
    module = FvgModule(timeframes=("15m",), max_state_bytes=1024, max_active_zones_per_timeframe=20)
    before = module.snapshot_state()
    with pytest.raises(FvgStateError, match="exceeds"):
        module.on_bar(view_for(monotonic_bars(10)))
    assert module.snapshot_state() == before


@pytest.mark.parametrize("value", [None, {}, "bad"])
def test_restore_state_lists_must_be_sequences(value: object) -> None:
    state = dict(FvgModule(timeframes=("15m",)).snapshot_state())
    state["last_processed"] = value
    with pytest.raises(FvgStateError, match="lists are malformed"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_restore_rejects_too_many_cursor_rows() -> None:
    module = FvgModule(timeframes=("15m",))
    state = dict(module.snapshot_state())
    state["last_processed"] = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "close_time": "2024-01-01T00:00:00+00:00",
            "bar_fingerprint": None,
            "history_fingerprints": [],
            "barrier_close_time": None,
        },
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "close_time": "2024-01-01T00:15:00+00:00",
            "bar_fingerprint": None,
            "history_fingerprints": [],
            "barrier_close_time": None,
        },
    ]
    state["bound_symbol"] = "BTCUSDT"
    with pytest.raises(FvgStateError, match="too many cursor"):
        module.restore_state(state)


@pytest.mark.parametrize("row", [1, {"symbol": "BTCUSDT"}])
def test_restore_rejects_bad_cursor_rows(row: object) -> None:
    module = FvgModule(timeframes=("15m",))
    state = dict(module.snapshot_state())
    state["last_processed"] = [row]
    state["bound_symbol"] = "BTCUSDT"
    with pytest.raises(FvgStateError):
        module.restore_state(state)


def test_restore_rejects_empty_or_unconfigured_cursor_contract() -> None:
    for symbol, timeframe in [("", "15m"), ("BTCUSDT", "4h")]:
        module = FvgModule(timeframes=("15m",))
        state = dict(module.snapshot_state())
        state["last_processed"] = [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "close_time": "2024-01-01T00:15:00+00:00",
                "bar_fingerprint": None,
                "history_fingerprints": [],
                "barrier_close_time": None,
            }
        ]
        state["bound_symbol"] = symbol or None
        with pytest.raises(FvgStateError):
            module.restore_state(state)


def test_restore_accepts_gap_barrier_after_cursor() -> None:
    state = copy.deepcopy(FvgModule(timeframes=("15m",)).snapshot_state())
    state["bound_symbol"] = "BTCUSDT"
    state["last_processed"] = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "close_time": "2024-01-01T00:15:00+00:00",
            "bar_fingerprint": None,
            "history_fingerprints": [],
            "barrier_close_time": "2024-01-01T00:30:00+00:00",
        }
    ]
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(state)
    assert restored.snapshot_state()["last_processed"][0]["barrier_close_time"] == "2024-01-01T00:30:00+00:00"


def test_restore_rejects_non_mapping_zone_row() -> None:
    state = dict(FvgModule(timeframes=("15m",)).snapshot_state())
    state["zones"] = [1]
    with pytest.raises(FvgStateError, match="zone state row"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_restore_rejects_non_json_state() -> None:
    with pytest.raises(FvgStateError, match="JSON-serializable"):
        FvgModule(timeframes=("15m",)).restore_state({"bad": object()})


@pytest.mark.parametrize("value", ["not-a-date", "2024-01-01T00:00:00"])
def test_restore_rejects_invalid_or_naive_cursor_datetime(value: str) -> None:
    module = FvgModule(timeframes=("15m",))
    state = dict(module.snapshot_state())
    state["bound_symbol"] = "BTCUSDT"
    state["last_processed"] = [
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "close_time": value,
            "bar_fingerprint": None,
            "history_fingerprints": [],
            "barrier_close_time": None,
        }
    ]
    with pytest.raises(FvgStateError):
        module.restore_state(state)


def test_internal_counter_rejects_corrupted_diagnostics() -> None:
    with pytest.raises(TypeError, match="not an integer"):
        FvgModule._increment_counter({"x": "bad"}, "x")
