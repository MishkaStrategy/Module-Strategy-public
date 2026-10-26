from __future__ import annotations

import sqlite3
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_bot.operator_contract import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    TestNotificationRequest,
    TrustedSignalBotOperator,
)
from signal_bot.operator_control import SignalBotOperatorControl
from signal_bot.operator_http import SignalBotControlHttpServer
from signal_bot.operator_ledger import SignalBotControlLedger

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
READER = "reader-edge-secret"
OPERATOR = "operator-edge-secret"


class Sender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def send_test_notification(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def request(request_id: str = "edge-request") -> TestNotificationRequest:
    return TestNotificationRequest(
        request_id=request_id,
        operator=TrustedSignalBotOperator("control-plane:edge-operator"),
    )


def control(
    ledger: SignalBotControlLedger,
    *,
    sender: Sender | None = None,
    configured: bool = True,
    health: object | None = None,
    notifications: object | None = None,
    clock: object | None = None,
) -> SignalBotOperatorControl:
    def health_value() -> dict[str, object]:
        if isinstance(health, BaseException):
            raise health
        if health is None:
            return {"status": "ok", "cycles": 1, "signals_sent": 1}
        assert isinstance(health, dict)
        return health

    def notifications_value() -> bool:
        if isinstance(notifications, BaseException):
            raise notifications
        if notifications is None:
            return True
        assert isinstance(notifications, bool)
        return notifications

    def clock_value() -> datetime:
        if isinstance(clock, BaseException):
            raise clock
        if clock is None:
            return NOW
        assert isinstance(clock, datetime)
        return clock

    return SignalBotOperatorControl(
        ledger=ledger,
        health=health_value,
        notifications_enabled=notifications_value,
        sender=sender,
        configured=configured,
        clock=clock_value,
    )


class ClaimFailureLedger(SignalBotControlLedger):
    def claim(self, request: TestNotificationRequest, now: datetime):  # type: ignore[no-untyped-def]
        raise OSError("/Users/private/token=claim-secret")


class FailurePersistenceLedger(SignalBotControlLedger):
    def complete_failure(
        self,
        request: TestNotificationRequest,
        code: SignalBotControlErrorCode,
        now: datetime,
    ) -> datetime:
        raise OSError("/Users/private/token=failure-secret")


class RecoveryFailureLedger(SignalBotControlLedger):
    def complete_failure(
        self,
        request: TestNotificationRequest,
        code: SignalBotControlErrorCode,
        now: datetime,
    ) -> datetime:
        raise SignalBotControlError(
            SignalBotControlErrorCode.RECOVERY_REQUIRED,
            "safe recovery marker",
            request_id=request.request_id,
        )


class NonRecoveryFailureLedger(SignalBotControlLedger):
    def complete_failure(
        self,
        request: TestNotificationRequest,
        code: SignalBotControlErrorCode,
        now: datetime,
    ) -> datetime:
        raise SignalBotControlError(
            SignalBotControlErrorCode.OWNER_FAILURE,
            "safe owner failure",
            request_id=request.request_id,
        )


def test_constructor_and_clock_contracts_fail_closed(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    try:
        with pytest.raises(TypeError, match="ledger"):
            SignalBotOperatorControl(
                ledger=object(),  # type: ignore[arg-type]
                health=lambda: {},
                notifications_enabled=lambda: True,
                sender=None,
                configured=False,
            )
        with pytest.raises(TypeError, match="configured"):
            SignalBotOperatorControl(
                ledger=ledger,
                health=lambda: {},
                notifications_enabled=lambda: True,
                sender=None,
                configured=1,  # type: ignore[arg-type]
            )

        invalid_clock = control(ledger, sender=Sender(), clock=datetime(2026, 8, 8, 18, 0))
        with pytest.raises(SignalBotControlError) as naive:
            invalid_clock.test_notification(request("naive-clock"))
        assert naive.value.code is SignalBotControlErrorCode.OWNER_FAILURE
        assert naive.value.__cause__ is None

        exploding_clock = control(ledger, sender=Sender(), clock=RuntimeError("private clock"))
        with pytest.raises(SignalBotControlError) as exploding:
            exploding_clock.test_notification(request("exploding-clock"))
        assert exploding.value.code is SignalBotControlErrorCode.OWNER_FAILURE
        assert "private clock" not in str(exploding.value)
    finally:
        ledger.close()


def test_status_tolerates_failed_health_notifications_and_closed_ledger(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    current = control(
        ledger,
        sender=Sender(),
        health=RuntimeError("private health"),
        notifications=RuntimeError("private preference"),
    )
    ledger.close()

    status = current.status()

    assert status.status == "unavailable"
    assert status.notifications_enabled is None
    assert status.reachable is None
    assert status.last_error_type == SignalBotControlErrorCode.OWNER_FAILURE.value
    assert status.cycles == 0
    assert status.signals_sent == 0
    assert "private" not in repr(status).lower()


def test_status_bounds_metrics_and_discards_invalid_timestamps(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    try:
        current = control(
            ledger,
            sender=Sender(),
            health={
                "status": "degraded",
                "cycles": 10**30,
                "signals_sent": True,
                "last_success_at": "not-a-timestamp",
            },
        )
        status = current.status()
        assert status.status == "degraded"
        assert status.cycles < 10**30
        assert status.signals_sent == 0
        assert status.last_success_at is None

        naive_health = control(
            ledger,
            sender=Sender(),
            health={"status": "ok", "last_success_at": datetime(2026, 8, 8, 18, 0)},
        )
        assert naive_health.status().last_success_at is None
    finally:
        ledger.close()


def test_claim_storage_failure_is_recovery_required_without_private_text(tmp_path: Path) -> None:
    ledger = ClaimFailureLedger(tmp_path / "signal.db")
    current = control(ledger, sender=Sender())
    try:
        with pytest.raises(SignalBotControlError) as raised:
            current.test_notification(request("claim-failure"))
        assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
        assert raised.value.__cause__ is None
        assert "/users/" not in str(raised.value).lower()
        assert "claim-secret" not in str(raised.value).lower()
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "ledger_type",
    [FailurePersistenceLedger, RecoveryFailureLedger, NonRecoveryFailureLedger],
)
def test_terminal_failure_persistence_errors_never_expose_sender_or_storage(
    tmp_path: Path,
    ledger_type: type[SignalBotControlLedger],
) -> None:
    ledger = ledger_type(tmp_path / f"{ledger_type.__name__}.db")
    current = control(ledger, sender=None, configured=False)
    try:
        with pytest.raises(SignalBotControlError) as raised:
            current.test_notification(request(ledger_type.__name__.lower()))
        assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
        assert raised.value.__cause__ is None
        text = str(raised.value).lower()
        assert "/users/" not in text
        assert "secret" not in text
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            urllib.error.HTTPError("https://private.invalid", 500, "server", None, None),
            SignalBotControlErrorCode.UNREACHABLE,
        ),
        (
            urllib.error.HTTPError("https://private.invalid", 302, "redirect", None, None),
            SignalBotControlErrorCode.OWNER_FAILURE,
        ),
        (TimeoutError("private timeout"), SignalBotControlErrorCode.UNREACHABLE),
    ],
)
def test_additional_sender_error_classes_are_durable_and_safe(
    tmp_path: Path,
    error: Exception,
    expected: SignalBotControlErrorCode,
) -> None:
    ledger = SignalBotControlLedger(tmp_path / f"{expected.value}-{type(error).__name__}.db")
    sender = Sender(error)
    current = control(ledger, sender=sender)
    try:
        with pytest.raises(SignalBotControlError) as raised:
            current.test_notification(request(f"send-{expected.value.lower()}"))
        assert raised.value.code is expected
        assert raised.value.__cause__ is None
        assert "private" not in str(raised.value).lower()
        replay_status = ledger.audit_entries(limit=1)[0].result
        assert replay_status == expected.value
    finally:
        ledger.close()


