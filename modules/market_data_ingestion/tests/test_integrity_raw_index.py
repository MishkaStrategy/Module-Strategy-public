from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_ingestion import hashing, manifest_io
from market_data_ingestion import storage as storage_module
from market_data_ingestion.contracts import (
    DownloadChunk,
    InstrumentReference,
    RawArtifactDescriptor,
)
from market_data_ingestion.enums import DataType, MarketType, SourceKind, ValidationStatus
from market_data_ingestion.errors import StorageError
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.storage import LocalStorage


def _raw_fixture(
    tmp_path: Path, count: int = 4
) -> tuple[LocalStorage, list[RawArtifactDescriptor]]:
    storage = LocalStorage(tmp_path / "data")
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    descriptors = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        chunk = DownloadChunk(
            chunk_id=f"fake:chunk:{index:06d}",
            data_type=DataType.KLINES,
            start=start + timedelta(minutes=5 * index),
            end=start + timedelta(minutes=5 * (index + 1)),
            source=SourceKind.OFFICIAL_REST,
            timeframe="5m",
        )
        descriptors.append(
            storage.write_raw(
                run_id="raw-index-test",
                instrument=instrument,
                chunk=chunk,
                payload=f"payload-{index}".encode(),
                source_uri="https://example.invalid/klines",
                adapter_version="1.0.0",
                response_metadata={"records": 1},
                upstream_checksum=None,
            )
        )
    return storage, descriptors


def _track_raw_reads(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, int]:
    counts = {"walks": 0, "metadata_reads": 0}
    real_walk = os.walk
    real_read_json = storage_module.read_json
    raw_root = root / "raw"

    def tracked_walk(top: str | os.PathLike[str], *args: object, **kwargs: object):
        if Path(top) == raw_root:
            counts["walks"] += 1
        for directory, directories, files in real_walk(top, *args, **kwargs):
            directories.sort()
            files.sort()
            yield directory, directories, files

    def tracked_read_json(path: Path) -> object:
        if path.name.endswith(".meta.json"):
            counts["metadata_reads"] += 1
        return real_read_json(path)

    monkeypatch.setattr(storage_module.os, "walk", tracked_walk)
    monkeypatch.setattr(storage_module, "read_json", tracked_read_json)
    return counts


def _legacy_raw_result(
    storage: LocalStorage, descriptors: list[RawArtifactDescriptor]
) -> tuple[list[str], int]:
    issues: list[str] = []
    checked = 0
    for raw in descriptors:
        try:
            found = storage.find_raw_artifact(raw.artifact_id)
        except Exception as exc:
            issues.append(
                f"raw artifact verification failed: {raw.artifact_id}: {type(exc).__name__}"
            )
            continue
        if found is None:
            issues.append(f"raw artifact missing: {raw.artifact_id}")
            continue
        payload, restored = found
        checked += 1
        fields = (
            ("source", restored.source, raw.source),
            ("source_uri", restored.source_uri, raw.source_uri),
            ("checksum", restored.checksum_sha256, raw.checksum_sha256),
            ("byte_size", restored.byte_size, raw.byte_size),
            ("record_count", restored.record_count, raw.record_count),
            ("start", restored.start, raw.start),
            ("end", restored.end, raw.end),
            ("adapter_version", restored.adapter_version, raw.adapter_version),
        )
        for field_name, actual, expected in fields:
            if actual != expected:
                issues.append(f"raw descriptor {field_name} mismatch: {raw.artifact_id}")
        if len(payload) != raw.byte_size:
            issues.append(f"raw artifact byte-size mismatch: {raw.artifact_id}")
    return issues, checked


def test_deep_validation_uses_one_raw_walk_and_matches_legacy_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, descriptors = _raw_fixture(tmp_path)
    (storage.root / "raw" / "unrelated").mkdir()
    (storage.root / "raw" / "unrelated" / "broken.meta.json").write_text("{broken")

    manifest = SimpleNamespace(
        dataset_id="test:dataset",
        version=1,
        exchange=SimpleNamespace(id="fake"),
        market_type=MarketType.SPOT,
        instrument=SimpleNamespace(canonical_id="fake:spot:btcusdt"),
        validation=SimpleNamespace(status=ValidationStatus.VALID),
        partitions=(),
        source_artifacts=tuple(descriptors),
    )
    manifest_path = storage.root / "manifests" / "synthetic.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(manifest_io, "manifest_from_dict", lambda _: manifest)
    monkeypatch.setattr(hashing, "dataset_hash", lambda _: "synthetic-dataset-hash")
    registry_descriptor = {
        "manifest_uri": "manifests/synthetic.json",
        "dataset_id": manifest.dataset_id,
        "version": manifest.version,
        "dataset_hash": "synthetic-dataset-hash",
        "exchange_id": "fake",
        "market_type": MarketType.SPOT.value,
        "instrument_id": "fake:spot:btcusdt",
        "validation_status": ValidationStatus.VALID.value,
    }

    counts = _track_raw_reads(monkeypatch, storage.root)
    report = verify_registered_dataset(storage.root, registry_descriptor, deep=True)
    indexed_counts = counts.copy()
    assert report["status"] == "ok"
    assert report["raw_artifacts_checked"] == len(descriptors)
    assert report["issues"] == []
    assert indexed_counts["walks"] == 1
    assert indexed_counts["metadata_reads"] == 2 * len(descriptors)
    legacy_issues, legacy_checked = _legacy_raw_result(storage, descriptors)
    assert report["issues"] == legacy_issues
    assert report["raw_artifacts_checked"] == legacy_checked

    counts["walks"] = 0
    counts["metadata_reads"] = 0
    legacy_results = [storage.find_raw_artifact(item.artifact_id) for item in descriptors]
    legacy_counts = counts.copy()
    assert legacy_results == [
        (
            storage.root.joinpath(
                "raw",
                "fake",
                "spot",
                "BTCUSDT",
                "klines",
                "2026",
                "01",
                f"fake:chunk:{index:06d}.raw",
            ).read_bytes(),
            item,
        )
        for index, item in enumerate(descriptors)
    ]
    assert legacy_counts["walks"] == len(descriptors)
    assert legacy_counts["metadata_reads"] == len(descriptors) * (len(descriptors) + 1) // 2
    assert indexed_counts["metadata_reads"] < legacy_counts["metadata_reads"]
    assert storage._index_raw_artifacts(["raw:missing"]) == {}

    damaged_metadata = next(
        path
        for path in (storage.root / "raw").rglob("*.meta.json")
        if path.name != "broken.meta.json"
        and json.loads(path.read_text(encoding="utf-8"))["artifact_id"]
        == descriptors[0].artifact_id
    )
    damaged_payload = damaged_metadata.with_name(damaged_metadata.name[: -len(".meta.json")])
    damaged_payload.write_bytes(damaged_payload.read_bytes() + b"tamper")
    legacy_issues, legacy_checked = _legacy_raw_result(storage, descriptors)
    tampered_report = verify_registered_dataset(storage.root, registry_descriptor, deep=True)
    assert tampered_report["issues"] == legacy_issues
    assert tampered_report["raw_artifacts_checked"] == legacy_checked
    assert tampered_report["status"] == "error"


