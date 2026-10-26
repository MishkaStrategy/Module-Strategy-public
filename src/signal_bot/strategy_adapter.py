from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from modular_core.module_api import ModuleDecision, StrategyModule

from .domain import EntryType, StrategyContext, StrategyDecision


class ModuleSignalAdapter:
    """Adapts Module API decisions that explicitly publish executable levels.

    Analytical signals without ``entry_price`` and ``stop_loss`` remain valid
    core events but are deliberately not deliverable trading signals.
    """

    def __init__(self, module: StrategyModule) -> None:
        if getattr(module, "signal_delivery_ready", False) is not True:
            raise ValueError("module does not declare signal_delivery_ready capability")
        if getattr(module, "portfolio_independent", False) is not True:
            raise ValueError("portfolio-sensitive module cannot run in Signal Bot")
        if not callable(getattr(module, "evaluate_delivery", None)):
            raise ValueError("delivery-ready module must implement evaluate_delivery")
        self.module = module
        manifest = module.manifest
        if manifest is None:
            raise ValueError("delivery-ready module requires a manifest")
        self.strategy_id = module.name
        self.version = manifest.version if manifest else "0.1.0"
        required = manifest.required_timeframes if manifest else ()
        self.required_timeframes = tuple(required) or ("5m",)

    def warmup_bars(self, timeframe: str) -> int:
        manifest = self.module.manifest
        return int(manifest.warmup_bars.get(timeframe, 0)) if manifest else 0

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        primary = context.bars[self.required_timeframes[0]]
        result = self.module.evaluate_delivery(context)  # type: ignore[attr-defined]
        if not isinstance(result, ModuleDecision):
            raise TypeError("Module API adapter requires ModuleDecision")
        if not result.signals:
            return StrategyDecision()
        if len(result.signals) != 1:
            raise ValueError("a module evaluation may expose only one deliverable signal")
        raw = result.signals[0]
        data: Mapping[str, Any] = raw.metadata
        required = {"entry_type", "entry_price", "stop_loss"}
        missing = sorted(required - set(data))
        if missing:
            raise ValueError(
                "analytical Module API signal lacks delivery levels: " + ", ".join(missing)
            )
        timestamp = data.get("signal_timestamp", primary.candles[-1].timestamp)
        if not isinstance(timestamp, datetime):
            timestamp = datetime.fromisoformat(str(timestamp))
        expiry = data.get("expires_at")
        if expiry is not None and not isinstance(expiry, datetime):
            expiry = datetime.fromisoformat(str(expiry))
        return StrategyDecision(
            side=raw.side,
            signal_timestamp=timestamp,
            signal_timeframe=str(data.get("signal_timeframe", primary.timeframe)),
            entry_type=EntryType(str(data["entry_type"])),
            entry_price=Decimal(str(data["entry_price"])),
            stop_loss=Decimal(str(data["stop_loss"])),
            take_profit_levels=tuple(Decimal(str(v)) for v in data.get("take_profit_levels", ())),
            invalidation_price=(
                Decimal(str(data["invalidation_price"]))
                if data.get("invalidation_price") is not None
                else None
            ),
            reason_codes=tuple(str(v) for v in data.get("reason_codes", (raw.reason,))),
            human_readable_reason=str(data.get("human_readable_reason", raw.reason)),
            expires_at=expiry,
            setup_id=str(data["setup_id"]) if data.get("setup_id") else None,
            metadata={key: value for key, value in data.items() if key not in required},
        )
