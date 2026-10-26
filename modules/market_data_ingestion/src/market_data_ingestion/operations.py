from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .canonical import sha256_file
from .contracts import NormalizedPartitionDescriptor, RepairResult
from .enums import DataType
from .errors import StructuredIngestionError
from .integrity import RegisteredPathError, resolve_registered_path
from .manifest_io import load_manifest
from .operations_impl import DatasetOperations as _DatasetOperations


class DatasetOperations(_DatasetOperations):
    """Dataset operations with immutable quarantine evidence for damaged repairs."""

    def repair(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        data_type: DataType | None = None,
        request_id: str | None = None,
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> RepairResult:
        parent = self._descriptor(dataset_id, version)
        manifest = load_manifest(self._storage.root, parent)
        candidates = tuple(
            item
            for item in manifest.partitions
            if (
                item.storage_uri.startswith("normalized/")
                or item.storage_uri.startswith("derived/")
            )
            and (data_type is None or item.data_type is data_type)
        )
        damaged: list[NormalizedPartitionDescriptor] = []
        issues: list[dict[str, object]] = []
        root = self._storage.root.resolve()
        for partition in candidates:
            issue = self._partition_integrity_issue(root, partition)
            if issue is not None:
                damaged.append(partition)
                issues.append(issue)

        quarantine = self._quarantine_single_type_damage(
            dataset_id=parent.dataset_id,
            version=parent.version,
            damaged=tuple(damaged),
            issues=tuple(issues),
        )
        result = super().repair(
            dataset_id,
            version=version,
            data_type=data_type,
            request_id=request_id,
            format_name=format_name,
            compression=compression,
        )
        if not quarantine:
            return result
        return self._link_quarantine_audit(result, quarantine)

    def _quarantine_single_type_damage(
        self,
        *,
        dataset_id: str,
        version: int,
        damaged: tuple[NormalizedPartitionDescriptor, ...],
        issues: tuple[dict[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        if not damaged:
            return ()
        if len(damaged) != len(issues):
            raise StructuredIngestionError(
                "REPAIR_EVIDENCE_MISMATCH",
                "damaged partition evidence is internally inconsistent",
                False,
                "repair",
            )
        if len({item.data_type for item in damaged}) > 1:
            # The base implementation stages one data type per child repair. Those child
            # calls dispatch back through this wrapper and quarantine each exact stream.
            return ()

        evidence: list[dict[str, object]] = []
        root = self._storage.root.resolve()
        for partition, issue in zip(damaged, issues, strict=True):
            item = dict(issue)
            payload, extension = self._quarantine_payload(
                dataset_id=dataset_id,
                version=version,
                partition=partition,
                issue=item,
                root=root,
            )
            content_hash = hashlib.sha256(payload).hexdigest()
            safe_id = partition.partition_id.replace(":", "-")
            name = (
                f"repair-{dataset_id}-v{version}-{safe_id}-{content_hash[:16]}{extension}.damaged"
            )
            quarantined = self._storage.quarantine(name, payload)
            item["partition_id"] = partition.partition_id
            item["quarantine_uri"] = quarantined.relative_to(self._storage.root).as_posix()
            item["quarantine_sha256"] = sha256_file(quarantined)
            item["quarantine_byte_size"] = quarantined.stat().st_size
            evidence.append(item)
        return tuple(evidence)

    @staticmethod
    def _quarantine_payload(
        *,
        dataset_id: str,
        version: int,
        partition: NormalizedPartitionDescriptor,
        issue: dict[str, object],
        root: Path,
    ) -> tuple[bytes, str]:
        try:
            source = resolve_registered_path(root, partition.storage_uri, kind="partition")
        except RegisteredPathError:
            source = None
        if source is not None and source.is_file() and not source.is_symlink():
            return source.read_bytes(), Path(partition.storage_uri).suffix or ".bin"
        payload = json.dumps(
            {
                "dataset_id": dataset_id,
                "version": version,
                "partition_id": partition.partition_id,
                "storage_uri": partition.storage_uri,
                "issue": issue.get("issue"),
                "evidence_only": True,
            },
            indent=2,
            sort_keys=True,
        ).encode()
        return payload, ".json"

    def _link_quarantine_audit(
        self,
        result: RepairResult,
        quarantine: tuple[dict[str, object], ...],
    ) -> RepairResult:
        original = self._storage.root / result.audit_uri
        audit = json.loads(original.read_text(encoding="utf-8"))
        by_partition = {
            str(item["partition_id"]): item for item in quarantine if "partition_id" in item
        }
        observed = audit.get("evidence")
        if isinstance(observed, list):
            for entry in observed:
                if not isinstance(entry, dict):
                    continue
                partition_id = entry.get("partition_id")
                linked = by_partition.get(str(partition_id))
                if linked is not None:
                    entry.update(
                        {
                            key: linked[key]
                            for key in (
                                "quarantine_uri",
                                "quarantine_sha256",
                                "quarantine_byte_size",
                            )
                        }
                    )
        audit["quarantine_evidence"] = list(quarantine)
        audit["quarantine_linked_at"] = datetime.now(UTC)
        linked_name = f"{original.stem}-quarantine"
        linked_path = self._storage.write_repair_audit(linked_name, audit)
        return replace(
            result,
            audit_uri=linked_path.relative_to(self._storage.root).as_posix(),
        )


__all__ = ["DatasetOperations"]
