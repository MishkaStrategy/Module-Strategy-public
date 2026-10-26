from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from modular_core import (
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderRequest,
    OrderType,
    PositionSide,
    Side,
    StrategyModule,
)

from .canonical import canonical_json
from .enums import TradeAction
from .errors import RouterSerializationError
from .module import EXECUTABLE_PLAN_FEATURE_KEY
from .serialization import TradePlanSerializer

MODULE_NAME = "trade_plan_core_order"
MODULE_VERSION = "0.1.0"
MODULE_API_VERSION = "1.0"
STATE_SCHEMA_VERSION = "1"

MANIFEST = ModuleManifest(
    name=MODULE_NAME,
    version=MODULE_VERSION,
    api_version=MODULE_API_VERSION,
    required_features=(EXECUTABLE_PLAN_FEATURE_KEY,),
    optional_features=(),
    produced_features=(),
    parameters_schema={},
    skip_until_ready=False,
    description=(
        "Fail-closed Backtest/Core adapter from canonical Router OPEN_POSITION plans "
        "to official Core OrderRequest without recomputing strategy economics."
    ),
    state_schema_version=STATE_SCHEMA_VERSION,
)


class CoreOrderBridgeError(RuntimeError):
    """Raised when a canonical Router plan cannot be represented exactly in Core."""


