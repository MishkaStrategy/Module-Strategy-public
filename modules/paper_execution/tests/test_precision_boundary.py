from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from paper_execution import PaperReasonCode
from paper_execution.exceptions import VenueRuleRejection
from paper_execution.venue import VenueRuleEngine


def _normalize(*, intent, metadata, now, reference_price=Decimal("100000")):
    return VenueRuleEngine().normalize(
        intent=intent,
        metadata=metadata,
        client_order_id="pevr-precision-boundary",
        reference_price=reference_price,
        event_time=now,
    )


def test_standard_decimal_values_cross_core_boundary_losslessly(
    plan,
    metadata,
    now,
) -> None:
    result = _normalize(
        intent=plan.intents[0],
        metadata=metadata,
        now=now,
    )

    assert result.audit.requested_quantity == plan.intents[0].quantity
    assert result.audit.normalized_quantity % metadata.quantity_step == 0
    assert result.audit.normalized_price is not None
    assert result.audit.normalized_price % metadata.tick_size == 0
    assert result.audit.rounding_mode == "core_exchange_rules_adapter_lossless_float_boundary"


def test_lossy_intent_quantity_is_rejected_before_core_normalization(
    plan,
    metadata,
    now,
) -> None:
    intent = replace(
        plan.intents[0],
        quantity=Decimal("0.12345678901234567890123456789"),
    )

    with pytest.raises(VenueRuleRejection, match="represented losslessly") as exc:
        _normalize(intent=intent, metadata=metadata, now=now)

    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED


def test_lossy_metadata_tick_is_rejected_before_core_normalization(
    plan,
    metadata,
    now,
) -> None:
    lossy_metadata = replace(
        metadata,
        tick_size=Decimal("0.12345678901234567890123456789"),
    )

    with pytest.raises(VenueRuleRejection, match="represented losslessly") as exc:
        _normalize(
            intent=plan.intents[0],
            metadata=lossy_metadata,
            now=now,
        )

    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED


def test_underflow_reference_is_rejected_instead_of_becoming_zero(
    plan,
    metadata,
    now,
) -> None:
    with pytest.raises(VenueRuleRejection, match="Core float boundary") as exc:
        _normalize(
            intent=plan.intents[0],
            metadata=metadata,
            now=now,
            reference_price=Decimal("1e-1000"),
        )

    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED


def test_overflow_metadata_is_rejected_instead_of_becoming_infinite(
    plan,
    metadata,
    now,
) -> None:
    overflow_metadata = replace(
        metadata,
        max_notional=Decimal("1e10000"),
    )

    with pytest.raises(VenueRuleRejection, match="not finite") as exc:
        _normalize(
            intent=plan.intents[0],
            metadata=overflow_metadata,
            now=now,
        )

    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED
