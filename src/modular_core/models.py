from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, cast

from .serialization import deep_freeze
from .validation import (
    finite_number,
    nonnegative_number,
    positive_number,
    require_nonblank,
    utc_datetime,
)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"  # v0.2 compatibility; behaves as stop-market
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT_MARKET = "take_profit_market"
    TAKE_PROFIT_LIMIT = "take_profit_limit"

    @property
    def requires_limit_price(self) -> bool:
        return self in {
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_LIMIT,
        }

    @property
    def requires_trigger_price(self) -> bool:
        return self in {
            OrderType.STOP,
            OrderType.STOP_MARKET,
            OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_MARKET,
            OrderType.TAKE_PROFIT_LIMIT,
        }


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTD = "gtd"


class OrderStatus(str, Enum):
    NEW = "new"
    PENDING = "pending"  # compatibility state used by existing adapter
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class SetupStatus(str, Enum):
    CREATED = "created"
    ARMED = "armed"
    CONFIRMED = "confirmed"
    ORDERED = "ordered"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CLOSED = "closed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ReasonCode(str, Enum):
    ACTIVE_ORDER_EXISTS = "active_order_exists"
    POSITION_EXISTS = "position_exists"
    DUPLICATE_CLIENT_ORDER_ID = "duplicate_client_order_id"
    INVALID_BRACKET = "invalid_bracket"
    RISK_REJECTED = "risk_rejected"
    INSTRUMENT_RULE_REJECTED = "instrument_rule_rejected"
    ORDER_EXPIRED = "order_expired"
    CANCELLED_BY_MODULE = "cancelled_by_module"
    CANCEL_SELECTOR_NOT_FOUND = "cancel_selector_not_found"
    END_OF_DATA = "end_of_data"
    POSITION_NOT_FOUND = "position_not_found"
    CLOSE_ALREADY_PENDING = "close_already_pending"
    INVALID_POSITION_UPDATE = "invalid_position_update"
    MODULE_NOT_READY = "module_not_ready"
    FEATURE_CONTRACT_VIOLATION = "feature_contract_violation"
    SETUP_TRANSITION_REJECTED = "setup_transition_rejected"
    POST_ONLY_REJECTED = "post_only_rejected"


_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdw])$")


