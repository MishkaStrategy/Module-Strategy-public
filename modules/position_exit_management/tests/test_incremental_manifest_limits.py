from __future__ import annotations

from datetime import timedelta

from modular_strategy_position_exit_management import (
    MANIFEST,
    PositionExitManagementModule,
)
from modular_strategy_position_exit_management.module import MANIFEST as direct_manifest

MAX_COUNTER_VALUE = 2**63 - 1
_ALLOWED_PARAMETER_RULES = {
    "type",
    "required",
    "minimum",
    "maximum",
    "enum",
    "default",
}


def test_manifest_reports_runtime_resource_limits_consistently():
    maximum_minutes = timedelta.max.days * 24 * 60 + timedelta.max.seconds // 60

    assert MANIFEST is PositionExitManagementModule.manifest
    assert direct_manifest is MANIFEST
    assert MANIFEST.parameters_schema["max_holding_minutes"]["maximum"] == maximum_minutes
    assert MANIFEST.parameters_schema["partial_exits"] == {
        "type": "array",
        "default": (),
    }
    assert {
        rule
        for rules in MANIFEST.parameters_schema.values()
        for rule in rules
        if rule not in _ALLOWED_PARAMETER_RULES
    } == set()
    for name in (
        "cancel_retry_bars",
        "max_tracked_cancels",
        "max_cancel_requests_per_bar",
    ):
        assert MANIFEST.parameters_schema[name]["maximum"] == MAX_COUNTER_VALUE
