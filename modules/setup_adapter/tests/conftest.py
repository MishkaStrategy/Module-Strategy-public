from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_core import Candle, Feature, FeatureView, MarketView


def bar(index: int = 0, *, close: float = 100.9) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=100.8,
        high=max(101.1, close),
        low=min(100.7, close),
        close=close,
        volume=2.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def breakout_feature(
    current: Candle,
    *,
    feature_id: str = "br-1:confirmed",
    side: str = "sell",
    return_close: object = "100.9",
    extreme: object = "103",
    created_at=None,
    schema_version: str = "1",
    source_module: str = "breakout_return",
    symbol: str = "BTCUSDT",
    payload_overrides: dict[str, object] | None = None,
) -> Feature:
    confirmation_time = created_at or current.close_time
    direction = "above" if side == "sell" else "below"
    payload: dict[str, object] = {
        "setup_id": "br-BTCUSDT-5m-above-1",
        "breakout_direction": direction,
        "signal_side": side,
        "range_high": 101.0,
        "range_low": 99.0,
        "range_width": 2.0,
        "reference_id": "ny-range",
        "reference_source": "feature:session_range.completed:session_range",
        "reference_created_at": current.close_time.isoformat(),
        "breakout_time": (current.close_time - timedelta(minutes=5)).isoformat(),
        "breakout_extreme": extreme,
        "symbol": symbol,
        "timeframe": "5m",
        "feature_revision": 0,
        "confirmation_time": confirmation_time.isoformat(),
        "return_open": 100.8,
        "return_close": return_close,
        "return_depth": 0.05,
        "bars_to_return": 1,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return Feature(
        key="breakout_return.confirmed",
        source_module=source_module,
        created_at=created_at or current.close_time,
        value=payload,
        symbol=symbol,
        timeframe="5m",
        valid_until=created_at or current.close_time,
        schema_version=schema_version,
        feature_id=feature_id,
    )


def view(current: Candle, features: tuple[Feature, ...] = ()) -> MarketView:
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=0,
        cash=1000.0,
        equity=1000.0,
        position=None,
        pending_orders=(),
        features=FeatureView(features, current.close_time),
    )
