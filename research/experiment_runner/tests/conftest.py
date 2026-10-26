from __future__ import annotations

import sys
from datetime import UTC, datetime
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any

import pytest

_RESULTS_REGISTRY_SRC = (
    Path(__file__).resolve().parents[3] / "modules" / "results_aggregator" / "src"
)
if str(_RESULTS_REGISTRY_SRC) not in sys.path:
    sys.path.insert(0, str(_RESULTS_REGISTRY_SRC))

from modular_research_runner import (  # noqa: E402
    ChronologicalSplitPolicy,
    DatasetLineage,
    ParameterDimension,
    ParameterKind,
    ResearchSpecification,
    SearchMethod,
    SearchSpace,
    SplitMode,
    TimeRange,
)


@pytest.fixture(autouse=True)
def _adapt_authoritative_backtest_writer_contract(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge only known neutral Backtest writer additions in the integration test."""
    if request.node.name != "test_registry_package_accepts_authoritative_backtest_engine_output":
        return

    try:
        from modular_backtest import runner as backtest_runner
    except ModuleNotFoundError:
        return

    original = backtest_runner._write_result_files
    parameters = signature(original).parameters
    if "application_id" not in parameters and "funding_source" not in parameters:
        return

    @wraps(original)
    def compatible_write_result_files(*args: Any, **kwargs: Any) -> Any:
        if "application_id" in parameters and "application_id" not in kwargs:
            result = kwargs.get("result")
            if result is None:
                raise TypeError("authoritative Backtest writer compatibility requires result")
            kwargs["application_id"] = result.run_manifest.run_id
        if "funding_source" in parameters:
            kwargs.setdefault("funding_source", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(backtest_runner, "_write_result_files", compatible_write_result_files)


@pytest.fixture
def overall_range() -> TimeRange:
    return TimeRange(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def dataset(overall_range: TimeRange) -> DatasetLineage:
    return DatasetLineage(
        dataset_id="binance-btcusdt-5m-v1",
        source="binance",
        market_type="binance-usdt-m-perpetual",
        instrument="BTCUSDT",
        timeframe="5m",
        period=overall_range,
        row_count=420_000,
        timezone="UTC",
        file_format="csv",
        file_size=64_000_000,
        sha256="a" * 64,
        preprocessing_version="raw-v1",
        aggregation_version="utc-v1",
        metadata_version="v1",
    )


@pytest.fixture
def split_policy() -> ChronologicalSplitPolicy:
    return ChronologicalSplitPolicy(
        mode=SplitMode.FIXED,
        train=TimeRange(
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=UTC),
        ),
        validation=TimeRange(
            datetime(2022, 1, 2, tzinfo=UTC),
            datetime(2023, 1, 1, tzinfo=UTC),
        ),
        test=TimeRange(
            datetime(2023, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        ),
        gap_seconds=86_400,
    )


@pytest.fixture
def search_space() -> SearchSpace:
    return SearchSpace(
        dimensions=(
            ParameterDimension("ttl", ParameterKind.INTEGER, (24, 36)),
            ParameterDimension("side", ParameterKind.CATEGORICAL, ("long", "short")),
        ),
        maximum_combinations=16,
    )


@pytest.fixture
def specification(
    dataset: DatasetLineage,
    overall_range: TimeRange,
    split_policy: ChronologicalSplitPolicy,
    search_space: SearchSpace,
) -> ResearchSpecification:
    return ResearchSpecification(
        research_id="btc-fvg-study-v1",
        title="BTC FVG study",
        description="Deterministic research specification used by unit tests.",
        strategy_id="fvg-rt-final",
        strategy_version="1.0.0",
        module_versions={"fvg": "0.2.1"},
        base_configuration={"risk_fraction": "0.005"},
        search_method=SearchMethod.GRID,
        search_space=search_space,
        datasets=(dataset,),
        instruments=("BTCUSDT",),
        market_type="binance-usdt-m-perpetual",
        timeframes=("5m", "15m", "4h", "1d"),
        date_range=overall_range,
        split_policy=split_policy,
        seeds=(11, 22),
        resource_limits={"maximum_workers": 4},
        parallelism=4,
        ranking_objective={"metric": "profit_factor", "direction": "maximize"},
        promotion_criteria={"minimum_trades": 100},
    )


@pytest.fixture
def specification_mapping() -> dict[str, Any]:
    return {
        "research_id": "btc-fvg-study-v1",
        "title": "BTC FVG study",
        "description": "Loaded from strict YAML.",
        "strategy_id": "fvg-rt-final",
        "strategy_version": "1.0.0",
        "module_versions": {"fvg": "0.2.1"},
        "base_configuration": {"risk_fraction": "0.005"},
        "search_method": "grid",
        "search_space": {
            "dimensions": [
                {"name": "ttl", "kind": "integer", "values": [24, 36]},
                {"name": "threshold", "kind": "decimal", "values": ["0.45", "0.50"]},
            ],
            "constraints": [],
            "maximum_combinations": 16,
        },
        "datasets": [
            {
                "dataset_id": "binance-btcusdt-5m-v1",
                "source": "binance",
                "market_type": "binance-usdt-m-perpetual",
                "instrument": "BTCUSDT",
                "timeframe": "5m",
                "period": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"},
                "row_count": 420000,
                "timezone": "UTC",
                "file_format": "csv",
                "file_size": 64000000,
                "sha256": "a" * 64,
                "preprocessing_version": "raw-v1",
                "aggregation_version": "utc-v1",
                "metadata_version": "v1",
                "quality_status": "valid",
            }
        ],
        "instruments": ["BTCUSDT"],
        "market_type": "binance-usdt-m-perpetual",
        "timeframes": ["5m", "15m", "4h", "1d"],
        "date_range": {"start": "2020-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z"},
        "split_policy": {
            "mode": "percentage",
            "train_fraction": 0.6,
            "validation_fraction": 0.2,
            "test_fraction": 0.2,
            "gap_seconds": 300,
        },
        "seeds": [11, 22],
        "resource_limits": {"maximum_workers": 4},
        "parallelism": 4,
        "schema_version": 1,
    }
