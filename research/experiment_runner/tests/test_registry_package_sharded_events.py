from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path

import pytest
from test_registry_package import (
    _context,
    _read_engine_manifest,
    _write_engine_manifest,
)

from modular_research_runner.models import ResearchSpecification, SpecificationError
from modular_research_runner.registry_package import LocalResultsAggregatorPackageSink

_SHARD_MANIFEST = "events.manifest.json"
_SHARD_PREFIX = "events.jsonl.gz.part-"
_SHARD_ENCODING = "gzip-byte-shards-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_plain_events_with_shards(output: Path) -> tuple[bytes, bytes, list[str]]:
    plain = output / "events.jsonl"
    canonical = plain.read_bytes()
    compressed_path = output / ".events.original.gz"
    with compressed_path.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=1,
            mtime=0,
        ) as handle:
            handle.write(canonical)
    compressed = compressed_path.read_bytes()
    compressed_path.unlink()
    plain.unlink()

    part_size = max(1, len(compressed) // 3)
    part_names: list[str] = []
    parts: list[dict[str, object]] = []
    offset = 0
    index = 1
    while offset < len(compressed):
        chunk = compressed[offset : offset + part_size]
        name = f"{_SHARD_PREFIX}{index:06d}"
        path = output / name
        path.write_bytes(chunk)
        part_names.append(name)
        parts.append(
            {
                "index": index,
                "path": name,
                "bytes": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
        offset += len(chunk)
        index += 1
    assert len(parts) >= 2

    semantic_sha256 = "d" * 64
    shard_manifest = {
        "schema_version": "1",
        "encoding": _SHARD_ENCODING,
        "source_encoding": "gzip",
        "source_path": "events.jsonl.gz",
        "compressed_bytes": len(compressed),
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "logical_bytes": len(canonical),
        "event_count": canonical.count(b"\n"),
        "semantic_sha256": semantic_sha256,
        "parts": parts,
    }
    (output / _SHARD_MANIFEST).write_text(
        json.dumps(shard_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    journal = {
        "schema_version": "4",
        "event_stream": {
            "path": _SHARD_MANIFEST,
            "encoding": _SHARD_ENCODING,
            "count": shard_manifest["event_count"],
            "bytes": len(compressed),
            "logical_bytes": len(canonical),
            "semantic_sha256": semantic_sha256,
            "compressed_sha256": shard_manifest["compressed_sha256"],
            "part_count": len(parts),
            "counts_by_type": {"done": shard_manifest["event_count"]},
            "retained_window": shard_manifest["event_count"],
        },
    }
    (output / "journal.json").write_text(
        json.dumps(journal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    application = json.loads((output / "application-run.json").read_text(encoding="utf-8"))
    application["artifact_schema_version"] = "4"
    (output / "application-run.json").write_text(
        json.dumps(application, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    engine_manifest = _read_engine_manifest(output)
    files = engine_manifest["files"]
    assert isinstance(files, dict)
    files.pop("events.jsonl")
    files["journal.json"] = _sha256(output / "journal.json")
    files["application-run.json"] = _sha256(output / "application-run.json")
    files[_SHARD_MANIFEST] = _sha256(output / _SHARD_MANIFEST)
    for name in part_names:
        files[name] = _sha256(output / name)
    _write_engine_manifest(output, engine_manifest)
    return canonical, compressed, part_names


def test_registry_package_preserves_sharded_event_stream_and_authoritative_intake(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    canonical, compressed, part_names = _replace_plain_events_with_shards(output)
    reference = LocalResultsAggregatorPackageSink(
        tmp_path / "registry-packages",
        producer_module_version="0.3.0",
    ).publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )

    package = reference.package_directory
    document = json.loads((package / "artifact-package.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == "3"
    assert not (package / "events.jsonl").exists()
    assert not (package / "events.jsonl.gz").exists()
    assert (package / _SHARD_MANIFEST).is_file()
    assert b"".join((package / name).read_bytes() for name in part_names) == compressed
    with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
        assert handle.read() == canonical
    packaged_application = json.loads(
        (package / "application-run.json").read_text(encoding="utf-8")
    )
    assert packaged_application["artifact_schema_version"] == "4"

    inventory = {item["relative_path"]: item for item in document["artifact_inventory"]}
    assert inventory[_SHARD_MANIFEST]["schema_id"] == "modular-core.events-shard-manifest"
    for name in part_names:
        item = inventory[name]
        assert item["schema_id"] == "modular-core.events-gzip-byte-shard"
        assert item["media_type"] == "application/octet-stream"
        assert set(item) == {
            "media_type",
            "relative_path",
            "schema_id",
            "schema_version",
            "sha256",
            "size_bytes",
        }

    intake = pytest.importorskip("modular_results_registry.intake")
    profile = intake.ProducerProfile(
        producer_module_id="modular-research-runner",
        allowed_producer_schema_versions=frozenset({"1"}),
        allowed_artifact_schema_prefixes=("modular-core.", "modular-backtest."),
        allowed_package_schema_versions=frozenset({"3"}),
    )
    verified = intake.validate_artifact_package(package, profile)
    assert verified.manifest.schema_version == "3"
    assert {item.relative_path for item in verified.manifest.artifact_inventory} == set(inventory)


def test_registry_package_rejects_shard_inventory_and_order_drift(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _replace_plain_events_with_shards(output)
    extra = output / f"{_SHARD_PREFIX}999999"
    extra.write_bytes(b"extra")
    with pytest.raises(SpecificationError, match="part inventory changed"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "registry-extra",
            producer_module_version="0.3.0",
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )
    extra.unlink()

    shard_manifest = json.loads((output / _SHARD_MANIFEST).read_text(encoding="utf-8"))
    shard_manifest["parts"][0]["index"] = 2
    (output / _SHARD_MANIFEST).write_text(json.dumps(shard_manifest), encoding="utf-8")
    with pytest.raises(SpecificationError, match="indices are not ordered"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "registry-order",
            producer_module_version="0.3.0",
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_sharded_application_schema_drift(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _replace_plain_events_with_shards(output)
    application_path = output / "application-run.json"
    application = json.loads(application_path.read_text(encoding="utf-8"))
    application["artifact_schema_version"] = "3"
    application_path.write_text(json.dumps(application), encoding="utf-8")
    engine_manifest = _read_engine_manifest(output)
    files = engine_manifest["files"]
    assert isinstance(files, dict)
    files["application-run.json"] = _sha256(application_path)
    _write_engine_manifest(output, engine_manifest)

    with pytest.raises(SpecificationError, match="application artifact schema v4"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "registry-schema-drift",
            producer_module_version="0.3.0",
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_shard_without_per_file_headroom(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _, _, part_names = _replace_plain_events_with_shards(output)
    max_part_size = max((output / name).stat().st_size for name in part_names)

    with pytest.raises(SpecificationError, match="does not retain Registry headroom"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "registry-packages",
            producer_module_version="0.3.0",
            max_file_bytes=max_part_size,
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )
