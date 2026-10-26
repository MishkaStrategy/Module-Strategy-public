from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_registry_package import _context, _publish, _sha256

from modular_research_runner.models import ResearchSpecification, SpecificationError


def _rewrite_engine_manifest(output: Path, *, engine_run_id: str) -> None:
    files = {
        path.relative_to(output).as_posix(): _sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "artifact-manifest.json"
    }
    (output / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": engine_run_id,
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_engine_evidence(output: Path) -> None:
    (output / "environment.json").write_text(
        '{"python":"3.12","lock_sha256":"' + "d" * 64 + '"}\n',
        encoding="utf-8",
    )
    (output / "SHA256SUMS.txt").write_text(
        "manifest-declared checksum evidence\n",
        encoding="utf-8",
    )


def test_registry_package_accepts_manifest_declared_environment_and_checksums(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_engine_evidence(output)
    _rewrite_engine_manifest(
        output,
        engine_run_id=envelope.engine.engine_run_id,
    )

    reference = _publish(
        tmp_path,
        envelope=envelope,
        manifest=manifest,
        specification=specification,
        job=job,
    )

    package = json.loads(
        (reference.package_directory / "artifact-package.json").read_text(encoding="utf-8")
    )
    inventory = {item["relative_path"]: item for item in package["artifact_inventory"]}
    assert inventory["environment.json"]["media_type"] == "application/json"
    assert inventory["SHA256SUMS.txt"]["media_type"] == "text/plain"
    assert inventory["environment.json"]["sha256"] == _sha256(output / "environment.json")
    assert inventory["SHA256SUMS.txt"]["sha256"] == _sha256(output / "SHA256SUMS.txt")


def test_registry_package_rejects_unmanifested_engine_evidence(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_engine_evidence(output)

    with pytest.raises(SpecificationError, match="file set has changed"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_engine_evidence_hash_drift(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_engine_evidence(output)
    _rewrite_engine_manifest(
        output,
        engine_run_id=envelope.engine.engine_run_id,
    )
    (output / "environment.json").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SpecificationError, match="checksum mismatch: environment.json"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )


def test_registry_package_rejects_symlinked_engine_evidence(
    tmp_path: Path,
    specification: ResearchSpecification,
) -> None:
    _, manifest, job, envelope, output = _context(tmp_path, specification)
    _write_engine_evidence(output)
    environment = output / "environment.json"
    environment.unlink()
    environment.symlink_to(output / "run.json")

    with pytest.raises(SpecificationError, match="contain symlinks"):
        _publish(
            tmp_path,
            envelope=envelope,
            manifest=manifest,
            specification=specification,
            job=job,
        )
