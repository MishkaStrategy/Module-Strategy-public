from modular_core import MarketView


def current_regime(view: MarketView) -> str | None:
    if view.features is None:
        return None
    value = view.features.get(
        "market_regime.current",
        symbol=view.current.symbol,
        timeframe="1h",
    )
    return None if value is None else str(value["regime"])
