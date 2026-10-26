from __future__ import annotations

import urllib.error
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import NoReturn

from .operator_contract import (
    MAX_METRIC,
    RequestStatus,
    SignalBotControlError,
    SignalBotControlErrorCode,
    SignalBotControlStatus,
    TelegramTestNotificationSender,
    TestNotificationRequest,
    TestNotificationResult,
    TestNotificationSender,
    TrustedSignalBotOperator,
)
from .operator_ledger import SignalBotControlLedger

_HEALTH_STATUSES = frozenset({"ok", "degraded"})


class SignalBotOperatorControl:
    """Safe owner-native observation and test-notification mutation boundary."""

    def __init__(
        self,
        *,
        ledger: SignalBotControlLedger,
        health: Callable[[], Mapping[str, object]],
        notifications_enabled: Callable[[], bool],
        sender: TestNotificationSender | None,
        configured: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(ledger, SignalBotControlLedger):
            raise TypeError("ledger must be SignalBotControlLedger")
        if not isinstance(configured, bool):
            raise TypeError("configured must be bool")
        self._ledger = ledger
        self._health = health
        self._notifications_enabled = notifications_enabled
        self._sender = sender
        self._configured = configured
        self._clock = clock or (lambda: datetime.now(UTC))

    def status(self) -> SignalBotControlStatus:
        try:
            health = self._health()
        except Exception:
            health = {}
        try:
            notifications_enabled = bool(self._notifications_enabled())
        except Exception:
            notifications_enabled = None
        try:
            reachable, control_success_at, last_error_type = self._ledger.owner_status()
        except Exception:
            reachable = None
            control_success_at = None
            last_error_type = SignalBotControlErrorCode.OWNER_FAILURE.value

        health_status = health.get("status")
        status = (
            health_status
            if isinstance(health_status, str) and health_status in _HEALTH_STATUSES
            else "unavailable"
        )
        health_success_at = _safe_timestamp(health.get("last_success_at"))
        return SignalBotControlStatus(
            configured=self._configured,
            reachable=reachable,
            notifications_enabled=notifications_enabled,
            status=status,
            cycles=_bounded_metric(health.get("cycles")),
            signals_sent=_bounded_metric(health.get("signals_sent")),
            last_success_at=_latest_timestamp(control_success_at, health_success_at),
            last_error_type=last_error_type,
        )

    def test_notification(self, request: TestNotificationRequest) -> TestNotificationResult:
        if not isinstance(request, TestNotificationRequest):
            raise SignalBotControlError(
                SignalBotControlErrorCode.INVALID_REQUEST,
                "request must be TestNotificationRequest",
            )
        now = _safe_clock(self._clock)
        try:
            replay = self._ledger.claim(request, now)
        except SignalBotControlError:
            raise
        except Exception:
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "test-notification idempotency ledger is unavailable",
                request_id=request.request_id,
            ) from None
        if replay is not None:
            if replay.status is RequestStatus.SUCCEEDED and replay.completed_at is not None:
                return TestNotificationResult(
                    request_id=replay.request_id,
                    operator=replay.operator,
                    completed_at=replay.completed_at,
                    replayed=True,
                )
            code = replay.error_code or SignalBotControlErrorCode.OWNER_FAILURE
            raise SignalBotControlError(
                code,
                "replayed Signal Bot test-notification failure",
                request_id=request.request_id,
            ) from None

        if not self._configured or self._sender is None:
            self._raise_terminal_failure(request, SignalBotControlErrorCode.NOT_CONFIGURED)

        try:
            self._sender.send_test_notification()
        except Exception as exc:
            self._raise_terminal_failure(request, _classify_send_error(exc))

        try:
            completed_at = self._ledger.complete_success(request, _safe_clock(self._clock))
        except Exception:
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "test notification sent but durable result requires owner recovery",
                request_id=request.request_id,
            ) from None
        return TestNotificationResult(
            request_id=request.request_id,
            operator=request.operator.subject,
            completed_at=completed_at,
        )

    def _raise_terminal_failure(
        self,
        request: TestNotificationRequest,
        code: SignalBotControlErrorCode,
    ) -> NoReturn:
        try:
            self._ledger.complete_failure(request, code, _safe_clock(self._clock))
        except SignalBotControlError as exc:
            if exc.code is SignalBotControlErrorCode.RECOVERY_REQUIRED:
                raise exc from None
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "test-notification failure requires owner recovery",
                request_id=request.request_id,
            ) from None
        except Exception:
            raise SignalBotControlError(
                SignalBotControlErrorCode.RECOVERY_REQUIRED,
                "test-notification failure requires owner recovery",
                request_id=request.request_id,
            ) from None
        raise SignalBotControlError(
            code,
            "Signal Bot test notification was not sent",
            request_id=request.request_id,
        ) from None


def _classify_send_error(exc: Exception) -> SignalBotControlErrorCode:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return SignalBotControlErrorCode.RATE_LIMITED
        if 400 <= exc.code < 500:
            return SignalBotControlErrorCode.REJECTED
        if 500 <= exc.code < 600:
            return SignalBotControlErrorCode.UNREACHABLE
        return SignalBotControlErrorCode.OWNER_FAILURE
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return SignalBotControlErrorCode.UNREACHABLE
    return SignalBotControlErrorCode.OWNER_FAILURE


def _safe_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        return _utc(clock())
    except Exception:
        raise SignalBotControlError(
            SignalBotControlErrorCode.OWNER_FAILURE,
            "Signal Bot owner clock is invalid",
        ) from None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _safe_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        try:
            return _utc(value)
        except ValueError:
            return None
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value))
        except (TypeError, ValueError):
            return None
    return None


def _bounded_metric(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_METRIC)


def _latest_timestamp(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


__all__ = [
    "SignalBotControlError",
    "SignalBotControlErrorCode",
    "SignalBotControlLedger",
    "SignalBotControlStatus",
    "SignalBotOperatorControl",
    "TelegramTestNotificationSender",
    "TestNotificationRequest",
    "TestNotificationResult",
    "TrustedSignalBotOperator",
]
