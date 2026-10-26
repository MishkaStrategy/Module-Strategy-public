from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from modular_core import PositionSide

from ._utils import decimal, utc
from .enums import PaperReasonCode
from .exceptions import PlanValidationError


@dataclass(frozen=True, slots=True)
class RouterPositionSnapshot:
    portfolio_id: str
    instrument: str
    position_id: str
    side: PositionSide
    quantity: Decimal
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        for name in (
            "portfolio_id",
            "instrument",
            "position_id",
            "source",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
        normalized_side = (
            self.side if isinstance(self.side, PositionSide) else PositionSide(self.side)
        )
        object.__setattr__(self, "side", normalized_side)
        object.__setattr__(
            self,
            "quantity",
            decimal(self.quantity, "Router position quantity", positive=True),
        )
        object.__setattr__(
            self,
            "observed_at",
            utc(self.observed_at, "Router position observed_at"),
        )

    def validate_for(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        position_id: str,
        side: PositionSide,
        quantity: Decimal,
        timestamp: datetime,
    ) -> None:
        if (
            self.portfolio_id != portfolio_id
            or self.instrument != instrument
            or self.position_id != position_id
            or self.side is not side
            or self.quantity != quantity
            or self.observed_at != utc(timestamp, "Router position timestamp")
        ):
            raise PlanValidationError(
                PaperReasonCode.STATE_VERSION_MISMATCH,
                "factual target position does not match Router PositionIntent",
                retryable=True,
            )


@runtime_checkable
class RouterPositionProvider(Protocol):
    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterPositionSnapshot: ...
