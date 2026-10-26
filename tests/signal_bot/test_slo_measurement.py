from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from modular_core import Side
from signal_bot.domain import EntryType, SignalStatus, TradeSignal
from signal_bot.measurement import (
    LatencySummary,
    canonical_signal_payload,
    snapshot_sqlite_state,
)
from signal_bot.state import SQLiteStateStore, StateCapacityError

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def make_signal(index: int = 0, *, metadata: dict[str, object] | None = None) -> TradeSignal:
    timestamp = NOW + timedelta(seconds=index)
    return TradeSignal(
        signal_id=f"sig_{index:032x}",
        strategy_id="wave2-slo",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        market="binance_usdt_m_perpetual",
        side=Side.BUY,
        signal_timestamp=timestamp,
        signal_timeframe="5m",
        entry_type=EntryType.LIMIT,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("99.0"),
        take_profit_levels=(Decimal("102.0"),),
        invalidation_price=None,
        risk_reward=(Decimal("2.0"),),
        reason_codes=("wave2_slo",),
        human_readable_reason="deterministic measurement signal",
        expires_at=timestamp + timedelta(hours=1),
        setup_id=f"setup-{index}",
        metadata=metadata or {"index": index},
        chart_enabled=False,
        source="core",
        contract_version="1.0",
    )


def mark_sent(store: SQLiteStateStore, signal_id: str) -> None:
    store.transition(signal_id, SignalStatus.VALIDATED)
    store.transition(signal_id, SignalStatus.RENDERING)
    store.transition(signal_id, SignalStatus.READY)
    store.transition(signal_id, SignalStatus.SENDING)
    store.transition(signal_id, SignalStatus.SENT, message_id="message-1")


def test_latency_summary_uses_deterministic_nearest_rank() -> None:
    summary = LatencySummary.from_samples((10, 20, 30, 40, 50, 60, 70, 80, 90, 100))
    assert summary.to_mapping() == {
        "samples": 10,
        "minimum_ns": 10,
        "p50_ns": 50,
        "p95_ns": 100,
        "p99_ns": 100,
        "maximum_ns": 100,
    }
    with pytest.raises(ValueError, match="at least one"):
        LatencySummary.from_samples(())


def test_canonical_payload_is_deterministic_and_strict() -> None:
    first = make_signal(metadata={"z": 1, "a": ["stable", 2]})
    second = make_signal(metadata={"a": ["stable", 2], "z": 1})
    assert canonical_signal_payload(first) == canonical_signal_payload(second)
    assert canonical_signal_payload(first).startswith(b'{"chart_enabled":false')

    unsupported = replace(first, metadata={"opaque": object()})
    with pytest.raises(TypeError):
        canonical_signal_payload(unsupported)


