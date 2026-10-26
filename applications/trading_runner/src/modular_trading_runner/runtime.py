from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .config import TradingRunnerConfig
from .enums import KillSwitchScope, PlanRiskClass, RuntimeState
from .errors import (
    BootstrapError,
    KillSwitchActiveError,
    KillSwitchResetError,
    PlanDispatchError,
    PlanIdentityConflict,
    ReadinessError,
    ReconciliationError,
    RecoveryError,
)
from .lifecycle import LifecycleMachine
from .ports import (
    AuditJournal,
    CheckpointCoordinator,
    DependencyPlanner,
    HealthManager,
    MarketDataRuntime,
    PlanExecutionBoundary,
    ReconciliationCoordinator,
    RecoveryCoordinator,
    RuntimeClock,
    RuntimeStateStore,
)

_RISK_INCREASING_ACTIONS = frozenset({"open_position", "add_to_position", "reverse_position"})
_RISK_REDUCING_ACTIONS = frozenset(
    {"reduce_position", "close_position", "cancel_pending_plan", "update_protection"}
)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PlanDispatchError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanDispatchError(f"{name} must be a non-blank string")
    return value


def _digest(value: object, name: str) -> str:
    text = _identifier(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PlanDispatchError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _action_text(envelope: object) -> str:
    action = getattr(envelope, "action", None)
    value = getattr(action, "value", action)
    return _identifier(value, "envelope.action")


def classify_plan(envelope: object) -> PlanRiskClass:
    action = _action_text(envelope)
    if action in _RISK_INCREASING_ACTIONS:
        return PlanRiskClass.RISK_INCREASING
    if action in _RISK_REDUCING_ACTIONS:
        return PlanRiskClass.RISK_REDUCING
    return PlanRiskClass.NEUTRAL


@dataclass(frozen=True, slots=True)
class KillSwitchRecord:
    active: bool
    reason: str
    scope: KillSwitchScope
    activated_at: datetime | None
    cancellation_pending: bool

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "active": self.active,
            "reason": self.reason,
            "scope": self.scope.value,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "cancellation_pending": self.cancellation_pending,
        }


