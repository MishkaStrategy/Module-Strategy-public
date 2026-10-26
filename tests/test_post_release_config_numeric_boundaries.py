from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    RiskConfig,
    Side,
    StrategyModule,
)


class _Noop(StrategyModule):
    name = "decimal_config_noop"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


class _OneTrade(StrategyModule):
    name = "decimal_config_trade"

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
                ),
            )
        )


def _bars() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(start, 100, 101, 99, 100, 10, "BTCUSDT", "5m"),
        Candle(
            start + timedelta(minutes=5),
            100,
            102,
            100,
            101,
            10,
            "BTCUSDT",
            "5m",
        ),
    ]


def test_numeric_configs_normalize_decimal_values_to_float() -> None:
    fees = FeeConfig(
        maker_rate=Decimal("0.0002"),
        taker_rate=Decimal("0.0005"),
    )
    execution = ExecutionConfig(
        slippage_bps=Decimal("1.5"),
        stop_slippage_bps=Decimal("2.5"),
        max_fill_fraction_per_bar=Decimal("0.75"),
        max_fill_quantity_per_bar=Decimal("3.5"),
    )
    risk = RiskConfig(
        default_risk_fraction=Decimal("0.005"),
        max_risk_fraction=Decimal("0.05"),
        max_notional_multiple=Decimal("3"),
        min_notional=Decimal("5"),
        min_quantity=Decimal("0.001"),
        quantity_step=Decimal("0.001"),
        min_stop_distance_pct=Decimal("0.001"),
        max_stop_distance_pct=Decimal("0.1"),
    )
    config = EngineConfig(
        initial_cash=Decimal("10000"),
        fees=fees,
        execution=execution,
        risk=risk,
    )

    for value in (
        fees.maker_rate,
        fees.taker_rate,
        execution.slippage_bps,
        execution.stop_slippage_bps,
        execution.max_fill_fraction_per_bar,
        execution.max_fill_quantity_per_bar,
        risk.default_risk_fraction,
        risk.max_risk_fraction,
        risk.max_notional_multiple,
        risk.min_notional,
        risk.min_quantity,
        risk.quantity_step,
        risk.min_stop_distance_pct,
        risk.max_stop_distance_pct,
        config.initial_cash,
    ):
        assert isinstance(value, float)


def test_decimal_and_float_configs_produce_identical_run_evidence() -> None:
    decimal_config = EngineConfig(
        initial_cash=Decimal("10000"),
        fees=FeeConfig(
            maker_rate=Decimal("0.0002"),
            taker_rate=Decimal("0.0005"),
        ),
        execution=ExecutionConfig(
            slippage_bps=Decimal("0"),
            stop_slippage_bps=Decimal("0"),
            max_fill_fraction_per_bar=Decimal("1"),
        ),
        risk=RiskConfig(
            default_risk_fraction=Decimal("0.005"),
            max_risk_fraction=Decimal("0.05"),
            max_notional_multiple=Decimal("3"),
        ),
    )
    float_config = EngineConfig()

    decimal_manifest = BacktestEngine(_Noop(), decimal_config).run(_bars()).run_manifest
    float_manifest = BacktestEngine(_Noop(), float_config).run(_bars()).run_manifest

    assert decimal_manifest.config_hash == float_manifest.config_hash
    assert decimal_manifest.run_id == float_manifest.run_id


def test_decimal_execution_config_runs_through_fill_and_fee_arithmetic() -> None:
    config = EngineConfig(
        initial_cash=Decimal("10000"),
        fees=FeeConfig(
            maker_rate=Decimal("0.0002"),
            taker_rate=Decimal("0.0005"),
        ),
        execution=ExecutionConfig(
            slippage_bps=Decimal("1"),
            stop_slippage_bps=Decimal("2"),
            max_fill_fraction_per_bar=Decimal("1"),
        ),
        risk=RiskConfig(max_notional_multiple=Decimal("3")),
    )

    result = BacktestEngine(_OneTrade(), config).run(_bars())

    assert result.bars_processed == 2
    assert result.journal.fills
    assert all(isinstance(fill.fee, float) for fill in result.journal.fills)
    assert isinstance(result.metrics.final_equity, float)
