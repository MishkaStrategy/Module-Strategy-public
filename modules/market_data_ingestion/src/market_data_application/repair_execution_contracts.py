from __future__ import annotations

from pydantic import Field, SecretStr

from market_data_ingestion.enums import DataType
from market_data_ingestion.repair_planning import RepairAction

from .contracts import ContractModel, StorageFormat


class CreateRepairExecutionRequest(ContractModel):
    version: int = Field(gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    selected_data_type: DataType | None = None
    confirmation_id: str = Field(pattern=r"^repair-confirmation:[0-9a-f]{24}$")
    confirmation_token: SecretStr = Field(min_length=32, max_length=256)
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)

    def persisted(self, *, dataset_id: str, action: RepairAction) -> RepairJobRequest:
        return RepairJobRequest(
            dataset_id=dataset_id,
            version=self.version,
            preview_id=self.preview_id,
            selected_data_type=self.selected_data_type,
            confirmation_id=self.confirmation_id,
            action=action,
            storage_format=self.storage_format,
            compression=self.compression,
        )


class RepairJobRequest(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    preview_id: str = Field(pattern=r"^repair-preview:[0-9a-f]{24}$")
    selected_data_type: DataType | None = None
    confirmation_id: str = Field(pattern=r"^repair-confirmation:[0-9a-f]{24}$")
    action: RepairAction
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)
