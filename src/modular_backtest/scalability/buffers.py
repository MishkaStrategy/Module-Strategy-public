from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from typing import Generic, TypeVar, overload


_T = TypeVar("_T")


class BoundedRecordBuffer(Sequence[_T], Generic[_T]):
    """List-compatible bounded tail for non-authoritative high-frequency records."""

    __slots__ = ("_items",)

    def __init__(self, maxlen: int) -> None:
        if not isinstance(maxlen, int) or isinstance(maxlen, bool):
            raise TypeError("maxlen must be int")
        if maxlen < 1:
            raise ValueError("maxlen must be positive")
        self._items: deque[_T] = deque(maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        value = self._items.maxlen
        if value is None:  # pragma: no cover - constructor always sets it
            raise RuntimeError("bounded buffer lost maxlen")
        return value

    def append(self, item: _T) -> None:
        self._items.append(item)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    @overload
    def __getitem__(self, index: int) -> _T: ...

    @overload
    def __getitem__(self, index: slice) -> list[_T]: ...

    def __getitem__(self, index: int | slice) -> _T | list[_T]:
        if isinstance(index, slice):
            return list(self._items)[index]
        return self._items[index]

    def __iter__(self) -> Iterator[_T]:
        return iter(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence):
            return list(self._items) == list(other)
        return False

    def to_list(self) -> list[_T]:
        return list(self._items)
