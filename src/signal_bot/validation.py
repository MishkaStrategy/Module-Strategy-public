from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from modular_core.data import timeframe_to_seconds

from .domain import InstrumentRules, StrategyDecision, TradeSignal


class SignalValidationError(ValueError):
    pass


def _on_tick(value: Decimal, tick: Decimal) -> bool:
    return value.is_finite() and value > 0 and value % tick == 0


def validate_decision(
    decision: StrategyDecision,
    *,
    strategy_id: str,
    strategy_version: str,
    instrument: InstrumentRules,
    evaluated_at: datetime,
    last_closed_at: datetime,
) -> TradeSignal:
    if (
        decision.side is None
        or decision.signal_timestamp is None
        or decision.signal_timeframe is None
        or decision.entry_type is None
        or decision.entry_price is None
        or decision.stop_loss is None
    ):
        raise SignalValidationError(
            "confirmed signal is missing a required level or identity field"
        )
    risk = abs(decision.entry_price - decision.stop_loss)
    if risk == 0:
        raise SignalValidationError("entry and stop cannot be equal")
    rewards = tuple(
        abs(target - decision.entry_price) / risk for target in decision.take_profit_levels
    )
    signal = TradeSignal(
        signal_id=TradeSignal.stable_id(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=instrument.symbol,
            side=decision.side,
            timeframe=decision.signal_timeframe,
            timestamp=decision.signal_timestamp,
            entry=decision.entry_price,
            setup_id=decision.setup_id,
        ),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=instrument.symbol,
        market=instrument.market,
        side=decision.side,
        signal_timestamp=decision.signal_timestamp,
        signal_timeframe=decision.signal_timeframe,
        entry_type=decision.entry_type,
        entry_price=decision.entry_price,
        stop_loss=decision.stop_loss,
        take_profit_levels=decision.take_profit_levels,
        invalidation_price=decision.invalidation_price,
        risk_reward=rewards,
        reason_codes=decision.reason_codes,
        human_readable_reason=decision.human_readable_reason,
        expires_at=decision.expires_at,
        setup_id=decision.setup_id,
        metadata=decision.metadata,
    )
    return validate_signal(
        signal,
        instrument=instrument,
        evaluated_at=evaluated_at,
        last_closed_at=last_closed_at,
    )


def validate_signal(
    signal: TradeSignal,
    *,
    instrument: InstrumentRules,
    evaluated_at: datetime,
    last_closed_at: datetime,
) -> TradeSignal:
    if evaluated_at.tzinfo is None or last_closed_at.tzinfo is None:
        raise SignalValidationError("validation timestamps must be timezone-aware")
    if signal.symbol != instrument.symbol or signal.market != instrument.market:
        raise SignalValidationError("signal instrument identity does not match current rules")
    if not signal.strategy_id or not signal.strategy_version:
        raise SignalValidationError("strategy identity is required")
    if signal.contract_version != "1.0" or signal.source not in {"standalone", "core"}:
        raise SignalValidationError("unsupported signal source or contract version")
    if not isinstance(signal.chart_enabled, bool):
        raise SignalValidationError("chart_enabled must be a boolean")

    for name, value in (
        ("entry", signal.entry_price),
        ("stop", signal.stop_loss),
        *(("target", target) for target in signal.take_profit_levels),
        *(
            (("invalidation", signal.invalidation_price),)
            if signal.invalidation_price is not None
            else ()
        ),
    ):
        if not _on_tick(value, instrument.tick_size):
            raise SignalValidationError(f"{name} must be finite, positive and aligned to tick_size")
    if len(set(signal.take_profit_levels)) != len(signal.take_profit_levels):
        raise SignalValidationError("take-profit levels must be unique")
    if instrument.min_price is not None and signal.entry_price < instrument.min_price:
        raise SignalValidationError("entry is below instrument min_price")

    risk = abs(signal.entry_price - signal.stop_loss)
    if risk == 0:
        raise SignalValidationError("entry and stop cannot be equal")
    if signal.side.value == "buy":
        if signal.stop_loss >= signal.entry_price:
            raise SignalValidationError("long stop must be below entry")
        if any(target <= signal.entry_price for target in signal.take_profit_levels):
            raise SignalValidationError("long targets must be above entry")
        if (
            signal.invalidation_price is not None
            and signal.invalidation_price >= signal.entry_price
        ):
            raise SignalValidationError("long invalidation must be below entry")
    else:
        if signal.stop_loss <= signal.entry_price:
            raise SignalValidationError("short stop must be above entry")
        if any(target >= signal.entry_price for target in signal.take_profit_levels):
            raise SignalValidationError("short targets must be below entry")
        if (
            signal.invalidation_price is not None
            and signal.invalidation_price <= signal.entry_price
        ):
            raise SignalValidationError("short invalidation must be above entry")

    expected_rewards = tuple(
        abs(target - signal.entry_price) / risk for target in signal.take_profit_levels
    )
    if signal.risk_reward != expected_rewards:
        raise SignalValidationError("risk_reward does not match current signal geometry")

    if signal.signal_timestamp.tzinfo is None or signal.signal_timestamp > last_closed_at:
        raise SignalValidationError(
            "signal timestamp does not reference an available closed candle"
        )
    timeframe_seconds = timeframe_to_seconds(signal.signal_timeframe)
    if timeframe_seconds is None:
        raise SignalValidationError("unsupported signal timeframe")
    if signal.signal_timestamp < last_closed_at - timedelta(seconds=timeframe_seconds):
        raise SignalValidationError("signal timestamp is stale")

    now = evaluated_at.astimezone(timezone.utc)
    if signal.expires_at is not None:
        if signal.expires_at.tzinfo is None or signal.expires_at <= now:
            raise SignalValidationError("signal is expired or expiry is timezone-naive")

    expected_id = TradeSignal.stable_id(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        side=signal.side,
        timeframe=signal.signal_timeframe,
        timestamp=signal.signal_timestamp,
        entry=signal.entry_price,
        setup_id=signal.setup_id,
    )
    if signal.signal_id != expected_id:
        raise SignalValidationError("signal ID is not canonical")
    return signal
