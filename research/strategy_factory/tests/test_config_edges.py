from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from test_config_cli import config_mapping

from strategy_factory.config import (
    _adaptation_policy,
    _boolean,
    _budget,
    _dataset,
    _datetime,
    _decimal,
    _improvement_policy,
    _integer,
    _mapping,
    _optional_string,
    _string,
    _string_tuple,
    load_request,
)
from strategy_factory.contracts import FactoryMode
from strategy_factory.errors import ContractError


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_load_request_rejects_unreadable_and_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot load"):
        load_request(tmp_path / "missing.yaml")

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("root: [\n", encoding="utf-8")
    with pytest.raises(ContractError, match="cannot load"):
        load_request(invalid)


def test_load_request_rejects_schema_and_forced_mode_mismatch(
    descriptor,
    budget,
    tmp_path: Path,
) -> None:
    payload = config_mapping(descriptor, budget)
    payload["schema_version"] = 2
    with pytest.raises(ContractError, match="schema_version"):
        load_request(_write(tmp_path, payload))

    payload = config_mapping(descriptor, budget)
    payload["mode"] = "validate"
    with pytest.raises(ContractError, match="mode differ"):
        load_request(_write(tmp_path, payload), forced_mode=FactoryMode.CREATE)


def test_dataset_resolves_relative_market_and_funding_paths(
    descriptor,
    budget,
    tmp_path: Path,
) -> None:
    payload = config_mapping(descriptor, budget)
    payload["dataset"]["uri"] = "market.csv"
    payload["dataset"]["funding_uri"] = "funding.csv"
    payload["dataset"]["funding_sha256"] = "f" * 64
    loaded = load_request(_write(tmp_path, payload))
    assert loaded.dataset.uri == str((tmp_path / "market.csv").resolve())
    assert loaded.dataset.funding_uri == str((tmp_path / "funding.csv").resolve())


def test_dataset_preserves_uri_schemes(descriptor, budget, tmp_path: Path) -> None:
    payload = config_mapping(descriptor, budget)
    payload["dataset"]["uri"] = "file:///tmp/market.csv"
    payload["dataset"]["funding_uri"] = "s3://bucket/funding.csv"
    payload["dataset"]["funding_sha256"] = "f" * 64
    loaded = load_request(_write(tmp_path, payload))
    assert loaded.dataset.uri == "file:///tmp/market.csv"
    assert loaded.dataset.funding_uri == "s3://bucket/funding.csv"


@pytest.mark.parametrize("value", [None, [], {1: "value"}])
def test_mapping_rejects_invalid_values(value) -> None:
    with pytest.raises(ContractError, match="mapping with string keys"):
        _mapping(value, "value")


@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_string_rejects_blank_or_non_string(value) -> None:
    with pytest.raises(ContractError, match="non-empty string"):
        _string({"key": value}, "key")


def test_string_strips_whitespace() -> None:
    assert _string({"key": " value "}, "key") == "value"


@pytest.mark.parametrize("value", ["", "   ", 1, False])
def test_optional_string_rejects_invalid_values(value) -> None:
    with pytest.raises(ContractError, match="optional string"):
        _optional_string(value)
    assert _optional_string(None) is None


@pytest.mark.parametrize("value", [None, "value", {"a": 1}])
def test_string_tuple_requires_sequence(value) -> None:
    with pytest.raises(ContractError, match="must be a sequence"):
        _string_tuple(value, "items")


@pytest.mark.parametrize("value", [[""], [1], ["ok", None]])
def test_string_tuple_requires_non_blank_strings(value) -> None:
    with pytest.raises(ContractError, match="non-empty strings"):
        _string_tuple(value, "items")
    assert _string_tuple((" a ", "b"), "items") == ("a", "b")


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5])
def test_integer_requires_positive_integer(value) -> None:
    with pytest.raises(ContractError, match="positive"):
        _integer(value, "count")
    assert _integer(0, "count", allow_zero=True) == 0


@pytest.mark.parametrize("value", [None, 0, "true"])
def test_boolean_requires_bool(value) -> None:
    with pytest.raises(ContractError, match="boolean"):
        _boolean(value, "flag")
    assert _boolean(False, "flag") is False


@pytest.mark.parametrize("value", [None, True, "bad", object()])
def test_decimal_rejects_invalid_values(value) -> None:
    with pytest.raises(ContractError, match="decimal"):
        _decimal(value, "amount")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_requires_finite_value(value) -> None:
    with pytest.raises(ContractError, match="finite"):
        _decimal(value, "amount")


@pytest.mark.parametrize(
    "value,message",
    [(None, "ISO-8601 string"), ("bad", "invalid ISO"), ("2026-01-01T00:00:00", "timezone")],
)
def test_datetime_is_strict(value, message) -> None:
    with pytest.raises(ContractError, match=message):
        _datetime(value)


def test_policy_decoders_reject_unknown_and_invalid_fields() -> None:
    with pytest.raises(ContractError, match="unknown adaptation_policy"):
        _adaptation_policy({"unknown": True})
    with pytest.raises(ContractError, match="boolean"):
        _adaptation_policy({"allow_parameter_adaptation": "yes"})
    with pytest.raises(ContractError, match="unknown improvement_policy"):
        _improvement_policy({"unknown": True})
    with pytest.raises(ContractError, match="non-negative"):
        _improvement_policy({"maximum_runtime_module_changes": -1})


def test_budget_rejects_invalid_stage_limit(descriptor, budget) -> None:
    payload = config_mapping(descriptor, budget)["budget"]
    payload["stage_experiment_limits"] = {"coarse": 0}
    with pytest.raises(ContractError, match="stage limit coarse"):
        _budget(payload)


def test_dataset_rejects_unknown_key(descriptor, budget, tmp_path: Path) -> None:
    payload = config_mapping(descriptor, budget)["dataset"]
    payload["unexpected"] = True
    with pytest.raises(ContractError, match="unknown dataset"):
        _dataset(payload, tmp_path)
