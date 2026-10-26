from __future__ import annotations

import logging
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any

from modular_core import (
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    StrategyModule,
)

from .canonical import deep_freeze, has_control_characters, sha256_hex, stable_id
from .config import MAX_CONFIGURATION_POSITIVE_INT, RouterConfiguration
from .decimal_math import (
    DecimalDomainError,
    exact_multiply,
    exact_subtract,
    validate_decimal_domain,
)
from .enums import (
    ActivePlanPolicy,
    ExistingPositionPolicy,
    LongShortConflictPolicy,
    MultiSetupPolicy,
    PendingOrderPolicy,
    PlanVerdict,
    ReasonCode,
    TradeSide,
)
from .errors import PlanInvariantError, RouterConfigurationError, RouterInputError
from .limits import MAX_ROUTER_COLLECTION_ITEMS
from .models import (
    AuditEvent,
    PendingOrderState,
    PortfolioSnapshot,
    PositionState,
    RouterInputPackage,
    RoutingResult,
)
from .normalizer import DecisionNormalizer
from .router import DecisionRouter


LOGGER = logging.getLogger(__name__)

ENTRY_PLAN_FEATURE_KEY = "entry_model.plan"
LEGACY_ENTRY_PLAN_FEATURE_KEY = "entry_model.entry_plan"
PROTECTION_PLAN_FEATURE_KEY = "protection_target.plan"
RISK_DECISION_FEATURE_KEY = "risk.decision"
SCALING_APPROVED_FEATURE_KEY = "position_scaling.approved"
PORTFOLIO_SNAPSHOT_FEATURE_KEY = "portfolio.snapshot"
EXECUTABLE_PLAN_FEATURE_KEY = "trade_plan_router.executable_plan"
REJECTED_PLAN_FEATURE_KEY = "trade_plan_router.rejected_plan"
ROUTING_DECISION_FEATURE_KEY = "trade_plan_router.routing_decision"
CONFLICT_TRACE_FEATURE_KEY = "trade_plan_router.conflict_trace"
FEATURE_SCHEMA_VERSION = "1"
STATE_SCHEMA_VERSION = "1"


def _core_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise RouterInputError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RouterInputError(f"{name} must be numeric") from exc
    try:
        return validate_decimal_domain(result, name)
    except DecimalDomainError as exc:
        raise RouterInputError(str(exc)) from exc


def _core_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RouterInputError(f"{name} must be an exact non-blank string")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise RouterInputError(f"{name} must use NFC normalization")
    if len(value) > 512:
        raise RouterInputError(f"{name} exceeds maximum length 512")
    if has_control_characters(value):
        raise RouterInputError(f"{name} cannot contain control characters")
    return value


def _optional_core_text(value: Any, name: str) -> str | None:
    return None if value is None else _core_text(value, name)


def _core_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RouterInputError(f"{name} must be timezone-aware datetime")
    return value.astimezone(UTC)


def _position_side(value: Any) -> TradeSide:
    if value is PositionSide.LONG:
        return TradeSide.LONG
    if value is PositionSide.SHORT:
        return TradeSide.SHORT
    raise RouterInputError("fallback position side must be PositionSide.LONG or SHORT")


def _order_side(value: Any) -> TradeSide:
    if value is Side.BUY:
        return TradeSide.LONG
    if value is Side.SELL:
        return TradeSide.SHORT
    raise RouterInputError("fallback order side must be Side.BUY or SELL")


def _order_status(value: Any) -> OrderStatus:
    if not isinstance(value, OrderStatus):
        raise RouterInputError("fallback order status must be OrderStatus")
    return value


def _order_type(value: Any) -> str:
    if not isinstance(value, OrderType):
        raise RouterInputError("fallback order type must be OrderType")
    return value.value


