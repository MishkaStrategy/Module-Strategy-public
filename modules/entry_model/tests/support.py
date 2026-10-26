from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from modular_core import Candle, Feature, FeatureView, MarketView

BASE_OPEN = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def bar_at(
    index: int = 0,
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    open_price: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: float = 1.0,
    timestamp: datetime | None = None,
) -> Candle:
    return Candle(
        timestamp or BASE_OPEN + timedelta(minutes=5 * index),
        open_price,
        high,
        low,
        close,
        volume,
        symbol,
        timeframe,
    )


def payload(
    *,
    setup_id: str = "setup-1",
    side: str = "buy",
    confirmed_at: datetime | None = None,
    confirmation_price: Any = "100",
    extreme_price: Any = "90",
    entry_kind: str | None = "limit_retracement",
    symbol: str = "BTCUSDT",
    status: str = "confirmed",
    entry_price: Any = None,
    trigger_price: Any = None,
    limit_price: Any = None,
    entry_cutoff_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    entry_hint: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "setup_id": setup_id,
        "status": status,
        "symbol": symbol,
        "side": side,
        "confirmed_at": (confirmed_at or (BASE_OPEN + timedelta(minutes=5))).isoformat(),
        "confirmation_price": confirmation_price,
        "metadata": dict(metadata or {}),
    }
    if entry_kind is not None:
        result["entry_kind"] = entry_kind
    if extreme_price is not None:
        result["extreme_price"] = extreme_price
    if entry_price is not None:
        result["entry_price"] = entry_price
    if trigger_price is not None:
        result["trigger_price"] = trigger_price
    if limit_price is not None:
        result["limit_price"] = limit_price
    if entry_cutoff_at is not None:
        result["entry_cutoff_at"] = entry_cutoff_at.isoformat()
    if entry_hint is not None:
        result["entry_hint"] = dict(entry_hint)
    if extra:
        result.update(extra)
    return result


def setup_feature(
    *,
    value: Any | None = None,
    index: int = 0,
    feature_id: str | None = "feature-1",
    created_at: datetime | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    schema_version: str = "1",
    source_module: str = "setup_source",
    metadata: Mapping[str, Any] | None = None,
    valid_until: datetime | None = None,
) -> Feature:
    bar = bar_at(index, symbol=symbol, timeframe=timeframe)
    publication_time = created_at or bar.close_time
    return Feature(
        key="setup.confirmed",
        source_module=source_module,
        created_at=created_at or bar.close_time,
        value=value if value is not None else payload(confirmed_at=bar.close_time),
        symbol=symbol,
        timeframe=timeframe,
        valid_until=valid_until or publication_time,
        schema_version=schema_version,
        feature_id=feature_id,
        metadata=dict(metadata or {}),
    )


def view_at(
    index: int = 0,
    *,
    features: Iterable[Feature] = (),
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    data_gap: bool = False,
    feature_view: bool = True,
    bar: Candle | None = None,
) -> MarketView:
    current = bar or bar_at(index, symbol=symbol, timeframe=timeframe)
    fv = FeatureView(tuple(features), current.close_time) if feature_view else None
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=index,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
        features=fv,
    )


def next_feature(
    index: int,
    *,
    setup_id: str | None = None,
    side: str = "buy",
    entry_kind: str = "limit_retracement",
    feature_id: str | None = None,
    **kwargs: Any,
) -> Feature:
    bar = bar_at(index)
    payload_kwargs: dict[str, Any] = {
        "setup_id": setup_id or f"setup-{index}",
        "side": side,
        "confirmed_at": bar.close_time,
        "entry_kind": entry_kind,
        "extreme_price": "90" if side == "buy" else "110",
    }
    payload_kwargs.update(kwargs)
    value = payload(**payload_kwargs)
    return setup_feature(
        value=value,
        index=index,
        feature_id=feature_id if feature_id is not None else f"feature-{index}",
    )
