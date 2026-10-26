from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from modular_core import (
    BacktestEngine,
    Candle,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from modular_modules.entry_model import ENTRY_PLAN_FEATURE_KEY, SETUP_FEATURE_KEY, EntryModelModule


class CanonicalSetupProducer(StrategyModule):
    name = "example_setup_source"
    manifest = ModuleManifest(
        name=name,
        version="1.0.0-example",
        api_version="1.0",
        produced_features=(SETUP_FEATURE_KEY,),
        skip_until_ready=False,
    )
    parameters: Mapping[str, object] = {}

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index != 0:
            return ModuleDecision()
        bar = view.current
        return ModuleDecision(
            features=(
                Feature(
                    key=SETUP_FEATURE_KEY,
                    source_module=self.name,
                    created_at=bar.close_time,
                    value={
                        "setup_id": "example-buy-1",
                        "status": "confirmed",
                        "symbol": bar.symbol,
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
                    feature_id="example-setup-feature-1",
                ),
            )
        )


def main() -> None:
    candle = Candle(datetime(2026, 7, 25, tzinfo=UTC), 100, 101, 99, 100, 1, "BTCUSDT", "5m")
    result = BacktestEngine((EntryModelModule(), CanonicalSetupProducer())).run((candle,))
    plans = [
        row["feature"]
        for row in result.journal.features
        if row["action"] == "published" and row["feature"].key == ENTRY_PLAN_FEATURE_KEY
    ]
    assert len(plans) == 1
    print(plans[0].value)


if __name__ == "__main__":
    main()
