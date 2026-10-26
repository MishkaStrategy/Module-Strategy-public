from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    Feature,
    FeatureStore,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
    capture_module_states,
    restore_module_states,
)
from strategy_modules.risk_sizing import RiskRejectReason, RiskSizingModule

NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def payload(request_id: str = "r1", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": "1",
        "request_id": request_id,
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_price": "100",
        "stop_loss": "99",
        "equity": "10000",
        "requested_risk_fraction": "0.005",
        "instrument": {
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "min_notional": "5",
            "tick_size": "0.1",
            "max_leverage": "20",
        },
        "portfolio": {"open_positions": 0},
    }
    row.update(overrides)
    return row


def make_view(
    value: object | None,
    *,
    feature_time: datetime | None = None,
    valid_until: datetime | None = None,
    feature_schema: str = "1",
    feature_symbol: str = "BTCUSDT",
    current_time: datetime = NOW + timedelta(minutes=5),
    feature_id: str | None = None,
    close: float = 100,
    data_gap: bool = False,
) -> MarketView:
    current = Candle(NOW, 100, 101, 99, close, 1, "BTCUSDT", "5m")
    store = FeatureStore()
    if value is not None:
        store.publish(
            Feature(
                "risk.request",
                "entry_model",
                feature_time or current_time,
                value,
                symbol=feature_symbol,
                timeframe="5m",
                valid_until=valid_until,
                schema_version=feature_schema,
                feature_id=feature_id,
            )
        )
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=0,
        cash=10000,
        equity=10000,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
        features=store.snapshot(current_time),
    )


class FakeFeatureView:
    def __init__(self, feature: object | None) -> None:
        self.feature = feature

    def latest(self, key: str, **filters: object) -> object | None:
        return self.feature


def fake_feature(**overrides: object) -> object:
    values: dict[str, object] = {
        "feature_id": "external-1",
        "created_at": NOW + timedelta(minutes=5),
        "valid_until": NOW + timedelta(minutes=5),
        "schema_version": "1",
        "symbol": "BTCUSDT",
        "value": payload(),
    }
    values.update(overrides)
    return type("FakeFeature", (), values)()


def fake_view(feature: object, symbol: str = "BTCUSDT") -> MarketView:
    current = Candle(NOW, 100, 101, 99, 100, 1, symbol, "5m")
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=0,
        cash=10000,
        equity=10000,
        position=None,
        pending_orders=(),
        features=FakeFeatureView(feature),  # type: ignore[arg-type]
    )


def test_manifest_and_real_core_contract() -> None:
    manifest = RiskSizingModule.manifest
    assert manifest.version == "1.2.0"
    assert manifest.state_schema_version == "2"
    assert manifest.required_features == ("risk.request",)
    result = RiskSizingModule().on_bar(make_view(payload()))
    feature = result.features[0]
    assert feature.key == "risk.decision"
    assert feature.value["approved"] is True
    assert feature.created_at == feature.valid_until == NOW + timedelta(minutes=5)


def test_missing_feature_and_features_none() -> None:
    assert RiskSizingModule().on_bar(make_view(None)).skips == ("risk_request_unavailable",)
    current = Candle(NOW, 100, 101, 99, 100, 1, "BTCUSDT", "5m")
    view = MarketView((current,), {}, 0, 10000, 10000, None, (), features=None)
    assert RiskSizingModule().on_bar(view).skips == ("risk_request_unavailable",)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"feature_time": NOW}, RiskRejectReason.STALE_REQUEST),
        ({"feature_schema": "2"}, RiskRejectReason.UNSUPPORTED_SCHEMA),
    ],
)
def test_temporal_and_schema_fail_safe(kwargs: dict[str, object], reason: RiskRejectReason) -> None:
    result = RiskSizingModule().on_bar(make_view(payload(), **kwargs))
    assert result.features[0].value["reject_reason"] == reason.value


def test_feature_store_hides_incompatible_requests() -> None:
    future = RiskSizingModule().on_bar(
        make_view(payload(), feature_time=NOW + timedelta(minutes=10))
    )
    expired = RiskSizingModule().on_bar(make_view(payload(), feature_time=NOW, valid_until=NOW))
    wrong_symbol = RiskSizingModule().on_bar(make_view(payload(), feature_symbol="ETHUSDT"))
    assert future.skips == ("risk_request_unavailable",)
    assert expired.skips == ("risk_request_unavailable",)
    assert wrong_symbol.skips == ("risk_request_unavailable",)


