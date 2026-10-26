from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from modular_core.models import Candle, Side
from modular_core.data import timeframe_to_seconds


class EntryType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class SignalStatus(str, Enum):
    DETECTED = "detected"
    VALIDATED = "validated"
    RENDERING = "rendering"
    READY = "ready"
    SENDING = "sending"
    SENT = "sent"
    SEND_FAILED = "send_failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    symbol: str
    market: str
    tick_size: Decimal
    step_size: Decimal = Decimal("0")
    min_price: Decimal | None = None
    min_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.market:
            raise ValueError("instrument symbol and market are required")
        if self.tick_size <= 0 or self.step_size < 0:
            raise ValueError("tick_size must be positive and step_size non-negative")


@dataclass(frozen=True, slots=True)
class CandleBatch:
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if not self.candles:
            raise ValueError("candle batch cannot be empty")
        if any(c.symbol != self.symbol or c.timeframe != self.timeframe for c in self.candles):
            raise ValueError("candle batch identity mismatch")
        if any(a.timestamp >= b.timestamp for a, b in zip(self.candles, self.candles[1:])):
            raise ValueError("candles must be strictly ordered without duplicates")
        interval = timeframe_to_seconds(self.timeframe)
        if interval is None:
            raise ValueError("unsupported candle timeframe")
        if any(
            (b.timestamp - a.timestamp).total_seconds() != interval
            for a, b in zip(self.candles, self.candles[1:])
        ):
            raise ValueError("candle batch contains a gap")
        if self.candles[-1].close_time > self.observed_at.astimezone(timezone.utc):
            raise ValueError("batch contains an open candle")


@dataclass(frozen=True, slots=True)
class StrategyContext:
    instrument: InstrumentRules
    bars: Mapping[str, CandleBatch]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", MappingProxyType(dict(self.bars)))


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    side: Side | None = None
    signal_timestamp: datetime | None = None
    signal_timeframe: str | None = None
    entry_type: EntryType | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit_levels: tuple[Decimal, ...] = ()
    invalidation_price: Decimal | None = None
    reason_codes: tuple[str, ...] = ()
    human_readable_reason: str = ""
    expires_at: datetime | None = None
    setup_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def has_signal(self) -> bool:
        return self.side is not None


@dataclass(frozen=True, slots=True)
class TradeSignal:
    signal_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    market: str
    side: Side
    signal_timestamp: datetime
    signal_timeframe: str
    entry_type: EntryType
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_levels: tuple[Decimal, ...]
    invalidation_price: Decimal | None
    risk_reward: tuple[Decimal, ...]
    reason_codes: tuple[str, ...]
    human_readable_reason: str
    expires_at: datetime | None
    setup_id: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    chart_enabled: bool = True
    source: str = "standalone"
    contract_version: str = "1.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @staticmethod
    def stable_id(
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        side: Side,
        timeframe: str,
        timestamp: datetime,
        entry: Decimal,
        setup_id: str | None,
    ) -> str:
        payload = [
            strategy_id,
            strategy_version,
            symbol,
            side.value,
            timeframe,
            timestamp.astimezone(timezone.utc).isoformat(),
            str(entry),
            setup_id or "",
        ]
        digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
        return f"sig_{digest[:32]}"


def decimal_places(step: Decimal) -> int:
    exponent = step.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def signal_to_dict(signal: TradeSignal) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "symbol": signal.symbol,
        "market": signal.market,
        "side": signal.side.value,
        "signal_timestamp": signal.signal_timestamp.isoformat(),
        "signal_timeframe": signal.signal_timeframe,
        "entry_type": signal.entry_type.value,
        "entry_price": str(signal.entry_price),
        "stop_loss": str(signal.stop_loss),
        "take_profit_levels": [str(value) for value in signal.take_profit_levels],
        "invalidation_price": (
            str(signal.invalidation_price) if signal.invalidation_price is not None else None
        ),
        "risk_reward": [str(value) for value in signal.risk_reward],
        "reason_codes": list(signal.reason_codes),
        "human_readable_reason": signal.human_readable_reason,
        "expires_at": signal.expires_at.isoformat() if signal.expires_at else None,
        "setup_id": signal.setup_id,
        "metadata": dict(signal.metadata),
        "chart_enabled": signal.chart_enabled,
        "source": signal.source,
        "contract_version": signal.contract_version,
    }


def _strict_boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def signal_from_dict(value: Mapping[str, Any]) -> TradeSignal:
    return TradeSignal(
        signal_id=str(value["signal_id"]),
        strategy_id=str(value["strategy_id"]),
        strategy_version=str(value["strategy_version"]),
        symbol=str(value["symbol"]),
        market=str(value["market"]),
        side=Side(str(value["side"])),
        signal_timestamp=datetime.fromisoformat(str(value["signal_timestamp"])),
        signal_timeframe=str(value["signal_timeframe"]),
        entry_type=EntryType(str(value["entry_type"])),
        entry_price=Decimal(str(value["entry_price"])),
        stop_loss=Decimal(str(value["stop_loss"])),
        take_profit_levels=tuple(Decimal(str(v)) for v in value["take_profit_levels"]),
        invalidation_price=(
            Decimal(str(value["invalidation_price"]))
            if value.get("invalidation_price") is not None
            else None
        ),
        risk_reward=tuple(Decimal(str(v)) for v in value["risk_reward"]),
        reason_codes=tuple(str(v) for v in value["reason_codes"]),
        human_readable_reason=str(value["human_readable_reason"]),
        expires_at=(
            datetime.fromisoformat(str(value["expires_at"])) if value.get("expires_at") else None
        ),
        setup_id=str(value["setup_id"]) if value.get("setup_id") else None,
        metadata=dict(value.get("metadata", {})),
        chart_enabled=_strict_boolean(value.get("chart_enabled", True), "chart_enabled"),
        source=str(value.get("source", "standalone")),
        contract_version=str(value.get("contract_version", "1.0")),
    )
