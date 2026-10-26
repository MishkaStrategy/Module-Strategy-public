from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import SimpleNamespace

import pytest

from modular_trading_runner import (
    KillSwitchActiveError,
    KillSwitchResetError,
    PlanIdentityConflict,
    ReadinessError,
    RuntimeState,
    TradingRunner,
    TradingRunnerConfig,
)


class Action(StrEnum):
    OPEN = "open_position"
    CLOSE = "close_position"


@dataclass
class Envelope:
    plan_id: str
    content_hash: str
    action: Action
    schema_version: str = "1"
    valid_from: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at: datetime = datetime(2027, 1, 1, tzinfo=UTC)

    def validate_at(self, timestamp: datetime) -> None:
        if timestamp < self.valid_from or timestamp >= self.expires_at:
            raise ValueError("expired")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class Journal:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.records: list[dict[str, object]] = []

    def append(self, **kwargs: object) -> None:
        self.events.append(str(kwargs["event_type"]))
        self.records.append(dict(kwargs))


class Store:
    def __init__(self, initial: dict[str, object] | None = None) -> None:
        self.data: dict[str, object] = initial or {}

    def load(self, key: str) -> dict[str, object] | None:
        value = self.data.get(key)
        return value if isinstance(value, dict) else None

    def save(self, key: str, value: object) -> None:
        assert isinstance(value, dict)
        self.data[key] = value


class FailingStore(Store):
    def __init__(self) -> None:
        super().__init__()
        self.fail_when: object | None = None

    def save(self, key: str, value: object) -> None:
        assert isinstance(value, dict)
        kill_switch = value.get("kill_switch")
        if self.fail_when is not None and kill_switch == self.fail_when:
            raise OSError("injected state persistence failure")
        super().save(key, value)


class PostReplaceFailingStore(Store):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after: object | None = None

    def save(self, key: str, value: object) -> None:
        assert isinstance(value, dict)
        super().save(key, value)
        if value.get("kill_switch") == self.fail_after:
            self.fail_after = None
            raise OSError("injected post-replace directory fsync failure")


class Module:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def on_start(self) -> None:
        self.calls.append(f"start:{self.name}")

    def on_finish(self) -> None:
        self.calls.append(f"finish:{self.name}")


class Planner:
    def __init__(self, modules: tuple[Module, ...]) -> None:
        self.modules = modules

    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        assert tuple(modules) == self.modules
        assert base_timeframe == "5m"
        return SimpleNamespace(modules=self.modules)


class Toggle:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def recover(self) -> bool:
        self.calls += 1
        return self.result

    def reconcile(self) -> bool:
        self.calls += 1
        return self.result


class Health:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready


class MarketData:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> None:
        self.calls.append("market:start")

    def stop(self) -> None:
        self.calls.append("market:stop")


class Executor:
    def __init__(self) -> None:
        self.calls = 0
        self.cancel_fails = False
        self.cancel_returns_false = False
        self.cancel_calls = 0

    def accept(self, envelope: object, **kwargs: object) -> object:
        self.calls += 1
        return {"accepted": True, "plan_id": envelope.plan_id}

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        self.cancel_calls += 1
        if self.cancel_fails:
            raise RuntimeError("cancel failed")
        return not self.cancel_returns_false


class Checkpoint:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def capture(self) -> None:
        self.calls.append("checkpoint")


def config() -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "runner-1",
            "portfolio_id": "portfolio-1",
            "strategy_id": "strategy-1",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("binance-usdtm",),
            "modules": (),
        }
    )


def build_runner(
    *, ready: bool = True, store: Store | None = None
) -> tuple[TradingRunner, list[str], Executor]:
    calls: list[str] = []
    modules = (Module("first", calls), Module("second", calls))
    executor = Executor()
    runner = TradingRunner(
        config=config(),
        modules=modules,
        planner=Planner(modules),
        clock=Clock(),
        journal=Journal(),
        state_store=store or Store(),
        recovery=Toggle(),
        reconciliation=Toggle(),
        health=Health(ready),
        market_data=MarketData(calls),
        plan_execution=executor,
        checkpoint=Checkpoint(calls),
    )
    return runner, calls, executor


