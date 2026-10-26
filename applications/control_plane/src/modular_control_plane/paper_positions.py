"""Owner-fact PAPER position snapshot contracts; intentionally not attached to a reader.

The owning Paper Execution/Core adapter must provide one complete, version-consistent
snapshot before constructing the available variant. The composition API owns exact
engine membership, exclusive writer/recovery access, and the whole-portfolio commit;
this DTO describes that committed result but does not itself guarantee atomicity.
Partial pages and mixed-version results belong in an unavailable response. This module
carries no valuation, ledger, fill reconstruction, freshness threshold, or
runtime-instrument inference policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .contracts import ContractModel

_OPAQUE_ID = Field(strict=True, min_length=1, max_length=256)


def _stable_text(value: str, label: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be non-blank without surrounding whitespace")
    return value


class PaperPositionFact(ContractModel):
    """A factual owner position with opaque owner identity and optional owner attribution.

    ``owner_position_id`` is unique only within this snapshot unless a later policy
    defines a wider scope. ``strategy_id`` is owner-native attribution and may be
    absent when unknown; consumers must not infer it. Unknown attribution is never a
    strategy-flat signal and never authorizes strategy-scoped close, protection, or
    cancel. Any future strategy-scoped mutation requires authoritative owner binding
    of the strategy instance, exact position/order, and current composition,
    membership, and state scope, or an owner-approved durable ID/restore scheme.
    """

    owner_position_id: str = _OPAQUE_ID
    instrument_id: str = _OPAQUE_ID
    instrument_version: str = _OPAQUE_ID
    side: Literal["long", "short"]
    quantity: str = Field(strict=True, min_length=1, max_length=128)
    quantity_unit: str = Field(strict=True, min_length=1, max_length=64)
    strategy_id: str | None = Field(default=None, strict=True, min_length=1, max_length=256)

    @field_validator("owner_position_id", "instrument_id", "instrument_version")
    @classmethod
    def validate_identity(cls, value: str, info: object) -> str:
        return _stable_text(value, getattr(info, "field_name", "identity"))

    @field_validator("quantity_unit", "strategy_id")
    @classmethod
    def validate_optional_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _stable_text(value, getattr(info, "field_name", "value"))

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("quantity must not contain surrounding whitespace")
        try:
            quantity = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("quantity must be a decimal string") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        return value


class PaperPositionsAvailableSnapshot(ContractModel):
    """A complete owner snapshot. Empty positions means flat only in this variant.

    ``membership_version`` identifies the exact owner engine-membership set included
    in this whole-portfolio commit. ``state_version`` is the composition-wide
    committed version across that membership, while ``snapshot_version`` identifies
    this immutable capture. ``owner_generation`` identifies the owner's restart or
    recovery incarnation. ``as_of`` is observation time; freshness is governed by
    owner-approved policy, not by a threshold in this DTO.
    """

    status: Literal["available"] = "available"
    owner_id: str = _OPAQUE_ID
    composition_id: str = _OPAQUE_ID
    portfolio_id: str = _OPAQUE_ID
    owner_generation: str = _OPAQUE_ID
    membership_version: str = _OPAQUE_ID
    snapshot_version: str = _OPAQUE_ID
    state_version: str = _OPAQUE_ID
    as_of: datetime
    coverage: Literal["complete"]
    positions: tuple[PaperPositionFact, ...]

    @field_validator(
        "owner_id",
        "composition_id",
        "portfolio_id",
        "owner_generation",
        "membership_version",
        "snapshot_version",
        "state_version",
    )
    @classmethod
    def validate_identity(cls, value: str, info: object) -> str:
        return _stable_text(value, getattr(info, "field_name", "identity"))

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_unique_owner_positions(self) -> PaperPositionsAvailableSnapshot:
        identifiers = tuple(position.owner_position_id for position in self.positions)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("owner_position_id values must be unique within a snapshot")
        return self


class PaperPositionsUnavailableSnapshot(ContractModel):
    status: Literal["unavailable"] = "unavailable"
    reason_code: str = Field(strict=True, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class PaperPositionsRecoveringSnapshot(ContractModel):
    status: Literal["recovering"] = "recovering"
    reason_code: str = Field(strict=True, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class PaperPositionsStaleSnapshot(ContractModel):
    status: Literal["stale"] = "stale"
    reason_code: str = Field(strict=True, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


PaperPositionsSnapshot = Annotated[
    PaperPositionsAvailableSnapshot
    | PaperPositionsUnavailableSnapshot
    | PaperPositionsRecoveringSnapshot
    | PaperPositionsStaleSnapshot,
    Field(discriminator="status"),
]

paper_positions_snapshot_adapter: TypeAdapter[PaperPositionsSnapshot] = TypeAdapter(
    PaperPositionsSnapshot
)


__all__ = [
    "PaperPositionFact",
    "PaperPositionsAvailableSnapshot",
    "PaperPositionsRecoveringSnapshot",
    "PaperPositionsSnapshot",
    "PaperPositionsStaleSnapshot",
    "PaperPositionsUnavailableSnapshot",
    "paper_positions_snapshot_adapter",
]
