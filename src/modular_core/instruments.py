from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .models import OrderRequest, OrderType, Side
from .validation import nonnegative_number, positive_number, require_nonblank


class InstrumentRuleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    base_asset: str = "BASE"
    quote_asset: str = "QUOTE"
    market_type: str = "linear_futures"
    tick_size: float = 0.0
    quantity_step: float = 0.0
    min_quantity: float = 0.0
    max_quantity: float | None = None
    min_notional: float = 0.0
    max_notional: float | None = None
    contract_size: float = 1.0
    price_precision: int | None = None
    quantity_precision: int | None = None

    def __post_init__(self) -> None:
        require_nonblank(self.symbol, "instrument symbol")
        require_nonblank(self.base_asset, "base_asset")
        require_nonblank(self.quote_asset, "quote_asset")
        require_nonblank(self.market_type, "market_type")
        for name in ("tick_size", "quantity_step", "min_quantity", "min_notional"):
            nonnegative_number(getattr(self, name), name)
        positive_number(self.contract_size, "contract_size")
        if self.max_quantity is not None:
            positive_number(self.max_quantity, "max_quantity")
            if self.max_quantity < self.min_quantity:
                raise ValueError("max_quantity cannot be below min_quantity")
        if self.max_notional is not None:
            positive_number(self.max_notional, "max_notional")
            if self.max_notional < self.min_notional:
                raise ValueError("max_notional cannot be below min_notional")
        for name in ("price_precision", "quantity_precision"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")


class ExchangeRulesAdapter:
    """Purpose-aware exchange rounding and post-normalization validation."""

    def __init__(self, spec: InstrumentSpec) -> None:
        if not isinstance(spec, InstrumentSpec):
            raise TypeError("spec must be InstrumentSpec")
        self.spec = spec

    def normalize_quantity(self, quantity: float, *, price: float) -> float:
        try:
            positive_number(quantity, "quantity")
            positive_number(price, "price")
        except (TypeError, ValueError) as exc:
            raise InstrumentRuleError(str(exc)) from exc
        result = self._floor_to_step(quantity, self.spec.quantity_step)
        if self.spec.quantity_precision is not None:
            result = round(result, self.spec.quantity_precision)
        if result < self.spec.min_quantity:
            raise InstrumentRuleError("quantity below instrument minimum")
        if self.spec.max_quantity is not None and result > self.spec.max_quantity:
            result = self._floor_to_step(self.spec.max_quantity, self.spec.quantity_step)
        notional = result * price * self.spec.contract_size
        if notional < self.spec.min_notional:
            raise InstrumentRuleError("notional below instrument minimum")
        if self.spec.max_notional is not None and notional > self.spec.max_notional:
            capped = self.spec.max_notional / (price * self.spec.contract_size)
            result = self._floor_to_step(capped, self.spec.quantity_step)
            if self.spec.quantity_precision is not None:
                result = round(result, self.spec.quantity_precision)
        notional = result * price * self.spec.contract_size
        if result <= 0 or not math.isfinite(result):
            raise InstrumentRuleError("quantity is invalid after normalization")
        if result < self.spec.min_quantity:
            raise InstrumentRuleError("quantity below instrument minimum after cap")
        if self.spec.max_quantity is not None and result > self.spec.max_quantity:
            raise InstrumentRuleError("quantity above instrument maximum after normalization")
        if notional < self.spec.min_notional:
            raise InstrumentRuleError("notional below instrument minimum after cap")
        if self.spec.max_notional is not None and notional > self.spec.max_notional + 1e-12:
            raise InstrumentRuleError("notional above instrument maximum after normalization")
        return result

    def normalize_entry_price(self, price: float, side: Side, order_type: OrderType) -> float:
        self._validate_side(side)
        if not isinstance(order_type, OrderType):
            raise InstrumentRuleError("order_type must be OrderType")
        if order_type in {
            OrderType.LIMIT,
            OrderType.TAKE_PROFIT_LIMIT,
            OrderType.STOP_LIMIT,
        }:
            # A buy limit rounds down and a sell limit rounds up so it does not
            # accidentally become more marketable. Trigger/market orders are
            # normalized conservatively in their dedicated methods.
            mode = "floor" if side is Side.BUY else "ceil"
        else:
            mode = "ceil" if side is Side.BUY else "floor"
        return self._normalize_price(price, mode)

    def normalize_stop_price(self, price: float, *, entry_price: float, side: Side) -> float:
        self._validate_side(side)
        self._validate_positive(entry_price, "entry_price")
        # Stop must never move closer to entry due to exchange rounding.
        mode = "floor" if side is Side.BUY else "ceil"
        result = self._normalize_price(price, mode)
        if side is Side.BUY and result >= entry_price:
            raise InstrumentRuleError("long stop is not below entry after normalization")
        if side is Side.SELL and result <= entry_price:
            raise InstrumentRuleError("short stop is not above entry after normalization")
        return result

    def normalize_target_price(self, price: float, *, entry_price: float, side: Side) -> float:
        self._validate_side(side)
        self._validate_positive(entry_price, "entry_price")
        # Target must not become more favorable than requested.
        mode = "floor" if side is Side.BUY else "ceil"
        result = self._normalize_price(price, mode)
        if side is Side.BUY and result <= entry_price:
            raise InstrumentRuleError("long target is not above entry after normalization")
        if side is Side.SELL and result >= entry_price:
            raise InstrumentRuleError("short target is not below entry after normalization")
        return result

    def normalize_order(self, request: OrderRequest, *, reference_price: float) -> OrderRequest:
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be OrderRequest")
        self._validate_positive(reference_price, "reference_price")
        price = request.effective_limit_price
        trigger = request.effective_trigger_price
        normalized_price = (
            self.normalize_entry_price(price, request.side, request.order_type)
            if price is not None
            else None
        )
        normalized_trigger = (
            self.normalize_entry_price(trigger, request.side, request.order_type)
            if trigger is not None
            else None
        )
        geometry_price = normalized_price or normalized_trigger or reference_price
        stop = (
            self.normalize_stop_price(
                request.stop_loss,
                entry_price=geometry_price,
                side=request.side,
            )
            if request.stop_loss is not None
            else None
        )
        target = (
            self.normalize_target_price(
                request.take_profit,
                entry_price=geometry_price,
                side=request.side,
            )
            if request.take_profit is not None
            else None
        )
        quantity = (
            self.normalize_quantity(request.quantity, price=geometry_price)
            if request.quantity is not None
            else None
        )
        result = request.replace_prices(
            price=normalized_price,
            trigger_price=normalized_trigger,
            stop_loss=stop,
            take_profit=target,
            quantity=quantity,
        )
        self.validate_post_only(result, reference_price=reference_price)
        return result

    def validate_post_only(self, request: OrderRequest, *, reference_price: float) -> None:
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be OrderRequest")
        if not request.post_only:
            return
        self._validate_positive(reference_price, "reference_price")
        price = request.effective_limit_price
        if price is None:
            raise InstrumentRuleError("post-only requires a limit price")
        marketable = (
            price >= reference_price if request.side is Side.BUY else price <= reference_price
        )
        if marketable:
            raise InstrumentRuleError("post-only order would be marketable")

    @staticmethod
    def _validate_side(side: Side) -> None:
        if not isinstance(side, Side):
            raise InstrumentRuleError("side must be Side")

    @staticmethod
    def _validate_positive(value: float, name: str) -> None:
        try:
            positive_number(value, name)
        except (TypeError, ValueError) as exc:
            raise InstrumentRuleError(str(exc)) from exc

    def _normalize_price(self, price: float, mode: str) -> float:
        try:
            positive_number(price, "price")
        except (TypeError, ValueError) as exc:
            raise InstrumentRuleError(str(exc)) from exc
        step = self.spec.tick_size
        if step <= 0:
            result = price
        else:
            result = (
                self._floor_to_step(price, step)
                if mode == "floor"
                else self._ceil_to_step(price, step)
            )
        if self.spec.price_precision is not None:
            result = round(result, self.spec.price_precision)
        if result <= 0 or not math.isfinite(result):
            raise InstrumentRuleError("invalid price after normalization")
        return result

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        units = (d_value / d_step).to_integral_value(rounding=ROUND_FLOOR)
        return float(units * d_step)

    @staticmethod
    def _ceil_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        units = (d_value / d_step).to_integral_value(rounding=ROUND_CEILING)
        return float(units * d_step)
