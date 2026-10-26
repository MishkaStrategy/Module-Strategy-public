from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .adapters.trading_runner_events_http import (
    TradingRunnerEventsSeverity,
    TradingRunnerOperationalEventsHttpAdapter,
)
from .contracts import ContractModel, SafeScalar


class OperationalEventSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class OperationalEvent(ContractModel):
    event_id: str = Field(min_length=1, max_length=64)
    source: Literal["trading_runner"]
    resource: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=128)
    severity: OperationalEventSeverity
    state: str | None = Field(default=None, max_length=64)
    payload: dict[str, SafeScalar]
    cursor: str = Field(min_length=1, max_length=128)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        if len(value) > 16:
            raise ValueError("payload has too many fields")
        normalized: dict[str, SafeScalar] = {}
        for key, item in value.items():
            if not key or len(key) > 64:
                raise ValueError("payload key is invalid")
            if isinstance(item, str) and len(item) > 256:
                raise ValueError("payload string is too large")
            normalized[key] = item
        return normalized


class OperationalEventPage(ContractModel):
    source: Literal["trading_runner"]
    events: tuple[OperationalEvent, ...]
    next_cursor: str = Field(min_length=1, max_length=128)
    has_more: bool
    scanned_count: int = Field(ge=0, le=1000)
    omitted_count: int = Field(ge=0, le=1000)
    retention_limit: int = Field(ge=1, le=1_000_000)
    truncated: bool

    @model_validator(mode="after")
    def validate_counts(self) -> OperationalEventPage:
        if self.omitted_count > self.scanned_count:
            raise ValueError("omitted_count exceeds scanned_count")
        if len(self.events) > self.scanned_count:
            raise ValueError("event count exceeds scanned_count")
        return self


class OperationalEventsControlService:
    """Stateless typed projection over accepted owner event cursors."""

    def __init__(self, adapter: TradingRunnerOperationalEventsHttpAdapter) -> None:
        if not isinstance(adapter, TradingRunnerOperationalEventsHttpAdapter):
            raise TypeError("adapter must be TradingRunnerOperationalEventsHttpAdapter")
        self._adapter = adapter

    def query(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        event_types: tuple[str, ...] = (),
        severities: tuple[OperationalEventSeverity, ...] = (),
    ) -> OperationalEventPage:
        page = self._adapter.query(
            cursor=cursor,
            limit=limit,
            event_types=event_types or None,
            severities=(
                tuple(TradingRunnerEventsSeverity(value.value) for value in severities)
                if severities
                else None
            ),
        )
        return OperationalEventPage(
            source="trading_runner",
            events=tuple(
                OperationalEvent(
                    event_id=event.event_id,
                    source="trading_runner",
                    resource=event.resource,
                    timestamp=event.timestamp,
                    event_type=event.event_type,
                    severity=OperationalEventSeverity(event.severity.value),
                    state=event.state,
                    payload=event.payload,
                    cursor=event.cursor,
                )
                for event in page.events
            ),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            scanned_count=page.scanned_count,
            omitted_count=page.omitted_count,
            retention_limit=page.retention_limit,
            truncated=page.truncated,
        )


__all__ = [
    "OperationalEvent",
    "OperationalEventPage",
    "OperationalEventSeverity",
    "OperationalEventsControlService",
]
