from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ComponentHealth, ComponentId, ContractModel, HealthStatus
from .service import Clock, utc_now


class PaperRuntimeNativeState(StrEnum):
    CREATED = "created"
    CONFIGURING = "configuring"
    CONFIGURED = "configured"
    INITIALIZING = "initializing"
    RECOVERING = "recovering"
    RECONCILING = "reconciling"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    DISPOSED = "disposed"


class PaperExecutionNativeLifecycle(StrEnum):
    CREATED = "created"
    RECOVERING = "recovering"
    RECONCILING = "reconciling"
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    FAILED = "failed"
    STOPPED = "stopped"


class PaperKillSwitchScope(StrEnum):
    GLOBAL = "global"
    PORTFOLIO = "portfolio"


class PaperKillSwitch(ContractModel):
    active: bool
    scope: PaperKillSwitchScope
    activated_at: datetime | None = None
    cancellation_pending: bool

    @field_validator("activated_at")
    @classmethod
    def validate_activated_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> PaperKillSwitch:
        if self.active and self.activated_at is None:
            raise ValueError("active kill switch requires activated_at")
        if not self.active and (self.activated_at is not None or self.cancellation_pending):
            raise ValueError("inactive kill switch cannot retain activation state")
        return self


class PaperRuntimeSummary(ContractModel):
    runtime_id: str = Field(min_length=1, max_length=256)
    portfolio_id: str = Field(min_length=1, max_length=256)
    strategy_id: str = Field(min_length=1, max_length=256)
    execution_mode: Literal["paper"] = "paper"
    native_state: PaperRuntimeNativeState
    base_timeframe: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=128)
    instruments: tuple[str, ...] = Field(min_length=1, max_length=512)
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kill_switch: PaperKillSwitch
    health_ready: bool
    queue_depth: int | None = Field(default=None, ge=0)
    queue_capacity: int | None = Field(default=None, ge=1)
    queue_high_water_mark: int | None = Field(default=None, ge=0)
    paper_execution_lifecycle: PaperExecutionNativeLifecycle
    order_count: int = Field(ge=0)
    active_order_count: int = Field(ge=0)
    blocked_scope_count: int = Field(ge=0)
    position_projection_available: Literal[False] = False
    lifecycle_mutations_available: Literal[False] = False

    @field_validator("instruments")
    @classmethod
    def validate_instruments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("instrument identities must be bounded non-blank strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("instrument identities must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> PaperRuntimeSummary:
        if self.active_order_count > self.order_count:
            raise ValueError("active_order_count cannot exceed order_count")
        values = (self.queue_depth, self.queue_capacity, self.queue_high_water_mark)
        if any(item is None for item in values) and any(item is not None for item in values):
            raise ValueError("queue metrics must be all present or all absent")
        if (
            self.queue_depth is not None
            and self.queue_capacity is not None
            and self.queue_depth > self.queue_capacity
        ):
            raise ValueError("queue_depth cannot exceed queue_capacity")
        if (
            self.queue_high_water_mark is not None
            and self.queue_capacity is not None
            and self.queue_high_water_mark > self.queue_capacity
        ):
            raise ValueError("queue_high_water_mark cannot exceed queue_capacity")
        return self


class PaperOrderSummary(ContractModel):
    client_order_id: str = Field(min_length=1, max_length=256)
    core_order_id: str = Field(min_length=1, max_length=256)
    portfolio_id: str = Field(min_length=1, max_length=256)
    instrument: str = Field(min_length=1, max_length=128)
    intent_role: str = Field(min_length=1, max_length=128)
    native_state: str = Field(min_length=1, max_length=64)
    requested_quantity: str = Field(min_length=1, max_length=128)
    filled_quantity: str = Field(min_length=1, max_length=128)
    remaining_quantity: str = Field(min_length=1, max_length=128)
    average_fill_price: str | None = Field(default=None, max_length=128)
    fees_paid: str = Field(min_length=1, max_length=128)
    last_sequence: int = Field(ge=0)
    last_event_timestamp: datetime | None = None

    @field_validator(
        "requested_quantity",
        "filled_quantity",
        "remaining_quantity",
        "average_fill_price",
        "fees_paid",
    )
    @classmethod
    def validate_decimal_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("order numeric fields must be decimal strings") from exc
        if not parsed.is_finite():
            raise ValueError("order numeric fields must be finite decimal strings")
        return value

    @field_validator("last_event_timestamp")
    @classmethod
    def validate_last_event_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_event_timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_accounting(self) -> PaperOrderSummary:
        requested = Decimal(self.requested_quantity)
        filled = Decimal(self.filled_quantity)
        remaining = Decimal(self.remaining_quantity)
        fees = Decimal(self.fees_paid)
        if requested <= 0:
            raise ValueError("requested_quantity must be positive")
        if filled < 0 or remaining < 0 or fees < 0:
            raise ValueError("filled, remaining and fees must be non-negative")
        if filled + remaining != requested:
            raise ValueError("filled_quantity + remaining_quantity must equal requested_quantity")
        if filled == 0 and self.average_fill_price is not None:
            raise ValueError("unfilled order cannot have average_fill_price")
        if filled > 0 and (
            self.average_fill_price is None or Decimal(self.average_fill_price) <= 0
        ):
            raise ValueError("filled order requires positive average_fill_price")
        return self


class PaperOrderList(ContractModel):
    orders: tuple[PaperOrderSummary, ...]
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    has_more: bool


class PaperBlockedScope(ContractModel):
    portfolio_id: str = Field(min_length=1, max_length=256)
    instrument: str = Field(min_length=1, max_length=128)


class PaperBlockedScopeList(ContractModel):
    scopes: tuple[PaperBlockedScope, ...]


class RuntimeSnapshotLike(Protocol):
    @property
    def runtime_id(self) -> str: ...

    @property
    def portfolio_id(self) -> str: ...

    @property
    def strategy_id(self) -> str: ...

    @property
    def native_state(self) -> str: ...

    @property
    def execution_mode(self) -> str: ...

    @property
    def base_timeframe(self) -> str: ...

    @property
    def market(self) -> str: ...

    @property
    def instruments(self) -> tuple[str, ...]: ...

    @property
    def configuration_hash(self) -> str: ...

    @property
    def kill_switch_active(self) -> bool: ...

    @property
    def kill_switch_scope(self) -> str: ...

    @property
    def kill_switch_activated_at(self) -> datetime | None: ...

    @property
    def kill_switch_cancellation_pending(self) -> bool: ...

    @property
    def health_ready(self) -> bool: ...

    @property
    def queue_depth(self) -> int | None: ...

    @property
    def queue_capacity(self) -> int | None: ...

    @property
    def queue_high_water_mark(self) -> int | None: ...


class ExecutionSnapshotLike(Protocol):
    @property
    def native_lifecycle(self) -> str: ...

    @property
    def order_count(self) -> int: ...

    @property
    def active_order_count(self) -> int: ...

    @property
    def blocked_scope_count(self) -> int: ...


class OrderSnapshotLike(Protocol):
    @property
    def client_order_id(self) -> str: ...

    @property
    def core_order_id(self) -> str: ...

    @property
    def portfolio_id(self) -> str: ...

    @property
    def instrument(self) -> str: ...

    @property
    def intent_role(self) -> str: ...

    @property
    def native_state(self) -> str: ...

    @property
    def requested_quantity(self) -> Decimal: ...

    @property
    def filled_quantity(self) -> Decimal: ...

    @property
    def remaining_quantity(self) -> Decimal: ...

    @property
    def average_fill_price(self) -> Decimal | None: ...

    @property
    def fees_paid(self) -> Decimal: ...

    @property
    def last_sequence(self) -> int: ...

    @property
    def last_event_timestamp(self) -> datetime | None: ...


class BlockedScopeLike(Protocol):
    @property
    def portfolio_id(self) -> str: ...

    @property
    def instrument(self) -> str: ...


class TradingRuntimeReader(Protocol):
    def snapshot(self) -> RuntimeSnapshotLike: ...


class PaperExecutionReader(Protocol):
    def snapshot(self) -> ExecutionSnapshotLike: ...

    def list_orders(self, *, limit: int, offset: int) -> tuple[OrderSnapshotLike, ...]: ...

    def list_blocked_scopes(self) -> tuple[BlockedScopeLike, ...]: ...


class PaperRuntimeControlService:
    component = ComponentId.PAPER_RUNTIME

    def __init__(
        self,
        runtime_reader: TradingRuntimeReader,
        execution_reader: PaperExecutionReader,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not callable(getattr(runtime_reader, "snapshot", None)):
            raise TypeError("runtime_reader must implement snapshot()")
        if not callable(getattr(execution_reader, "snapshot", None)):
            raise TypeError("execution_reader must implement snapshot()")
        if not callable(getattr(execution_reader, "list_orders", None)):
            raise TypeError("execution_reader must implement list_orders()")
        if not callable(getattr(execution_reader, "list_blocked_scopes", None)):
            raise TypeError("execution_reader must implement list_blocked_scopes()")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._runtime_reader = runtime_reader
        self._execution_reader = execution_reader
        self._clock = clock

    def health(self) -> ComponentHealth:
        summary = self.summary()
        degraded = (
            summary.native_state is not PaperRuntimeNativeState.RUNNING
            or not summary.health_ready
            or summary.kill_switch.active
            or summary.paper_execution_lifecycle is not PaperExecutionNativeLifecycle.READY
            or summary.blocked_scope_count > 0
        )
        return ComponentHealth(
            component=self.component,
            status=HealthStatus.DEGRADED if degraded else HealthStatus.HEALTHY,
            checked_at=self._now(),
            summary=(
                "Paper Runtime is observable but requires operator attention"
                if degraded
                else "Paper Runtime is running and reconciled"
            ),
            details={
                "runtime_state": summary.native_state.value,
                "paper_execution_state": summary.paper_execution_lifecycle.value,
                "kill_switch_active": summary.kill_switch.active,
                "kill_switch_cancellation_pending": summary.kill_switch.cancellation_pending,
                "blocked_scope_count": summary.blocked_scope_count,
                "active_order_count": summary.active_order_count,
                "execution_mode": "paper",
            },
        )

    def summary(self) -> PaperRuntimeSummary:
        runtime = self._runtime_reader.snapshot()
        execution = self._execution_reader.snapshot()
        if runtime.execution_mode != "paper":
            raise ValueError("Paper Runtime reader reported non-paper execution mode")
        return PaperRuntimeSummary(
            runtime_id=runtime.runtime_id,
            portfolio_id=runtime.portfolio_id,
            strategy_id=runtime.strategy_id,
            native_state=PaperRuntimeNativeState(runtime.native_state),
            base_timeframe=runtime.base_timeframe,
            market=runtime.market,
            instruments=runtime.instruments,
            configuration_hash=runtime.configuration_hash,
            kill_switch=PaperKillSwitch(
                active=runtime.kill_switch_active,
                scope=PaperKillSwitchScope(runtime.kill_switch_scope),
                activated_at=runtime.kill_switch_activated_at,
                cancellation_pending=runtime.kill_switch_cancellation_pending,
            ),
            health_ready=runtime.health_ready,
            queue_depth=runtime.queue_depth,
            queue_capacity=runtime.queue_capacity,
            queue_high_water_mark=runtime.queue_high_water_mark,
            paper_execution_lifecycle=PaperExecutionNativeLifecycle(execution.native_lifecycle),
            order_count=execution.order_count,
            active_order_count=execution.active_order_count,
            blocked_scope_count=execution.blocked_scope_count,
        )

    def orders(self, *, limit: int = 100, offset: int = 0) -> PaperOrderList:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        rows = self._execution_reader.list_orders(limit=limit, offset=offset)
        has_more = False
        if len(rows) == limit:
            has_more = bool(self._execution_reader.list_orders(limit=1, offset=offset + limit))
        return PaperOrderList(
            orders=tuple(self._project_order(row) for row in rows),
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    def blocked_scopes(self) -> PaperBlockedScopeList:
        return PaperBlockedScopeList(
            scopes=tuple(
                PaperBlockedScope(portfolio_id=row.portfolio_id, instrument=row.instrument)
                for row in self._execution_reader.list_blocked_scopes()
            )
        )

    @staticmethod
    def _project_order(row: OrderSnapshotLike) -> PaperOrderSummary:
        return PaperOrderSummary(
            client_order_id=row.client_order_id,
            core_order_id=row.core_order_id,
            portfolio_id=row.portfolio_id,
            instrument=row.instrument,
            intent_role=row.intent_role,
            native_state=row.native_state,
            requested_quantity=str(row.requested_quantity),
            filled_quantity=str(row.filled_quantity),
            remaining_quantity=str(row.remaining_quantity),
            average_fill_price=(
                str(row.average_fill_price) if row.average_fill_price is not None else None
            ),
            fees_paid=str(row.fees_paid),
            last_sequence=row.last_sequence,
            last_event_timestamp=row.last_event_timestamp,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(UTC)