PARAMETER_SCHEMA: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "schema_version": {"type": "string", "enum": ("1",), "default": "1"},
        "long_short_policy": {
            "type": "string",
            "enum": tuple(item.value for item in LongShortConflictPolicy),
            "default": LongShortConflictPolicy.REJECT_ALL.value,
        },
        "multi_setup_policy": {
            "type": "string",
            "enum": tuple(item.value for item in MultiSetupPolicy),
            "default": MultiSetupPolicy.HIGHEST_PRIORITY.value,
        },
        "existing_position_policy": {
            "type": "string",
            "enum": tuple(item.value for item in ExistingPositionPolicy),
            "default": ExistingPositionPolicy.REJECT.value,
        },
        "pending_order_policy": {
            "type": "string",
            "enum": tuple(item.value for item in PendingOrderPolicy),
            "default": PendingOrderPolicy.REJECT.value,
        },
        "active_plan_policy": {
            "type": "string",
            "enum": tuple(item.value for item in ActivePlanPolicy),
            "default": ActivePlanPolicy.REJECT_NEW_EXPOSURE.value,
        },
        "strict_mode": {"type": "boolean", "default": True},
        "require_portfolio_snapshot": {"type": "boolean", "default": False},
        "allow_risk_reducing_when_locked": {"type": "boolean", "default": True},
        "max_decision_age_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_CONFIGURATION_POSITIVE_INT,
            "default": 86_400,
        },
        "max_snapshot_age_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_CONFIGURATION_POSITIVE_INT,
            "default": 300,
        },
        "plan_ttl_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_CONFIGURATION_POSITIVE_INT,
            "default": 300,
        },
        "max_portfolio_risk_fraction": {
            "type": "number",
            "minimum": 0.000001,
            "maximum": 1,
            "default": 0.03,
        },
        "max_symbol_notional_fraction": {
            "type": "number",
            "minimum": 0.000001,
            "default": 3.0,
        },
        "max_entry_candidates": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_ROUTER_COLLECTION_ITEMS,
            "default": 1_000,
        },
        "max_decisions_per_type": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_ROUTER_COLLECTION_ITEMS,
            "default": 2_000,
        },
        "max_portfolio_positions": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_ROUTER_COLLECTION_ITEMS,
            "default": 5_000,
        },
        "max_pending_orders": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_ROUTER_COLLECTION_ITEMS,
            "default": 10_000,
        },
        "max_targets_per_plan": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_ROUTER_COLLECTION_ITEMS,
            "default": 100,
        },
        "portfolio_priorities": {"type": "object", "default": {}},
        "strategy_priorities": {"type": "object", "default": {}},
        "setup_priorities": {"type": "object", "default": {}},
        "module_priorities": {"type": "object", "default": {}},
        "side_priorities": {"type": "object", "default": {}},
        "warning_escalation": {"type": "boolean", "default": False},
        "audit_level": {
            "type": "string",
            "enum": ("minimal", "full"),
            "default": "full",
        },
    }
)


