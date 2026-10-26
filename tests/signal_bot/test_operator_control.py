from __future__ import annotations

import threading
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_bot.operator_control import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    SignalBotControlLedger,
    SignalBotOperatorControl,
    TelegramTestNotificationSender,
    TestNotificationRequest,
    TrustedSignalBotOperator,
)

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)


def request(
    request_id: str = "test-001", operator: str = "control-plane:operator"
) -> TestNotificationRequest:
    return TestNotificationRequest(
        request_id=request_id,
        operator=TrustedSignalBotOperator(operator),
    )


class Sender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def send_test_notification(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def build_control(
    path: Path,
    *,
    sender: Sender | None = None,
    configured: bool = True,
    health: dict[str, object] | None = None,
) -> tuple[SignalBotOperatorControl, SignalBotControlLedger, Sender | None]:
    ledger = SignalBotControlLedger(path)
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: (
            health
            or {
                "status": "ok",
                "cycles": 7,
                "signals_sent": 3,
                "last_success_at": NOW,
            }
        ),
        notifications_enabled=lambda: True,
        sender=sender,
        configured=configured,
        clock=lambda: NOW,
    )
    return control, ledger, sender


def test_first_request_and_exact_duplicate_send_once(tmp_path: Path) -> None:
    sender = Sender()
    control, ledger, _ = build_control(tmp_path / "signal.db", sender=sender)
    mutation = request()

    first = control.test_notification(mutation)
    replay = control.test_notification(mutation)

    assert first.replayed is False
    assert replay.replayed is True
    assert first.completed_at == replay.completed_at == NOW
    assert sender.calls == 1
    ledger.close()


def test_same_request_different_operator_conflicts(tmp_path: Path) -> None:
    sender = Sender()
    control, ledger, _ = build_control(tmp_path / "signal.db", sender=sender)
    control.test_notification(request("same-id", "control-plane:operator-a"))

    with pytest.raises(SignalBotControlError) as raised:
        control.test_notification(request("same-id", "control-plane:operator-b"))

    assert raised.value.code is SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT
    assert sender.calls == 1
    ledger.close()


class BlockingSender(Sender):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_test_notification(self) -> None:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test synchronization timeout")


def test_concurrent_duplicate_across_owner_instances_sends_once(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    sender = BlockingSender()
    first, first_ledger, _ = build_control(path, sender=sender)
    second, second_ledger, _ = build_control(path, sender=sender)
    mutation = request("concurrent")
    outcome: list[object] = []

    thread = threading.Thread(target=lambda: outcome.append(first.test_notification(mutation)))
    thread.start()
    assert sender.entered.wait(timeout=5)

    with pytest.raises(SignalBotControlError) as raised:
        second.test_notification(mutation)
    assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED

    sender.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert sender.calls == 1
    assert len(outcome) == 1
    first_ledger.close()
    second_ledger.close()


def test_process_restart_replays_without_second_send(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    first_sender = Sender()
    first, first_ledger, _ = build_control(path, sender=first_sender)
    original = first.test_notification(request("restart"))
    first_ledger.close()

    second_sender = Sender()
    restarted, restarted_ledger, _ = build_control(path, sender=second_sender)
    replay = restarted.test_notification(request("restart"))

    assert replay.replayed is True
    assert replay.completed_at == original.completed_at
    assert first_sender.calls == 1
    assert second_sender.calls == 0
    restarted_ledger.close()


class FailCompleteLedger(SignalBotControlLedger):
    def complete_success(self, request: TestNotificationRequest, now: datetime) -> datetime:
        raise OSError("/Users/private/signal.db token=should-not-leak")


def test_uncertain_terminal_persistence_never_resends(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    sender = Sender()
    ledger = FailCompleteLedger(path)
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {"status": "ok"},
        notifications_enabled=lambda: True,
        sender=sender,
        configured=True,
        clock=lambda: NOW,
    )

    with pytest.raises(SignalBotControlError) as first_error:
        control.test_notification(request("uncertain"))
    assert first_error.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    assert "/Users/" not in str(first_error.value)
    assert sender.calls == 1
    ledger.close()

    replay_ledger = SignalBotControlLedger(path)
    replay_control = SignalBotOperatorControl(
        ledger=replay_ledger,
        health=lambda: {"status": "ok"},
        notifications_enabled=lambda: True,
        sender=sender,
        configured=True,
        clock=lambda: NOW,
    )
    with pytest.raises(SignalBotControlError) as replay_error:
        replay_control.test_notification(request("uncertain"))
    assert replay_error.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    assert sender.calls == 1
    replay_ledger.close()


def test_not_configured_failure_is_durable_and_replayed(tmp_path: Path) -> None:
    control, ledger, _ = build_control(tmp_path / "signal.db", sender=None, configured=False)
    mutation = request("not-configured")

    with pytest.raises(SignalBotControlError) as first:
        control.test_notification(mutation)
    with pytest.raises(SignalBotControlError) as replay:
        control.test_notification(mutation)

    assert first.value.code is SignalBotControlErrorCode.NOT_CONFIGURED
    assert replay.value.code is SignalBotControlErrorCode.NOT_CONFIGURED
    ledger.close()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            urllib.error.HTTPError(
                "https://api.telegram.org/botTOPSECRET/sendMessage", 429, "rate", None, None
            ),
            SignalBotControlErrorCode.RATE_LIMITED,
        ),
        (
            urllib.error.HTTPError(
                "https://api.telegram.org/botTOPSECRET/sendMessage", 403, "rejected", None, None
            ),
            SignalBotControlErrorCode.REJECTED,
        ),
        (
            urllib.error.URLError("https://api.telegram.org/botTOPSECRET unreachable"),
            SignalBotControlErrorCode.UNREACHABLE,
        ),
        (
            RuntimeError("/Users/private/token=TOPSECRET"),
            SignalBotControlErrorCode.OWNER_FAILURE,
        ),
    ],
)
def test_transport_errors_are_safe_and_deterministic(
    tmp_path: Path,
    error: Exception,
    code: SignalBotControlErrorCode,
) -> None:
    sender = Sender(error)
    control, ledger, _ = build_control(tmp_path / f"{code.value}.db", sender=sender)

    with pytest.raises(SignalBotControlError) as raised:
        control.test_notification(request(code.value.lower()))

    assert raised.value.code is code
    assert raised.value.__cause__ is None
    text = str(raised.value).lower()
    assert "topsecret" not in text
    assert "/users/" not in text
    assert "api.telegram.org" not in text
    ledger.close()


