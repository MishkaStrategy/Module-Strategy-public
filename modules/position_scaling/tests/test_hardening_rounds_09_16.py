from __future__ import annotations


def test_round_09_regime_configuration_is_strict_and_canonical() -> None:
    from modular_strategy_position_scaling import PositionScalingConfig

    first = PositionScalingConfig.from_mapping(
        {"require_market_regime": True, "allowed_market_regimes": ["trend", "range"]}
    )
    second = PositionScalingConfig.from_mapping(
        {"require_market_regime": True, "allowed_market_regimes": ["range", "trend"]}
    )
    assert first.allowed_market_regimes == ("range", "trend")
    assert first.fingerprint == second.fingerprint
    for invalid in (["trend", "trend"], [""], [" trend"], [True]):
        try:
            PositionScalingConfig.from_mapping({"allowed_market_regimes": invalid})
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid regimes accepted: {invalid!r}")


def test_round_10_instrument_fallback_is_deeply_immutable() -> None:
    from modular_strategy_position_scaling import PositionScalingConfig

    source = {
        "quantity_step": "0.001",
        "contract_size": "1",
        "metadata": {"venue": "test"},
    }
    config = PositionScalingConfig.from_mapping({"instrument_rules": source})
    before = config.fingerprint
    source["metadata"]["venue"] = "mutated"
    assert config.instrument_rules is not None
    assert config.instrument_rules["metadata"]["venue"] == "test"
    assert config.fingerprint == before
    try:
        config.instrument_rules["quantity_step"] = "1"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("frozen instrument_rules must reject mutation")


def test_round_11_restore_rejects_lifecycle_pending_mismatch() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    )
    planner.on_bar(make_view(close=98, position=make_position(), features=standard_features()))
    state = dict(planner.snapshot_state())
    state["lifecycle"] = "eligible"
    try:
        PositionScalingPlannerModule(
            enabled=True,
            allow_averaging_down=True,
            rules=[
                {
                    "stage": 1,
                    "mode": "averaging_down",
                    "action": "add",
                    "trigger_kind": "adverse_percent",
                    "trigger_value": "0.01",
                    "quantity_kind": "fixed",
                    "quantity_value": "0.1",
                }
            ],
        ).restore_state(state)
    except ValueError:
        pass
    else:
        raise AssertionError("pending plan with non-pending lifecycle must be rejected")


def test_round_11_unapproved_position_change_fails_closed() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    )
    planner.on_bar(make_view(close=98, position=make_position(), features=standard_features()))
    decision = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.1, entry_price=99.8, entry_fill_count=2),
            features=standard_features(1),
        )
    )
    assert decision.skips == ("adjustment_not_approved",)
    assert planner.snapshot_state()["lifecycle"] == "recovery_required"


def test_round_12_stop_scaling_is_terminal_and_restart_safe() -> None:
    from conftest import make_position, make_view

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    parameters = {
        "enabled": True,
        "require_setup_validity": False,
        "require_liquidation_distance": False,
        "require_margin_usage": False,
        "rules": [
            {
                "stage": 1,
                "mode": "scale_out",
                "action": "stop_scaling",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": 1,
                "expires_bars": 0,
            }
        ],
    }
    planner = PositionScalingPlannerModule(**parameters)
    planner.on_bar(make_view(position=make_position()))
    snapshot = planner.snapshot_state()
    assert snapshot["lifecycle"] == "stopped"
    assert snapshot["pending_plan"] is None
    assert snapshot["completed_stages"] == [1]

    restored = PositionScalingPlannerModule(**parameters)
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    decision = restored.on_bar(make_view(index=1, position=make_position()))
    assert decision.skips == ("stop_scaling_requested",)


def test_round_13_planner_checkpoint_has_integrity_fingerprint() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    parameters = {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    }
    planner = PositionScalingPlannerModule(**parameters)
    planner.on_bar(make_view(close=98, position=make_position(), features=standard_features()))
    checkpoint = dict(planner.snapshot_state())
    assert checkpoint["schema_version"] == "4"
    assert isinstance(checkpoint["state_fingerprint"], str)

    tampered = dict(checkpoint)
    tampered["last_seen_index"] = 999
    try:
        PositionScalingPlannerModule(**parameters).restore_state(tampered)
    except ValueError as exc:
        assert "fingerprint mismatch" in str(exc)
    else:
        raise AssertionError("tampered checkpoint must fail integrity validation")