def test_adapter_rejects_incompatible_feature_from_unfiltered_view() -> None:
    future = RiskSizingModule().on_bar(
        fake_view(
            fake_feature(
                created_at=NOW + timedelta(minutes=10),
                valid_until=NOW + timedelta(minutes=10),
            )
        )
    )
    expired = RiskSizingModule().on_bar(fake_view(fake_feature(created_at=NOW, valid_until=NOW)))
    wrong_symbol = RiskSizingModule().on_bar(fake_view(fake_feature(symbol="ETHUSDT")))
    assert future.features[0].value["reject_reason"] == "future_request"
    assert expired.features[0].value["reject_reason"] == "stale_request"
    assert wrong_symbol.features[0].value["reject_reason"] == "invalid_request"


def test_payload_symbol_mismatch_is_rejected() -> None:
    result = RiskSizingModule().on_bar(make_view(payload(symbol="ETHUSDT")))
    assert result.features[0].value["reject_reason"] == "invalid_request"


def test_exact_event_and_republished_request_are_idempotent() -> None:
    module = RiskSizingModule()
    first_view = make_view(payload(), feature_id="f1")
    assert module.on_bar(first_view).features
    duplicate_event = module.on_bar(first_view)
    assert duplicate_event.features == ()
    assert duplicate_event.diagnostics["event"] == "duplicate_event"
    duplicate_request = module.on_bar(make_view(payload(), feature_id="f2"))
    assert duplicate_request.features == ()
    assert duplicate_request.diagnostics["event"] == "duplicate_request"


def test_conflicting_duplicate_is_rejected_once() -> None:
    module = RiskSizingModule()
    module.on_bar(make_view(payload(), feature_id="f1"))
    conflict = make_view(payload(entry_price="101", stop_loss="100"), feature_id="f2")
    first = module.on_bar(conflict)
    second = module.on_bar(conflict)
    assert first.features[0].value["reject_reason"] == "request_id_conflict"
    assert second.features == ()


def test_malformed_request_does_not_reserve_id() -> None:
    module = RiskSizingModule()
    malformed = payload()
    del malformed["entry_price"]
    assert not module.on_bar(make_view(malformed, feature_id="bad")).features[0].value["approved"]
    assert module.on_bar(make_view(payload(), feature_id="fixed")).features[0].value["approved"]


def test_non_mapping_and_unsupported_payloads_are_structured_and_idempotent() -> None:
    result = RiskSizingModule().on_bar(fake_view(fake_feature(value=[1, 2])))
    assert result.features[0].value["reject_reason"] == "invalid_request"

    class Unsupported:
        pass

    feature = fake_feature(
        feature_id="unsupported",
        value={"request_id": "bad", "object": Unsupported()},
    )
    module = RiskSizingModule()
    first = module.on_bar(fake_view(feature))
    second = module.on_bar(fake_view(feature))
    assert first.features[0].value["reject_reason"] == "invalid_request"
    assert second.features == ()
    assert second.diagnostics["event"] == "duplicate_invalid_event"


def test_checkpoint_roundtrip_uses_real_core_freeze() -> None:
    first = RiskSizingModule()
    view = make_view(payload(), feature_id="f1")
    first.on_bar(view)
    states = capture_module_states((first,))
    second = RiskSizingModule()
    restore_module_states((second,), states)
    assert second.on_bar(view).features == ()


def test_restore_is_atomic_and_config_bound() -> None:
    module = RiskSizingModule()
    module.on_bar(make_view(payload(), feature_id="f1"))
    before = module.snapshot_state()
    corrupted = dict(before)
    corrupted["request_order"] = ["missing"]
    with pytest.raises(ValueError, match="disagree"):
        module.restore_state(corrupted)
    assert module.snapshot_state() == before
    with pytest.raises(ValueError, match="configuration fingerprint"):
        RiskSizingModule(max_positions=11).restore_state(before)


def test_empty_restore_and_instances_are_independent() -> None:
    view = make_view(payload(), feature_id="f1")
    first = RiskSizingModule()
    second = RiskSizingModule()
    assert first.on_bar(view).features
    assert second.on_bar(view).features
    first.restore_state({})
    assert first.on_bar(view).features


