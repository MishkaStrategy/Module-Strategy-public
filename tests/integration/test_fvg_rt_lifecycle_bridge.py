from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_core import Candle, Feature, FeatureView, MarketView
from modular_modules.entry_model import EntryKind, EntryModelModule
from modular_setup_adapter import CanonicalSetupAdapterModule
from modular_strategy_protection_target import ProtectionTargetModule
from modular_strategy_trade_plan_router.normalizer import DecisionNormalizer
from strategy_modules.fvg_rt import (
    FEATURE_KEY,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    Frozen1hShortModule,
    Frozen4hShortModule,
    FvgRtFinal15mModule,
)


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _bar() -> Candle:
    return Candle(
        timestamp=NOW - timedelta(minutes=5),
        timeframe="5m",
        symbol="BTCUSDT",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
    )


def _view(current: Candle, features: tuple[Feature, ...]) -> MarketView:
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        features=FeatureView(features, current.close_time),
    )


def test_final15_lifecycle_survives_setup_entry_protection_router_chain() -> None:
    current = _bar()
    producer = FvgRtFinal15mModule()
    raw = producer._snapshot(
        "long",
        current.symbol,
        current.close_time,
        entry=100.0,
        stop=95.0,
    )
    assert raw is not None
    source = producer._feature(raw, current.close_time, current.symbol)

    adapter = CanonicalSetupAdapterModule(
        sources=(
            {
                "feature_key": FEATURE_KEY,
                "profile": "fvg_rt_final_15m_v1",
                "source_module": "fvg_rt_final_15m",
            },
        )
    )
    adapted = adapter.on_bar(_view(current, (source,)))
    assert len(adapted.features) == 1
    setup = adapted.features[0]

    entry_decision = EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_EXPLICIT,
        ttl_bars=576,
        allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
    ).on_bar(_view(current, (setup,)))
    assert len(entry_decision.features) == 1
    entry = entry_decision.features[0]
    assert entry.key == "entry_model.plan"
    assert entry.value["ttl_bars"] == 36
    assert entry.value["expires_at"] == (current.close_time + timedelta(minutes=180)).isoformat()
    assert entry.value["metadata"]["priority"] == -10
    assert entry.value["metadata"]["strategy_id"] == "fvg_rt"
    assert entry.value["metadata"]["module_id"] == "fvg_rt_final_15m"

    protection_decision = ProtectionTargetModule(
        entry_feature_key="entry_model.plan",
        invalidation_mode="entry_plan",
        invalidation_field="metadata.source_setup_metadata.invalidation_price",
        stop_buffer_bps="0",
        target_mode="structural",
        structural_target_source="entry_plan",
        target_field="metadata.source_setup_metadata.target_price",
        max_holding_seconds_field="metadata.source_setup_metadata.max_holding_seconds",
        no_progress_rule_field="metadata.source_setup_metadata.no_progress_rule",
    ).on_bar(_view(current, (entry,)))
    assert len(protection_decision.features) == 1
    protection = protection_decision.features[0]
    assert protection.key == "protection_target.plan"
    assert protection.value["stop_loss"] == "95"
    assert protection.value["take_profit"] == "122.5"
    assert protection.value["reward_r"] == "4.5"
    assert protection.value["max_holding_seconds"] == 86_400
    assert protection.value["no_progress_rule"] == {
        "after_seconds": 14_400,
        "minimum_mfe_r": "0.14999999999999999",
    }

    normalizer = DecisionNormalizer()
    entry_candidate = normalizer.normalize_entry(
        entry.value,
        feature_key=entry.key,
        feature_schema_version=entry.schema_version,
        feature_created_at=entry.created_at,
        feature_valid_until=entry.valid_until,
        feature_id=entry.feature_id,
        source_module=entry.source_module,
        symbol=current.symbol,
        portfolio_id="default",
    )
    protection_candidate = normalizer.normalize_protection(
        protection.value,
        feature_schema_version=protection.schema_version,
        feature_created_at=protection.created_at,
        feature_valid_until=protection.valid_until,
        feature_id=protection.feature_id,
        symbol=current.symbol,
    )

    assert entry_candidate.ttl_bars == 36
    assert entry_candidate.priority == -10
    assert entry_candidate.strategy_id == "fvg_rt"
    assert entry_candidate.module_id == "fvg_rt_final_15m"
    assert protection_candidate.stop_loss == 95
    assert protection_candidate.targets[0].price == 122.5
    assert protection_candidate.max_holding_seconds == 86_400
    assert protection_candidate.no_progress_rule == {
        "after_seconds": 14_400,
        "minimum_mfe_r": "0.14999999999999999",
    }


