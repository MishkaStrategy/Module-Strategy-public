from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest
from modular_core import Candle, MarketView, ModuleRegistry, ReadOnlySeries
from strategy_modules.fvg_rt import CONDITIONS_BY_SETUP, FEATURE_KEY, FvgRtFinal15mModule
from strategy_modules.fvg_rt.module import (
    _IndicatorCache,
    _SymbolSeriesValidator,
    _indicators,
)


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)

EXPECTED_CONDITIONS_BY_SETUP = {
    "long": frozenset(
        {
            "all_inputs_completed",
            "atr_fraction_inclusive",
            "bullish_fvg",
            "close_above_ema50",
            "completed_4h_close_above_ema200",
            "ema50_rising_4",
            "fvg_size_atr_inclusive",
            "impulse_efficiency_min",
            "location_12h_min",
            "location_8h_min",
            "middle_body_atr_min",
            "middle_bullish",
            "rsi14_min",
            "session_allowed",
            "volume_ratio_min",
        }
    ),
    "short_a": frozenset(
        {
            "all_inputs_completed",
            "atr_fraction_inclusive",
            "bearish_fvg",
            "completed_1d_close_below_ema200",
            "completed_4h_trend_down_3",
            "fvg_size_atr_inclusive",
            "impulse_efficiency_min",
            "middle_bearish",
            "middle_body_atr_min",
            "short_location_4h_max",
            "volume_ratio_min",
        }
    ),
    "short_b": frozenset(
        {
            "all_inputs_completed",
            "atr_fraction_inclusive",
            "bearish_bos_48",
            "bearish_fvg",
            "close_strength_min",
            "completed_1d_close_below_ema200",
            "fvg_size_atr_inclusive",
            "impulse_efficiency_min",
            "middle_bearish",
            "middle_body_atr_min",
            "no_recent_bullish_fvg_12",
            "rsi14_max",
            "short_location_48h_min",
            "short_location_8h_min",
            "volume_ratio_min",
        }
    ),
}


