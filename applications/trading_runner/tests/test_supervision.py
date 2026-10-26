from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from modular_trading_runner.enums import HealthStatus
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.supervision import (
    IsolatedNotificationDispatcher,
    NotificationEvent,
    RuntimeHealthRegistry,
    ShutdownOperation,
    ShutdownSupervisor,
    SupervisionPolicy,
    WatchdogSupervisor,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class KillSwitch:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.reasons: list[str] = []
        self.fail_once = fail_once

    async def activate_kill_switch(self, *, reason: str) -> None:
        self.reasons.append(reason)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("kill switch unavailable")


class Checkpoint:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def capture(self) -> object:
        self.calls += 1
        if self.fail:
            raise RuntimeError("checkpoint unavailable")
        return {"captured": True}


class Sink:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_ids: set[str] = set()

    async def send(self, event: NotificationEvent) -> None:
        if event.event_id in self.fail_ids:
            raise RuntimeError("notification unavailable")
        self.sent.append(event.event_id)


def policy() -> SupervisionPolicy:
    return SupervisionPolicy(
        heartbeat_timeout_seconds=10.0,
        max_consecutive_failures=2,
        notification_capacity=2,
        shutdown_timeout_seconds=0.05,
    )


def event(event_id: str) -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        event_type="signal",
        created_at=NOW,
        payload={"plan_id": "plan-1"},
    )


def test_health_registry_requires_healthy_critical_components() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("market-data", critical=True)
    health.register("telegram", critical=False)
    assert health.is_ready() is False
    health.heartbeat("market-data")
    assert health.is_ready() is True
    health.record_failure("telegram", error="notification failed")
    assert health.is_ready() is True
    snapshot = health.snapshot()
    assert snapshot.live is True
    assert snapshot.components[1].status is HealthStatus.UNHEALTHY


def test_stale_critical_heartbeat_fails_readiness() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("paper-execution", critical=True)
    health.heartbeat("paper-execution")
    clock.advance(11)
    snapshot = health.snapshot()
    assert snapshot.ready is False
    assert snapshot.critical_failures[0].status is HealthStatus.CRITICAL


@pytest.mark.asyncio
async def test_watchdog_respects_consecutive_failure_threshold() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("market-data", critical=True)
    health.record_failure("market-data", error="first")
    kill_switch = KillSwitch()
    watchdog = WatchdogSupervisor(
        health=health,
        kill_switch=kill_switch,
        journal=RuntimeEventJournal(run_id="run-1"),
        clock=clock,
    )
    assert await watchdog.check_once() is False
    assert kill_switch.reasons == []
    health.record_failure("market-data", error="second")
    assert await watchdog.check_once() is True
    assert len(kill_switch.reasons) == 1


@pytest.mark.asyncio
async def test_watchdog_trips_once_and_checkpoints_after_critical_failure() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("market-data", critical=True)
    health.record_failure("market-data", error="first")
    health.record_failure("market-data", error="second")
    kill_switch = KillSwitch()
    checkpoint = Checkpoint()
    journal = RuntimeEventJournal(run_id="run-1")
    watchdog = WatchdogSupervisor(
        health=health,
        kill_switch=kill_switch,
        journal=journal,
        clock=clock,
        checkpoint=checkpoint,
    )
    assert await watchdog.check_once() is True
    assert await watchdog.check_once() is False
    assert len(kill_switch.reasons) == 1
    assert kill_switch.reasons[0].startswith("watchdog:market-data=critical")
    assert checkpoint.calls == 1
    assert len(journal.read("watchdog.tripped")) == 1


@pytest.mark.asyncio
async def test_watchdog_retries_failed_activation() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("paper", critical=True)
    health.heartbeat("paper")
    clock.advance(11)
    kill_switch = KillSwitch(fail_once=True)
    journal = RuntimeEventJournal(run_id="run-1")
    watchdog = WatchdogSupervisor(
        health=health,
        kill_switch=kill_switch,
        journal=journal,
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="kill switch unavailable"):
        await watchdog.check_once()
    assert await watchdog.check_once() is True
    assert len(kill_switch.reasons) == 2
    assert len(journal.read("watchdog.activation_failed")) == 1


@pytest.mark.asyncio
async def test_watchdog_retries_checkpoint_without_retripping_kill_switch() -> None:
    clock = Clock()
    health = RuntimeHealthRegistry(policy=policy(), clock=clock)
    health.register("paper", critical=True)
    health.heartbeat("paper")
    clock.advance(11)
    kill_switch = KillSwitch()
    checkpoint = Checkpoint(fail=True)
    journal = RuntimeEventJournal(run_id="run-1")
    watchdog = WatchdogSupervisor(
        health=health,
        kill_switch=kill_switch,
        journal=journal,
        clock=clock,
        checkpoint=checkpoint,
    )
    assert await watchdog.check_once() is True
    checkpoint.fail = False
    assert await watchdog.check_once() is False
    assert len(kill_switch.reasons) == 1
    assert checkpoint.calls == 2
    assert len(journal.read("watchdog.checkpoint_failed")) == 1


@pytest.mark.asyncio
async def test_notification_failures_and_overflow_are_isolated() -> None:
    clock = Clock()
    sink = Sink()
    sink.fail_ids.add("notification-1")
    journal = RuntimeEventJournal(run_id="run-1")
    dispatcher = IsolatedNotificationDispatcher(
        sink=sink,
        capacity=2,
        journal=journal,
        clock=clock,
    )
    assert dispatcher.publish(event("notification-1")) is True
    assert dispatcher.publish(event("notification-2")) is True
    assert dispatcher.publish(event("notification-3")) is False
    stats = await dispatcher.drain()
    assert sink.sent == ["notification-2"]
    assert stats.sent == 1
    assert stats.failed == 1
    assert stats.dropped == 1
    assert stats.queued == 0
    assert len(journal.read("notification.failed")) == 1
    assert len(journal.read("notification.dropped")) == 1


@pytest.mark.asyncio
async def test_shutdown_supervisor_completes_gracefully_in_order() -> None:
    clock = Clock()
    calls: list[str] = []
    journal = RuntimeEventJournal(run_id="run-1")
    supervisor = ShutdownSupervisor(
        timeout_seconds=0.1,
        force_stop=lambda: calls.append("force"),
        journal=journal,
        clock=clock,
    )
    report = await supervisor.shutdown(
        (
            ShutdownOperation("market-stop", lambda: calls.append("market")),
            ShutdownOperation("checkpoint", lambda: calls.append("checkpoint")),
        )
    )
    assert report.graceful is True
    assert report.forced is False
    assert calls == ["market", "checkpoint"]
    assert report.completed == ("market-stop", "checkpoint")


@pytest.mark.asyncio
async def test_shutdown_timeout_invokes_forced_stop() -> None:
    clock = Clock()
    calls: list[str] = []
    journal = RuntimeEventJournal(run_id="run-1")

    async def blocked() -> None:
        await asyncio.sleep(0.05)

    supervisor = ShutdownSupervisor(
        timeout_seconds=0.001,
        force_stop=lambda: calls.append("force"),
        journal=journal,
        clock=clock,
    )
    report = await supervisor.shutdown((ShutdownOperation("blocked", blocked),))
    assert report.graceful is False
    assert report.forced is True
    assert calls == ["force"]
    assert "shutdown_timeout" in report.errors
    assert len(journal.read("runtime.shutdown_completed")) == 1
