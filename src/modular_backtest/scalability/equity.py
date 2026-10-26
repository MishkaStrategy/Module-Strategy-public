from __future__ import annotations

from array import array
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import overload

from modular_core.models import EquityPoint


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _datetime_to_epoch_us(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class CompactEquitySeries(Sequence[EquityPoint]):
    """Exact per-bar equity stored in compact primitive columns."""

    __slots__ = ("_timestamps_us", "_cash", "_unrealized", "_equity", "_position")

    def __init__(self) -> None:
        self._timestamps_us = array("q")
        self._cash = array("d")
        self._unrealized = array("d")
        self._equity = array("d")
        self._position = bytearray()

    def append(self, point: EquityPoint) -> None:
        if not isinstance(point, EquityPoint):
            raise TypeError("equity series requires EquityPoint")
        self._timestamps_us.append(_datetime_to_epoch_us(point.timestamp))
        self._cash.append(point.cash)
        self._unrealized.append(point.unrealized_pnl)
        self._equity.append(point.equity)
        self._position.append(1 if point.position_open else 0)

    def __len__(self) -> int:
        return len(self._equity)

    @overload
    def __getitem__(self, index: int) -> EquityPoint: ...

    @overload
    def __getitem__(self, index: slice) -> list[EquityPoint]: ...

    def __getitem__(self, index: int | slice) -> EquityPoint | list[EquityPoint]:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        return EquityPoint(
            timestamp=datetime.fromtimestamp(self._timestamps_us[normalized] / 1_000_000, tz=UTC),
            cash=self._cash[normalized],
            unrealized_pnl=self._unrealized[normalized],
            equity=self._equity[normalized],
            position_open=bool(self._position[normalized]),
        )

    def __setitem__(self, index: int, point: EquityPoint) -> None:
        if not isinstance(point, EquityPoint):
            raise TypeError("equity series requires EquityPoint")
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        self._timestamps_us[normalized] = _datetime_to_epoch_us(point.timestamp)
        self._cash[normalized] = point.cash
        self._unrealized[normalized] = point.unrealized_pnl
        self._equity[normalized] = point.equity
        self._position[normalized] = 1 if point.position_open else 0

    def __iter__(self) -> Iterator[EquityPoint]:
        for index in range(len(self)):
            yield self[index]


class EquitySampler:
    __slots__ = (
        "policy",
        "every_n",
        "_last_hour",
        "_last_day",
        "_last_equity",
        "_last_position_open",
    )

    def __init__(self, policy: str, every_n: int) -> None:
        self.policy = policy
        self.every_n = every_n
        self._last_hour: tuple[int, int, int, int] | None = None
        self._last_day: tuple[int, int, int] | None = None
        self._last_equity: float | None = None
        self._last_position_open: bool | None = None

    def accept(
        self,
        point: EquityPoint,
        *,
        bar_index: int,
        trade_count_changed: bool,
        force: bool = False,
    ) -> bool:
        if force or self.policy == "every_bar":
            accepted = True
        elif self.policy == "every_n_bars":
            accepted = bar_index % self.every_n == 0
        elif self.policy == "hourly":
            hour_key = (
                point.timestamp.year,
                point.timestamp.month,
                point.timestamp.day,
                point.timestamp.hour,
            )
            accepted = hour_key != self._last_hour
            self._last_hour = hour_key
        elif self.policy == "daily":
            day_key = (point.timestamp.year, point.timestamp.month, point.timestamp.day)
            accepted = day_key != self._last_day
            self._last_day = day_key
        elif self.policy == "on_change":
            accepted = self._last_equity is None or point.equity != self._last_equity
        elif self.policy == "on_trade":
            accepted = trade_count_changed
        else:
            hybrid_key = (
                point.timestamp.year,
                point.timestamp.month,
                point.timestamp.day,
                point.timestamp.hour,
            )
            position_changed = (
                self._last_position_open is None or point.position_open != self._last_position_open
            )
            accepted = (
                bar_index % self.every_n == 0
                or hybrid_key != self._last_hour
                or trade_count_changed
                or self._last_equity is None
                or position_changed
            )
            self._last_hour = hybrid_key
        self._last_equity = point.equity
        self._last_position_open = point.position_open
        return accepted
