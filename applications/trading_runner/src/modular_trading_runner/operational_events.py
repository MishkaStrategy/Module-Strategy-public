from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType

from modular_core import EventEnvelope

from .enums import KillSwitchScope, RuntimeState
from .journal import RuntimeEventJournal

_RESOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OPERATOR_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}\Z")
_CURSOR_PATTERN = re.compile(r"trr-v1:([0-9a-f]{16}):([0-9]{1,7})\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID_PATTERN = re.compile(r"trr-[0-9]{12}-[0-9a-f]{16}\Z")
_MAX_PAGE_SIZE = 200
_MAX_SCAN_PER_PAGE = 1_000
_RETENTION_LIMIT = 1_000_000

type Scalar = str | int | float | bool | None


class OperationalSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class OperationalEventQueryErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CURSOR = "INVALID_CURSOR"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class OperationalEventQueryError(RuntimeError):
    def __init__(self, code: OperationalEventQueryErrorCode, message: str) -> None:
        if not isinstance(code, OperationalEventQueryErrorCode):
            raise TypeError("code must be OperationalEventQueryErrorCode")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StructuredOperationalEvent:
    event_id: str
    source: str
    resource: str
    timestamp: datetime
    event_type: str
    severity: OperationalSeverity
    state: str | None
    payload: Mapping[str, Scalar]
    cursor: str


@dataclass(frozen=True, slots=True)
class StructuredOperationalEventPage:
    source: str
    events: tuple[StructuredOperationalEvent, ...]
    next_cursor: str
    has_more: bool
    scanned_count: int
    omitted_count: int
    retention_limit: int
    truncated: bool


class TradingRunnerOperationalEventQuery:
    """Stateless sanitized query over the authoritative Trading Runner journal."""

    def __init__(self, journal: RuntimeEventJournal, *, resource: str) -> None:
        if not isinstance(journal, RuntimeEventJournal):
            raise TypeError("journal must be RuntimeEventJournal")
        if not isinstance(resource, str) or _RESOURCE_PATTERN.fullmatch(resource) is None:
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_REQUEST,
                "resource identity is invalid",
            )
        self._journal = journal
        self._resource = resource
        self._stream_id = sha256(journal.run_id.encode("utf-8")).hexdigest()[:16]

    def query(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        event_types: Sequence[str] | None = None,
        severities: Sequence[OperationalSeverity] | None = None,
    ) -> StructuredOperationalEventPage:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_REQUEST,
                "limit must be between 1 and 200",
            )
        allowed_event_types = _event_type_filter(event_types)
        allowed_severities = _severity_filter(severities)
        after = self._parse_cursor(cursor)
        try:
            if not self._journal.verify_chain():
                raise OperationalEventQueryError(
                    OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE,
                    "Trading Runner operational event source is unavailable",
                )
            owner_events = self._journal.read()
        except OperationalEventQueryError:
            raise
        except Exception:
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE,
                "Trading Runner operational event source is unavailable",
            ) from None
        if after > len(owner_events):
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_CURSOR,
                "cursor is ahead of the owner event stream",
            )

        projected: list[StructuredOperationalEvent] = []
        scanned_count = 0
        omitted_count = 0
        last_scanned = after
        for owner_event in owner_events[after:]:
            if scanned_count >= _MAX_SCAN_PER_PAGE:
                break
            scanned_count += 1
            last_scanned = owner_event.sequence
            event = self._project(owner_event)
            if event is None or not _matches_filters(
                event,
                event_types=allowed_event_types,
                severities=allowed_severities,
            ):
                omitted_count += 1
            else:
                projected.append(event)
                if len(projected) >= limit:
                    break
        return StructuredOperationalEventPage(
            source="trading_runner",
            events=tuple(projected),
            next_cursor=self._cursor(last_scanned),
            has_more=last_scanned < len(owner_events),
            scanned_count=scanned_count,
            omitted_count=omitted_count,
            retention_limit=_RETENTION_LIMIT,
            truncated=False,
        )

    def _parse_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not isinstance(cursor, str):
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_CURSOR,
                "cursor is invalid",
            )
        match = _CURSOR_PATTERN.fullmatch(cursor)
        if match is None or match.group(1) != self._stream_id:
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_CURSOR,
                "cursor does not belong to this owner stream",
            )
        return int(match.group(2))

    def _cursor(self, sequence: int) -> str:
        return f"trr-v1:{self._stream_id}:{sequence}"

    def _project(self, event: EventEnvelope) -> StructuredOperationalEvent | None:
        sanitizer = _SANITIZERS.get(event.event_type)
        if sanitizer is None or _EVENT_ID_PATTERN.fullmatch(event.event_id) is None:
            return None
        payload, state, severity = sanitizer(event.payload)
        return StructuredOperationalEvent(
            event_id=event.event_id,
            source="trading_runner",
            resource=self._resource,
            timestamp=_utc(event.timestamp),
            event_type=event.event_type,
            severity=severity,
            state=state,
            payload=MappingProxyType(payload),
            cursor=self._cursor(event.sequence),
        )


