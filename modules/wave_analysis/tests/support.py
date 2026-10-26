from __future__ import annotations

from collections.abc import Mapping, Sequence

from modular_core import Candle, FeatureView, MarketView


def market_view(
    base_bars: Sequence[Candle],
    timeframe_bars: Mapping[str, Sequence[Candle]],
    index: int,
    *,
    data_gap: bool = False,
    features: FeatureView | None = None,
) -> MarketView:
    """Build a full Module API v1 MarketView for module-level tests."""
    return MarketView(
        base_bars=base_bars,
        timeframe_bars=timeframe_bars,
        index=index,
        cash=0.0,
        equity=0.0,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
        features=features,
    )