def test_ledger_rejects_invalid_capacity_and_terminal_codes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SignalBotControlLedger(tmp_path / "zero.db", max_requests=0)
    with pytest.raises(ValueError, match="positive integer"):
        SignalBotControlLedger(tmp_path / "bool.db", max_requests=True)

    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    mutation = request("invalid-terminal")
    try:
        for code in (
            SignalBotControlErrorCode.INVALID_REQUEST,
            SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT,
            SignalBotControlErrorCode.RECOVERY_REQUIRED,
        ):
            with pytest.raises(ValueError, match="terminal"):
                ledger.complete_failure(mutation, code, NOW)
    finally:
        ledger.close()


def test_ledger_completion_without_claim_requires_recovery(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    mutation = request("missing-claim")
    try:
        with pytest.raises(SignalBotControlError) as success:
            ledger.complete_success(mutation, NOW)
        assert success.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED

        with pytest.raises(SignalBotControlError) as failure:
            ledger.complete_failure(mutation, SignalBotControlErrorCode.UNREACHABLE, NOW)
        assert failure.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    finally:
        ledger.close()


def test_owner_status_corruption_cases_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    ledger = SignalBotControlLedger(path)
    ledger.close()

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE signal_bot_operator_status SET reachable=2,last_success_at=NULL,last_error_type=NULL "
            "WHERE singleton=1"
        )
        db.commit()
    invalid_reachability = SignalBotControlLedger(path)
    try:
        with pytest.raises(SignalBotControlError) as raised:
            invalid_reachability.owner_status()
        assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    finally:
        invalid_reachability.close()

    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE signal_bot_operator_status SET reachable=1,last_success_at='not-a-time',"
            "last_error_type=NULL WHERE singleton=1"
        )
        db.commit()
    invalid_time = SignalBotControlLedger(path)
    try:
        with pytest.raises(ValueError):
            invalid_time.owner_status()
    finally:
        invalid_time.close()

    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM signal_bot_operator_status WHERE singleton=1")
        db.commit()
    missing = SignalBotControlLedger(path)
    try:
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM signal_bot_operator_status WHERE singleton=1")
            db.commit()
        with pytest.raises(SignalBotControlError) as absent:
            missing.owner_status()
        assert absent.value.code is SignalBotControlErrorCode.OWNER_FAILURE
    finally:
        missing.close()


