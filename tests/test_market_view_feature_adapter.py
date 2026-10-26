from datetime import datetime, timezone

import pytest

from modular_core import Candle, MarketView

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR = Candle(NOW, 100, 101, 99, 100, 1, "X", "5m")


class CompatibleFeatureAdapter:
    def get_all(self, *_args, **_kwargs):
        return ()


def test_market_view_accepts_duck_typed_feature_adapter() -> None:
    view = MarketView(
        base_bars=(BAR,),
        timeframe_bars={},
        index=0,
        cash=1000,
        equity=1000,
        position=None,
        pending_orders=(),
        features=CompatibleFeatureAdapter(),  # type: ignore[arg-type]
    )
    assert view.features is not None
    assert view.features.get_all("test.value") == ()


def test_market_view_rejects_object_without_feature_access() -> None:
    with pytest.raises(TypeError, match="provide get_all"):
        MarketView(
            base_bars=(BAR,),
            timeframe_bars={},
            index=0,
            cash=1000,
            equity=1000,
            position=None,
            pending_orders=(),
            features=object(),  # type: ignore[arg-type]
        )
