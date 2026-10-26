from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from modular_core import MarketView

from .enums import SetupAdapterDataError
from .primitives import aware_datetime


class StreamTracker:
    def __init__(self) -> None:
        self.last_bar_time: datetime | None = None
        self.last_bar_signature: tuple[str, ...] | None = None
        self.bound_symbol: str | None = None
        self.bound_timeframe: str | None = None

    def validate(self, view: MarketView) -> bool:
        bar = view.current
        if self.bound_symbol is None:
            self.bound_symbol = bar.symbol
            self.bound_timeframe = bar.timeframe
        elif bar.symbol != self.bound_symbol or bar.timeframe != self.bound_timeframe:
            raise SetupAdapterDataError(
                "one CanonicalSetupAdapterModule instance cannot switch symbol or timeframe"
            )
        signature = self.bar_signature(view)
        if self.last_bar_time is None:
            return False
        if bar.timestamp == self.last_bar_time:
            if signature == self.last_bar_signature:
                return True
            raise SetupAdapterDataError(
                f"contradictory duplicate bar at {bar.timestamp.isoformat()}"
            )
        if bar.timestamp < self.last_bar_time:
            raise SetupAdapterDataError(
                f"out-of-order bar {bar.timestamp.isoformat()} after {self.last_bar_time.isoformat()}"
            )
        return False

    @staticmethod
    def bar_signature(view: MarketView) -> tuple[str, ...]:
        bar = view.current
        return (
            bar.timestamp.isoformat(),
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            bar.symbol,
            bar.timeframe,
        )

    def complete(self, view: MarketView) -> None:
        self.last_bar_time = view.current.timestamp
        self.last_bar_signature = self.bar_signature(view)

    def snapshot(self) -> Mapping[str, object]:
        return {
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "last_bar_signature": (
                list(self.last_bar_signature) if self.last_bar_signature else None
            ),
            "bound_symbol": self.bound_symbol,
            "bound_timeframe": self.bound_timeframe,
        }

    def restore(self, state: Mapping[str, object]) -> None:
        last_bar_time_raw = state.get("last_bar_time")
        last_bar_time = (
            aware_datetime(last_bar_time_raw, "last_bar_time")
            if last_bar_time_raw is not None
            else None
        )
        signature_raw = state.get("last_bar_signature")
        signature: tuple[str, ...] | None = None
        if signature_raw is not None:
            if isinstance(signature_raw, str | bytes) or not isinstance(signature_raw, Iterable):
                raise ValueError("last_bar_signature must be a sequence")
            signature = tuple(str(value) for value in signature_raw)
            if len(signature) != 8:
                raise ValueError("last_bar_signature has invalid length")
        if (last_bar_time is None) != (signature is None):
            raise ValueError("last_bar_time and signature must appear together")
        if (
            signature is not None
            and aware_datetime(signature[0], "signature timestamp") != last_bar_time
        ):
            raise ValueError("last_bar_signature timestamp mismatch")
        bound_symbol = state.get("bound_symbol")
        bound_timeframe = state.get("bound_timeframe")
        if (bound_symbol is None) != (bound_timeframe is None):
            raise ValueError("bound stream fields must appear together")
        if bound_symbol is not None and not str(bound_symbol).strip():
            raise ValueError("bound_symbol cannot be blank")
        if bound_timeframe is not None and not str(bound_timeframe).strip():
            raise ValueError("bound_timeframe cannot be blank")
        if (
            signature is not None
            and bound_symbol is not None
            and (signature[6] != str(bound_symbol) or signature[7] != str(bound_timeframe))
        ):
            raise ValueError("last_bar_signature contradicts bound stream")
        self.last_bar_time = last_bar_time
        self.last_bar_signature = signature
        self.bound_symbol = str(bound_symbol) if bound_symbol is not None else None
        self.bound_timeframe = str(bound_timeframe) if bound_timeframe is not None else None
