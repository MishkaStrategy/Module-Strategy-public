from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from modular_core import Feature, MarketView, ModuleDecision, ModuleManifest, StrategyModule
from strategy_modules.risk_sizing import InstrumentRiskRules, PortfolioRiskSnapshot

MODULE_NAME = "runtime_risk_context"
MODULE_VERSION = "0.1.0"
MODULE_API_VERSION = "1.0"
FEATURE_SCHEMA_VERSION = "1"
INSTRUMENT_RULES_FEATURE = "instrument.rules"
PORTFOLIO_RISK_FEATURE = "portfolio.risk_snapshot"

PARAMETER_SCHEMA: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "instrument_rules": MappingProxyType({"default": None}),
    }
)

MANIFEST = ModuleManifest(
    name=MODULE_NAME,
    version=MODULE_VERSION,
    api_version=MODULE_API_VERSION,
    required_timeframes=(),
    required_features=(),
    optional_features=(),
    produced_features=(INSTRUMENT_RULES_FEATURE, PORTFOLIO_RISK_FEATURE),
    warmup_bars={},
    parameters_schema=PARAMETER_SCHEMA,
    skip_until_ready=False,
    description=(
        "Owner-bound instrument rules plus factual current portfolio risk context "
        "for deterministic risk-request composition."
    ),
)


class RuntimeRiskContextError(RuntimeError):
    """Raised when truthful runtime risk context cannot be produced."""


class RuntimeRiskContextModule(StrategyModule):
    """Publish the exact risk context consumed by ``RiskRequestComposerModule``.

    Instrument rules are immutable constructor state supplied by the authoritative
    runtime owner. Portfolio facts are derived only from the current Core
    ``MarketView``. The module never invents a second portfolio state owner.
    """

    name = MODULE_NAME
    manifest = MANIFEST

    def __init__(self, *, instrument_rules: Mapping[str, Any] | None = None) -> None:
        if instrument_rules is None:
            raise ValueError("instrument_rules are required")
        if not isinstance(instrument_rules, Mapping):
            raise TypeError("instrument_rules must be a mapping")
        try:
            rules = InstrumentRiskRules.from_mapping(instrument_rules)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid instrument_rules: {exc}") from exc
        self._instrument = rules
        self._instrument_mapping = MappingProxyType(dict(rules.to_mapping()))
        self.parameters = MappingProxyType({"instrument_rules": self._instrument_mapping})

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not isinstance(view, MarketView):
            raise TypeError("view must be MarketView")
        current = view.current
        portfolio = self._portfolio_snapshot(view)
        created_at = current.close_time
        symbol = current.symbol
        return ModuleDecision(
            features=(
                Feature(
                    key=INSTRUMENT_RULES_FEATURE,
                    source_module=self.name,
                    created_at=created_at,
                    value=self._instrument_mapping,
                    symbol=symbol,
                    timeframe=current.timeframe,
                    schema_version=FEATURE_SCHEMA_VERSION,
                ),
                Feature(
                    key=PORTFOLIO_RISK_FEATURE,
                    source_module=self.name,
                    created_at=created_at,
                    value=portfolio.to_mapping(),
                    symbol=symbol,
                    timeframe=current.timeframe,
                    schema_version=FEATURE_SCHEMA_VERSION,
                ),
            ),
            diagnostics={
                "context_source": "owner_bound_instrument+core_market_view",
                "position_open": view.position is not None,
            },
        )

    def _portfolio_snapshot(self, view: MarketView) -> PortfolioRiskSnapshot:
        position = view.position
        if position is None:
            return PortfolioRiskSnapshot()
        if position.stop_loss is None:
            raise RuntimeRiskContextError(
                "open position has no stop_loss; portfolio risk is not measurable"
            )
        quantity = _decimal(position.quantity, "position.quantity")
        entry_price = _decimal(position.entry_price, "position.entry_price")
        stop_loss = _decimal(position.stop_loss, "position.stop_loss")
        current_price = _decimal(view.current.close, "current.close")
        contract_size = self._instrument.contract_size
        current_notional = quantity * current_price * contract_size
        open_risk = abs(entry_price - stop_loss) * quantity * contract_size
        return PortfolioRiskSnapshot(
            gross_notional=current_notional,
            symbol_notional=current_notional,
            open_risk_amount=open_risk,
            open_positions=1,
        )


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeRiskContextError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeRiskContextError(f"{name} is not a valid decimal") from exc
    if not result.is_finite() or result <= 0:
        raise RuntimeRiskContextError(f"{name} must be finite and positive")
    return result


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "INSTRUMENT_RULES_FEATURE",
    "MODULE_API_VERSION",
    "MODULE_NAME",
    "MODULE_VERSION",
    "PORTFOLIO_RISK_FEATURE",
    "RuntimeRiskContextError",
    "RuntimeRiskContextModule",
]
