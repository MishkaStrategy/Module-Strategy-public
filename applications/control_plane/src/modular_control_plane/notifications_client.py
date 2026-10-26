from __future__ import annotations

from pydantic import ValidationError

from .client import ControlClient, ControlClientError
from .notifications import NotificationStatus, TestNotificationRequest, TestNotificationResult


class NotificationsControlClient(ControlClient):
    """ControlClient extension for the bounded Signal Bot owner surface."""

    def notifications_status(self) -> NotificationStatus:
        payload = self._get("/api/control/v1/notifications/status")
        try:
            return NotificationStatus.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_notifications_response("status") from exc

    def notifications_test(self, request: TestNotificationRequest) -> TestNotificationResult:
        if not isinstance(request, TestNotificationRequest):
            raise TypeError("request must be TestNotificationRequest")
        payload = self._post(
            "/api/control/v1/notifications/test",
            request.model_dump(mode="json"),
        )
        try:
            return TestNotificationResult.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_notifications_response("test notification") from exc


def _invalid_notifications_response(surface: str) -> ControlClientError:
    return ControlClientError(
        "CONTROL_RESPONSE_INVALID",
        f"Control Plane returned an incompatible Notifications {surface} response.",
        action="Verify that modular-control and modular-control-api use compatible versions.",
    )


__all__ = ["NotificationsControlClient"]
