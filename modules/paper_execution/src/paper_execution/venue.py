from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException
import math
import re

from modular_core import OrderRequest, Side
from modular_core.instruments import (
    ExchangeRulesAdapter,
    InstrumentRuleError,
    InstrumentSpec,
)

from ._utils import decimal
from .contracts import (
    ExecutionIntent,
    NormalizationAudit,
    NormalizedOrder,
    VenueMetadataSnapshot,
)
from .enums import IntentRole, PaperReasonCode
from .exceptions import VenueRuleRejection

_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _to_core_float(
    value: Decimal,
    name: str,
    *,
    allow_zero: bool = False,
) -> float:
    """Convert Decimal into the Core float contract without silent mutation."""

    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} is not finite at the Core float boundary")
    if converted < 0 or (converted == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must remain {qualifier} at the Core float boundary")
    if Decimal(str(converted)) != value:
        raise ValueError(f"{name} cannot be represented losslessly at the Core float boundary")
    return converted


def _from_core_float(value: float, name: str) -> Decimal:
    """Recover a canonical Decimal from a finite Core float result."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} returned by Core must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} returned by Core must be finite")
    return Decimal(str(converted))


def _require_step_alignment(value: Decimal, step: Decimal, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} returned by Core must be positive")
    if value % step != 0:
        raise ValueError(f"{name} returned by Core is not aligned to venue metadata")


def _core_spec(metadata: VenueMetadataSnapshot) -> InstrumentSpec:
    """Build Core metadata only after every Decimal survives lossless conversion."""

    return InstrumentSpec(
        symbol=metadata.instrument,
        base_asset=metadata.base_asset,
        quote_asset=metadata.quote_asset,
        market_type=metadata.market_type,
        tick_size=_to_core_float(metadata.tick_size, "tick_size"),
        quantity_step=_to_core_float(
            metadata.quantity_step,
            "quantity_step",
        ),
        min_quantity=_to_core_float(
            metadata.min_quantity,
            "min_quantity",
            allow_zero=True,
        ),
        max_quantity=(
            _to_core_float(metadata.max_quantity, "max_quantity")
            if metadata.max_quantity is not None
            else None
        ),
        min_notional=_to_core_float(
            metadata.min_notional,
            "min_notional",
            allow_zero=True,
        ),
        max_notional=(
            _to_core_float(metadata.max_notional, "max_notional")
            if metadata.max_notional is not None
            else None
        ),
        contract_size=_to_core_float(
            metadata.contract_multiplier,
            "contract_multiplier",
        ),
    )


class VenueRuleEngine:
    def normalize(
        self,
        *,
        intent: ExecutionIntent,
        metadata: VenueMetadataSnapshot,
        client_order_id: str,
        reference_price: Decimal,
        event_time: datetime,
    ) -> NormalizedOrder:
        if not isinstance(intent, ExecutionIntent):
            raise TypeError("intent must be ExecutionIntent")
        if not isinstance(metadata, VenueMetadataSnapshot):
            raise TypeError("metadata must be VenueMetadataSnapshot")
        if (
            not isinstance(client_order_id, str)
            or _CLIENT_ORDER_ID.fullmatch(client_order_id) is None
        ):
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                "client_order_id contains unsafe characters or length",
            )
        if intent.role is IntentRole.CANCEL:
            raise VenueRuleRejection(
                PaperReasonCode.INVALID_PLAN,
                "cancel intent cannot be normalized as an order",
            )
        if not metadata.active_at(event_time):
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                "metadata snapshot is not effective at event time",
            )
        if intent.order_type is None or intent.side is None:
            raise VenueRuleRejection(
                PaperReasonCode.INVARIANT_VIOLATION,
                "validated order intent is incomplete",
            )
        if not intent.close_position and intent.quantity is None:
            raise VenueRuleRejection(
                PaperReasonCode.INVARIANT_VIOLATION,
                "validated non-close order intent is missing quantity",
            )
        if intent.order_type not in metadata.permitted_order_types:
            raise VenueRuleRejection(
                PaperReasonCode.UNSUPPORTED_PAPER_ORDER_TYPE,
                intent.order_type.value,
            )
        if intent.time_in_force not in metadata.permitted_time_in_force:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                "time_in_force is not permitted",
            )
        if intent.post_only and not metadata.post_only_supported:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                "post_only is not supported",
            )
        if intent.reduce_only and not metadata.reduce_only_supported:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                "reduce_only is not supported",
            )
        reference = decimal(
            reference_price,
            "reference_price",
            positive=True,
        )
        requested_price = intent.limit_price or intent.trigger_price
        try:
            quantity_float = (
                _to_core_float(intent.quantity, "quantity") if intent.quantity is not None else None
            )
            limit_price_float = (
                _to_core_float(intent.limit_price, "limit_price")
                if intent.limit_price is not None
                else None
            )
            trigger_price_float = (
                _to_core_float(intent.trigger_price, "trigger_price")
                if intent.trigger_price is not None
                else None
            )
            reference_float = _to_core_float(reference, "reference_price")
            raw = OrderRequest(
                side=intent.side,
                order_type=intent.order_type,
                quantity=quantity_float,
                limit_price=limit_price_float,
                trigger_price=trigger_price_float,
                time_in_force=intent.time_in_force,
                post_only=intent.post_only,
                reduce_only=intent.reduce_only,
                close_position=intent.close_position,
                client_order_id=client_order_id,
                position_side=intent.position_side,
                metadata={
                    **dict(intent.metadata),
                    "intent_id": intent.intent_id,
                    "intent_role": intent.role.value,
                    "oco_group_id": intent.oco_group_id,
                    "metadata_version": metadata.metadata_version,
                },
            )
            adapter = ExchangeRulesAdapter(_core_spec(metadata))
            normalized = adapter.normalize_order(
                raw,
                reference_price=reference_float,
            )
            effective_price = (
                normalized.effective_limit_price
                or normalized.effective_trigger_price
                or reference_float
            )
            normalized_quantity_float = (
                adapter.normalize_quantity(
                    quantity_float,
                    price=effective_price,
                )
                if quantity_float is not None
                else None
            )
        except (
            DecimalException,
            InstrumentRuleError,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                str(exc),
            ) from exc
        normalized = OrderRequest(
            side=normalized.side,
            order_type=normalized.order_type,
            quantity=normalized_quantity_float,
            limit_price=normalized.limit_price,
            trigger_price=normalized.trigger_price,
            time_in_force=normalized.time_in_force,
            post_only=normalized.post_only,
            reduce_only=normalized.reduce_only,
            close_position=normalized.close_position,
            client_order_id=normalized.client_order_id,
            position_side=normalized.position_side,
            metadata=normalized.metadata,
        )
        normalized_price_float = (
            normalized.effective_limit_price or normalized.effective_trigger_price
        )
        normalized_price = (
            _from_core_float(normalized_price_float, "normalized_price")
            if normalized_price_float is not None
            else None
        )
        quantity = (
            _from_core_float(
                normalized_quantity_float,
                "normalized_quantity",
            )
            if normalized_quantity_float is not None
            else None
        )
        try:
            if quantity is not None:
                _require_step_alignment(
                    quantity,
                    metadata.quantity_step,
                    "normalized_quantity",
                )
            if normalized_price is not None:
                _require_step_alignment(
                    normalized_price,
                    metadata.tick_size,
                    "normalized_price",
                )
        except ValueError as exc:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                str(exc),
            ) from exc
        self._validate_semantics(
            intent.role,
            intent.side,
            normalized_price,
            reference,
        )
        notional = None
        if quantity is not None:
            notional_price = normalized_price or reference
            notional = quantity * notional_price * metadata.contract_multiplier
            if quantity < metadata.min_quantity:
                raise VenueRuleRejection(
                    PaperReasonCode.VENUE_RULE_REJECTED,
                    "normalized quantity is below venue minimum",
                )
            if metadata.max_quantity is not None and quantity > metadata.max_quantity:
                raise VenueRuleRejection(
                    PaperReasonCode.VENUE_RULE_REJECTED,
                    "normalized quantity exceeds venue maximum",
                )
            if notional < metadata.min_notional:
                raise VenueRuleRejection(
                    PaperReasonCode.VENUE_RULE_REJECTED,
                    "normalized notional is below venue minimum",
                )
            if metadata.max_notional is not None and notional > metadata.max_notional:
                raise VenueRuleRejection(
                    PaperReasonCode.VENUE_RULE_REJECTED,
                    "normalized notional exceeds venue maximum",
                )
        audit = NormalizationAudit(
            intent_id=intent.intent_id,
            requested_quantity=intent.quantity,
            normalized_quantity=quantity,
            requested_price=requested_price,
            normalized_price=normalized_price,
            notional=notional,
            metadata_version=metadata.metadata_version,
            rounding_mode="core_exchange_rules_adapter_lossless_float_boundary",
        )
        return NormalizedOrder(
            request=normalized,
            audit=audit,
            client_order_id=client_order_id,
            role=intent.role,
            oco_group_id=intent.oco_group_id,
        )

    @staticmethod
    def _validate_semantics(
        role: IntentRole,
        side: Side,
        price: Decimal | None,
        reference: Decimal,
    ) -> None:
        if price is None or role in {
            IntentRole.ENTRY,
            IntentRole.REPLACEMENT,
        }:
            return
        if role is IntentRole.STOP:
            valid = price < reference if side is Side.SELL else price > reference
        elif role is IntentRole.TARGET:
            valid = price > reference if side is Side.SELL else price < reference
        else:
            valid = True
        if not valid:
            raise VenueRuleRejection(
                PaperReasonCode.VENUE_RULE_REJECTED,
                f"{role.value} price is on the unsafe side of reference",
            )