def test_snapshot_is_payload_free_and_reports_delivery_state(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    secret = "private-token-must-not-leak"
    store = SQLiteStateStore(database)
    sent = make_signal(1, metadata={"secret": secret})
    pending = make_signal(2, metadata={"secret": secret})
    assert store.register(sent)
    assert store.register(pending)
    mark_sent(store, sent.signal_id)
    store.transition(pending.signal_id, SignalStatus.VALIDATED)
    store.transition(
        pending.signal_id,
        SignalStatus.RENDERING,
    )
    store.transition(
        pending.signal_id,
        SignalStatus.SEND_FAILED,
        error="TimeoutError",
        retry_at=datetime.now(UTC) + timedelta(seconds=30),
    )

    snapshot = snapshot_sqlite_state(
        database,
        captured_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    rendered = json.dumps(snapshot.to_mapping(), sort_keys=True)
    assert snapshot.total_signals == 2
    assert snapshot.pending_signals == 1
    assert snapshot.terminal_signals == 1
    assert snapshot.retry_scheduled == 1
    assert snapshot.status_counts == {"send_failed": 1, "sent": 1}
    assert snapshot.persisted_bytes > 0
    assert secret not in rendered
    store.close()


def test_persistent_outbox_fetch_is_bounded_but_state_growth_is_visible(tmp_path: Path) -> None:
    database = tmp_path / "bounded.sqlite3"
    store = SQLiteStateStore(database)
    for index in range(105):
        signal = make_signal(index)
        assert store.register(signal)
        store.transition(signal.signal_id, SignalStatus.VALIDATED)

    pending = store.pending(now=datetime.now(UTC) + timedelta(days=1))
    snapshot = snapshot_sqlite_state(database)
    assert len(pending) == 100
    assert snapshot.pending_signals == 105
    assert snapshot.total_signals == 105
    store.close()


def test_pending_capacity_fails_closed_and_terminal_state_releases_slot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pending-capacity.sqlite3"
    store = SQLiteStateStore(database, max_total_signals=3, max_pending_signals=2)
    first = make_signal(1)
    second = make_signal(2)
    third = make_signal(3)
    assert store.register(first)
    assert store.register(second)

    with pytest.raises(StateCapacityError) as captured:
        store.register(third)
    assert captured.value.resource == "pending_signals"
    assert captured.value.current == 2
    assert captured.value.limit == 2
    assert store.status(third.signal_id) is None

    store.transition(first.signal_id, SignalStatus.CANCELLED)
    assert store.register(third)
    snapshot = snapshot_sqlite_state(database)
    assert snapshot.total_signals == 3
    assert snapshot.pending_signals == 2
    assert snapshot.terminal_signals == 1
    store.close()


def test_total_capacity_preserves_duplicate_suppression_and_rejects_new_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "total-capacity.sqlite3"
    store = SQLiteStateStore(database, max_total_signals=2, max_pending_signals=2)
    first = make_signal(1)
    second = make_signal(2)
    third = make_signal(3)
    assert store.register(first)
    mark_sent(store, first.signal_id)
    assert store.register(second)
    mark_sent(store, second.signal_id)

    assert store.register(first) is False
    with pytest.raises(StateCapacityError) as captured:
        store.register(third)
    assert captured.value.resource == "total_signals"
    assert captured.value.current == 2
    assert captured.value.limit == 2
    assert store.status(third.signal_id) is None

    snapshot = snapshot_sqlite_state(database)
    assert snapshot.total_signals == 2
    assert snapshot.pending_signals == 0
    assert snapshot.terminal_signals == 2
    store.close()


def test_capacity_counters_are_reconciled_and_preserved_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "capacity-restart.sqlite3"
    sent = make_signal(1)
    pending = make_signal(2)
    new = make_signal(3)
    store = SQLiteStateStore(database, max_total_signals=3, max_pending_signals=2)
    assert store.register(sent)
    mark_sent(store, sent.signal_id)
    assert store.register(pending)
    store.close()

    reopened = SQLiteStateStore(database, max_total_signals=3, max_pending_signals=2)
    assert reopened.register(sent) is False
    assert reopened.register(new)
    with pytest.raises(StateCapacityError, match="total_signals capacity exhausted"):
        reopened.register(make_signal(4))
    snapshot = snapshot_sqlite_state(database)
    assert snapshot.total_signals == 3
    assert snapshot.pending_signals == 2
    reopened.close()


def test_capacity_configuration_is_strict() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SQLiteStateStore(":memory:", max_total_signals=0)
    with pytest.raises(ValueError, match="must not exceed"):
        SQLiteStateStore(":memory:", max_total_signals=1, max_pending_signals=2)


def test_acknowledged_state_survives_restart_and_is_not_replayed(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    signal = make_signal(7)
    store = SQLiteStateStore(database)
    assert store.register(signal)
    mark_sent(store, signal.signal_id)
    store.close()

    reopened = SQLiteStateStore(database)
    assert reopened.register(signal) is False
    assert reopened.status(signal.signal_id) is SignalStatus.SENT
    assert signal.signal_id not in {
        item.signal_id for item in reopened.pending(now=datetime.now(UTC) + timedelta(days=1))
    }
    reopened.close()


def test_corrupt_persisted_payload_is_cancelled_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "corrupt.sqlite3"
    signal = make_signal(9)
    store = SQLiteStateStore(database)
    assert store.register(signal)
    store.transition(signal.signal_id, SignalStatus.VALIDATED)

    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(
                "UPDATE signals SET payload=? WHERE signal_id=?",
                ("{not-json", signal.signal_id),
            )
    finally:
        connection.close()

    assert store.pending(now=datetime.now(UTC) + timedelta(days=1)) == ()
    assert store.status(signal.signal_id) is SignalStatus.CANCELLED
    snapshot = snapshot_sqlite_state(database)
    assert snapshot.pending_signals == 0
    assert snapshot.terminal_signals == 1
    store.close()


def test_cursor_updates_are_monotonic_under_out_of_order_input(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "cursor.sqlite3")
    newer = NOW + timedelta(minutes=10)
    older = NOW
    store.set_last_processed("BTCUSDT", "5m", newer)
    store.set_last_processed("BTCUSDT", "5m", older)
    assert store.last_processed("BTCUSDT", "5m") == newer
    store.close()
