from __future__ import annotations

from decimal import Decimal

import pytest

from paper_execution.exceptions import OrderStateError
from paper_execution.oco import OcoAdjustment, OcoAdjustmentKind, OcoGroup


def test_partial_target_fill_reduces_stop_to_residual() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target-1", Decimal("0.5"))

    assert group.record_fill("target-1", Decimal("0.5")) == ()
    assert group.remaining_position_quantity == Decimal("0.5")
    assert group.required_adjustments() == (
        OcoAdjustment(
            "stop",
            OcoAdjustmentKind.REDUCE_REMAINING_TO,
            Decimal("0.5"),
        ),
    )


def test_full_position_close_cancels_all_unfilled_siblings() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("1"))
    assert group.record_fill("target", Decimal("1")) == ("stop",)
    assert group.required_adjustments() == (OcoAdjustment("stop", OcoAdjustmentKind.CANCEL),)


def test_duplicate_fill_and_cancel_are_idempotent() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("1"))
    group.record_fill("target", Decimal("0.25"))
    assert group.record_fill("target", Decimal("0.25")) == ()
    assert group.record_canceled("stop")
    assert not group.record_canceled("stop")


def test_fill_after_cancel_and_overclose_fail_closed() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("1"))
    group.record_canceled("stop")
    with pytest.raises(OrderStateError, match="canceled OCO member"):
        group.record_fill("stop", Decimal("0.1"))

    second = OcoGroup("g2", Decimal("1"))
    second.register("stop", Decimal("1"))
    second.register("target", Decimal("1"))
    second.record_fill("target", Decimal("0.6"))
    with pytest.raises(OrderStateError, match="over-close"):
        second.record_fill("stop", Decimal("0.5"))
    assert second.filled["stop"] == Decimal("0")


def test_snapshot_restore_preserves_partial_fill_and_cancel_state() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("0.5"))
    group.record_fill("target", Decimal("0.25"))
    group.record_canceled("target")

    snapshot = group.snapshot_state()
    restored = OcoGroup.from_state(snapshot)

    assert restored.snapshot_state() == snapshot
    assert restored.filled == group.filled
    assert restored.canceled == group.canceled
    assert restored.required_adjustments() == group.required_adjustments()


def test_snapshot_restore_rejects_tampering_and_invalid_membership() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    snapshot = dict(group.snapshot_state())

    tampered = dict(snapshot)
    tampered["position_quantity"] = "2"
    with pytest.raises(ValueError, match="hash mismatch"):
        OcoGroup.from_state(tampered)

    malformed = dict(snapshot)
    malformed["extra"] = True
    with pytest.raises(ValueError, match="state fields"):
        OcoGroup.from_state(malformed)