def test_round_14_approval_checkpoint_integrity_and_atomic_restore() -> None:
    from modular_strategy_position_scaling import PositionScalingApprovalModule

    module = PositionScalingApprovalModule(max_processed_adjustments=2)
    checkpoint = dict(module.snapshot_state())
    assert checkpoint["schema_version"] == "4"
    tampered = dict(checkpoint)
    tampered["last_index"] = 10
    before = module.snapshot_state()
    try:
        module.restore_state(tampered)
    except ValueError as exc:
        assert "fingerprint mismatch" in str(exc)
    else:
        raise AssertionError("tampered approval checkpoint must be rejected")
    assert module.snapshot_state() == before

    legacy = dict(checkpoint)
    legacy["schema_version"] = "3"
    legacy.pop("state_fingerprint")
    restored = PositionScalingApprovalModule(max_processed_adjustments=2)
    restored.restore_state(legacy)
    assert restored.snapshot_state()["schema_version"] == "4"


def _round_feedback_fixture(quantity: str = "0.1") -> tuple[object, object, object]:
    from conftest import feature, make_position, make_view, standard_features

    from modular_strategy_position_scaling import (
        FEATURE_APPROVED,
        FEATURE_PLAN,
        FEATURE_RISK_DECISION,
        PositionScalingApprovalModule,
        PositionScalingPlannerModule,
    )

    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "expires_bars": 5,
            }
        ],
    )
    plan = next(
        item
        for item in planner.on_bar(
            make_view(close=98, position=make_position(), features=standard_features())
        ).features
        if item.key == FEATURE_PLAN
    )
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan.value["risk_request_id"],
            "symbol": plan.value["symbol"],
            "status": "approved",
            "approved": True,
            "quantity": quantity,
            "setup_id": plan.value["setup_id"],
        },
        source="risk_sizing",
        feature_id=f"risk-{quantity}",
    )
    approved = next(
        item
        for item in PositionScalingApprovalModule()
        .on_bar(make_view(close=98, position=make_position(), features=(plan, risk)))
        .features
        if item.key == FEATURE_APPROVED
    )
    return planner, plan, approved


def test_round_15_duplicate_feedback_after_restore_is_idempotent() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    planner, _, approved = _round_feedback_fixture()
    first_view = make_view(
        index=1,
        close=98,
        position=make_position(),
        features=(*standard_features(1), approved),
    )
    planner.on_bar(first_view)
    checkpoint = planner.snapshot_state()
    history_size = len(checkpoint["history"])

    restored = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "expires_bars": 5,
            }
        ],
    )
    restored.restore_state(checkpoint)
    duplicate = restored.on_bar(first_view)
    state = restored.snapshot_state()
    assert duplicate.skips == ("pending_adjustment",)
    assert state["lifecycle"] == "approved"
    assert len(state["history"]) == history_size


def test_round_16_conflicting_feedback_and_out_of_order_replay_fail_closed() -> None:
    from conftest import make_position, make_view, standard_features

    from modular_strategy_position_scaling import PositionScalingPlannerModule

    planner, plan, approved = _round_feedback_fixture("0.1")
    _, same_plan, conflicting = _round_feedback_fixture("0.05")
    assert same_plan.value["adjustment_id"] == plan.value["adjustment_id"]
    planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), approved),
        )
    )
    checkpoint = planner.snapshot_state()
    parameters = {
        "enabled": True,
        "allow_averaging_down": True,
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "expires_bars": 5,
            }
        ],
    }
    restored = PositionScalingPlannerModule(**parameters)
    restored.restore_state(checkpoint)
    conflict = restored.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), conflicting),
        )
    )
    assert conflict.skips == ("conflict",)
    assert restored.snapshot_state()["lifecycle"] == "recovery_required"

    ordered = PositionScalingPlannerModule(**parameters)
    ordered.restore_state(checkpoint)
    out_of_order = ordered.on_bar(
        make_view(index=0, close=98, position=make_position(), features=standard_features())
    )
    assert out_of_order.skips == ("out_of_order",)
    state = ordered.snapshot_state()
    assert state["last_seen_index"] == 1
    assert state["lifecycle"] == "approved"
