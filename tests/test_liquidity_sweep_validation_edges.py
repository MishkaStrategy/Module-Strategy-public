from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

import pytest

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepModule
from strategy_modules.liquidity_sweep.models import (
    BarSnapshot,
    LiquidityLevel,
    LiquiditySide,
    SweepDirection,
    SweepMeasurement,
    public_float,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def valid_bar(**overrides: object) -> BarSnapshot:
    values: dict[str, object] = {
        "timestamp": NOW,
        "close_time": NOW + timedelta(minutes=15),
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10"),
    }
    values.update(overrides)
    return BarSnapshot(**values)  # type: ignore[arg-type]


def valid_level(**overrides: object) -> LiquidityLevel:
    values: dict[str, object] = {
        "level_id": "liq-level-0123456789abcdef01234567",
        "feature_id": "feat-liq-level-0123456789abcdef01234567",
        "side": LiquiditySide.HIGH,
        "price": Decimal("10"),
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "pivot_time": NOW,
        "confirmed_at": NOW + timedelta(minutes=15),
        "confirmed_sequence": 1,
    }
    values.update(overrides)
    return LiquidityLevel(**values)  # type: ignore[arg-type]


def checkpoint_with_level() -> tuple[LiquiditySweepModule, dict[str, object]]:
    module = LiquiditySweepModule(
        pivot_left_bars=1,
        pivot_right_bars=1,
        min_wick_to_body_ratio=0,
        min_wick_fraction=0,
    )
    bars = [
        Candle(NOW, 9, 10, 8, 9, symbol="BTCUSDT", timeframe="15m"),
        Candle(
            NOW + timedelta(minutes=15),
            10,
            12,
            9,
            11,
            symbol="BTCUSDT",
            timeframe="15m",
        ),
        Candle(
            NOW + timedelta(minutes=30),
            10.5,
            11,
            9.5,
            10,
            symbol="BTCUSDT",
            timeframe="15m",
        ),
    ]
    history: list[Candle] = []
    for item in bars:
        history.append(item)
        module.on_bar(
            MarketView(
                base_bars=tuple(history),
                timeframe_bars={},
                index=len(history) - 1,
                cash=10_000,
                equity=10_000,
                position=None,
                pending_orders=(),
            )
        )
    return module, deepcopy(dict(module.snapshot_state()))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("timestamp", "2026-01-01", "datetime"),
        ("close_time", "2026-01-01", "datetime"),
        ("timestamp", datetime(2026, 1, 1), "timezone-aware"),
        ("close_time", datetime(2026, 1, 1), "timezone-aware"),
        ("symbol", " BTCUSDT", "trimmed"),
        ("symbol", "", "trimmed"),
        ("timeframe", 15, "trimmed"),
        ("timeframe", " 15m", "trimmed"),
        ("close_time", NOW - timedelta(minutes=1), "before"),
        ("open", Decimal("0"), "positive"),
        ("high", Decimal("9"), "high"),
        ("low", Decimal("11"), "low"),
    ],
)
def test_bar_snapshot_rejects_invalid_boundary(
    field: str,
    value: object,
    error: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        valid_bar(**{field: value})


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("level_id", 1, "trimmed"),
        ("feature_id", " ", "trimmed"),
        ("symbol", " BTCUSDT", "trimmed"),
        ("timeframe", " 15m", "trimmed"),
        ("price", Decimal("0"), "positive"),
        ("pivot_time", "2026", "datetime"),
        ("confirmed_at", "2026", "datetime"),
        ("pivot_time", datetime(2026, 1, 1), "timezone-aware"),
        ("confirmed_at", datetime(2026, 1, 1), "timezone-aware"),
        ("confirmed_at", NOW - timedelta(minutes=1), "before"),
        ("confirmed_sequence", 0, "positive"),
        ("feature_id", "feat-wrong", "deterministic"),
    ],
)
def test_liquidity_level_rejects_invalid_boundary(
    field: str,
    value: object,
    error: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        valid_level(**{field: value})


def test_public_float_rejects_nan_and_saturates_both_signs() -> None:
    with pytest.raises(ValueError, match="NaN"):
        public_float(Decimal("NaN"))
    assert public_float(Decimal("1e10000")) == float.fromhex("0x1.fffffffffffffp+1023")
    assert public_float(Decimal("-1e10000")) == -float.fromhex("0x1.fffffffffffffp+1023")
    assert math.isfinite(public_float(Decimal("Infinity")))


def test_sweep_measurement_rejects_negative_wick_ratio() -> None:
    with pytest.raises(ValueError, match="wick_to_body"):
        SweepMeasurement(
            SweepDirection.BEARISH,
            Decimal("1"),
            Decimal("-1"),
            Decimal("0.5"),
            Decimal("10"),
        )


def test_state_rejects_sequence_bool() -> None:
    module, state = checkpoint_with_level()
    state["sequence"] = True
    with pytest.raises(ValueError, match="sequence"):
        module.restore_state(state)


def test_state_rejects_more_bars_than_sequence() -> None:
    module, state = checkpoint_with_level()
    state["sequence"] = 1
    state["stats"]["processed_bars"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="more buffered bars"):
        module.restore_state(state)


def test_state_rejects_future_level_sequence() -> None:
    module, state = checkpoint_with_level()
    state["levels"][0]["confirmed_sequence"] = 99  # type: ignore[index]
    with pytest.raises(ValueError, match="cannot exceed"):
        module.restore_state(state)


def test_state_rejects_duplicate_level_id() -> None:
    module, state = checkpoint_with_level()
    duplicate = deepcopy(state["levels"][0])  # type: ignore[index]
    state["levels"].append(duplicate)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="duplicate level_id"):
        module.restore_state(state)


