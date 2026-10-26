from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

from .canonical import require_json_mapping
from .enums import HealthStatus
from .journal import RuntimeEventJournal
from .ports import RuntimeClock


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if result <= 0 or not result < float("inf"):
        raise ValueError(f"{name} must be positive and finite")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class SupervisionPolicy:
    heartbeat_timeout_seconds: float = 30.0
    max_consecutive_failures: int = 3
    notification_capacity: int = 1024
    shutdown_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "heartbeat_timeout_seconds",
            _positive_number(
                self.heartbeat_timeout_seconds,
                "heartbeat_timeout_seconds",
            ),
        )
        object.__setattr__(
            self,
            "max_consecutive_failures",
            _positive_int(
                self.max_consecutive_failures,
                "max_consecutive_failures",
            ),
        )
        object.__setattr__(
            self,
            "notification_capacity",
            _positive_int(self.notification_capacity, "notification_capacity"),
        )
        object.__setattr__(
            self,
            "shutdown_timeout_seconds",
            _positive_number(
                self.shutdown_timeout_seconds,
                "shutdown_timeout_seconds",
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    critical: bool
    status: HealthStatus
    last_heartbeat: datetime | None
    consecutive_failures: int
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeHealthSnapshot:
    observed_at: datetime
    ready: bool
    live: bool
    components: tuple[ComponentHealth, ...]

    @property
    def critical_failures(self) -> tuple[ComponentHealth, ...]:
        return tuple(
            component
            for component in self.components
            if component.critical and component.status is HealthStatus.CRITICAL
        )


@dataclass(slots=True)
class _MutableComponent:
    name: str
    critical: bool
    status: HealthStatus = HealthStatus.STARTING
    last_heartbeat: datetime | None = None
    consecutive_failures: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)


class RuntimeHealthRegistry:
    """Deterministic component health registry implementing the Runner readiness port."""

    def __init__(self, *, policy: SupervisionPolicy, clock: RuntimeClock) -> None:
        if not isinstance(policy, SupervisionPolicy):
            raise TypeError("policy must be SupervisionPolicy")
        self.policy = policy
        self.clock = clock
        self._components: dict[str, _MutableComponent] = {}

    def register(self, name: str, *, critical: bool) -> None:
        normalized = _text(name, "component name")
        if not isinstance(critical, bool):
            raise TypeError("critical must be bool")
        existing = self._components.get(normalized)
        if existing is not None:
            if existing.critical != critical:
                raise ValueError("component criticality cannot change")
            return
        self._components[normalized] = _MutableComponent(
            name=normalized,
            critical=critical,
        )

    def heartbeat(
        self,
        name: str,
        *,
        status: HealthStatus = HealthStatus.HEALTHY,
        timestamp: datetime | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        component = self._require(name)
        if not isinstance(status, HealthStatus):
            raise TypeError("status must be HealthStatus")
        moment = _utc(timestamp or self.clock.now(), "heartbeat timestamp")
        if component.last_heartbeat is not None and moment < component.last_heartbeat:
            raise ValueError("component heartbeat cannot move backwards")
        component.last_heartbeat = moment
        component.status = status
        component.details = MappingProxyType(
            dict(require_json_mapping(details or {}, "health details"))
        )
        if status is HealthStatus.HEALTHY:
            component.consecutive_failures = 0

    def record_failure(
        self,
        name: str,
        *,
        error: BaseException | str,
        timestamp: datetime | None = None,
    ) -> None:
        component = self._require(name)
        moment = _utc(timestamp or self.clock.now(), "failure timestamp")
        if component.last_heartbeat is not None and moment < component.last_heartbeat:
            raise ValueError("component failure timestamp cannot move backwards")
        component.last_heartbeat = moment
        component.consecutive_failures += 1
        component.status = (
            HealthStatus.CRITICAL
            if component.critical
            and component.consecutive_failures >= self.policy.max_consecutive_failures
            else HealthStatus.UNHEALTHY
        )
        component.details = MappingProxyType(
            {
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )

    def snapshot(self, *, now: datetime | None = None) -> RuntimeHealthSnapshot:
        moment = _utc(now or self.clock.now(), "health snapshot timestamp")
        components: list[ComponentHealth] = []
        for component in sorted(self._components.values(), key=lambda item: item.name):
            status = component.status
            if component.last_heartbeat is None:
                status = HealthStatus.STARTING
            else:
                age = (moment - component.last_heartbeat).total_seconds()
                if age < 0:
                    raise ValueError("health snapshot precedes component heartbeat")
                if age > self.policy.heartbeat_timeout_seconds:
                    status = HealthStatus.CRITICAL if component.critical else HealthStatus.DEGRADED
            components.append(
                ComponentHealth(
                    name=component.name,
                    critical=component.critical,
                    status=status,
                    last_heartbeat=component.last_heartbeat,
                    consecutive_failures=component.consecutive_failures,
                    details=component.details,
                )
            )
        critical = tuple(item for item in components if item.critical)
        ready = bool(critical) and all(item.status is HealthStatus.HEALTHY for item in critical)
        live = any(
            item.status not in {HealthStatus.CRITICAL, HealthStatus.STOPPED} for item in components
        )
        return RuntimeHealthSnapshot(
            observed_at=moment,
            ready=ready,
            live=live,
            components=tuple(components),
        )

    def is_ready(self) -> bool:
        return self.snapshot().ready

    def is_live(self) -> bool:
        return self.snapshot().live

    def _require(self, name: str) -> _MutableComponent:
        normalized = _text(name, "component name")
        try:
            return self._components[normalized]
        except KeyError as exc:
            raise KeyError(f"component is not registered: {normalized}") from exc


class KillSwitchBoundary(Protocol):
    def activate_kill_switch(self, *, reason: str) -> object: ...


class CheckpointCaptureBoundary(Protocol):
    def capture(self) -> object: ...


class WatchdogSupervisor:
    """Trips the existing kill switch exactly once per critical health signature."""

    def __init__(
        self,
        *,
        health: RuntimeHealthRegistry,
        kill_switch: KillSwitchBoundary,
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
        checkpoint: CheckpointCaptureBoundary | None = None,
    ) -> None:
        if not isinstance(health, RuntimeHealthRegistry):
            raise TypeError("health must be RuntimeHealthRegistry")
        if not callable(getattr(kill_switch, "activate_kill_switch", None)):
            raise TypeError("kill_switch must implement activate_kill_switch()")
        if not isinstance(journal, RuntimeEventJournal):
            raise TypeError("journal must be RuntimeEventJournal")
        if checkpoint is not None and not callable(getattr(checkpoint, "capture", None)):
            raise TypeError("checkpoint must implement capture() or be None")
        self.health = health
        self.kill_switch = kill_switch
        self.journal = journal
        self.clock = clock
        self.checkpoint = checkpoint
        self._last_signature: tuple[tuple[str, str], ...] | None = None
        self._checkpoint_pending = False

    async def check_once(self) -> bool:
        snapshot = self.health.snapshot()
        failures = snapshot.critical_failures
        if not failures:
            self._last_signature = None
            self._checkpoint_pending = False
            return False
        signature = tuple((item.name, item.status.value) for item in failures)
        if signature == self._last_signature:
            if self._checkpoint_pending:
                self._checkpoint_pending = not await self._capture_checkpoint()
            return False
        reason = "watchdog:" + ",".join(f"{name}={status}" for name, status in signature)
        self.journal.append(
            timestamp=_utc(self.clock.now(), "clock.now"),
            event_type="watchdog.tripped",
            aggregate_id="runtime-watchdog",
            payload={
                "reason": reason,
                "components": [
                    {
                        "name": item.name,
                        "status": item.status.value,
                        "consecutive_failures": item.consecutive_failures,
                    }
                    for item in failures
                ],
            },
        )
        try:
            await _await_if_needed(self.kill_switch.activate_kill_switch(reason=reason))
        except Exception as exc:
            self.journal.append(
                timestamp=_utc(self.clock.now(), "clock.now"),
                event_type="watchdog.activation_failed",
                aggregate_id="runtime-watchdog",
                payload={
                    "reason": reason,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        self._last_signature = signature
        self._checkpoint_pending = not await self._capture_checkpoint()
        return True

    async def _capture_checkpoint(self) -> bool:
        if self.checkpoint is None:
            return True
        try:
            await _await_if_needed(self.checkpoint.capture())
        except Exception as exc:
            self.journal.append(
                timestamp=_utc(self.clock.now(), "clock.now"),
                event_type="watchdog.checkpoint_failed",
                aggregate_id="runtime-watchdog",
                payload={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return False
        return True


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    event_type: str
    created_at: datetime
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "event_type", _text(self.event_type, "event_type"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(require_json_mapping(self.payload, "notification payload"))),
        )


class NotificationSink(Protocol):
    def send(self, event: NotificationEvent) -> object: ...


@dataclass(frozen=True, slots=True)
class NotificationStats:
    queued: int
    sent: int
    failed: int
    dropped: int
    high_water_mark: int


class IsolatedNotificationDispatcher:
    """Best-effort notification side channel that cannot fail the trading path."""

    def __init__(
        self,
        *,
        sink: NotificationSink,
        capacity: int,
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
    ) -> None:
        if not callable(getattr(sink, "send", None)):
            raise TypeError("sink must implement send()")
        self.sink = sink
        self.capacity = _positive_int(capacity, "capacity")
        self.journal = journal
        self.clock = clock
        self._queue: deque[NotificationEvent] = deque()
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._high_water_mark = 0

    def publish(self, event: NotificationEvent) -> bool:
        if not isinstance(event, NotificationEvent):
            raise TypeError("event must be NotificationEvent")
        if len(self._queue) >= self.capacity:
            self._dropped += 1
            self._audit(
                "notification.dropped",
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "reason": "queue_capacity",
                },
            )
            return False
        self._queue.append(event)
        self._high_water_mark = max(self._high_water_mark, len(self._queue))
        return True

    async def drain(self, *, limit: int | None = None) -> NotificationStats:
        if limit is not None:
            limit = _positive_int(limit, "limit")
        processed = 0
        while self._queue and (limit is None or processed < limit):
            event = self._queue.popleft()
            processed += 1
            try:
                await _await_if_needed(self.sink.send(event))
            except Exception as exc:
                self._failed += 1
                self._audit(
                    "notification.failed",
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                continue
            self._sent += 1
        return self.stats()

    def stats(self) -> NotificationStats:
        return NotificationStats(
            queued=len(self._queue),
            sent=self._sent,
            failed=self._failed,
            dropped=self._dropped,
            high_water_mark=self._high_water_mark,
        )

    def _audit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        try:
            self.journal.append(
                timestamp=_utc(self.clock.now(), "clock.now"),
                event_type=event_type,
                aggregate_id="notification-dispatcher",
                payload=payload,
            )
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class ShutdownOperation:
    name: str
    callback: Callable[[], object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "shutdown operation name"))
        if not callable(self.callback):
            raise TypeError("shutdown operation callback must be callable")


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    graceful: bool
    forced: bool
    completed: tuple[str, ...]
    errors: tuple[str, ...]


class ShutdownSupervisor:
    """Bounded graceful shutdown with an explicit forced-stop fallback."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        force_stop: Callable[[], object],
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
    ) -> None:
        self.timeout_seconds = _positive_number(timeout_seconds, "timeout_seconds")
        if not callable(force_stop):
            raise TypeError("force_stop must be callable")
        self.force_stop = force_stop
        self.journal = journal
        self.clock = clock

    async def shutdown(self, operations: Sequence[ShutdownOperation]) -> ShutdownReport:
        if isinstance(operations, (str, bytes)) or not isinstance(operations, Sequence):
            raise TypeError("operations must be a sequence")
        normalized = tuple(operations)
        if any(not isinstance(item, ShutdownOperation) for item in normalized):
            raise TypeError("operations must contain ShutdownOperation values")
        completed: list[str] = []
        errors: list[str] = []

        async def execute() -> None:
            for operation in normalized:
                try:
                    await _await_if_needed(operation.callback())
                except Exception as exc:
                    errors.append(f"{operation.name}:{type(exc).__name__}:{exc}")
                    continue
                completed.append(operation.name)

        forced = False
        try:
            await asyncio.wait_for(execute(), timeout=self.timeout_seconds)
        except TimeoutError:
            errors.append("shutdown_timeout")
            forced = True
        if errors:
            forced = True
        if forced:
            try:
                await _await_if_needed(self.force_stop())
            except Exception as exc:
                errors.append(f"force_stop:{type(exc).__name__}:{exc}")
        report = ShutdownReport(
            graceful=not forced,
            forced=forced,
            completed=tuple(completed),
            errors=tuple(errors),
        )
        self.journal.append(
            timestamp=_utc(self.clock.now(), "clock.now"),
            event_type="runtime.shutdown_completed",
            aggregate_id="runtime-shutdown",
            payload={
                "graceful": report.graceful,
                "forced": report.forced,
                "completed": report.completed,
                "errors": report.errors,
            },
        )
        return report


async def _await_if_needed(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value
