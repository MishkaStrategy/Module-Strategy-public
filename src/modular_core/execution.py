from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import uuid4

from .adapters import ExecutionAdapter
from .audit import Journal
from .config import EngineConfig
from .instruments import ExchangeRulesAdapter, InstrumentRuleError, InstrumentSpec
from .models import (
    CancelRequest,
    Candle,
    ClosePositionRequest,
    Fill,
    Order,
    OrderEvent,
    OrderRequest,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    PositionSnapshot,
    PositionUpdateRequest,
    ReasonCode,
    Side,
    TimeInForce,
    Trade,
)
from .risk import FixedFractionalSizer, RiskError


@dataclass(slots=True)
class PendingClose:
    requested_at: datetime
    eligible_from_index: int
    reason: str
    source_module: str
    quantity: float | None = None
    fraction: float | None = None


@dataclass(slots=True)
class Portfolio:
    cash: float
    position: Position | None = None
    contract_size: float = 1.0

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position is None:
            return 0.0
        return (
            (mark_price - self.position.entry_price)
            * self.position.quantity
            * self.position.direction
            * self.contract_size
        )

    def equity(self, mark_price: float) -> float:
        return self.cash + self.unrealized_pnl(mark_price)

    def position_snapshot(self, mark_price: float) -> PositionSnapshot | None:
        p = self.position
        if p is None:
            return None
        return PositionSnapshot(
            id=p.id,
            side=p.side,
            quantity=p.quantity,
            entry_price=p.entry_price,
            entry_time=p.entry_time,
            stop_loss=p.stop_loss,
            take_profit=p.take_profit,
            tag=p.tag,
            unrealized_pnl=self.unrealized_pnl(mark_price),
            setup_id=p.setup_id,
            realised_pnl=p.realised_pnl,
            entry_fill_count=len(p.entry_fills),
            exit_fill_count=len(p.exit_fills),
            entry_index=p.entry_index,
            metadata=p.metadata,
        )


