from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .contracts import ComponentId, ContractModel, SafeScalar, _aware, _bounded_details


class ApiVersion(ContractModel):
    api_version: Literal["v1"] = "v1"
    mode: Literal["PAPER"] = "PAPER"
    remote_access_allowed: Literal[False] = False


class ApiErrorDetail(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    action: str | None = Field(default=None, max_length=2000)
    error_id: str = Field(min_length=1, max_length=128)
    component: ComponentId | None = None
    operation: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    retryable: bool = False
    technical_details: dict[str, SafeScalar] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_aware_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")

    @field_validator("technical_details")
    @classmethod
    def validate_technical_details(
        cls,
        value: dict[str, SafeScalar],
    ) -> dict[str, SafeScalar]:
        return _bounded_details(value)


class ApiError(ContractModel):
    error: ApiErrorDetail
