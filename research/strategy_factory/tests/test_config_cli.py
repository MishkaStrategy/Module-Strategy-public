from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from strategy_factory.cli import main
from strategy_factory.config import load_request
from strategy_factory.contracts import FactoryMode
from strategy_factory.errors import ContractError


def config_mapping(descriptor, budget) -> dict:
    return {
        "schema_version": 1,
        "request_id": "request-cli",
        "mode": "create",
        "symbol": descriptor.symbol,
        "exchange": descriptor.exchange,
        "market_type": descriptor.market_type.value,
        "dataset": {
            "dataset_id": descriptor.dataset_id,
            "source": descriptor.source,
            "symbol": descriptor.symbol,
            "exchange": descriptor.exchange,
            "market_type": descriptor.market_type.value,
            "base_timeframe": descriptor.base_timeframe,
            "available_timeframes": list(descriptor.available_timeframes),
            "period": {
                "start": descriptor.period.start.isoformat(),
                "end": descriptor.period.end.isoformat(),
            },
            "timezone": "UTC",
            "file_format": "csv",
            "uri": descriptor.uri,
            "sha256": descriptor.sha256,
            "row_count": descriptor.row_count,
            "received_at": descriptor.received_at.isoformat(),
        },
        "allowed_timeframes": ["5m", "15m"],
        "initial_capital": "10000",
        "research_risk_fraction": "0.005",
        "maximum_drawdown_fraction": "0.15",
        "minimum_trade_count": 30,
        "objectives": {"profit_factor": "maximize"},
        "budget": {
            "maximum_experiments": budget.maximum_experiments,
            "maximum_architectures": budget.maximum_architectures,
            "maximum_finalists": budget.maximum_finalists,
            "maximum_duration_seconds": budget.maximum_duration_seconds,
            "maximum_memory_bytes": budget.maximum_memory_bytes,
            "maximum_cpu_workers": budget.maximum_cpu_workers,
            "maximum_parallel_tasks": budget.maximum_parallel_tasks,
            "stage_experiment_limits": dict(budget.stage_experiment_limits),
        },
        "random_seed": 42,
        "allowed_strategy_families": ["fvg"],
        "forbidden_module_ids": [],
        "created_at": "2026-01-02T00:00:00+00:00",
    }


def test_load_request_round_trip(descriptor, budget, tmp_path: Path) -> None:
    path = tmp_path / "factory.yaml"
    path.write_text(yaml.safe_dump(config_mapping(descriptor, budget)), encoding="utf-8")
    loaded = load_request(path, forced_mode=FactoryMode.CREATE)
    assert loaded.dataset.sha256 == descriptor.sha256
    assert loaded.budget.maximum_experiments == budget.maximum_experiments


def test_config_rejects_unknown_key(descriptor, budget, tmp_path: Path) -> None:
    value = config_mapping(descriptor, budget)
    value["surprise"] = True
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown root keys"):
        load_request(path)


def test_cli_create_status_inspect_and_export(descriptor, budget, tmp_path: Path, capsys) -> None:
    config = tmp_path / "factory.yaml"
    config.write_text(yaml.safe_dump(config_mapping(descriptor, budget)), encoding="utf-8")
    state = tmp_path / "state"
    exit_code = main(
        [
            "--state-root",
            str(state),
            "create",
            "--config",
            str(config),
            "--commit-sha",
            "e" * 40,
            "--interval-seconds",
            "300",
            "--minimum-rows",
            "10",
            "--minimum-history-days",
            "0",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    run_id = output["run_id"]
    assert main(["--state-root", str(state), "status", "--run-id", run_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["stage"] == "regime_discovery"
    assert main(["--state-root", str(state), "inspect", "--run-id", run_id]) == 0
    inspected = json.loads(capsys.readouterr().out)
    artifact_id = inspected["checkpoint"]["completed_artifacts"]["data_gate"]
    target = tmp_path / "exported.json"
    assert (
        main(
            [
                "--state-root",
                str(state),
                "export",
                "--run-id",
                run_id,
                "--artifact-id",
                artifact_id,
                "--output",
                str(target),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert target.is_file()


def test_cli_doctor_reports_missing_strategy_audit(tmp_path: Path, capsys) -> None:
    code = main(["--state-root", str(tmp_path), "doctor"])
    output = json.loads(capsys.readouterr().out)
    assert output["strategy_audit"]["available"] is False
    assert code in {0, 2}


def test_cli_uses_injected_full_application(descriptor, budget, tmp_path: Path, capsys) -> None:
    from test_application import Assembly, _non_data_stages

    from strategy_factory.application import StrategyFactoryApplication

    config = tmp_path / "factory-full.yaml"
    config.write_text(
        yaml.safe_dump(config_mapping(descriptor, budget)),
        encoding="utf-8",
    )
    state = tmp_path / "full-state"
    application = StrategyFactoryApplication(
        state_root=state,
        assembly=Assembly(_non_data_stages()),
    )

    code = main(
        [
            "--state-root",
            str(state),
            "create",
            "--config",
            str(config),
            "--commit-sha",
            "e" * 40,
            "--interval-seconds",
            "300",
            "--minimum-rows",
            "10",
            "--minimum-history-days",
            "0",
            "--module-version",
            "fvg=1.0.0",
        ],
        application=application,
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "COMPLETED"
    assert output["completed"] is True
    assert output["steps"][-1]["stage"] == "paper_admission"
