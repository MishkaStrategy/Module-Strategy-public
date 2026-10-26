from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    RiskConfig,
)

from benchmarks._common import (
    ADVERSE_SLIPPAGE,
    MAKER_FEE,
    MARKET_FEE,
    MAX_NOTIONAL_MULTIPLE,
    RISK_FRACTION,
    Signal,
)
from benchmarks.authoritative_historical_parity import (
    HistoricalParityModule,
    _load_baseline,
    register_signal_plan,
    unregister_signal_plan,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _signal(
    *,
    available_at: datetime,
    signal_key: str,
    module_id: str = "fvg_rt_final_15m",
    module_version: str = "2.0.0",
    setup_id: str = "long",
    side: str = "long",
    priority: int = 1,
    entry: float = 100.0,
    stop: float = 95.0,
    target_r: float = 4.5,
    ttl_bars: int = 36,
    max_hold_bars: int = 288,
    no_progress_bars: int = 48,
    no_progress_min_r: float = 0.15,
) -> Signal:
    return Signal(
        available_at=available_at,
        module_id=module_id,
        module_version=module_version,
        setup_id=setup_id,
        side=side,
        priority=priority,
        entry=entry,
        stop=stop,
        target_r=target_r,
        ttl_bars=ttl_bars,
        max_hold_bars=max_hold_bars,
        no_progress_bars=no_progress_bars,
        no_progress_min_r=no_progress_min_r,
        signal_key=signal_key,
    )


def _bar(
    index: int,
    *,
    open_price: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _config(*, close_at_end: bool = True) -> EngineConfig:
    return EngineConfig(
        close_open_position_at_end=close_at_end,
        fees=FeeConfig(maker_rate=MAKER_FEE, taker_rate=MARKET_FEE),
        execution=ExecutionConfig(
            slippage_bps=ADVERSE_SLIPPAGE * 10_000,
            stop_slippage_bps=ADVERSE_SLIPPAGE * 10_000,
            intrabar_priority="stop",
            limit_fill_policy="at_limit",
            allow_exit_on_entry_bar=False,
            allow_limit_stop_on_entry_bar=True,
            allow_reentry_on_exit_bar=False,
            close_request_timing="current_close",
            target_liquidity="maker",
        ),
        risk=RiskConfig(
            default_risk_fraction=RISK_FRACTION,
            max_risk_fraction=0.05,
            max_notional_multiple=MAX_NOTIONAL_MULTIPLE,
            sizing_model="historical_limit_cost",
        ),
    )


def _run(signals: list[Signal], bars: list[Candle], *, close_at_end: bool = True):
    registered = register_signal_plan(signals)
    try:
        module = HistoricalParityModule(plan_digest=registered.digest)
        result = BacktestEngine(module, _config(close_at_end=close_at_end)).run(bars)
        return result, registered
    finally:
        unregister_signal_plan(registered.digest)


def test_baseline_loader_preserves_17_digit_float_round_trip(tmp_path: Path) -> None:
    expected = float.fromhex("0x1.231434346c360p+0")
    serialized = format(expected, ".17g")
    assert serialized == "1.1370270374283891"
    (tmp_path / "combined-ledger.csv").write_text(
        f"quantity\n{serialized}\n",
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text("{}\n", encoding="utf-8")

    ledger, _ = _load_baseline(tmp_path)
    actual = float(ledger["quantity"].iloc[0])

    assert actual == expected
    assert actual.hex() == "0x1.231434346c360p+0"


def test_bridge_pre_submits_limit_and_preserves_same_fill_bar_stop() -> None:
    signal = _signal(
        available_at=START + timedelta(minutes=5),
        signal_key="a" * 64,
    )
    result, _ = _run(
        [signal],
        [
            _bar(0),
            _bar(1, open_price=100.0, high=101.0, low=94.0, close=96.0),
        ],
    )

    submitted = [event for event in result.journal.orders if event.event == "submitted"]
    assert len(submitted) == 1
    assert submitted[0].timestamp == START
    assert submitted[0].order.tag == signal.signal_key
    assert submitted[0].order.price == signal.entry
    assert len(result.journal.trades) == 1
    trade = result.journal.trades[0]
    assert trade.entry_time == START + timedelta(minutes=5)
    assert trade.exit_time == START + timedelta(minutes=5)
    assert trade.exit_reason == "stop_on_fill_bar"
    assert trade.exit_price == signal.stop * (1 - ADVERSE_SLIPPAGE)


def test_bridge_executes_no_progress_on_current_close() -> None:
    signal = _signal(
        available_at=START + timedelta(minutes=5),
        signal_key="b" * 64,
    )
    bars = [_bar(0)]
    bars.append(_bar(1, open_price=101.0, high=101.0, low=99.0, close=100.0))
    bars.extend(
        _bar(index, open_price=100.0, high=100.1, low=99.9, close=100.0) for index in range(2, 50)
    )

    result, _ = _run([signal], bars)

    assert len(result.journal.trades) == 1
    trade = result.journal.trades[0]
    assert trade.exit_reason == "no_progress"
    assert trade.holding_bars == 48
    assert trade.exit_time == START + timedelta(minutes=5 * 49)
    assert trade.exit_price == 100.0 * (1 - ADVERSE_SLIPPAGE)


def test_bridge_replaces_pending_order_only_for_higher_priority_signal() -> None:
    lower_priority = _signal(
        available_at=START + timedelta(minutes=5),
        signal_key="c" * 64,
        module_id="frozen_4h_short",
        module_version="1.0.0",
        setup_id="short",
        side="short",
        priority=3,
        entry=110.0,
        stop=115.0,
        target_r=6.0,
        ttl_bars=576,
        max_hold_bars=576,
        no_progress_bars=432,
        no_progress_min_r=0.35,
    )
    higher_priority = _signal(
        available_at=START + timedelta(minutes=10),
        signal_key="d" * 64,
        entry=90.0,
        stop=85.0,
    )

    result, _ = _run(
        [lower_priority, higher_priority],
        [
            _bar(0, high=101.0, low=99.0),
            _bar(1, high=105.0, low=95.0),
            _bar(2, high=105.0, low=95.0),
        ],
        close_at_end=False,
    )

    submitted = [event for event in result.journal.orders if event.event == "submitted"]
    assert [event.order.tag for event in submitted] == [
        lower_priority.signal_key,
        higher_priority.signal_key,
    ]
    priority_cancellations = [
        event
        for event in result.journal.orders
        if event.event == "cancelled" and event.reason == "historical_priority_replace"
    ]
    assert len(priority_cancellations) == 1
    assert priority_cancellations[0].order.tag == lower_priority.signal_key
    assert result.journal.trades == []


def test_module_state_round_trips_open_position_tracking() -> None:
    signal = _signal(
        available_at=START + timedelta(minutes=5),
        signal_key="e" * 64,
    )
    registered = register_signal_plan([signal])
    try:
        module = HistoricalParityModule(plan_digest=registered.digest)
        state = {
            "position_signal_key": signal.signal_key,
            "fill_index": 17,
            "mfe_price": 103.25,
        }
        module.restore_state(state)
        restored = HistoricalParityModule(plan_digest=registered.digest)
        restored.restore_state(module.snapshot_state())
        assert restored.snapshot_state() == module.snapshot_state()
    finally:
        unregister_signal_plan(registered.digest)
