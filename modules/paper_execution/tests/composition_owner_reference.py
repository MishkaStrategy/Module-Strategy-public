"""Reference-only, single-writer composition for configured Core paper brokers.

This prototype is deliberately not wired to TradingRunner or the Control Plane.
It owns one privately constructed Core ``BrokerSimulator`` for each member in
the finite, non-empty tuple supplied at construction. Membership remains fixed
for the owner's lifetime; this prototype defines no production maximum or
dynamic membership policy. Only the methods below expose mutations; raw
brokers, portfolios, orders and positions are never returned. A Core mutation
or restore failure fences all position reads until a fresh pristine owner
restores a validated state root for the exact configured membership.

This is not the #933 Stage-1 TradingRunner provisioning/reconnect owner. It also
does not checkpoint Core strategy-module state or journals, and therefore must
not be treated as a complete runtime recovery contract.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeVar
from uuid import uuid4

from modular_core import (
    BrokerStateSnapshot,
    CancelRequest,
    Candle,
    ClosePositionRequest,
    EngineConfig,
    InMemoryEventStore,
    InstrumentSpec,
    Journal,
    OrderRequest,
    OrderSnapshot,
    PositionSide,
    PositionUpdateRequest,
    Trade,
    capture_broker_state,
    restore_broker_state,
)
from modular_core.execution import BrokerSimulator
from modular_core.serialization import to_jsonable
from modular_core.validation import positive_number, require_nonblank, utc_datetime


_STATE_SCHEMA_VERSION = "paper-composition-state-v1"
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class PaperCompositionMemberConfig:
    """Immutable configuration used to construct one private Core broker."""

    strategy_instance_id: str
    engine_config: EngineConfig
    instrument: InstrumentSpec

    def __post_init__(self) -> None:
        require_nonblank(self.strategy_instance_id, "strategy_instance_id")
        if not isinstance(self.engine_config, EngineConfig):
            raise TypeError("engine_config must be EngineConfig")
        if not isinstance(self.instrument, InstrumentSpec):
            raise TypeError("instrument must be InstrumentSpec")


@dataclass(frozen=True, slots=True)
class PaperPositionIdentity:
    """Position identity namespaced by its stable strategy instance."""

    strategy_instance_id: str
    core_position_id: str

    def __post_init__(self) -> None:
        require_nonblank(self.strategy_instance_id, "strategy_instance_id")
        require_nonblank(self.core_position_id, "core_position_id")


@dataclass(frozen=True, slots=True)
class PaperCompositionPosition:
    identity: PaperPositionIdentity
    instrument: str
    side: PositionSide
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: float | None
    take_profit: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, PaperPositionIdentity):
            raise TypeError("identity must be PaperPositionIdentity")
        require_nonblank(self.instrument, "instrument")
        if not isinstance(self.side, PositionSide):
            raise TypeError("side must be PositionSide")
        object.__setattr__(self, "quantity", positive_number(self.quantity, "quantity"))
        object.__setattr__(self, "entry_price", positive_number(self.entry_price, "entry_price"))
        object.__setattr__(self, "entry_time", utc_datetime(self.entry_time, "entry_time"))


@dataclass(frozen=True, slots=True)
class PaperCompositionSnapshot:
    """Immutable full-membership projection from one composition owner."""

    composition_id: str
    owner_generation: str
    owner_instance_id: str
    membership_version: str
    snapshot_version: int
    state_version: int
    as_of: datetime
    coverage: Literal["complete", "unavailable", "recovering"]
    membership: tuple[str, ...]
    positions: tuple[PaperCompositionPosition, ...] | None

    def __post_init__(self) -> None:
        require_nonblank(self.composition_id, "composition_id")
        require_nonblank(self.owner_generation, "owner_generation")
        require_nonblank(self.owner_instance_id, "owner_instance_id")
        require_nonblank(self.membership_version, "membership_version")
        for name in ("snapshot_version", "state_version"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.coverage not in {"complete", "unavailable", "recovering"}:
            raise ValueError("unsupported coverage")
        object.__setattr__(self, "as_of", utc_datetime(self.as_of, "as_of"))
        object.__setattr__(self, "membership", tuple(self.membership))
        if not self.membership:
            raise ValueError("snapshot must bind at least one member")
        for strategy_instance_id in self.membership:
            require_nonblank(strategy_instance_id, "membership strategy_instance_id")
        if self.membership != tuple(sorted(set(self.membership))):
            raise ValueError("snapshot membership must be unique and sorted")
        if self.coverage == "complete":
            if self.positions is None:
                raise ValueError("complete coverage requires a full position tuple")
            positions = tuple(self.positions)
            if any(not isinstance(item, PaperCompositionPosition) for item in positions):
                raise TypeError("positions must contain PaperCompositionPosition")
            position_members = [item.identity.strategy_instance_id for item in positions]
            if len(position_members) != len(set(position_members)):
                raise ValueError("Core membership can expose at most one position per member")
            if any(item not in self.membership for item in position_members):
                raise ValueError("position identity is outside snapshot membership")
            object.__setattr__(self, "positions", positions)
        elif self.positions is not None:
            raise ValueError("incomplete coverage cannot expose positions")


@dataclass(frozen=True, slots=True)
class PaperCompositionMemberState:
    strategy_instance_id: str
    state_digest: str
    broker_state: BrokerStateSnapshot

    def __post_init__(self) -> None:
        require_nonblank(self.strategy_instance_id, "strategy_instance_id")
        if len(self.state_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.state_digest
        ):
            raise ValueError("state_digest must be a lowercase SHA-256 digest")
        if not isinstance(self.broker_state, BrokerStateSnapshot):
            raise TypeError("broker_state must be BrokerStateSnapshot")


@dataclass(frozen=True, slots=True)
class PaperCompositionStateRoot:
    """Digest-bound state for every configured composition member."""

    schema_version: str
    composition_id: str
    source_owner_generation: str
    membership_version: str
    state_version: int
    members: tuple[PaperCompositionMemberState, ...]
    root_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported composition state schema")
        require_nonblank(self.composition_id, "composition_id")
        require_nonblank(self.source_owner_generation, "source_owner_generation")
        require_nonblank(self.membership_version, "membership_version")
        if (
            not isinstance(self.state_version, int)
            or isinstance(self.state_version, bool)
            or self.state_version < 0
        ):
            raise ValueError("state_version must be a non-negative integer")
        object.__setattr__(self, "members", tuple(self.members))
        if not self.members:
            raise ValueError("state root must contain at least one member state")
        if any(not isinstance(member, PaperCompositionMemberState) for member in self.members):
            raise TypeError("state root members must be PaperCompositionMemberState values")
        member_ids = tuple(member.strategy_instance_id for member in self.members)
        if member_ids != tuple(sorted(set(member_ids))):
            raise ValueError("state root members must be unique and sorted")
        expected_root = _root_digest(
            self.schema_version,
            self.composition_id,
            self.source_owner_generation,
            self.membership_version,
            self.state_version,
            self.members,
        )
        if self.root_digest != expected_root:
            raise ValueError("composition root digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        composition_id: str,
        source_owner_generation: str,
        membership_version: str,
        state_version: int,
        members: tuple[PaperCompositionMemberState, ...],
    ) -> PaperCompositionStateRoot:
        return cls(
            schema_version=_STATE_SCHEMA_VERSION,
            composition_id=composition_id,
            source_owner_generation=source_owner_generation,
            membership_version=membership_version,
            state_version=state_version,
            members=members,
            root_digest=_root_digest(
                _STATE_SCHEMA_VERSION,
                composition_id,
                source_owner_generation,
                membership_version,
                state_version,
                members,
            ),
        )


class PaperCompositionUnavailable(RuntimeError):
    """Raised when a mutation/checkpoint cannot use an active composition."""


class PaperCompositionOwner:
    """Composition-wide boundary for an explicit finite member tuple.

    The public mutation surface mirrors every public ``BrokerSimulator`` state
    mutator: submit, cancel, update_position, schedule_close, process_bar,
    close_position and cancel_all. Batch bar processing holds one gate across
    every configured member. Membership is fixed at construction; this
    reference owner sets no production member limit and has no join/leave
    policy. ``pending_snapshots`` is read-only. No other Core mutator is
    exposed through this owner.
    """

    __slots__ = (
        "_composition_id",
        "_owner_generation",
        "_owner_instance_id",
        "_member_configs",
        "_membership",
        "_membership_version",
        "_members_by_id",
        "_brokers",
        "_lock",
        "_clock",
        "_snapshot_version",
        "_state_version",
        "_status",
        "_pristine",
    )

    def __init__(
        self,
        composition_id: str,
        members: tuple[PaperCompositionMemberConfig, ...],
        *,
        owner_generation: str | None = None,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._composition_id = require_nonblank(composition_id, "composition_id")
        if not isinstance(members, tuple) or not members:
            raise ValueError("composition must have a non-empty tuple of configured members")
        if any(not isinstance(member, PaperCompositionMemberConfig) for member in members):
            raise TypeError("members must contain PaperCompositionMemberConfig")
        ordered = tuple(sorted(members, key=lambda item: item.strategy_instance_id))
        ids = tuple(member.strategy_instance_id for member in ordered)
        if len(set(ids)) != len(ids):
            raise ValueError("strategy_instance_id values must be unique")
        self._owner_generation = require_nonblank(
            uuid4().hex if owner_generation is None else owner_generation,
            "owner_generation",
        )
        self._owner_instance_id = uuid4().hex
        self._member_configs = ordered
        self._membership = ids
        self._members_by_id = {member.strategy_instance_id: member for member in ordered}
        self._membership_version = _digest(
            tuple(
                {
                    "strategy_instance_id": member.strategy_instance_id,
                    "engine_config": to_jsonable(member.engine_config),
                    "instrument": to_jsonable(member.instrument),
                }
                for member in ordered
            )
        )
        self._brokers = {
            member.strategy_instance_id: BrokerSimulator(
                member.engine_config,
                Journal(
                    run_id=f"{self._composition_id}:{member.strategy_instance_id}",
                    event_store=InMemoryEventStore(),
                ),
                member.instrument,
            )
            for member in ordered
        }
        self._lock = threading.RLock()
        self._clock = utc_clock or (lambda: datetime.now(timezone.utc))
        if not callable(self._clock):
            raise TypeError("utc_clock must be callable")
        self._snapshot_version = 0
        self._state_version = 0
        self._status: Literal["active", "recovering"] = "active"
        self._pristine = True

    @property
    def membership_version(self) -> str:
        return self._membership_version

    @property
    def membership(self) -> tuple[str, ...]:
        return self._membership

    @property
    def state_version(self) -> int:
        with self._lock:
            return self._state_version

    def snapshot(self) -> PaperCompositionSnapshot:
        clock_failed = False
        try:
            as_of = utc_datetime(self._clock(), "utc_clock result")
        except Exception:
            as_of = datetime.now(timezone.utc)
            clock_failed = True
        with self._lock:
            self._snapshot_version += 1
            if clock_failed:
                self._status = "recovering"
                self._state_version += 1
            if self._status != "active":
                return self._snapshot("recovering", None, as_of=as_of)
            try:
                positions = tuple(
                    sorted(
                        (
                            PaperCompositionPosition(
                                identity=PaperPositionIdentity(
                                    strategy_instance_id=member.strategy_instance_id,
                                    core_position_id=position.id,
                                ),
                                instrument=member.instrument.symbol,
                                side=position.side,
                                quantity=position.quantity,
                                entry_price=position.entry_price,
                                entry_time=position.entry_time,
                                stop_loss=position.stop_loss,
                                take_profit=position.take_profit,
                            )
                            for member in self._member_configs
                            if (
                                position := self._brokers[
                                    member.strategy_instance_id
                                ].portfolio.position
                            )
                            is not None
                        ),
                        key=lambda item: (
                            item.identity.strategy_instance_id,
                            item.identity.core_position_id,
                        ),
                    )
                )
            except Exception:
                self._status = "recovering"
                self._state_version += 1
                return self._snapshot("recovering", None, as_of=as_of)
            return self._snapshot("complete", positions, as_of=as_of)

    def is_current(self, snapshot: object) -> bool:
        if not isinstance(snapshot, PaperCompositionSnapshot):
            return False
        with self._lock:
            return self._is_current_locked(snapshot)

    def pending_snapshots(self, strategy_instance_id: str) -> tuple[OrderSnapshot, ...]:
        with self._lock:
            broker = self._broker(strategy_instance_id)
            if self._status != "active":
                raise PaperCompositionUnavailable("composition is recovering")
            return broker.pending_snapshots()

    def submit(
        self,
        strategy_instance_id: str,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> OrderSnapshot | None:
        return self._mutate(
            strategy_instance_id,
            lambda broker: (
                None
                if (
                    order := broker.submit(
                        request,
                        source_module=source_module,
                        created_at=created_at,
                        current_index=current_index,
                        mark_price=mark_price,
                    )
                )
                is None
                else OrderSnapshot.from_order(order)
            ),
        )

    def cancel(
        self,
        strategy_instance_id: str,
        request: CancelRequest,
        *,
        timestamp: datetime,
        source_module: str,
    ) -> int:
        return self._mutate(
            strategy_instance_id,
            lambda broker: broker.cancel(request, timestamp=timestamp, source_module=source_module),
        )

    def update_position(
        self,
        strategy_instance_id: str,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool:
        return self._mutate(
            strategy_instance_id,
            lambda broker: broker.update_position(
                request,
                timestamp=timestamp,
                source_module=source_module,
                mark_price=mark_price,
            ),
        )

    def schedule_close(
        self,
        strategy_instance_id: str,
        request: ClosePositionRequest,
        *,
        timestamp: datetime,
        current_index: int,
        source_module: str,
    ) -> bool:
        return self._mutate(
            strategy_instance_id,
            lambda broker: broker.schedule_close(
                request,
                timestamp=timestamp,
                current_index=current_index,
                source_module=source_module,
            ),
        )

    def process_bar(self, strategy_instance_id: str, bar: Candle, index: int) -> None:
        member = self._member(strategy_instance_id)
        self._validate_bar_symbol(member, bar)
        self._mutate(strategy_instance_id, lambda broker: broker.process_bar(bar, index))

    def process_bars(
        self,
        bars: Mapping[str, Candle],
        index: int,
    ) -> None:
        if not isinstance(bars, Mapping) or set(bars) != set(self._membership):
            raise ValueError("batch bars must cover the exact composition membership")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("bar index must be a non-negative integer")
        frozen_bars = dict(bars)
        if any(not isinstance(bar, Candle) for bar in frozen_bars.values()):
            raise TypeError("batch bars must contain Candle values")
        for member_id, bar in frozen_bars.items():
            self._validate_bar_symbol(self._members_by_id[member_id], bar)
        with self._lock:
            self._require_active()
            self._pristine = False
            try:
                for member_id in self._membership:
                    self._process_one_locked(member_id, frozen_bars[member_id], index)
            except BaseException:
                self._status = "recovering"
                self._state_version += 1
                raise
            self._state_version += 1

    def close_position(
        self,
        snapshot: PaperCompositionSnapshot,
        position: PaperCompositionPosition,
        timestamp: datetime,
        price: float,
        reason: str,
        *,
        index: int | None = None,
        liquidity: str = "taker",
        reference_price: float | None = None,
        quantity: float | None = None,
    ) -> Trade | None:
        """Close only the exact position in the still-current owner snapshot.

        The snapshot and position identity are checked under the same owner lock
        as the Core close, so a stale request cannot target a replacement
        position belonging to the same strategy instance.
        """
        if not isinstance(snapshot, PaperCompositionSnapshot):
            raise TypeError("snapshot must be PaperCompositionSnapshot")
        if not isinstance(position, PaperCompositionPosition):
            raise TypeError("position must be PaperCompositionPosition")
        strategy_instance_id = position.identity.strategy_instance_id
        with self._lock:
            broker = self._broker(strategy_instance_id)
            if not self._is_current_locked(snapshot):
                raise PaperCompositionUnavailable("composition snapshot is stale")
            if snapshot.positions is None or position not in snapshot.positions:
                raise PaperCompositionUnavailable("position is outside composition snapshot")
            current_position = broker.portfolio.position
            if (
                current_position is None
                or current_position.id != position.identity.core_position_id
            ):
                raise PaperCompositionUnavailable("position identity is stale")
            self._pristine = False
            try:
                trade = broker.close_position(
                    timestamp,
                    price,
                    reason,
                    index=index,
                    liquidity=liquidity,
                    reference_price=reference_price,
                    quantity=quantity,
                )
            except BaseException:
                self._status = "recovering"
                self._state_version += 1
                raise
            self._state_version += 1
            return trade

    def cancel_all(
        self,
        strategy_instance_id: str,
        timestamp: datetime,
        reason: str,
    ) -> None:
        self._mutate(
            strategy_instance_id,
            lambda broker: broker.cancel_all(timestamp, reason),
        )

    def capture_state(self) -> PaperCompositionStateRoot:
        with self._lock:
            self._require_active()
            members = tuple(
                PaperCompositionMemberState(
                    strategy_instance_id=member.strategy_instance_id,
                    broker_state=(
                        broker_state := capture_broker_state(
                            self._brokers[member.strategy_instance_id]
                        )
                    ),
                    state_digest=_digest(broker_state.to_state()),
                )
                for member in self._member_configs
            )
            return PaperCompositionStateRoot.create(
                composition_id=self._composition_id,
                source_owner_generation=self._owner_generation,
                membership_version=self._membership_version,
                state_version=self._state_version,
                members=members,
            )

    def restore_state(self, root: PaperCompositionStateRoot) -> None:
        if not isinstance(root, PaperCompositionStateRoot):
            raise TypeError("root must be PaperCompositionStateRoot")
        with self._lock:
            self._require_pristine_candidate()
            self._validate_root(root)
            candidate_brokers = {
                member.strategy_instance_id: _new_broker(self._composition_id, member)
                for member in self._member_configs
            }
            # Validate every child against an isolated Core broker before any
            # live candidate member is changed.
            for member_state in root.members:
                restore_broker_state(
                    candidate_brokers[member_state.strategy_instance_id],
                    member_state.broker_state,
                )
            self._pristine = False
            try:
                for member_state in root.members:
                    restore_broker_state(
                        self._brokers[member_state.strategy_instance_id],
                        member_state.broker_state,
                    )
            except BaseException:
                self._status = "recovering"
                self._state_version += 1
                raise
            self._state_version = root.state_version

    def _validate_root(self, root: PaperCompositionStateRoot) -> None:
        if root.composition_id != self._composition_id:
            raise ValueError("composition identity mismatch")
        if root.source_owner_generation == self._owner_generation:
            raise ValueError("restore requires a new owner generation")
        if root.membership_version != self._membership_version:
            raise ValueError("membership version mismatch")
        if tuple(member.strategy_instance_id for member in root.members) != self._membership:
            raise ValueError("state root membership mismatch")
        for member_state in root.members:
            if _digest(member_state.broker_state.to_state()) != member_state.state_digest:
                raise ValueError("member state digest mismatch")

    def _require_pristine_candidate(self) -> None:
        if self._status != "active" or not self._pristine or self._state_version != 0:
            raise PaperCompositionUnavailable("restore requires a pristine candidate owner")

    def _mutate(
        self,
        strategy_instance_id: str,
        operation: Callable[[BrokerSimulator], _ResultT],
    ) -> _ResultT:
        with self._lock:
            broker = self._broker(strategy_instance_id)
            self._require_active()
            self._pristine = False
            try:
                result = operation(broker)
            except BaseException:
                self._status = "recovering"
                self._state_version += 1
                raise
            self._state_version += 1
            return result

    def _process_one_locked(self, strategy_instance_id: str, bar: Candle, index: int) -> None:
        self._brokers[strategy_instance_id].process_bar(bar, index)

    def _member(self, strategy_instance_id: str) -> PaperCompositionMemberConfig:
        require_nonblank(strategy_instance_id, "strategy_instance_id")
        if strategy_instance_id not in self._members_by_id:
            raise KeyError("strategy instance is outside composition membership")
        return self._members_by_id[strategy_instance_id]

    def _broker(self, strategy_instance_id: str) -> BrokerSimulator:
        self._member(strategy_instance_id)
        if strategy_instance_id not in self._brokers:
            raise KeyError("strategy instance is outside composition membership")
        return self._brokers[strategy_instance_id]

    @staticmethod
    def _validate_bar_symbol(member: PaperCompositionMemberConfig, bar: Candle) -> None:
        if not isinstance(bar, Candle):
            raise TypeError("bar must be Candle")
        if bar.symbol != member.instrument.symbol:
            raise ValueError("bar symbol does not match exact member instrument")

    def _require_active(self) -> None:
        if self._status != "active":
            raise PaperCompositionUnavailable("composition is recovering")

    def _is_current_locked(self, snapshot: PaperCompositionSnapshot) -> bool:
        return (
            self._status == "active"
            and snapshot.coverage == "complete"
            and snapshot.composition_id == self._composition_id
            and snapshot.owner_generation == self._owner_generation
            and snapshot.owner_instance_id == self._owner_instance_id
            and snapshot.membership == self._membership
            and snapshot.membership_version == self._membership_version
            and snapshot.snapshot_version == self._snapshot_version
            and snapshot.state_version == self._state_version
        )

    def _snapshot(
        self,
        coverage: Literal["complete", "unavailable", "recovering"],
        positions: tuple[PaperCompositionPosition, ...] | None,
        *,
        as_of: datetime,
    ) -> PaperCompositionSnapshot:
        return PaperCompositionSnapshot(
            composition_id=self._composition_id,
            owner_generation=self._owner_generation,
            owner_instance_id=self._owner_instance_id,
            membership_version=self._membership_version,
            snapshot_version=self._snapshot_version,
            state_version=self._state_version,
            as_of=as_of,
            coverage=coverage,
            membership=self._membership,
            positions=positions,
        )


def _new_broker(
    composition_id: str,
    member: PaperCompositionMemberConfig,
) -> BrokerSimulator:
    return BrokerSimulator(
        member.engine_config,
        Journal(
            run_id=f"{composition_id}:{member.strategy_instance_id}",
            event_store=InMemoryEventStore(),
        ),
        member.instrument,
    )


def _root_digest(
    schema_version: str,
    composition_id: str,
    owner_generation: str,
    membership_version: str,
    state_version: int,
    members: tuple[PaperCompositionMemberState, ...],
) -> str:
    return _digest(
        {
            "schema_version": schema_version,
            "composition_id": composition_id,
            "source_owner_generation": owner_generation,
            "membership_version": membership_version,
            "state_version": state_version,
            "members": tuple(
                {
                    "strategy_instance_id": member.strategy_instance_id,
                    "state_digest": member.state_digest,
                    "broker_state": member.broker_state.to_state(),
                }
                for member in members
            ),
        }
    )


def _digest(value: object) -> str:
    primitive = to_jsonable(value)
    body = json.dumps(primitive, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
