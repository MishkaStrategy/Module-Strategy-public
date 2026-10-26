import pytest

from modular_core.config import (
    DataConfig,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    RiskConfig,
)


@pytest.mark.parametrize(
    ("field_name", "value", "expected_name"),
    [
        ("fees", {}, "FeeConfig"),
        ("execution", {}, "ExecutionConfig"),
        ("risk", {}, "RiskConfig"),
        ("data", {}, "DataConfig"),
    ],
)
def test_engine_config_rejects_wrong_nested_config_types(
    field_name: str,
    value: object,
    expected_name: str,
) -> None:
    with pytest.raises(TypeError, match=expected_name):
        EngineConfig(**{field_name: value})  # type: ignore[arg-type]


def test_engine_config_accepts_typed_nested_configs() -> None:
    config = EngineConfig(
        fees=FeeConfig(),
        execution=ExecutionConfig(),
        risk=RiskConfig(),
        data=DataConfig(),
    )
    assert isinstance(config.fees, FeeConfig)
    assert isinstance(config.execution, ExecutionConfig)
    assert isinstance(config.risk, RiskConfig)
    assert isinstance(config.data, DataConfig)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("intrabar_priority", ["stop"]),
        ("limit_fill_policy", {"policy": "at_limit"}),
        ("target_liquidity", 1),
        ("stop_limit_same_bar_policy", False),
    ],
)
def test_execution_policy_fields_require_strings(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(TypeError, match=f"{field_name} must be str"):
        ExecutionConfig(**{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("gap_policy", ["allow"]),
        ("timestamp_semantics", 1),
    ],
)
def test_data_policy_fields_require_strings(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(TypeError, match=f"{field_name} must be str"):
        DataConfig(**{field_name: value})  # type: ignore[arg-type]
