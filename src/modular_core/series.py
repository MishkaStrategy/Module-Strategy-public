from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Generic, TypeVar, overload

T = TypeVar("T")


class ReadOnlySeries(Sequence[T], Generic[T]):
    """O(1) snapshot boundary over an append-only sequence.

    The visible end is captured at construction, so a saved view never gains
    access to elements appended later by the engine.
    """

    __slots__ = ("__source", "__start", "__end")

    def __init__(
        self,
        source: Sequence[T],
        end: int | None = None,
        *,
        start: int = 0,
    ) -> None:
        if not isinstance(source, Sequence):
            raise TypeError("source must be a sequence")
        if not isinstance(start, int) or isinstance(start, bool):
            raise TypeError("start must be int")
        if end is not None and (not isinstance(end, int) or isinstance(end, bool)):
            raise TypeError("end must be int or None")
        visible_end = len(source) if end is None else end
        if not 0 <= start <= visible_end <= len(source):
            raise ValueError("snapshot bounds are outside source")
        self.__source = source
        self.__start = start
        self.__end = visible_end

    def __len__(self) -> int:
        return self.__end - self.__start

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[T, ...]: ...

    def __getitem__(self, index: int | slice) -> T | tuple[T, ...]:
        length = len(self)
        if isinstance(index, slice):
            start, stop, step = index.indices(length)
            return tuple(
                self.__source[self.__start + offset] for offset in range(start, stop, step)
            )
        normalized = index + length if index < 0 else index
        if normalized < 0 or normalized >= length:
            raise IndexError("series index out of range")
        return self.__source[self.__start + normalized]

    def __iter__(self) -> Iterator[T]:
        for index in range(self.__start, self.__end):
            yield self.__source[index]