def test_state_rejects_closed_lifecycle_above_created() -> None:
    module, state = checkpoint_with_level()
    state["stats"]["levels_swept"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="lifecycle"):
        module.restore_state(state)


@pytest.mark.parametrize("bad", ["bars", b"bars", 42, None])
def test_state_rejects_non_sequence_bar_container(bad: object) -> None:
    module, state = checkpoint_with_level()
    state["bars"] = bad
    with pytest.raises(ValueError, match="must be a sequence"):
        module.restore_state(state)


@pytest.mark.parametrize("bad", [None, 1, "", "not-a-date", "2026-01-01T00:00:00"])
def test_state_rejects_invalid_datetime_payload(bad: object) -> None:
    module, state = checkpoint_with_level()
    state["last_bar"]["timestamp"] = bad  # type: ignore[index]
    with pytest.raises(ValueError, match="datetime|timezone-aware"):
        module.restore_state(state)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", object()])
def test_state_rejects_invalid_decimal_payload(bad: object) -> None:
    module, state = checkpoint_with_level()
    state["last_bar"]["open"] = bad  # type: ignore[index]
    with pytest.raises(ValueError, match="decimal"):
        module.restore_state(state)


def test_state_rejects_non_string_and_unknown_side() -> None:
    module, state = checkpoint_with_level()
    state["levels"][0]["side"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="side must be a string"):
        module.restore_state(state)
    _, state = checkpoint_with_level()
    state["levels"][0]["side"] = "middle"  # type: ignore[index]
    with pytest.raises(ValueError, match="invalid level side"):
        module.restore_state(state)


def test_module_snapshot_rejects_boolean_ohlc_with_stable_error() -> None:
    class UnsafeCandle:
        timestamp = NOW
        close_time = NOW + timedelta(minutes=15)
        symbol = "BTCUSDT"
        timeframe = "15m"
        open = True
        high = 2
        low = 1
        close = 1

    module = LiquiditySweepModule()
    with pytest.raises(ValueError, match="booleans"):
        module._snapshot(UnsafeCandle())  # type: ignore[arg-type]


