from __future__ import annotations

import csv
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import sha256_file
from .contracts import NormalizedPartitionDescriptor


class RegisteredPathError(ValueError):
    """Raised when a registered URI is unsafe for local filesystem access."""


def resolve_registered_path(storage_root: Path, uri: str, *, kind: str) -> Path:
    """Resolve an existing registered path without accepting symlink traversal."""

    root = storage_root.expanduser().resolve()
    if not isinstance(uri, str) or not uri.strip() or "\\" in uri:
        raise RegisteredPathError(f"{kind} URI is invalid")
    pure = PurePosixPath(uri)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RegisteredPathError(f"{kind} path escapes storage root")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise RegisteredPathError(f"{kind} path contains a symbolic link")
    resolved = current.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RegisteredPathError(f"{kind} path escapes storage root") from exc
    return current


def partition_integrity_issue(
    storage_root: Path,
    partition: NormalizedPartitionDescriptor,
) -> dict[str, object] | None:
    """Return deterministic read-only evidence for a damaged registered partition."""

    try:
        path = resolve_registered_path(
            storage_root,
            partition.storage_uri,
            kind="partition",
        )
    except RegisteredPathError:
        return {
            "partition_id": partition.partition_id,
            "storage_uri": partition.storage_uri,
            "issue": "unsafe_path",
        }
    if not path.is_file():
        return {
            "partition_id": partition.partition_id,
            "storage_uri": partition.storage_uri,
            "issue": "missing",
        }
    actual = sha256_file(path)
    if actual != partition.checksum_sha256:
        return {
            "partition_id": partition.partition_id,
            "storage_uri": partition.storage_uri,
            "issue": "checksum_mismatch",
            "expected": partition.checksum_sha256,
            "actual": actual,
        }
    return None


def _csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def verify_registered_dataset(
    storage_root: Path,
    descriptor: dict[str, Any],
    *,
    deep: bool = False,
) -> dict[str, Any]:
    """Verify registry identity, manifest hash, raw lineage and partition bytes."""

    manifest_uri = descriptor.get("manifest_uri")
    if not isinstance(manifest_uri, str):
        return {"status": "error", "issues": ["descriptor manifest_uri is invalid"]}
    try:
        manifest_path = resolve_registered_path(storage_root, manifest_uri, kind="manifest")
    except RegisteredPathError as exc:
        return {"status": "error", "issues": [str(exc)]}
    if not manifest_path.is_file():
        return {"status": "error", "issues": ["manifest file is missing"]}
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "issues": [f"manifest cannot be decoded: {type(exc).__name__}"],
        }

    issues: list[str] = []
    checked = 0
    raw_checked = 0
    rows_checked = 0
    try:
        from .hashing import dataset_hash
        from .manifest_io import manifest_from_dict

        manifest = manifest_from_dict(manifest_payload)
    except Exception as exc:
        return {
            "status": "error",
            "issues": [f"manifest schema is invalid: {type(exc).__name__}: {exc}"],
            "manifest": str(manifest_path),
        }

    expected_identity = {
        "dataset_id": manifest.dataset_id,
        "version": manifest.version,
        "dataset_hash": dataset_hash(manifest),
        "exchange_id": manifest.exchange.id,
        "market_type": manifest.market_type.value,
        "instrument_id": manifest.instrument.canonical_id,
        "validation_status": manifest.validation.status.value,
    }
    for key, expected in expected_identity.items():
        actual = descriptor.get(key)
        if actual != expected:
            issues.append(
                f"descriptor/manifest {key} mismatch: expected {expected!r}, got {actual!r}"
            )

    partition_ids = {item.partition_id for item in manifest.partitions}
    raw_ids = {item.artifact_id for item in manifest.source_artifacts}
    allowed_lineage_ids = partition_ids | raw_ids
    for partition in manifest.partitions:
        unknown = set(partition.source_artifact_ids) - allowed_lineage_ids
        if unknown:
            details = f"{partition.partition_id}: {sorted(unknown)}"
            issues.append(f"partition has unknown lineage references: {details}")
        try:
            path = resolve_registered_path(storage_root, partition.storage_uri, kind="partition")
        except RegisteredPathError as exc:
            issues.append(f"{exc}: {partition.storage_uri}")
            continue
        if not path.is_file():
            issues.append(f"partition missing: {partition.storage_uri}")
            continue
        actual = sha256_file(path)
        checked += 1
        if actual != partition.checksum_sha256:
            issues.append(f"partition checksum mismatch: {partition.storage_uri}")
            continue
        if deep:
            if path.suffix.lower() == ".csv":
                count = _csv_row_count(path)
                rows_checked += count
                if count != partition.row_count:
                    issues.append(
                        f"partition row count mismatch: {partition.storage_uri}: "
                        f"expected {partition.row_count}, got {count}"
                    )
            elif path.suffix.lower() == ".parquet":
                try:
                    import pyarrow.parquet as pq
                except ImportError:
                    issues.append(
                        f"deep Parquet validation requires pyarrow: {partition.storage_uri}"
                    )
                else:
                    count = int(pq.ParquetFile(path).metadata.num_rows)
                    rows_checked += count
                    if count != partition.row_count:
                        issues.append(
                            f"partition row count mismatch: {partition.storage_uri}: "
                            f"expected {partition.row_count}, got {count}"
                        )
            else:
                issues.append(f"unsupported partition format: {partition.storage_uri}")

    if deep:
        from .storage import LocalStorage

        storage = LocalStorage(storage_root)
        raw_index = storage._index_raw_artifacts(
            raw.artifact_id for raw in manifest.source_artifacts
        )
        for raw in manifest.source_artifacts:
            indexed = raw_index.get(raw.artifact_id)
            if indexed is None:
                issues.append(f"raw artifact missing: {raw.artifact_id}")
                continue
            try:
                found = storage._read_indexed_raw_artifact(
                    raw.artifact_id,
                    indexed[0],
                    indexed[1],
                )
            except Exception as exc:
                issues.append(
                    f"raw artifact verification failed: {raw.artifact_id}: {type(exc).__name__}"
                )
                continue
            payload, restored = found
            raw_checked += 1
            descriptor_fields = (
                ("source", restored.source, raw.source),
                ("source_uri", restored.source_uri, raw.source_uri),
                ("checksum", restored.checksum_sha256, raw.checksum_sha256),
                ("byte_size", restored.byte_size, raw.byte_size),
                ("record_count", restored.record_count, raw.record_count),
                ("start", restored.start, raw.start),
                ("end", restored.end, raw.end),
                ("adapter_version", restored.adapter_version, raw.adapter_version),
            )
            for field_name, actual, expected in descriptor_fields:
                if actual != expected:
                    issues.append(f"raw descriptor {field_name} mismatch: {raw.artifact_id}")
            if len(payload) != raw.byte_size:
                issues.append(f"raw artifact byte-size mismatch: {raw.artifact_id}")

    return {
        "status": "ok" if not issues else "error",
        "partitions_checked": checked,
        "raw_artifacts_checked": raw_checked,
        "rows_checked": rows_checked,
        "deep": deep,
        "issues": issues,
        "manifest": str(manifest_path),
    }
