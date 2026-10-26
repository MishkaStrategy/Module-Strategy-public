from __future__ import annotations

from datetime import UTC, datetime

import pytest
from modular_core import Candle, Feature, FeatureView, MarketView
from strategy_modules.risk_sizing import RiskSizingModule

from modular_strategy_risk_request_composer import (
    RiskRequestComposerError,
    RiskRequestComposerModule,
)

OPEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _features(*, stop: str = "95", include_instrument: bool = True, include_portfolio: bool = True):
    candle = Candle(OPEN, 100, 101, 99, 100, 1000, "BTCUSDT", "5m")
    now = candle.close_time
    entry = Feature(
        key="entry_model.plan",
        source_module="entry_model",
        created_at=now,
        valid_until=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        symbol="BTCUSDT",
        timeframe="5m",
        schema_version="2",
        feature_id="entry-feature",
        value={
            "schema_version": "2",
            "plan_id": "entry-plan-1",
            "module_version": "0.2.0",
            "setup_id": "setup-1",
            "setup_fingerprint": "a" * 64,
            "config_fingerprint": "b" * 64,
            "symbol": "BTCUSDT",
            "side": "buy",
            "entry_kind": "limit_explicit",
            "order_type": "limit",
            "created_at": now.isoformat(),
            "setup_confirmed_at": now.isoformat(),
            "expires_at": datetime(2026, 1, 1, 12, 10, tzinfo=UTC).isoformat(),
            "ttl_bars": 12,
            "price": "100",
            "trigger_price": None,
            "limit_price": "100",
            "post_only": False,
            "rationale": "fixture",
            "metadata": {},
        },
    )
    protection = Feature(
        key="protection_target.plan",
        source_module="protection_target",
        created_at=now,
        valid_until=now,
        symbol="BTCUSDT",
        timeframe="5m",
        schema_version="1",
        feature_id="protection-feature",
        value={
            "schema_version": "1",
            "protection_plan_id": "protection-1",
            "entry_plan_id": "entry-plan-1",
            "setup_id": "setup-1",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "side": "buy",
            "entry_price": "100",
            "invalidation_price": "94",
            "stop_loss": stop,
            "take_profit": "110",
            "risk_distance": "5",
            "reward_distance": "10",
            "stop_distance_bps": "500",
            "reward_r": "2",
            "invalidation_mode": "explicit",
            "target_mode": "r_multiple",
            "created_at": now.isoformat(),
            "valid_until": now.isoformat(),
            "source_entry_feature_id": "entry-feature",
            "source_invalidation_feature_id": "invalidation-feature",
            "source_target_feature_id": "target-feature",
        },
    )
    values = [entry, protection]
    if include_instrument:
        values.append(
            Feature(
                key="instrument.rules",
                source_module="instrument_metadata",
                created_at=now,
                valid_until=now,
                symbol="BTCUSDT",
                timeframe="5m",
                feature_id="instrument-feature",
                value={
                    "quantity_step": "0.001",
                    "min_quantity": "0.001",
                    "min_notional": "5",
                    "tick_size": "0.1",
                    "contract_size": "1",
                    "max_leverage": "20",
                },
            )
        )
    if include_portfolio:
        values.append(
            Feature(
                key="portfolio.risk_snapshot",
                source_module="portfolio_snapshot",
                created_at=now,
                valid_until=now,
                symbol="BTCUSDT",
                timeframe="5m",
                feature_id="portfolio-feature",
                value={
                    "gross_notional": "0",
                    "symbol_notional": "0",
                    "open_risk_amount": "0",
                    "open_positions": 0,
                },
            )
        )
    return candle, tuple(values)


def _view(features, *, equity: float = 10_000.0):
    candle, values = features
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=equity,
        position=None,
        pending_orders=(),
        features=FeatureView(values, candle.close_time),
    )


def test_composes_real_risk_request_and_risk_sizing_consumes_it() -> None:
    candle, values = _features()
    composer = RiskRequestComposerModule(requested_risk_fraction="0.005")
    result = composer.on_bar(_view((candle, values)))
    assert not result.skips
    assert len(result.features) == 1
    request_feature = result.features[0]
    assert request_feature.key == "risk.request"
    assert request_feature.value["entry_price"] == "100"
    assert request_feature.value["stop_loss"] == "95"
    assert request_feature.value["requested_risk_fraction"] == "0.005"

    risk_view = _view((candle, (*values, request_feature)))
    risk_result = RiskSizingModule().on_bar(risk_view)
    assert len(risk_result.features) == 1
    assert risk_result.features[0].key == "risk.decision"
    assert risk_result.features[0].value["status"] == "approved"


def test_exact_duplicate_is_suppressed() -> None:
    composer = RiskRequestComposerModule()
    view = _view(_features())
    first = composer.on_bar(view)
    second = composer.on_bar(view)
    assert first.features
    assert second.skips == ("risk_request_composer.duplicate_request",)


def test_missing_external_risk_evidence_fails_closed() -> None:
    composer = RiskRequestComposerModule()
    result = composer.on_bar(_view(_features(include_instrument=False)))
    assert result.skips == ("risk_request_composer.invalid_upstream",)
    assert "instrument rules feature is required" in result.diagnostics["error"]


def test_entry_protection_identity_mismatch_fails_closed() -> None:
    candle, values = _features()
    altered = list(values)
    protection = altered[1]
    payload = dict(protection.value)
    payload["entry_plan_id"] = "other-plan"
    altered[1] = Feature(
        key=protection.key,
        source_module=protection.source_module,
        created_at=protection.created_at,
        valid_until=protection.valid_until,
        symbol=protection.symbol,
        timeframe=protection.timeframe,
        schema_version=protection.schema_version,
        feature_id="protection-feature-conflict",
        value=payload,
    )
    result = RiskRequestComposerModule().on_bar(_view((candle, tuple(altered))))
    assert result.skips == ("risk_request_composer.invalid_upstream",)
    assert "another entry plan" in result.diagnostics["error"]


def test_state_round_trip_and_corruption_detection() -> None:
    module = RiskRequestComposerModule()
    module.on_bar(_view(_features()))
    state = module.snapshot_state()
    restored = RiskRequestComposerModule()
    restored.restore_state(state)
    assert restored.on_bar(_view(_features())).skips == ("risk_request_composer.duplicate_request",)
    corrupt = dict(state)
    corrupt["processed"] = {}
    with pytest.raises(RiskRequestComposerError, match="integrity"):
        RiskRequestComposerModule().restore_state(corrupt)


def test_same_stream_identity_conflict_is_rejected() -> None:
    module = RiskRequestComposerModule()
    module.on_bar(_view(_features()))
    candle, values = _features()
    changed = list(values)
    protection = changed[1]
    payload = dict(protection.value)
    payload["stop_loss"] = "96"
    changed[1] = Feature(
        key=protection.key,
        source_module=protection.source_module,
        created_at=protection.created_at,
        valid_until=protection.valid_until,
        symbol=protection.symbol,
        timeframe=protection.timeframe,
        schema_version=protection.schema_version,
        feature_id="protection-feature-changed",
        value=payload,
    )
    with pytest.raises(RiskRequestComposerError, match="different content"):
        module.on_bar(_view((candle, tuple(changed))))
