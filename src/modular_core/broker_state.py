from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Any, cast

from .config import DataConfig, EngineConfig, ExecutionConfig, FeeConfig, RiskConfig
from .execution import BrokerSimulator, PendingClose
from .extensions import StateRepository
from .instruments import InstrumentSpec
from .models import (
    Candle,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    TimeInForce,
)
from .serialization import deep_freeze, to_jsonable
from .validation import (
    finite_number,
    nonnegative_number,
    positive_number,
    require_nonblank,
    utc_datetime,
)

BROKER_STATE_SCHEMA_VERSION = "1"


class _BrokerStateValidationMode(Enum):
    DEFAULT = "default"
    CORE_CLOSED_FENCE = "core_closed_fence"
    QUARANTINE = "quarantine"


class _ClosedFenceStateIncompatible(ValueError):
    """The default snapshot is intact but cannot enter strict closed-fence mode."""


_BROKER_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "config",
        "instrument",
        "cash",
        "orders",
        "pending_order_ids",
        "used_client_order_ids",
        "order_sequence",
        "position_sequence",
        "fill_sequence",
        "position",
        "pending_close",
        "last_processed_bar",
        "historical_closed_equity",
    }
)
_BROKER_STATE_COMPAT_DEFAULTS: Mapping[str, object] = {
    "last_processed_bar": None,
    "historical_closed_equity": None,
}
_ENGINE_CONFIG_FIELDS = frozenset(field.name for field in fields(EngineConfig))
_FEE_CONFIG_FIELDS = frozenset(field.name for field in fields(FeeConfig))
_EXECUTION_CONFIG_FIELDS = frozenset(field.name for field in fields(ExecutionConfig))
_RISK_CONFIG_FIELDS = frozenset(field.name for field in fields(RiskConfig))
_DATA_CONFIG_FIELDS = frozenset(field.name for field in fields(DataConfig))
_INSTRUMENT_FIELDS = frozenset(field.name for field in fields(InstrumentSpec))
_ORDER_FIELDS = frozenset(field.name for field in fields(Order))
_POSITION_FIELDS = frozenset(field.name for field in fields(Position))
_FILL_FIELDS = frozenset(field.name for field in fields(Fill))
_PENDING_CLOSE_FIELDS = frozenset(field.name for field in fields(PendingClose))
_LAST_PROCESSED_BAR_FIELDS = frozenset({"timestamp", "index", "close"})
_EXECUTION_CONFIG_COMPAT_DEFAULTS: Mapping[str, object] = {
    "allow_limit_stop_on_entry_bar": False,
    "close_request_timing": "next_open",
}
_RISK_CONFIG_COMPAT_DEFAULTS: Mapping[str, object] = {"sizing_model": "stop_distance"}


