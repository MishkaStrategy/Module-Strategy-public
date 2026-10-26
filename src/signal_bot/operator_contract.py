from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
OPERATOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
OPERATION = "TEST_NOTIFICATION"
TEST_NOTIFICATION_TEXT = "Module Strategy Signal Bot test notification"
MAX_REQUESTS = 2048
MAX_METRIC = 2**63 - 1


class SignalBotControlErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNREACHABLE = "UNREACHABLE"
    RATE_LIMITED = "RATE_LIMITED"
    REJECTED = "REJECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    OWNER_FAILURE = "OWNER_FAILURE"


class RequestStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SignalBotControlError(RuntimeError):
    def __init__(
        self,
        code: SignalBotControlErrorCode,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        if not isinstance(code, SignalBotControlErrorCode):
            raise TypeError("code must be SignalBotControlErrorCode")
        super().__init__(message)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class TrustedSignalBotOperator:
    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or OPERATOR_PATTERN.fullmatch(self.subject) is None:
            raise SignalBotControlError(
                SignalBotControlErrorCode.INVALID_REQUEST,
                "operator identity is invalid",
            )


@dataclass(frozen=True, slots=True)
class TestNotificationRequest:
    request_id: str
    operator: TrustedSignalBotOperator

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or REQUEST_ID_PATTERN.fullmatch(self.request_id) is None
        ):
            raise SignalBotControlError(
                SignalBotControlErrorCode.INVALID_REQUEST,
                "request_id is invalid",
            )
        if not isinstance(self.operator, TrustedSignalBotOperator):
            raise SignalBotControlError(
                SignalBotControlErrorCode.INVALID_REQUEST,
                "operator must be TrustedSignalBotOperator",
            )


@dataclass(frozen=True, slots=True)
class TestNotificationResult:
    request_id: str
    operator: str
    completed_at: datetime
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class SignalBotControlStatus:
    configured: bool
    reachable: bool | None
    notifications_enabled: bool | None
    status: str
    cycles: int
    signals_sent: int
    last_success_at: datetime | None
    last_error_type: str | None


@dataclass(frozen=True, slots=True)
class SignalBotAuditEntry:
    event_id: int
    timestamp: datetime
    operation: str
    request_id: str
    operator: str
    result: str


@dataclass(frozen=True, slots=True)
class RequestRecord:
    request_id: str
    operator: str
    status: RequestStatus
    error_code: SignalBotControlErrorCode | None
    created_at: datetime
    completed_at: datetime | None


class TestNotificationSender(Protocol):
    def send_test_notification(self) -> None: ...


class TextBotClient(Protocol):
    def send_text(self, chat_id: str, text: str) -> str: ...


class TelegramTestNotificationSender:
    """Private owner adapter; wrapped Telegram internals are never projected."""

    def __init__(self, client: TextBotClient, chat_id: str) -> None:
        if not isinstance(chat_id, str) or not chat_id or not chat_id.lstrip("-").isdigit():
            raise ValueError("Telegram chat ID must be a canonical numeric ID")
        self._client = client
        self._chat_id = chat_id

    def send_test_notification(self) -> None:
        self._client.send_text(self._chat_id, TEST_NOTIFICATION_TEXT)