def test_future_market_values_and_gap_do_not_change_decision() -> None:
    first = RiskSizingModule().on_bar(make_view(payload(), close=100)).features[0].value
    second = (
        RiskSizingModule()
        .on_bar(make_view(payload(), close=100.9, data_gap=True))
        .features[0]
        .value
    )
    assert first == second


def test_output_and_diagnostics_are_immutable() -> None:
    result = RiskSizingModule().on_bar(make_view(payload(setup_id="setup-42")))
    feature = result.features[0]
    assert feature.value["setup_id"] == "setup-42"
    assert feature.metadata["setup_id"] == "setup-42"
    assert isinstance(feature.value, MappingProxyType)
    with pytest.raises(TypeError):
        feature.value["quantity"] = "999"  # type: ignore[index]
    missing = RiskSizingModule().on_bar(make_view(None))
    with pytest.raises(TypeError):
        missing.diagnostics["x"] = 1  # type: ignore[index]


def test_cache_is_bounded_and_state_validation_is_strict() -> None:
    module = RiskSizingModule(max_processed_requests=2)
    for index in range(3):
        module.on_bar(make_view(payload(f"r{index}"), feature_id=f"f{index}"))
    state = module.snapshot_state()
    assert state["request_order"] == ["r1", "r2"]
    assert len(state["event_order"]) == 2
    variants = [
        {**state, "schema_version": "999"},
        {**state, "request_fingerprints": []},
        {**state, "request_order": ["r1", "r1"]},
        {**state, "event_order": ["x", "x"]},
        {**state, "event_fingerprints": []},
        {**state, "counters": {"received": True}},
        {**state, "counters": {**state["counters"], "received": -1}},
    ]
    for variant in variants:
        with pytest.raises(ValueError):
            module.restore_state(variant)


def test_stream_symbol_change_is_rejected_and_checkpointed() -> None:
    module = RiskSizingModule()
    module.on_bar(make_view(payload(), feature_id="btc"))
    current_time = NOW + timedelta(minutes=10)
    candle = Candle(NOW + timedelta(minutes=5), 10, 11, 9, 10, 1, "ETHUSDT", "5m")
    store = FeatureStore()
    store.publish(
        Feature(
            "risk.request",
            "entry",
            current_time,
            {**payload("eth"), "symbol": "ETHUSDT"},
            symbol="ETHUSDT",
            timeframe="5m",
        )
    )
    view = MarketView(
        (candle,), {}, 1, 10000, 10000, None, (), features=store.snapshot(current_time)
    )
    result = module.on_bar(view)
    assert result.features[0].value["reject_reason"] == "stream_changed"
    restored = RiskSizingModule()
    restored.restore_state(module.snapshot_state())
    assert restored.snapshot_state()["stream_symbol"] == "BTCUSDT"


def test_exact_config_must_fit_core_run_identity_contract() -> None:
    with pytest.raises(ValueError, match="precision cannot be represented"):
        RiskSizingModule(default_risk_fraction=Decimal("0.0050000000000000000001"))


class Producer(StrategyModule):
    name = "producer"
    manifest = ModuleManifest(name=name, produced_features=("risk.request",))

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(
                Feature(
                    "risk.request",
                    self.name,
                    view.current.close_time,
                    payload(f"r{view.index}"),
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=view.current.close_time,
                ),
            )
        )


def test_dependency_planner_and_real_engine_pipeline() -> None:
    plan = ModuleRegistry().validate_and_plan([RiskSizingModule(), Producer()], base_timeframe="5m")
    assert [module.name for module in plan.modules] == ["producer", "risk_sizing"]
    with pytest.raises(ValueError, match="missing module dependencies"):
        ModuleRegistry().validate_and_plan([RiskSizingModule()], base_timeframe="5m")

    bars = [
        Candle(NOW + timedelta(minutes=5 * index), 100, 101, 99, 100, 1, "BTCUSDT", "5m")
        for index in range(3)
    ]
    result = BacktestEngine((RiskSizingModule(), Producer())).run(bars)
    decisions = [
        row["feature"]
        for row in result.journal.features
        if row["action"] == "published" and row["feature"].key == "risk.decision"
    ]
    assert len(decisions) == 3
    assert all(feature.value["approved"] for feature in decisions)


def test_entry_point_discovery() -> None:
    registry = ModuleRegistry()
    assert "risk_sizing" in registry.discover_entry_points()
    assert isinstance(registry.load_named("risk_sizing"), RiskSizingModule)
