from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from modular_core import Candle, Feature, FeatureView, MarketView

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def make_bar(
    index: int = 0,
    *,
    open_price: float = 100.0,
    high: float = 102.0,
    low: float = 98.0,
    close: float = 100.0,
    volume: float = 10.0,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
) -> Candle:
    return Candle(
        timestamp=BASE_TIME + timedelta(minutes=5 * index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        symbol=symbol,
        timeframe=timeframe,
    )


def make_entry_feature(
    bar: Candle,
    *,
    plan_id: str = "entry-1",
    setup_id: str = "setup-1",
    side: str = "buy",
    entry_price: object = "100",
    invalidation_price: object | None = "99",
    target_price: object | None = None,
    created_at: datetime | None = None,
    valid_until: datetime | None = None,
    feature_id: str | None = None,
    extra: Mapping[str, object] | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    schema_version: str = "1",
    source_module: str = "entry_model",
) -> Feature:
    created = created_at or bar.close_time
    payload: dict[str, object] = {
        "schema_version": "1",
        "entry_plan_id": plan_id,
        "setup_id": setup_id,
        "symbol": symbol or bar.symbol,
        "timeframe": timeframe or bar.timeframe,
        "side": side,
        "entry_price": entry_price,
        "created_at": created.isoformat(),
    }
    if invalidation_price is not None:
        payload["invalidation_price"] = invalidation_price
    if target_price is not None:
        payload["target_price"] = target_price
    if valid_until is not None:
        payload["valid_until"] = valid_until.isoformat()
    if extra:
        payload.update(extra)
    return Feature(
        key="entry_model.entry_plan",
        source_module=source_module,
        created_at=created,
        value=payload,
        symbol=symbol or bar.symbol,
        timeframe=timeframe or bar.timeframe,
        valid_until=valid_until,
        schema_version=schema_version,
        feature_id=feature_id or f"{plan_id}:feature",
        invalidate_on_gap=True,
    )


def make_structural_feature(
    bar: Candle,
    *,
    key: str,
    price: object,
    feature_id: str,
    setup_id: str | None = None,
    field: str = "price",
    timeframe: str | None = None,
    created_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> Feature:
    value: dict[str, object] = {field: price}
    if setup_id is not None:
        value["setup_id"] = setup_id
    created = created_at or bar.close_time
    return Feature(
        key=key,
        source_module="structure",
        created_at=created,
        value=value,
        symbol=bar.symbol,
        timeframe=timeframe,
        valid_until=valid_until or bar.close_time,
        feature_id=feature_id,
    )


def make_view(
    bar: Candle,
    *features: Feature,
    data_gap: bool = False,
    history: tuple[Candle, ...] | None = None,
    include_feature_view: bool = True,
    feature_timestamp: datetime | None = None,
) -> MarketView:
    bars = history or (bar,)
    feature_view = (
        FeatureView(features, feature_timestamp or bar.close_time) if include_feature_view else None
    )
    return MarketView(
        base_bars=bars,
        timeframe_bars={},
        index=len(bars) - 1,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
        features=feature_view,
    )


@pytest.fixture
def bar() -> Candle:
    return make_bar()


@pytest.fixture
def entry_feature(bar: Candle) -> Feature:
    return make_entry_feature(bar)


@pytest.fixture
def view(bar: Candle, entry_feature: Feature) -> MarketView:
    return make_view(bar, entry_feature)
