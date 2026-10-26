from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_MAX_RESPONSE_BYTES = 64 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_OWNER_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "INVALID_TRANSITION",
        "RECOVERY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "OWNER_FAILURE",
        "PAPER_ONLY",
    }
)
_PATHS = {
    "START": "/lifecycle/v1/start",
    "STOP": "/lifecycle/v1/stop",
    "ACTIVATE_KILL_SWITCH": "/lifecycle/v1/kill-switch/activate",
    "RESET_KILL_SWITCH": "/lifecycle/v1/kill-switch/reset",
}


class TradingRunnerLifecycleTransportErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    OWNER_FAILURE = "OWNER_FAILURE"
    PAPER_ONLY = "PAPER_ONLY"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNREACHABLE = "UNREACHABLE"


class TradingRunnerLifecycleHttpError(RuntimeError):
    """Safe lifecycle owner transport failure without raw transport detail."""

    def __init__(
        self,
        code: TradingRunnerLifecycleTransportErrorCode,
        *,
        status_code: int | None = None,
    ) -> None:
        if not isinstance(code, TradingRunnerLifecycleTransportErrorCode):
            raise TypeError("code must be TradingRunnerLifecycleTransportErrorCode")
        super().__init__(f"Trading Runner lifecycle owner failed: {code.value}")
        self.code = code
        self.status_code = status_code


class TradingRunnerLifecycleOperation(StrEnum):
    START = "START"
    STOP = "STOP"
    ACTIVATE_KILL_SWITCH = "ACTIVATE_KILL_SWITCH"
    RESET_KILL_SWITCH = "RESET_KILL_SWITCH"


RuntimeState = Literal[
    "created",
    "configuring",
    "configured",
    "initializing",
    "recovering",
    "reconciling",
    "starting",
    "running",
    "degraded",
    "pausing",
    "paused",
    "stopping",
    "stopped",
    "failed",
    "kill_switch_active",
    "disposed",
]


class TradingRunnerLifecycleMutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    operator: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
    )
    operation: TradingRunnerLifecycleOperation
    previous_state: RuntimeState
    new_state: RuntimeState
    config_hash: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    completed_at: datetime
    replayed: bool

    @field_validator("completed_at")
    @classmethod
    def require_aware_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value.astimezone(UTC)


class TradingRunnerLifecycleHttpAdapter:
    """Forward bounded PAPER lifecycle mutations to the singleton owner transport."""

    def __init__(
        self,
        base_url: str,
        *,
        reader_token: str,
        operator_token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_trading_runner_lifecycle_base_url(base_url)
        self._reader_token = _token(reader_token, "reader_token")
        self._operator_token = _token(operator_token, "operator_token")
        if self._reader_token == self._operator_token:
            raise ValueError("Trading Runner lifecycle reader/operator tokens must be distinct")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if not 0.1 <= float(timeout_seconds) <= 300.0:
            raise ValueError("timeout_seconds must be between 0.1 and 300")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def mutate(
        self,
        operation: TradingRunnerLifecycleOperation,
        request_id: str,
    ) -> TradingRunnerLifecycleMutationResult:
        if not isinstance(operation, TradingRunnerLifecycleOperation):
            raise TypeError("operation must be TradingRunnerLifecycleOperation")
        _request_id(request_id)
        payload = self._request(
            _PATHS[operation.value],
            body=json.dumps(
                {"request_id": request_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        try:
            result = TradingRunnerLifecycleMutationResult.model_validate(payload)
        except ValidationError:
            raise TradingRunnerLifecycleHttpError(
                TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE
            ) from None
        if result.request_id != request_id or result.operation is not operation:
            raise TradingRunnerLifecycleHttpError(
                TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE
            )
        return result

    def _request(self, path: str, *, body: bytes) -> object:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._operator_token}",
            "Content-Type": "application/json",
        }
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream("POST", path, content=body, headers=headers) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise TradingRunnerLifecycleHttpError(
                            TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE,
                            status_code=response.status_code,
                        )
                status_code = response.status_code
        except TradingRunnerLifecycleHttpError:
            raise
        except httpx.HTTPError:
            raise TradingRunnerLifecycleHttpError(
                TradingRunnerLifecycleTransportErrorCode.UNREACHABLE
            ) from None

        payload = _decode(bytes(raw))
        if status_code == 200:
            return payload
        raise TradingRunnerLifecycleHttpError(
            _error_code(status_code, payload),
            status_code=status_code,
        )

    def __repr__(self) -> str:
        return (
            "TradingRunnerLifecycleHttpAdapter("
            f"base_url={self._base_url!r}, reader_token_configured=True, "
            "operator_token_configured=True)"
        )


def validate_trading_runner_lifecycle_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise TypeError("Trading Runner lifecycle URL must be str")
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("Trading Runner lifecycle URL must use HTTP on loopback")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("Trading Runner lifecycle URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Trading Runner lifecycle URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Trading Runner lifecycle URL must be a pathless loopback base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Trading Runner lifecycle URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Trading Runner lifecycle URL port must be between 1 and 65535")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}{f':{port}' if port is not None else ''}"


def _token(value: str, name: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")
    return value


def _request_id(value: str) -> None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise TradingRunnerLifecycleHttpError(
            TradingRunnerLifecycleTransportErrorCode.INVALID_REQUEST
        )


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TradingRunnerLifecycleHttpError(
            TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE
        ) from None


def _error_code(status_code: int, payload: object) -> TradingRunnerLifecycleTransportErrorCode:
    if status_code == 401:
        return TradingRunnerLifecycleTransportErrorCode.UNAUTHORIZED
    if isinstance(payload, dict):
        value = payload.get("error")
        if isinstance(value, str) and value in _OWNER_ERROR_CODES:
            return TradingRunnerLifecycleTransportErrorCode(value)
    return TradingRunnerLifecycleTransportErrorCode.OWNER_FAILURE


__all__ = [
    "TradingRunnerLifecycleHttpAdapter",
    "TradingRunnerLifecycleHttpError",
    "TradingRunnerLifecycleMutationResult",
    "TradingRunnerLifecycleOperation",
    "TradingRunnerLifecycleTransportErrorCode",
    "validate_trading_runner_lifecycle_base_url",
]