@dataclass(frozen=True, slots=True)
class Candle:
    """A completed OHLCV bar whose timestamp is the bar open time in UTC.

    ``close_time`` is derived from ``timeframe`` where possible. All engine
    decisions are still made only after the bar has completed, so retaining an
    open-time timestamp does not expose future information.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    symbol: str = "UNKNOWN"
    timeframe: str = "base"

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp, "Candle timestamp"))
        for name in ("open", "high", "low", "close"):
            positive_number(getattr(self, name), f"Candle {name}")
        nonnegative_number(self.volume, "Candle volume")
        require_nonblank(self.symbol, "Candle symbol")
        require_nonblank(self.timeframe, "Candle timeframe")
        timeframe_match = _TIMEFRAME_RE.match(self.timeframe.strip().lower())
        if timeframe_match and int(timeframe_match.group(1)) <= 0:
            raise ValueError("Candle timeframe amount must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is inconsistent with OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is inconsistent with OHLC")

    @property
    def open_time(self) -> datetime:
        return self.timestamp

    @property
    def close_time(self) -> datetime:
        match = _TIMEFRAME_RE.match(self.timeframe.strip().lower())
        if not match:
            return self.timestamp
        amount = int(match.group(1))
        seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        return self.timestamp + timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class Signal:
    module: str
    side: Side
    reason: str
    strength: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_nonblank(self.module, "signal module")
        if not isinstance(self.side, Side):
            raise TypeError("signal side must be Side")
        require_nonblank(self.reason, "signal reason")
        finite_number(self.strength, "signal strength")
        if not 0 <= self.strength <= 1:
            raise ValueError("signal strength must be in [0, 1]")
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, "signal metadata"),
        )


@dataclass(frozen=True, slots=True)
class SetupEvent:
    module: str
    setup_id: str
    status: SetupStatus
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    symbol: str | None = None
    side: Side | None = None
    parent_setup_id: str | None = None

    def __post_init__(self) -> None:
        require_nonblank(self.module, "setup module")
        require_nonblank(self.setup_id, "setup_id")
        if not isinstance(self.status, SetupStatus):
            raise TypeError("setup status must be SetupStatus")
        if not isinstance(self.reason, str):
            raise TypeError("setup reason must be str")
        if self.symbol is not None:
            require_nonblank(self.symbol, "setup symbol")
        if self.side is not None and not isinstance(self.side, Side):
            raise TypeError("setup side must be Side or None")
        if self.parent_setup_id is not None:
            require_nonblank(self.parent_setup_id, "parent_setup_id")
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, "setup metadata"),
        )


@dataclass(frozen=True, slots=True)
class ClosePositionRequest:
    reason: str = "module_close"
    quantity: float | None = None
    fraction: float | None = None

    def __post_init__(self) -> None:
        require_nonblank(self.reason, "close reason")
        if self.quantity is not None:
            positive_number(self.quantity, "close quantity")
        if self.fraction is not None:
            finite_number(self.fraction, "close fraction")
            if not 0 < self.fraction <= 1:
                raise ValueError("close fraction must be in (0, 1]")
        if self.quantity is not None and self.fraction is not None:
            raise ValueError("close quantity and fraction are mutually exclusive")


@dataclass(frozen=True, slots=True)
class PositionUpdateRequest:
    stop_loss: float | None
    take_profit: float | None
    reason: str = "module_update"

    def __post_init__(self) -> None:
        if self.stop_loss is not None:
            positive_number(self.stop_loss, "stop_loss")
        if self.take_profit is not None:
            positive_number(self.take_profit, "take_profit")
        require_nonblank(self.reason, "update reason")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    side: Side
    order_type: OrderType
    quantity: float | None = None
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_fraction: float | None = None
    tag: str = ""
    setup_id: str | None = None
    client_order_id: str | None = None
    ttl_bars: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    good_till_time: datetime | None = None
    close_position: bool = False
    trigger_price: float | None = None
    limit_price: float | None = None
    position_side: PositionSide | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError("order side must be Side")
        if not isinstance(self.order_type, OrderType):
            raise TypeError("order_type must be OrderType")
        if not isinstance(self.time_in_force, TimeInForce):
            raise TypeError("time_in_force must be TimeInForce")
        if self.position_side is not None and not isinstance(self.position_side, PositionSide):
            raise TypeError("position_side must be PositionSide or None")
        for name in ("post_only", "reduce_only", "close_position"):
            _require_bool(getattr(self, name), name)
        if not isinstance(self.tag, str):
            raise TypeError("tag must be str")
        if self.quantity is not None:
            positive_number(self.quantity, "quantity")
        if self.quantity is not None and self.risk_fraction is not None:
            raise ValueError("quantity and risk_fraction are mutually exclusive")
        for name, value in (
            ("price", self.price),
            ("limit_price", self.limit_price),
            ("trigger_price", self.trigger_price),
            ("stop_loss", self.stop_loss),
            ("take_profit", self.take_profit),
        ):
            if value is not None:
                positive_number(value, name)
        if (
            self.limit_price is not None
            and self.price is not None
            and self.limit_price != self.price
        ):
            raise ValueError("price and limit_price cannot disagree")
        triggered_market_types = {
            OrderType.STOP,
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_MARKET,
        }
        if (
            self.order_type in triggered_market_types
            and self.trigger_price is not None
            and self.price is not None
            and self.trigger_price != self.price
        ):
            raise ValueError("price and trigger_price cannot disagree")
        if self.order_type is OrderType.MARKET and any(
            value is not None for value in (self.price, self.limit_price, self.trigger_price)
        ):
            raise ValueError("market order cannot define execution prices")
        if self.order_type is OrderType.LIMIT and self.trigger_price is not None:
            raise ValueError("limit order cannot define trigger_price")
        if self.order_type in triggered_market_types and self.limit_price is not None:
            raise ValueError("triggered market order cannot define limit_price")
        if self.order_type is OrderType.LIMIT and self.effective_limit_price is None:
            raise ValueError("limit order requires price or limit_price")
        if self.order_type is OrderType.STOP and self.effective_trigger_price is None:
            raise ValueError("stop order requires price or trigger_price")
        if self.order_type.requires_limit_price and self.order_type is not OrderType.LIMIT:
            if self.effective_limit_price is None:
                raise ValueError(f"{self.order_type.value} requires limit_price")
        if self.order_type.requires_trigger_price and self.order_type is not OrderType.STOP:
            if self.effective_trigger_price is None:
                raise ValueError(f"{self.order_type.value} requires trigger_price")
        if self.post_only and self.order_type not in {
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_LIMIT,
        }:
            raise ValueError("post_only is valid only for limit order types")
        if self.time_in_force is TimeInForce.GTD:
            if self.good_till_time is None:
                raise ValueError("GTD order requires good_till_time")
        elif self.good_till_time is not None:
            raise ValueError("good_till_time is valid only for GTD orders")
        if self.good_till_time is not None:
            object.__setattr__(
                self,
                "good_till_time",
                utc_datetime(self.good_till_time, "good_till_time"),
            )
        if self.risk_fraction is not None:
            finite_number(self.risk_fraction, "risk_fraction")
            if not 0 < self.risk_fraction <= 1:
                raise ValueError("risk_fraction must be in (0, 1]")
        if self.ttl_bars is not None:
            if not isinstance(self.ttl_bars, int) or isinstance(self.ttl_bars, bool):
                raise TypeError("ttl_bars must be int or None")
            if self.ttl_bars < 1:
                raise ValueError("ttl_bars must be >= 1")
        if self.setup_id is not None:
            require_nonblank(self.setup_id, "setup_id")
        if self.client_order_id is not None:
            require_nonblank(self.client_order_id, "client_order_id")
        if self.close_position and not self.reduce_only:
            raise ValueError("close_position requires reduce_only")
        if self.close_position and self.quantity is not None:
            raise ValueError("close_position cannot define quantity")
        if self.reduce_only and self.risk_fraction is not None:
            raise ValueError("reduce_only cannot define risk_fraction")
        if self.reduce_only and (self.stop_loss is not None or self.take_profit is not None):
            raise ValueError("reduce_only cannot define bracket exits")
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, "order metadata"),
        )

    @property
    def effective_limit_price(self) -> float | None:
        if self.limit_price is not None:
            return self.limit_price
        if self.order_type in {
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
            OrderType.TAKE_PROFIT_LIMIT,
        }:
            return self.price
        return None

    @property
    def effective_trigger_price(self) -> float | None:
        if self.trigger_price is not None:
            return self.trigger_price
        if self.order_type in {
            OrderType.STOP,
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_MARKET,
        }:
            return self.price
        return None

    def replace_prices(
        self,
        *,
        price: float | None,
        trigger_price: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        quantity: float | None,
    ) -> "OrderRequest":
        return replace(
            self,
            price=price,
            limit_price=(price if self.limit_price is not None else None),
            trigger_price=trigger_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=quantity,
        )


@dataclass(frozen=True, slots=True)
class CancelRequest:
    order_id: str | None = None
    client_order_id: str | None = None
    tag: str | None = None
    reason: str = ReasonCode.CANCELLED_BY_MODULE.value

    def __post_init__(self) -> None:
        selectors = [self.order_id, self.client_order_id, self.tag]
        if sum(value is not None for value in selectors) != 1:
            raise ValueError("exactly one cancel selector is required")
        selected = next(value for value in selectors if value is not None)
        require_nonblank(selected, "cancel selector")
        require_nonblank(self.reason, "cancel reason")


@dataclass(slots=True)
class Order:
    id: str
    sequence: int
    source_module: str
    created_at: datetime
    eligible_from_index: int
    expires_after_index: int | None
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_fraction: float | None
    quantity_is_explicit: bool
    tag: str = ""
    setup_id: str | None = None
    client_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    good_till_time: datetime | None = None
    close_position: bool = False
    trigger_price: float | None = None
    limit_price: float | None = None
    position_side: PositionSide | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    fees_paid: float = 0.0
    triggered_at: datetime | None = None
    triggered_index: int | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def effective_limit_price(self) -> float | None:
        return (
            self.limit_price
            if self.limit_price is not None
            else (
                self.price
                if self.order_type
                in {OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT}
                else None
            )
        )

    @property
    def effective_trigger_price(self) -> float | None:
        return (
            self.trigger_price
            if self.trigger_price is not None
            else (
                self.price
                if self.order_type
                in {OrderType.STOP, OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET}
                else None
            )
        )


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    id: str
    source_module: str
    created_at: datetime
    side: Side
    order_type: OrderType
    quantity: float
    price: float | None
    stop_loss: float | None
    take_profit: float | None
    tag: str
    setup_id: str | None
    client_order_id: str | None
    status: OrderStatus
    filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    average_fill_price: float | None = None
    fees_paid: float = 0.0
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    good_till_time: datetime | None = None
    trigger_price: float | None = None
    limit_price: float | None = None
    triggered_at: datetime | None = None
    triggered_index: int | None = None

    @classmethod
    def from_order(cls, order: Order) -> "OrderSnapshot":
        return cls(
            id=order.id,
            source_module=order.source_module,
            created_at=order.created_at,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            tag=order.tag,
            setup_id=order.setup_id,
            client_order_id=order.client_order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            average_fill_price=order.average_fill_price,
            fees_paid=order.fees_paid,
            time_in_force=order.time_in_force,
            post_only=order.post_only,
            reduce_only=order.reduce_only,
            good_till_time=order.good_till_time,
            trigger_price=order.trigger_price,
            limit_price=order.limit_price,
            triggered_at=order.triggered_at,
            triggered_index=order.triggered_index,
        )


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    timestamp: datetime
    side: Side
    quantity: float
    price: float
    fee: float
    slippage: float
    reason: str
    liquidity: str
    fill_id: str | None = None
    position_id: str | None = None
    cumulative_quantity: float | None = None
    remaining_quantity: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp, "fill timestamp"))
        require_nonblank(self.order_id, "fill order_id")
        require_nonblank(self.reason, "fill reason")
        require_nonblank(self.liquidity, "fill liquidity")
        positive_number(self.quantity, "fill quantity")
        positive_number(self.price, "fill price")
        nonnegative_number(self.fee, "fill fee")
        nonnegative_number(self.slippage, "fill slippage")
        if self.cumulative_quantity is not None:
            positive_number(self.cumulative_quantity, "fill cumulative_quantity")
        if self.remaining_quantity is not None:
            nonnegative_number(self.remaining_quantity, "fill remaining_quantity")


@dataclass(slots=True)
class Position:
    id: str
    entry_order_id: str
    side: PositionSide
    quantity: float
    entry_price: float
    entry_time: datetime
    entry_index: int
    entry_fee: float
    initial_risk_amount: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    tag: str = ""
    setup_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    realised_pnl: float = 0.0
    exit_fees: float = 0.0
    entry_fills: list[Fill] = field(default_factory=list)
    exit_fills: list[Fill] = field(default_factory=list)

    @property
    def direction(self) -> int:
        return 1 if self.side is PositionSide.LONG else -1

    def add_entry_fill(self, fill: Fill) -> None:
        old_notional = self.entry_price * self.quantity
        new_quantity = self.quantity + fill.quantity
        self.entry_price = (old_notional + fill.price * fill.quantity) / new_quantity
        self.quantity = new_quantity
        self.entry_fee += fill.fee
        self.entry_fills.append(fill)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    id: str
    side: PositionSide
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: float | None
    take_profit: float | None
    tag: str
    unrealized_pnl: float
    setup_id: str | None = None
    realised_pnl: float = 0.0
    entry_fill_count: int = 0
    exit_fill_count: int = 0
    entry_index: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_nonblank(self.id, "position snapshot id")
        if not isinstance(self.side, PositionSide):
            raise TypeError("position snapshot side must be PositionSide")
        positive_number(self.quantity, "position snapshot quantity")
        positive_number(self.entry_price, "position snapshot entry_price")
        object.__setattr__(
            self,
            "entry_time",
            utc_datetime(self.entry_time, "position snapshot entry_time"),
        )
        if self.stop_loss is not None:
            positive_number(self.stop_loss, "position snapshot stop_loss")
        if self.take_profit is not None:
            positive_number(self.take_profit, "position snapshot take_profit")
        if not isinstance(self.tag, str):
            raise TypeError("position snapshot tag must be str")
        if self.setup_id is not None:
            require_nonblank(self.setup_id, "position snapshot setup_id")
        finite_number(self.unrealized_pnl, "position snapshot unrealized_pnl")
        finite_number(self.realised_pnl, "position snapshot realised_pnl")
        if self.entry_index is not None:
            if isinstance(self.entry_index, bool) or not isinstance(self.entry_index, int):
                raise TypeError("position snapshot entry_index must be int or None")
            if self.entry_index < 0:
                raise ValueError("position snapshot entry_index cannot be negative")
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, "position snapshot metadata"),
        )
        for name in ("entry_fill_count", "exit_fill_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"position snapshot {name} must be int")
            if value < 0:
                raise ValueError(f"position snapshot {name} cannot be negative")


@dataclass(frozen=True, slots=True)
class Trade:
    side: PositionSide
    quantity: float
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    net_pnl: float
    exit_reason: str
    tag: str = ""
    initial_risk_amount: float | None = None
    r_multiple: float | None = None
    holding_bars: int | None = None
    setup_id: str | None = None
    position_id: str | None = None
    entry_fill_count: int = 1
    exit_fill_count: int = 1
    position_closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_time", utc_datetime(self.entry_time, "trade entry_time"))
        object.__setattr__(self, "exit_time", utc_datetime(self.exit_time, "trade exit_time"))
        if self.exit_time < self.entry_time:
            raise ValueError("trade exit_time cannot precede entry_time")
        positive_number(self.quantity, "trade quantity")
        positive_number(self.entry_price, "trade entry_price")
        positive_number(self.exit_price, "trade exit_price")
        finite_number(self.gross_pnl, "trade gross_pnl")
        nonnegative_number(self.fees, "trade fees")
        finite_number(self.net_pnl, "trade net_pnl")
        require_nonblank(self.exit_reason, "trade exit_reason")
        if self.initial_risk_amount is not None:
            nonnegative_number(self.initial_risk_amount, "trade initial_risk_amount")
        if self.r_multiple is not None:
            finite_number(self.r_multiple, "trade r_multiple")
        if self.holding_bars is not None and self.holding_bars < 0:
            raise ValueError("trade holding_bars cannot be negative")
        if self.entry_fill_count < 1 or self.exit_fill_count < 1:
            raise ValueError("trade fill counts must be positive")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    cash: float
    unrealized_pnl: float
    equity: float
    position_open: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp, "equity timestamp"))
        finite_number(self.cash, "equity cash")
        finite_number(self.unrealized_pnl, "equity unrealized_pnl")
        finite_number(self.equity, "equity value")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    timestamp: datetime
    event: str
    order: OrderSnapshot
    reason: str | None = None
    event_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


def _freeze_metadata(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    frozen = deep_freeze(value)
    if not isinstance(frozen, Mapping):
        raise RuntimeError(f"frozen {name} must remain a mapping")
    return cast(Mapping[str, Any], frozen)


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
