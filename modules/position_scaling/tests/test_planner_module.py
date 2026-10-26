from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from conftest import feature, make_order, make_position, make_view, standard_features
from modular_core import PositionSide

from modular_strategy_position_scaling import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_RISK_DECISION,
    FEATURE_RISK_REQUEST,
    FEATURE_STATUS,
    PositionAdjustmentPlan,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.canonical import fingerprint
from modular_strategy_position_scaling.enums import ReasonCode, ScalingLifecycle


def feature_by_key(decision: Any, key: str) -> Any:
    return next(item for item in decision.features if item.key == key)


def test_disabled_module_attaches_but_emits_no_plan() -> None:
    module = PositionScalingPlannerModule(enabled=False)
    decision = module.on_bar(make_view(position=make_position(), features=standard_features()))
    assert FEATURE_PLAN not in {item.key for item in decision.features}
    assert ReasonCode.SCALING_DISABLED.value in decision.skips
    state = module.snapshot_state()
    assert state["position_id"] == "position-1"


def test_averaging_plan_and_risk_request_emitted(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    decision = module.on_bar(
        make_view(
            close=98,
            position=make_position(pnl=-20),
            features=standard_features(),
        )
    )
    keys = {item.key for item in decision.features}
    assert {FEATURE_PLAN, FEATURE_RISK_REQUEST, FEATURE_STATUS} <= keys
    plan = PositionAdjustmentPlan.from_mapping(feature_by_key(decision, FEATURE_PLAN).value)
    request = feature_by_key(decision, FEATURE_RISK_REQUEST).value
    assert plan.desired_quantity == Decimal("0.25")
    assert request["request_id"] == plan.risk_request_id
    assert request["metadata"]["adjustment_id"] == plan.adjustment_id
    assert decision.orders == ()
    assert decision.position_updates == ()
    assert decision.closes == ()


def test_short_averaging_uses_sell_risk_side(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    decision = module.on_bar(
        make_view(
            close=102,
            position=make_position(
                side=PositionSide.SHORT,
                stop_loss=105,
                pnl=-20,
            ),
            features=standard_features(),
        )
    )
    request = feature_by_key(decision, FEATURE_RISK_REQUEST).value
    assert request["side"] == "sell"


def test_missing_instrument_rules_fails_closed(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    features = tuple(item for item in standard_features() if item.key != "instrument.rules")
    decision = module.on_bar(make_view(close=98, position=make_position(), features=features))
    assert ReasonCode.INVALID_RULE.value in decision.skips
    assert FEATURE_RISK_REQUEST not in {item.key for item in decision.features}


def test_instrument_rules_config_fallback(
    averaging_parameters: Mapping[str, Any],
) -> None:
    parameters = dict(averaging_parameters)
    parameters["instrument_rules"] = {
        "quantity_step": "0.001",
        "min_quantity": "0.001",
        "min_notional": "5",
        "contract_size": "1",
    }
    module = PositionScalingPlannerModule(**parameters)
    features = tuple(item for item in standard_features() if item.key != "instrument.rules")
    decision = module.on_bar(make_view(close=98, position=make_position(), features=features))
    assert FEATURE_RISK_REQUEST in {item.key for item in decision.features}


def test_data_gap_blocks_addition_but_keeps_state(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    decision = module.on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=standard_features(),
            data_gap=True,
        )
    )
    assert ReasonCode.DATA_GAP.value in decision.skips
    assert module.snapshot_state()["position_id"] == "position-1"


def test_exit_pending_blocks_addition(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    decision = module.on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=standard_features(),
            exit_pending=True,
        )
    )
    assert ReasonCode.EXIT_PENDING.value in decision.skips


def test_conflicting_pending_order_blocks_addition(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    decision = module.on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=standard_features(),
            pending_orders=(make_order(),),
        )
    )
    assert ReasonCode.CONFLICTING_PENDING_ORDER.value in decision.skips


def test_pending_plan_is_not_reissued(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    view = make_view(close=98, position=make_position(), features=standard_features())
    first = module.on_bar(view)
    second = module.on_bar(view)
    assert FEATURE_PLAN in {item.key for item in first.features}
    assert FEATURE_PLAN not in {item.key for item in second.features}
    assert ReasonCode.PENDING_ADJUSTMENT.value in second.skips


def test_plan_expires_and_can_be_replanned_next_bar(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    first = module.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    first_plan = PositionAdjustmentPlan.from_mapping(feature_by_key(first, FEATURE_PLAN).value)
    second = module.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=standard_features(1),
        )
    )
    second_plan = PositionAdjustmentPlan.from_mapping(feature_by_key(second, FEATURE_PLAN).value)
    assert first_plan.adjustment_id != second_plan.adjustment_id
    assert module.snapshot_state()["counters"]["expired"] == 1