def _event_type_filter(values: Sequence[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise OperationalEventQueryError(
            OperationalEventQueryErrorCode.INVALID_REQUEST,
            "event_types filter is invalid",
        )
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value not in _SANITIZERS:
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_REQUEST,
                "event_types filter is invalid",
            )
        normalized.add(value)
    if len(normalized) != len(values):
        raise OperationalEventQueryError(
            OperationalEventQueryErrorCode.INVALID_REQUEST,
            "event_types filter contains duplicates",
        )
    return frozenset(normalized)


def _severity_filter(
    values: Sequence[OperationalSeverity] | None,
) -> frozenset[OperationalSeverity] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise OperationalEventQueryError(
            OperationalEventQueryErrorCode.INVALID_REQUEST,
            "severities filter is invalid",
        )
    normalized: set[OperationalSeverity] = set()
    for value in values:
        if not isinstance(value, OperationalSeverity):
            raise OperationalEventQueryError(
                OperationalEventQueryErrorCode.INVALID_REQUEST,
                "severities filter is invalid",
            )
        normalized.add(value)
    if len(normalized) != len(values):
        raise OperationalEventQueryError(
            OperationalEventQueryErrorCode.INVALID_REQUEST,
            "severities filter contains duplicates",
        )
    return frozenset(normalized)


def _matches_filters(
    event: StructuredOperationalEvent,
    *,
    event_types: frozenset[str] | None,
    severities: frozenset[OperationalSeverity] | None,
) -> bool:
    if event_types is not None and event.event_type not in event_types:
        return False
    if severities is not None and event.severity not in severities:
        return False
    return True


def _runtime_configuring(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    result: dict[str, Scalar] = {}
    config_hash = _digest(payload.get("config_hash"))
    if config_hash is not None:
        result["config_hash"] = config_hash
    return result, RuntimeState.CONFIGURING.value, OperationalSeverity.INFO


def _runtime_started(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    state = _runtime_state(payload.get("state"))
    result: dict[str, Scalar] = {}
    if state is not None:
        result["state"] = state
    return result, state, OperationalSeverity.INFO


def _runtime_failed(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    result: dict[str, Scalar] = {}
    error_type = _error_type(payload.get("error_type"))
    if error_type is not None:
        result["error_type"] = error_type
    return result, RuntimeState.FAILED.value, OperationalSeverity.ERROR


def _runtime_stopping(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    return {}, RuntimeState.STOPPING.value, OperationalSeverity.INFO


def _runtime_stopped(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    return {}, RuntimeState.STOPPED.value, OperationalSeverity.INFO


def _kill_activated(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    result: dict[str, Scalar] = {}
    scope = payload.get("scope")
    if isinstance(scope, str) and scope in {item.value for item in KillSwitchScope}:
        result["scope"] = scope
    cancellation_pending = payload.get("cancellation_pending")
    if isinstance(cancellation_pending, bool):
        result["cancellation_pending"] = cancellation_pending
    return result, RuntimeState.KILL_SWITCH_ACTIVE.value, OperationalSeverity.WARNING


def _kill_reset(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    return {}, None, OperationalSeverity.INFO


def _kill_retry(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    result: dict[str, Scalar] = {}
    succeeded = payload.get("succeeded")
    if isinstance(succeeded, bool):
        result["succeeded"] = succeeded
    return result, RuntimeState.KILL_SWITCH_ACTIVE.value, OperationalSeverity.WARNING


def _operator_lifecycle(
    payload: Mapping[str, object],
) -> tuple[dict[str, Scalar], str | None, OperationalSeverity]:
    result: dict[str, Scalar] = {}
    operation = payload.get("operation")
    if operation in {"START", "STOP", "ACTIVATE_KILL_SWITCH", "RESET_KILL_SWITCH"}:
        result["operation"] = str(operation)
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and _REQUEST_ID_PATTERN.fullmatch(request_id) is not None:
        result["request_id"] = request_id
    operator = payload.get("operator")
    if isinstance(operator, str) and _OPERATOR_PATTERN.fullmatch(operator) is not None:
        result["operator"] = operator
    result_value = payload.get("result")
    if isinstance(result_value, str) and result_value in _OPERATOR_RESULTS:
        result["result"] = result_value
    previous_state = _runtime_state(payload.get("previous_state"))
    if previous_state is not None:
        result["previous_state"] = previous_state
    new_state = _runtime_state(payload.get("new_state"))
    if new_state is not None:
        result["new_state"] = new_state
    config_hash = _digest(payload.get("config_hash"))
    if config_hash is not None:
        result["config_hash"] = config_hash
    replayed = payload.get("replayed")
    if isinstance(replayed, bool):
        result["replayed"] = replayed
    error_code = payload.get("error_code")
    if isinstance(error_code, str) and error_code in _OPERATOR_ERRORS:
        result["error_code"] = error_code
    severity = (
        OperationalSeverity.ERROR
        if error_code in {"RECOVERY_REQUIRED", "OWNER_FAILURE"}
        else OperationalSeverity.WARNING
        if error_code is not None or result_value == "IDEMPOTENCY_CONFLICT"
        else OperationalSeverity.INFO
    )
    return result, new_state, severity


def _runtime_state(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return RuntimeState(value).value
    except ValueError:
        return None


def _digest(value: object) -> str | None:
    return (
        value if isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None else None
    )


def _error_type(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and _ERROR_TYPE_PATTERN.fullmatch(value) is not None
        else None
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OperationalEventQueryError(
            OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE,
            "Trading Runner operational event timestamp is invalid",
        )
    return value.astimezone(UTC)


_OPERATOR_ERRORS = frozenset(
    {
        "INVALID_REQUEST",
        "INVALID_TRANSITION",
        "RECOVERY_REQUIRED",
        "IDEMPOTENCY_CONFLICT",
        "OWNER_FAILURE",
        "PAPER_ONLY",
    }
)
_OPERATOR_RESULTS = frozenset({"ACCEPTED", "SUCCEEDED", "REPLAYED", *_OPERATOR_ERRORS})
_SANITIZERS = {
    "runtime.configuring": _runtime_configuring,
    "runtime.started": _runtime_started,
    "runtime.failed": _runtime_failed,
    "runtime.stopping": _runtime_stopping,
    "runtime.stopped": _runtime_stopped,
    "kill_switch.activated": _kill_activated,
    "kill_switch.reset": _kill_reset,
    "kill_switch.cancellation_retry": _kill_retry,
    "operator.lifecycle": _operator_lifecycle,
}
