from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from test_registry_package import (
    _ENGINE_MANIFEST,
    _context,
    _read_engine_manifest,
    _write_engine_manifest,
)

from modular_research_runner.models import ResearchSpecification, SpecificationError
from modular_research_runner.registry_package import LocalResultsAggregatorPackageSink


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_plain_events_with_gzip(output: Path) -> bytes:
    plain = output / "events.jsonl"
    canonical = plain.read_bytes()
    compressed = output / "events.jsonl.gz"
    with compressed.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as handle:
            handle.write(canonical)
    plain.unlink()

    manifest = _read_engine_manifest(output)
    files = manifest["files"]
    assert isinstance(files, dict)
    files.pop("events.jsonl")
    files["events.jsonl.gz"] = _sha256(compressed)
    _write_engine_manifest(output, manifest)
    return canonical


def _assert_authoritative_intake(
    package_directory: Path,
    *,
    schema_version: str,
) -> None:
    intake = pytest.importorskip("modular_results_registry.intake")
    profile = intake.ProducerProfile(
        producer_module_id="modular-research-runner",
        allowed_producer_schema_versions=frozenset({"1"}),
        allowed_artifact_schema_prefixes=("modular-core.", "modular-backtest."),
        allowed_package_schema_versions=frozenset({schema_version}),
    )
    verified = intake.validate_artifact_package(package_directory, profile)
    assert verified.manifest.schema_version == schema_version


def test_registry_package_preserves_compressed_event_stream_contract(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    canonical = _replace_plain_events_with_gzip(output)

    reference = LocalResultsAggregatorPackageSink(
        tmp_path / "registry-packages",
        producer_module_version="0.3.0",
    ).publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )

    package_document = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    assert package_document["schema_version"] == "2"
    event_items = [
        item
        for item in package_document["artifact_inventory"]
        if item["relative_path"] in {"events.jsonl", "events.jsonl.gz"}
    ]
    assert event_items == [
        {
            "media_type": "application/gzip",
            "relative_path": "events.jsonl.gz",
            "schema_id": "modular-core.events",
            "schema_version": "0.4.0",
            "sha256": _sha256(output / "events.jsonl.gz"),
            "size_bytes": (output / "events.jsonl.gz").stat().st_size,
        }
    ]
    assert not (reference.package_directory / "events.jsonl").exists()
    with gzip.open(reference.package_directory / "events.jsonl.gz", "rb") as handle:
        assert handle.read() == canonical
    _assert_authoritative_intake(reference.package_directory, schema_version="2")


def test_registry_package_rejects_plain_and_compressed_event_stream_ambiguity(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    with gzip.open(output / "events.jsonl.gz", "wb") as handle:
        handle.write((output / "events.jsonl").read_bytes())

    with pytest.raises(SpecificationError, match="event_streams"):
        LocalResultsAggregatorPackageSink(
            tmp_path / "registry-packages",
            producer_module_version="0.3.0",
        ).publish(
            envelope=envelope,
            experiment_manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_keeps_legacy_plain_event_schema_v1(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, _ = _context(tmp_path, specification)
    reference = LocalResultsAggregatorPackageSink(
        tmp_path / "registry-packages",
        producer_module_version="0.3.0",
    ).publish(
        envelope=envelope,
        experiment_manifest=manifest,
        specification=specification,
        job=job,
    )
    package_document = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    assert package_document["schema_version"] == "1"
    event_item = next(
        item
        for item in package_document["artifact_inventory"]
        if item["relative_path"] == "events.jsonl"
    )
    assert event_item["media_type"] == "application/x-ndjson"
    assert set(event_item) == {
        "media_type",
        "relative_path",
        "schema_id",
        "schema_version",
        "sha256",
        "size_bytes",
    }
    assert (reference.package_directory / _ENGINE_MANIFEST).is_file()