def test_frozen_lifecycle_survives_setup_entry_protection_router_chain() -> None:
    current = _bar()
    cases = (
        (
            Frozen1hShortModule(),
            FROZEN_1H_FEATURE_KEY,
            "fvg_rt_frozen_1h_v1",
            "frozen_1h_short",
            48,
            -20,
            43_200,
            21_600,
            "0.5",
        ),
        (
            Frozen4hShortModule(),
            FROZEN_4H_FEATURE_KEY,
            "fvg_rt_frozen_4h_v1",
            "frozen_4h_short",
            576,
            -30,
            172_800,
            129_600,
            "0.34999999999999998",
        ),
    )

    for (
        producer,
        feature_key,
        profile,
        module_id,
        ttl_bars,
        priority,
        max_holding_seconds,
        no_progress_seconds,
        minimum_mfe_r,
    ) in cases:
        raw = producer._snapshot(
            current.symbol,
            current.close_time,
            entry=101.0,
            stop=103.0,
        )
        assert raw is not None
        source = producer._feature(raw, current.close_time, current.symbol)

        adapter = CanonicalSetupAdapterModule(
            sources=(
                {
                    "feature_key": feature_key,
                    "profile": profile,
                    "source_module": module_id,
                },
            )
        )
        adapted = adapter.on_bar(_view(current, (source,)))
        assert len(adapted.features) == 1
        setup = adapted.features[0]

        entry_decision = EntryModelModule(
            default_entry_kind=EntryKind.LIMIT_EXPLICIT,
            ttl_bars=576,
            allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
        ).on_bar(_view(current, (setup,)))
        assert len(entry_decision.features) == 1
        entry = entry_decision.features[0]
        assert entry.key == "entry_model.plan"
        assert entry.value["ttl_bars"] == ttl_bars
        assert entry.value["metadata"]["priority"] == priority
        assert entry.value["metadata"]["strategy_id"] == "fvg_rt"
        assert entry.value["metadata"]["module_id"] == module_id

        protection_decision = ProtectionTargetModule(
            entry_feature_key="entry_model.plan",
            invalidation_mode="entry_plan",
            invalidation_field="metadata.source_setup_metadata.invalidation_price",
            stop_buffer_bps="0",
            target_mode="structural",
            structural_target_source="entry_plan",
            target_field="metadata.source_setup_metadata.target_price",
            max_holding_seconds_field="metadata.source_setup_metadata.max_holding_seconds",
            no_progress_rule_field="metadata.source_setup_metadata.no_progress_rule",
        ).on_bar(_view(current, (entry,)))
        assert len(protection_decision.features) == 1
        protection = protection_decision.features[0]
        assert protection.key == "protection_target.plan"
        assert protection.value["stop_loss"] == "103"
        assert protection.value["take_profit"] == "89"
        assert protection.value["reward_r"] == "6"
        assert protection.value["max_holding_seconds"] == max_holding_seconds
        assert protection.value["no_progress_rule"] == {
            "after_seconds": no_progress_seconds,
            "minimum_mfe_r": minimum_mfe_r,
        }

        normalizer = DecisionNormalizer()
        entry_candidate = normalizer.normalize_entry(
            entry.value,
            feature_key=entry.key,
            feature_schema_version=entry.schema_version,
            feature_created_at=entry.created_at,
            feature_valid_until=entry.valid_until,
            feature_id=entry.feature_id,
            source_module=entry.source_module,
            symbol=current.symbol,
            portfolio_id="default",
        )
        protection_candidate = normalizer.normalize_protection(
            protection.value,
            feature_schema_version=protection.schema_version,
            feature_created_at=protection.created_at,
            feature_valid_until=protection.valid_until,
            feature_id=protection.feature_id,
            symbol=current.symbol,
        )

        assert entry_candidate.ttl_bars == ttl_bars
        assert entry_candidate.priority == priority
        assert entry_candidate.strategy_id == "fvg_rt"
        assert entry_candidate.module_id == module_id
        assert protection_candidate.stop_loss == 103
        assert protection_candidate.targets[0].price == 89
        assert protection_candidate.max_holding_seconds == max_holding_seconds
        assert protection_candidate.no_progress_rule == {
            "after_seconds": no_progress_seconds,
            "minimum_mfe_r": minimum_mfe_r,
        }
