from datetime import datetime, timezone

import pytest

from modular_core import Candle, Feature, MarketView, ModuleDecision, StrategyModule
from modular_core.module_api import normalize_modules

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class NoopModule(StrategyModule):
    name = "noop"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def _bar(close: float = 100.0) -> Candle:
    return Candle(NOW, close, close + 1, close - 1, close, 1, "X", "5m")


def test_market_view_copies_caller_owned_collections() -> None:
    base = [_bar()]
    higher = [_bar(101)]
    timeframes = {"15m": higher}
    pending: list[object] = []
    tape = [{"price": 100}]

    view = MarketView(
        base_bars=base,
        timeframe_bars=timeframes,
        index=0,
        cash=1000,
        equity=1000,
        position=None,
        pending_orders=pending,  # type: ignore[arg-type]
        trade_tape=tape,  # type: ignore[arg-type]
    )

    base.append(_bar(102))
    higher.append(_bar(103))
    timeframes["1h"] = [_bar(104)]
    pending.append(object())
    tape.append({"price": 101})

    assert len(view.base_bars) == 1
    assert tuple(view.timeframe_bars) == ("15m",)
    assert len(view.timeframe_bars["15m"]) == 1
    assert view.pending_orders == ()
    assert len(view.trade_tape) == 1


def test_market_view_rejects_malformed_collections_and_numeric_coercion() -> None:
    with pytest.raises(TypeError, match="base_bars must contain Candle"):
        MarketView(
            base_bars=[object()],  # type: ignore[list-item]
            timeframe_bars={},
            index=0,
            cash=1000,
            equity=1000,
            position=None,
            pending_orders=(),
        )
    with pytest.raises(TypeError, match="cash must be numeric"):
        MarketView(
            base_bars=(_bar(),),
            timeframe_bars={},
            index=0,
            cash="1000",  # type: ignore[arg-type]
            equity=1000,
            position=None,
            pending_orders=(),
        )


def test_module_decision_copies_sequences_and_deep_freezes_diagnostics() -> None:
    feature = Feature("test.value", "noop", NOW, {"value": 1})
    features = [feature]
    skips = ["not_ready"]
    nested = [1]
    diagnostics = {"nested": nested}

    decision = ModuleDecision(
        features=features,  # type: ignore[arg-type]
        skips=skips,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )

    features.clear()
    skips.append("changed")
    nested.append(2)
    diagnostics["new"] = True

    assert decision.features == (feature,)
    assert decision.skips == ("not_ready",)
    assert decision.diagnostics == {"nested": (1,)}


def test_module_decision_rejects_wrong_item_types_and_blank_keys() -> None:
    with pytest.raises(TypeError, match="orders must contain OrderRequest"):
        ModuleDecision(orders=[object()])  # type: ignore[list-item,arg-type]
    with pytest.raises(ValueError, match="diagnostic key is required"):
        ModuleDecision(diagnostics={" ": True})


def test_normalize_modules_rejects_non_modules_and_non_string_names() -> None:
    with pytest.raises(TypeError, match="implement StrategyModule"):
        normalize_modules([object()])  # type: ignore[list-item]
    module = NoopModule()
    module.name = 1  # type: ignore[assignment]
    with pytest.raises(TypeError, match="module name must be str"):
        normalize_modules(module)
