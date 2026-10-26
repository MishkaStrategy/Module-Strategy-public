from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from market_data_ingestion.enums import DataType, ValidationStatus
from market_data_ingestion.repair_planning import RepairAction

from .contracts import ContractModel


class DatasetValidationResponse(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_status: ValidationStatus
    status: Literal["ok", "error"]
    deep: bool
    partitions_checked: int = Field(ge=0)
    raw_artifacts_checked: int = Field(ge=0)
    rows_checked: int = Field(ge=0)
    issues: tuple[str, ...]
    manifest_uri: str = Field(min_length=1)


class RepairAffectedRangeResponse(ContractModel):
    data_type: DataType
    timeframe: str | None
    start: datetime
    end: datetime
    source: Literal["partition_integrity", "validation_gap"]
    partition_id: str | None = None


class RepairPlanPreviewResponse(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    action: RepairAction
    selected_data_type: DataType | None
    checked_partitions: int = Field(ge=0)
    damaged_partitions: int = Field(ge=0)
    unresolved_gaps: int = Field(ge=0)
    affected_ranges: tuple[RepairAffectedRangeResponse, ...]
    evidence: tuple[dict[str, Any], ...]
    expected_mutations: tuple[str, ...]
    confirmation_required: bool
    requires_network: bool
    validation: DatasetValidationResponse