def test_malformed_and_oversized_request_fields_are_rejected() -> None:
    with pytest.raises(SignalBotControlError) as oversized:
        TestNotificationRequest(
            request_id="x" * 129,
            operator=TrustedSignalBotOperator("control-plane:operator"),
        )
    assert oversized.value.code is SignalBotControlErrorCode.INVALID_REQUEST

    with pytest.raises(SignalBotControlError) as operator:
        TrustedSignalBotOperator("reader token with spaces")
    assert operator.value.code is SignalBotControlErrorCode.INVALID_REQUEST

    with pytest.raises(SignalBotControlError) as raw_operator:
        TestNotificationRequest(
            request_id="typed",
            operator="reader-token",  # type: ignore[arg-type]
        )
    assert raw_operator.value.code is SignalBotControlErrorCode.INVALID_REQUEST


def test_safe_status_projection_never_exposes_owner_private_values(tmp_path: Path) -> None:
    sender = Sender()
    health = {
        "status": "ok",
        "cycles": 11,
        "signals_sent": 4,
        "last_success_at": NOW.isoformat(),
        "token": "TOPSECRET",
        "chat_id": "123456789",
        "private_path": "/Users/private/state.db",
        "errors_by_symbol": {"BTCUSDT": "RuntimeError(TOPSECRET)"},
    }
    control, ledger, _ = build_control(tmp_path / "signal.db", sender=sender, health=health)
    control.test_notification(request("status-success"))

    status = control.status()

    assert status.configured is True
    assert status.reachable is True
    assert status.notifications_enabled is True
    assert status.status == "ok"
    assert status.cycles == 11
    assert status.signals_sent == 4
    assert status.last_success_at == NOW
    assert status.last_error_type is None
    text = repr(status).lower()
    assert "topsecret" not in text
    assert "123456789" not in text
    assert "/users/" not in text
    ledger.close()


def test_status_preferences_failure_is_bounded_and_side_effect_free(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {"status": "ok", "cycles": -10, "signals_sent": "bad"},
        notifications_enabled=lambda: (_ for _ in ()).throw(
            RuntimeError("/Users/private/token=TOPSECRET")
        ),
        sender=Sender(),
        configured=True,
        clock=lambda: NOW,
    )

    status = control.status()

    assert status.notifications_enabled is None
    assert status.cycles == 0
    assert status.signals_sent == 0
    assert "topsecret" not in repr(status).lower()
    ledger.close()


def test_audit_is_durable_bounded_and_contains_no_message_or_chat_id(tmp_path: Path) -> None:
    sender = Sender()
    control, ledger, _ = build_control(tmp_path / "signal.db", sender=sender)
    mutation = request("audit")
    control.test_notification(mutation)
    control.test_notification(mutation)

    entries = ledger.audit_entries()

    assert [entry.result for entry in entries[:3]] == ["REPLAYED", "SUCCEEDED", "ACCEPTED"]
    for entry in entries:
        text = repr(entry).lower()
        assert "module strategy signal bot test notification" not in text
        assert "chat_id" not in text
        assert "token" not in text
        assert "/users/" not in text
    ledger.close()


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> str:
        self.calls.append((chat_id, text))
        return "message-1"


def test_telegram_sender_has_fixed_owner_message_and_no_user_body() -> None:
    client = Client()
    sender = TelegramTestNotificationSender(client, "-100123")

    sender.send_test_notification()

    assert client.calls == [("-100123", "Module Strategy Signal Bot test notification")]
    assert "-100123" not in repr(sender)
