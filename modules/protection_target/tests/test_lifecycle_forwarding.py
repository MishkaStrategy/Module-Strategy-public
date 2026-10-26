from __future__ import annotations

from conftest import make_bar, make_entry_feature, make_view

from modular_strategy_protection_target import ProtectionTargetModule


def _module() -> ProtectionTargetModule:
    return ProtectionTargetModule(
        invalidation_mode="entry_plan",
        invalidation_field="metadata.source_setup_metadata.invalidation_price",
        stop_buffer_bps="0",
        target_mode="structural",
        structural_target_source="entry_plan",
        target_field="metadata.source_setup_metadata.target_price",
        max_holding_seconds_field="metadata.source_setup_metadata.max_holding_seconds",
        no_progress_rule_field="metadata.source_setup_metadata.no_progress_rule",
    )


def _entry(extra_source: dict[str, object]):
    bar = make_bar()
    return bar, make_entry_feature(
        bar,
        invalidation_price=None,
        target_price=None,
        extra={"metadata": {"source_setup_metadata": extra_source}},
    )


def test_entry_lifecycle_fields_flow_into_protection_plan() -> None:
    bar, feature = _entry(
        {
            "invalidation_price": "95",
            "target_price": "122.5",
            "max_holding_seconds": 86_400,
            "no_progress_rule": {
                "after_seconds": 14_400,
                "minimum_mfe_r": "0.15",
            },
        }
    )

    decision = _module().on_bar(make_view(bar, feature))

    assert len(decision.features) == 1
    plan = decision.features[0]
    assert plan.key == "protection_target.plan"
    assert plan.value["stop_loss"] == "95"
    assert plan.value["take_profit"] == "122.5"
    assert plan.value["reward_r"] == "4.5"
    assert plan.value["max_holding_seconds"] == 86_400
    assert plan.value["no_progress_rule"] == {
        "after_seconds": 14_400,
        "minimum_mfe_r": "0.15",
    }


def test_invalid_max_holding_seconds_is_rejected_fail_closed() -> None:
    bar, feature = _entry(
        {
            "invalidation_price": "95",
            "target_price": "122.5",
            "max_holding_seconds": True,
            "no_progress_rule": {"after_seconds": 14_400, "minimum_mfe_r": "0.15"},
        }
    )

    decision = _module().on_bar(make_view(bar, feature))

    assert decision.features[0].key == "protection_target.rejected"
    assert "protection_target.invalid_entry_plan" in decision.skips


def test_non_mapping_no_progress_rule_is_rejected_fail_closed() -> None:
    bar, feature = _entry(
        {
            "invalidation_price": "95",
            "target_price": "122.5",
            "max_holding_seconds": 86_400,
            "no_progress_rule": "invalid",
        }
    )

    decision = _module().on_bar(make_view(bar, feature))

    assert decision.features[0].key == "protection_target.rejected"
    assert "protection_target.invalid_entry_plan" in decision.skips
