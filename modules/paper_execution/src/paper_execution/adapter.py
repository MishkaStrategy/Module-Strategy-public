from __future__ import annotations

from datetime import datetime
from typing import Protocol

from modular_core import (
    CancelRequest,
    Candle,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    PositionUpdateRequest,
)
from modular_core.models import Order


class CorePaperVenue(Protocol):
    def submit(
        self,
        request: OrderRequest,
        *,
        source_module: str,
        created_at: datetime,
        current_index: int,
        mark_price: float,
    ) -> Order | None: ...

    def cancel(self, request: CancelRequest, *, timestamp: datetime, source_module: str) -> int: ...

    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]: ...

    def process_bar(self, bar: Candle, index: int) -> None: ...

    def update_position(
        self,
        request: PositionUpdateRequest,
        *,
        timestamp: datetime,
        source_module: str,
        mark_price: float,
    ) -> bool: ...


class PositionSnapshotProvider(Protocol):
    def position_snapshot(self, mark_price: float) -> PositionSnapshot | None: ...
