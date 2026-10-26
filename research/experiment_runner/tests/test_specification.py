from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from modular_research_runner import (
    SpecificationError,
    load_research_specification,
    research_specification_from_mapping,
)


def test_mapping_loader_builds_immutable_specification(specification_mapping) -> None:
    specification = research_specification_from_mapping(specification_mapping)
    assert specification.research_id == "btc-fvg-study-v1"
    assert tuple(item.name for item in specification.search_space.dimensions) == (
        "threshold",
        "ttl",
    )
    with pytest.raises(TypeError):
        specification.base_configuration["risk_fraction"] = "0.01"  # type: ignore[index]


def test_yaml_loader_is_strict_and_deterministic(
    tmp_path: Path,
    specification_mapping,
) -> None:
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump(specification_mapping, sort_keys=False), encoding="utf-8")
    first = load_research_specification(path)
    second = load_research_specification(path)
    assert first == second
    assert first.fingerprint == second.fingerprint


def test_unknown_top_level_and_nested_fields_are_rejected(specification_mapping) -> None:
    top_level = deepcopy(specification_mapping)
    top_level["hidden_parameter"] = True
    with pytest.raises(SpecificationError, match="unknown"):
        research_specification_from_mapping(top_level)

    nested = deepcopy(specification_mapping)
    nested["search_space"]["hidden"] = True
    with pytest.raises(SpecificationError, match="unknown"):
        research_specification_from_mapping(nested)


def test_missing_required_field_is_rejected(specification_mapping) -> None:
    value = deepcopy(specification_mapping)
    del value["strategy_id"]
    with pytest.raises(SpecificationError, match="missing"):
        research_specification_from_mapping(value)


def test_unsafe_yaml_tag_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.yaml"
    path.write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")
    with pytest.raises(SpecificationError, match="valid YAML"):
        load_research_specification(path)


def test_symlinked_specification_is_rejected(tmp_path: Path, specification_mapping) -> None:
    real = tmp_path / "real.yaml"
    real.write_text(yaml.safe_dump(specification_mapping), encoding="utf-8")
    linked = tmp_path / "linked.yaml"
    linked.symlink_to(real)
    with pytest.raises(SpecificationError, match="regular file"):
        load_research_specification(linked)


def test_loader_rejects_bad_shapes_and_values(specification_mapping) -> None:

    bad = deepcopy(specification_mapping)
    bad["search_space"]["dimensions"] = "not-a-list"
    with pytest.raises(SpecificationError, match="must be lists"):
        research_specification_from_mapping(bad)

    bad = deepcopy(specification_mapping)
    bad["datasets"] = "not-a-list"
    with pytest.raises(SpecificationError, match="datasets must be a list"):
        research_specification_from_mapping(bad)

    bad = deepcopy(specification_mapping)
    bad["date_range"]["start"] = "not-a-date"
    with pytest.raises(SpecificationError, match="ISO-8601"):
        research_specification_from_mapping(bad)

    bad = deepcopy(specification_mapping)
    bad["search_space"]["dimensions"][0]["values"] = "bad"
    with pytest.raises(SpecificationError, match="values must be a list"):
        research_specification_from_mapping(bad)

    bad = deepcopy(specification_mapping)
    bad["search_space"]["dimensions"][1]["values"] = ["NaN"]
    with pytest.raises(SpecificationError, match="finite"):
        research_specification_from_mapping(bad)


def test_loader_parses_walk_forward_policy(specification_mapping) -> None:

    value = deepcopy(specification_mapping)
    value["walk_forward_policy"] = {
        "mode": "rolling",
        "train_seconds": 100,
        "validation_seconds": 20,
        "oos_seconds": 20,
        "step_seconds": 20,
        "purge_seconds": 1,
        "embargo_seconds": 1,
    }
    specification = research_specification_from_mapping(value)
    assert specification.walk_forward_policy is not None
    assert specification.walk_forward_policy.train_seconds == 100


def test_loader_parses_and_restricts_staged_search_policy(specification_mapping) -> None:
    value = deepcopy(specification_mapping)
    value["search_method"] = "staged"
    value["staged_search_policy"] = {
        "train_fractions": [0.25, 0.5, 1.0],
        "promotion_counts": [2, 1],
    }
    specification = research_specification_from_mapping(value)
    assert specification.staged_search_policy is not None
    assert specification.staged_search_policy.promotion_counts == (2, 1)

    missing = deepcopy(specification_mapping)
    missing["search_method"] = "staged"
    with pytest.raises(SpecificationError, match="requires staged_search_policy"):
        research_specification_from_mapping(missing)

    unexpected = deepcopy(specification_mapping)
    unexpected["staged_search_policy"] = value["staged_search_policy"]
    with pytest.raises(SpecificationError, match="only valid for staged"):
        research_specification_from_mapping(unexpected)

    walk_forward = deepcopy(value)
    walk_forward["walk_forward_policy"] = {
        "mode": "rolling",
        "train_seconds": 100,
        "validation_seconds": 20,
        "oos_seconds": 20,
        "step_seconds": 20,
    }
    with pytest.raises(SpecificationError, match="cannot be combined"):
        research_specification_from_mapping(walk_forward)

    malformed = deepcopy(value)
    malformed["staged_search_policy"]["unknown"] = 1
    with pytest.raises(SpecificationError, match="unknown staged_search_policy"):
        research_specification_from_mapping(malformed)


def test_yaml_loader_accepts_safe_loader_timestamp_objects(
    tmp_path: Path, specification_mapping
) -> None:
    value = deepcopy(specification_mapping)
    path = tmp_path / "timestamps.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["datasets"][0]["period"]["start"] = "2020-01-01T00:00:00Z"
    raw["datasets"][0]["period"]["end"] = "2024-01-01T00:00:00Z"
    raw["date_range"]["start"] = "2020-01-01T00:00:00Z"
    raw["date_range"]["end"] = "2024-01-01T00:00:00Z"
    serialized = (
        yaml.safe_dump(raw, sort_keys=False)
        .replace("'2020-01-01T00:00:00Z'", "2020-01-01T00:00:00Z")
        .replace("'2024-01-01T00:00:00Z'", "2024-01-01T00:00:00Z")
    )
    path.write_text(serialized, encoding="utf-8")
    specification = load_research_specification(path)
    assert specification.date_range.start.isoformat() == "2020-01-01T00:00:00+00:00"


def test_loader_rejects_naive_yaml_timestamp(specification_mapping) -> None:
    value = deepcopy(specification_mapping)
    value["date_range"]["start"] = datetime(2020, 1, 1)
    with pytest.raises(SpecificationError, match="timezone offset"):
        research_specification_from_mapping(value)
