from datetime import UTC, datetime, timedelta

import pytest

from modular_core import Candle


@pytest.fixture(autouse=True)
def install_risk_feature_view_compat(request: pytest.FixtureRequest) -> None:
    """Keep the legacy Risk Sizing test view compatible with frozen Core 0.4.2."""
    fake_view_type = getattr(request.module, "FakeFeatureView", None)
    if fake_view_type is None or hasattr(fake_view_type, "get_all"):
        return

    def get_all(self: object, key: str, **filters: object) -> tuple[object, ...]:
        del key, filters
        feature = getattr(self, "feature", None)
        return () if feature is None else (feature,)

    setattr(fake_view_type, "get_all", get_all)


@pytest.fixture
def bars_factory():
    def make(prices):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        bars = []
        for i, item in enumerate(prices):
            if isinstance(item, (int, float)):
                o = c = float(item)
                h = o + 1
                low = o - 1
            else:
                o, h, low, c = map(float, item)
            bars.append(
                Candle(start + timedelta(minutes=5 * i), o, h, low, c, 1.0, "BTCUSDT", "5m")
            )
        return bars

    return make
