from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from paper_execution import (
    ExecutionIntent,
    IntentRole,
    PaperReasonCode,
    VenueRuleEngine,
)
from paper_execution.exceptions import VenueRuleRejection


def test_client_order_id_is_strictly_bounded(entry_intent, metadata, now) -> None:
    for client_order_id in ("", "unsafe/id", "x" * 129):
        with pytest.raises(VenueRuleRejection, match="client_order_id"):
            VenueRuleEngine().normalize(
                intent=entry_intent,
                metadata=metadata,
                client_order_id=client_order_id,
                reference_price=Decimal("100000"),
                event_time=now,
            )


def test_normalize_requires_contract_instances(entry_intent, metadata, now) -> None:
    with pytest.raises(TypeError, match="intent must be"):
        VenueRuleEngine().normalize(
            intent=object(),
            metadata=metadata,
            client_order_id="safe-id",
            reference_price=Decimal("100000"),
            event_time=now,
        )
    with pytest.raises(TypeError, match="metadata must be"):
        VenueRuleEngine().normalize(
            intent=entry_intent,
            metadata=object(),
            client_order_id="safe-id",
            reference_price=Decimal("100000"),
            event_time=now,
        )


def _huge_intent(entry_intent):
    return replace(
        entry_intent,
        quantity=Decimal("1E+100000"),
        limit_price=Decimal("1E+100000"),
    )


def _normalize_huge(entry_intent, metadata, now):
    return VenueRuleEngine().normalize(
        intent=_huge_intent(entry_intent),
        metadata=metadata,
        client_order_id="safe-id",
        reference_price=Decimal("1E+100000"),
        event_time=now,
    )


def test_oversized_decimal_intent_is_constructible(entry_intent) -> None:
    huge = _huge_intent(entry_intent)
    assert huge.quantity == Decimal("1E+100000")


def test_decimal_to_float_overflow_is_typed_venue_rejection(
    entry_intent,
    metadata,
    now,
) -> None:
    with pytest.raises(VenueRuleRejection) as exc:
        _normalize_huge(entry_intent, metadata, now)
    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED
    assert isinstance(exc.value.__cause__, ValueError)


def test_inactive_metadata_is_rejected_before_core_conversion(
    entry_intent,
    metadata,
    now,
) -> None:
    future = replace(
        metadata,
        effective_from=now + timedelta(minutes=1),
    )
    with pytest.raises(VenueRuleRejection, match="not effective"):
        VenueRuleEngine().normalize(
            intent=entry_intent,
            metadata=future,
            client_order_id="safe-id",
            reference_price=Decimal("100000"),
            event_time=now,
        )


def test_cancel_intent_never_reaches_order_normalization(metadata, now) -> None:
    cancel = ExecutionIntent(
        intent_id="cancel",
        role=IntentRole.CANCEL,
        cancel_client_order_id="safe-id",
    )
    with pytest.raises(VenueRuleRejection, match="cancel intent"):
        VenueRuleEngine().normalize(
            intent=cancel,
            metadata=metadata,
            client_order_id="safe-id",
            reference_price=Decimal("100000"),
            event_time=now,
        )
