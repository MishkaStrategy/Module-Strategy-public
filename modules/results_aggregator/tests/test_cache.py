from __future__ import annotations

import json

from pathlib import Path

import pytest

from modular_results_registry.cache import DerivedCacheKey, DerivedViewCache
from modular_results_registry.errors import IntegrityError, ValidationError


def make_key(revision: int = 1, *, query_order: bool = False) -> DerivedCacheKey:
    query = {"b": 2, "a": 1} if query_order else {"a": 1, "b": 2}
    return DerivedCacheKey(
        registry_revision=revision,
        query_specification=query,
        metric_versions=("core.metrics.v1",),
        slice_definition={"instrument": "BTCUSDT"},
        comparison_definition={"baseline": "base"},
        ranking_configuration={"objectives": ["return"]},
        report_schema="strategy-audit.v1",
    )


def test_cache_is_deterministic_revision_aware_and_hash_bound(tmp_path: Path) -> None:
    cache = DerivedViewCache(tmp_path / "cache")
    key = make_key()
    assert key.key_id == make_key(query_order=True).key_id
    assert cache.get(key) is None
    payload_hash = cache.put(key, {"z": 3, "a": [1, 2]})
    assert cache.get(key) == {"a": (1, 2), "z": 3}
    assert len(payload_hash) == 64
    assert cache.get(make_key(2)) is None
    assert cache.stats().hits == 1
    assert cache.stats().misses == 2
    assert cache.stats().writes == 1


def test_corrupt_cache_is_removed_and_becomes_a_miss(tmp_path: Path) -> None:
    cache = DerivedViewCache(tmp_path / "cache")
    key = make_key()
    cache.put(key, {"value": 1})
    path = cache.directory / f"{key.key_id}.json"
    path.write_text('{"payload":{"value":2}}', encoding="utf-8")
    assert cache.get(key) is None
    assert not path.exists()
    assert cache.stats().corruptions == 1


def test_cache_rejects_symlinks_and_oversized_entries(tmp_path: Path) -> None:
    cache = DerivedViewCache(tmp_path / "cache", max_entry_bytes=1024)
    key = make_key()
    external = tmp_path / "external"
    external.write_text("x", encoding="utf-8")
    path = cache.directory / f"{key.key_id}.json"
    path.symlink_to(external)
    with pytest.raises(IntegrityError, match="symlink"):
        cache.get(key)
    path.unlink()
    with pytest.raises(ValidationError, match="exceeds"):
        cache.put(key, {"value": "x" * 2000})


def test_cache_validation_and_explicit_invalidation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        DerivedViewCache(tmp_path, max_entry_bytes=100)
    with pytest.raises(ValidationError):
        DerivedCacheKey(registry_revision=-1)
    with pytest.raises(ValidationError):
        DerivedCacheKey(registry_revision=0, report_schema=" ")
    with pytest.raises(ValidationError):
        DerivedCacheKey(registry_revision=0, metric_versions=("",))
    cache = DerivedViewCache(tmp_path / "cache")
    key = make_key()
    assert cache.invalidate(key) is False
    cache.put(key, {"value": 1})
    assert cache.invalidate(key) is True
    assert cache.invalidate(key) is False


def test_cache_key_runtime_types_order_and_payload_contract(tmp_path: Path) -> None:
    from modular_results_registry.errors import ValidationError

    with pytest.raises(ValidationError, match="non-negative integer"):
        DerivedCacheKey(True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="must be a tuple"):
        DerivedCacheKey(1, metric_versions=["v1"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="unique"):
        DerivedCacheKey(1, metric_versions=("v1", "v1"))
    with pytest.raises(ValidationError, match="canonical text"):
        DerivedCacheKey(1, report_schema=" report ")

    left = DerivedCacheKey(1, metric_versions=("v2", "v1"))
    right = DerivedCacheKey(1, metric_versions=("v1", "v2"))
    assert left.key_id == right.key_id
    assert left.metric_versions == ("v1", "v2")

    cache = DerivedViewCache(tmp_path / "cache")
    with pytest.raises(TypeError, match="payload must be a mapping"):
        cache.put(left, ["not", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DerivedCacheKey"):
        cache.put("bad-key", {"value": 1})  # type: ignore[arg-type]


def test_cache_rejects_noncanonical_envelope_and_embedded_key_tampering(
    tmp_path: Path,
) -> None:
    key = DerivedCacheKey(3, metric_versions=("v1",))
    cache = DerivedViewCache(tmp_path / "cache")
    cache.put(key, {"value": 1})
    path = cache._path(key)
    document = json.loads(path.read_text(encoding="utf-8"))

    document["key"]["registry_revision"] = 999
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    assert cache.get(key) is None
    assert not path.exists()

    cache.put(key, {"value": 1})
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    assert cache.get(key) is None
    assert not path.exists()


def test_cache_nonfinite_json_is_corruption_and_miss(tmp_path: Path) -> None:
    cache = DerivedViewCache(tmp_path / "cache")
    key = make_key()
    path = cache._path(key)
    path.write_text(
        '{"key":{},"key_hash":"x","payload":{"value":NaN},'
        '"payload_hash":"y","schema_version":"derived-cache.v1"}',
        encoding="utf-8",
    )
    assert cache.get(key) is None
    assert not path.exists()
    assert cache.stats().corruptions == 1
