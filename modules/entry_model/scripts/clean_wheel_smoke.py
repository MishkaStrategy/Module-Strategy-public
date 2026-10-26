from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points

from modular_core import Candle, Feature, FeatureView, MarketView

from modular_modules.entry_model import ENTRY_PLAN_FEATURE_KEY, EntryModelModule, EntryPlan


def main() -> None:
    discovered = {point.name: point for point in entry_points(group="modular_strategy.modules")}
    assert "entry_model" in discovered
    loaded = discovered["entry_model"].load()
    assert loaded is EntryModelModule

    bar = Candle(datetime(2026, 7, 25, tzinfo=UTC), 100, 101, 99, 100, 1, "BTCUSDT", "5m")
    setup = Feature(
        key="setup.confirmed",
        source_module="clean_smoke",
        created_at=bar.close_time,
        value={
            "setup_id": "clean-smoke-1",
            "status": "confirmed",
            "side": "buy",
            "confirmed_at": bar.close_time.isoformat(),
            "confirmation_price": "100",
            "extreme_price": "90",
            "entry_kind": "limit_retracement",
        },
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=bar.close_time,
        schema_version="1",
        feature_id="clean-smoke-source-1",
    )
    view = MarketView(
        base_bars=(bar,),
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        features=FeatureView((setup,), bar.close_time),
    )
    module = EntryModelModule()
    decision = module.on_bar(view)
    plans = [feature for feature in decision.features if feature.key == ENTRY_PLAN_FEATURE_KEY]
    assert len(plans) == 1
    plan = EntryPlan.from_feature_value(plans[0].value)
    assert str(plan.price) == "96.18"

    snapshot = module.snapshot_state()
    restored = EntryModelModule()
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    print(
        {
            "plan_id": plan.plan_id,
            "price": str(plan.price),
            "state_schema": snapshot["state_schema_version"],
        }
    )


if __name__ == "__main__":
    main()
