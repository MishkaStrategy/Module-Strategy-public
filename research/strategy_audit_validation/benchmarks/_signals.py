"""Causal completed-candle signal generation for canonical Variant 2."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ._common import (
    Signal,
    _append_signal,
    add_indicators,
    aggregate,
    completed_asof,
    rolling_location,
)


def final_15m_signals(
    frame: pd.DataFrame,
    frame_4h: pd.DataFrame,
    frame_1d: pd.DataFrame,
) -> list[Signal]:
    data = add_indicators(frame)
    availability = data.index + pd.Timedelta(minutes=15)
    four_source = add_indicators(frame_4h)
    four_source["trend_down_3"] = four_source["close"] < four_source["close"].shift(3)
    four = completed_asof(four_source, availability)
    daily = completed_asof(add_indicators(frame_1d), availability)
    location_long_8h = rolling_location(data, 32, short=False)
    location_long_12h = rolling_location(data, 48, short=False)
    location_short_8h = rolling_location(data, 32, short=True)
    location_short_48h = rolling_location(data, 192, short=True)
    recent_bullish = (data["low"] > data["high"].shift(2)).shift(1).rolling(12).max().fillna(0)
    previous_48_low = data["low"].shift(1).rolling(48, min_periods=48).min()
    output: list[Signal] = []
    for index in range(2, len(data)):
        row = data.iloc[index]
        a = data.iloc[index - 2]
        b = data.iloc[index - 1]
        atr = float(row["atr14"])
        if not math.isfinite(atr) or atr <= 0:
            continue
        denominator = (a["high"] - a["low"]) + (b["high"] - b["low"]) + (row["high"] - row["low"])
        if denominator <= 0:
            continue
        quality = abs(row["close"] - a["open"]) / denominator
        available_at = availability[index]
        hour = available_at.hour
        bullish = row["low"] > a["high"]
        bearish = row["high"] < a["low"]
        body_atr = abs(b["close"] - b["open"]) / atr
        volume_ratio = b["volume"] / b["volume_sma20"] if b["volume_sma20"] > 0 else np.nan
        atr_fraction = atr / row["close"]
        if bullish:
            fvg_size = (row["low"] - a["high"]) / atr
            conditions = (
                0.10 <= fvg_size <= 0.50
                and body_atr >= 0.75
                and b["close"] > b["open"]
                and volume_ratio >= 1.0
                and row["close"] > row["ema50"]
                and row["ema50"] > data.iloc[index - 4]["ema50"]
                and location_long_8h.iloc[index] >= 0.25
                and location_long_12h.iloc[index] >= 0.25
                and row["rsi14"] >= 60
                and 0.002 <= atr_fraction <= 0.012
                and not 0 <= hour <= 5
                and quality >= 0.45
                and four.iloc[index]["close"] > four.iloc[index]["ema200"]
            )
            if conditions:
                entry = float(row["low"])
                stop = float(data["low"].iloc[index - 15 : index + 1].min() - 0.20 * atr)
                _append_signal(
                    output,
                    available_at=available_at,
                    module_id="fvg_rt_final_15m",
                    module_version="2.0.0",
                    setup_id="long",
                    side="long",
                    priority=1,
                    entry=entry,
                    stop=stop,
                    max_stop_fraction=0.05,
                    target_r=4.5,
                    ttl_bars=36,
                    max_hold_bars=288,
                    no_progress_bars=48,
                    no_progress_min_r=0.15,
                )
        if bearish:
            fvg_size = (a["low"] - row["high"]) / atr
            four_row = four.iloc[index]
            short_a = (
                0.15 <= fvg_size <= 1.50
                and body_atr >= 0.25
                and b["close"] < b["open"]
                and volume_ratio >= 0.70
                and _strict_bool(four_row["trend_down_3"])
                and _single_candle_short_location(four_row) <= 0.60
                and 0.002 <= atr_fraction <= 0.005
                and quality >= 0.45
                and daily.iloc[index]["close"] < daily.iloc[index]["ema200"]
            )
            if short_a:
                entry = float(row["high"] + 0.25 * (a["low"] - row["high"]))
                stop = float(data["high"].iloc[index - 7 : index + 1].max())
                _append_signal(
                    output,
                    available_at=available_at,
                    module_id="fvg_rt_final_15m",
                    module_version="2.0.0",
                    setup_id="short_a",
                    side="short",
                    priority=1,
                    entry=entry,
                    stop=stop,
                    max_stop_fraction=0.015,
                    target_r=5.0,
                    ttl_bars=48,
                    max_hold_bars=288,
                    no_progress_bars=24,
                    no_progress_min_r=0.35,
                )
            close_strength = (
                (b["high"] - b["close"]) / (b["high"] - b["low"])
                if b["high"] > b["low"]
                else np.nan
            )
            short_b = (
                0.20 <= fvg_size <= 2.00
                and body_atr >= 0.50
                and b["close"] < b["open"]
                and volume_ratio >= 0.70
                and row["close"] < previous_48_low.iloc[index]
                and row["rsi14"] <= 40
                and close_strength >= 0.50
                and location_short_8h.iloc[index] >= 0.50
                and location_short_48h.iloc[index] >= 0.60
                and recent_bullish.iloc[index] == 0
                and 0 <= atr_fraction <= 0.02
                and quality >= 0.45
                and daily.iloc[index]["close"] < daily.iloc[index]["ema200"]
            )
            if short_b:
                entry = float(row["high"])
                stop = float(data["high"].iloc[index - 15 : index + 1].max())
                _append_signal(
                    output,
                    available_at=available_at,
                    module_id="fvg_rt_final_15m",
                    module_version="2.0.0",
                    setup_id="short_b",
                    side="short",
                    priority=1,
                    entry=entry,
                    stop=stop,
                    max_stop_fraction=0.03,
                    target_r=6.0,
                    ttl_bars=48,
                    max_hold_bars=576,
                    no_progress_bars=60,
                    no_progress_min_r=0.35,
                )
    return output


def _strict_bool(value: object) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value)


def _single_candle_short_location(row: pd.Series) -> float:
    candle_range = row["high"] - row["low"]
    return (row["high"] - row["close"]) / candle_range if candle_range > 0 else math.nan


def frozen_1h_signals(frame: pd.DataFrame, frame_4h: pd.DataFrame) -> list[Signal]:
    data = add_indicators(frame)
    availability = data.index + pd.Timedelta(hours=1)
    four = completed_asof(add_indicators(frame_4h), availability)
    short_location = rolling_location(data, 8, short=True)
    output: list[Signal] = []
    for index in range(4, len(data)):
        row = data.iloc[index]
        a = data.iloc[index - 2]
        b = data.iloc[index - 1]
        atr = float(row["atr14"])
        if not math.isfinite(atr) or atr <= 0 or not row["high"] < a["low"]:
            continue
        denominator = (a["high"] - a["low"]) + (b["high"] - b["low"]) + (row["high"] - row["low"])
        quality = abs(row["close"] - a["open"]) / denominator if denominator > 0 else np.nan
        fvg_size = (a["low"] - row["high"]) / atr
        body_atr = abs(b["close"] - b["open"]) / atr
        volume_ratio = b["volume"] / b["volume_sma20"] if b["volume_sma20"] > 0 else np.nan
        conditions = (
            0.08 <= fvg_size <= 1.25
            and body_atr >= 0.40
            and b["close"] < b["open"]
            and volume_ratio >= 0.85
            and quality >= 0.45
            and row["close"] < row["ema50"]
            and row["ema50"] < data.iloc[index - 4]["ema50"]
            and four.iloc[index]["close"] < four.iloc[index]["ema200"]
            and short_location.iloc[index] >= 0.75
            and 0 <= atr / row["close"] <= 0.025
        )
        if not conditions:
            continue
        entry = float(row["high"] + 0.75 * (a["low"] - row["high"]))
        stop = float(data["high"].iloc[index - 4 : index + 1].max() + 0.05 * atr)
        _append_signal(
            output,
            available_at=availability[index],
            module_id="frozen_1h_short",
            module_version="1.0.0",
            setup_id="short",
            side="short",
            priority=2,
            entry=entry,
            stop=stop,
            max_stop_fraction=0.04,
            target_r=6.0,
            ttl_bars=48,
            max_hold_bars=144,
            no_progress_bars=72,
            no_progress_min_r=0.50,
        )
    return output


def frozen_4h_signals(frame: pd.DataFrame) -> list[Signal]:
    data = add_indicators(frame)
    availability = data.index + pd.Timedelta(hours=4)
    short_location = rolling_location(data, 8, short=True)
    output: list[Signal] = []
    for index in range(2, len(data)):
        row = data.iloc[index]
        a = data.iloc[index - 2]
        b = data.iloc[index - 1]
        atr = float(row["atr14"])
        if not math.isfinite(atr) or atr <= 0 or not row["high"] < a["low"]:
            continue
        denominator = (a["high"] - a["low"]) + (b["high"] - b["low"]) + (row["high"] - row["low"])
        quality = abs(row["close"] - a["open"]) / denominator if denominator > 0 else np.nan
        fvg_size = (a["low"] - row["high"]) / atr
        body_atr = abs(b["close"] - b["open"]) / atr
        volume_ratio = b["volume"] / b["volume_sma20"] if b["volume_sma20"] > 0 else np.nan
        conditions = (
            0.05 <= fvg_size <= 1.25
            and body_atr >= 0.15
            and b["close"] < b["open"]
            and volume_ratio >= 1.15
            and quality >= 0.55
            and row["rsi14"] <= 40
            and short_location.iloc[index] >= 0.35
            and 0 <= atr / row["close"] <= 0.025
        )
        if not conditions:
            continue
        entry = float(row["high"] + 0.50 * (a["low"] - row["high"]))
        stop = float(data["high"].iloc[index - 1 : index + 1].max() + 0.05 * atr)
        _append_signal(
            output,
            available_at=availability[index],
            module_id="frozen_4h_short",
            module_version="1.0.0",
            setup_id="short",
            side="short",
            priority=3,
            entry=entry,
            stop=stop,
            max_stop_fraction=0.08,
            target_r=6.0,
            ttl_bars=576,
            max_hold_bars=576,
            no_progress_bars=432,
            no_progress_min_r=0.35,
        )
    return output


def build_signals(frame_5m: pd.DataFrame) -> list[Signal]:
    frame_15m = aggregate(frame_5m, "15min", 3)
    frame_1h = aggregate(frame_5m, "1h", 12)
    frame_4h = aggregate(frame_5m, "4h", 48)
    frame_1d = aggregate(frame_5m, "1D", 288)
    signals = [
        *final_15m_signals(frame_15m, frame_4h, frame_1d),
        *frozen_1h_signals(frame_1h, frame_4h),
        *frozen_4h_signals(frame_4h),
    ]
    setup_priority = {"long": 0, "short_a": 1, "short_b": 2, "short": 0}
    signals.sort(
        key=lambda signal: (
            signal.available_at,
            signal.priority,
            setup_priority[signal.setup_id],
            signal.module_id,
            signal.signal_key,
        )
    )
    return signals
