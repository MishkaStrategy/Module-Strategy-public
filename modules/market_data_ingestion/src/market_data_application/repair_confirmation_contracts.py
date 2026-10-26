from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from market_data_ingestion.enums import DataType
from market_data_ingestion.repair_planning import RepairAction

from .contracts import ContractModel

RepairConfirmationStatus = Literal["active", "reserved", "expired", "consumed", "revoked"]


class CreateRepairConfirmationRequest(ContractModel):
    version: int | None = Field(default=None, gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    selected_data_type: DataType | None = None
    acknowledged_mutations: tuple[str, ...]
    acknowledgement: str = Field(min_length=1, max_length=160)

    @field_validator("acknowledged_mutations")
    @classmethod
    def validate_mutations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if not normalized or any(not item for item in normalized):
            raise ValueError("acknowledged_mutations must contain non-empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("acknowledged_mutations must be unique")
        return normalized


class RepairConfirmationResponse(ContractModel):
    confirmation_id: str = Field(pattern=r"^repair-confirmation:[0-9a-f]{24}$")
    confirmation_token: str = Field(min_length=32, max_length=256)
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    action: RepairAction
    selected_data_type: DataType | None
    acknowledged_mutations: tuple[str, ...]
    actor_id: str = Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    consumed: bool = False


class RepairConfirmationStatusResponse(ContractModel):
    confirmation_id: str = Field(pattern=r"^repair-confirmation:[0-9a-f]{24}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    action: RepairAction
    actor_id: str = Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    status: RepairConfirmationStatus
    reserved_job_id: str | None = Field(default=None, max_length=128)
    reserved_at: datetime | None = None
    consumed_job_id: str | None = Field(default=None, max_length=128)