def test_index_preserves_first_duplicate_and_fails_closed_on_payload_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, descriptors = _raw_fixture(tmp_path, count=1)
    original = descriptors[0]
    original_metadata = next((storage.root / "raw").rglob("*.meta.json"))
    first_metadata = storage.root / "raw" / "00-first" / "duplicate.meta.json"
    second_metadata = storage.root / "raw" / "99-second" / "duplicate.meta.json"
    first_metadata.parent.mkdir(parents=True)
    second_metadata.parent.mkdir(parents=True)
    metadata_payload = original_metadata.read_bytes()
    first_metadata.write_bytes(metadata_payload)
    second_metadata.write_bytes(metadata_payload)
    first_payload = first_metadata.with_name("duplicate")
    second_payload = second_metadata.with_name("duplicate")
    first_payload.write_bytes(b"corrupt-first")
    second_payload.write_bytes(b"payload-0")

    real_walk = os.walk

    def ordered_walk(top: str | os.PathLike[str], *args: object, **kwargs: object):
        for directory, directories, files in real_walk(top, *args, **kwargs):
            directories.sort()
            files.sort()
            yield directory, directories, files

    monkeypatch.setattr(storage_module.os, "walk", ordered_walk)
    indexed = storage._index_raw_artifacts([original.artifact_id])
    assert indexed[original.artifact_id][0] == first_metadata
    with pytest.raises(StorageError) as raised:
        storage._read_indexed_raw_artifact(
            original.artifact_id, indexed[original.artifact_id][0], indexed[original.artifact_id][1]
        )
    assert raised.value.code == "RAW_CHECKSUM_MISMATCH"


def test_index_rejects_selected_metadata_mutation(tmp_path: Path) -> None:
    storage, descriptors = _raw_fixture(tmp_path, count=1)
    descriptor = descriptors[0]
    indexed = storage._index_raw_artifacts([descriptor.artifact_id])[descriptor.artifact_id]
    metadata_path = indexed[0]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["adapter_version"] = "changed-after-index"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(StorageError) as raised:
        storage._read_indexed_raw_artifact(descriptor.artifact_id, *indexed)
    assert raised.value.code == "RAW_METADATA_CHANGED"


def test_index_skips_symlink_metadata_and_prunes_symlink_directories(
    tmp_path: Path,
) -> None:
    storage, descriptors = _raw_fixture(tmp_path, count=1)
    descriptor = descriptors[0]
    source_metadata = next((storage.root / "raw").rglob("*.meta.json"))
    linked_directory = storage.root / "raw" / "linked"
    linked_metadata = storage.root / "raw" / "linked-meta.meta.json"
    try:
        linked_directory.symlink_to(source_metadata.parent, target_is_directory=True)
        linked_metadata.symlink_to(source_metadata)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    indexed = storage._index_raw_artifacts([descriptor.artifact_id])
    assert indexed[descriptor.artifact_id][0] == source_metadata
    assert linked_metadata not in [metadata_path for metadata_path, _ in indexed.values()]
    assert storage._index_raw_artifacts(["raw:missing"]) == {}


def test_indexed_payload_symlink_fails_closed(tmp_path: Path) -> None:
    storage, descriptors = _raw_fixture(tmp_path, count=1)
    descriptor = descriptors[0]
    indexed = storage._index_raw_artifacts([descriptor.artifact_id])[descriptor.artifact_id]
    payload_path = indexed[0].with_name(indexed[0].name[: -len(".meta.json")])
    outside_payload = tmp_path / "outside.raw"
    outside_payload.write_bytes(b"payload-0")
    payload_path.unlink()
    try:
        payload_path.symlink_to(outside_payload)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(StorageError) as raised:
        storage._read_indexed_raw_artifact(descriptor.artifact_id, *indexed)
    assert raised.value.code == "RAW_ARTIFACT_MISSING"
