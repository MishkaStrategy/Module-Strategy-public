from __future__ import annotations

import queue
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class InjectedProcessExit(RuntimeError):
    """Synthetic process termination raised only by the acceptance test harness."""


class InjectedTransportUnavailable(ConnectionError):
    """Synthetic transport outage raised only by the acceptance test harness."""


class InjectedResponseTimeout(TimeoutError):
    """Synthetic lost response after an owner commit."""


def process_kill_before_commit(commit: Callable[[], T]) -> None:
    raise InjectedProcessExit("injected process exit before authoritative commit")


def process_kill_after_commit(commit: Callable[[], T]) -> None:
    commit()
    raise InjectedProcessExit("injected process exit after authoritative commit")


def stale_timestamp(now: datetime, *, age: timedelta) -> datetime:
    if age <= timedelta(0):
        raise ValueError("age must be positive")
    return now - age


def out_of_order[TItem](items: Sequence[TItem]) -> tuple[TItem, ...]:
    if len(items) < 2:
        raise ValueError("out_of_order requires at least two items")
    return tuple(reversed(items))


@contextmanager
def exclusive_sqlite_lock(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=0.01, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def truncate_copy(payload: bytes) -> bytes:
    if len(payload) < 2:
        raise ValueError("payload must contain at least two bytes")
    return payload[: max(1, len(payload) // 2)]


def jump_clock(now: datetime, *, delta: timedelta) -> datetime:
    if delta == timedelta(0):
        raise ValueError("clock jump delta cannot be zero")
    return now + delta


def unavailable_transport() -> None:
    raise InjectedTransportUnavailable("injected owner transport unavailable")


def drop_response_after_commit(commit: Callable[[], T]) -> None:
    commit()
    raise InjectedResponseTimeout("injected response timeout after authoritative commit")


def bounded_queue[TItem](*, capacity: int) -> queue.Queue[TItem]:
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    return queue.Queue(maxsize=capacity)


def recovery_order_out_of_order(owners: Sequence[str]) -> tuple[str, ...]:
    if len(owners) < 2:
        raise ValueError("recovery order requires at least two owners")
    return tuple(reversed(owners))
