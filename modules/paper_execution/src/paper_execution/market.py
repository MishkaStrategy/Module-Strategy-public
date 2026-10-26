from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from ._utils import decimal, utc


_MARKET_ALIASES: dict[str, frozenset[str]] = {
    "linear_futures": frozenset({"linear_futures", "binance_usdtm", "usdt_perpetual"}),
    "inverse_futures": frozenset({"inverse_futures", "binance_coinm"}),
    "spot": frozenset({"spot", "binance_spot"}),
}


def markets_compatible(plan_market: str, venue_market_type: str) -> bool:
    """Return whether a canonical plan market matches venue metadata.

    Compatibility is explicit and fail-closed. Unknown aliases never match a
    different venue market type.
    """

    if not isinstance(plan_market, str) or not plan_market.strip():
        return False
    if not isinstance(venue_market_type, str) or not venue_market_type.strip():
        return False
    if plan_market == venue_market_type:
        return True
    return plan_market in _MARKET_ALIASES.get(venue_market_type, frozenset())


@dataclass(frozen=True, slots=True)
class VenueMarkSnapshot:
    instrument: str
    price: Decimal
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        for name in ("instrument", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
        object.__setattr__(
            self,
            "price",
            decimal(self.price, "mark price", positive=True),
        )
        object.__setattr__(
            self,
            "observed_at",
            utc(self.observed_at, "mark observed_at"),
        )

    def validate_for(self, *, instrument: str, timestamp: datetime) -> None:
        expected_time = utc(timestamp, "mark request timestamp")
        if self.instrument != instrument:
            raise ValueError("mark snapshot instrument does not match execution scope")
        if self.observed_at != expected_time:
            raise ValueError("mark snapshot is stale or from a future timestamp")


@runtime_checkable
class VenueMarkProvider(Protocol):
    def mark_snapshot(
        self,
        instrument: str,
        timestamp: datetime,
    ) -> VenueMarkSnapshot: ...