class TradePlanRouterModule(StrategyModule):
    """Stateless Module API adapter for the deterministic Router domain service."""

    name = "trade_plan_router"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        required_features=(
            ENTRY_PLAN_FEATURE_KEY,
            PROTECTION_PLAN_FEATURE_KEY,
            RISK_DECISION_FEATURE_KEY,
        ),
        optional_features=(
            LEGACY_ENTRY_PLAN_FEATURE_KEY,
            SCALING_APPROVED_FEATURE_KEY,
            PORTFOLIO_SNAPSHOT_FEATURE_KEY,
        ),
        produced_features=(
            EXECUTABLE_PLAN_FEATURE_KEY,
            REJECTED_PLAN_FEATURE_KEY,
            ROUTING_DECISION_FEATURE_KEY,
            CONFLICT_TRACE_FEATURE_KEY,
        ),
        parameters_schema=PARAMETER_SCHEMA,
        skip_until_ready=False,
        description=(
            "Deterministically composes upstream entry, protection, sizing and scaling "
            "decisions into an executable trade plan without placing exchange orders."
        ),
        state_schema_version=STATE_SCHEMA_VERSION,
    )

    def __init__(self, **parameters: Any) -> None:
        try:
            self.config = RouterConfiguration.from_mapping(parameters)
        except (ValueError, RouterConfigurationError) as exc:
            raise RouterConfigurationError(str(exc)) from exc
        self.parameters = deep_freeze(dict(parameters))
        self._normalizer = DecisionNormalizer(
            strict_mode=self.config.strict_mode,
            max_targets_per_plan=self.config.max_targets_per_plan,
            max_portfolio_positions=self.config.max_portfolio_positions,
            max_pending_orders=self.config.max_pending_orders,
        )
        self._router = DecisionRouter(self.config)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        started_ns = perf_counter_ns()
        as_of = view.current.close_time
        if view.features is None:
            result = self._input_error_result(
                as_of=as_of,
                portfolio_id="default",
                reason=ReasonCode.MISSING_ENTRY_DECISION,
                detail="MarketView.features is unavailable",
            )
        else:
            try:
                portfolio = self._portfolio(view, as_of)
                entries = self._entries(view, portfolio.portfolio_id)
                protections = self._protections(view)
                sizings = self._sizings(view)
                scalings = self._scalings(view)
                package = RouterInputPackage(
                    as_of=as_of,
                    portfolio=portfolio,
                    entries=entries,
                    protections=protections,
                    sizings=sizings,
                    scalings=scalings,
                )
                result = self._router.route(package)
            except RouterInputError as exc:
                result = self._input_error_result(
                    as_of=as_of,
                    portfolio_id="default",
                    reason=exc.reason,
                    detail=str(exc),
                )
        self._log_result(
            result=result,
            symbol=view.current.symbol,
            latency_ns=perf_counter_ns() - started_ns,
        )
        return self._decision(result, view)

    def _log_result(
        self,
        *,
        result: RoutingResult,
        symbol: str,
        latency_ns: int,
    ) -> None:
        plan = result.plan
        severity = "info" if result.verdict is PlanVerdict.ACCEPTED else "warning"
        LOGGER.log(
            logging.INFO if result.verdict is PlanVerdict.ACCEPTED else logging.WARNING,
            "trade_plan_routing_completed",
            extra={
                "correlation_id": result.result_id,
                "plan_id": None if plan is None else plan.plan_id,
                "portfolio_id": result.portfolio_id,
                "strategy_id": None if plan is None else plan.strategy_id,
                "setup_id": None if plan is None else plan.setup_id,
                "instrument": symbol,
                "routing_result": result.verdict.value,
                "reason_code": result.reason_codes[0].value,
                "policy_version": self.config.schema_version,
                "module_version": self.manifest.version,
                "configuration_hash": result.configuration_hash,
                "latency_ns": latency_ns,
                "latency_ms": latency_ns / 1_000_000,
                "severity": severity,
            },
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return MappingProxyType({})

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise ValueError(
                "TradePlanRouterModule is stateless and requires an empty mapping state"
            )
        try:
            frozen = deep_freeze(state)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Router state mapping: {exc}") from exc
        if frozen:
            raise ValueError("TradePlanRouterModule is stateless and accepts only empty state")

    def _entries(self, view: MarketView, portfolio_id: str) -> tuple[Any, ...]:
        if view.features is None:
            raise RouterInputError("MarketView.features is unavailable")
        features: list[Feature] = []
        for key in (ENTRY_PLAN_FEATURE_KEY, LEGACY_ENTRY_PLAN_FEATURE_KEY):
            features.extend(view.features.get_all(key, symbol=view.current.symbol))
        self._enforce_feature_limit(
            "entry candidates",
            len(features),
            self.config.max_entry_candidates,
        )
        return tuple(
            self._normalizer.normalize_entry(
                feature.value,
                feature_key=feature.key,
                feature_schema_version=feature.schema_version,
                feature_created_at=feature.created_at,
                feature_valid_until=feature.valid_until,
                feature_id=feature.feature_id,
                source_module=feature.source_module,
                symbol=view.current.symbol,
                portfolio_id=portfolio_id,
            )
            for feature in features
        )

    def _protections(self, view: MarketView) -> tuple[Any, ...]:
        if view.features is None:
            raise RouterInputError("MarketView.features is unavailable")
        features = view.features.get_all(
            PROTECTION_PLAN_FEATURE_KEY,
            symbol=view.current.symbol,
        )
        self._enforce_feature_limit(
            "protection decisions",
            len(features),
            self.config.max_decisions_per_type,
        )
        return tuple(
            self._normalizer.normalize_protection(
                feature.value,
                feature_schema_version=feature.schema_version,
                feature_created_at=feature.created_at,
                feature_valid_until=feature.valid_until,
                feature_id=feature.feature_id,
                symbol=view.current.symbol,
            )
            for feature in features
        )

    def _sizings(self, view: MarketView) -> tuple[Any, ...]:
        if view.features is None:
            raise RouterInputError("MarketView.features is unavailable")
        features = view.features.get_all(RISK_DECISION_FEATURE_KEY, symbol=view.current.symbol)
        self._enforce_feature_limit(
            "sizing decisions",
            len(features),
            self.config.max_decisions_per_type,
        )
        return tuple(
            self._normalizer.normalize_sizing(
                feature.value,
                feature_schema_version=feature.schema_version,
                feature_created_at=feature.created_at,
                feature_valid_until=feature.valid_until,
                feature_id=feature.feature_id,
                symbol=view.current.symbol,
            )
            for feature in features
        )

    def _scalings(self, view: MarketView) -> tuple[Any, ...]:
        if view.features is None:
            raise RouterInputError("MarketView.features is unavailable")
        features = view.features.get_all(
            SCALING_APPROVED_FEATURE_KEY,
            symbol=view.current.symbol,
        )
        self._enforce_feature_limit(
            "scaling decisions",
            len(features),
            self.config.max_decisions_per_type,
        )
        return tuple(
            self._normalizer.normalize_scaling(
                feature.value,
                feature_schema_version=feature.schema_version,
                feature_created_at=feature.created_at,
                feature_valid_until=feature.valid_until,
                feature_id=feature.feature_id,
                symbol=view.current.symbol,
            )
            for feature in features
        )

    @staticmethod
    def _enforce_feature_limit(label: str, actual: int, maximum: int) -> None:
        if actual > maximum:
            raise RouterInputError(
                f"{label} count {actual} exceeds configured maximum {maximum}",
                ReasonCode.INPUT_LIMIT_EXCEEDED,
            )

    def _portfolio(self, view: MarketView, as_of: datetime) -> PortfolioSnapshot:
        if view.features is None:
            raise RouterInputError("MarketView.features is unavailable")
        feature = view.features.latest(PORTFOLIO_SNAPSHOT_FEATURE_KEY)
        if feature is not None:
            return self._normalizer.normalize_portfolio(
                feature.value, feature_created_at=feature.created_at
            )
        if self.config.require_portfolio_snapshot:
            raise RouterInputError(
                "portfolio.snapshot is required", ReasonCode.PORTFOLIO_STATE_STALE
            )
        return self._fallback_portfolio(view, as_of)

    def _fallback_portfolio(self, view: MarketView, as_of: datetime) -> PortfolioSnapshot:
        as_of = _core_datetime(as_of, "fallback as_of")
        symbol = _core_text(view.current.symbol, "fallback symbol")
        equity = _core_decimal(view.equity, "view.equity")
        cash = _core_decimal(view.cash, "view.cash")
        positions: tuple[PositionState, ...] = ()
        if view.position is not None:
            side = _position_side(view.position.side)
            position_id = _core_text(view.position.id, "position.id")
            setup_id = _optional_core_text(view.position.setup_id, "position.setup_id")
            quantity = _core_decimal(view.position.quantity, "position.quantity")
            average_entry = _core_decimal(view.position.entry_price, "position.entry_price")
            opened_at = _core_datetime(view.position.entry_time, "position.entry_time")
            position_payload = {
                "id": position_id,
                "side": side.value,
                "quantity": quantity,
                "entry_price": average_entry,
                "setup_id": setup_id,
                "opened_at": opened_at,
            }
            positions = (
                PositionState(
                    position_id=position_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    average_entry=average_entry,
                    version=sha256_hex(position_payload),
                    setup_id=setup_id,
                    opened_at=opened_at,
                ),
            )
        pending: list[PendingOrderState] = []
        for order in view.pending_orders:
            status = _order_status(order.status)
            if status.terminal:
                continue
            side = _order_side(order.side)
            order_id = _core_text(order.id, "order.id")
            setup_id = _optional_core_text(order.setup_id, "order.setup_id")
            created_at = _core_datetime(order.created_at, "order.created_at")
            expires_at = (
                None
                if order.good_till_time is None
                else _core_datetime(order.good_till_time, "order.good_till_time")
            )
            quantity = _core_decimal(order.quantity, "order.quantity")
            filled_quantity = _core_decimal(order.filled_quantity, "order.filled_quantity")
            reported_remaining = _core_decimal(order.remaining_quantity, "order.remaining_quantity")
            try:
                derived_remaining = exact_subtract(quantity, filled_quantity)
            except DecimalDomainError as exc:
                raise RouterInputError(str(exc)) from exc
            if derived_remaining < 0:
                raise RouterInputError("pending order filled_quantity cannot exceed quantity")
            if reported_remaining > 0 and reported_remaining != derived_remaining:
                raise RouterInputError(
                    "pending order remaining_quantity conflicts with quantity and filled_quantity"
                )
            remaining_quantity = reported_remaining if reported_remaining > 0 else derived_remaining
            if remaining_quantity <= 0:
                raise RouterInputError("active pending order must have positive remaining quantity")
            order_type = _order_type(order.order_type)
            effective_limit_price = (
                order.limit_price
                if order.limit_price is not None
                else order.price
                if order_type in {"limit", "stop_limit", "take_profit_limit"}
                else None
            )
            effective_trigger_price = (
                order.trigger_price
                if order.trigger_price is not None
                else order.price
                if order_type in {"stop", "stop_market", "take_profit_market"}
                else None
            )
            effective_limit_decimal = (
                None
                if effective_limit_price is None
                else _core_decimal(
                    effective_limit_price,
                    "order.effective_limit_price",
                )
            )
            effective_trigger_decimal = (
                None
                if effective_trigger_price is None
                else _core_decimal(
                    effective_trigger_price,
                    "order.effective_trigger_price",
                )
            )
            payload = {
                "id": order_id,
                "status": status.value,
                "filled_quantity": filled_quantity,
                "quantity": quantity,
                "remaining_quantity": remaining_quantity,
                "order_type": order_type,
                "effective_limit_price": effective_limit_decimal,
                "effective_trigger_price": effective_trigger_decimal,
            }
            pending.append(
                PendingOrderState(
                    order_intent_id=order_id,
                    plan_id=None,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=remaining_quantity,
                    status=status.value,
                    version=sha256_hex(payload),
                    created_at=created_at,
                    price=effective_limit_decimal,
                    stop_price=effective_trigger_decimal,
                    reduce_only=order.reduce_only,
                    post_only=order.post_only,
                    setup_id=setup_id,
                    expires_at=expires_at,
                )
            )
        snapshot_payload = {
            "as_of": as_of,
            "cash": cash,
            "equity": equity,
            "position_versions": tuple(item.version for item in positions),
            "order_versions": tuple(item.version for item in pending),
        }
        snapshot_version = sha256_hex(snapshot_payload)
        try:
            available_risk_budget = exact_multiply(
                max(equity, Decimal("0")),
                self.config.max_portfolio_risk_fraction,
            )
        except DecimalDomainError as exc:
            raise RouterInputError(str(exc)) from exc
        return PortfolioSnapshot(
            portfolio_id="default",
            version=snapshot_version,
            timestamp=as_of,
            equity=equity,
            available_balance=max(cash, Decimal("0")),
            used_margin=Decimal("0"),
            free_margin=max(cash, Decimal("0")),
            risk_already_reserved=Decimal("0"),
            available_risk_budget=available_risk_budget,
            position_state_version=sha256_hex(tuple(item.version for item in positions)),
            order_state_version=sha256_hex(tuple(item.version for item in pending)),
            risk_reservation_version=sha256_hex({"reserved": "0", "snapshot": snapshot_version}),
            positions=positions,
            pending_orders=tuple(pending),
            metadata={"source": "market_view_fallback"},
        )

    def _decision(self, result: RoutingResult, view: MarketView) -> ModuleDecision:
        as_of = view.current.close_time
        common: dict[str, Any] = {
            "source_module": self.name,
            "created_at": as_of,
            "symbol": view.current.symbol,
            "timeframe": view.current.timeframe,
            "schema_version": FEATURE_SCHEMA_VERSION,
            "metadata": {
                "configuration_hash": self.config.fingerprint,
                "routing_result_id": result.result_id,
            },
        }
        routing = Feature(
            key=ROUTING_DECISION_FEATURE_KEY,
            value=result.to_mapping(),
            valid_until=as_of,
            feature_id=stable_id(
                "feature-routing", {"result": result.result_id, "key": ROUTING_DECISION_FEATURE_KEY}
            ),
            **common,
        )
        trace = Feature(
            key=CONFLICT_TRACE_FEATURE_KEY,
            value={
                "schema_version": FEATURE_SCHEMA_VERSION,
                "routing_result_id": result.result_id,
                "rejected_candidates": [
                    {
                        "candidate_id": item.candidate_id,
                        "setup_id": item.setup_id,
                        "reason_codes": [reason.value for reason in item.reason_codes],
                        "detail": item.detail,
                    }
                    for item in result.rejected_candidates
                ],
                "audit_trace": result.to_mapping()["audit_trace"],
            },
            valid_until=as_of,
            feature_id=stable_id(
                "feature-trace", {"result": result.result_id, "key": CONFLICT_TRACE_FEATURE_KEY}
            ),
            **common,
        )
        if result.verdict is PlanVerdict.ACCEPTED:
            if result.plan is None:
                raise PlanInvariantError("accepted routing result is missing executable plan")
            terminal = Feature(
                key=EXECUTABLE_PLAN_FEATURE_KEY,
                value=result.plan.to_mapping(),
                valid_until=result.plan.expires_at,
                feature_id=stable_id(
                    "feature-plan",
                    {"plan": result.plan.plan_id, "key": EXECUTABLE_PLAN_FEATURE_KEY},
                ),
                **common,
            )
        else:
            terminal = Feature(
                key=REJECTED_PLAN_FEATURE_KEY,
                value=result.to_mapping(),
                valid_until=as_of,
                feature_id=stable_id(
                    "feature-rejection",
                    {"result": result.result_id, "key": REJECTED_PLAN_FEATURE_KEY},
                ),
                **common,
            )
        return ModuleDecision(
            features=(routing, trace, terminal),
            skips=(
                ()
                if result.verdict is PlanVerdict.ACCEPTED
                else tuple(reason.value for reason in result.reason_codes)
            ),
            diagnostics={
                "result_id": result.result_id,
                "verdict": result.verdict.value,
                "configuration_hash": self.config.fingerprint,
                "plan_id": None if result.plan is None else result.plan.plan_id,
            },
        )

    def _input_error_result(
        self,
        *,
        as_of: datetime,
        portfolio_id: str,
        reason: ReasonCode,
        detail: str,
    ) -> RoutingResult:
        return RoutingResult.create(
            verdict=PlanVerdict.REJECTED,
            created_at=as_of,
            portfolio_id=portfolio_id,
            configuration_hash=self.config.fingerprint,
            plan=None,
            reason_codes=(reason,),
            rejected_candidates=(),
            audit_trace=(
                AuditEvent(
                    sequence=1,
                    stage="input_normalization",
                    outcome="rejected",
                    reason_code=reason,
                    detail={"detail": detail},
                ),
            ),
            warnings=(),
        )