class TradingRunner:
    """Paper-only lifecycle owner. Trading semantics remain in upstream modules."""

    def __init__(
        self,
        *,
        config: TradingRunnerConfig,
        modules: Sequence[Any],
        planner: DependencyPlanner,
        clock: RuntimeClock,
        journal: AuditJournal,
        state_store: RuntimeStateStore,
        recovery: RecoveryCoordinator,
        reconciliation: ReconciliationCoordinator,
        health: HealthManager,
        market_data: MarketDataRuntime,
        plan_execution: PlanExecutionBoundary,
        checkpoint: CheckpointCoordinator,
    ) -> None:
        if config.execution_mode.value != "paper":
            raise ValueError("TradingRunner accepts paper execution only")
        self.config = config
        self.modules = tuple(modules)
        self.planner = planner
        self.clock = clock
        self.journal = journal
        self.state_store = state_store
        self.recovery = recovery
        self.reconciliation = reconciliation
        self.health = health
        self.market_data = market_data
        self.plan_execution = plan_execution
        self.checkpoint = checkpoint
        self.lifecycle = LifecycleMachine()
        self._lifecycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._ordered_modules: tuple[Any, ...] = ()
        self._started_modules: list[Any] = []
        self._plan_hashes: dict[str, str] = {}
        self._plan_results: dict[str, object] = {}
        self._kill_switch = KillSwitchRecord(
            active=False,
            reason="",
            scope=KillSwitchScope.GLOBAL,
            activated_at=None,
            cancellation_pending=False,
        )
        self._restore_runtime_state()

    @property
    def state(self) -> RuntimeState:
        return self.lifecycle.state

    @property
    def kill_switch(self) -> KillSwitchRecord:
        return self._kill_switch

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.state is RuntimeState.RUNNING:
                return
            if self.state is not RuntimeState.CREATED:
                raise BootstrapError(f"cannot start from {self.state.value}")
            try:
                self.lifecycle.transition(RuntimeState.CONFIGURING)
                self._audit("runtime.configuring", {"config_hash": self.config.canonical_hash})
                self.lifecycle.transition(RuntimeState.CONFIGURED)
                self.lifecycle.transition(RuntimeState.INITIALIZING)
                plan = self.planner.validate_and_plan(
                    self.modules,
                    base_timeframe=self.config.base_timeframe,
                )
                ordered = tuple(getattr(plan, "modules", ()))
                configured_ids = tuple(id(module) for module in self.modules)
                ordered_ids = tuple(id(module) for module in ordered)
                if (
                    len(ordered_ids) != len(configured_ids)
                    or len(set(ordered_ids)) != len(ordered_ids)
                    or set(ordered_ids) != set(configured_ids)
                ):
                    raise BootstrapError(
                        "dependency plan must contain every configured module exactly once"
                    )
                self._ordered_modules = ordered
                for module in self._ordered_modules:
                    await _maybe_await(module.on_start())
                    self._started_modules.append(module)
                self.lifecycle.transition(RuntimeState.RECOVERING)
                recovery_result = await _maybe_await(self.recovery.recover())
                if recovery_result is False:
                    raise RecoveryError("runtime recovery did not complete")
                self.lifecycle.transition(RuntimeState.RECONCILING)
                reconciliation_result = await _maybe_await(self.reconciliation.reconcile())
                if reconciliation_result is False:
                    raise ReconciliationError("initial reconciliation did not complete")
                self.lifecycle.transition(RuntimeState.STARTING)
                if self._kill_switch.active:
                    await self._retry_entry_cancellation(
                        reason=self._kill_switch.reason,
                        timestamp=_utc(self.clock.now(), "clock.now"),
                    )
                await _maybe_await(self.market_data.start())
                if not self.health.is_ready():
                    raise ReadinessError("runtime health is not ready after reconciliation")
                if self._kill_switch.active:
                    self.lifecycle.transition(RuntimeState.DEGRADED)
                    self.lifecycle.transition(RuntimeState.KILL_SWITCH_ACTIVE)
                else:
                    self.lifecycle.transition(RuntimeState.RUNNING)
                self._persist_runtime_state()
                self._audit("runtime.started", {"state": self.state.value})
            except Exception as exc:
                await self._rollback_start()
                self.lifecycle.force_failed()
                try:
                    self._persist_runtime_state()
                except Exception:
                    pass
                self._audit("runtime.failed", {"error_type": type(exc).__name__, "error": str(exc)})
                raise

    async def dispatch_plan(
        self,
        envelope: object,
        *,
        current_index: int,
        mark_price: Decimal,
        paper_versions: object,
    ) -> object:
        async with self._dispatch_lock:
            risk_class = classify_plan(envelope)
            if self.state not in {
                RuntimeState.RUNNING,
                RuntimeState.DEGRADED,
                RuntimeState.KILL_SWITCH_ACTIVE,
            }:
                raise PlanDispatchError(f"plan dispatch forbidden in {self.state.value}")
            if (
                self.state is not RuntimeState.RUNNING
                and risk_class is PlanRiskClass.RISK_INCREASING
            ):
                raise KillSwitchActiveError("risk-increasing plans require RUNNING state")
            if self._kill_switch.active and risk_class is PlanRiskClass.RISK_INCREASING:
                raise KillSwitchActiveError("kill switch blocks risk-increasing plans")
            now = _utc(self.clock.now(), "clock.now")
            schema_version = _identifier(
                getattr(envelope, "schema_version", None), "schema_version"
            )
            if schema_version != "1":
                raise PlanDispatchError("unsupported Router envelope schema")
            plan_id = _identifier(getattr(envelope, "plan_id", None), "plan_id")
            content_hash = _digest(getattr(envelope, "content_hash", None), "content_hash")
            validator = getattr(envelope, "validate_at", None)
            if not callable(validator):
                raise PlanDispatchError("Router envelope must provide validate_at()")
            validator(now)
            existing_hash = self._plan_hashes.get(plan_id)
            if existing_hash is not None:
                if existing_hash != content_hash:
                    raise PlanIdentityConflict("plan_id was already used with different content")
                cached = self._plan_results.get(plan_id)
                if cached is not None:
                    return cached
            result = await _maybe_await(
                self.plan_execution.accept(
                    envelope,
                    now=now,
                    current_index=current_index,
                    mark_price=mark_price,
                    paper_versions=paper_versions,
                )
            )
            self._plan_hashes[plan_id] = content_hash
            self._plan_results[plan_id] = result
            self._persist_runtime_state()
            self._audit(
                "plan.dispatched",
                {"plan_id": plan_id, "content_hash": content_hash, "risk_class": risk_class.value},
                correlation_id=plan_id,
            )
            return result

    async def activate_kill_switch(
        self,
        *,
        reason: str,
        scope: KillSwitchScope = KillSwitchScope.GLOBAL,
    ) -> None:
        async with self._lifecycle_lock:
            reason = _identifier(reason, "kill switch reason")
            if not isinstance(scope, KillSwitchScope):
                raise TypeError("scope must be KillSwitchScope")
            now = _utc(self.clock.now(), "clock.now")
            async with self._dispatch_lock:
                self._kill_switch = KillSwitchRecord(
                    active=True,
                    reason=reason,
                    scope=scope,
                    activated_at=now,
                    cancellation_pending=True,
                )
                if self.state in {RuntimeState.RUNNING, RuntimeState.DEGRADED}:
                    self.lifecycle.transition(RuntimeState.KILL_SWITCH_ACTIVE)
                # Persist the conservative fence before entering the owner callback.
                try:
                    self._persist_runtime_state()
                except Exception:
                    # Atomic replace may have happened before a directory-fsync
                    # error. Retry the conservative state; repeated storage failure
                    # still leaves restart durability uncertain.
                    try:
                        self._persist_runtime_state()
                    except Exception:
                        pass
                    raise

            # Do not hold the dispatch lock across an arbitrary callback: callbacks
            # may re-enter Runner dispatch, which must observe the active fence.
            cancellation_succeeded = await self._cancel_pending_entries(
                reason=reason,
                timestamp=now,
            )
            async with self._dispatch_lock:
                self._kill_switch = KillSwitchRecord(
                    active=True,
                    reason=reason,
                    scope=scope,
                    activated_at=now,
                    cancellation_pending=not cancellation_succeeded,
                )
                try:
                    self._persist_runtime_state()
                except Exception:
                    # The durable state already contains active/pending. Keep that
                    # conservative in-memory state if result persistence fails.
                    self._kill_switch = KillSwitchRecord(
                        active=True,
                        reason=reason,
                        scope=scope,
                        activated_at=now,
                        cancellation_pending=True,
                    )
                    try:
                        # Atomic replace may have happened before a directory-fsync
                        # error. Best-effort compensation restores active/pending.
                        self._persist_runtime_state()
                    except Exception:
                        pass
                    raise
            self._audit(
                "kill_switch.activated",
                {
                    "reason": reason,
                    "scope": scope.value,
                    "cancellation_pending": self._kill_switch.cancellation_pending,
                },
            )

    async def reset_kill_switch(self) -> None:
        async with self._lifecycle_lock:
            if not self._kill_switch.active:
                return
            if self._kill_switch.cancellation_pending:
                timestamp = _utc(self.clock.now(), "clock.now")
                retry_reason = self._kill_switch.reason
                cancellation_succeeded = await self._cancel_pending_entries(
                    reason=retry_reason,
                    timestamp=timestamp,
                )
                async with self._dispatch_lock:
                    self._kill_switch = KillSwitchRecord(
                        active=True,
                        reason=self._kill_switch.reason,
                        scope=self._kill_switch.scope,
                        activated_at=self._kill_switch.activated_at or timestamp,
                        cancellation_pending=not cancellation_succeeded,
                    )
                    try:
                        self._persist_runtime_state()
                    except Exception:
                        self._kill_switch = KillSwitchRecord(
                            active=True,
                            reason=self._kill_switch.reason,
                            scope=self._kill_switch.scope,
                            activated_at=self._kill_switch.activated_at or timestamp,
                            cancellation_pending=True,
                        )
                        try:
                            self._persist_runtime_state()
                        except Exception:
                            pass
                        raise
                self._audit(
                    "kill_switch.cancellation_retry",
                    {"succeeded": cancellation_succeeded, "reason": retry_reason},
                )
                if not cancellation_succeeded:
                    raise KillSwitchResetError(
                        "pending entry cancellation must complete before reset"
                    )
            reconciliation_result = await _maybe_await(self.reconciliation.reconcile())
            if reconciliation_result is False or not self.health.is_ready():
                raise KillSwitchResetError("health and reconciliation must pass before reset")
            async with self._dispatch_lock:
                previous_kill_switch = self._kill_switch
                previous_state = self.state
                self._kill_switch = KillSwitchRecord(
                    active=False,
                    reason="",
                    scope=previous_kill_switch.scope,
                    activated_at=None,
                    cancellation_pending=False,
                )
                if self.state is RuntimeState.KILL_SWITCH_ACTIVE:
                    self.lifecycle.transition(RuntimeState.RUNNING)
                try:
                    self._persist_runtime_state()
                except Exception as exc:
                    self._kill_switch = previous_kill_switch
                    if previous_state is RuntimeState.KILL_SWITCH_ACTIVE:
                        self.lifecycle.transition(RuntimeState.KILL_SWITCH_ACTIVE)
                    compensation_failed = False
                    try:
                        # save() can fail after replacing the file; rewrite the
                        # conservative state if that happened.
                        self._persist_runtime_state()
                    except Exception:
                        compensation_failed = True
                    detail = (
                        "active in memory; durable state is uncertain"
                        if compensation_failed
                        else "active state was restored"
                    )
                    raise KillSwitchResetError(
                        f"kill switch reset could not be persisted; {detail}"
                    ) from exc
            self._audit("kill_switch.reset", {})

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self.state is RuntimeState.STOPPED:
                return
            if self.state is RuntimeState.DISPOSED:
                return
            if self.state is not RuntimeState.STOPPING:
                self.lifecycle.transition(RuntimeState.STOPPING)
            self._audit("runtime.stopping", {})
            errors: list[Exception] = []
            for operation in (self.market_data.stop, self.checkpoint.capture):
                try:
                    await _maybe_await(operation())
                except Exception as exc:
                    errors.append(exc)
            for module in reversed(self._started_modules):
                try:
                    await _maybe_await(module.on_finish())
                except Exception as exc:
                    errors.append(exc)
            if errors:
                self.lifecycle.force_failed()
                self._persist_runtime_state()
                raise BootstrapError(
                    "shutdown failed: "
                    + "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
                )
            self.lifecycle.transition(RuntimeState.STOPPED)
            self._persist_runtime_state()
            self._audit("runtime.stopped", {})

    async def _cancel_pending_entries(
        self,
        *,
        reason: str,
        timestamp: datetime,
    ) -> bool:
        cancel = getattr(self.plan_execution, "cancel_pending_entries", None)
        if not callable(cancel):
            return False
        try:
            result = await _maybe_await(cancel(reason=reason, timestamp=timestamp))
        except Exception:
            return False
        # Cancellation clears the fence only when the boundary explicitly confirms
        # success. Truthy values such as None or an unexpected object fail closed.
        return result is True

    async def _retry_entry_cancellation(
        self,
        *,
        reason: str,
        timestamp: datetime,
    ) -> None:
        succeeded = await self._cancel_pending_entries(
            reason=reason,
            timestamp=timestamp,
        )
        self._kill_switch = KillSwitchRecord(
            active=True,
            reason=reason,
            scope=self._kill_switch.scope,
            activated_at=self._kill_switch.activated_at or timestamp,
            cancellation_pending=not succeeded,
        )
        self._persist_runtime_state()
        self._audit(
            "kill_switch.cancellation_retry",
            {"succeeded": succeeded, "reason": reason},
        )

    async def _rollback_start(self) -> None:
        try:
            await _maybe_await(self.market_data.stop())
        except Exception:
            pass
        for module in reversed(self._started_modules):
            try:
                await _maybe_await(module.on_finish())
            except Exception:
                pass

    def _audit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> None:
        self.journal.append(
            timestamp=_utc(self.clock.now(), "clock.now"),
            event_type=event_type,
            aggregate_id=self.config.runtime_id,
            payload=payload,
            correlation_id=correlation_id,
        )

    def _restore_runtime_state(self) -> None:
        payload = self.state_store.load(self.config.runtime_id)
        if payload is None:
            return
        if not isinstance(payload, Mapping):
            raise BootstrapError("persisted runtime state must be a mapping")
        if payload.get("schema_version") != "1":
            raise BootstrapError("persisted runtime state schema is unsupported")
        persisted_config_hash = _digest(
            payload.get("configuration_hash"),
            "persisted configuration_hash",
        )
        if persisted_config_hash != self.config.canonical_hash:
            raise BootstrapError("persisted runtime configuration hash does not match")
        raw_lifecycle_state = payload.get("lifecycle_state")
        if not isinstance(raw_lifecycle_state, str):
            raise BootstrapError("persisted lifecycle_state must be a string")
        try:
            RuntimeState(raw_lifecycle_state)
        except (TypeError, ValueError) as exc:
            raise BootstrapError("persisted lifecycle_state is unsupported") from exc
        raw_hashes = payload.get("processed_plan_hashes", {})
        if not isinstance(raw_hashes, Mapping):
            raise BootstrapError("persisted processed_plan_hashes must be a mapping")
        self._plan_hashes = {
            _identifier(key, "persisted plan_id"): _digest(value, "persisted content_hash")
            for key, value in raw_hashes.items()
        }
        raw_kill = payload.get("kill_switch")
        if raw_kill is None:
            return
        if not isinstance(raw_kill, Mapping):
            raise BootstrapError("persisted kill_switch must be a mapping")
        active = raw_kill.get("active")
        pending = raw_kill.get("cancellation_pending")
        if not isinstance(active, bool) or not isinstance(pending, bool):
            raise BootstrapError("persisted kill-switch flags must be booleans")
        reason = str(raw_kill.get("reason", ""))
        try:
            scope = KillSwitchScope(raw_kill.get("scope", KillSwitchScope.GLOBAL.value))
        except ValueError as exc:
            raise BootstrapError("persisted kill-switch scope is unsupported") from exc
        activated_raw = raw_kill.get("activated_at")
        activated_at = None
        if activated_raw is not None:
            if not isinstance(activated_raw, str):
                raise BootstrapError("persisted kill-switch timestamp must be a string")
            activated_at = _utc(datetime.fromisoformat(activated_raw), "activated_at")
        self._kill_switch = KillSwitchRecord(
            active=active,
            reason=reason,
            scope=scope,
            activated_at=activated_at,
            cancellation_pending=pending,
        )

    def _persist_runtime_state(self) -> None:
        self.state_store.save(
            self.config.runtime_id,
            {
                "schema_version": "1",
                "configuration_hash": self.config.canonical_hash,
                "processed_plan_hashes": dict(sorted(self._plan_hashes.items())),
                "kill_switch": self._kill_switch.to_mapping(),
                "lifecycle_state": self.state.value,
            },
        )
