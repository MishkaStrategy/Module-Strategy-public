from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from modular_core import Candle, MarketView, PositionSide, PositionSnapshot

from modular_strategy_position_exit_management import PositionExitManagementModule

START = datetime(2026, 1, 1, tzinfo=UTC)


def _candle(
    index: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        START + timedelta(minutes=5 * index),
        open_,
        high,
        low,
        close,
        1.0,
        "BTCUSDT",
        "5m",
    )


def _position(
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: float = 4.0,
    entry_price: float = 100.0,
    stop_loss: float | None = 95.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        id="property-position",
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_time=START,
        stop_loss=stop_loss,
        take_profit=None,
        tag="property",
        unrealized_pnl=0.0,
        setup_id="property-setup",
    )


def _view(
    index: int,
    *,
    candle: Candle,
    position: PositionSnapshot,
) -> MarketView:
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={},
        index=index,
        cash=10_000.0,
        equity=10_000.0,
        position=position,
        pending_orders=(),
    )


@settings(max_examples=60, deadline=None)
@given(
    entry=st.floats(
        min_value=10,
        max_value=100_000,
        allow_nan=False,
        allow_infinity=False,
    ),
    risk_fraction=st.floats(
        min_value=0.001,
        max_value=0.2,
        allow_nan=False,
        allow_infinity=False,
    ),
    quantity=st.floats(
        min_value=0.001,
        max_value=10_000,
        allow_nan=False,
        allow_infinity=False,
    ),
    exit_fraction=st.floats(
        min_value=0.01,
        max_value=1,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_partial_exit_buy_sell_symmetry_property(
    entry: float,
    risk_fraction: float,
    quantity: float,
    exit_fraction: float,
) -> None:
    risk = entry * risk_fraction
    long_close = entry + risk * 1.5
    short_close = entry - risk * 1.5
    if short_close <= 0:
        return
    long = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": exit_fraction}])
    short = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": exit_fraction}]
    )
    long_candle = _candle(
        0,
        open_=long_close,
        high=long_close + max(risk * 0.1, entry * 1e-8),
        low=min(entry, long_close),
        close=long_close,
    )
    short_candle = _candle(
        0,
        open_=short_close,
        high=max(entry, short_close),
        low=max(short_close - max(risk * 0.1, entry * 1e-8), entry * 1e-12),
        close=short_close,
    )
    long_decision = long.on_bar(
        _view(
            0,
            candle=long_candle,
            position=_position(
                side=PositionSide.LONG,
                quantity=quantity,
                entry_price=entry,
                stop_loss=entry - risk,
            ),
        )
    )
    short_decision = short.on_bar(
        _view(
            0,
            candle=short_candle,
            position=_position(
                side=PositionSide.SHORT,
                quantity=quantity,
                entry_price=entry,
                stop_loss=entry + risk,
            ),
        )
    )
    assert long_decision.closes[0].quantity == pytest.approx(short_decision.closes[0].quantity)


@settings(max_examples=50, deadline=None)
@given(
    closes=st.lists(
        st.floats(
            min_value=96,
            max_value=140,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=2,
        max_size=20,
    )
)
def test_deterministic_replay_and_midstream_restore_property(
    closes: list[float],
) -> None:
    config = {
        "partial_exits": [
            {"trigger_r": 1, "fraction": 0.25},
            {"trigger_r": 2, "fraction": 0.25},
        ],
        "breakeven_trigger_r": 1,
        "trailing_mode": "r_multiple",
        "trailing_activation_r": 2,
        "trailing_distance_r": 1,
    }
    first = PositionExitManagementModule(**config)
    second = PositionExitManagementModule(**config)
    first_outputs = []
    second_outputs = []
    position = _position()
    for index, close in enumerate(closes):
        low = min(95.5, close - 1)
        high = max(100.5, close + 1)
        candle = _candle(index, open_=close, high=high, low=low, close=close)
        view = _view(index, candle=candle, position=position)
        first_outputs.append(first.on_bar(view))
        second_outputs.append(second.on_bar(view))
        if index == len(closes) // 2:
            checkpoint = second.snapshot_state()
            replacement = PositionExitManagementModule(**config)
            replacement.restore_state(checkpoint)
            second = replacement
    assert first_outputs == second_outputs
    assert first.snapshot_state() == second.snapshot_state()


@settings(max_examples=40, deadline=None)
@given(
    prices=st.lists(
        st.floats(
            min_value=101,
            max_value=200,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=2,
        max_size=15,
    )
)
def test_long_trailing_stop_is_monotonic_property(prices: list[float]) -> None:
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.05",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    current_stop = 95.0
    emitted: list[float] = []
    for index, price in enumerate(prices):
        candle = _candle(
            index,
            open_=price,
            high=price + 1,
            low=max(0.000001, price - 1),
            close=price,
        )
        decision = module.on_bar(
            _view(
                index,
                candle=candle,
                position=_position(stop_loss=current_stop),
            )
        )
        if decision.position_updates:
            updated = decision.position_updates[0].stop_loss
            assert updated is not None
            current_stop = updated
            emitted.append(current_stop)
    assert emitted == sorted(emitted)


@pytest.mark.parametrize(
    ("entry", "stop", "close"),
    [
        (1e-6, 5e-7, 1.5e-6),
        (1e12, 9.5e11, 1.05e12),
    ],
)
def test_extremely_small_and_large_financial_values(
    entry: float,
    stop: float,
    close: float,
) -> None:
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    candle = _candle(
        0,
        open_=close,
        high=close * 1.01,
        low=min(stop, close) * 0.99,
        close=close,
    )
    decision = module.on_bar(
        _view(
            0,
            candle=candle,
            position=_position(
                entry_price=entry,
                stop_loss=stop,
            ),
        )
    )
    assert decision.position_updates
    assert decision.position_updates[0].stop_loss == pytest.approx(entry)


def test_hash_randomization_does_not_change_public_config_fingerprint() -> None:
    code = (
        "from modular_strategy_position_exit_management import PositionExitConfig;"
        "print(PositionExitConfig.from_parameters("
        "partial_exits=[{'trigger_r':'1.0','fraction':'0.25'}],"
        "breakeven_trigger_r='1.0').fingerprint)"
    )
    outputs = []
    for seed in ("1", "777"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]