def test_fill_reconciliation_updates_stage_and_cooldown(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    planned = module.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    plan = feature_by_key(planned, FEATURE_PLAN)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": plan.value["desired_quantity"],
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
    )
    approved = feature_by_key(
        PositionScalingApprovalModule().on_bar(
            make_view(close=98, position=make_position(), features=(plan, risk))
        ),
        FEATURE_APPROVED,
    )
    changed = make_position(
        quantity=1.25,
        entry_price=99.6,
        entry_fill_count=2,
    )
    decision = module.on_bar(
        make_view(
            index=1,
            close=99,
            position=changed,
            features=(*standard_features(1), approved),
        )
    )
    state = module.snapshot_state()
    assert state["scale_in_count"] == 1
    assert state["completed_stages"] == [1]
    assert state["pending_plan"] is None
    assert state["lifecycle"] in {
        ScalingLifecycle.RECONCILED.value,
        ScalingLifecycle.COOLDOWN.value,
    }
    assert FEATURE_PLAN not in {item.key for item in decision.features}


def test_external_position_change_is_recorded_without_stage_completion(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    module.on_bar(make_view(close=100, position=make_position(), features=standard_features()))
    module.restore_state(
        {
            **module.snapshot_state(),
            "pending_plan": None,
            "pending_fingerprint": None,
            "lifecycle": "eligible",
        }
    )
    module.on_bar(
        make_view(
            index=1,
            close=100,
            position=make_position(quantity=0.9),
            features=standard_features(1),
        )
    )
    state = module.snapshot_state()
    assert state["completed_stages"] == []
    assert state["counters"]["external_changes"] == 1


def test_position_identity_change_enters_recovery_required(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    module.on_bar(make_view(close=100, position=make_position(), features=standard_features()))
    decision = module.on_bar(
        make_view(
            index=1,
            close=100,
            position=make_position(position_id="position-2"),
            features=standard_features(1),
        )
    )
    assert ReasonCode.POSITION_CHANGED.value in decision.skips
    assert module.snapshot_state()["lifecycle"] == ScalingLifecycle.RECOVERY_REQUIRED.value


def test_flat_position_resets_position_state(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    module.on_bar(make_view(close=100, position=make_position(), features=standard_features()))
    decision = module.on_bar(make_view(index=1, position=None))
    assert ReasonCode.NO_OPEN_POSITION.value in decision.skips
    state = module.snapshot_state()
    assert state["position_id"] is None
    assert state["initial_quantity"] == "0"


def test_out_of_order_bar_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    module.on_bar(
        make_view(index=2, close=100, position=make_position(), features=standard_features(2))
    )
    decision = module.on_bar(
        make_view(index=1, close=100, position=make_position(), features=standard_features(1))
    )
    assert ReasonCode.OUT_OF_ORDER.value in decision.skips


def test_snapshot_restore_is_atomic_and_deterministic(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    module.on_bar(make_view(close=98, position=make_position(), features=standard_features()))
    snapshot = module.snapshot_state()
    restored = PositionScalingPlannerModule(**averaging_parameters)
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    ).skips == (ReasonCode.PENDING_ADJUSTMENT.value,)
    damaged = deepcopy(snapshot)
    damaged["schema_version"] = "999"
    before = restored.snapshot_state()
    with pytest.raises(ValueError):
        restored.restore_state(damaged)
    assert restored.snapshot_state() == before


def test_restore_rejects_config_mismatch(
    averaging_parameters: Mapping[str, Any],
) -> None:
    module = PositionScalingPlannerModule(**averaging_parameters)
    snapshot = module.snapshot_state()
    different = dict(averaging_parameters)
    different["max_scale_in_count"] = 1
    other = PositionScalingPlannerModule(**different)
    with pytest.raises(ValueError, match="fingerprint"):
        other.restore_state(snapshot)


def test_restore_rejects_oversized_history(
    averaging_parameters: Mapping[str, Any],
) -> None:
    parameters = dict(averaging_parameters)
    parameters["max_history"] = 2
    module = PositionScalingPlannerModule(**parameters)
    state = dict(module.snapshot_state())
    state["history"] = [{}, {}, {}]
    state["state_fingerprint"] = fingerprint(
        {key: value for key, value in state.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="oversized"):
        module.restore_state(state)


def test_status_feature_is_namespaced_and_bounded(
    averaging_parameters: Mapping[str, Any],
) -> None:
    parameters = dict(averaging_parameters)
    parameters["max_processed_events"] = 2
    module = PositionScalingPlannerModule(**parameters)
    for index in range(5):
        module.on_bar(
            make_view(
                index=index,
                close=98,
                position=make_position(),
                features=standard_features(index),
            )
        )
    state = module.snapshot_state()
    assert len(state["processed_event_ids"]) <= 2
