from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from modular_core.features import Feature, FeatureDelete, FeatureStore, FeatureView


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _feature(**overrides: object) -> Feature:
    values: dict[str, object] = {
        "key": "probe.value",
        "source_module": "probe",
        "created_at": NOW,
        "value": {"values": [1, 2]},
        "symbol": "BTCUSDT",
        "metadata": {"nested": {"labels": ["a"]}},
    }
    values.update(overrides)
    return Feature(**values)  # type: ignore[arg-type]


def test_feature_detaches_nested_value_and_metadata() -> None:
    value = {"values": [1, 2]}
    metadata = {"nested": {"labels": ["a"]}}

    feature = _feature(value=value, metadata=metadata)
    value["values"].append(3)
    metadata["nested"]["labels"].append("b")  # type: ignore[index,union-attr]

    assert isinstance(feature.value, MappingProxyType)
    assert isinstance(feature.metadata, MappingProxyType)
    assert feature.value["values"] == (1, 2)
    assert feature.metadata["nested"]["labels"] == ("a",)  # type: ignore[index]


def test_feature_rejects_non_mapping_metadata() -> None:
    with pytest.raises(TypeError, match="metadata must be a mapping"):
        _feature(metadata=[("value", 1)])


@pytest.mark.parametrize("value", [1, "yes", None])
def test_feature_rejects_non_boolean_gap_flag(value: object) -> None:
    with pytest.raises(TypeError, match="invalidate_on_gap must be bool"):
        _feature(invalidate_on_gap=value)


@pytest.mark.parametrize("field", ["feature_id", "symbol", "timeframe"])
def test_feature_delete_rejects_blank_optional_identity(field: str) -> None:
    values: dict[str, object] = {
        "key": "probe.value",
        "source_module": "probe",
        field: " ",
    }

    with pytest.raises(ValueError, match=field):
        FeatureDelete(**values)  # type: ignore[arg-type]


def test_feature_view_materializes_one_shot_iterable() -> None:
    feature = _feature()
    source = (item for item in (feature,))

    view = FeatureView(source, NOW)

    assert view.latest("probe.value") == feature
    assert view.latest("probe.value") == feature


@pytest.mark.parametrize("value", ["features", 1, object()])
def test_feature_view_rejects_invalid_iterable_boundary(value: object) -> None:
    with pytest.raises(TypeError, match="iterable of Feature"):
        FeatureView(value, NOW)  # type: ignore[arg-type]


def test_feature_view_rejects_non_feature_elements() -> None:
    with pytest.raises(TypeError, match="must contain Feature"):
        FeatureView((_feature(), object()), NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_feature_store_capacity_requires_exact_integer(value: object) -> None:
    with pytest.raises(TypeError, match="max_features must be int or None"):
        FeatureStore(value)  # type: ignore[arg-type]


def test_feature_store_rejects_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="max_features must be >= 1"):
        FeatureStore(0)


def test_feature_store_publish_rejects_foreign_type_without_mutation() -> None:
    store = FeatureStore()

    with pytest.raises(TypeError, match="feature must be Feature"):
        store.publish(object())  # type: ignore[arg-type]

    assert store.count == 0


def test_feature_store_delete_rejects_foreign_type_without_mutation() -> None:
    store = FeatureStore()
    stored = store.publish(_feature())

    with pytest.raises(TypeError, match="request must be FeatureDelete"):
        store.delete(object())  # type: ignore[arg-type]

    assert store.all() == (stored,)


def test_feature_store_gap_invalidation_rejects_blank_symbol() -> None:
    store = FeatureStore()
    stored = store.publish(_feature())

    with pytest.raises(ValueError, match="feature gap symbol is required"):
        store.invalidate_on_gap(symbol=" ")

    assert store.all() == (stored,)


def test_feature_store_copy_keeps_immutable_feature_evidence() -> None:
    store = FeatureStore()
    stored = store.publish(_feature())
    clone = store.copy()

    assert clone.all() == (stored,)
    assert clone.all()[0].value["values"] == (1, 2)
