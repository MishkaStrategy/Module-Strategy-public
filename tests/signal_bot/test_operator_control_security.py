from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_bot.operator_control import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    SignalBotControlLedger,
    SignalBotOperatorControl,
    TestNotificationRequest,
    TrustedSignalBotOperator,
)

NOW = datetime(2026, 8, 8, 17, 0, tzinfo=UTC)


def request(request_id: str, operator: str = "control-plane:operator") -> TestNotificationRequest:
    return TestNotificationRequest(
        request_id=request_id,
        operator=TrustedSignalBotOperator(operator),
    )


class Sender:
    def __init__(self) -> None:
        self.calls = 0

    def send_test_notification(self) -> None:
        self.calls += 1


def control_for(
    ledger: SignalBotControlLedger,
    sender: Sender | None = None,
    *,
    health_status: str = "ok",
) -> SignalBotOperatorControl:
    return SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {
            "status": health_status,
            "cycles": 1,
            "signals_sent": 0,
            "last_success_at": NOW,
        },
        notifications_enabled=lambda: True,
        sender=sender,
        configured=sender is not None,
        clock=lambda: NOW,
    )


def test_conflict_and_uncertain_replay_attempts_are_durably_audited(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    sender = Sender()
    ledger = SignalBotControlLedger(path)
    control = control_for(ledger, sender)
    control.test_notification(request("conflict", "control-plane:a"))

    with pytest.raises(SignalBotControlError) as conflict:
        control.test_notification(request("conflict", "control-plane:b"))
    assert conflict.value.code is SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT
    assert ledger.audit_entries(limit=1)[0].result == "IDEMPOTENCY_CONFLICT"
    ledger.close()

    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO signal_bot_operator_requests "
            "(request_id,operator,operation,status,error_code,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "uncertain",
                "control-plane:a",
                "TEST_NOTIFICATION",
                "IN_PROGRESS",
                None,
                NOW.isoformat(),
                None,
            ),
        )
        db.commit()

    restarted = SignalBotControlLedger(path)
    restarted_control = control_for(restarted, sender)
    with pytest.raises(SignalBotControlError) as uncertain:
        restarted_control.test_notification(request("uncertain", "control-plane:a"))
    assert uncertain.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    assert restarted.audit_entries(limit=1)[0].result == "RECOVERY_REQUIRED"
    assert sender.calls == 1
    restarted.close()


def test_capacity_failure_is_audited_without_forgetting_prior_request(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db", max_requests=1)
    sender = Sender()
    control = control_for(ledger, sender)
    control.test_notification(request("first"))

    with pytest.raises(SignalBotControlError) as raised:
        control.test_notification(request("second"))

    assert raised.value.code is SignalBotControlErrorCode.OWNER_FAILURE
    assert ledger.audit_entries(limit=1)[0].result == "OWNER_FAILURE"
    replay = control.test_notification(request("first"))
    assert replay.replayed is True
    assert sender.calls == 1
    ledger.close()


def test_unknown_health_status_is_not_projected(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    control = control_for(ledger, Sender(), health_status="token=TOPSECRET")

    status = control.status()

    assert status.status == "unavailable"
    assert "topsecret" not in repr(status).lower()
    ledger.close()


def test_corrupt_owner_status_never_projects_raw_private_value(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    ledger = SignalBotControlLedger(path)
    ledger.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE signal_bot_operator_status SET last_error_type=? WHERE singleton=1",
            ("/Users/private/token=TOPSECRET",),
        )
        db.commit()

    reopened = SignalBotControlLedger(path)
    status = control_for(reopened, Sender()).status()

    assert status.reachable is None
    assert status.last_error_type == SignalBotControlErrorCode.OWNER_FAILURE.value
    text = repr(status).lower()
    assert "topsecret" not in text
    assert "/users/" not in text
    reopened.close()


def test_corrupt_audit_row_fails_closed_without_raw_value(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    ledger = SignalBotControlLedger(path)
    ledger.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO signal_bot_operator_audit "
            "(timestamp,operation,request_id,operator,result) VALUES(?,?,?,?,?)",
            (
                NOW.isoformat(),
                "TEST_NOTIFICATION",
                "audit-corrupt",
                "control-plane:operator",
                "/Users/private/token=TOPSECRET",
            ),
        )
        db.commit()

    reopened = SignalBotControlLedger(path)
    with pytest.raises(SignalBotControlError) as raised:
        reopened.audit_entries()

    assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    assert raised.value.__cause__ is None
    text = str(raised.value).lower()
    assert "topsecret" not in text
    assert "/users/" not in text
    reopened.close()
