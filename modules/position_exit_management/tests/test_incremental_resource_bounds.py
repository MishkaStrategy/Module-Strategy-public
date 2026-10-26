from __future__ import annotations

from datetime import timedelta

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule


def _unbounded_partial_rules():
    index = 0
    while True:
        index += 1
        yield {"trigger_r": index, "fraction": "0.001"}


def test_holding_limit_cannot_exceed_timedelta_domain():
    maximum_minutes = timedelta.max.days * 24 * 60 + timedelta.max.seconds // 60

    PositionExitManagementModule(max_holding_minutes=maximum_minutes)
    with pytest.raises(ValueError, match="max_holding_minutes cannot exceed"):
        PositionExitManagementModule(max_holding_minutes=maximum_minutes + 1)


def test_extreme_holding_limit_fails_during_configuration_not_event_processing():
    with pytest.raises(ValueError, match="max_holding_minutes cannot exceed"):
        PositionExitManagementModule(max_holding_minutes=10**10000)


def test_partial_rule_sequence_is_bounded_before_unbounded_materialization():
    with pytest.raises(ValueError, match="cannot contain more than 64 rules"):
        PositionExitManagementModule(partial_exits=_unbounded_partial_rules())


def test_direct_sequence_with_more_than_64_partial_rules_is_rejected():
    rules = [{"trigger_r": index + 1, "fraction": "0.001"} for index in range(65)]
    with pytest.raises(ValueError, match="cannot contain more than 64 rules"):
        PositionExitManagementModule(partial_exits=rules)