@dataclass(frozen=True, slots=True)
class BrokerStateSnapshot:
    """Versioned factual state owned by one Core paper broker.

    Journal/event history is intentionally excluded. Core journals already own a
    durable EventStore contract; duplicating that history here would create a
    second audit ledger. The broker snapshot carries only state required to resume
    deterministic OMS execution without reusing order, position, fill, or
    client-order identities.
    """

    schema_version: str
    config: Mapping[str, Any]
    instrument: Mapping[str, Any]
    cash: float
    orders: tuple[Mapping[str, Any], ...]
    pending_order_ids: tuple[str, ...]
    used_client_order_ids: tuple[str, ...]
    order_sequence: int
    position_sequence: int
    fill_sequence: int
    position: Mapping[str, Any] | None
    pending_close: Mapping[str, Any] | None
    last_processed_bar: Mapping[str, Any] | None = None
    historical_closed_equity: float | None = None

    def __post_init__(self) -> None:
        if self.schema_version != BROKER_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported broker state schema version")
        object.__setattr__(self, "config", _mapping(self.config, "broker config"))
        object.__setattr__(
            self,
            "instrument",
            _mapping(self.instrument, "broker instrument"),
        )
        object.__setattr__(self, "cash", finite_number(self.cash, "broker cash"))
        object.__setattr__(
            self,
            "orders",
            tuple(_mapping(order, "broker order") for order in self.orders),
        )
        object.__setattr__(
            self,
            "pending_order_ids",
            _identity_tuple(self.pending_order_ids, "pending order id"),
        )
        object.__setattr__(
            self,
            "used_client_order_ids",
            _identity_tuple(self.used_client_order_ids, "used client order id"),
        )
        for name in ("order_sequence", "position_sequence", "fill_sequence"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be int")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.position is not None:
            object.__setattr__(
                self,
                "position",
                _mapping(self.position, "broker position"),
            )
        if self.pending_close is not None:
            object.__setattr__(
                self,
                "pending_close",
                _mapping(self.pending_close, "broker pending close"),
            )
        if self.last_processed_bar is not None:
            object.__setattr__(
                self,
                "last_processed_bar",
                _validated_last_processed_bar_mapping(self.last_processed_bar),
            )
        if self.historical_closed_equity is not None:
            object.__setattr__(
                self,
                "historical_closed_equity",
                finite_number(
                    self.historical_closed_equity,
                    "historical closed equity",
                ),
            )

    def to_state(self) -> Mapping[str, Any]:
        state = to_jsonable(self)
        if not isinstance(state, Mapping):
            raise RuntimeError("broker snapshot did not serialize to a mapping")
        return _mapping(state, "broker state")

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> BrokerStateSnapshot:
        state = _compatible_broker_state_mapping(value)
        orders = _sequence(state["orders"], "broker orders")
        pending_ids = _sequence(state["pending_order_ids"], "pending order ids")
        used_client_ids = _sequence(
            state["used_client_order_ids"],
            "used client order ids",
        )
        return cls(
            schema_version=_string(state["schema_version"], "schema_version"),
            config=_mapping(state["config"], "broker config"),
            instrument=_mapping(state["instrument"], "broker instrument"),
            cash=_number(state["cash"], "broker cash"),
            orders=tuple(_mapping(item, "broker order") for item in orders),
            pending_order_ids=tuple(_string(item, "pending order id") for item in pending_ids),
            used_client_order_ids=tuple(
                _string(item, "used client order id") for item in used_client_ids
            ),
            order_sequence=_integer(state["order_sequence"], "order_sequence"),
            position_sequence=_integer(
                state["position_sequence"],
                "position_sequence",
            ),
            fill_sequence=_integer(state["fill_sequence"], "fill_sequence"),
            position=(
                None
                if state["position"] is None
                else _mapping(state["position"], "broker position")
            ),
            pending_close=(
                None
                if state["pending_close"] is None
                else _mapping(state["pending_close"], "broker pending close")
            ),
            last_processed_bar=(
                None
                if state["last_processed_bar"] is None
                else _mapping(state["last_processed_bar"], "last processed bar")
            ),
            historical_closed_equity=_optional_number(
                state["historical_closed_equity"],
                "historical closed equity",
            ),
        )


@dataclass(frozen=True, slots=True)
class _PreparedBrokerState:
    orders: tuple[Order, ...]
    pending: Mapping[str, Order]
    used_client_order_ids: frozenset[str]
    position: Position | None
    pending_close: PendingClose | None
    last_processed_bar: Candle | None
    last_processed_index: int | None
    historical_closed_equity: float


def capture_broker_state(
    broker: BrokerSimulator,
) -> BrokerStateSnapshot:
    return _capture_broker_state(broker, _BrokerStateValidationMode.DEFAULT)


def _capture_core_closed_fence_broker_state(broker: BrokerSimulator) -> BrokerStateSnapshot:
    return _capture_broker_state(broker, _BrokerStateValidationMode.CORE_CLOSED_FENCE)


def _capture_quarantine_broker_state(broker: BrokerSimulator) -> BrokerStateSnapshot:
    return _capture_broker_state(broker, _BrokerStateValidationMode.QUARANTINE)


def _capture_broker_state(
    broker: BrokerSimulator,
    validation_mode: _BrokerStateValidationMode,
) -> BrokerStateSnapshot:
    if not isinstance(broker, BrokerSimulator):
        raise TypeError("broker must be BrokerSimulator")
    _validate_validation_mode(validation_mode)
    snapshot = BrokerStateSnapshot(
        schema_version=BROKER_STATE_SCHEMA_VERSION,
        config=_encode_mapping(broker.config, "broker config"),
        instrument=_encode_mapping(broker.instrument, "broker instrument"),
        cash=broker.portfolio.cash,
        orders=tuple(_encode_mapping(order, "broker order") for order in broker.orders),
        pending_order_ids=tuple(sorted(broker._pending)),
        used_client_order_ids=tuple(sorted(broker._used_client_order_ids)),
        order_sequence=broker._order_sequence,
        position_sequence=broker._position_sequence,
        fill_sequence=broker._fill_sequence,
        position=(
            None
            if broker.portfolio.position is None
            else _encode_mapping(broker.portfolio.position, "broker position")
        ),
        pending_close=(
            None
            if broker.pending_close is None
            else _encode_mapping(broker.pending_close, "broker pending close")
        ),
        last_processed_bar=_capture_last_processed_bar_state(broker),
        historical_closed_equity=broker._historical_closed_equity,
    )
    _prepare_broker_state(
        snapshot,
        expected_config=broker.config,
        expected_instrument=broker.instrument,
        validation_mode=validation_mode,
    )
    return snapshot


def restore_broker_state(
    broker: BrokerSimulator,
    snapshot: BrokerStateSnapshot,
) -> None:
    _restore_broker_state(broker, snapshot, _BrokerStateValidationMode.DEFAULT)


def _restore_core_closed_fence_broker_state(
    broker: BrokerSimulator,
    snapshot: BrokerStateSnapshot,
) -> None:
    _restore_broker_state(broker, snapshot, _BrokerStateValidationMode.CORE_CLOSED_FENCE)


def _restore_quarantine_broker_state(
    broker: BrokerSimulator,
    snapshot: BrokerStateSnapshot,
) -> None:
    _restore_broker_state(broker, snapshot, _BrokerStateValidationMode.QUARANTINE)


def _restore_broker_state(
    broker: BrokerSimulator,
    snapshot: BrokerStateSnapshot,
    validation_mode: _BrokerStateValidationMode,
) -> None:
    if not isinstance(broker, BrokerSimulator):
        raise TypeError("broker must be BrokerSimulator")
    if not isinstance(snapshot, BrokerStateSnapshot):
        raise TypeError("snapshot must be BrokerStateSnapshot")
    _validate_validation_mode(validation_mode)
    _require_pristine_restore_target(broker)
    prepared = _prepare_broker_state(
        snapshot,
        expected_config=broker.config,
        expected_instrument=broker.instrument,
        validation_mode=validation_mode,
    )

    original = (
        broker.portfolio.cash,
        broker._historical_closed_equity,
        broker.portfolio.position,
        broker.portfolio.contract_size,
        broker.orders,
        broker._pending,
        broker._used_client_order_ids,
        broker._order_sequence,
        broker._position_sequence,
        broker._fill_sequence,
        broker.pending_close,
        broker._last_processed_bar,
        broker._last_processed_index,
        broker._core_closed_fence_validation,
        broker._core_quarantine_validation,
    )
    try:
        broker.portfolio.cash = snapshot.cash
        broker._historical_closed_equity = prepared.historical_closed_equity
        broker.portfolio.position = prepared.position
        broker.portfolio.contract_size = broker.instrument.contract_size
        broker.orders = list(prepared.orders)
        broker._pending = dict(prepared.pending)
        broker._used_client_order_ids = set(prepared.used_client_order_ids)
        broker._order_sequence = snapshot.order_sequence
        broker._position_sequence = snapshot.position_sequence
        broker._fill_sequence = snapshot.fill_sequence
        broker.pending_close = prepared.pending_close
        broker._last_processed_bar = prepared.last_processed_bar
        broker._last_processed_index = prepared.last_processed_index
        broker._core_closed_fence_validation = (
            validation_mode is _BrokerStateValidationMode.CORE_CLOSED_FENCE
        )
        broker._core_quarantine_validation = (
            validation_mode is _BrokerStateValidationMode.QUARANTINE
        )
    except Exception:
        (
            broker.portfolio.cash,
            broker._historical_closed_equity,
            broker.portfolio.position,
            broker.portfolio.contract_size,
            broker.orders,
            broker._pending,
            broker._used_client_order_ids,
            broker._order_sequence,
            broker._position_sequence,
            broker._fill_sequence,
            broker.pending_close,
            broker._last_processed_bar,
            broker._last_processed_index,
            broker._core_closed_fence_validation,
            broker._core_quarantine_validation,
        ) = original
        raise


def save_broker_state(
    repository: StateRepository,
    state_key: str,
    broker: BrokerSimulator,
) -> BrokerStateSnapshot:
    snapshot = capture_broker_state(broker)
    repository.save(state_key, snapshot.to_state())
    return snapshot


def restore_broker_state_from_repository(
    repository: StateRepository,
    state_key: str,
    broker: BrokerSimulator,
) -> BrokerStateSnapshot:
    state = repository.load(state_key)
    if state is None:
        raise ValueError("broker state is missing")
    snapshot = BrokerStateSnapshot.from_state(state)
    restore_broker_state(broker, snapshot)
    return snapshot


def _prepare_broker_state(
    snapshot: BrokerStateSnapshot,
    *,
    expected_config: EngineConfig,
    expected_instrument: InstrumentSpec,
    validation_mode: _BrokerStateValidationMode = _BrokerStateValidationMode.DEFAULT,
) -> _PreparedBrokerState:
    _validate_validation_mode(validation_mode)
    config = _decode_engine_config(snapshot.config)
    instrument = _decode_instrument(snapshot.instrument)
    if config != expected_config:
        raise ValueError("broker state config identity mismatch")
    if instrument != expected_instrument:
        raise ValueError("broker state instrument identity mismatch")

    historical_closed_equity = _resolve_historical_closed_equity(snapshot, config)
    orders = tuple(_decode_order(value) for value in snapshot.orders)
    position = None if snapshot.position is None else _decode_position(snapshot.position)
    by_id = _validate_orders(snapshot, config, orders, position, validation_mode)
    pending_close = (
        None if snapshot.pending_close is None else _decode_pending_close(snapshot.pending_close)
    )
    last_processed = (
        None
        if snapshot.last_processed_bar is None
        else _decode_last_processed_bar(snapshot.last_processed_bar, instrument)
    )
    _validate_position_and_fill_state(snapshot, orders, by_id, position)
    _validate_pending_relationships(
        snapshot, by_id, position, pending_close, validation_mode
    )
    return _PreparedBrokerState(
        orders=orders,
        pending={order_id: by_id[order_id] for order_id in snapshot.pending_order_ids},
        used_client_order_ids=frozenset(snapshot.used_client_order_ids),
        position=position,
        pending_close=pending_close,
        last_processed_bar=None if last_processed is None else last_processed[0],
        last_processed_index=None if last_processed is None else last_processed[1],
        historical_closed_equity=historical_closed_equity,
    )


def _resolve_historical_closed_equity(
    snapshot: BrokerStateSnapshot,
    config: EngineConfig,
) -> float:
    value = snapshot.historical_closed_equity
    if value is None:
        if config.risk.sizing_model == "historical_limit_cost" and snapshot.fill_sequence:
            raise ValueError(
                "legacy broker state is missing historical closed equity after historical execution"
            )
        return config.initial_cash
    if config.risk.sizing_model == "historical_limit_cost":
        if snapshot.fill_sequence == 0 and value != config.initial_cash:
            raise ValueError("historical closed equity changed before first historical fill")
    elif value != config.initial_cash:
        raise ValueError("non-historical broker state has unexpected historical closed equity")
    return value


def _validate_orders(
    snapshot: BrokerStateSnapshot,
    config: EngineConfig,
    orders: tuple[Order, ...],
    position: Position | None,
    validation_mode: _BrokerStateValidationMode,
) -> dict[str, Order]:
    if len(orders) != snapshot.order_sequence:
        raise ValueError("order sequence counter does not match complete order history")
    expected_sequences = list(range(1, snapshot.order_sequence + 1))
    actual_sequences = [order.sequence for order in orders]
    if actual_sequences != expected_sequences:
        raise ValueError("order sequence history must be contiguous and ordered")

    order_ids = [order.id for order in orders]
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("broker state contains duplicate order ids")
    if config.deterministic_order_ids:
        for order in orders:
            if order.id != f"ord-{order.sequence:08d}":
                raise ValueError("deterministic order id does not match sequence")

    by_id = {order.id: order for order in orders}
    if any(order.close_position and not order.reduce_only for order in orders):
        raise ValueError("close-position order is not reduce-only")
    expected_pending = {order.id for order in orders if not order.status.terminal}
    if set(snapshot.pending_order_ids) != expected_pending:
        raise ValueError("pending order membership does not match order status")
    if len(expected_pending) > 1 and validation_mode is _BrokerStateValidationMode.CORE_CLOSED_FENCE:
        pending = tuple(by_id[order_id] for order_id in expected_pending)
        non_reduce = tuple(order for order in pending if not _is_reduce_only_order(order))
        reducers = tuple(order for order in pending if _is_reduce_only_order(order))
        if len(pending) > 2 or len(non_reduce) > 1 or len(reducers) > 1:
            raise _ClosedFenceStateIncompatible(
                "Core closed-fence state exceeds the entry/reducer pair bound"
            )
    elif config.one_active_order and len(expected_pending) > 1:
        raise ValueError("broker state violates one-active-order policy")

    client_ids = [order.client_order_id for order in orders if order.client_order_id is not None]
    if len(client_ids) != len(set(client_ids)):
        raise ValueError("broker state contains duplicate client order ids")
    if set(client_ids) != set(snapshot.used_client_order_ids):
        raise ValueError("client-order duplicate fence does not match order history")
    return by_id


def _validate_position_and_fill_state(
    snapshot: BrokerStateSnapshot,
    orders: tuple[Order, ...],
    by_id: Mapping[str, Order],
    position: Position | None,
) -> None:
    if position is None:
        return
    if position.id != f"pos-{snapshot.position_sequence:08d}":
        raise ValueError("active position id does not match position sequence")
    if position.entry_order_id not in by_id:
        raise ValueError("active position entry order is missing from order history")
    if not position.entry_fills:
        raise ValueError("active position requires at least one entry fill")

    all_fills = (*position.entry_fills, *position.exit_fills)
    fill_ids: list[str] = []
    for fill in all_fills:
        if fill.fill_id is None:
            raise ValueError("broker-managed position fill must have a deterministic fill id")
        fill_ids.append(fill.fill_id)
        if fill.position_id != position.id:
            raise ValueError("position fill references a different position id")
        numeric_id = _deterministic_numeric_id(fill.fill_id, "fill-", "fill id")
        if numeric_id > snapshot.fill_sequence:
            raise ValueError("fill sequence counter is behind active position fills")
    if len(fill_ids) != len(set(fill_ids)):
        raise ValueError("active position contains duplicate fill ids")

    entry_side = Side.BUY if position.side is PositionSide.LONG else Side.SELL
    exit_side = entry_side.opposite
    for fill in position.entry_fills:
        if fill.side is not entry_side:
            raise ValueError("entry fill side conflicts with active position")
        order = by_id.get(fill.order_id)
        if order is None or order.reduce_only:
            raise ValueError("entry fill must reference a persisted risk-increasing order")
    for fill in position.exit_fills:
        if fill.side is not exit_side:
            raise ValueError("exit fill side conflicts with active position")
        if fill.order_id.startswith("exit:"):
            if fill.order_id != f"exit:{position.entry_order_id}":
                raise ValueError("synthetic exit fill references the wrong entry order")
        else:
            order = by_id.get(fill.order_id)
            if order is None or not order.reduce_only:
                raise ValueError("exit fill must reference a reduce-only order or synthetic exit")

    entry_quantity = sum(fill.quantity for fill in position.entry_fills)
    exit_quantity = sum(fill.quantity for fill in position.exit_fills)
    expected_quantity = entry_quantity - exit_quantity
    if not math.isclose(
        position.quantity,
        expected_quantity,
        rel_tol=1e-10,
        abs_tol=1e-10,
    ):
        raise ValueError("active position quantity disagrees with persisted fills")

    active_fill_quantity_by_order: dict[str, float] = {}
    for fill in all_fills:
        if fill.order_id in by_id:
            active_fill_quantity_by_order[fill.order_id] = (
                active_fill_quantity_by_order.get(fill.order_id, 0.0) + fill.quantity
            )
    for order_id, fill_quantity in active_fill_quantity_by_order.items():
        order = by_id[order_id]
        if not math.isclose(
            order.filled_quantity,
            fill_quantity,
            rel_tol=1e-10,
            abs_tol=1e-10,
        ):
            raise ValueError("order filled quantity disagrees with active position fills")

    if snapshot.fill_sequence < len(all_fills):
        raise ValueError("fill sequence counter cannot be below active fill count")
    if not orders and snapshot.position_sequence:
        raise ValueError("position history cannot exist without order history")


def _validate_pending_relationships(
    snapshot: BrokerStateSnapshot,
    by_id: Mapping[str, Order],
    position: Position | None,
    pending_close: PendingClose | None,
    validation_mode: _BrokerStateValidationMode,
) -> None:
    quarantine_mode = validation_mode is _BrokerStateValidationMode.QUARANTINE
    if pending_close is not None and position is None and not quarantine_mode:
        raise ValueError("pending close requires an active position")
    pending_orders = tuple(by_id[order_id] for order_id in snapshot.pending_order_ids)
    reducers = tuple(order for order in pending_orders if _is_reduce_only_order(order))
    entries = tuple(order for order in pending_orders if not _is_reduce_only_order(order))
    closed_mode = validation_mode is _BrokerStateValidationMode.CORE_CLOSED_FENCE
    if closed_mode and pending_close is not None and reducers:
        raise _ClosedFenceStateIncompatible(
            "Core closed-fence pending close conflicts with reduce-only order"
        )
    if closed_mode and position is not None and entries:
        entry_side = Side.BUY if position.side is PositionSide.LONG else Side.SELL
        entry = entries[0]
        if (
            entry.id != position.entry_order_id
            or entry.filled_quantity <= 0
            or entry.side is not entry_side
            or entry.filled_quantity + 1e-12 < position.quantity
        ):
            raise _ClosedFenceStateIncompatible(
                "Core closed-fence pending entry conflicts with active position"
            )
    for order in reducers:
        if position is None:
            if quarantine_mode:
                continue
            raise ValueError("pending reduce-only order requires an active position")
        expected_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        if order.side is not expected_side:
            raise ValueError("pending reduce-only order side conflicts with active position")
        if order.position_side is not None and order.position_side is not position.side:
            raise ValueError(
                "pending reduce-only order position side conflicts with active position"
            )
        if closed_mode and order.remaining_quantity > position.quantity + 1e-12:
            raise _ClosedFenceStateIncompatible(
                "Core closed-fence reduce-only volume exceeds active position"
            )


def _is_reduce_only_order(order: Order) -> bool:
    return order.reduce_only or order.close_position


def _validate_validation_mode(mode: _BrokerStateValidationMode) -> None:
    if not isinstance(mode, _BrokerStateValidationMode):
        raise TypeError("invalid broker state validation mode")


def _require_pristine_restore_target(broker: BrokerSimulator) -> None:
    if broker._core_closed_fence_validation or broker._core_quarantine_validation:
        raise ValueError("broker restore target cannot carry Core closed-fence context")
    if broker.orders or broker._pending or broker._used_client_order_ids:
        raise ValueError("broker restore target must not contain order state")
    if broker._order_sequence or broker._position_sequence or broker._fill_sequence:
        raise ValueError("broker restore target must have zero identity sequences")
    if broker.portfolio.position is not None or broker.pending_close is not None:
        raise ValueError("broker restore target must not contain position state")
    if broker._last_processed_bar is not None or broker._last_processed_index is not None:
        raise ValueError("broker restore target must not contain processed-bar state")
    if broker.portfolio.cash != broker.config.initial_cash:
        raise ValueError("broker restore target cash is not pristine")
    if broker._historical_closed_equity != broker.config.initial_cash:
        raise ValueError("broker restore target historical closed equity is not pristine")
    if broker.portfolio.contract_size != broker.instrument.contract_size:
        raise ValueError("broker restore target contract size is inconsistent")


def _capture_last_processed_bar_state(broker: BrokerSimulator) -> Mapping[str, Any] | None:
    bar = broker._last_processed_bar
    index = broker._last_processed_index
    if (bar is None) != (index is None):
        raise RuntimeError("broker last processed bar state is inconsistent")
    if bar is None or index is None:
        return None
    encoded = to_jsonable(
        {
            "timestamp": bar.timestamp,
            "index": index,
            "close": bar.close,
        }
    )
    return _validated_last_processed_bar_mapping(encoded)


def _decode_last_processed_bar(
    value: Mapping[str, Any],
    instrument: InstrumentSpec,
) -> tuple[Candle, int]:
    state = _validated_last_processed_bar_mapping(value)
    timestamp = _datetime(state["timestamp"], "last processed bar timestamp")
    index = _integer(state["index"], "last processed bar index")
    close = positive_number(
        _number(state["close"], "last processed bar close"),
        "last processed bar close",
    )
    return (
        Candle(
            timestamp=timestamp,
            open=close,
            high=close,
            low=close,
            close=close,
            symbol=instrument.symbol,
            timeframe="base",
        ),
        index,
    )


def _validated_last_processed_bar_mapping(value: object) -> Mapping[str, Any]:
    state = _exact_mapping(value, _LAST_PROCESSED_BAR_FIELDS, "last processed bar")
    _datetime(state["timestamp"], "last processed bar timestamp")
    index = _integer(state["index"], "last processed bar index")
    if index < 0:
        raise ValueError("last processed bar index cannot be negative")
    positive_number(
        _number(state["close"], "last processed bar close"),
        "last processed bar close",
    )
    return state


def _decode_engine_config(value: Mapping[str, Any]) -> EngineConfig:
    state = _exact_mapping(value, _ENGINE_CONFIG_FIELDS, "broker config")
    fees_state = _exact_mapping(state["fees"], _FEE_CONFIG_FIELDS, "fee config")
    execution_state = _compatible_config_mapping(
        state["execution"],
        _EXECUTION_CONFIG_FIELDS,
        _EXECUTION_CONFIG_COMPAT_DEFAULTS,
        "execution config",
    )
    risk_state = _compatible_config_mapping(
        state["risk"],
        _RISK_CONFIG_FIELDS,
        _RISK_CONFIG_COMPAT_DEFAULTS,
        "risk config",
    )
    data_state = _exact_mapping(state["data"], _DATA_CONFIG_FIELDS, "data config")
    return EngineConfig(
        initial_cash=_number(state["initial_cash"], "initial_cash"),
        one_active_order=_boolean(state["one_active_order"], "one_active_order"),
        one_position=_boolean(state["one_position"], "one_position"),
        allow_scale_in=_boolean(state["allow_scale_in"], "allow_scale_in"),
        close_open_position_at_end=_boolean(
            state["close_open_position_at_end"],
            "close_open_position_at_end",
        ),
        strict_data_validation=_boolean(
            state["strict_data_validation"],
            "strict_data_validation",
        ),
        strict_module_errors=_boolean(
            state["strict_module_errors"],
            "strict_module_errors",
        ),
        deterministic_order_ids=_boolean(
            state["deterministic_order_ids"],
            "deterministic_order_ids",
        ),
        strict_feature_contracts=_boolean(
            state["strict_feature_contracts"],
            "strict_feature_contracts",
        ),
        strict_setup_transitions=_boolean(
            state["strict_setup_transitions"],
            "strict_setup_transitions",
        ),
        fees=FeeConfig(
            maker_rate=_number(fees_state["maker_rate"], "maker_rate"),
            taker_rate=_number(fees_state["taker_rate"], "taker_rate"),
        ),
        execution=ExecutionConfig(
            slippage_bps=_number(execution_state["slippage_bps"], "slippage_bps"),
            stop_slippage_bps=_number(
                execution_state["stop_slippage_bps"],
                "stop_slippage_bps",
            ),
            intrabar_priority=_string(
                execution_state["intrabar_priority"],
                "intrabar_priority",
            ),
            limit_fill_policy=_string(
                execution_state["limit_fill_policy"],
                "limit_fill_policy",
            ),
            allow_exit_on_entry_bar=_boolean(
                execution_state["allow_exit_on_entry_bar"],
                "allow_exit_on_entry_bar",
            ),
            allow_limit_stop_on_entry_bar=_boolean(
                execution_state["allow_limit_stop_on_entry_bar"],
                "allow_limit_stop_on_entry_bar",
            ),
            allow_reentry_on_exit_bar=_boolean(
                execution_state["allow_reentry_on_exit_bar"],
                "allow_reentry_on_exit_bar",
            ),
            close_request_timing=_string(
                execution_state["close_request_timing"],
                "close_request_timing",
            ),
            target_liquidity=_string(
                execution_state["target_liquidity"],
                "target_liquidity",
            ),
            max_fill_fraction_per_bar=_number(
                execution_state["max_fill_fraction_per_bar"],
                "max_fill_fraction_per_bar",
            ),
            max_fill_quantity_per_bar=_optional_number(
                execution_state["max_fill_quantity_per_bar"],
                "max_fill_quantity_per_bar",
            ),
            stop_limit_same_bar_policy=_string(
                execution_state["stop_limit_same_bar_policy"],
                "stop_limit_same_bar_policy",
            ),
        ),
        risk=RiskConfig(
            default_risk_fraction=_number(
                risk_state["default_risk_fraction"],
                "default_risk_fraction",
            ),
            max_risk_fraction=_number(
                risk_state["max_risk_fraction"],
                "max_risk_fraction",
            ),
            max_notional_multiple=_number(
                risk_state["max_notional_multiple"],
                "max_notional_multiple",
            ),
            min_notional=_number(risk_state["min_notional"], "min_notional"),
            min_quantity=_number(risk_state["min_quantity"], "min_quantity"),
            quantity_step=_number(risk_state["quantity_step"], "quantity_step"),
            min_stop_distance_pct=_number(
                risk_state["min_stop_distance_pct"],
                "min_stop_distance_pct",
            ),
            max_stop_distance_pct=_optional_number(
                risk_state["max_stop_distance_pct"],
                "max_stop_distance_pct",
            ),
            sizing_model=_string(risk_state["sizing_model"], "sizing_model"),
        ),
        data=DataConfig(
            expected_interval_seconds=_optional_integer(
                data_state["expected_interval_seconds"],
                "expected_interval_seconds",
            ),
            gap_policy=_string(data_state["gap_policy"], "gap_policy"),
            timestamp_semantics=_string(
                data_state["timestamp_semantics"],
                "timestamp_semantics",
            ),
            max_base_history=_optional_integer(
                data_state["max_base_history"],
                "max_base_history",
            ),
        ),
    )


def _decode_instrument(value: Mapping[str, Any]) -> InstrumentSpec:
    state = _exact_mapping(value, _INSTRUMENT_FIELDS, "broker instrument")
    return InstrumentSpec(
        symbol=_string(state["symbol"], "instrument symbol"),
        base_asset=_string(state["base_asset"], "base_asset"),
        quote_asset=_string(state["quote_asset"], "quote_asset"),
        market_type=_string(state["market_type"], "market_type"),
        tick_size=_number(state["tick_size"], "tick_size"),
        quantity_step=_number(state["quantity_step"], "quantity_step"),
        min_quantity=_number(state["min_quantity"], "min_quantity"),
        max_quantity=_optional_number(state["max_quantity"], "max_quantity"),
        min_notional=_number(state["min_notional"], "min_notional"),
        max_notional=_optional_number(state["max_notional"], "max_notional"),
        contract_size=_number(state["contract_size"], "contract_size"),
        price_precision=_optional_integer(
            state["price_precision"],
            "price_precision",
        ),
        quantity_precision=_optional_integer(
            state["quantity_precision"],
            "quantity_precision",
        ),
    )


def _decode_order(value: Mapping[str, Any]) -> Order:
    state = _exact_mapping(value, _ORDER_FIELDS, "broker order")
    metadata = _mapping(state["metadata"], "order metadata")
    order = Order(
        id=_string(state["id"], "order id"),
        sequence=_positive_integer(state["sequence"], "order sequence"),
        source_module=_string(state["source_module"], "order source_module"),
        created_at=_datetime(state["created_at"], "order created_at"),
        eligible_from_index=_integer(state["eligible_from_index"], "eligible_from_index"),
        expires_after_index=_optional_integer(
            state["expires_after_index"],
            "expires_after_index",
        ),
        side=Side(_string(state["side"], "order side")),
        order_type=OrderType(_string(state["order_type"], "order type")),
        quantity=positive_number(_number(state["quantity"], "quantity"), "quantity"),
        price=_optional_positive_number(state["price"], "price"),
        stop_loss=_optional_positive_number(state["stop_loss"], "stop_loss"),
        take_profit=_optional_positive_number(state["take_profit"], "take_profit"),
        risk_fraction=_optional_positive_number(
            state["risk_fraction"],
            "risk_fraction",
        ),
        quantity_is_explicit=_boolean(
            state["quantity_is_explicit"],
            "quantity_is_explicit",
        ),
        tag=_blankable_string(state["tag"], "order tag"),
        setup_id=_optional_string(state["setup_id"], "setup_id"),
        client_order_id=_optional_string(
            state["client_order_id"],
            "client_order_id",
        ),
        status=OrderStatus(_string(state["status"], "order status")),
        metadata=dict(metadata),
        time_in_force=TimeInForce(_string(state["time_in_force"], "time_in_force")),
        post_only=_boolean(state["post_only"], "post_only"),
        reduce_only=_boolean(state["reduce_only"], "reduce_only"),
        good_till_time=_optional_datetime(state["good_till_time"], "good_till_time"),
        close_position=_boolean(state["close_position"], "close_position"),
        trigger_price=_optional_positive_number(
            state["trigger_price"],
            "trigger_price",
        ),
        limit_price=_optional_positive_number(state["limit_price"], "limit_price"),
        position_side=(
            None
            if state["position_side"] is None
            else PositionSide(_string(state["position_side"], "position_side"))
        ),
        filled_quantity=nonnegative_number(
            _number(state["filled_quantity"], "filled_quantity"),
            "filled_quantity",
        ),
        average_fill_price=_optional_positive_number(
            state["average_fill_price"],
            "average_fill_price",
        ),
        fees_paid=nonnegative_number(
            _number(state["fees_paid"], "fees_paid"),
            "fees_paid",
        ),
        triggered_at=_optional_datetime(state["triggered_at"], "triggered_at"),
        triggered_index=_optional_integer(state["triggered_index"], "triggered_index"),
    )
    _validate_decoded_order(order)
    return order


def _validate_decoded_order(order: Order) -> None:
    if order.eligible_from_index < 0:
        raise ValueError("eligible_from_index cannot be negative")
    if order.expires_after_index is not None:
        if order.expires_after_index < order.eligible_from_index:
            raise ValueError("expires_after_index cannot precede eligibility")
    if order.triggered_index is not None and order.triggered_index < 0:
        raise ValueError("triggered_index cannot be negative")
    if (order.triggered_at is None) != (order.triggered_index is None):
        raise ValueError("triggered_at and triggered_index must be present together")
    if order.triggered_at is not None and order.order_type not in {
        OrderType.STOP_LIMIT,
        OrderType.TAKE_PROFIT_LIMIT,
    }:
        raise ValueError("only triggered limit orders may persist trigger state")
    if order.filled_quantity > order.quantity + 1e-12:
        raise ValueError("order filled quantity exceeds order quantity")
    if order.filled_quantity <= 1e-12:
        if order.average_fill_price is not None or order.fees_paid > 1e-12:
            raise ValueError("unfilled order cannot contain fill accounting")
    elif order.average_fill_price is None:
        raise ValueError("filled order requires average_fill_price")
    if order.status is OrderStatus.FILLED and order.remaining_quantity > 1e-12:
        raise ValueError("filled order has remaining quantity")
    if order.status is OrderStatus.PARTIALLY_FILLED and not (
        1e-12 < order.filled_quantity < order.quantity - 1e-12
    ):
        raise ValueError("partially-filled order has inconsistent filled quantity")

    OrderRequest(
        side=order.side,
        order_type=order.order_type,
        quantity=order.quantity if order.quantity_is_explicit else None,
        price=order.price,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        risk_fraction=order.risk_fraction,
        tag=order.tag,
        setup_id=order.setup_id,
        client_order_id=order.client_order_id,
        metadata=order.metadata,
        time_in_force=order.time_in_force,
        post_only=order.post_only,
        reduce_only=order.reduce_only,
        good_till_time=order.good_till_time,
        close_position=order.close_position,
        trigger_price=order.trigger_price,
        limit_price=order.limit_price,
        position_side=order.position_side,
    )


def _decode_position(value: Mapping[str, Any]) -> Position:
    state = _exact_mapping(value, _POSITION_FIELDS, "broker position")
    entry_fills = [
        _decode_fill(_mapping(item, "entry fill"))
        for item in _sequence(state["entry_fills"], "entry_fills")
    ]
    exit_fills = [
        _decode_fill(_mapping(item, "exit fill"))
        for item in _sequence(state["exit_fills"], "exit_fills")
    ]
    entry_index = _integer(state["entry_index"], "entry_index")
    if entry_index < 0:
        raise ValueError("entry_index cannot be negative")
    position = Position(
        id=_string(state["id"], "position id"),
        entry_order_id=_string(state["entry_order_id"], "entry_order_id"),
        side=PositionSide(_string(state["side"], "position side")),
        quantity=positive_number(
            _number(state["quantity"], "position quantity"),
            "position quantity",
        ),
        entry_price=positive_number(
            _number(state["entry_price"], "entry_price"),
            "entry_price",
        ),
        entry_time=_datetime(state["entry_time"], "entry_time"),
        entry_index=entry_index,
        entry_fee=nonnegative_number(
            _number(state["entry_fee"], "entry_fee"),
            "entry_fee",
        ),
        initial_risk_amount=_optional_nonnegative_number(
            state["initial_risk_amount"],
            "initial_risk_amount",
        ),
        stop_loss=_optional_positive_number(state["stop_loss"], "stop_loss"),
        take_profit=_optional_positive_number(state["take_profit"], "take_profit"),
        tag=_blankable_string(state["tag"], "position tag"),
        setup_id=_optional_string(state["setup_id"], "setup_id"),
        realised_pnl=finite_number(
            _number(state["realised_pnl"], "realised_pnl"),
            "realised_pnl",
        ),
        exit_fees=nonnegative_number(
            _number(state["exit_fees"], "exit_fees"),
            "exit_fees",
        ),
        entry_fills=entry_fills,
        exit_fills=exit_fills,
    )
    return position


def _decode_fill(value: Mapping[str, Any]) -> Fill:
    state = _exact_mapping(value, _FILL_FIELDS, "broker fill")
    return Fill(
        order_id=_string(state["order_id"], "fill order_id"),
        timestamp=_datetime(state["timestamp"], "fill timestamp"),
        side=Side(_string(state["side"], "fill side")),
        quantity=positive_number(
            _number(state["quantity"], "fill quantity"),
            "fill quantity",
        ),
        price=positive_number(_number(state["price"], "fill price"), "fill price"),
        fee=nonnegative_number(_number(state["fee"], "fill fee"), "fill fee"),
        slippage=nonnegative_number(
            _number(state["slippage"], "fill slippage"),
            "fill slippage",
        ),
        reason=_string(state["reason"], "fill reason"),
        liquidity=_string(state["liquidity"], "fill liquidity"),
        fill_id=_optional_string(state["fill_id"], "fill_id"),
        position_id=_optional_string(state["position_id"], "position_id"),
        cumulative_quantity=_optional_positive_number(
            state["cumulative_quantity"],
            "cumulative_quantity",
        ),
        remaining_quantity=_optional_nonnegative_number(
            state["remaining_quantity"],
            "remaining_quantity",
        ),
    )


def _decode_pending_close(value: Mapping[str, Any]) -> PendingClose:
    state = _exact_mapping(value, _PENDING_CLOSE_FIELDS, "broker pending close")
    eligible_from_index = _integer(
        state["eligible_from_index"],
        "pending close eligible_from_index",
    )
    if eligible_from_index < 0:
        raise ValueError("pending close eligible_from_index cannot be negative")
    quantity = _optional_positive_number(state["quantity"], "pending close quantity")
    fraction = _optional_positive_number(state["fraction"], "pending close fraction")
    if quantity is not None and fraction is not None:
        raise ValueError("pending close quantity and fraction are mutually exclusive")
    if fraction is not None and fraction > 1:
        raise ValueError("pending close fraction must be <= 1")
    return PendingClose(
        requested_at=_datetime(state["requested_at"], "pending close requested_at"),
        eligible_from_index=eligible_from_index,
        reason=_string(state["reason"], "pending close reason"),
        source_module=_string(state["source_module"], "pending close source_module"),
        quantity=quantity,
        fraction=fraction,
    )


def _encode_mapping(value: object, name: str) -> Mapping[str, Any]:
    encoded = to_jsonable(value)
    return _mapping(encoded, name)


def _compatible_broker_state_mapping(value: object) -> Mapping[str, Any]:
    state = dict(_mapping(value, "broker state"))
    keys = set(state)
    extra = keys - _BROKER_STATE_FIELDS
    if extra:
        raise ValueError(f"broker state fields mismatch: missing=[], extra={sorted(extra)}")
    missing = _BROKER_STATE_FIELDS - keys
    unsupported_missing = missing - set(_BROKER_STATE_COMPAT_DEFAULTS)
    if unsupported_missing:
        raise ValueError(
            f"broker state fields mismatch: missing={sorted(unsupported_missing)}, extra=[]"
        )
    for key in missing:
        state[key] = _BROKER_STATE_COMPAT_DEFAULTS[key]
    return _mapping(state, "broker state")


def _compatible_config_mapping(
    value: object,
    expected_fields: frozenset[str],
    compatibility_defaults: Mapping[str, object],
    name: str,
) -> Mapping[str, Any]:
    state = dict(_mapping(value, name))
    keys = set(state)
    extra = keys - expected_fields
    if extra:
        raise ValueError(f"{name} fields mismatch: missing=[], extra={sorted(extra)}")
    missing = expected_fields - keys
    unsupported_missing = missing - set(compatibility_defaults)
    if unsupported_missing:
        raise ValueError(f"{name} fields mismatch: missing={sorted(unsupported_missing)}, extra=[]")
    for key in missing:
        state[key] = compatibility_defaults[key]
    return _mapping(state, name)


def _exact_mapping(
    value: object,
    expected_fields: frozenset[str],
    name: str,
) -> Mapping[str, Any]:
    state = _mapping(value, name)
    keys = set(state)
    if keys != expected_fields:
        missing = sorted(expected_fields - keys)
        extra = sorted(keys - expected_fields)
        raise ValueError(f"{name} fields mismatch: missing={missing}, extra={extra}")
    return state


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be str")
    frozen = deep_freeze(dict(value))
    if not isinstance(frozen, Mapping):
        raise RuntimeError(f"frozen {name} must remain a mapping")
    return cast(Mapping[str, Any], frozen)


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{name} must be a sequence")
    return value


def _identity_tuple(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(sorted(_string(value, name) for value in values))
    if len(result) != len(set(result)):
        raise ValueError(f"duplicate {name}")
    return result


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return require_nonblank(value, name)


def _blankable_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    return value


def _positive_integer(value: object, name: str) -> int:
    result = _integer(value, name)
    if result < 1:
        raise ValueError(f"{name} must be positive")
    return result


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    return finite_number(value, name)


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _optional_positive_number(value: object, name: str) -> float | None:
    return None if value is None else positive_number(_number(value, name), name)


def _optional_nonnegative_number(value: object, name: str) -> float | None:
    return None if value is None else nonnegative_number(_number(value, name), name)


def _datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime") from exc
    return utc_datetime(parsed, name)


def _optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _datetime(value, name)


def _deterministic_numeric_id(value: str, prefix: str, name: str) -> int:
    if not value.startswith(prefix):
        raise ValueError(f"{name} has unexpected prefix")
    suffix = value.removeprefix(prefix)
    if not suffix.isdigit():
        raise ValueError(f"{name} has malformed deterministic sequence")
    return int(suffix)
