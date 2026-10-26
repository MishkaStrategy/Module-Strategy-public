from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol

from .models import Candle, PositionSnapshot
from .validation import utc_datetime


class DataFeed(ABC):
    @abstractmethod
    def __iter__(self) -> Iterator[Candle]:
        raise NotImplementedError


class HistoricalDataFeed(DataFeed):
    def __init__(self, candles: Iterable[Candle]) -> None:
        if isinstance(candles, (str, bytes)) or not isinstance(candles, Iterable):
            raise TypeError("candles must be an iterable of Candle")
        materialized = tuple(candles)
        if any(not isinstance(candle, Candle) for candle in materialized):
            raise TypeError("candles must contain Candle")
        self._candles = materialized

    def __iter__(self) -> Iterator[Candle]:
        return iter(self._candles)


class Clock(Protocol):
    @property
    def now(self) -> datetime | None: ...


class SimulationClock:
    def __init__(self) -> None:
        self._now: datetime | None = None

    @property
    def now(self) -> datetime | None:
        return self._now

    def advance(self, timestamp: datetime) -> None:
        normalized = utc_datetime(timestamp, "clock timestamp")
        if self._now is not None and normalized < self._now:
            raise ValueError("simulation clock cannot move backwards")
        self._now = normalized


class ExecutionAdapter(ABC):
    """Boundary implemented by backtest, paper and live execution adapters."""

    @abstractmethod
    def process_bar(self, bar: Candle, index: int) -> None:
        raise NotImplementedError


class PortfolioRepository(Protocol):
    def snapshot(self, mark_price: float) -> PositionSnapshot | None: ...

    def save(self) -> None: ...
