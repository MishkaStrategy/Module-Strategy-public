from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from .adapters.signal_bot_control_http import (
    SignalBotControlHttpAdapter,
    SignalBotOperatorStatus,
)
from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus, SafeScalar
from .service import Clock, utc_now

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class NotificationStatus(ContractModel):
    configured: bool
    reachable: bool | None
    notifications_enabled: bool | None
    status: Literal["ok", "degraded", "unavailable"]
    cycles: int = Field(ge=0, le=2**63 - 1)
    signals_sent: int = Field(ge=0, le=2**63 - 1)
    last_success_at: datetime | None
    last_error_type: str | None = Field(default=None, max_length=128)

    @field_validator("last_success_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_success_at must be timezone-aware")
        return value.astimezone(UTC)


class TestNotificationRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("request_id must be a bounded portable identifier")
        return value


class TestNotificationResult(ContractModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    operator: str = Field(min_length=1, max_length=128)
    completed_at: datetime
    replayed: bool

    @field_validator("completed_at")
    @classmethod
    def require_aware_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value.astimezone(UTC)


class NotificationsControlService:
    component = ComponentId.SIGNAL_BOT

    def __init__(
        self,
        adapter: SignalBotControlHttpAdapter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not isinstance(adapter, SignalBotControlHttpAdapter):
            raise TypeError("adapter must be SignalBotControlHttpAdapter")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._adapter = adapter
        self._clock = clock

    def status(self) -> NotificationStatus:
        return _status(self._adapter.status())

    def test_notification(self, request: TestNotificationRequest) -> TestNotificationResult:
        if not isinstance(request, TestNotificationRequest):
            raise TypeError("request must be TestNotificationRequest")
        result = self._adapter.test_notification(request.request_id)
        return TestNotificationResult(
            request_id=result.request_id,
            operator=result.operator,
            completed_at=result.completed_at,
            replayed=result.replayed,
        )

    def health(self) -> ComponentHealth:
        snapshot = self.status()
        status = {
            "ok": HealthStatus.HEALTHY,
            "degraded": HealthStatus.DEGRADED,
            "unavailable": HealthStatus.UNAVAILABLE,
        }[snapshot.status]
        details: dict[str, SafeScalar] = {
            "configured": snapshot.configured,
            "reachable": snapshot.reachable,
            "notifications_enabled": snapshot.notifications_enabled,
            "cycles": snapshot.cycles,
            "signals_sent": snapshot.signals_sent,
            "last_success_at": (
                snapshot.last_success_at.isoformat()
                if snapshot.last_success_at is not None
                else None
            ),
            "last_error_type": snapshot.last_error_type,
            "test_notification_available": True,
        }
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._checked_at(),
            summary=f"Signal Bot owner control status is {snapshot.status}",
            details=details,
        )

    def _checked_at(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value


def _status(value: SignalBotOperatorStatus) -> NotificationStatus:
    return NotificationStatus(
        configured=value.configured,
        reachable=value.reachable,
        notifications_enabled=value.notifications_enabled,
        status=value.status,
        cycles=value.cycles,
        signals_sent=value.signals_sent,
        last_success_at=value.last_success_at,
        last_error_type=value.last_error_type,
    )


__all__ = [
    "NotificationStatus",
    "NotificationsControlService",
    "TestNotificationRequest",
    "TestNotificationResult",
]
