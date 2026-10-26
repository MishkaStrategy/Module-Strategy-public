from __future__ import annotations

from copy import deepcopy

import pytest

from modular_research_runner import SpecificationError, research_specification_from_mapping


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instruments", "BTCUSDT"),
        ("timeframes", "5m"),
        ("seeds", 11),
        ("execution_cost_scenarios", {"maker_fee": 0.0002}),
        ("robustness_scenarios", {"name": "delay"}),
    ],
)
def test_sequence_fields_reject_scalar_coercion(
    specification_mapping,
    field: str,
    value: object,
) -> None:
    malformed = deepcopy(specification_mapping)
    malformed[field] = value

    with pytest.raises(SpecificationError, match=rf"{field} must be a list"):
        research_specification_from_mapping(malformed)


@pytest.mark.parametrize(
    ("field_path", "value", "message"),
    [
        (("search_space", "maximum_combinations"), True, "maximum_combinations"),
        (("search_space", "maximum_combinations"), "16", "maximum_combinations"),
        (("parallelism",), True, "parallelism"),
        (("parallelism",), "4", "parallelism"),
        (("schema_version",), True, "schema_version"),
        (("schema_version",), "1", "schema_version"),
    ],
)
def test_integer_fields_reject_bool_and_string_coercion(
    specification_mapping,
    field_path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    malformed = deepcopy(specification_mapping)
    target = malformed
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value

    with pytest.raises(SpecificationError, match=rf"{message} must be an integer"):
        research_specification_from_mapping(malformed)


def test_invalid_enums_use_the_public_specification_error(specification_mapping) -> None:
    malformed = deepcopy(specification_mapping)
    malformed["search_method"] = "not-a-search-method"
    with pytest.raises(SpecificationError, match="search_method is unsupported"):
        research_specification_from_mapping(malformed)

    malformed = deepcopy(specification_mapping)
    malformed["datasets"][0]["quality_status"] = "unknown"
    with pytest.raises(SpecificationError, match="dataset.quality_status is unsupported"):
        research_specification_from_mapping(malformed)


@pytest.mark.parametrize(
    ("container", "field", "message"),
    [
        ("dataset", "dataset_id", "missing dataset fields"),
        ("dimension", "name", "missing search dimension fields"),
    ],
)
def test_nested_required_fields_cannot_be_coerced_from_none(
    specification_mapping,
    container: str,
    field: str,
    message: str,
) -> None:
    malformed = deepcopy(specification_mapping)
    if container == "dataset":
        del malformed["datasets"][0][field]
    else:
        del malformed["search_space"]["dimensions"][0][field]

    with pytest.raises(SpecificationError, match=message):
        research_specification_from_mapping(malformed)


def test_string_fields_reject_non_strings(specification_mapping) -> None:
    malformed = deepcopy(specification_mapping)
    malformed["research_id"] = 123

    with pytest.raises(SpecificationError, match="research_id must be a string"):
        research_specification_from_mapping(malformed)


def test_sequence_items_reject_non_strings(specification_mapping) -> None:
    malformed = deepcopy(specification_mapping)
    malformed["instruments"] = [123]

    with pytest.raises(SpecificationError, match=r"instruments\[0\] must be a string"):
        research_specification_from_mapping(malformed)


def test_scenario_items_must_be_mappings(specification_mapping) -> None:
    malformed = deepcopy(specification_mapping)
    malformed["execution_cost_scenarios"] = ["not-a-mapping"]

    with pytest.raises(
        SpecificationError,
        match=r"execution_cost_scenarios\[0\] must be a mapping",
    ):
        research_specification_from_mapping(malformed)


def test_split_fractions_reject_non_finite_values(specification_mapping) -> None:
    malformed = deepcopy(specification_mapping)
    malformed["split_policy"]["train_fraction"] = float("nan")

    with pytest.raises(
        SpecificationError,
        match="split_policy.train_fraction must be a finite number",
    ):
        research_specification_from_mapping(malformed)