def test_module_utc_guard_rejects_wrong_type() -> None:
    from strategy_modules.liquidity_sweep.hardened import _utc

    with pytest.raises(TypeError, match="datetime"):
        _utc("2026", "test")  # type: ignore[arg-type]


def test_base_module_accepts_core_opaque_timeframe() -> None:
    item = Candle(
        NOW,
        10,
        11,
        9,
        10,
        symbol="BTCUSDT",
        timeframe="vendor",
    )
    decision = LiquiditySweepModule().on_bar(
        MarketView(
            base_bars=(item,),
            timeframe_bars={},
            index=0,
            cash=10_000,
            equity=10_000,
            position=None,
            pending_orders=(),
        )
    )
    assert decision.diagnostics["processed_sequence"] == 1


def test_numeric_timeframe_requires_exact_close_duration() -> None:
    with pytest.raises(ValueError, match="timeframe duration"):
        valid_bar(close_time=NOW + timedelta(minutes=14))


def test_zero_numeric_timeframe_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        valid_bar(timeframe="0m", close_time=NOW)


def test_opaque_timeframe_requires_equal_close_time() -> None:
    with pytest.raises(ValueError, match="opaque timeframe"):
        valid_bar(timeframe="vendor")


def test_corrupted_checkpoint_close_time_is_rejected_atomically() -> None:
    source, state = checkpoint_with_level()
    latest = state["bars"][-1]  # type: ignore[index]
    latest_timestamp = datetime.fromisoformat(latest["timestamp"])
    wrong_close = (latest_timestamp + timedelta(minutes=16)).isoformat()
    latest["close_time"] = wrong_close
    state["last_bar"]["close_time"] = wrong_close  # type: ignore[index]

    target = LiquiditySweepModule(
        pivot_left_bars=1,
        pivot_right_bars=1,
        min_wick_to_body_ratio=0,
        min_wick_fraction=0,
    )
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="timeframe duration"):
        target.restore_state(state)
    assert target.snapshot_state() == before
    assert source.snapshot_state() != before


def test_explicit_module_rejects_other_timeframe_checkpoint_atomically() -> None:
    source = LiquiditySweepModule(
        timeframe="15m",
        pivot_left_bars=1,
        pivot_right_bars=1,
    )
    item = Candle(
        NOW,
        10,
        11,
        9,
        10,
        symbol="BTCUSDT",
        timeframe="15m",
    )
    source.on_bar(
        MarketView(
            base_bars=(item,),
            timeframe_bars={"15m": (item,)},
            index=0,
            cash=10_000,
            equity=10_000,
            position=None,
            pending_orders=(),
        )
    )
    state = deepcopy(dict(source.snapshot_state()))
    for bar_state in state["bars"]:  # type: ignore[union-attr]
        timestamp = datetime.fromisoformat(bar_state["timestamp"])
        bar_state["timeframe"] = "5m"
        bar_state["close_time"] = (timestamp + timedelta(minutes=5)).isoformat()
    last_bar = state["last_bar"]
    timestamp = datetime.fromisoformat(last_bar["timestamp"])  # type: ignore[index]
    last_bar["timeframe"] = "5m"  # type: ignore[index]
    last_bar["close_time"] = (timestamp + timedelta(minutes=5)).isoformat()  # type: ignore[index]

    target = LiquiditySweepModule(
        timeframe="15m",
        pivot_left_bars=1,
        pivot_right_bars=1,
    )
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="configured timeframe"):
        target.restore_state(state)
    assert target.snapshot_state() == before


def test_model_scalar_types_are_strict() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        valid_bar(open=10.0)
    with pytest.raises(ValueError, match="positive integer"):
        valid_level(confirmed_sequence=True)
    with pytest.raises(TypeError, match="LiquiditySide"):
        valid_level(side="high")
    with pytest.raises(TypeError, match="Decimal"):
        public_float(1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="SweepDirection"):
        SweepMeasurement(  # type: ignore[arg-type]
            "bearish",
            Decimal("1"),
            Decimal("1"),
            Decimal("0.5"),
            Decimal("10"),
        )
