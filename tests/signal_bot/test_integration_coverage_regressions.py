from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_core import Candle, ModuleDecision, ModuleManifest, Side, Signal, StrategyModule
from signal_bot.control import (
    PreferenceAwareNotifier,
    TelegramCommandService,
    TelegramControlLoop,
    TelegramLifecycleNotifier,
)
from signal_bot.domain import (
    CandleBatch,
    EntryType,
    InstrumentRules,
    SignalStatus,
    StrategyContext,
    StrategyDecision,
)
from signal_bot.strategy_adapter import ModuleSignalAdapter
from signal_bot.validation import SignalValidationError, validate_decision, validate_signal

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
RULES = InstrumentRules(
    "BTCUSDT",
    "binance_usdt_m_perpetual",
    Decimal("0.1"),
    min_price=Decimal("10"),
)


def _decision(**changes: object) -> StrategyDecision:
    values: dict[str, object] = {
        "side": Side.BUY,
        "signal_timestamp": NOW - timedelta(minutes=5),
        "signal_timeframe": "5m",
        "entry_type": EntryType.LIMIT,
        "entry_price": Decimal("100.0"),
        "stop_loss": Decimal("99.0"),
        "take_profit_levels": (Decimal("102.0"),),
        "invalidation_price": Decimal("98.0"),
        "reason_codes": ("confirmed",),
        "human_readable_reason": "confirmed",
        "expires_at": NOW + timedelta(hours=1),
        "setup_id": "setup-1",
    }
    values.update(changes)
    return StrategyDecision(**values)


