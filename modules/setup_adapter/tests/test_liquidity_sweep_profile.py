from __future__ import annotations

from decimal import Decimal

import pytest
from modular_core import Feature, FeatureView, MarketView
from modular_modules.entry_model import EntryModelModule, EntryPlan

from modular_setup_adapter import AdapterReason, CanonicalSetupAdapterModule

from .conftest import bar, view


def sweep_feature(
    current,
    *,
    direction: str,
    feature_id: str,
    overrides: dict[str, object] | None = None,
) -> Feature:
    bullish = direction == "bullish"
    payload: dict[str, object] = {
        "event_id": feature_id.removeprefix("feat-"),
        "decision_time": current.close_time.isoformat(),
        "level_id": "liq-level-1",
        "liquidity_side": "low" if bullish else "high",
        "level_price": 100.8 if bullish else 101.0,
        "close_price": current.close,
        "pivot_time": current.open_time.isoformat(),
        "confirmed_at": current.open_time.isoformat(),
        "bars_since_confirmation": 4,
        "direction": direction,
        "penetration_bps": 9.0,
        "wick_to_body_ratio": 2.0,
        "wick_fraction": 0.5,
        "extreme_price": current.low if bullish else current.high,
    }
    if overrides:
        payload.update(overrides)
    return Feature(
        key="liquidity_sweep.event",
        source_module="liquidity_sweep",
        created_at=current.close_time,
        value=payload,
        symbol=current.symbol,
        timeframe=current.timeframe,
        valid_until=current.close_time,
        schema_version="1",
        feature_id=feature_id,
    )


def adapter() -> CanonicalSetupAdapterModule:
    return CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "liquidity_sweep.event",
                "profile": "liquidity_sweep_v1",
                "source_module": "liquidity_sweep",
            }
        ]
    )


@pytest.mark.parametrize(
    ("direction", "feature_id", "expected_side", "expected_limit"),
    (
        ("bullish", "feat-liq-sweep-buy", "buy", Decimal("100.8")),
        ("bearish", "feat-liq-sweep-sell", "sell", Decimal("101")),
    ),
)
def test_liquidity_sweep_profile_reaches_entry_model_retracement(
    direction: str,
    feature_id: str,
    expected_side: str,
    expected_limit: Decimal,
) -> None:
    current = bar(close=100.9)
    source = sweep_feature(current, direction=direction, feature_id=feature_id)

    adapted = adapter().on_bar(view(current, (source,)))
    assert adapted.skips == ()
    assert adapted.diagnostics["adapted"] == 1
    assert len(adapted.features) == 1

    setup = adapted.features[0]
    assert setup.key == "setup.confirmed"
    assert setup.value["setup_id"] == feature_id.removeprefix("feat-")
    assert setup.value["side"] == expected_side
    assert setup.value["confirmation_price"] == "100.9"
    assert "entry_kind" not in setup.value
    assert setup.value["metadata"]["strategy_family"] == "liquidity_sweep"
    assert setup.value["metadata"]["adapter_profile"] == "liquidity_sweep_v1"
    assert setup.value["metadata"]["source"]["level_id"] == "liq-level-1"

    entry = EntryModelModule(
        default_entry_kind="limit_retracement",
        retracement_fraction="0.5",
        ttl_bars=12,
        allowed_entry_kinds=("limit_retracement",),
    )
    entry_view = MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=0,
        cash=1000.0,
        equity=1000.0,
        position=None,
        pending_orders=(),
        features=FeatureView((setup,), current.close_time),
    )
    decision = entry.on_bar(entry_view)
    assert decision.skips == ()
    assert len(decision.features) == 1
    plan = EntryPlan.from_feature_value(decision.features[0].value)
    assert plan.side.value == expected_side
    assert plan.limit_price == expected_limit


def test_liquidity_sweep_profile_rejects_direction_level_contradiction() -> None:
    current = bar(close=100.9)
    source = sweep_feature(
        current,
        direction="bullish",
        feature_id="feat-liq-sweep-contradiction",
        overrides={"liquidity_side": "high"},
    )

    decision = adapter().on_bar(view(current, (source,)))

    assert decision.skips == (AdapterReason.INVALID_PRICE_GEOMETRY.value,)
    assert decision.features[0].key == "setup_adapter.rejection"


def test_liquidity_sweep_profile_rejects_source_bar_price_drift() -> None:
    current = bar(close=100.9)
    source = sweep_feature(
        current,
        direction="bullish",
        feature_id="feat-liq-sweep-drift",
        overrides={"close_price": 100.85},
    )

    decision = adapter().on_bar(view(current, (source,)))

    assert decision.skips == (AdapterReason.INVALID_PRICE_GEOMETRY.value,)


def test_liquidity_sweep_profile_rejects_unknown_source_fields() -> None:
    current = bar(close=100.9)
    source = sweep_feature(
        current,
        direction="bearish",
        feature_id="feat-liq-sweep-unknown",
        overrides={"entry_kind": "market"},
    )

    decision = adapter().on_bar(view(current, (source,)))

    assert decision.skips == (AdapterReason.INVALID_SOURCE_PAYLOAD.value,)


def test_liquidity_sweep_profile_rejects_invalid_level_chronology() -> None:
    current = bar(close=100.9)
    source = sweep_feature(
        current,
        direction="bullish",
        feature_id="feat-liq-sweep-chronology",
        overrides={"pivot_time": current.close_time.isoformat()},
    )

    decision = adapter().on_bar(view(current, (source,)))

    assert decision.skips == (AdapterReason.FEATURE_CAUSALITY_VIOLATION.value,)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("bars_since_confirmation", -1),
        ("penetration_bps", -1),
        ("wick_to_body_ratio", -0.1),
        ("wick_fraction", 1.1),
    ),
)
def test_liquidity_sweep_profile_rejects_invalid_diagnostics(
    field: str,
    value: object,
) -> None:
    current = bar(close=100.9)
    source = sweep_feature(
        current,
        direction="bearish",
        feature_id=f"feat-liq-sweep-invalid-{field}",
        overrides={field: value},
    )

    decision = adapter().on_bar(view(current, (source,)))

    assert decision.skips == (AdapterReason.INVALID_SOURCE_PAYLOAD.value,)
