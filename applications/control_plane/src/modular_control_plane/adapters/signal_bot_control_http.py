from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .signal_bot_http import validate_signal_bot_base_url

_MAX_RESPONSE_BYTES = 64 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OWNER_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "NOT_CONFIGURED",
        "UNREACHABLE",
        "RATE_LIMITED",
        "REJECTED",
        "IDEMPOTENCY_CONFLICT",
        "RECOVERY_REQUIRED",
        "OWNER_FAILURE",
    }
)


class SignalBotControlTransportErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNREACHABLE = "UNREACHABLE"
    RATE_LIMITED = "RATE_LIMITED"
    REJECTED = "REJECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    OWNER_FAILURE = "OWNER_FAILURE"
    UNAUTHORIZED = "UNAUTHORIZED"


class SignalBotControlHttpError(RuntimeError):
    """Safe owner transport failure without token, URL, response body or raw exception text."""

    def __init__(
        self,
        code: SignalBotControlTransportErrorCode,
        *,
        status_code: int | None = None,
    ) -> None:
        if not isinstance(code, SignalBotControlTransportErrorCode):
            raise TypeError("code must be SignalBotControlTransportErrorCode")
        super().__init__(f"Signal Bot owner control failed: {code.value}")
        self.code = code
        self.status_code = status_code


class SignalBotOperatorStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
            raise ValueError("Signal Bot last_success_at must be timezone-aware")
        return value.astimezone(UTC)


class SignalBotTestNotificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    operator: str = Field(min_length=1, max_length=128)
    completed_at: datetime
    replayed: bool

    @field_validator("completed_at")
    @classmethod
    def require_aware_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Signal Bot completed_at must be timezone-aware")
        return value.astimezone(UTC)


class SignalBotControlHttpAdapter:
    """Consume the accepted Signal Bot owner control transport over loopback only."""

    def __init__(
        self,
        base_url: str,
        *,
        reader_token: str,
        operator_token: str,
        timeout_seconds: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_signal_bot_base_url(base_url)
        self._reader_token = _token(reader_token, "reader_token")
        self._operator_token = _token(operator_token, "operator_token")
        if self._reader_token == self._operator_token:
            raise ValueError("Signal Bot reader and operator tokens must be distinct")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if not 0.05 <= float(timeout_seconds) <= 30.0:
            raise ValueError("timeout_seconds must be between 0.05 and 30")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def status(self) -> SignalBotOperatorStatus:
        payload = self._request(
            "GET",
            "/control/v1/status",
            token=self._reader_token,
        )
        try:
            return SignalBotOperatorStatus.model_validate(payload)
        except ValidationError:
            raise SignalBotControlHttpError(
                SignalBotControlTransportErrorCode.OWNER_FAILURE
            ) from None

    def test_notification(self, request_id: str) -> SignalBotTestNotificationResult:
        _request_id(request_id)
        payload = self._request(
            "POST",
            "/control/v1/test-notification",
            token=self._operator_token,
            body=json.dumps(
                {"request_id": request_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        try:
            result = SignalBotTestNotificationResult.model_validate(payload)
        except ValidationError:
            raise SignalBotControlHttpError(
                SignalBotControlTransportErrorCode.OWNER_FAILURE
            ) from None
        if result.request_id != request_id:
            raise SignalBotControlHttpError(SignalBotControlTransportErrorCode.OWNER_FAILURE)
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: bytes | None = None,
    ) -> object:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream(
                    method,
                    path,
                    content=body,
                    headers=headers,
                ) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise SignalBotControlHttpError(
                            SignalBotControlTransportErrorCode.OWNER_FAILURE,
                            status_code=response.status_code,
                        )
                status_code = response.status_code
        except SignalBotControlHttpError:
            raise
        except httpx.HTTPError:
            raise SignalBotControlHttpError(
                SignalBotControlTransportErrorCode.UNREACHABLE
            ) from None

        payload = _decode(bytes(raw))
        if status_code == 200:
            return payload
        raise SignalBotControlHttpError(
            _error_code(status_code, payload),
            status_code=status_code,
        )

    def __repr__(self) -> str:
        return (
            "SignalBotControlHttpAdapter("
            f"base_url={self._base_url!r}, reader_token_configured=True, "
            "operator_token_configured=True)"
        )


def _token(value: str, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 1..4096 characters")
    return value


def _request_id(value: str) -> None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise SignalBotControlHttpError(SignalBotControlTransportErrorCode.INVALID_REQUEST)


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SignalBotControlHttpError(SignalBotControlTransportErrorCode.OWNER_FAILURE) from None


def _error_code(status_code: int, payload: object) -> SignalBotControlTransportErrorCode:
    if status_code == 401:
        return SignalBotControlTransportErrorCode.UNAUTHORIZED
    if isinstance(payload, dict):
        value = payload.get("error")
        if isinstance(value, str) and value in _OWNER_ERROR_CODES:
            return SignalBotControlTransportErrorCode(value)
    return SignalBotControlTransportErrorCode.OWNER_FAILURE