def candle(
    *,
    timestamp: datetime,
    timeframe: str,
    open: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: float = 10.0,
    symbol: str = "BTCUSDT",
) -> Candle:
    return Candle(
        timestamp=timestamp,
        timeframe=timeframe,
        symbol=symbol,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def view() -> MarketView:
    base = candle(timestamp=NOW - timedelta(minutes=5), timeframe="5m")
    fifteen = candle(timestamp=NOW - timedelta(minutes=15), timeframe="15m")
    four_hours = tuple(
        candle(timestamp=NOW - timedelta(hours=4 * (index + 1)), timeframe="4h")
        for index in reversed(range(200))
    )
    daily = tuple(
        candle(timestamp=NOW - timedelta(days=index + 1), timeframe="1d")
        for index in reversed(range(200))
    )
    return MarketView(
        base_bars=(base,),
        timeframe_bars={"15m": (fifteen,), "4h": four_hours, "1d": daily},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )


def _rising_series(*, timeframe: str, interval: timedelta, count: int) -> tuple[Candle, ...]:
    start = NOW - interval * count
    return tuple(
        candle(
            timestamp=start + interval * index,
            timeframe=timeframe,
            open=100.0 + index * 0.1,
            high=100.35 + index * 0.1,
            low=99.85 + index * 0.1,
            close=100.1 + index * 0.1,
            volume=10.0,
        )
        for index in range(count)
    )


def _falling_series(*, timeframe: str, interval: timedelta, count: int) -> tuple[Candle, ...]:
    start = NOW - interval * count
    return tuple(
        candle(
            timestamp=start + interval * index,
            timeframe=timeframe,
            open=120.05 - index * 0.1,
            high=120.2 - index * 0.1,
            low=119.8 - index * 0.1,
            close=120.0 - index * 0.1,
            volume=10.0,
        )
        for index in range(count)
    )


def _assert_indicator_series_equal(actual: object, expected: object) -> None:
    actual_rows = tuple(actual)
    expected_rows = tuple(expected)
    assert len(actual_rows) == len(expected_rows)
    for actual_row, expected_row in zip(actual_rows, expected_rows, strict=True):
        assert actual_row.atr14 == pytest.approx(expected_row.atr14, nan_ok=True)
        assert actual_row.ema50 == pytest.approx(expected_row.ema50, nan_ok=True)
        assert actual_row.ema200 == pytest.approx(expected_row.ema200, nan_ok=True)
        assert actual_row.rsi14 == pytest.approx(expected_row.rsi14, nan_ok=True)
        assert actual_row.volume_sma20 == pytest.approx(expected_row.volume_sma20, nan_ok=True)


def snapshot(*, setup_id: str = "long") -> dict[str, object]:
    module = FvgRtFinal15mModule()
    stops = {"long": 95.0, "short_a": 101.0, "short_b": 102.0}
    raw = module._snapshot(
        setup_id,
        "BTCUSDT",
        NOW,
        entry=100.0,
        stop=stops[setup_id],
    )
    assert raw is not None
    return dict(raw)


@pytest.mark.parametrize("mixed_index", (0, 2, 4))
def test_symbol_validator_fail_closed_scans_direct_market_view(mixed_index: int) -> None:
    bars = tuple(
        candle(
            timestamp=NOW - timedelta(minutes=15 * (5 - index)),
            timeframe="15m",
            symbol="ETHUSDT" if index == mixed_index else "BTCUSDT",
        )
        for index in range(5)
    )
    market_view = MarketView(
        base_bars=(candle(timestamp=NOW - timedelta(minutes=5), timeframe="5m"),),
        timeframe_bars={"15m": bars},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )

    assert not _SymbolSeriesValidator().matches(
        market_view.timeframe_bars,
        ("15m",),
        "BTCUSDT",
        view_index=market_view.index,
    )


def test_symbol_validator_checks_only_new_candles_on_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = [
        candle(timestamp=NOW - timedelta(minutes=15 * (3 - index)), timeframe="15m")
        for index in range(3)
    ]
    validator = _SymbolSeriesValidator()
    assert validator.matches(
        {"15m": ReadOnlySeries(source, end=3)},
        ("15m",),
        "BTCUSDT",
        view_index=0,
    )

    iteration_count = 0
    original_iter = ReadOnlySeries.__iter__

    def counted_iter(self: ReadOnlySeries[Candle]) -> Iterator[Candle]:
        nonlocal iteration_count
        iteration_count += 1
        yield from original_iter(self)

    monkeypatch.setattr(ReadOnlySeries, "__iter__", counted_iter)
    source.append(candle(timestamp=NOW, timeframe="15m", symbol="ETHUSDT"))

    assert not validator.matches(
        {"15m": ReadOnlySeries(source, end=4)},
        ("15m",),
        "BTCUSDT",
        view_index=3,
    )
    assert iteration_count == 0


def test_symbol_validator_handles_capped_sliding_window_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = [
        candle(timestamp=NOW - timedelta(minutes=15 * (3 - index)), timeframe="15m")
        for index in range(3)
    ]
    validator = _SymbolSeriesValidator()
    assert validator.matches(
        {"15m": ReadOnlySeries(source, end=3, start=0)},
        ("15m",),
        "BTCUSDT",
        view_index=0,
    )

    iteration_count = 0
    original_iter = ReadOnlySeries.__iter__

    def counted_iter(self: ReadOnlySeries[Candle]) -> Iterator[Candle]:
        nonlocal iteration_count
        iteration_count += 1
        yield from original_iter(self)

    monkeypatch.setattr(ReadOnlySeries, "__iter__", counted_iter)
    source.append(candle(timestamp=NOW, timeframe="15m"))
    assert validator.matches(
        {"15m": ReadOnlySeries(source, end=4, start=1)},
        ("15m",),
        "BTCUSDT",
        view_index=3,
    )
    source.append(
        candle(
            timestamp=NOW + timedelta(minutes=15),
            timeframe="15m",
            symbol="ETHUSDT",
        )
    )
    assert not validator.matches(
        {"15m": ReadOnlySeries(source, end=5, start=2)},
        ("15m",),
        "BTCUSDT",
        view_index=6,
    )
    assert iteration_count == 0


def test_symbol_validator_falls_back_for_replaced_or_unexpected_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = [
        candle(timestamp=NOW - timedelta(minutes=15 * (5 - index)), timeframe="15m")
        for index in range(5)
    ]
    validator = _SymbolSeriesValidator()
    assert validator.matches(
        {"15m": ReadOnlySeries(original, end=5)},
        ("15m",),
        "BTCUSDT",
        view_index=0,
    )

    iteration_count = 0
    original_iter = ReadOnlySeries.__iter__

    def counted_iter(self: ReadOnlySeries[Candle]) -> Iterator[Candle]:
        nonlocal iteration_count
        iteration_count += 1
        yield from original_iter(self)

    monkeypatch.setattr(ReadOnlySeries, "__iter__", counted_iter)
    replacement = list(original)
    replacement[2] = candle(
        timestamp=replacement[2].timestamp,
        timeframe="15m",
        symbol="ETHUSDT",
    )
    assert not validator.matches(
        {"15m": ReadOnlySeries(replacement, end=5)},
        ("15m",),
        "BTCUSDT",
        view_index=1,
    )
    assert iteration_count == 1

    validator.reset()
    replacement[2] = original[2]
    assert validator.matches(
        {"15m": ReadOnlySeries(replacement, end=5)},
        ("15m",),
        "BTCUSDT",
        view_index=2,
    )
    iterations_before_unexpected_geometry = iteration_count
    replacement.extend(
        (
            candle(timestamp=NOW + timedelta(minutes=15), timeframe="15m"),
            candle(
                timestamp=NOW + timedelta(minutes=30),
                timeframe="15m",
                symbol="ETHUSDT",
            ),
        )
    )
    assert not validator.matches(
        {"15m": ReadOnlySeries(replacement, end=7)},
        ("15m",),
        "BTCUSDT",
        view_index=3,
    )
    assert iteration_count == iterations_before_unexpected_geometry + 1


def test_snapshots_skip_indicator_work_when_no_fvg_geometry(monkeypatch) -> None:
    bars = _rising_series(timeframe="15m", interval=timedelta(minutes=15), count=200)
    module = FvgRtFinal15mModule()

    def fail_if_called(_: object) -> object:
        raise AssertionError("indicator cache must not run without FVG geometry")

    for cache in module._indicator_cache.values():
        monkeypatch.setattr(cache, "values", fail_if_called)

    assert (
        module._snapshots(
            bars_15m=bars,
            bars_4h=_rising_series(timeframe="4h", interval=timedelta(hours=4), count=200),
            bars_1d=_rising_series(timeframe="1d", interval=timedelta(days=1), count=200),
            available_at=NOW,
            symbol="BTCUSDT",
        )
        == ()
    )


def test_incremental_indicator_cache_matches_full_recomputation() -> None:
    bars = _rising_series(timeframe="15m", interval=timedelta(minutes=15), count=260)
    cache = _IndicatorCache()

    for length in (1, 2, 14, 15, 20, 50, 199, 200, 259, 260):
        incremental = tuple(cache.values(bars[:length]))
        recomputed = _indicators(bars[:length])
        _assert_indicator_series_equal(incremental, recomputed)


def test_indicator_cache_materializes_shifted_read_only_window_once(monkeypatch) -> None:
    bars = _rising_series(timeframe="15m", interval=timedelta(minutes=15), count=280)
    cache = _IndicatorCache()
    first = ReadOnlySeries(bars, end=260)
    shifted = ReadOnlySeries(bars, end=280, start=20)

    _assert_indicator_series_equal(cache.values(first), _indicators(tuple(first)))

    getitem_calls = 0
    original_getitem = ReadOnlySeries.__getitem__

    def counted_getitem(self: ReadOnlySeries[Candle], index: int | slice) -> object:
        nonlocal getitem_calls
        getitem_calls += 1
        return original_getitem(self, index)

    monkeypatch.setattr(ReadOnlySeries, "__getitem__", counted_getitem)

    actual = tuple(cache.values(shifted))
    expected = _indicators(tuple(shifted))
    _assert_indicator_series_equal(actual, expected)

    assert getitem_calls == 1


def test_indicator_cache_resets_fail_safe_for_a_replaced_prefix() -> None:
    bars = _rising_series(timeframe="15m", interval=timedelta(minutes=15), count=60)
    cache = _IndicatorCache()
    _assert_indicator_series_equal(cache.values(bars[:50]), _indicators(bars[:50]))

    replaced = tuple(
        Candle(
            timestamp=item.timestamp,
            timeframe=item.timeframe,
            symbol=item.symbol,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
        )
        for item in bars
    )
    _assert_indicator_series_equal(cache.values(replaced), _indicators(replaced))


def test_condition_identities_match_owner_bound_runtime_contract() -> None:
    assert CONDITIONS_BY_SETUP == EXPECTED_CONDITIONS_BY_SETUP


def test_completed_timeframes_match_owner_bound_runtime_contract() -> None:
    expected = {
        "long": ("5m", "15m", "4h"),
        "short_a": ("5m", "15m", "4h", "1d"),
        "short_b": ("5m", "15m", "1d"),
    }

    for setup_id, completed_timeframes in expected.items():
        assert tuple(snapshot(setup_id=setup_id)["completed_timeframes"]) == completed_timeframes


def test_semantic_input_digest_matches_historical_signal_key_contract() -> None:
    value = snapshot(setup_id="long")
    expected = hashlib.sha256(
        json.dumps(
            {
                "available_at": NOW.isoformat(),
                "entry": 100.0,
                "module_id": "fvg_rt_final_15m",
                "module_version": "2.0.0",
                "setup_id": "long",
                "stop": 95.0,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert value["semantic_input_digest"] == expected


def test_runtime_is_discoverable_through_the_module_entry_point() -> None:
    discovered = ModuleRegistry().discover_entry_points()

    assert "fvg_rt_final_15m" in discovered


def test_module_publishes_a_same_pass_snapshot_from_closed_15m_input(monkeypatch) -> None:
    module = FvgRtFinal15mModule()
    expected = snapshot()
    monkeypatch.setattr(module, "_snapshots", lambda **_: (expected,))

    decision = module.on_bar(view())

    assert len(decision.features) == 1
    feature = decision.features[0]
    assert feature.key == FEATURE_KEY
    assert feature.created_at == NOW
    assert feature.valid_until is not None and feature.valid_until > feature.created_at
    assert feature.value["snapshot_id"] == expected["snapshot_id"]
    assert module.on_bar(view()).skips == ("fvg_rt_final_15m.duplicate_15m_close",)


def test_restore_state_clears_derived_indicator_cache() -> None:
    module = FvgRtFinal15mModule()
    bars = _rising_series(timeframe="15m", interval=timedelta(minutes=15), count=60)
    module._indicator_cache["15m"].values(bars)
    module._symbol_series_validator.matches(
        {
            "15m": ReadOnlySeries(bars, end=len(bars)),
            "4h": ReadOnlySeries(
                _rising_series(timeframe="4h", interval=timedelta(hours=4), count=1),
                end=1,
            ),
            "1d": ReadOnlySeries(
                _rising_series(timeframe="1d", interval=timedelta(days=1), count=1),
                end=1,
            ),
        },
        ("15m", "4h", "1d"),
        "BTCUSDT",
        view_index=0,
    )

    assert module._indicator_cache["15m"]._results
    assert module._symbol_series_validator._view_index == 0

    module.restore_state(
        {
            "payload_version": 1,
            "last_processed_close": None,
            "bound_symbol": None,
        }
    )

    assert module._indicator_cache["15m"]._results == []
    assert module._symbol_series_validator._view_index is None


def test_data_gap_resets_symbol_series_validation() -> None:
    module = FvgRtFinal15mModule()
    module._symbol_series_validator.matches(
        {
            "15m": ReadOnlySeries((candle(timestamp=NOW, timeframe="15m"),)),
            "4h": ReadOnlySeries((candle(timestamp=NOW, timeframe="4h"),)),
            "1d": ReadOnlySeries((candle(timestamp=NOW, timeframe="1d"),)),
        },
        ("15m", "4h", "1d"),
        "BTCUSDT",
        view_index=0,
    )
    gap_view = MarketView(
        base_bars=(candle(timestamp=NOW, timeframe="5m"),),
        timeframe_bars={},
        index=1,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=True,
    )

    assert module.on_bar(gap_view).skips == ("fvg_rt_final_15m.data_gap_reset",)
    assert module._symbol_series_validator._view_index is None


def test_module_state_round_trip_preserves_duplicate_suppression(monkeypatch) -> None:
    module = FvgRtFinal15mModule()
    expected = snapshot()
    monkeypatch.setattr(module, "_snapshots", lambda **_: (expected,))
    assert len(module.on_bar(view()).features) == 1

    state = module.snapshot_state()
    restored = FvgRtFinal15mModule()
    restored.restore_state(state)

    assert restored.snapshot_state() == state
    assert restored.on_bar(view()).skips == ("fvg_rt_final_15m.duplicate_15m_close",)


@pytest.mark.parametrize(
    "state",
    (
        {},
        {
            "payload_version": 2,
            "last_processed_close": None,
            "bound_symbol": None,
        },
        {
            "payload_version": 1,
            "last_processed_close": "2026-01-02T12:00:00",
            "bound_symbol": "BTCUSDT",
        },
        {
            "payload_version": 1,
            "last_processed_close": NOW.isoformat(),
            "bound_symbol": None,
        },
    ),
)
def test_module_restore_state_fails_closed(state: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FvgRtFinal15mModule().restore_state(state)


def test_module_evaluates_owner_bound_short_a_and_short_b_contracts_from_closed_bars() -> None:
    fifteen = list(_falling_series(timeframe="15m", interval=timedelta(minutes=15), count=200))
    fifteen[-3] = candle(
        timestamp=NOW - timedelta(minutes=45),
        timeframe="15m",
        open=100.30,
        high=100.50,
        low=100.10,
        close=100.25,
    )
    fifteen[-2] = candle(
        timestamp=NOW - timedelta(minutes=30),
        timeframe="15m",
        open=100.00,
        high=100.10,
        low=99.35,
        close=99.40,
        volume=20.0,
    )
    fifteen[-1] = candle(
        timestamp=NOW - timedelta(minutes=15),
        timeframe="15m",
        open=99.35,
        high=99.75,
        low=99.10,
        close=99.20,
    )
    four_hours = _falling_series(timeframe="4h", interval=timedelta(hours=4), count=4)
    daily = _falling_series(timeframe="1d", interval=timedelta(days=1), count=200)
    runtime_view = MarketView(
        base_bars=(candle(timestamp=NOW - timedelta(minutes=5), timeframe="5m"),),
        timeframe_bars={
            "15m": tuple(fifteen),
            "4h": four_hours,
            "1d": daily,
        },
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )

    decision = FvgRtFinal15mModule().on_bar(runtime_view)

    values = {str(feature.value["setup_id"]): feature.value for feature in decision.features}
    assert set(values) == {"short_a", "short_b"}

    # short_a only needs 4h trend/location, while short_b does not consume
    # 4h at all; four bars are intentionally insufficient for a 4h EMA200.
    assert len(runtime_view.timeframe_bars["4h"]) == 4

    gap = fifteen[-3].low - fifteen[-1].high
    expected_short_a_entry = fifteen[-1].high + 0.25 * gap
    expected_short_a_stop = max(item.high for item in fifteen[-8:])
    assert float(values["short_a"]["entry_price"]) == expected_short_a_entry
    assert float(values["short_a"]["stop_price"]) == expected_short_a_stop
    assert values["short_a"]["target_r"] == "5"
    assert values["short_a"]["ttl_bars_5m"] == 48

    expected_short_b_entry = fifteen[-1].high
    expected_short_b_stop = max(item.high for item in fifteen[-16:])
    assert float(values["short_b"]["entry_price"]) == expected_short_b_entry
    assert float(values["short_b"]["stop_price"]) == expected_short_b_stop
    assert values["short_b"]["target_r"] == "6"
    assert values["short_b"]["ttl_bars_5m"] == 48


def test_module_evaluates_the_owner_bound_long_contract_from_closed_bars() -> None:
    fifteen = list(_rising_series(timeframe="15m", interval=timedelta(minutes=15), count=200))
    fifteen[-3] = candle(
        timestamp=NOW - timedelta(minutes=45),
        timeframe="15m",
        open=119.3,
        high=119.55,
        low=119.05,
        close=119.3,
    )
    fifteen[-2] = candle(
        timestamp=NOW - timedelta(minutes=30),
        timeframe="15m",
        open=119.3,
        high=120.25,
        low=119.25,
        close=120.2,
        volume=20.0,
    )
    fifteen[-1] = candle(
        timestamp=NOW - timedelta(minutes=15),
        timeframe="15m",
        open=120.2,
        high=120.5,
        low=119.75,
        close=120.35,
    )
    runtime_view = MarketView(
        base_bars=(candle(timestamp=NOW - timedelta(minutes=5), timeframe="5m"),),
        timeframe_bars={
            "15m": tuple(fifteen),
            "4h": _rising_series(timeframe="4h", interval=timedelta(hours=4), count=200),
            "1d": _rising_series(timeframe="1d", interval=timedelta(days=1), count=2),
        },
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )

    decision = FvgRtFinal15mModule().on_bar(runtime_view)

    assert len(decision.features) == 1
    feature = decision.features[0]
    assert feature.value["setup_id"] == "long"
    assert feature.value["side"] == "buy"
    assert feature.value["entry_price"] == "119.75"
    assert feature.value["stop_price"] == "118.13714285714286"

    # Long does not depend on a completed 1d EMA200; two daily bars are
    # intentionally insufficient for that indicator.
    assert len(runtime_view.timeframe_bars["1d"]) == 2
