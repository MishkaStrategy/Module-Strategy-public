from __future__ import annotations

from decimal import Decimal

from .domain import InstrumentRules, SignalStatus, TradeSignal, decimal_places


def format_signal(signal: TradeSignal, rules: InstrumentRules) -> str:
    places = decimal_places(rules.tick_size)

    def price(value: Decimal) -> str:
        return f"{value:,.{places}f}".replace(",", " ")

    direction = "🟢 LONG" if signal.side.value == "buy" else "🔴 SHORT"
    risk_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price * Decimal("100")
    lines = [
        f"{direction} {signal.symbol}",
        "",
        f"Стратегия: {signal.strategy_id} v{signal.strategy_version}",
        f"Таймфрейм: {signal.signal_timeframe}",
        f"Время: {signal.signal_timestamp:%d.%m.%Y %H:%M} UTC",
        "",
        f"ТВХ: {price(signal.entry_price)}",
        f"Тип входа: {signal.entry_type.value}",
        f"Стоп: {price(signal.stop_loss)}",
        f"Риск до стопа: {risk_pct:.2f}%",
    ]
    for index, (target, ratio) in enumerate(zip(signal.take_profit_levels, signal.risk_reward), 1):
        lines.append(f"TP{index}: {price(target)} — {ratio:.2f}R")
    if signal.expires_at:
        lines.extend(("", f"Действителен до: {signal.expires_at:%d.%m.%Y %H:%M} UTC"))
    if signal.human_readable_reason:
        lines.append(f"Причина: {signal.human_readable_reason}")
    lines.extend(("", f"Signal ID: {signal.signal_id}"))
    return "\n".join(lines)


def format_lifecycle(signal: TradeSignal, status: SignalStatus) -> str:
    labels = {
        SignalStatus.CANCELLED: "⚪ СИГНАЛ ОТМЕНЁН",
        SignalStatus.EXPIRED: "⌛ СИГНАЛ ПРОСРОЧЕН",
        SignalStatus.SEND_FAILED: "⚠️ ОШИБКА ДОСТАВКИ",
        SignalStatus.SENT: "🆕 НОВЫЙ СИГНАЛ",
    }
    label = labels.get(status, f"СИГНАЛ: {status.value.upper()}")
    return f"{label}\n{signal.side.value.upper()} {signal.symbol}\nSignal ID: {signal.signal_id}"