@pytest.mark.asyncio
async def test_bootstrap_orders_modules_recovery_reconciliation_and_market_start() -> None:
    runner, calls, _ = build_runner()
    await runner.start()
    assert runner.state is RuntimeState.RUNNING
    assert calls == ["start:first", "start:second", "market:start"]


@pytest.mark.asyncio
async def test_readiness_failure_rolls_back_started_components() -> None:
    runner, calls, _ = build_runner(ready=False)
    with pytest.raises(ReadinessError):
        await runner.start()
    assert runner.state is RuntimeState.FAILED
    assert calls == [
        "start:first",
        "start:second",
        "market:start",
        "market:stop",
        "finish:second",
        "finish:first",
    ]


@pytest.mark.asyncio
async def test_duplicate_plan_returns_original_result_without_second_execution() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    envelope = Envelope("plan-1", "a" * 64, Action.OPEN)
    first = await runner.dispatch_plan(
        envelope,
        current_index=1,
        mark_price=Decimal("100"),
        paper_versions=object(),
    )
    second = await runner.dispatch_plan(
        envelope,
        current_index=2,
        mark_price=Decimal("101"),
        paper_versions=object(),
    )
    assert second is first
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_same_plan_id_with_different_content_fails_closed() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    await runner.dispatch_plan(
        Envelope("plan-1", "a" * 64, Action.OPEN),
        current_index=1,
        mark_price=Decimal("100"),
        paper_versions=object(),
    )
    with pytest.raises(PlanIdentityConflict):
        await runner.dispatch_plan(
            Envelope("plan-1", "b" * 64, Action.OPEN),
            current_index=2,
            mark_price=Decimal("101"),
            paper_versions=object(),
        )
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_kill_switch_blocks_entries_but_allows_close() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    await runner.activate_kill_switch(reason="manual")
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    with pytest.raises(KillSwitchActiveError):
        await runner.dispatch_plan(
            Envelope("entry", "a" * 64, Action.OPEN),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    result = await runner.dispatch_plan(
        Envelope("close", "b" * 64, Action.CLOSE),
        current_index=2,
        mark_price=Decimal("99"),
        paper_versions=object(),
    )
    assert result["accepted"] is True
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_failed_entry_cancellation_persists_and_blocks_reset() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    executor.cancel_fails = True
    await runner.activate_kill_switch(reason="watchdog")
    assert runner.kill_switch.cancellation_pending is True
    with pytest.raises(KillSwitchResetError):
        await runner.reset_kill_switch()
    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    retries = [
        record
        for record in runner.journal.records
        if record["event_type"] == "kill_switch.cancellation_retry"
    ]
    assert len(retries) == 1
    assert retries[0]["payload"] == {"succeeded": False, "reason": "watchdog"}


@pytest.mark.asyncio
async def test_kill_switch_is_persisted_active_and_pending_before_cancel_callback() -> None:
    store = Store()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    observed: list[object] = []

    async def inspect_before_cancel(**_: object) -> bool:
        persisted = store.data["runner-1"]
        assert isinstance(persisted, dict)
        observed.append(persisted["kill_switch"])
        observed.append((runner.kill_switch.active, runner.kill_switch.cancellation_pending))
        return True

    executor.cancel_pending_entries = inspect_before_cancel  # type: ignore[method-assign]
    await runner.activate_kill_switch(reason="manual")

    assert observed == [
        {
            "active": True,
            "reason": "manual",
            "scope": "global",
            "activated_at": "2026-07-26T00:00:00+00:00",
            "cancellation_pending": True,
        },
        (True, True),
    ]
    assert runner.kill_switch.cancellation_pending is False


@pytest.mark.asyncio
async def test_activation_waits_for_inflight_risk_dispatch_before_persisting_fence() -> None:
    store = Store()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    cancel_started = asyncio.Event()

    async def blocked_accept(envelope: object, **_: object) -> object:
        dispatch_started.set()
        await release_dispatch.wait()
        return {"accepted": True, "plan_id": envelope.plan_id}

    async def observe_cancel(**_: object) -> bool:
        cancel_started.set()
        persisted = store.data["runner-1"]
        assert isinstance(persisted, dict)
        assert persisted["kill_switch"]["active"] is True
        assert persisted["kill_switch"]["cancellation_pending"] is True
        return True

    executor.accept = blocked_accept  # type: ignore[method-assign]
    executor.cancel_pending_entries = observe_cancel  # type: ignore[method-assign]
    dispatch = asyncio.create_task(
        runner.dispatch_plan(
            Envelope("in-flight", "a" * 64, Action.OPEN),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    )
    await dispatch_started.wait()
    activation = asyncio.create_task(runner.activate_kill_switch(reason="manual"))
    await asyncio.sleep(0)
    assert not cancel_started.is_set()
    assert not runner.kill_switch.active

    release_dispatch.set()
    await dispatch
    await activation
    assert cancel_started.is_set()
    assert runner.kill_switch.active


@pytest.mark.asyncio
async def test_reset_persistence_waits_for_inflight_close_dispatch() -> None:
    store = Store()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    await runner.activate_kill_switch(reason="manual")
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def blocked_close(envelope: object, **_: object) -> object:
        dispatch_started.set()
        await release_dispatch.wait()
        return {"accepted": True, "plan_id": envelope.plan_id}

    executor.accept = blocked_close  # type: ignore[method-assign]
    close = asyncio.create_task(
        runner.dispatch_plan(
            Envelope("close-before-reset", "a" * 64, Action.CLOSE),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    )
    await dispatch_started.wait()
    reset = asyncio.create_task(runner.reset_kill_switch())
    await asyncio.sleep(0)
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    assert runner.kill_switch.active

    release_dispatch.set()
    await close
    await reset
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is False
    assert runner.kill_switch.active is False


@pytest.mark.asyncio
async def test_initial_fence_persistence_failure_keeps_active_pending_and_skips_cancel() -> None:
    store = FailingStore()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    store.fail_when = {
        "active": True,
        "reason": "manual",
        "scope": "global",
        "activated_at": "2026-07-26T00:00:00+00:00",
        "cancellation_pending": True,
    }

    with pytest.raises(OSError, match="injected state persistence failure"):
        await runner.activate_kill_switch(reason="manual")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    assert executor.cancel_calls == 0
    with pytest.raises(KillSwitchActiveError):
        await runner.dispatch_plan(
            Envelope("blocked-entry", "a" * 64, Action.OPEN),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )


@pytest.mark.asyncio
async def test_initial_fence_post_replace_failure_compensates_pending_state() -> None:
    store = PostReplaceFailingStore()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    store.fail_after = {
        "active": True,
        "reason": "manual",
        "scope": "global",
        "activated_at": "2026-07-26T00:00:00+00:00",
        "cancellation_pending": True,
    }

    with pytest.raises(OSError, match="post-replace"):
        await runner.activate_kill_switch(reason="manual")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    assert executor.cancel_calls == 0
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    assert persisted["kill_switch"]["cancellation_pending"] is True


@pytest.mark.asyncio
async def test_blocked_cancel_rejects_new_risk_and_allows_reentrant_close() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def reentrant_cancel(**_: object) -> bool:
        cancel_started.set()
        with pytest.raises(KillSwitchActiveError):
            await runner.dispatch_plan(
                Envelope("blocked-entry", "a" * 64, Action.OPEN),
                current_index=1,
                mark_price=Decimal("100"),
                paper_versions=object(),
            )
        result = await runner.dispatch_plan(
            Envelope("allowed-close", "b" * 64, Action.CLOSE),
            current_index=2,
            mark_price=Decimal("99"),
            paper_versions=object(),
        )
        assert result["accepted"] is True
        await release_cancel.wait()
        return False

    executor.cancel_pending_entries = reentrant_cancel  # type: ignore[method-assign]
    activation = asyncio.create_task(runner.activate_kill_switch(reason="watchdog"))
    await cancel_started.wait()
    assert runner.kill_switch.active
    assert runner.kill_switch.cancellation_pending
    release_cancel.set()
    await activation
    assert runner.kill_switch.cancellation_pending
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_cancel_result_persistence_failure_keeps_active_pending_memory_and_store() -> None:
    store = FailingStore()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    store.fail_when = {
        "active": True,
        "reason": "manual",
        "scope": "global",
        "activated_at": "2026-07-26T00:00:00+00:00",
        "cancellation_pending": False,
    }

    with pytest.raises(OSError, match="injected state persistence failure"):
        await runner.activate_kill_switch(reason="manual")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    assert persisted["kill_switch"]["cancellation_pending"] is True
    with pytest.raises(KillSwitchActiveError):
        await runner.dispatch_plan(
            Envelope("blocked-entry", "a" * 64, Action.OPEN),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_cancel_result_post_replace_failure_compensates_pending_state() -> None:
    store = PostReplaceFailingStore()
    runner, _, _ = build_runner(store=store)
    await runner.start()
    store.fail_after = {
        "active": True,
        "reason": "manual",
        "scope": "global",
        "activated_at": "2026-07-26T00:00:00+00:00",
        "cancellation_pending": False,
    }

    with pytest.raises(OSError, match="post-replace"):
        await runner.activate_kill_switch(reason="manual")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    assert persisted["kill_switch"]["cancellation_pending"] is True


@pytest.mark.asyncio
async def test_reset_persistence_failure_restores_active_memory_and_blocks_risk() -> None:
    store = FailingStore()
    runner, _, executor = build_runner(store=store)
    await runner.start()
    await runner.activate_kill_switch(reason="manual")
    store.fail_when = {
        "active": False,
        "reason": "",
        "scope": "global",
        "activated_at": None,
        "cancellation_pending": False,
    }

    with pytest.raises(KillSwitchResetError, match="could not be persisted"):
        await runner.reset_kill_switch()

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is False
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    with pytest.raises(KillSwitchActiveError):
        await runner.dispatch_plan(
            Envelope("blocked-entry", "a" * 64, Action.OPEN),
            current_index=1,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_reset_post_replace_failure_compensates_persisted_active_state() -> None:
    store = PostReplaceFailingStore()
    runner, _, _ = build_runner(store=store)
    await runner.start()
    await runner.activate_kill_switch(reason="manual")
    store.fail_after = {
        "active": False,
        "reason": "",
        "scope": "global",
        "activated_at": None,
        "cancellation_pending": False,
    }

    with pytest.raises(KillSwitchResetError, match="could not be persisted"):
        await runner.reset_kill_switch()

    assert runner.kill_switch.active is True
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["kill_switch"]["active"] is True
    assert persisted["kill_switch"]["cancellation_pending"] is False


@pytest.mark.asyncio
async def test_graceful_stop_checkpoints_and_finishes_reverse_order() -> None:
    runner, calls, _ = build_runner()
    await runner.start()
    await runner.stop()
    assert runner.state is RuntimeState.STOPPED
    assert calls[-4:] == ["market:stop", "checkpoint", "finish:second", "finish:first"]


@pytest.mark.asyncio
async def test_persisted_plan_hash_replays_through_paper_boundary_after_restart() -> None:
    current_config = config()
    store = Store(
        {
            "runner-1": {
                "schema_version": "1",
                "configuration_hash": current_config.canonical_hash,
                "processed_plan_hashes": {"plan-1": "a" * 64},
                "kill_switch": {
                    "active": False,
                    "reason": "",
                    "scope": "global",
                    "activated_at": None,
                    "cancellation_pending": False,
                },
                "lifecycle_state": "running",
            }
        }
    )
    runner, _, executor = build_runner(store=store)
    await runner.start()
    result = await runner.dispatch_plan(
        Envelope("plan-1", "a" * 64, Action.OPEN),
        current_index=1,
        mark_price=Decimal("100"),
        paper_versions=object(),
    )
    assert result["accepted"] is True
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_false_cancellation_result_is_pending_and_reset_retries() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    executor.cancel_returns_false = True
    await runner.activate_kill_switch(reason="watchdog")
    assert runner.kill_switch.cancellation_pending is True
    executor.cancel_returns_false = False
    await runner.reset_kill_switch()
    assert runner.kill_switch.active is False
    assert executor.cancel_calls == 2
    retries = [
        record
        for record in runner.journal.records
        if record["event_type"] == "kill_switch.cancellation_retry"
    ]
    assert len(retries) == 1
    assert retries[0]["payload"] == {"succeeded": True, "reason": "watchdog"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [None, 1, "cancelled", object()], ids=["none", "int", "string", "object"]
)
async def test_non_true_cancellation_results_keep_fence_pending_and_block_reset(
    result: object,
) -> None:
    runner, _, executor = build_runner()
    await runner.start()

    def unexpected_result(**_: object) -> object:
        return result

    executor.cancel_pending_entries = unexpected_result  # type: ignore[method-assign]
    await runner.activate_kill_switch(reason="watchdog")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    with pytest.raises(KillSwitchResetError):
        await runner.reset_kill_switch()
    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True


@pytest.mark.asyncio
async def test_missing_cancellation_callback_keeps_fence_pending_and_blocks_reset() -> None:
    runner, _, executor = build_runner()
    await runner.start()
    executor.cancel_pending_entries = None  # type: ignore[method-assign]

    await runner.activate_kill_switch(reason="watchdog")

    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True
    with pytest.raises(KillSwitchResetError):
        await runner.reset_kill_switch()
    assert runner.kill_switch.active is True
    assert runner.kill_switch.cancellation_pending is True


@pytest.mark.asyncio
async def test_active_kill_switch_reapplies_entry_cancellation_after_restart() -> None:
    current_config = config()
    store = Store(
        {
            "runner-1": {
                "schema_version": "1",
                "configuration_hash": current_config.canonical_hash,
                "processed_plan_hashes": {},
                "kill_switch": {
                    "active": True,
                    "reason": "restart-safety",
                    "scope": "global",
                    "activated_at": "2026-07-26T00:00:00+00:00",
                    "cancellation_pending": True,
                },
                "lifecycle_state": "kill_switch_active",
            }
        }
    )
    runner, _, executor = build_runner(store=store)
    await runner.start()
    assert runner.state is RuntimeState.KILL_SWITCH_ACTIVE
    assert executor.cancel_calls == 1
    assert runner.kill_switch.cancellation_pending is False


def test_persisted_configuration_mismatch_fails_closed() -> None:
    store = Store(
        {
            "runner-1": {
                "schema_version": "1",
                "configuration_hash": "f" * 64,
                "processed_plan_hashes": {},
                "kill_switch": {
                    "active": False,
                    "reason": "",
                    "scope": "global",
                    "activated_at": None,
                    "cancellation_pending": False,
                },
                "lifecycle_state": "running",
            }
        }
    )
    with pytest.raises(Exception, match="configuration hash does not match"):
        build_runner(store=store)


@pytest.mark.asyncio
async def test_successful_stop_persists_terminal_state() -> None:
    store = Store()
    runner, _, _ = build_runner(store=store)
    await runner.start()
    await runner.stop()
    persisted = store.data["runner-1"]
    assert isinstance(persisted, dict)
    assert persisted["lifecycle_state"] == "stopped"


@pytest.mark.asyncio
async def test_dependency_plan_must_be_exact_permutation() -> None:
    runner, _, _ = build_runner()
    foreign_calls: list[str] = []
    foreign_modules = (Module("foreign", foreign_calls), Module("other", foreign_calls))

    class MaliciousPlanner:
        def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
            return SimpleNamespace(modules=foreign_modules)

    runner.planner = MaliciousPlanner()
    with pytest.raises(Exception, match="every configured module exactly once"):
        await runner.start()
    assert runner.state is RuntimeState.FAILED
