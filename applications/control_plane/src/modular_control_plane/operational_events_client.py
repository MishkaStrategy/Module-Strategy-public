from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlencode

from pydantic import ValidationError

from .client import ControlClient, ControlClientError
from .operational_events import OperationalEventPage, OperationalEventSeverity

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


class OperationalEventsControlClient(ControlClient):
    """Typed CLI client for bounded owner-backed operational event reads."""

    def operational_events(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        event_types: Sequence[str] = (),
        severities: Sequence[OperationalEventSeverity] = (),
    ) -> OperationalEventPage:
        parameters = _query_parameters(
            cursor=cursor,
            limit=limit,
            event_types=event_types,
            severities=severities,
        )
        payload = self._get(
            f"/api/control/v1/events/trading-runner?{urlencode(parameters, doseq=True)}"
        )
        try:
            return OperationalEventPage.model_validate(payload)
        except ValidationError as exc:
            raise ControlClientError(
                "CONTROL_RESPONSE_INVALID",
                "Control Plane returned an incompatible operational-events response.",
                action="Verify that modular-control and modular-control-api use compatible versions.",
            ) from exc


def _query_parameters(
    *,
    cursor: str | None,
    limit: int,
    event_types: Sequence[str],
    severities: Sequence[OperationalEventSeverity],
) -> list[tuple[str, str]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    result: list[tuple[str, str]] = [("limit", str(limit))]
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.startswith("trr-v1:") or len(cursor) > 128:
            raise ValueError("cursor must be an opaque Trading Runner cursor")
        result.append(("cursor", cursor))
    if isinstance(event_types, (str, bytes)):
        raise TypeError("event_types must be a sequence")
    normalized_types = tuple(event_types)
    if len(normalized_types) > len(_ACCEPTED_EVENT_TYPES) or len(set(normalized_types)) != len(
        normalized_types
    ):
        raise ValueError("event_types contain duplicates or too many values")
    if any(value not in _ACCEPTED_EVENT_TYPES for value in normalized_types):
        raise ValueError("event_types contain an unsupported value")
    result.extend(("event_type", value) for value in normalized_types)
    if isinstance(severities, (str, bytes)):
        raise TypeError("severities must be a sequence")
    normalized_severities = tuple(severities)
    if len(normalized_severities) > len(OperationalEventSeverity) or len(
        set(normalized_severities)
    ) != len(normalized_severities):
        raise ValueError("severities contain duplicates or too many values")
    if any(not isinstance(value, OperationalEventSeverity) for value in normalized_severities):
        raise TypeError("severities must contain OperationalEventSeverity values")
    result.extend(("severity", value.value) for value in normalized_severities)
    return result


__all__ = ["OperationalEventsControlClient"]
