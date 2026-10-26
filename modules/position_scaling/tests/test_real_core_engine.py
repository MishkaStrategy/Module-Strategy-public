from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from modular_core import (
    BacktestEngine,
    Candle,
    Feature,
    InstrumentSpec,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)

from modular_strategy_position_scaling import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_RISK_DECISION,
    FEATURE_RISK_REQUEST,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)


class EntryStub(StrategyModule):
    name = "entry_stub"
    manifest = ModuleManifest(name=name, skip_until_ready=False)
    parameters: Mapping[str, object] = MappingProxyType({})

    def __init__(self) -> None:
        self.sent = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.sent:
            return ModuleDecision()
        self.sent = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    stop_loss=95.0,
                    take_profit=120.0,
                    tag="engine-smoke",
                ),
            )
        )


class ContextStub(StrategyModule):
    name = "context_stub"
    manifest = ModuleManifest(
        name=name,
        produced_features=(
            "setup.validity",
            "instrument.rules",
            "portfolio.risk_snapshot",
            "position_scaling.context",
        ),
        skip_until_ready=False,
    )
    parameters: Mapping[str, object] = MappingProxyType({})

    def on_bar(self, view: MarketView) -> ModuleDecision:
        now = view.current.close_time
        rows = {
            "setup.validity": {"valid": True},
            "instrument.rules": {
                "quantity_step": "0.001",
                "min_quantity": "0.001",
                "min_notional": "5",
                "contract_size": "1",
                "max_leverage": "10",
            },
            "portfolio.risk_snapshot": {
                "gross_notional": "100",
                "symbol_notional": "100",
                "open_risk_amount": "5",
                "open_positions": 1,
            },
            "position_scaling.context": {
                "liquidation_distance_fraction": "0.2",
                "margin_usage_fraction": "0.1",
                "available_margin": "9000",
            },
        }
        return ModuleDecision(
            features=tuple(
                Feature(
                    key=key,
                    source_module=self.name,
                    created_at=now,
                    value=value,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=now,
                    feature_id=f"ctx-{view.index}-{key}",
                    invalidate_on_gap=False,
                )
                for key, value in rows.items()
            )
        )


class RiskStub(StrategyModule):
    name = "risk_stub_engine"
    manifest = ModuleManifest(
        name=name,
        required_features=(FEATURE_RISK_REQUEST,),
        produced_features=(FEATURE_RISK_DECISION,),
        skip_until_ready=False,
    )
    parameters: Mapping[str, object] = MappingProxyType({})

    def on_bar(self, view: MarketView) -> ModuleDecision:
        assert view.features is not None
        request = view.features.latest(FEATURE_RISK_REQUEST, symbol=view.current.symbol)
        if request is None:
            return ModuleDecision()
        value = request.value
        assert isinstance(value, Mapping)
        desired = value["metadata"]["desired_quantity"]
        return ModuleDecision(
            features=(
                Feature(
                    key=FEATURE_RISK_DECISION,
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value={
                        "schema_version": "1",
                        "request_id": value["request_id"],
                        "symbol": value["symbol"],
                        "status": "approved",
                        "approved": True,
                        "quantity": desired,
                        "setup_id": value["setup_id"],
                    },
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                    valid_until=view.current.close_time,
                    feature_id=f"risk-engine-{view.index}",
                    invalidate_on_gap=False,
                ),
            )
        )


def test_real_core_backtest_engine_closed_bar_pipeline() -> None:
    planner = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.25",
                "expires_bars": 0,
            }
        ],
    )
    modules = (
        EntryStub(),
        ContextStub(),
        planner,
        RiskStub(),
        PositionScalingApprovalModule(),
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = (
        Candle(
            timestamp=start,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            symbol="BTCUSDT",
            timeframe="1m",
        ),
        Candle(
            timestamp=start + timedelta(minutes=1),
            open=100,
            high=100,
            low=97,
            close=98,
            volume=1,
            symbol="BTCUSDT",
            timeframe="1m",
        ),
        Candle(
            timestamp=start + timedelta(minutes=2),
            open=98,
            high=99,
            low=97,
            close=98,
            volume=1,
            symbol="BTCUSDT",
            timeframe="1m",
        ),
    )
    result = BacktestEngine(
        modules,
        instrument=InstrumentSpec(
            symbol="BTCUSDT",
            tick_size=0.1,
            quantity_step=0.001,
            min_quantity=0.001,
            min_notional=5,
        ),
    ).run(candles)
    published = [row["feature"].key for row in result.journal.features]
    assert FEATURE_PLAN in published
    assert FEATURE_RISK_REQUEST in published
    assert FEATURE_RISK_DECISION in published
    assert FEATURE_APPROVED in published
    assert not result.journal.errors
