from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .control_plane_security import ApiTokenRecord, TokenSnapshotState
from .control_plane_store import AuditRecord
from .security import ApiRole


class TokenProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str = Field(min_length=1, max_length=128)
    token: SecretStr
    role: ApiRole
    not_before: datetime | None = None
    expires_at: datetime | None = None
    revoked: bool = False

    @field_validator("not_before", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("token timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> TokenProvisionRequest:
        if (
            self.not_before is not None
            and self.expires_at is not None
            and self.expires_at <= self.not_before
        ):
            raise ValueError("token expiry must be after activation")
        return self

    def to_record(self, *, default_not_before: datetime) -> ApiTokenRecord:
        return ApiTokenRecord.from_plaintext(
            token_id=self.token_id,
            token=self.token.get_secret_value(),
            role=self.role,
            not_before=self.not_before or default_not_before,
            expires_at=self.expires_at,
            revoked=self.revoked,
        )


class TokenReloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=1)
    overlap_seconds: int = Field(default=0, ge=0, le=3600)
    tokens: tuple[TokenProvisionRequest, ...] = Field(min_length=1, max_length=1024)


class TokenRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=1)


class TokenRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str
    role: ApiRole
    not_before: datetime
    expires_at: datetime | None
    revoked: bool


class TokenStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int
    previous_generation: int | None
    previous_expires_at: datetime | None
    records: tuple[TokenRecordResponse, ...]

    @classmethod
    def from_state(cls, state: TokenSnapshotState) -> TokenStateResponse:
        return cls(
            generation=state.current.generation,
            previous_generation=(state.previous.generation if state.previous is not None else None),
            previous_expires_at=state.previous_expires_at,
            records=tuple(
                TokenRecordResponse(
                    token_id=record.token_id,
                    role=record.role,
                    not_before=record.not_before,
                    expires_at=record.expires_at,
                    revoked=record.revoked,
                )
                for record in state.current.records
            ),
        )


class AuditRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    timestamp: datetime
    correlation_id: str
    direct_peer: str
    principal_id: str | None
    token_id: str | None
    action: str
    outcome: str
    details: dict[str, object]
    previous_mac: str
    record_mac: str

    @classmethod
    def from_record(cls, record: AuditRecord) -> AuditRecordResponse:
        return cls(
            sequence=record.sequence,
            timestamp=record.timestamp,
            correlation_id=record.correlation_id,
            direct_peer=record.direct_peer,
            principal_id=record.principal_id,
            token_id=record.token_id,
            action=record.action,
            outcome=record.outcome,
            details=dict(record.details),
            previous_mac=record.previous_mac,
            record_mac=record.record_mac,
        )
