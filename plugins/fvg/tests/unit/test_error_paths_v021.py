from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from modular_core import Candle

from conftest import bullish_triplet, view_for
from modular_strategy_fvg import FvgConfig, FvgDataConflictError, FvgModule, FvgStateError
from modular_strategy_fvg import models_base as models_base_module
from modular_strategy_fvg.detector import candle_fingerprint


def test_config_rejects_object_without_decimal_representation() -> None:
    class BadDecimal:
        def __str__(self) -> str:
            raise ValueError("bad")

    with pytest.raises(TypeError, match="canonical decimal-compatible"):
        FvgConfig.from_parameters(min_gap_absolute=BadDecimal())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("open", 111.0, "open is outside"), ("volume", -1.0, "volume must be non-negative")],
)
def test_full_candle_validation_rejects_invalid_optional_fields(
    field: str,
    value: float,
    message: str,
) -> None:
    _, _, candle = bullish_triplet()
    data = {
        "timestamp": candle.timestamp,
        "close_time": candle.close_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
    }
    data[field] = value
    fake = type("FullCandle", (), data)()
    with pytest.raises(ValueError, match=message):
        candle_fingerprint(fake)


def test_finite_float_wraps_conversion_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_: object) -> float:
        raise OverflowError("forced")

    monkeypatch.setattr(models_base_module, "float", fail, raising=False)
    with pytest.raises(ValueError, match="finite float"):
        models_base_module._finite_float(Decimal("1"), "value")  # noqa: SLF001


def test_restored_duplicate_detects_changed_latest_fingerprint() -> None:
    bars = list(bullish_triplet())
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bars))
    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(source.snapshot_state())

    c = bars[-1]
    bars[-1] = Candle(
        c.timestamp,
        c.open,
        c.high,
        c.low,
        c.close,
        c.volume + 1,
        c.symbol,
        c.timeframe,
    )
    with pytest.raises(FvgDataConflictError, match="conflicting duplicate"):
        restored.on_bar(view_for(bars))


def test_exact_inner_duplicate_is_idempotent_in_initial_batch() -> None:
    bars = list(bullish_triplet())
    bars.append(bars[-1])
    decision = FvgModule(timeframes=("15m",)).on_bar(view_for(bars))
    assert decision.diagnostics["duplicates"] == 1
    assert len([feature for feature in decision.features if feature.key == "fvg.active_zone"]) == 1


def test_history_validator_rejects_missing_retained_anchor() -> None:
    _, b, c = bullish_triplet()
    history = ((b.close_time, candle_fingerprint(b)),)
    with pytest.raises(FvgDataConflictError, match="historical candle missing"):
        FvgModule._validate_history_fingerprints(  # noqa: SLF001
            (c,),
            ("BTCUSDT", "15m"),
            history,
            (),
            0,
        )


def test_restore_wraps_invalid_zone_payload() -> None:
    state = copy.deepcopy(FvgModule(timeframes=("15m",)).snapshot_state())
    state["zones"] = [{"invalid": "zone"}]
    with pytest.raises(FvgStateError, match="invalid zone state"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_restore_rejects_zone_without_causal_cursor() -> None:
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bullish_triplet()))
    state = copy.deepcopy(source.snapshot_state())
    state["last_processed"] = []
    with pytest.raises(FvgStateError, match="causally covered"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_restore_rejects_bound_symbol_mismatch_with_cursor() -> None:
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bullish_triplet()))
    state = copy.deepcopy(source.snapshot_state())
    state["bound_symbol"] = "ETHUSDT"
    with pytest.raises(FvgStateError, match="does not match"):
        FvgModule(timeframes=("15m",)).restore_state(state)


def test_restore_rejects_unconfigured_zone_timeframe_with_canonical_id() -> None:
    source = FvgModule(timeframes=("4h",))
    source.on_bar(view_for(bullish_triplet(timeframe="4h"), timeframe="4h"))
    zone = copy.deepcopy(source.snapshot_state()["zones"][0])

    target = FvgModule(timeframes=("15m",))
    target_state = copy.deepcopy(target.snapshot_state())
    target_state["bound_symbol"] = "BTCUSDT"
    target_state["zones"] = [zone]
    with pytest.raises(FvgStateError, match="unconfigured timeframe"):
        target.restore_state(target_state)
