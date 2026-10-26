from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from market_data_ingestion.enums import DataType
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.registry import SQLiteDatasetRegistry, descriptor_from_dict
from market_data_ingestion.repair_planning import preview_registered_dataset_repair

from .errors import ApplicationError, DatasetNotFoundError
from .inspection_contracts import (
    DatasetValidationResponse,
    RepairAffectedRangeResponse,
    RepairPlanPreviewResponse,
)


class DatasetInspection:
    """Read-only validation and repair planning over registered immutable datasets."""

    def __init__(self, registry: SQLiteDatasetRegistry, storage_root: Path) -> None:
        self._registry = registry
        self._storage_root = storage_root.expanduser().resolve()

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        deep: bool = False,
    ) -> DatasetValidationResponse:
        payload = self._descriptor_payload(dataset_id, version)
        descriptor = descriptor_from_dict(payload)
        try:
            report = verify_registered_dataset(
                self._storage_root,
                payload,
                deep=deep,
            )
        except Exception as exc:
            raise ApplicationError(
                "DATASET_VALIDATION_FAILED",
                "registered dataset validation could not be completed",
                False,
                "dataset_validation",
                {
                    "dataset_id": descriptor.dataset_id,
                    "version": descriptor.version,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        normalized_status: Literal["ok", "error"] = (
            "ok" if report.get("status") == "ok" else "error"
        )
        return DatasetValidationResponse(
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
            dataset_hash=descriptor.dataset_hash,
            validation_status=descriptor.validation_status,
            status=normalized_status,
            deep=deep,
            partitions_checked=self._count(report, "partitions_checked"),
            raw_artifacts_checked=self._count(report, "raw_artifacts_checked"),
            rows_checked=self._count(report, "rows_checked"),
            issues=self._issues(report),
            manifest_uri=descriptor.manifest_uri,
        )

    def preview_repair(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        data_type: DataType | None = None,
        deep: bool = False,
    ) -> RepairPlanPreviewResponse:
        payload = self._descriptor_payload(dataset_id, version)
        validation = self.validate_dataset(
            dataset_id,
            version=version,
            deep=deep,
        )
        try:
            plan = preview_registered_dataset_repair(
                self._storage_root,
                payload,
                data_type=data_type,
            )
        except Exception as exc:
            raise ApplicationError(
                "REPAIR_PREVIEW_FAILED",
                "repair plan preview could not be generated",
                False,
                "repair_preview",
                {
                    "dataset_id": validation.dataset_id,
                    "version": validation.version,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        return RepairPlanPreviewResponse(
            dataset_id=plan.dataset_id,
            version=plan.version,
            preview_id=plan.preview_id,
            action=plan.action,
            selected_data_type=plan.selected_data_type,
            checked_partitions=plan.checked_partitions,
            damaged_partitions=plan.damaged_partitions,
            unresolved_gaps=plan.unresolved_gaps,
            affected_ranges=tuple(
                RepairAffectedRangeResponse(
                    data_type=item.data_type,
                    timeframe=item.timeframe,
                    start=item.start,
                    end=item.end,
                    source=item.source,
                    partition_id=item.partition_id,
                )
                for item in plan.affected_ranges
            ),
            evidence=plan.evidence,
            expected_mutations=plan.expected_mutations,
            confirmation_required=plan.confirmation_required,
            requires_network=plan.requires_network,
            validation=validation,
        )

    def _descriptor_payload(self, dataset_id: str, version: int | None) -> dict[str, Any]:
        payload = self._registry.inspect(dataset_id, version)
        if payload is None:
            raise DatasetNotFoundError(dataset_id=dataset_id, version=version)
        return payload

    @staticmethod
    def _count(report: dict[str, Any], key: str) -> int:
        value = report.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _issues(report: dict[str, Any]) -> tuple[str, ...]:
        value = report.get("issues", ())
        if not isinstance(value, (list, tuple)):
            return ("validation report issues payload is invalid",)
        return tuple(str(item) for item in value)