class BrokerSimulator(ExecutionAdapter):
    """Deterministic OHLC execution adapter with a fill-based position ledger."""

    def __init__(
        self,
        config: EngineConfig,
        journal: Journal,
        instrument: InstrumentSpec | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.portfolio = Portfolio(
            cash=config.initial_cash,
            contract_size=(instrument.contract_size if instrument is not None else 1.0),
        )
        self._historical_closed_equity = config.initial_cash
        self.orders: list[Order] = []
        self._pending: dict[str, Order] = {}
        self._used_client_order_ids: set[str] = set()
        self.sizer = FixedFractionalSizer(config.risk)
        self._order_sequence = 0
        self._position_sequence = 0
        self._fill_sequence = 0
        self.pending_close: PendingClose | None = None
        self._last_processed_bar: Candle | None = None
        self._last_processed_index: int | None = None
        self._core_closed_fence_validation = False
        self._core_quarantine_validation = False
        self.instrument = instrument or InstrumentSpec(
            symbol="*",
            quantity_step=config.risk.quantity_step,
            min_quantity=config.risk.min_quantity,
            min_notional=config.risk.min_notional,
        )
        self.rules = ExchangeRulesAdapter(self.instrument)
        self.portfolio.contract_size = self.instrument.contract_size

    @property
    def pending_orders(self) -> list[Order]:
        return list(self._pending.values())

    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]:
        return tuple(OrderSnapshot.from_order(order) for order in self.pending_orders)

    def submit(
        self,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> Order | None:
        if self.pending_orders and (
            self.config.one_active_order or self._core_closed_fence_validation
        ) and not self._closed_fence_pair_is_allowed(request):
            self.journal.log_skip(created_at, source_module, ReasonCode.ACTIVE_ORDER_EXISTS.value)
            return None
        if (
            self.config.one_position
            and self.portfolio.position is not None
            and not request.reduce_only
        ):
            position = self.portfolio.position
            same_direction = (position.side is PositionSide.LONG and request.side is Side.BUY) or (
                position.side is PositionSide.SHORT and request.side is Side.SELL
            )
            if not (self.config.allow_scale_in and same_direction):
                self.journal.log_skip(created_at, source_module, ReasonCode.POSITION_EXISTS.value)
                return None
        if request.client_order_id and request.client_order_id in self._used_client_order_ids:
            self.journal.log_skip(
                created_at, source_module, ReasonCode.DUPLICATE_CLIENT_ORDER_ID.value
            )
            return None

        try:
            normalized = self.rules.normalize_order(request, reference_price=mark_price)
        except InstrumentRuleError as exc:
            self.journal.log_error(created_at, f"instrument:{source_module}", exc)
            reason = (
                ReasonCode.POST_ONLY_REJECTED.value
                if "post-only" in str(exc)
                else ReasonCode.INSTRUMENT_RULE_REJECTED.value
            )
            self.journal.log_skip(created_at, source_module, reason)
            return None

        if normalized.good_till_time is not None and normalized.good_till_time <= created_at:
            self.journal.log_skip(created_at, source_module, ReasonCode.ORDER_EXPIRED.value)
            return None

        reference_price = (
            normalized.effective_limit_price or normalized.effective_trigger_price or mark_price
        )
        if not normalized.reduce_only and not self._valid_bracket(normalized, reference_price):
            self.journal.log_skip(created_at, source_module, ReasonCode.INVALID_BRACKET.value)
            return None

        if normalized.reduce_only:
            if self._core_closed_fence_validation and self.pending_close is not None:
                self.journal.log_skip(
                    created_at, source_module, ReasonCode.CLOSE_ALREADY_PENDING.value
                )
                return None
            current_position = self.portfolio.position
            if current_position is None:
                self.journal.log_skip(
                    created_at, source_module, ReasonCode.POSITION_NOT_FOUND.value
                )
                return None
            if (
                normalized.position_side is not None
                and normalized.position_side is not current_position.side
            ):
                self.journal.log_skip(
                    created_at, source_module, ReasonCode.INVALID_POSITION_UPDATE.value
                )
                return None
            expected_side = Side.SELL if current_position.side is PositionSide.LONG else Side.BUY
            if normalized.side is not expected_side:
                self.journal.log_skip(
                    created_at, source_module, ReasonCode.INVALID_POSITION_UPDATE.value
                )
                return None
            quantity = (
                current_position.quantity
                if normalized.close_position or normalized.quantity is None
                else normalized.quantity
            )
            quantity = min(quantity, current_position.quantity)
        else:
            expected_position_side = (
                PositionSide.LONG if normalized.side is Side.BUY else PositionSide.SHORT
            )
            if (
                normalized.position_side is not None
                and normalized.position_side is not expected_position_side
            ):
                self.journal.log_skip(
                    created_at, source_module, ReasonCode.INVALID_POSITION_UPDATE.value
                )
                return None
            try:
                quantity = self._size_order(
                    normalized,
                    equity=self.portfolio.equity(mark_price),
                    reference_price=reference_price,
                )
                quantity = self.rules.normalize_quantity(quantity, price=reference_price)
            except (RiskError, InstrumentRuleError) as exc:
                self.journal.log_error(created_at, f"risk:{source_module}", exc)
                self.journal.log_skip(created_at, source_module, ReasonCode.RISK_REJECTED.value)
                return None

        next_sequence = self._order_sequence + 1
        order_id = (
            f"ord-{next_sequence:08d}" if self.config.deterministic_order_ids else uuid4().hex
        )
        order = Order(
            id=order_id,
            sequence=next_sequence,
            source_module=source_module,
            created_at=created_at,
            eligible_from_index=current_index + 1,
            expires_after_index=(
                current_index + normalized.ttl_bars if normalized.ttl_bars is not None else None
            ),
            side=normalized.side,
            order_type=normalized.order_type,
            quantity=quantity,
            price=normalized.price,
            stop_loss=normalized.stop_loss,
            take_profit=normalized.take_profit,
            risk_fraction=normalized.risk_fraction,
            quantity_is_explicit=normalized.quantity is not None,
            tag=normalized.tag,
            setup_id=normalized.setup_id,
            client_order_id=normalized.client_order_id,
            metadata=dict(normalized.metadata),
            time_in_force=normalized.time_in_force,
            post_only=normalized.post_only,
            reduce_only=normalized.reduce_only,
            good_till_time=normalized.good_till_time,
            close_position=normalized.close_position,
            trigger_price=normalized.trigger_price,
            limit_price=normalized.limit_price,
            position_side=normalized.position_side,
        )
        self._log_order_event(created_at, "submitted", order)
        self._order_sequence = next_sequence
        self.orders.append(order)
        self._pending[order.id] = order
        if order.client_order_id is not None:
            self._used_client_order_ids.add(order.client_order_id)
        return order

    def cancel(self, request: CancelRequest, *, timestamp: datetime, source_module: str) -> int:
        matched = 0
        for order in self.pending_orders:
            if request.order_id is not None and order.id != request.order_id:
                continue
            if (
                request.client_order_id is not None
                and order.client_order_id != request.client_order_id
            ):
                continue
            if request.tag is not None and order.tag != request.tag:
                continue
            cancel_pending = replace(order, status=OrderStatus.CANCEL_PENDING)
            self._log_order_event(timestamp, "cancel_pending", cancel_pending, request.reason)
            order.status = OrderStatus.CANCEL_PENDING
            cancelled = replace(order, status=OrderStatus.CANCELLED)
            self._log_order_event(timestamp, "cancelled", cancelled, request.reason)
            order.status = OrderStatus.CANCELLED
            self._pending.pop(order.id, None)
            matched += 1
        if matched == 0:
            self.journal.log_skip(
                timestamp, source_module, ReasonCode.CANCEL_SELECTOR_NOT_FOUND.value
            )
        return matched

    def update_position(
        self,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool:
        position = self.portfolio.position
        if position is None:
            self.journal.log_skip(timestamp, source_module, ReasonCode.POSITION_NOT_FOUND.value)
            return False
        if position.side is PositionSide.LONG:
            valid = (request.stop_loss is None or request.stop_loss < mark_price) and (
                request.take_profit is None or request.take_profit > mark_price
            )
        else:
            valid = (request.stop_loss is None or request.stop_loss > mark_price) and (
                request.take_profit is None or request.take_profit < mark_price
            )
        if not valid:
            self.journal.log_skip(
                timestamp, source_module, ReasonCode.INVALID_POSITION_UPDATE.value
            )
            return False
        try:
            side = Side.BUY if position.side is PositionSide.LONG else Side.SELL
            normalized_stop = (
                self.rules.normalize_stop_price(
                    request.stop_loss, entry_price=mark_price, side=side
                )
                if request.stop_loss is not None
                else None
            )
            normalized_target = (
                self.rules.normalize_target_price(
                    request.take_profit, entry_price=mark_price, side=side
                )
                if request.take_profit is not None
                else None
            )
        except InstrumentRuleError:
            self.journal.log_skip(
                timestamp, source_module, ReasonCode.INVALID_POSITION_UPDATE.value
            )
            return False
        self.journal.log_diagnostics(
            timestamp,
            source_module,
            {
                "event": "position_updated",
                "reason": request.reason,
                "stop_loss": normalized_stop,
                "take_profit": normalized_target,
            },
        )
        position.stop_loss = normalized_stop
        position.take_profit = normalized_target
        return True

    def schedule_close(
        self,
        request: ClosePositionRequest,
        *,
        timestamp: datetime,
        current_index: int,
        source_module: str,
    ) -> bool:
        position = self.portfolio.position
        if position is None:
            self.journal.log_skip(timestamp, source_module, ReasonCode.POSITION_NOT_FOUND.value)
            return False
        if self.pending_close is not None:
            self.journal.log_skip(timestamp, source_module, ReasonCode.CLOSE_ALREADY_PENDING.value)
            return False
        if self.config.execution.close_request_timing == "current_close":
            bar = self._last_processed_bar
            if (
                bar is None
                or self._last_processed_index != current_index
                or bar.timestamp != timestamp
            ):
                raise RuntimeError("current-close request does not match the last processed bar")
            quantity = request.quantity or (
                position.quantity * request.fraction
                if request.fraction is not None
                else position.quantity
            )
            reference_price = bar.close
            price = self._market_exit_price(position, reference_price)
            trade = self.close_position(
                timestamp=timestamp,
                price=price,
                reason=request.reason,
                index=current_index,
                liquidity="taker",
                reference_price=reference_price,
                quantity=quantity,
            )
            if trade is None:
                return False
            self.journal.log_diagnostics(
                timestamp,
                source_module,
                {
                    "event": "position_close_executed",
                    "reason": request.reason,
                    "timing": "current_close",
                },
            )
            return True
        pending_close = PendingClose(
            requested_at=timestamp,
            eligible_from_index=current_index + 1,
            reason=request.reason,
            source_module=source_module,
            quantity=request.quantity,
            fraction=request.fraction,
        )
        self.journal.log_diagnostics(
            timestamp,
            source_module,
            {"event": "position_close_scheduled", "reason": request.reason},
        )
        self.pending_close = pending_close
        return True

    def process_bar(self, bar: Candle, index: int) -> None:
        self._process_bar(bar, index, closed_risk_fence=False)

    def _process_bar_with_closed_risk_fence(self, bar: Candle, index: int) -> None:
        """Core-internal closed-fence bar path; never evaluate pending entries."""
        if not self._core_closed_fence_validation:
            raise RuntimeError("closed-fence processing requires Core validation context")
        self._process_bar(bar, index, closed_risk_fence=True)

    def _process_quarantine_position_exit(self, bar: Candle, index: int) -> None:
        """Apply only the bound position's own SL/TP; preserve all pending context."""
        if not self._core_quarantine_validation:
            raise RuntimeError("quarantine processing requires Core validation context")
        position = self.portfolio.position
        if position is None or index == position.entry_index:
            raise ValueError("quarantine allows only a triggered position-level exit")
        if position.side is PositionSide.LONG:
            stop_hit = position.stop_loss is not None and bar.low <= position.stop_loss
            target_hit = position.take_profit is not None and bar.high >= position.take_profit
        else:
            stop_hit = position.stop_loss is not None and bar.high >= position.stop_loss
            target_hit = position.take_profit is not None and bar.low <= position.take_profit
        if not stop_hit and not target_hit:
            raise ValueError("quarantine allows only a triggered position-level exit")

        reason = (
            self.config.execution.intrabar_priority
            if stop_hit and target_hit
            else ("stop" if stop_hit else "target")
        )
        if reason == "stop":
            if position.stop_loss is None:
                raise RuntimeError("stop exit selected without a stop-loss price")
            if position.side is PositionSide.LONG:
                base, side_sign = min(position.stop_loss, bar.open), Side.SELL.sign
            else:
                base, side_sign = max(position.stop_loss, bar.open), Side.BUY.sign
            slip = self.config.execution.stop_slippage_bps / 10_000
            price = base * (1 + slip * side_sign)
            liquidity, reference_price = "taker", position.stop_loss
        else:
            if position.take_profit is None:
                raise RuntimeError("target exit selected without a take-profit price")
            price = position.take_profit
            liquidity, reference_price = (
                self.config.execution.target_liquidity,
                position.take_profit,
            )
        self._last_processed_bar = bar
        self._last_processed_index = index
        self.close_position(
            timestamp=bar.timestamp,
            price=price,
            reason=reason,
            index=index,
            liquidity=liquidity,
            reference_price=reference_price,
            _preserve_pending_context=True,
        )

    def _closed_fence_pair_is_allowed(self, request: OrderRequest) -> bool:
        if not self._core_closed_fence_validation or not request.reduce_only:
            return False
        if self.pending_close is not None or self.portfolio.position is None:
            return False
        pending = self.pending_orders
        if len(pending) != 1:
            return False
        entry = pending[0]
        position = self.portfolio.position
        expected_entry_side = Side.BUY if position.side is PositionSide.LONG else Side.SELL
        if (
            entry.reduce_only
            or entry.close_position
            or entry.id != position.entry_order_id
            or entry.filled_quantity <= 0
            or entry.side is not expected_entry_side
            or entry.filled_quantity + 1e-12 < position.quantity
        ):
            return False
        expected_reduce_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        return (
            request.side is expected_reduce_side
            and (request.position_side is None or request.position_side is position.side)
        )

    def _process_bar(self, bar: Candle, index: int, *, closed_risk_fence: bool) -> None:
        self._last_processed_bar = bar
        self._last_processed_index = index
        position_at_start = self.portfolio.position is not None
        self._expire_orders(
            bar.timestamp,
            index,
            preserve_non_protective=closed_risk_fence,
        )
        self._process_position_exit(bar, index)
        self._process_pending_close(bar, index)
        if closed_risk_fence:
            candidates = [
                order for order in self.pending_orders if order.reduce_only or order.close_position
            ]
        elif self.portfolio.position is not None and self.config.one_position:
            position = self.portfolio.position
            candidates = [
                order
                for order in self.pending_orders
                if order.reduce_only
                or order.filled_quantity > 0
                or (
                    self.config.allow_scale_in
                    and (
                        (position.side is PositionSide.LONG and order.side is Side.BUY)
                        or (position.side is PositionSide.SHORT and order.side is Side.SELL)
                    )
                )
            ]
        elif position_at_start and not self.config.execution.allow_reentry_on_exit_bar:
            candidates = [order for order in self.pending_orders if order.reduce_only]
        else:
            candidates = list(self.pending_orders)

        for order in candidates:
            if index < order.eligible_from_index:
                continue
            details = self._fill_details(order, bar, index)
            if details is None:
                if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                    order.status = OrderStatus.CANCELLED
                    self._pending.pop(order.id, None)
                    self._log_order_event(bar.timestamp, "cancelled", order, "time_in_force")
                continue
            fill_price, reference_price, liquidity = details
            if not order.reduce_only and order.filled_quantity == 0:
                if not self._valid_bracket_for_order(order, fill_price):
                    order.status = OrderStatus.REJECTED
                    self._pending.pop(order.id, None)
                    self._log_order_event(
                        bar.timestamp,
                        "rejected",
                        order,
                        ReasonCode.INVALID_BRACKET.value,
                    )
                    continue
                if not self._resize_at_actual_fill(order, fill_price, bar.open):
                    order.status = OrderStatus.REJECTED
                    self._pending.pop(order.id, None)
                    self._log_order_event(
                        bar.timestamp, "rejected", order, ReasonCode.RISK_REJECTED.value
                    )
                    continue
            fill_quantity = self._fill_quantity(order)
            if order.time_in_force is TimeInForce.FOK and fill_quantity < order.remaining_quantity:
                order.status = OrderStatus.REJECTED
                self._pending.pop(order.id, None)
                self._log_order_event(bar.timestamp, "rejected", order, "fok_not_fillable")
                continue
            if fill_quantity <= 0:
                continue
            if order.reduce_only:
                self._apply_reduce_fill(
                    order,
                    timestamp=bar.timestamp,
                    index=index,
                    price=fill_price,
                    reference_price=reference_price,
                    liquidity=liquidity,
                    quantity=fill_quantity,
                )
            else:
                self._apply_entry_fill(
                    order,
                    timestamp=bar.timestamp,
                    index=index,
                    price=fill_price,
                    reference_price=reference_price,
                    liquidity=liquidity,
                    quantity=fill_quantity,
                )
            if order.time_in_force is TimeInForce.IOC and order.remaining_quantity > 0:
                order.status = OrderStatus.CANCELLED
                self._pending.pop(order.id, None)
                self._log_order_event(bar.timestamp, "cancelled", order, "ioc_remainder")
            if (
                self.portfolio.position is not None
                and not order.reduce_only
                and order.order_type is OrderType.LIMIT
                and self.config.execution.allow_limit_stop_on_entry_bar
            ):
                self._process_position_exit(
                    bar,
                    index,
                    allow_entry_bar=True,
                    stop_only=True,
                    stop_reason="stop_on_fill_bar",
                )
                if (
                    self.portfolio.position is None
                    and order.remaining_quantity > 1e-12
                    and order.id in self._pending
                ):
                    order.status = OrderStatus.CANCELLED
                    self._pending.pop(order.id, None)
                    self._log_order_event(
                        bar.timestamp,
                        "cancelled",
                        order,
                        "stop_on_fill_bar_remainder",
                    )
            elif (
                self.portfolio.position is not None
                and self.config.execution.allow_exit_on_entry_bar
                and not order.reduce_only
                and order.order_type is OrderType.MARKET
            ):
                self._process_position_exit(bar, index, allow_entry_bar=True)
            if (
                self.config.one_position
                and self.portfolio.position is not None
                and not order.reduce_only
            ):
                break

    def _process_pending_close(self, bar: Candle, index: int) -> None:
        pending = self.pending_close
        if pending is None or index < pending.eligible_from_index:
            return
        position = self.portfolio.position
        if position is None:
            self.pending_close = None
            return
        quantity = pending.quantity or (
            position.quantity * pending.fraction
            if pending.fraction is not None
            else position.quantity
        )
        self.close_position(
            timestamp=bar.timestamp,
            price=bar.open,
            reason=pending.reason,
            index=index,
            liquidity="taker",
            reference_price=bar.open,
            quantity=quantity,
        )
        self.pending_close = None

    def _expire_orders(
        self,
        timestamp: datetime,
        index: int,
        *,
        preserve_non_protective: bool = False,
    ) -> None:
        for order in self.pending_orders:
            if preserve_non_protective and not (order.reduce_only or order.close_position):
                continue
            expired_by_bar = (
                order.expires_after_index is not None and index > order.expires_after_index
            )
            expired_by_time = order.good_till_time is not None and timestamp >= order.good_till_time
            if expired_by_bar or expired_by_time:
                order.status = OrderStatus.EXPIRED
                self._pending.pop(order.id, None)
                self._log_order_event(timestamp, "expired", order, ReasonCode.ORDER_EXPIRED.value)

    def _valid_bracket(self, request: OrderRequest, reference_price: float) -> bool:
        if request.side is Side.BUY:
            return not (
                (request.stop_loss is not None and request.stop_loss >= reference_price)
                or (request.take_profit is not None and request.take_profit <= reference_price)
            )
        return not (
            (request.stop_loss is not None and request.stop_loss <= reference_price)
            or (request.take_profit is not None and request.take_profit >= reference_price)
        )

    def _valid_bracket_for_order(self, order: Order, reference_price: float) -> bool:
        request = self._request_from_order(order)
        return self._valid_bracket(request, reference_price)

    def _request_from_order(self, order: Order) -> OrderRequest:
        return OrderRequest(
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

    def _size_order(self, request: OrderRequest, *, equity: float, reference_price: float) -> float:
        risk_per_unit = self._historical_limit_cost_per_unit_risk(request, reference_price)
        sizing_equity = (
            self._historical_closed_equity
            if self.config.risk.sizing_model == "historical_limit_cost"
            and self.portfolio.position is None
            else equity
        )
        return self.sizer.size(
            request,
            equity=sizing_equity,
            reference_price=reference_price,
            contract_size=self.instrument.contract_size,
            risk_per_unit=risk_per_unit,
        )

    def _historical_limit_cost_per_unit_risk(
        self, request: OrderRequest, reference_price: float
    ) -> float | None:
        if self.config.risk.sizing_model != "historical_limit_cost" or request.quantity is not None:
            return None
        if request.reduce_only:
            return None
        if request.order_type is not OrderType.LIMIT:
            raise RiskError("historical_limit_cost sizing requires a LIMIT entry order")
        if request.stop_loss is None:
            raise RiskError("historical_limit_cost sizing requires stop_loss")
        risk_fraction = (
            request.risk_fraction
            if request.risk_fraction is not None
            else self.config.risk.default_risk_fraction
        )
        if risk_fraction > self.config.risk.max_risk_fraction:
            raise RiskError("risk_fraction exceeds configured maximum")
        raw_distance = abs(reference_price - request.stop_loss)
        if raw_distance <= 0:
            raise RiskError("stop distance must be positive")
        exit_side = Side.SELL if request.side is Side.BUY else Side.BUY
        stop_slip = self.config.execution.stop_slippage_bps / 10_000
        stop_fill = request.stop_loss * (1 + stop_slip * exit_side.sign)
        if stop_fill <= 0:
            raise RiskError("historical stop fill must be positive")
        per_unit_risk = (
            abs(reference_price - stop_fill)
            + reference_price * self.config.fees.maker_rate
            + stop_fill * self.config.fees.taker_rate
        )
        if per_unit_risk <= 0:
            raise RiskError("historical per-unit risk must be positive")
        return per_unit_risk

    def _resize_at_actual_fill(self, order: Order, fill_price: float, mark_price: float) -> bool:
        try:
            actual_quantity = self._size_order(
                self._request_from_order(order),
                equity=self.portfolio.equity(mark_price),
                reference_price=fill_price,
            )
            actual_quantity = self.rules.normalize_quantity(actual_quantity, price=fill_price)
        except (RiskError, InstrumentRuleError) as exc:
            self.journal.log_error(order.created_at, f"fill_risk:{order.source_module}", exc)
            return False
        order.quantity = min(order.quantity, actual_quantity)
        return order.quantity > 0

    def _fill_details(
        self, order: Order, bar: Candle, index: int
    ) -> tuple[float, float, str] | None:
        slip = self.config.execution.slippage_bps / 10_000
        if order.order_type is OrderType.MARKET:
            return bar.open * (1 + slip * order.side.sign), bar.open, "taker"

        limit_price = order.effective_limit_price
        trigger_price = order.effective_trigger_price
        if order.order_type is OrderType.LIMIT:
            if limit_price is None:
                raise RuntimeError("limit order missing effective limit price")
            touched = bar.low <= limit_price if order.side is Side.BUY else bar.high >= limit_price
            if not touched:
                return None
            if self.config.execution.limit_fill_policy == "price_improvement":
                base = (
                    min(limit_price, bar.open)
                    if order.side is Side.BUY
                    else max(limit_price, bar.open)
                )
            else:
                base = limit_price
            return base, limit_price, "maker"

        if order.order_type in {
            OrderType.STOP,
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_MARKET,
        }:
            if trigger_price is None:
                raise RuntimeError("triggered market order missing effective trigger price")
            take_profit = order.order_type is OrderType.TAKE_PROFIT_MARKET
            triggered = self._trigger_hit(order.side, trigger_price, bar, take_profit=take_profit)
            if not triggered:
                return None
            base = self._trigger_gap_price(
                order.side, trigger_price, bar.open, take_profit=take_profit
            )
            return base * (1 + slip * order.side.sign), trigger_price, "taker"

        if order.order_type in {OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT}:
            if trigger_price is None or limit_price is None:
                raise RuntimeError("triggered limit order missing effective trigger or limit price")
            newly_triggered = False
            if order.triggered_at is None:
                take_profit = order.order_type is OrderType.TAKE_PROFIT_LIMIT
                triggered = self._trigger_hit(
                    order.side, trigger_price, bar, take_profit=take_profit
                )
                if not triggered:
                    return None
                order.triggered_at = bar.timestamp
                order.triggered_index = index
                newly_triggered = True
                self._log_order_event(bar.timestamp, "triggered", order)
            if newly_triggered and self.config.execution.stop_limit_same_bar_policy == "defer":
                return None
            touched = bar.low <= limit_price if order.side is Side.BUY else bar.high >= limit_price
            if not touched:
                return None
            if self.config.execution.limit_fill_policy == "price_improvement":
                base = (
                    min(limit_price, bar.open)
                    if order.side is Side.BUY
                    else max(limit_price, bar.open)
                )
            else:
                base = limit_price
            return base, limit_price, "maker"
        return None

    @staticmethod
    def _trigger_hit(side: Side, trigger: float, bar: Candle, *, take_profit: bool) -> bool:
        if take_profit:
            return bar.low <= trigger if side is Side.BUY else bar.high >= trigger
        return bar.high >= trigger if side is Side.BUY else bar.low <= trigger

    @staticmethod
    def _trigger_gap_price(
        side: Side, trigger: float, open_price: float, *, take_profit: bool
    ) -> float:
        if take_profit:
            return min(trigger, open_price) if side is Side.BUY else max(trigger, open_price)
        return max(trigger, open_price) if side is Side.BUY else min(trigger, open_price)

    def _fill_quantity(self, order: Order) -> float:
        quantity = order.remaining_quantity * self.config.execution.max_fill_fraction_per_bar
        cap = self.config.execution.max_fill_quantity_per_bar
        if cap is not None:
            quantity = min(quantity, cap)
        if order.time_in_force in {TimeInForce.FOK, TimeInForce.IOC} and cap is None:
            quantity = order.remaining_quantity
        return min(order.remaining_quantity, quantity)

    def _apply_entry_fill(
        self,
        order: Order,
        *,
        timestamp: datetime,
        index: int,
        price: float,
        reference_price: float,
        liquidity: str,
        quantity: float,
    ) -> None:
        fee = price * quantity * self.instrument.contract_size * self._fee_rate(liquidity)
        self.portfolio.cash -= fee
        fill = self._new_fill(
            order=order,
            timestamp=timestamp,
            price=price,
            reference_price=reference_price,
            liquidity=liquidity,
            quantity=quantity,
            fee=fee,
            reason="entry",
        )
        position = self.portfolio.position
        position_side = PositionSide.LONG if order.side is Side.BUY else PositionSide.SHORT
        if position is None:
            self._position_sequence += 1
            position = Position(
                id=f"pos-{self._position_sequence:08d}",
                entry_order_id=order.id,
                side=position_side,
                quantity=quantity,
                entry_price=price,
                entry_time=timestamp,
                entry_index=index,
                entry_fee=fee,
                initial_risk_amount=(
                    abs(price - order.stop_loss) * quantity * self.instrument.contract_size
                    if order.stop_loss is not None
                    else None
                ),
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                tag=order.tag,
                setup_id=order.setup_id,
                metadata=dict(order.metadata),
                entry_fills=[fill],
            )
            self.portfolio.position = position
        elif position.side is position_side:
            position.add_entry_fill(fill)
            if order.stop_loss is not None:
                incremental = (
                    abs(price - order.stop_loss) * quantity * self.instrument.contract_size
                )
                position.initial_risk_amount = (position.initial_risk_amount or 0.0) + incremental
        else:
            order.status = OrderStatus.REJECTED
            self._pending.pop(order.id, None)
            self._log_order_event(timestamp, "rejected", order, "opposite_position_exists")
            return
        fill = (
            Fill(**{**fill.__dict__, "position_id": position.id})
            if hasattr(fill, "__dict__")
            else Fill(
                order_id=fill.order_id,
                timestamp=fill.timestamp,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                slippage=fill.slippage,
                reason=fill.reason,
                liquidity=fill.liquidity,
                fill_id=fill.fill_id,
                position_id=position.id,
                cumulative_quantity=fill.cumulative_quantity,
                remaining_quantity=fill.remaining_quantity,
            )
        )
        # Replace last stored fill in the position with the enriched immutable fill.
        position.entry_fills[-1] = fill
        self._record_fill_and_order(order, fill, timestamp)

    def _apply_reduce_fill(
        self,
        order: Order,
        *,
        timestamp: datetime,
        index: int,
        price: float,
        reference_price: float,
        liquidity: str,
        quantity: float,
    ) -> None:
        position = self.portfolio.position
        if position is None:
            order.status = OrderStatus.REJECTED
            self._pending.pop(order.id, None)
            self._log_order_event(timestamp, "rejected", order, ReasonCode.POSITION_NOT_FOUND.value)
            return
        quantity = min(quantity, position.quantity)
        trade = self.close_position(
            timestamp=timestamp,
            price=price,
            reason="reduce_only",
            index=index,
            liquidity=liquidity,
            reference_price=reference_price,
            quantity=quantity,
            exit_order_id=order.id,
        )
        if trade is None:
            return
        order.filled_quantity += quantity
        order.average_fill_price = self._weighted_average(
            order.average_fill_price, order.filled_quantity - quantity, price, quantity
        )
        # Order-level fees include only this reduce execution.  The trade also
        # contains an allocated share of the original entry fee, which must not
        # be charged to the exit order a second time.
        order.fees_paid += (
            price * quantity * self.instrument.contract_size * self._fee_rate(liquidity)
        )
        if order.remaining_quantity <= 1e-12 or self.portfolio.position is None:
            order.status = OrderStatus.FILLED
            self._pending.pop(order.id, None)
            self._log_order_event(timestamp, "filled", order)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            self._log_order_event(timestamp, "partially_filled", order)

    def _new_fill(
        self,
        *,
        order: Order,
        timestamp: datetime,
        price: float,
        reference_price: float,
        liquidity: str,
        quantity: float,
        fee: float,
        reason: str,
    ) -> Fill:
        self._fill_sequence += 1
        cumulative = order.filled_quantity + quantity
        return Fill(
            order_id=order.id,
            timestamp=timestamp,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            slippage=abs(price - reference_price),
            reason=reason,
            liquidity=liquidity,
            fill_id=f"fill-{self._fill_sequence:010d}",
            cumulative_quantity=cumulative,
            remaining_quantity=max(0.0, order.quantity - cumulative),
        )

    def _record_fill_and_order(self, order: Order, fill: Fill, timestamp: datetime) -> None:
        previous_quantity = order.filled_quantity
        order.filled_quantity += fill.quantity
        order.average_fill_price = self._weighted_average(
            order.average_fill_price,
            previous_quantity,
            fill.price,
            fill.quantity,
        )
        order.fees_paid += fill.fee
        self.journal.record_fill(fill, symbol=self.instrument.symbol)
        if order.remaining_quantity <= 1e-12:
            order.status = OrderStatus.FILLED
            self._pending.pop(order.id, None)
            self._log_order_event(timestamp, "filled", order)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            self._log_order_event(timestamp, "partially_filled", order)

    @staticmethod
    def _weighted_average(
        old_price: float | None, old_qty: float, new_price: float, new_qty: float
    ) -> float:
        if old_price is None or old_qty <= 0:
            return new_price
        return (old_price * old_qty + new_price * new_qty) / (old_qty + new_qty)

    def _process_position_exit(
        self,
        bar: Candle,
        index: int,
        *,
        allow_entry_bar: bool = False,
        stop_only: bool = False,
        stop_reason: str = "stop",
    ) -> None:
        position = self.portfolio.position
        if position is None or (index == position.entry_index and not allow_entry_bar):
            return
        if position.side is PositionSide.LONG:
            stop_hit = position.stop_loss is not None and bar.low <= position.stop_loss
            target_hit = (
                not stop_only
                and position.take_profit is not None
                and bar.high >= position.take_profit
            )
        else:
            stop_hit = position.stop_loss is not None and bar.high >= position.stop_loss
            target_hit = (
                not stop_only
                and position.take_profit is not None
                and bar.low <= position.take_profit
            )
        if not stop_hit and not target_hit:
            return
        reason = (
            self.config.execution.intrabar_priority
            if stop_hit and target_hit
            else ("stop" if stop_hit else "target")
        )
        close_reason = stop_reason if reason == "stop" else reason
        if reason == "stop":
            if position.stop_loss is None:
                raise RuntimeError("stop exit selected without a stop-loss price")
            if position.side is PositionSide.LONG:
                base, side_sign = min(position.stop_loss, bar.open), Side.SELL.sign
            else:
                base, side_sign = max(position.stop_loss, bar.open), Side.BUY.sign
            slip = self.config.execution.stop_slippage_bps / 10_000
            price = base * (1 + slip * side_sign)
            liquidity, reference_price = "taker", position.stop_loss
        else:
            if position.take_profit is None:
                raise RuntimeError("target exit selected without a take-profit price")
            price = position.take_profit
            liquidity, reference_price = (
                self.config.execution.target_liquidity,
                position.take_profit,
            )
        self.close_position(
            timestamp=bar.timestamp,
            price=price,
            reason=close_reason,
            index=index,
            liquidity=liquidity,
            reference_price=reference_price,
        )

    def _market_exit_price(self, position: Position, reference_price: float) -> float:
        exit_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        slip = self.config.execution.slippage_bps / 10_000
        return reference_price * (1 + slip * exit_side.sign)

    def close_position(
        self,
        timestamp: datetime,
        price: float,
        reason: str,
        *,
        index: int | None = None,
        liquidity: str = "taker",
        reference_price: float | None = None,
        quantity: float | None = None,
        exit_order_id: str | None = None,
        _preserve_pending_context: bool = False,
    ) -> Trade | None:
        position = self.portfolio.position
        if position is None:
            return None
        if (
            reason == ReasonCode.END_OF_DATA.value
            and self.config.execution.close_request_timing == "current_close"
            and reference_price is not None
        ):
            price = self._market_exit_price(position, reference_price)
        if (
            not isinstance(price, (int, float))
            or not __import__("math").isfinite(float(price))
            or price <= 0
        ):
            raise ValueError("close price must be finite and positive")
        if quantity is not None and (
            not __import__("math").isfinite(float(quantity)) or quantity <= 0
        ):
            raise ValueError("close quantity must be finite and positive")
        close_quantity = min(quantity or position.quantity, position.quantity)
        position_will_close = close_quantity >= position.quantity - 1e-12
        fraction = close_quantity / position.quantity
        gross = (
            (price - position.entry_price)
            * close_quantity
            * position.direction
            * self.instrument.contract_size
        )
        exit_fee = (
            price * close_quantity * self.instrument.contract_size * self._fee_rate(liquidity)
        )
        allocated_entry_fee = position.entry_fee * fraction
        allocated_risk = (
            position.initial_risk_amount * fraction
            if position.initial_risk_amount is not None
            else None
        )
        net_pnl = gross - allocated_entry_fee - exit_fee
        exit_side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        next_fill_sequence = self._fill_sequence + 1
        exit_fill = Fill(
            order_id=exit_order_id or f"exit:{position.entry_order_id}",
            timestamp=timestamp,
            side=exit_side,
            quantity=close_quantity,
            price=price,
            fee=exit_fee,
            slippage=abs(price - (reference_price if reference_price is not None else price)),
            reason=reason,
            liquidity=liquidity,
            fill_id=f"fill-{next_fill_sequence:010d}",
            position_id=position.id,
            cumulative_quantity=sum(fill.quantity for fill in position.exit_fills) + close_quantity,
            remaining_quantity=max(0.0, position.quantity - close_quantity),
        )
        trade = Trade(
            side=position.side,
            quantity=close_quantity,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=price,
            gross_pnl=gross,
            fees=allocated_entry_fee + exit_fee,
            net_pnl=net_pnl,
            exit_reason=reason,
            tag=position.tag,
            initial_risk_amount=allocated_risk,
            r_multiple=(
                net_pnl / allocated_risk if allocated_risk and allocated_risk > 0 else None
            ),
            holding_bars=(index - position.entry_index if index is not None else None),
            setup_id=position.setup_id,
            position_id=position.id,
            entry_fill_count=len(position.entry_fills),
            exit_fill_count=len(position.exit_fills) + 1,
            position_closed=position_will_close,
        )
        self.journal.record_fill_and_trade(exit_fill, trade, symbol=self.instrument.symbol)
        self._fill_sequence = next_fill_sequence
        self.portfolio.cash += gross - exit_fee
        if self.config.risk.sizing_model == "historical_limit_cost":
            self._historical_closed_equity += net_pnl
            if position_will_close:
                self.portfolio.cash = self._historical_closed_equity
        position.exit_fills.append(exit_fill)
        position.realised_pnl += net_pnl
        position.quantity -= close_quantity
        position.entry_fee -= allocated_entry_fee
        if position.initial_risk_amount is not None:
            position.initial_risk_amount -= allocated_risk or 0.0
        if position.quantity <= 1e-12:
            self.portfolio.position = None
            if not _preserve_pending_context:
                self.pending_close = None
                for pending_order in tuple(self.pending_orders):
                    if pending_order.reduce_only and pending_order.id != exit_order_id:
                        pending_order.status = OrderStatus.CANCELLED
                        self._pending.pop(pending_order.id, None)
                        self._log_order_event(
                            timestamp, "cancelled", pending_order, "position_closed"
                        )
        return trade

    def cancel_all(self, timestamp: datetime, reason: str = ReasonCode.END_OF_DATA.value) -> None:
        for order in self.pending_orders:
            cancelled = replace(order, status=OrderStatus.CANCELLED)
            self._log_order_event(timestamp, "cancelled", cancelled, reason)
            order.status = OrderStatus.CANCELLED
            self._pending.pop(order.id, None)

    def _fee_rate(self, liquidity: str) -> float:
        return self.config.fees.maker_rate if liquidity == "maker" else self.config.fees.taker_rate

    def _log_order_event(
        self, timestamp: datetime, event: str, order: Order, reason: str | None = None
    ) -> None:
        self.journal.record_order(
            OrderEvent(
                timestamp=timestamp,
                event=event,
                order=OrderSnapshot.from_order(order),
                reason=reason,
            ),
            symbol=self.instrument.symbol,
        )


BacktestExecutionAdapter = BrokerSimulator