def _signal(**changes: object):
    signal = validate_decision(
        _decision(),
        strategy_id="coverage",
        strategy_version="1.0.0",
        instrument=RULES,
        evaluated_at=NOW,
        last_closed_at=NOW,
    )
    return replace(signal, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"strategy_id": ""}, "strategy identity"),
        ({"contract_version": "2.0"}, "unsupported signal source"),
        ({"source": "live"}, "unsupported signal source"),
        ({"chart_enabled": 1}, "chart_enabled"),
        ({"market": "spot"}, "instrument identity"),
        ({"entry_price": Decimal("9.9")}, "min_price"),
        ({"entry_price": Decimal("100.05")}, "tick_size"),
        ({"risk_reward": (Decimal("999"),)}, "risk_reward"),
        ({"signal_timeframe": "bad"}, "unsupported signal timeframe"),
        ({"signal_id": "not-canonical"}, "not canonical"),
        ({"expires_at": datetime(2026, 7, 22, 13, 0)}, "timezone-naive"),
    ],
)
def test_validate_signal_rejects_contract_and_identity_failures(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(SignalValidationError, match=message):
        validate_signal(
            _signal(**changes),
            instrument=RULES,
            evaluated_at=NOW,
            last_closed_at=NOW,
        )


def test_validate_signal_rejects_naive_validation_clock_and_short_geometry() -> None:
    with pytest.raises(SignalValidationError, match="timezone-aware"):
        validate_signal(
            _signal(),
            instrument=RULES,
            evaluated_at=NOW.replace(tzinfo=None),
            last_closed_at=NOW,
        )

    short_signal = validate_decision(
        _decision(
            side=Side.SELL,
            stop_loss=Decimal("101.0"),
            take_profit_levels=(Decimal("98.0"),),
            invalidation_price=Decimal("102.0"),
        ),
        strategy_id="coverage",
        strategy_version="1.0.0",
        instrument=RULES,
        evaluated_at=NOW,
        last_closed_at=NOW,
    )
    with pytest.raises(SignalValidationError, match="short invalidation"):
        validate_signal(
            replace(short_signal, invalidation_price=Decimal("99.0")),
            instrument=RULES,
            evaluated_at=NOW,
            last_closed_at=NOW,
        )


class _State:
    def __init__(self) -> None:
        self.enabled = True
        self.offset: int | None = None
        self.notification_changes: list[tuple[str, bool]] = []

    def notifications_enabled(self, chat_id: str) -> bool:
        return self.enabled

    def set_notifications(self, chat_id: str, enabled: bool) -> None:
        self.notification_changes.append((chat_id, enabled))

    def telegram_update_offset(self) -> int | None:
        return self.offset

    def set_telegram_update_offset(self, value: int) -> None:
        self.offset = value

    def latest_signal(self):
        return None


class _Client:
    def __init__(self, updates: list[dict[str, object]] | None = None) -> None:
        self.updates = updates or []
        self.sent: list[tuple[str, str]] = []

    def get_updates(self, *, offset: int | None = None, poll_timeout: int = 20):
        return self.updates

    def send_text(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return "message-id"


class _Delegate:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, signal, message: str, chart):
        self.calls += 1
        return "delegated"


class _Orchestrator:
    bindings: tuple[object, ...] = ()

    def health(self) -> dict[str, object]:
        return {"status": "ok", "cycles": 1, "signals_sent": 2, "pair_errors": 0}


def test_preference_and_lifecycle_notifiers_are_fail_closed() -> None:
    state = _State()
    delegate = _Delegate()
    client = _Client()
    signal = _signal()

    preference = PreferenceAwareNotifier(delegate, state, "1")
    lifecycle = TelegramLifecycleNotifier(client, state, "1")
    state.enabled = False
    assert preference.send(signal, "message", None) == "notifications-disabled"
    assert lifecycle.send_lifecycle(signal, SignalStatus.SENT, "done") == "notifications-disabled"
    assert delegate.calls == 0 and client.sent == []

    state.enabled = True
    assert preference.send(signal, "message", None) == "delegated"
    assert lifecycle.send_lifecycle(signal, SignalStatus.SENT, "done") == "message-id"


def test_command_service_permissions_and_notification_controls() -> None:
    state = _State()
    service = TelegramCommandService(
        _Orchestrator(),
        state,  # type: ignore[arg-type]
        admin_chat_ids=frozenset({"admin"}),
        allowed_chat_ids=frozenset({"admin", "primary", "guest"}),
        primary_recipient="primary",
    )

    assert service.handle("unknown", "/status") == ""
    assert service.handle("guest", "hello") == ""
    assert "Статус: ok" in service.handle("guest", "/status")
    assert service.handle("guest", "/last") == "Сигналов пока нет"
    assert "основному получателю" in service.handle("guest", "/notifications_on")
    assert service.handle("primary", "/notifications_on") == "Уведомления включены"
    assert service.handle("primary", "/notifications_off") == "Уведомления отключены"
    assert state.notification_changes == [("primary", True), ("primary", False)]
    assert "только администратору" in service.handle("guest", "/health")
    assert "'status': 'ok'" in service.handle("admin", "/health")
    assert "Неизвестная команда" in service.handle("guest", "/does_not_exist")


def test_control_loop_skips_malformed_updates_and_commits_valid_offsets() -> None:
    state = _State()
    service = TelegramCommandService(
        _Orchestrator(),
        state,  # type: ignore[arg-type]
        allowed_chat_ids=frozenset({"7"}),
    )
    client = _Client(
        [
            {"update_id": True},
            {"update_id": "bad"},
            {"update_id": -1},
            {"update_id": 2, "message": "bad"},
            {"update_id": 3, "message": {"chat": {}, "text": 7}},
            {"update_id": "4", "message": {"chat": {"id": 7}, "text": "/start"}},
        ]
    )
    loop = TelegramControlLoop(client, service)

    assert loop._update_id({"update_id": object()}) is None
    assert loop.run_once() == 1
    assert state.offset == 5
    assert client.sent and client.sent[0][0] == "7"
    loop.stop()
    assert loop._stop.is_set()


class _BaseModule(StrategyModule):
    name = "coverage-module"
    signal_delivery_ready = True
    portfolio_independent = True
    manifest = ModuleManifest(name=name, version="1.0.0", required_timeframes=("5m",))

    def on_bar(self, view):
        return ModuleDecision()

    def evaluate_delivery(self, context):
        return ModuleDecision()


class _MissingMethodModule(StrategyModule):
    name = "missing-method"
    signal_delivery_ready = True
    portfolio_independent = True
    manifest = ModuleManifest(name=name, version="1.0.0", required_timeframes=("5m",))

    def on_bar(self, view):
        return ModuleDecision()


class _NoManifestModule(_BaseModule):
    manifest = None


class _WrongResultModule(_BaseModule):
    def evaluate_delivery(self, context):
        return object()


class _MultipleSignalModule(_BaseModule):
    def evaluate_delivery(self, context):
        signal = Signal(module=self.name, side=Side.BUY, reason="x", metadata={})
        return ModuleDecision(signals=(signal, signal))


class _MissingLevelsModule(_BaseModule):
    def evaluate_delivery(self, context):
        return ModuleDecision(
            signals=(Signal(module=self.name, side=Side.BUY, reason="x", metadata={}),)
        )


def _context() -> StrategyContext:
    candle = Candle(
        NOW - timedelta(minutes=5),
        100,
        101,
        99,
        100,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return StrategyContext(
        RULES,
        {"5m": CandleBatch("BTCUSDT", "5m", (candle,), NOW)},
        NOW,
    )


def test_module_adapter_rejects_invalid_delivery_contracts() -> None:
    with pytest.raises(ValueError, match="implement evaluate_delivery"):
        ModuleSignalAdapter(_MissingMethodModule())
    with pytest.raises(ValueError, match="requires a manifest"):
        ModuleSignalAdapter(_NoManifestModule())
    with pytest.raises(TypeError, match="ModuleDecision"):
        ModuleSignalAdapter(_WrongResultModule()).evaluate(_context())
    assert not ModuleSignalAdapter(_BaseModule()).evaluate(_context()).has_signal
    with pytest.raises(ValueError, match="only one"):
        ModuleSignalAdapter(_MultipleSignalModule()).evaluate(_context())
    with pytest.raises(ValueError, match="lacks delivery levels"):
        ModuleSignalAdapter(_MissingLevelsModule()).evaluate(_context())
