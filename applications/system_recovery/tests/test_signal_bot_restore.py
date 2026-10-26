from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from modular_core import Side
from signal_bot.domain import EntryType, SignalStatus, TradeSignal
from signal_bot.operator_contract import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    TestNotificationRequest,
    TrustedSignalBotOperator,
)
from signal_bot.operator_control import SignalBotOperatorControl
from signal_bot.operator_ledger import SignalBotControlLedger
from signal_bot.state import SQLiteStateStore

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "8" * 40
_RELEASE_ID = "signal-bot-owner-restore"
_AT = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)


class _CountingSender:
    def __init__(self) -> None:
        self.calls = 0

    def send_test_notification(self) -> None:
        self.calls += 1


def _signal(sequence: int) -> TradeSignal:
    timestamp = _AT + timedelta(minutes=sequence)
    setup_id = f"recovery-setup-{sequence}"
    signal_id = TradeSignal.stable_id(
        strategy_id="recovery-strategy",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        side=Side.BUY,
        timeframe="5m",
        timestamp=timestamp,
        entry=Decimal("100.0"),
        setup_id=setup_id,
    )
    return TradeSignal(
        signal_id=signal_id,
        strategy_id="recovery-strategy",
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
        reason_codes=("RECOVERY_PROOF",),
        human_readable_reason="clean-host recovery proof",
        expires_at=_AT + timedelta(days=1),
        setup_id=setup_id,
        metadata={"sequence": sequence},
    )


def _advance(
    store: SQLiteStateStore,
    signal: TradeSignal,
    statuses: tuple[SignalStatus, ...],
) -> None:
    assert store.register(signal) is True
    for status in statuses:
        store.transition(signal.signal_id, status)


def _snapshot_and_restore(*, source_db: Path, backup_root: Path, clean_root: Path) -> Path:
    entry = snapshot_entry(
        source_root=source_db.parent,
        backup_root=backup_root,
        spec=SnapshotSpec(
            logical_id="signal-bot.owner.sqlite3",
            owner="signal-bot",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source=source_db.name,
            restore_target="signal-bot.sqlite3",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(entry,),
    )
    return restore_snapshot_entry(
        backup_root=backup_root,
        restore_root=clean_root,
        manifest=manifest,
        logical_id=entry.logical_id,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )


def _counters(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as connection:
        rows = dict(connection.execute("SELECT name,value FROM state_counters").fetchall())
    return int(rows["total_signals"]), int(rows["pending_signals"])


def test_signal_bot_clean_host_restore_preserves_delivery_and_operator_fences(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "signal-bot.sqlite3"

    sent = _signal(1)
    pending = _signal(2)
    state = SQLiteStateStore(source_db, max_total_signals=4, max_pending_signals=4)
    _advance(
        state,
        sent,
        (
            SignalStatus.VALIDATED,
            SignalStatus.RENDERING,
            SignalStatus.READY,
            SignalStatus.SENDING,
            SignalStatus.SENT,
        ),
    )
    _advance(
        state,
        pending,
        (
            SignalStatus.VALIDATED,
            SignalStatus.RENDERING,
            SignalStatus.READY,
            SignalStatus.SENDING,
            SignalStatus.SEND_FAILED,
        ),
    )
    state.set_last_processed("BTCUSDT", "5m", _AT)
    state.set_notifications("recovery-chat", False)
    state.set_telegram_update_offset(100)

    successful_request = TestNotificationRequest(
        request_id="recovery-success-001",
        operator=TrustedSignalBotOperator("recovery-operator"),
    )
    uncertain_request = TestNotificationRequest(
        request_id="recovery-uncertain-001",
        operator=TrustedSignalBotOperator("recovery-operator"),
    )
    ledger = SignalBotControlLedger(source_db)
    assert ledger.claim(successful_request, _AT) is None
    ledger.complete_success(successful_request, _AT + timedelta(seconds=1))
    assert ledger.claim(uncertain_request, _AT + timedelta(seconds=2)) is None
    ledger.close()
    state.close()
    assert _counters(source_db) == (2, 1)

    restored_db = _snapshot_and_restore(
        source_db=source_db,
        backup_root=backup_root,
        clean_root=clean_root,
    )

    restored = SQLiteStateStore(restored_db, max_total_signals=4, max_pending_signals=4)
    assert restored.get_signal(sent.signal_id) == sent
    assert restored.status(sent.signal_id) is SignalStatus.SENT
    assert restored.get_signal(pending.signal_id) == pending
    assert restored.status(pending.signal_id) is SignalStatus.SEND_FAILED
    assert restored.register(sent) is False
    assert _counters(restored_db) == (2, 1)

    assert restored.last_processed("BTCUSDT", "5m") == _AT
    restored.set_last_processed("BTCUSDT", "5m", _AT - timedelta(minutes=5))
    assert restored.last_processed("BTCUSDT", "5m") == _AT
    assert restored.notifications_enabled("recovery-chat") is False
    assert restored.telegram_update_offset() == 100
    restored.set_telegram_update_offset(99)
    assert restored.telegram_update_offset() == 100

    restored.transition(pending.signal_id, SignalStatus.SENDING)
    restored.transition(pending.signal_id, SignalStatus.SENT, message_id="recovered-message")
    continuation = _signal(3)
    assert restored.register(continuation) is True
    assert _counters(restored_db) == (3, 1)

    restored_ledger = SignalBotControlLedger(restored_db)
    sender = _CountingSender()
    control = SignalBotOperatorControl(
        ledger=restored_ledger,
        health=lambda: {"status": "ok", "cycles": 1, "signals_sent": 2},
        notifications_enabled=lambda: restored.notifications_enabled("recovery-chat"),
        sender=sender,
        configured=True,
        clock=lambda: _AT + timedelta(minutes=10),
    )
    replay = control.test_notification(successful_request)
    assert replay.replayed is True
    assert sender.calls == 0

    conflict = TestNotificationRequest(
        request_id=successful_request.request_id,
        operator=TrustedSignalBotOperator("other-recovery-operator"),
    )
    with pytest.raises(SignalBotControlError) as conflict_error:
        control.test_notification(conflict)
    assert conflict_error.value.code is SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT
    assert sender.calls == 0

    with pytest.raises(SignalBotControlError) as recovery_error:
        control.test_notification(uncertain_request)
    assert recovery_error.value.code is SignalBotControlErrorCode.RECOVERY_REQUIRED
    assert sender.calls == 0

    results = tuple(entry.result for entry in restored_ledger.audit_entries(limit=20))
    assert "REPLAYED" in results
    assert SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT.value in results
    assert SignalBotControlErrorCode.RECOVERY_REQUIRED.value in results
    restored_ledger.close()
    restored.close()


def test_signal_bot_corrupt_snapshot_fails_closed_before_publication(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    source_db = source_root / "signal-bot.sqlite3"

    state = SQLiteStateStore(source_db)
    assert state.register(_signal(1)) is True
    state.close()
    entry = snapshot_entry(
        source_root=source_root,
        backup_root=backup_root,
        spec=SnapshotSpec(
            logical_id="signal-bot.owner.sqlite3",
            owner="signal-bot",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source=source_db.name,
            restore_target="signal-bot.sqlite3",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(entry,),
    )
    (backup_root / "payload" / entry.logical_id).write_bytes(b"corrupt")

    with pytest.raises(RecoveryManifestError, match="snapshot"):
        restore_snapshot_entry(
            backup_root=backup_root,
            restore_root=clean_root,
            manifest=manifest,
            logical_id=entry.logical_id,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )
    assert not (clean_root / "signal-bot.sqlite3").exists()
