from __future__ import annotations

import hashlib
import os
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.scalability import PerformanceConfig
from modular_core.config import DataConfig, EngineConfig, ExecutionConfig, RiskConfig

from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_CONTRACT,
    PreparedBacktestConfiguration,
    canonical_json_bytes,
    verify_actual_backtest_configuration,
)


def prepared_effective_config(
    tmp_path: Path,
    *,
    run_id: str = "run",
    engine_run_id: str | None = None,
    context_overrides: dict[str, object] | None = None,
):
    start = "2026-01-01T00:00:00.000000Z"
    end = "2026-01-02T00:00:00.000000Z"
    rendered_modules = {
        "modules": [{"class": "pkg:Noop", "enabled": True, "parameters": {"period": 2}}]
    }
    module_identity = {
        "core_manifest_name": "noop-core",
        "runtime_component_id": "noop",
        "runtime_component_version": "1.0.0",
        "import_path": "pkg:Noop",
        "api_version": "1.0",
        "state_schema_version": "1",
        "source_commit_sha": "c" * 40,
        "parameter_schema": {"period": {"type": "integer", "minimum": 1}},
        "parameters": {"period": 2},
    }
    configuration = {
        "engine_contract_profile": "official-extended-registered.v1",
        "app": {
            "initial_capital": 1000.0,
            "requested_timeframes": ["15m"],
            "max_base_history": None,
            "max_timeframe_history": None,
            "seed": 7,
            "checkpoint": {"mode": "disabled", "restart_at_bar": None},
            "end_of_test_policy": "force_close",
            "output_directory_sha256": "c" * 64,
        },
        "data_source": {
            "source_kind": "registered",
            "path_sha256": None,
            "storage_root_sha256": "d" * 64,
            "symbol": "BTCUSDT",
            "format": None,
            "market_type": "binance-usdt-m-perpetual",
            "base_timeframe": "5m",
            "gap_policy": "reject",
            "timestamp_column": "open_time",
            "start": start,
            "end": end,
            "columns": {
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
            "dataset_id": "dataset-001",
            "dataset_sha256": "a" * 64,
            "dataset_version": 1,
            "registered_mode": "streaming",
            "stream_batch_size": 8192,
        },
        "engine": {
            "initial_cash": 1000.0,
            "one_active_order": True,
            "one_position": True,
            "allow_scale_in": False,
            "close_open_position_at_end": True,
            "strict_data_validation": True,
            "strict_module_errors": True,
            "deterministic_order_ids": True,
            "strict_feature_contracts": True,
            "strict_setup_transitions": True,
            "fees": {"maker_rate": 0.0002, "taker_rate": 0.0005},
            "execution": {
                "slippage_bps": 2.5,
                "stop_slippage_bps": 3.0,
                "intrabar_priority": "stop",
                "limit_fill_policy": "at_limit",
                "allow_exit_on_entry_bar": False,
                "allow_limit_stop_on_entry_bar": False,
                "allow_reentry_on_exit_bar": False,
                "close_request_timing": "next_open",
                "target_liquidity": "maker",
                "max_fill_fraction_per_bar": 1.0,
                "max_fill_quantity_per_bar": None,
                "stop_limit_same_bar_policy": "defer",
            },
            "risk": {
                "default_risk_fraction": 0.005,
                "max_risk_fraction": 0.05,
                "max_notional_multiple": 3.0,
                "min_notional": 0.0,
                "min_quantity": 0.0,
                "quantity_step": 0.0,
                "min_stop_distance_pct": 0.0,
                "max_stop_distance_pct": None,
                "sizing_model": "stop_distance",
            },
            "data": {
                "expected_interval_seconds": 300,
                "gap_policy": "reject",
                "timestamp_semantics": "open",
                "max_base_history": None,
            },
        },
        "instrument": {
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "market_type": "linear_futures",
            "tick_size": 0.1,
            "quantity_step": 0.001,
            "min_quantity": 0.001,
            "max_quantity": None,
            "min_notional": 5.0,
            "max_notional": None,
            "contract_size": 1.0,
            "price_precision": None,
            "quantity_precision": None,
        },
        "module_identity": {
            "configuration_sha256": hashlib.sha256(
                canonical_json_bytes(rendered_modules)
            ).hexdigest(),
            "configuration_path_sha256": "e" * 64,
            "configuration": {
                "modules": [module_identity],
                "rendered": rendered_modules,
            },
            "components": ["noop"],
        },
    }
    context = {
        "run_id": run_id,
        "experiment_id": "experiment-001",
        "stage_id": "stage-001",
        "stage_role": "validation",
        "split_id": "split-001",
        "dataset_id": "dataset-001",
        "dataset_sha256": "a" * 64,
        "scenario_id": "base",
        "seed": 7,
        "repetition_index": 0,
        "period_start": start,
        "period_end": end,
        "configuration_sha256": "b" * 64,
        "execution_performance_mode": "canonical_audit",
    }
    if context_overrides is not None:
        context.update(context_overrides)
        configuration["app"]["seed"] = context["seed"]
        source = configuration["data_source"]
        source["dataset_id"] = context["dataset_id"]
        source["dataset_sha256"] = context["dataset_sha256"]
        source["start"] = context["period_start"]
        source["end"] = context["period_end"]
    performance_mode = context["execution_performance_mode"]
    if performance_mode not in {"canonical_audit", "research_fast"}:
        performance_mode = "canonical_audit"
    performance = PerformanceConfig(
        mode=performance_mode,
        max_base_history=None,
        max_timeframe_history=None,
    )
    configuration["app"]["performance"] = {
        "schema_id": "modular-backtest.performance-config",
        "schema_version": "1",
        "values": {item.name: getattr(performance, item.name) for item in fields(performance)},
    }
    if engine_run_id is not None:
        context["engine_run_id"] = engine_run_id
    run_root = tmp_path / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    modules_path = run_root / "modules.yaml"
    modules_path.write_bytes(canonical_json_bytes(rendered_modules))
    output_directory = run_root / "engine-results"
    storage_root = tmp_path / "registered-data-root"
    file_only = "storage_root" not in {item.name for item in fields(DataSourceConfig)}
    if file_only:
        configuration["engine_contract_profile"] = "official-file-only.v1"
        source = configuration["data_source"]
        source.update(
            source_kind="file",
            path_sha256=hashlib.sha256(
                os.fsencode(str(run_root / "synthetic-bars.csv"))
            ).hexdigest(),
            storage_root_sha256=None,
            format="csv",
            dataset_version=None,
            registered_mode=None,
            stream_batch_size=None,
        )
        for key, config_type in (
            ("execution", ExecutionConfig),
            ("risk", RiskConfig),
            ("data", DataConfig),
        ):
            actual_fields = {item.name for item in fields(config_type)}
            configuration["engine"][key] = {
                name: value
                for name, value in configuration["engine"][key].items()
                if name in actual_fields
            }
        engine_fields = {item.name for item in fields(EngineConfig)}
        configuration["engine"] = {
            name: value for name, value in configuration["engine"].items() if name in engine_fields
        }
    configuration["app"]["output_directory_sha256"] = hashlib.sha256(
        os.fsencode(str(output_directory))
    ).hexdigest()
    if not file_only:
        configuration["data_source"]["storage_root_sha256"] = hashlib.sha256(
            os.fsencode(str(storage_root))
        ).hexdigest()
    configuration["module_identity"]["configuration_path_sha256"] = hashlib.sha256(
        os.fsencode(str(modules_path))
    ).hexdigest()
    start_value = datetime.fromisoformat(context["period_start"].replace("Z", "+00:00"))
    end_value = datetime.fromisoformat(context["period_end"].replace("Z", "+00:00"))
    data_values = {
        "symbol": configuration["data_source"]["symbol"],
        "format": configuration["data_source"]["format"],
        "market_type": configuration["data_source"]["market_type"],
        "base_timeframe": configuration["data_source"]["base_timeframe"],
        "gap_policy": configuration["data_source"]["gap_policy"],
        "timestamp_column": configuration["data_source"]["timestamp_column"],
        "start": start_value,
        "end": end_value,
        "columns": configuration["data_source"]["columns"],
    }
    if file_only:
        data_values["path"] = run_root / "synthetic-bars.csv"
    else:
        data_values.update(
            storage_root=storage_root,
            dataset_id=configuration["data_source"]["dataset_id"],
            dataset_version=configuration["data_source"]["dataset_version"],
            registered_mode=configuration["data_source"]["registered_mode"],
            stream_batch_size=configuration["data_source"]["stream_batch_size"],
        )
    config = BacktestAppConfig(
        data=DataSourceConfig(**data_values),
        modules=modules_path,
        initial_capital=configuration["app"]["initial_capital"],
        requested_timeframes=tuple(configuration["app"]["requested_timeframes"]),
        execution=configuration["engine"]["execution"],
        fees=configuration["engine"]["fees"],
        risk=configuration["engine"]["risk"],
        instrument={
            key: value for key, value in configuration["instrument"].items() if key != "symbol"
        },
        output=OutputConfig(output_directory),
        seed=configuration["app"]["seed"],
        checkpoint=CheckpointConfig(**configuration["app"]["checkpoint"]),
        end_of_test_policy=configuration["app"]["end_of_test_policy"],
    )
    prepared = PreparedBacktestConfiguration(
        contract=EFFECTIVE_CONFIG_CONTRACT,
        config=config,
        context=context,
        effective_configuration=configuration,
        performance_config=performance,
    )
    verify_actual_backtest_configuration(prepared)
    return prepared