class TradePlanCoreOrderModule(StrategyModule):
    """Translate one canonical Router opening plan into one Core order request.

    The Router remains the owner of candidate selection, quantity, entry intent and
    protection intent. Core/Backtest remains the owner of order normalization,
    fills, fees, slippage, funding, valuation and portfolio mutation.
    """

    name = MODULE_NAME
    manifest = MANIFEST

    def __init__(self) -> None:
        self.parameters = MappingProxyType({})
        self._last_plan_id: str | None = None
        self._last_content_hash: str | None = None
        self._serializer = TradePlanSerializer()

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not isinstance(view, MarketView):
            raise TypeError("view must be MarketView")
        if view.features is None:
            raise CoreOrderBridgeError("MarketView.features is unavailable")
        feature = view.features.latest(
            EXECUTABLE_PLAN_FEATURE_KEY,
            symbol=view.current.symbol,
        )
        if feature is None:
            return ModuleDecision(skips=("no_executable_trade_plan",))
        if feature.schema_version != "1":
            raise CoreOrderBridgeError("unsupported executable-plan feature schema")
        mapping = self._validated_mapping(feature.value)
        plan_id = _text(mapping.get("plan_id"), "plan_id")
        content_hash = _digest(mapping.get("content_hash"), "content_hash")
        if self._last_plan_id == plan_id:
            if self._last_content_hash != content_hash:
                raise CoreOrderBridgeError("same plan_id was observed with different content")
            return ModuleDecision(skips=("trade_plan_already_processed",))

        order = self._opening_order(mapping, view=view)
        self._last_plan_id = plan_id
        self._last_content_hash = content_hash
        return ModuleDecision(
            orders=(order,),
            diagnostics={
                "router_plan_id": plan_id,
                "router_content_hash": content_hash,
                "bridge_action": "open_position",
            },
        )

    def snapshot_state(self) -> Mapping[str, object]:
        if self._last_plan_id is None:
            return MappingProxyType({})
        return MappingProxyType(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "last_plan_id": self._last_plan_id,
                "last_content_hash": self._last_content_hash,
            }
        )

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise ValueError("bridge state must be a mapping")
        if not state:
            self._last_plan_id = None
            self._last_content_hash = None
            return
        if set(state) != {"schema_version", "last_plan_id", "last_content_hash"}:
            raise ValueError("bridge state has unknown or missing fields")
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported bridge state schema")
        self._last_plan_id = _text(state.get("last_plan_id"), "last_plan_id")
        self._last_content_hash = _digest(state.get("last_content_hash"), "last_content_hash")

    def _validated_mapping(self, value: object) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise CoreOrderBridgeError("executable-plan feature value must be a mapping")
        try:
            serialized = canonical_json(value)
            validated = self._serializer.loads_plan_mapping(serialized)
        except (TypeError, ValueError, RouterSerializationError) as exc:
            raise CoreOrderBridgeError(f"canonical Router plan validation failed: {exc}") from exc
        if not isinstance(validated, Mapping):
            raise CoreOrderBridgeError("canonical Router validation did not return a mapping")
        return validated

    def _opening_order(self, mapping: Mapping[str, Any], *, view: MarketView) -> OrderRequest:
        action = _text(mapping.get("action"), "action")
        if action != TradeAction.OPEN_POSITION.value:
            raise CoreOrderBridgeError(f"unsupported Router action for Backtest bridge: {action}")
        symbol = _text(mapping.get("symbol"), "symbol")
        if symbol != view.current.symbol:
            raise CoreOrderBridgeError("Router plan symbol does not match current stream")
        now = view.current.close_time.astimezone(UTC)
        valid_from = _utc(mapping.get("valid_from"), "valid_from")
        expires_at = _utc(mapping.get("expires_at"), "expires_at")
        if now < valid_from:
            raise CoreOrderBridgeError("Router plan is not yet valid")
        if now >= expires_at:
            raise CoreOrderBridgeError("Router plan is stale")
        cancellations = _sequence(mapping.get("cancellation_intents"), "cancellation_intents")
        if cancellations:
            raise CoreOrderBridgeError("Router cancellation intents are unsupported")
        sequencing = _sequence(mapping.get("sequencing_rules"), "sequencing_rules")
        if sequencing:
            raise CoreOrderBridgeError("Router sequencing rules are unsupported for opening bridge")

        side_name = _text(mapping.get("side"), "side")
        if side_name == "long":
            side = Side.BUY
            position_side = PositionSide.LONG
        elif side_name == "short":
            side = Side.SELL
            position_side = PositionSide.SHORT
        else:
            raise CoreOrderBridgeError(f"unsupported Router side: {side_name}")

        entry = _mapping(mapping.get("entry_intent"), "entry_intent")
        if _boolean(entry.get("reduce_only"), "entry_intent.reduce_only"):
            raise CoreOrderBridgeError("OPEN_POSITION plan cannot be reduce_only")
        order_type = _order_type(entry.get("order_type"))
        quantity = _positive_float(entry.get("quantity"), "entry_intent.quantity")
        limit_price = _optional_positive_float(entry.get("limit_price"), "entry_intent.limit_price")
        if limit_price is None:
            limit_price = _optional_positive_float(entry.get("price"), "entry_intent.price")
        trigger_price = _optional_positive_float(
            entry.get("trigger_price"), "entry_intent.trigger_price"
        )
        if order_type is OrderType.MARKET:
            limit_price = None
            trigger_price = None
        elif order_type is OrderType.LIMIT:
            if limit_price is None:
                raise CoreOrderBridgeError("limit plan is missing limit price")
            trigger_price = None
        elif order_type is OrderType.STOP_MARKET:
            if trigger_price is None:
                raise CoreOrderBridgeError("stop-market plan is missing trigger price")
            limit_price = None
        elif order_type is OrderType.STOP_LIMIT:
            if trigger_price is None or limit_price is None:
                raise CoreOrderBridgeError("stop-limit plan is missing trigger or limit price")

        protection = _mapping(mapping.get("protection_intent"), "protection_intent")
        stop_loss = _positive_float(protection.get("stop_loss"), "protection_intent.stop_loss")
        targets = _sequence(protection.get("targets"), "protection_intent.targets")
        if len(targets) != 1:
            raise CoreOrderBridgeError("Backtest Core bracket requires exactly one Router target")
        target = _mapping(targets[0], "protection_intent.targets[0]")
        if _decimal(target.get("allocation"), "target.allocation") != Decimal("1"):
            raise CoreOrderBridgeError("Backtest Core bracket requires full target allocation")
        if _text(target.get("target_type"), "target.target_type") != "limit":
            raise CoreOrderBridgeError("Backtest Core bracket supports Router limit target only")
        take_profit = _positive_float(target.get("price"), "target.price")

        plan_id = _text(mapping.get("plan_id"), "plan_id")
        metadata = {
            "router_plan_id": plan_id,
            "router_content_hash": _digest(mapping.get("content_hash"), "content_hash"),
            "router_audit_hash": _digest(mapping.get("audit_hash"), "audit_hash"),
            "router_configuration_hash": _digest(
                mapping.get("configuration_hash"), "configuration_hash"
            ),
            "router_selected_candidate_id": _text(
                mapping.get("selected_candidate_id"), "selected_candidate_id"
            ),
            "router_reference_entry_price": str(
                _decimal(entry.get("reference_entry_price"), "entry.reference_entry_price")
            ),
            "router_sizing": _mapping(mapping.get("sizing_intent"), "sizing_intent"),
            "router_input_hashes": tuple(
                _digest(item, "input_hash")
                for item in _sequence(mapping.get("input_hashes"), "input_hashes")
            ),
        }
        return OrderRequest(
            side=side,
            order_type=order_type,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            tag=f"router:{plan_id}",
            setup_id=_text(mapping.get("setup_id"), "setup_id"),
            client_order_id=f"router:{plan_id}",
            ttl_bars=_optional_positive_int(entry.get("ttl_bars"), "entry_intent.ttl_bars"),
            metadata=metadata,
            post_only=_boolean(entry.get("post_only"), "entry_intent.post_only"),
            reduce_only=False,
            trigger_price=trigger_price,
            limit_price=limit_price,
            position_side=position_side,
        )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoreOrderBridgeError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        raise CoreOrderBridgeError(f"{name} must be a sequence")
    return tuple(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CoreOrderBridgeError(f"{name} must be a canonical non-blank string")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CoreOrderBridgeError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise CoreOrderBridgeError(f"{name} must be boolean")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, (str, Decimal)) or isinstance(value, bool):
        raise CoreOrderBridgeError(f"{name} must be a canonical decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise CoreOrderBridgeError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise CoreOrderBridgeError(f"{name} must be finite")
    return result


def _positive_float(value: object, name: str) -> float:
    decimal = _decimal(value, name)
    result = float(decimal)
    if decimal <= 0 or not math.isfinite(result) or result <= 0:
        raise CoreOrderBridgeError(f"{name} must be finite and positive")
    return result


def _optional_positive_float(value: object, name: str) -> float | None:
    return None if value is None else _positive_float(value, name)


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoreOrderBridgeError(f"{name} must be a positive integer or null")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CoreOrderBridgeError(f"{name} must be canonical UTC text")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError as exc:
        raise CoreOrderBridgeError(f"{name} must be canonical UTC text") from exc
    return result


def _order_type(value: object) -> OrderType:
    name = _text(value, "entry_intent.order_type")
    mapping = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "stop_market": OrderType.STOP_MARKET,
        "stop_limit": OrderType.STOP_LIMIT,
    }
    try:
        return mapping[name]
    except KeyError as exc:
        raise CoreOrderBridgeError(f"unsupported Router entry order type: {name}") from exc


__all__ = [
    "CoreOrderBridgeError",
    "MODULE_API_VERSION",
    "MODULE_NAME",
    "MODULE_VERSION",
    "TradePlanCoreOrderModule",
]
