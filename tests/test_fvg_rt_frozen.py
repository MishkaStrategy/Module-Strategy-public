from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from modular_core import Candle, MarketView, ModuleRegistry, ReadOnlySeries
from strategy_modules.fvg_rt import (
    FROZEN_1H_CONDITIONS,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_CONDITIONS,
    FROZEN_4H_FEATURE_KEY,
    Frozen1hShortModule,
    Frozen4hShortModule,
)


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def candle(
    *,
    timestamp: datetime,
    timeframe: str,
    open: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: float = 10.0,
) -> Candle:
    return Candle(
        timestamp=timestamp,
        timeframe=timeframe,
        symbol="BTCUSDT",
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def falling_series(*, timeframe: str, interval: timedelta, count: int) -> tuple[Candle, ...]:
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


def bearish_fvg_series(*, timeframe: str, interval: timedelta) -> tuple[Candle, ...]:
    bars = list(falling_series(timeframe=timeframe, interval=interval, count=200))
    bars[-3] = candle(
        timestamp=NOW - interval * 3,
        timeframe=timeframe,
        open=100.30,
        high=100.50,
        low=100.10,
        close=100.25,
    )
    bars[-2] = candle(
        timestamp=NOW - interval * 2,
        timeframe=timeframe,
        open=100.00,
        high=100.10,
        low=99.35,
        close=99.40,
        volume=20.0,
    )
    bars[-1] = candle(
        timestamp=NOW - interval,
        timeframe=timeframe,
        open=99.35,
        high=99.75,
        low=99.10,
        close=99.20,
    )
    return tuple(bars)


def market_view(
    *,
    one_hour: tuple[Candle, ...] | None = None,
    four_hour: tuple[Candle, ...],
) -> MarketView:
    timeframe_bars = {"4h": four_hour}
    if one_hour is not None:
        timeframe_bars["1h"] = one_hour
    return MarketView(
        base_bars=(candle(timestamp=NOW - timedelta(minutes=5), timeframe="5m"),),
        timeframe_bars=timeframe_bars,
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )


def test_frozen_condition_identities_match_canonical_runtime() -> None:
    assert FROZEN_1H_CONDITIONS == frozenset(
        {
            "all_inputs_completed",
            "atr_fraction_inclusive",
            "bearish_fvg",
            "close_below_ema50",
            "completed_4h_close_below_ema200",
            "ema50_falling_4",
            "fvg_size_atr_inclusive",
            "impulse_efficiency_min",
            "middle_body_atr_min",
            "middle_bearish",
            "short_location_8h_min",
            "volume_ratio_min",
        }
    )
    assert FROZEN_4H_CONDITIONS == frozenset(
        {
            "all_inputs_completed",
            "atr_fraction_inclusive",
            "bearish_fvg",
            "fvg_size_atr_inclusive",
            "impulse_efficiency_min",
            "middle_body_atr_min",
            "middle_bearish",
            "rsi14_max",
            "short_location_32h_min",
            "volume_ratio_min",
        }
    )


def test_frozen_runtime_entry_points_are_discoverable() -> None:
    discovered = ModuleRegistry().discover_entry_points()

    assert "frozen_1h_short" in discovered
    assert "frozen_4h_short" in discovered


def test_frozen_1h_reproduces_owner_bound_signal_geometry_and_execution_contract() -> None:
    one_hour = bearish_fvg_series(timeframe="1h", interval=timedelta(hours=1))
    four_hour = falling_series(timeframe="4h", interval=timedelta(hours=4), count=200)
    module = Frozen1hShortModule()

    decision = module.on_bar(market_view(one_hour=one_hour, four_hour=four_hour))

    assert len(decision.features) == 1
    value = decision.features[0].value
    assert decision.features[0].key == FROZEN_1H_FEATURE_KEY
    gap = one_hour[-3].low - one_hour[-1].high
    expected_entry = one_hour[-1].high + 0.75 * gap
    true_ranges = []
    for index, item in enumerate(one_hour):
        previous_close = one_hour[index - 1].close if index else item.close
        true_ranges.append(
            max(
                item.high - item.low,
                abs(item.high - previous_close),
                abs(item.low - previous_close),
            )
        )
    expected_atr = sum(true_ranges[-14:]) / 14
    expected_stop = max(item.high for item in one_hour[-5:]) + 0.05 * expected_atr
    assert float(value["entry_price"]) == expected_entry
    assert float(value["stop_price"]) == expected_stop
    assert tuple(value["completed_timeframes"]) == ("5m", "1h", "4h")
    assert value["target_r"] == "6"
    assert value["ttl_bars_5m"] == 48
    assert value["max_hold_bars_5m"] == 144
    assert value["no_progress_bars_5m"] == 72
    assert value["no_progress_min_mfe_r"] == "0.5"

    expected_digest = hashlib.sha256(
        json.dumps(
            {
                "available_at": NOW.isoformat(),
                "entry": round(expected_entry, 12),
                "module_id": "frozen_1h_short",
                "module_version": "1.0.0",
                "setup_id": "short",
                "stop": round(float(value["stop_price"]), 12),
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert value["semantic_input_digest"] == expected_digest


def test_frozen_4h_reproduces_owner_bound_signal_geometry_and_execution_contract() -> None:
    four_hour = bearish_fvg_series(timeframe="4h", interval=timedelta(hours=4))
    module = Frozen4hShortModule()

    decision = module.on_bar(market_view(four_hour=four_hour))

    assert len(decision.features) == 1
    value = decision.features[0].value
    assert decision.features[0].key == FROZEN_4H_FEATURE_KEY
    gap = four_hour[-3].low - four_hour[-1].high
    expected_entry = four_hour[-1].high + 0.50 * gap
    assert float(value["entry_price"]) == expected_entry
    assert float(value["stop_price"]) > expected_entry
    assert tuple(value["completed_timeframes"]) == ("5m", "4h")
    assert value["target_r"] == "6"
    assert value["ttl_bars_5m"] == 576
    assert value["max_hold_bars_5m"] == 576
    assert value["no_progress_bars_5m"] == 432
    assert value["no_progress_min_mfe_r"] == "0.34999999999999998"


def test_frozen_1h_candidate_skips_indicator_work_when_no_fvg_geometry(monkeypatch) -> None:
    module = Frozen1hShortModule()

    def fail_if_called(_: object) -> object:
        raise AssertionError("indicator cache must not run without FVG geometry")

    for cache in module._indicator_cache.values():
        monkeypatch.setattr(cache, "values", fail_if_called)

    assert (
        module._candidate(
            {
                "1h": falling_series(timeframe="1h", interval=timedelta(hours=1), count=200),
                "4h": falling_series(timeframe="4h", interval=timedelta(hours=4), count=200),
            },
            available_at=NOW,
        )
        is None
    )


def test_frozen_4h_candidate_skips_indicator_work_when_no_fvg_geometry(monkeypatch) -> None:
    module = Frozen4hShortModule()

    def fail_if_called(_: object) -> object:
        raise AssertionError("indicator cache must not run without FVG geometry")

    for cache in module._indicator_cache.values():
        monkeypatch.setattr(cache, "values", fail_if_called)

    assert (
        module._candidate(
            {"4h": falling_series(timeframe="4h", interval=timedelta(hours=4), count=200)},
            available_at=NOW,
        )
        is None
    )


def test_frozen_restore_state_clears_derived_indicator_cache() -> None:
    module = Frozen1hShortModule()
    bars = falling_series(timeframe="1h", interval=timedelta(hours=1), count=60)
    module._indicator_cache["1h"].values(bars)
    module._symbol_series_validator.matches(
        {
            "1h": ReadOnlySeries(bars, end=len(bars)),
            "4h": ReadOnlySeries(
                falling_series(timeframe="4h", interval=timedelta(hours=4), count=1),
                end=1,
            ),
        },
        ("1h", "4h"),
        "BTCUSDT",
        view_index=0,
    )

    assert module._indicator_cache["1h"]._results
    assert module._symbol_series_validator._view_index == 0

    module.restore_state(
        {
            "payload_version": 1,
            "last_processed_close": None,
            "bound_symbol": None,
        }
    )

    assert module._indicator_cache["1h"]._results == []
    assert module._symbol_series_validator._view_index is None


def test_frozen_data_gap_resets_symbol_series_validation() -> None:
    module = Frozen1hShortModule()
    module._symbol_series_validator.matches(
        {
            "1h": ReadOnlySeries((candle(timestamp=NOW, timeframe="1h"),)),
            "4h": ReadOnlySeries((candle(timestamp=NOW, timeframe="4h"),)),
        },
        ("1h", "4h"),
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

    assert module.on_bar(gap_view).skips == ("frozen_1h_short.data_gap_reset",)
    assert module._symbol_series_validator._view_index is None


def test_frozen_runtime_state_round_trip_preserves_duplicate_suppression() -> None:
    one_hour = bearish_fvg_series(timeframe="1h", interval=timedelta(hours=1))
    four_hour = falling_series(timeframe="4h", interval=timedelta(hours=4), count=200)
    module = Frozen1hShortModule()
    runtime_view = market_view(one_hour=one_hour, four_hour=four_hour)

    assert len(module.on_bar(runtime_view).features) == 1
    state = module.snapshot_state()
    restored = Frozen1hShortModule()
    restored.restore_state(state)

    assert restored.snapshot_state() == state
    assert restored.on_bar(runtime_view).skips == ("frozen_1h_short.duplicate_1h_close",)
