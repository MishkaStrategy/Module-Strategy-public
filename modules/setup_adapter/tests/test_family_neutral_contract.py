from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from modular_core import Feature, FeatureView, MarketView, Side
from modular_modules.entry_model import EntryModelModule, EntryPlan

from modular_setup_adapter import (
    AdapterReason,
    CanonicalSetupAdapterModule,
    SourceProfile,
    build_canonical_setup_payload,
)

from .conftest import bar, breakout_feature, view


def canonical_source_feature(
    *,
    current,
    feature_id: str = "family-1",
    timeframe: str = "1h",
    metadata: dict[str, object] | None = None,
    overrides: dict[str, object] | None = None,
) -> Feature:
    value: dict[str, object] = {
        "setup_id": "family-BTCUSDT-1",
        "status": "confirmed",
        "symbol": "BTCUSDT",
        "side": "buy",
        "confirmed_at": current.close_time.isoformat(),
        "confirmation_price": "100.9",
        "entry_kind": "limit_explicit",
        "entry_price": "100.8",
        "metadata": metadata or {"strategy_family": "test_family"},
    }
    if overrides:
        value.update(overrides)
    return Feature(
        key="family.confirmed",
        source_module="family_module",
        created_at=current.close_time,
        value=value,
        symbol="BTCUSDT",
        timeframe=timeframe,
        valid_until=current.close_time,
        schema_version="1",
        feature_id=feature_id,
    )


def test_canonical_builder_emits_minimal_schema_without_policy() -> None:
    current = bar()
    payload = build_canonical_setup_payload(
        setup_id="setup-1",
        symbol="BTCUSDT",
        side=Side.BUY,
        confirmed_at=current.close_time,
        confirmation_price=Decimal("100.9"),
        entry_kind="limit_explicit",
        entry_price=Decimal("100.8"),
        metadata={"strategy_family": "example"},
    )

    assert dict(payload) == {
        "setup_id": "setup-1",
        "status": "confirmed",
        "symbol": "BTCUSDT",
        "side": "buy",
        "confirmed_at": current.close_time.isoformat(),
        "confirmation_price": "100.9",
        "entry_kind": "limit_explicit",
        "entry_price": "100.8",
        "metadata": {"strategy_family": "example"},
    }


def test_multiple_profiles_dispatch_to_same_canonical_schema() -> None:
    current = bar()
    canonical_source = canonical_source_feature(current=current)
    module = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "breakout_return.confirmed",
                "profile": "breakout_return_v1",
                "source_module": "breakout_return",
            },
            {
                "feature_key": "family.confirmed",
                "profile": "canonical_setup_v1",
                "source_module": "family_module",
            },
        ]
    )

    decision = module.on_bar(view(current, (breakout_feature(current), canonical_source)))
    setups = [feature for feature in decision.features if feature.key == "setup.confirmed"]

    assert len(setups) == 2
    assert {feature.value["metadata"]["adapter_profile"] for feature in setups} == {
        SourceProfile.BREAKOUT_RETURN_V1.value,
        SourceProfile.CANONICAL_SETUP_V1.value,
    }
    assert decision.diagnostics["adapted"] == 2
    assert decision.diagnostics["rejected"] == 0


def test_canonical_profile_accepts_htf_source_and_entry_model_consumes_output() -> None:
    current = bar()
    source = canonical_source_feature(current=current, timeframe="4h")
    adapter = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "family.confirmed",
                "profile": "canonical_setup_v1",
                "source_module": "family_module",
            }
        ]
    )
    adapted = adapter.on_bar(view(current, (source,)))
    assert len(adapted.features) == 1
    setup = adapted.features[0]
    assert setup.key == "setup.confirmed"
    assert setup.timeframe == "5m"
    assert setup.value["metadata"]["strategy_family"] == "test_family"
    assert setup.value["metadata"]["source_feature_key"] == "family.confirmed"
    assert setup.value["metadata"]["source_module"] == "family_module"

    entry = EntryModelModule(
        default_entry_kind="limit_explicit",
        ttl_bars=12,
        allowed_entry_kinds=("limit_explicit",),
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
    assert len(decision.features) == 1
    plan = EntryPlan.from_feature_value(decision.features[0].value)
    assert plan.entry_kind.value == "limit_explicit"
    assert plan.limit_price == Decimal("100.8")


def test_canonical_profile_overwrites_reserved_provenance() -> None:
    current = bar()
    source = canonical_source_feature(
        current=current,
        metadata={
            "strategy_family": "test_family",
            "adapter_profile": "forged",
            "source_module": "forged",
        },
    )
    module = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "family.confirmed",
                "profile": "canonical_setup_v1",
                "source_module": "family_module",
            }
        ]
    )

    setup = module.on_bar(view(current, (source,))).features[0]
    metadata = setup.value["metadata"]
    assert metadata["strategy_family"] == "test_family"
    assert metadata["adapter_profile"] == "canonical_setup_v1"
    assert metadata["source_module"] == "family_module"


def test_malformed_canonical_profile_fails_closed() -> None:
    current = bar()
    source = canonical_source_feature(
        current=current,
        overrides={"unexpected_policy": "hidden"},
    )
    module = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "family.confirmed",
                "profile": "canonical_setup_v1",
                "source_module": "family_module",
            }
        ]
    )

    decision = module.on_bar(view(current, (source,)))
    assert decision.skips == (AdapterReason.INVALID_SOURCE_PAYLOAD.value,)
    assert decision.features[0].key == "setup_adapter.rejection"


def test_canonical_profile_rejects_stale_confirmation() -> None:
    current = bar()
    source = canonical_source_feature(
        current=current,
        overrides={"confirmed_at": (current.close_time - timedelta(minutes=5)).isoformat()},
    )
    module = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "family.confirmed",
                "profile": "canonical_setup_v1",
                "source_module": "family_module",
            }
        ]
    )

    decision = module.on_bar(view(current, (source,)))
    assert decision.skips == (AdapterReason.FEATURE_CAUSALITY_VIOLATION.value,)
