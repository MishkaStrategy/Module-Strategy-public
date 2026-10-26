from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_QUERY_BYTES = 4096
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CURSOR = re.compile(r"trr-v1:[0-9a-f]{16}:[0-9]{1,7}\Z")
_EVENT_ID = re.compile(r"trr-[0-9]{12}-[0-9a-f]{16}\Z")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EVENT_TYPE = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_PAYLOAD_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_ACCEPTED_EVENT_TYPES = frozenset(
    {
        "runtime.configuring",
        "runtime.started",
        "runtime.failed",
        "runtime.stopping",
        "runtime.stopped",
        "kill_switch.activated",
        "kill_switch.reset",
        "kill_switch.cancellation_retry",
        "operator.lifecycle",
    }
)
_OWNER_ERROR_CODES = frozenset({"INVALID_REQUEST", "INVALID_CURSOR", "SOURCE_UNAVAILABLE"})
type Scalar = str | int | float | bool | None


class TradingRunnerEventsSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class TradingRunnerEventsTransportErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CURSOR = "INVALID_CURSOR"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNREACHABLE = "UNREACHABLE"
    OWNER_FAILURE = "OWNER_FAILURE"


class TradingRunnerEventsHttpError(RuntimeError):
    """Safe owner-event transport failure without raw response/exception detail."""

    def __init__(
        self,
        code: TradingRunnerEventsTransportErrorCode,
        *,
        status_code: int | None = None,
    ) -> None:
        if not isinstance(code, TradingRunnerEventsTransportErrorCode):
            raise TypeError("code must be TradingRunnerEventsTransportErrorCode")
        super().__init__(f"Trading Runner operational events owner failed: {code.value}")
        self.code = code
        self.status_code = status_code


class TradingRunnerOperationalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^trr-[0-9]{12}-[0-9a-f]{16}$")
    source: str
    resource: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=128)
    severity: TradingRunnerEventsSeverity
    state: str | None = Field(default=None, max_length=64)
    payload: dict[str, Scalar]
    cursor: str = Field(pattern=r"^trr-v1:[0-9a-f]{16}:[0-9]{1,7}$")

    @field_validator("source")
    @classmethod
    def require_source(cls, value: str) -> str:
        if value != "trading_runner":
            raise ValueError("source must be trading_runner")
        return value

    @field_validator("resource")
    @classmethod
    def validate_resource(cls, value: str) -> str:
        if _RESOURCE.fullmatch(value) is None:
            raise ValueError("resource is invalid")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if _EVENT_TYPE.fullmatch(value) is None or value not in _ACCEPTED_EVENT_TYPES:
            raise ValueError("event_type is not accepted")
        return value

    @field_validator("state")
    @classmethod
    def validate_state(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value.strip() != value or len(value) > 64:
            raise ValueError("state is invalid")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Scalar]) -> dict[str, Scalar]:
        if len(value) > 16:
            raise ValueError("payload has too many fields")
        for key, item in value.items():
            if _PAYLOAD_KEY.fullmatch(key) is None:
                raise ValueError("payload key is invalid")
            if isinstance(item, str):
                if len(item) > 256:
                    raise ValueError("payload string is too large")
            elif isinstance(item, float) and not isfinite(item):
                raise ValueError("payload float must be finite")
        return dict(value)


class TradingRunnerOperationalEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    events: tuple[TradingRunnerOperationalEvent, ...]
    next_cursor: str = Field(pattern=r"^trr-v1:[0-9a-f]{16}:[0-9]{1,7}$")
    has_more: bool
    scanned_count: int = Field(ge=0, le=1000)
    omitted_count: int = Field(ge=0, le=1000)
    retention_limit: int = Field(ge=1, le=1_000_000)
    truncated: bool

    @field_validator("source")
    @classmethod
    def require_source(cls, value: str) -> str:
        if value != "trading_runner":
            raise ValueError("source must be trading_runner")
        return value

    @model_validator(mode="after")
    def validate_counts_and_stream(self) -> TradingRunnerOperationalEventPage:
        if self.omitted_count > self.scanned_count:
            raise ValueError("omitted_count exceeds scanned_count")
        if len(self.events) > self.scanned_count:
            raise ValueError("event count exceeds scanned_count")
        stream = self.next_cursor.split(":", 2)[1]
        if any(event.cursor.split(":", 2)[1] != stream for event in self.events):
            raise ValueError("event cursors do not belong to page stream")
        return self