def test_audit_limit_and_identity_corruption_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    ledger = SignalBotControlLedger(path)
    try:
        for invalid in (0, 501, True):
            with pytest.raises(ValueError, match="limit"):
                ledger.audit_entries(limit=invalid)
    finally:
        ledger.close()

    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO signal_bot_operator_audit "
            "(timestamp,operation,request_id,operator,result) VALUES(?,?,?,?,?)",
            (NOW.isoformat(), "WRONG", "audit-edge", "control-plane:edge", "SUCCEEDED"),
        )
        db.commit()
    reopened = SignalBotControlLedger(path)
    try:
        with pytest.raises(SignalBotControlError) as raised:
            reopened.audit_entries()
        assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    finally:
        reopened.close()


def test_corrupt_persisted_request_completion_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "signal.db"
    ledger = SignalBotControlLedger(path)
    ledger.close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO signal_bot_operator_requests "
            "(request_id,operator,operation,status,error_code,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "corrupt-success",
                "control-plane:edge-operator",
                "TEST_NOTIFICATION",
                "SUCCEEDED",
                None,
                NOW.isoformat(),
                None,
            ),
        )
        db.commit()

    reopened = SignalBotControlLedger(path)
    try:
        with pytest.raises(SignalBotControlError) as raised:
            reopened.claim(request("corrupt-success"), NOW)
        assert raised.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
        assert raised.value.__cause__ is None
    finally:
        reopened.close()


def test_http_constructor_bounds_unauthorized_status_404_and_no_store(tmp_path: Path) -> None:
    ledger = SignalBotControlLedger(tmp_path / "signal.db")
    current = control(ledger, sender=Sender())
    try:
        with pytest.raises(TypeError, match="control"):
            SignalBotControlHttpServer(
                object(),  # type: ignore[arg-type]
                reader_token=READER,
                operator_token=OPERATOR,
                port=0,
            )
        with pytest.raises(ValueError, match="port"):
            SignalBotControlHttpServer(
                current,
                reader_token=READER,
                operator_token=OPERATOR,
                port=70000,
            )
        with pytest.raises(ValueError, match="reader_token"):
            SignalBotControlHttpServer(
                current,
                reader_token="",
                operator_token=OPERATOR,
                port=0,
            )

        server = SignalBotControlHttpServer(
            current,
            reader_token=READER,
            operator_token=OPERATOR,
            port=0,
        )
        try:
            denied, denied_headers, denied_body = server.response(
                method="GET",
                path="/control/v1/status",
                authorization="Bearer wrong-reader",
            )
            assert denied == 401
            assert denied_headers["Cache-Control"] == "no-store"
            assert denied_body == b'{"error":"UNAUTHORIZED"}'

            missing, headers, body = server.response(
                method="GET",
                path="/control/v1/missing",
                authorization=f"Bearer {READER}",
            )
            assert missing == 404
            assert headers["Cache-Control"] == "no-store"
            assert body == b'{"error":"NOT_FOUND"}'

            server.start()
            server.start()
        finally:
            server.close()
    finally:
        ledger.close()


def test_http_maps_not_configured_rejected_and_unreachable_without_private_text(
    tmp_path: Path,
) -> None:
    cases: list[tuple[str, Sender | None, bool, int, str]] = [
        ("not-configured", None, False, 503, "NOT_CONFIGURED"),
        (
            "rejected",
            Sender(urllib.error.HTTPError("https://private.invalid", 403, "bad", None, None)),
            True,
            502,
            "REJECTED",
        ),
        (
            "unreachable",
            Sender(TimeoutError("private timeout")),
            True,
            503,
            "UNREACHABLE",
        ),
    ]
    for request_id, sender, configured, expected_status, expected_error in cases:
        ledger = SignalBotControlLedger(tmp_path / f"{request_id}.db")
        current = control(ledger, sender=sender, configured=configured)
        server = SignalBotControlHttpServer(
            current,
            reader_token=READER,
            operator_token=OPERATOR,
            port=0,
        )
        try:
            status, headers, body = server.response(
                method="POST",
                path="/control/v1/test-notification",
                authorization=f"Bearer {OPERATOR}",
                body=(f'{{"request_id":"{request_id}"}}').encode(),
            )
            payload = body.decode().lower()
            assert status == expected_status
            assert headers["Cache-Control"] == "no-store"
            assert expected_error.lower() in payload
            assert "private" not in payload
        finally:
            server.close()
            ledger.close()
