from __future__ import annotations

import pytest

from paper_execution import LiveExecutionNotSupported, PaperExecutionConfig
from paper_execution import config as config_module


def test_config_enums_fail_with_typed_errors_for_wrong_types() -> None:
    with pytest.raises(LiveExecutionNotSupported):
        PaperExecutionConfig(execution_mode={})
    with pytest.raises(ValueError, match="reconciliation_mode"):
        PaperExecutionConfig(reconciliation_mode={})


def test_config_requires_strict_schema_bool_and_namespace_types() -> None:
    with pytest.raises(TypeError, match="schema_version"):
        PaperExecutionConfig(schema_version=1)
    with pytest.raises(TypeError, match="strict_invariants"):
        PaperExecutionConfig(strict_invariants=1)
    with pytest.raises(TypeError, match="module_namespace"):
        PaperExecutionConfig(module_namespace=None)


def test_config_rejects_bool_numeric_limits() -> None:
    for name in (
        "maximum_open_orders",
        "maximum_conditional_orders",
        "client_order_id_max_length",
    ):
        with pytest.raises(ValueError, match="positive integer"):
            PaperExecutionConfig(**{name: True})


def test_config_caps_resource_and_identifier_limits(monkeypatch) -> None:
    monkeypatch.setattr(config_module, "MAX_CONFIGURATION_ORDER_LIMIT", 10)
    with pytest.raises(ValueError, match="maximum_open_orders exceeds"):
        PaperExecutionConfig(
            maximum_open_orders=11,
            maximum_conditional_orders=10,
        )
    with pytest.raises(ValueError, match="maximum_conditional_orders exceeds"):
        PaperExecutionConfig(
            maximum_open_orders=10,
            maximum_conditional_orders=11,
        )

    with pytest.raises(ValueError, match="client_order_id_max_length exceeds"):
        PaperExecutionConfig(
            maximum_open_orders=10,
            maximum_conditional_orders=10,
            client_order_id_max_length=129,
        )
    with pytest.raises(ValueError, match="module_namespace has invalid length"):
        PaperExecutionConfig(
            maximum_open_orders=10,
            maximum_conditional_orders=10,
            module_namespace="x" * 33,
        )


def test_long_safe_namespace_uses_factory_fallback_without_config_rejection() -> None:
    config = PaperExecutionConfig(
        module_namespace="long-safe-namespace",
        client_order_id_max_length=20,
    )
    assert config.module_namespace == "long-safe-namespace"
