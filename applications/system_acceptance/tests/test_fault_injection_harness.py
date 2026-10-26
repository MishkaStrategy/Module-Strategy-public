from __future__ import annotations

import queue
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from fault_injection_harness import (
    InjectedProcessExit,
    InjectedResponseTimeout,
    InjectedTransportUnavailable,
    bounded_queue,
    drop_response_after_commit,
    exclusive_sqlite_lock,
    jump_clock,
    out_of_order,
    process_kill_after_commit,
    process_kill_before_commit,
    recovery_order_out_of_order,
    stale_timestamp,
    truncate_copy,
    unavailable_transport,
)

NOW = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)


def test_process_kill_windows_and_timeout_have_deterministic_commit_boundary() -> None:
    committed: list[str] = []

    with pytest.raises(InjectedProcessExit):
        process_kill_before_commit(lambda: committed.append("before"))
    assert committed == []

    with pytest.raises(InjectedProcessExit):
        process_kill_after_commit(lambda: committed.append("after"))
    with pytest.raises(InjectedResponseTimeout):
        drop_response_after_commit(lambda: committed.append("timeout"))

    assert committed == ["after", "timeout"]


def test_timestamp_order_corruption_and_truncation_are_reproducible() -> None:
    assert stale_timestamp(NOW, age=timedelta(seconds=61)) == NOW - timedelta(seconds=61)
    assert out_of_order((1, 2, 3)) == (3, 2, 1)
    assert recovery_order_out_of_order(("market_data", "runner")) == (
        "runner",
        "market_data",
    )
    assert truncate_copy(b"abcdef") == b"abc"
    assert jump_clock(NOW, delta=timedelta(hours=2)) == NOW + timedelta(hours=2)


def test_exclusive_sqlite_lock_blocks_competing_writer_without_mutation(tmp_path) -> None:
    database = tmp_path / "owner.sqlite3"
    primary = sqlite3.connect(database)
    primary.execute("CREATE TABLE owner_state (revision INTEGER NOT NULL)")
    primary.execute("INSERT INTO owner_state VALUES (1)")
    primary.commit()
    primary.close()

    with exclusive_sqlite_lock(database):
        competing = sqlite3.connect(database, timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("UPDATE owner_state SET revision = 2")
        finally:
            competing.close()

    reopened = sqlite3.connect(database)
    try:
        revision = reopened.execute("SELECT revision FROM owner_state").fetchone()
    finally:
        reopened.close()
    assert revision == (1,)


def test_unavailable_transport_is_explicit_and_never_returns_placeholder_state() -> None:
    with pytest.raises(InjectedTransportUnavailable):
        unavailable_transport()


def test_bounded_queue_applies_real_backpressure() -> None:
    work: queue.Queue[int] = bounded_queue(capacity=1)
    work.put_nowait(1)

    with pytest.raises(queue.Full):
        work.put_nowait(2)

    assert work.get_nowait() == 1
