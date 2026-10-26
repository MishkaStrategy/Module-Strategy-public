from __future__ import annotations

import csv
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_factory.contracts import (
    DatasetDescriptor,
    Direction,
    FactoryMode,
    FactoryStage,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange,
)


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sample.csv"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["open_time", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        price = Decimal("100")
        for index in range(20):
            open_ = price
            close = price + Decimal("0.5")
            writer.writerow(
                {
                    "open_time": int((start + timedelta(minutes=5 * index)).timestamp() * 1000),
                    "open": str(open_),
                    "high": str(close + Decimal("0.25")),
                    "low": str(open_ - Decimal("0.25")),
                    "close": str(close),
                    "volume": str(1000 + index),
                }
            )
            price = close
    return path


@pytest.fixture
def descriptor(sample_csv: Path) -> DatasetDescriptor:
    raw = sample_csv.read_bytes()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return DatasetDescriptor(
        dataset_id="dataset-ethusdt-5m",
        source="fixture",
        symbol="ETHUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m", "1h"),
        period=TimeRange(start, start + timedelta(minutes=5 * 20)),
        timezone="UTC",
        file_format="csv",
        uri=str(sample_csv),
        sha256=hashlib.sha256(raw).hexdigest(),
        row_count=20,
        received_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


@pytest.fixture
def budget() -> ResourceBudget:
    return ResourceBudget(
        maximum_experiments=100,
        maximum_architectures=20,
        maximum_finalists=3,
        maximum_duration_seconds=3600,
        maximum_memory_bytes=1_000_000_000,
        maximum_cpu_workers=4,
        maximum_parallel_tasks=2,
        stage_experiment_limits={
            FactoryStage.STRUCTURAL_SCREENING: 30,
            FactoryStage.COARSE_SEARCH: 40,
            FactoryStage.LOCAL_REFINEMENT: 30,
        },
    )


@pytest.fixture
def factory_request(
    descriptor: DatasetDescriptor, budget: ResourceBudget
) -> StrategyFactoryRequest:
    return StrategyFactoryRequest(
        request_id="request-eth-create",
        mode=FactoryMode.CREATE,
        symbol="ETHUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        dataset=descriptor,
        allowed_timeframes=("5m", "15m"),
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.005"),
        maximum_drawdown_fraction=Decimal("0.15"),
        minimum_trade_count=30,
        objectives={"profit_factor": "maximize", "max_drawdown": "minimize"},
        budget=budget,
        random_seed=42,
        allowed_strategy_families=frozenset({"fvg", "breakout"}),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def module(
    module_id: str,
    category: ModuleCategory,
    *,
    required_data: frozenset[str] = frozenset(),
    required_features: tuple[str, ...] = (),
    produced_features: tuple[str, ...] = (),
    conflict_tags: frozenset[str] = frozenset(),
    compatibility_tags: frozenset[str] = frozenset(),
    maturity: ModuleMaturity = ModuleMaturity.PAPER_READY,
    coverage: str = "95",
    import_path: str | None = None,
    runtime_component_id: str | None = None,
    runtime_component_version: str | None = None,
    runtime_role_categories: frozenset[ModuleCategory] = frozenset(),
    role_facet: str | None = None,
    parameter_schema: dict[str, object] | None = None,
    parameter_search_hints: dict[str, dict[str, object]] | None = None,
    fixed_parameters: dict[str, object] | None = None,
) -> ModuleReference:
    return ModuleReference(
        module_id=module_id,
        version="1.0.0",
        category=category,
        import_path=import_path or f"fixture:{module_id}",
        api_version="1.0",
        state_schema_version="1",
        required_timeframes=("5m",),
        required_features=required_features,
        produced_features=produced_features,
        required_data=required_data,
        supported_markets=frozenset({MarketType.LINEAR_FUTURES}),
        supported_directions=frozenset({Direction.LONG, Direction.SHORT}),
        deterministic=True,
        test_coverage=Decimal(coverage),
        maturity=maturity,
        conflict_tags=conflict_tags,
        compatibility_tags=compatibility_tags,
        parameter_schema={} if parameter_schema is None else parameter_schema,
        parameter_search_hints=({} if parameter_search_hints is None else parameter_search_hints),
        fixed_parameters={} if fixed_parameters is None else fixed_parameters,
        runtime_component_id=runtime_component_id,
        runtime_component_version=runtime_component_version,
        runtime_role_categories=runtime_role_categories,
        role_facet=role_facet,
    )


@pytest.fixture
def architecture(descriptor):
    from strategy_factory.contracts import StrategyArchitecture

    signal = module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",))
    entry = module("limit_retest", ModuleCategory.ENTRY, required_features=("setup",))
    protection = module("atr_stop", ModuleCategory.PROTECTION)
    target = module(
        "fixed_r",
        ModuleCategory.TARGET,
        parameter_schema={"reward_r": {"minimum": 1, "maximum": 10}},
    )
    risk = module(
        "fixed_fractional",
        ModuleCategory.RISK,
        parameter_schema={"risk_fraction": {"minimum": "0.0001", "maximum": "0.02"}},
    )
    return StrategyArchitecture(
        architecture_id="arch-fixture",
        market_type=descriptor.market_type,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        signal=signal,
        context_filters=(),
        entry=entry,
        protection=protection,
        target=target,
        risk=risk,
    )