class TradingRunnerOperationalEventsHttpAdapter:
    """Read sanitized Trading Runner owner events through the accepted loopback transport."""

    def __init__(
        self,
        base_url: str,
        *,
        reader_token: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = validate_trading_runner_events_base_url(base_url)
        self._reader_token = _token(reader_token)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if not 0.1 <= float(timeout_seconds) <= 30.0:
            raise ValueError("timeout_seconds must be between 0.1 and 30")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def query(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        event_types: Sequence[str] | None = None,
        severities: Sequence[TradingRunnerEventsSeverity] | None = None,
    ) -> TradingRunnerOperationalEventPage:
        normalized_cursor = _cursor(cursor)
        normalized_limit = _limit(limit)
        normalized_types = _event_types(event_types)
        normalized_severities = _severities(severities)
        parameters: list[tuple[str, str]] = [("limit", str(normalized_limit))]
        if normalized_cursor is not None:
            parameters.append(("cursor", normalized_cursor))
        parameters.extend(("event_type", value) for value in normalized_types)
        parameters.extend(("severity", value.value) for value in normalized_severities)
        query = urlencode(parameters)
        if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
            raise TradingRunnerEventsHttpError(
                TradingRunnerEventsTransportErrorCode.INVALID_REQUEST
            )
        payload = self._request(f"/events/v1/query?{query}")
        try:
            page = TradingRunnerOperationalEventPage.model_validate(payload)
        except ValidationError:
            raise TradingRunnerEventsHttpError(
                TradingRunnerEventsTransportErrorCode.OWNER_FAILURE
            ) from None
        if len(page.events) > normalized_limit:
            raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.OWNER_FAILURE)
        return page

    def _request(self, path: str) -> object:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._reader_token}",
        }
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream("GET", path, headers=headers) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise TradingRunnerEventsHttpError(
                            TradingRunnerEventsTransportErrorCode.OWNER_FAILURE,
                            status_code=response.status_code,
                        )
                status_code = response.status_code
        except TradingRunnerEventsHttpError:
            raise
        except httpx.HTTPError:
            raise TradingRunnerEventsHttpError(
                TradingRunnerEventsTransportErrorCode.UNREACHABLE
            ) from None
        payload = _decode(bytes(raw))
        if status_code == 200:
            return payload
        raise TradingRunnerEventsHttpError(
            _error_code(status_code, payload),
            status_code=status_code,
        )

    def __repr__(self) -> str:
        return (
            "TradingRunnerOperationalEventsHttpAdapter("
            f"base_url={self._base_url!r}, reader_token_configured=True)"
        )


def validate_trading_runner_events_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise TypeError("Trading Runner operational events URL must be str")
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("Trading Runner operational events URL must use HTTP on loopback")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("Trading Runner operational events URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Trading Runner operational events URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "Trading Runner operational events URL must be a pathless loopback base URL"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Trading Runner operational events URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Trading Runner operational events URL port must be between 1 and 65535")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}{f':{port}' if port is not None else ''}"


def _token(value: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError("reader_token must contain 16..4096 characters")
    return value


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CURSOR.fullmatch(value) is None:
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_CURSOR)
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    return value


def _event_types(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    result = tuple(values)
    if len(result) > len(_ACCEPTED_EVENT_TYPES) or len(set(result)) != len(result):
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    if any(not isinstance(value, str) or value not in _ACCEPTED_EVENT_TYPES for value in result):
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    return result


def _severities(
    values: Sequence[TradingRunnerEventsSeverity] | None,
) -> tuple[TradingRunnerEventsSeverity, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    result = tuple(values)
    if len(result) > len(TradingRunnerEventsSeverity) or len(set(result)) != len(result):
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    if any(not isinstance(value, TradingRunnerEventsSeverity) for value in result):
        raise TradingRunnerEventsHttpError(TradingRunnerEventsTransportErrorCode.INVALID_REQUEST)
    return result


def _decode(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TradingRunnerEventsHttpError(
            TradingRunnerEventsTransportErrorCode.OWNER_FAILURE
        ) from None


def _error_code(status_code: int, payload: object) -> TradingRunnerEventsTransportErrorCode:
    if status_code == 401:
        return TradingRunnerEventsTransportErrorCode.UNAUTHORIZED
    if isinstance(payload, dict):
        value = payload.get("error")
        if isinstance(value, str) and value in _OWNER_ERROR_CODES:
            return TradingRunnerEventsTransportErrorCode(value)
    return TradingRunnerEventsTransportErrorCode.OWNER_FAILURE


__all__ = [
    "TradingRunnerEventsHttpError",
    "TradingRunnerEventsSeverity",
    "TradingRunnerEventsTransportErrorCode",
    "TradingRunnerOperationalEvent",
    "TradingRunnerOperationalEventPage",
    "TradingRunnerOperationalEventsHttpAdapter",
    "validate_trading_runner_events_base_url",
]
