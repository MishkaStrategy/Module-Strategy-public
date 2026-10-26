from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .canonical import sha256_file
from .errors import StorageError
from .integrity import resolve_registered_path, verify_registered_dataset
from .manifest_io import load_manifest
from .registry import descriptor_from_dict
from .serialization import json_bytes


class _CopiedFile(TypedDict):
    path: str
    sha256: str
    bytes: int
    partition_id: NotRequired[str]


def export_registered_dataset(
    *,
    storage_root: Path,
    descriptor_payload: dict[str, Any],
    target: Path,
) -> dict[str, object]:
    """Export one immutable registered dataset without following symlinks."""

    root = storage_root.expanduser().resolve()
    target = target.expanduser().resolve()
    descriptor = descriptor_from_dict(descriptor_payload)
    verification = verify_registered_dataset(root, descriptor_payload)
    if verification["status"] != "ok":
        raise StorageError(
            "EXPORT_SOURCE_INVALID",
            "registered dataset failed integrity verification before export",
            False,
            "export",
            {"issues": verification.get("issues", [])},
        )
    manifest = load_manifest(root, descriptor)
    if target.exists():
        raise StorageError(
            "EXPORT_TARGET_EXISTS",
            "export target already exists",
            False,
            "export",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    copied: list[_CopiedFile] = []
    try:
        descriptor_path = temporary / "descriptor.json"
        descriptor_path.write_bytes(json_bytes(descriptor, pretty=True))
        copied.append(
            {
                "path": "descriptor.json",
                "sha256": sha256_file(descriptor_path),
                "bytes": descriptor_path.stat().st_size,
            }
        )

        source_manifest = resolve_registered_path(root, descriptor.manifest_uri, kind="manifest")
        manifest_target = temporary / "manifest.json"
        shutil.copyfile(source_manifest, manifest_target, follow_symlinks=False)
        copied.append(
            {
                "path": "manifest.json",
                "sha256": sha256_file(manifest_target),
                "bytes": manifest_target.stat().st_size,
            }
        )

        for partition in manifest.partitions:
            try:
                source = resolve_registered_path(root, partition.storage_uri, kind="partition")
            except ValueError as exc:
                raise StorageError(
                    "EXPORT_PATH_ESCAPE",
                    "partition path is unsafe",
                    False,
                    "export",
                    {"partition_id": partition.partition_id},
                ) from exc
            if not source.is_file():
                raise StorageError(
                    "EXPORT_SOURCE_INVALID",
                    "partition is missing or is a symbolic link",
                    False,
                    "export",
                    {"partition_id": partition.partition_id},
                )
            relative = Path("partitions") / partition.storage_uri
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            actual = sha256_file(destination)
            if actual != partition.checksum_sha256:
                raise StorageError(
                    "EXPORT_CHECKSUM_MISMATCH",
                    "copied partition checksum differs from registered evidence",
                    False,
                    "export",
                    {"partition_id": partition.partition_id},
                )
            copied.append(
                {
                    "path": relative.as_posix(),
                    "sha256": actual,
                    "bytes": destination.stat().st_size,
                    "partition_id": partition.partition_id,
                }
            )

        export_manifest = {
            "schema_version": 1,
            "dataset_id": descriptor.dataset_id,
            "dataset_version": descriptor.version,
            "dataset_hash": descriptor.dataset_hash,
            "created_at": datetime.now(UTC),
            "files": copied,
        }
        export_path = temporary / "export-manifest.json"
        export_path.write_bytes(json_bytes(export_manifest, pretty=True))
        with export_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return {
        "status": "ok",
        "dataset_id": descriptor.dataset_id,
        "version": descriptor.version,
        "dataset_hash": descriptor.dataset_hash,
        "target": str(target),
        "files": len(copied) + 1,
        "bytes": sum(item["bytes"] for item in copied),
    }
