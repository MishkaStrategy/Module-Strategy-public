from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from market_data_ingestion.canonical import sha256_canonical
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState, Severity


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    schema_version: Literal[1] = 1


class JobOperation(StrEnum):
    DOWNLOAD = "download"
    UPDATE = "update"
    VALIDATE = "validate"
    REPAIR = "repair"
    BACKFILL = "backfill"
    EXPORT = "export"


class JobEventType(StrEnum):
    STATE = "state"
    PROGRESS = "progress"
    LOG = "log"
    WARNING = "warning"
    RESULT = "result"
    ERROR = "error"


class StorageFormat(StrEnum):
    PARQUET = "parquet"
    CSV = "csv"


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def canonical_request_payload(request: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        payload = request.model_dump(mode="json", by_alias=True, exclude_none=False)
    else:
        payload = dict(request)
    return payload


def canonical_request_hash(request: BaseModel | Mapping[str, Any]) -> str:
    return sha256_canonical(canonical_request_payload(request))


class CreateDownloadRequest(ContractModel):
    exchange_id: str = Field(min_length=1, max_length=64)
    market_type: MarketType
    symbol: str = Field(min_length=1, max_length=64)
    required_data: tuple[DataType, ...] = (DataType.KLINES,)
    optional_data: tuple[DataType, ...] = ()
    base_timeframe: str = Field(default="5m", min_length=1, max_length=16)
    derived_timeframes: tuple[str, ...] = ()
    start: datetime | None = None
    end: datetime
    full_available_history: bool = False
    allow_provisional: bool = False
    storage_format: StorageFormat = StorageFormat.PARQUET
    compression: str = Field(default="zstd", min_length=1, max_length=32)
    reject_unresolved_required_gaps: bool = True
    concurrency: int = Field(default=1, ge=1, le=64)
    max_records_in_memory: int = Field(default=5_000_000, ge=1)

    @field_validator("exchange_id")
    @classmethod
    def normalize_exchange(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("base_timeframe")
    @classmethod
    def normalize_base_timeframe(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("derived_timeframes")
    @classmethod
    def normalize_derived_timeframes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().lower() for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("derived_timeframes must be unique")
        return normalized

    @field_validator("start", "end")
    @classmethod
    def normalize_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_request(self) -> CreateDownloadRequest:
        if not self.required_data:
            raise ValueError("required_data must not be empty")
        if len(set(self.required_data)) != len(self.required_data):
            raise ValueError("required_data must be unique")
        if len(set(self.optional_data)) != len(self.optional_data):
            raise ValueError("optional_data must be unique")
        if set(self.required_data) & set(self.optional_data):
            raise ValueError("required_data and optional_data must not overlap")
        if self.start is None and not self.full_available_history:
            raise ValueError("start is required unless full_available_history is true")
        if self.start is not None and self.start >= self.end:
            raise ValueError("start must precede end")
        return self

    def to_domain(self, *, request_id: str) -> MarketDataRequest:
        return MarketDataRequest(
            request_id=request_id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            symbol=self.symbol,
            required_data=self.required_data,
            optional_data=self.optional_data,
            base_timeframe=self.base_timeframe,
            derived_timeframes=self.derived_timeframes,
            start=self.start,
            end=self.end,
            full_available_history=self.full_available_history,
            allow_provisional=self.allow_provisional,
        )


class JobProgress(ContractModel):
    phase: RunState
    determinate: bool = False
    completed_units: int = Field(default=0, ge=0)
    total_units: int | None = Field(default=None, ge=1)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    message: str | None = Field(default=None, max_length=500)
    counters: dict[str, int] = Field(default_factory=dict)
    current_range_start: datetime | None = None
    current_range_end: datetime | None = None

    @field_validator("current_range_start", "current_range_end")
    @classmethod
    def normalize_ranges(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware_utc(value, info.field_name)

    @field_validator("counters")
    @classmethod
    def validate_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not name.strip() or count < 0 for name, count in value.items()):
            raise ValueError("counter names must be non-empty and values non-negative")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_determinate(self) -> JobProgress:
        if self.determinate:
            if self.total_units is None:
                raise ValueError("determinate progress requires total_units")
            if self.completed_units > self.total_units:
                raise ValueError("completed_units must not exceed total_units")
            expected = round(self.completed_units * 100.0 / self.total_units, 6)
            if self.percent is not None and abs(self.percent - expected) > 0.001:
                raise ValueError("percent must match completed_units / total_units")
            object.__setattr__(self, "percent", expected)
        elif self.percent is not None or self.total_units is not None:
            raise ValueError("indeterminate progress must not include percent or total_units")
        if (
            self.current_range_start is not None
            and self.current_range_end is not None
            and self.current_range_start >= self.current_range_end
        ):
            raise ValueError("current_range_start must precede current_range_end")
        return self


class JobError(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    stage: str | None = Field(default=None, max_length=128)
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(min_length=1, max_length=128)


class JobEvent(ContractModel):
    event_id: int = Field(ge=1)
    job_id: str = Field(min_length=1, max_length=128)
    event_type: JobEventType
    state: RunState
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "timestamp")


class JobSummary(ContractModel):
    job_id: str = Field(min_length=1, max_length=128)
    operation: JobOperation
    state: RunState
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_run_id: str | None = Field(default=None, max_length=128)
    progress: JobProgress | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("created_at", "updated_at", "started_at", "completed_at")
    @classmethod
    def normalize_job_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return None if value is None else _aware_utc(value, info.field_name)


class JobDetails(JobSummary):
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: JobError | None = None
    warnings: tuple[str, ...] = ()
    correlation_id: str = Field(min_length=1, max_length=128)
    actor_id: str | None = Field(default=None, max_length=128)


class JobCreatedResponse(ContractModel):
    job: JobSummary
    reused: bool = False


class Pagination(ContractModel):
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class JobListResponse(ContractModel):
    items: tuple[JobSummary, ...]
    pagination: Pagination


class ApiErrorDetail(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(min_length=1, max_length=128)


class ApiError(ContractModel):
    error: ApiErrorDetail


class HealthResponse(ContractModel):
    status: Literal["ok", "degraded", "error"]
    component: Literal["market_data_api"] = "market_data_api"
    api_version: Literal["v1"] = "v1"
    build_version: str = Field(min_length=1, max_length=64)
    checks: dict[str, str] = Field(default_factory=dict)


class DoctorCheck(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    status: Literal["ok", "warning", "error"]
    explanation: str = Field(min_length=1, max_length=2000)
    remediation: str | None = Field(default=None, max_length=2000)
    severity: Severity = Severity.INFO


class DoctorResponse(ContractModel):
    status: Literal["ok", "degraded", "error"]
    checks: tuple[DoctorCheck, ...]
