from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from modular_core import (
    AtomicJsonStateRepository,
    Candle,
    ClosePositionRequest,
    EngineConfig,
    ExecutionConfig,
    Journal,
    OrderRequest,
    OrderType,
    Side,
)
from modular_core.broker_state import (
    BrokerStateSnapshot,
    capture_broker_state,
    restore_broker_state,
    restore_broker_state_from_repository,
    save_broker_state,
)
from modular_core.execution import BrokerSimulator
from modular_core.serialization import to_jsonable

_AT = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)


def _bar(index: int, *, close: float) -> Candle:
    return Candle(
        timestamp=_AT + timedelta(minutes=5 * index),
        open=100.0,
        high=104.0,
        low=99.0,
        close=close,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _broker(config: EngineConfig, *, run_id: str) -> BrokerSimulator:
    return BrokerSimulator(config, Journal(run_id=run_id))


def _current_close_config() -> EngineConfig:
    return EngineConfig(
        execution=ExecutionConfig(
            slippage_bps=100.0,
            close_request_timing="current_close",
        )
    )


def _broker_after_processed_entry(
    config: EngineConfig,
    *,
    run_id: str,
) -> tuple[BrokerSimulator, Candle]:
    broker = _broker(config, run_id=run_id)
    assert (
        broker.submit(
            OrderRequest(
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                price=100.0,
            ),
            source_module="canonical",
            created_at=_AT,
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )
    current = _bar(1, close=102.0)
    broker.process_bar(current, 1)
    assert broker.portfolio.position is not None
    return broker, current


def _mutable_state(snapshot: BrokerStateSnapshot) -> dict[str, Any]:
    value = to_jsonable(snapshot.to_state())
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _assert_pristine(broker: BrokerSimulator) -> None:
    assert broker.portfolio.cash == broker.config.initial_cash
    assert broker.portfolio.position is None
    assert broker.orders == []
    assert broker.pending_orders == []
    assert broker.pending_close is None
    assert broker._last_processed_bar is None
    assert broker._last_processed_index is None


def test_current_close_save_reopen_matches_uninterrupted_execution(tmp_path: Path) -> None:
    config = _current_close_config()
    source, current = _broker_after_processed_entry(config, run_id="current-close-source")

    repository = AtomicJsonStateRepository(tmp_path / "broker-state")
    saved = save_broker_state(repository, "paper-broker", source)
    source_fill_count = len(source.journal.fills)
    source_trade_count = len(source.journal.trades)

    request = ClosePositionRequest(reason="no_progress")
    assert source.schedule_close(
        request,
        timestamp=current.timestamp,
        current_index=1,
        source_module="canonical",
    )
    assert source.portfolio.position is None
    assert source.pending_close is None
    uninterrupted_fill = source.journal.fills[source_fill_count]
    uninterrupted_trade = source.journal.trades[source_trade_count]

    reopened_repository = AtomicJsonStateRepository(tmp_path / "broker-state")
    restored = _broker(config, run_id="current-close-restored")
    loaded = restore_broker_state_from_repository(
        reopened_repository,
        "paper-broker",
        restored,
    )
    assert loaded.to_state() == saved.to_state()
    assert restored.journal.fills == []
    assert restored.journal.trades == []

    assert restored.schedule_close(
        request,
        timestamp=current.timestamp,
        current_index=1,
        source_module="canonical",
    )
    assert restored.portfolio.position is None
    assert restored.pending_close is None

    restored_fill = restored.journal.fills[0]
    restored_trade = restored.journal.trades[0]
    assert restored_fill == uninterrupted_fill
    assert restored_trade == uninterrupted_trade
    assert restored_fill.price == pytest.approx(current.close * 0.99)
    assert restored_fill.quantity == 1.0
    assert restored_fill.reason == "no_progress"
    assert restored_fill.timestamp == current.timestamp
    assert restored_fill.liquidity == "taker"
    assert restored_trade.exit_price == pytest.approx(current.close * 0.99)
    assert restored_trade.quantity == 1.0
    assert restored_trade.exit_reason == "no_progress"
    assert restored_trade.exit_time == current.timestamp
    assert capture_broker_state(restored).to_state() == capture_broker_state(source).to_state()


def test_legacy_v1_snapshot_without_processed_bar_does_not_invent_current_close_eligibility() -> (
    None
):
    config = _current_close_config()
    source, current = _broker_after_processed_entry(config, run_id="legacy-current-close-source")
    state = _mutable_state(capture_broker_state(source))
    state.pop("last_processed_bar")

    legacy = BrokerStateSnapshot.from_state(state)
    restored = _broker(config, run_id="legacy-current-close-restored")
    restore_broker_state(restored, legacy)

    assert restored.portfolio.position is not None
    assert restored._last_processed_bar is None
    assert restored._last_processed_index is None
    with pytest.raises(RuntimeError, match="current-close request does not match"):
        restored.schedule_close(
            ClosePositionRequest(reason="no_progress"),
            timestamp=current.timestamp,
            current_index=1,
            source_module="canonical",
        )


def test_restore_failure_with_processed_bar_state_keeps_target_pristine() -> None:
    source_config = _current_close_config()
    source, _ = _broker_after_processed_entry(source_config, run_id="restore-failure-source")
    snapshot = capture_broker_state(source)
    target_config = EngineConfig(
        execution=ExecutionConfig(
            slippage_bps=50.0,
            close_request_timing="current_close",
        )
    )
    target = _broker(target_config, run_id="restore-failure-target")

    with pytest.raises(ValueError, match="config identity mismatch"):
        restore_broker_state(target, snapshot)

    _assert_pristine(target)


def test_processed_bar_state_rejects_partial_and_extra_fields_before_restore_mutation() -> None:
    config = _current_close_config()
    source, _ = _broker_after_processed_entry(config, run_id="processed-bar-shape-source")
    snapshot = capture_broker_state(source)

    for field in ("timestamp", "index", "close"):
        state = _mutable_state(snapshot)
        persisted = cast(dict[str, Any], state["last_processed_bar"])
        persisted.pop(field)
        target = _broker(config, run_id=f"processed-bar-missing-{field}")
        with pytest.raises(ValueError, match="last processed bar fields mismatch"):
            BrokerStateSnapshot.from_state(state)
        _assert_pristine(target)

    state = _mutable_state(snapshot)
    persisted = cast(dict[str, Any], state["last_processed_bar"])
    persisted["open"] = 100.0
    target = _broker(config, run_id="processed-bar-extra-field")
    with pytest.raises(ValueError, match="last processed bar fields mismatch"):
        BrokerStateSnapshot.from_state(state)
    _assert_pristine(target)


def test_processed_bar_state_rejects_invalid_index_timestamp_and_close_before_restore_mutation() -> (
    None
):
    config = _current_close_config()
    source, _ = _broker_after_processed_entry(config, run_id="processed-bar-value-source")
    snapshot = capture_broker_state(source)
    cases: tuple[tuple[str, object, type[Exception], str], ...] = (
        ("index", -1, ValueError, "index cannot be negative"),
        ("index", 1.5, TypeError, "index must be int"),
        ("index", True, TypeError, "index must be int"),
        ("timestamp", 123, TypeError, "ISO datetime string"),
        ("timestamp", "not-a-datetime", ValueError, "must be an ISO datetime"),
        ("close", "102.0", TypeError, "close must be a number"),
        ("close", float("nan"), ValueError, "finite"),
        ("close", float("inf"), ValueError, "finite"),
        ("close", 0.0, ValueError, "positive"),
    )

    for field, value, error_type, match in cases:
        state = _mutable_state(snapshot)
        persisted = cast(dict[str, Any], state["last_processed_bar"])
        persisted[field] = value
        target = _broker(config, run_id=f"processed-bar-invalid-{field}")
        with pytest.raises(error_type, match=match):
            BrokerStateSnapshot.from_state(state)
        _assert_pristine(target)


def test_broker_state_still_rejects_unknown_top_level_field() -> None:
    config = _current_close_config()
    source, _ = _broker_after_processed_entry(config, run_id="processed-bar-unknown-source")
    state = _mutable_state(capture_broker_state(source))
    state["caller_execution_price"] = 1.0

    with pytest.raises(ValueError, match="broker state fields mismatch"):
        BrokerStateSnapshot.from_state(state)


def test_capture_rejects_inconsistent_processed_bar_runtime_tuple() -> None:
    config = _current_close_config()
    source, _ = _broker_after_processed_entry(config, run_id="processed-bar-inconsistent-source")
    source._last_processed_index = None

    with pytest.raises(RuntimeError, match="last processed bar state is inconsistent"):
        capture_broker_state(source)
