from __future__ import annotations

from pathlib import Path

from .artifact_store import VerifiedFile, verify_file
from .canonical import canonical_sha256, deterministic_id
from .errors import IntegrityError, ResourceLimitError, ValidationError
from .intake_manifest import (
    _MANIFEST_FILENAME,
    _MAX_MANIFEST_BYTES,
    _load_json,
    manifest_from_mapping,
)
from .intake_models import (
    IntakeLimits,
    LegacyFileRecord,
    LegacyImportReport,
    ProducerProfile,
    VerifiedArtifactPackage,
)


def validate_artifact_package(
    directory: str | Path,
    profile: ProducerProfile,
    *,
    limits: IntakeLimits = IntakeLimits(),
    reject_extra_files: bool = True,
) -> VerifiedArtifactPackage:
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValidationError("artifact package must be a real directory")
    document = _load_json(root / _MANIFEST_FILENAME, max_bytes=_MAX_MANIFEST_BYTES)
    manifest = manifest_from_mapping(document)
    manifest.verify_checksum()
    profile.validate(manifest)
    if len(manifest.artifact_inventory) > limits.max_artifact_count:
        raise ResourceLimitError("artifact count exceeds configured limit")
    total_declared = sum(item.size_bytes for item in manifest.artifact_inventory)
    if total_declared > limits.max_total_bytes:
        raise ResourceLimitError("artifact package exceeds total byte limit")

    declared_paths = {item.relative_path for item in manifest.artifact_inventory}
    if reject_extra_files:
        actual_paths: set[str] = set()
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if relative != _MANIFEST_FILENAME:
                actual_paths.add(relative)
        extras = sorted(actual_paths - declared_paths)
        missing = sorted(declared_paths - actual_paths)
        if extras or missing:
            raise IntegrityError(f"artifact inventory mismatch; extras={extras}, missing={missing}")

    verified_files: list[VerifiedFile] = []
    by_hash: dict[str, list[str]] = {}
    for item in sorted(manifest.artifact_inventory, key=lambda value: value.relative_path):
        source = root / item.relative_path
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise IntegrityError("artifact escaped package root") from exc
        verified = verify_file(
            source,
            expected_sha256=item.sha256,
            expected_size=item.size_bytes,
            limits=limits.file_limits,
        )
        verified_files.append(verified)
        by_hash.setdefault(verified.sha256, []).append(item.relative_path)
    duplicates = {
        digest: tuple(sorted(paths)) for digest, paths in sorted(by_hash.items()) if len(paths) > 1
    }
    validation_hash = canonical_sha256(
        {
            "manifest_checksum": manifest.integrity_checksum,
            "verified_files": tuple(
                {
                    "path": item.path.relative_to(root).as_posix(),
                    "sha256": item.sha256,
                    "size": item.size_bytes,
                }
                for item in verified_files
            ),
        }
    )
    return VerifiedArtifactPackage(
        manifest=manifest,
        verified_files=tuple(verified_files),
        validation_hash=validation_hash,
        duplicate_content=duplicates,
    )


def inspect_legacy_directory(
    directory: str | Path,
    *,
    limits: IntakeLimits = IntakeLimits(),
) -> LegacyImportReport:
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValidationError("legacy import source must be a real directory")
    files: list[LegacyFileRecord] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if len(files) >= limits.max_artifact_count:
            raise ResourceLimitError("legacy artifact count exceeds configured limit")
        verified = verify_file(path, limits=limits.file_limits)
        files.append(
            LegacyFileRecord(
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=verified.size_bytes,
                sha256=verified.sha256,
            )
        )
    total = sum(item.size_bytes for item in files)
    if total > limits.max_total_bytes:
        raise ResourceLimitError("legacy import exceeds total byte limit")
    missing = (
        "verified artifact package manifest",
        "trusted producer contract",
        "commit SHA",
        "dataset lineage",
        "metric definition versions",
        "environment manifest",
    )
    limitations = (
        "Legacy files are retained and hashed but are not VERIFIED.",
        "Legacy results cannot participate in release comparisons without explicit compatible evidence.",
    )
    report_id = deterministic_id(
        "legacy",
        {
            "files": tuple({"path": item.relative_path, "sha256": item.sha256} for item in files),
            "missing": missing,
        },
        digest_chars=48,
    )
    return LegacyImportReport(
        profile="LEGACY_UNVERIFIED_IMPORT",
        files=tuple(files),
        missing_lineage=missing,
        limitations=limitations,
        report_id=report_id,
    )
