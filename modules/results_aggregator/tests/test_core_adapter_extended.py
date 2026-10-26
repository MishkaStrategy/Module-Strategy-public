from __future__ import annotations

import hashlib
import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import NOW, make_package
from modular_results_registry.artifact_store import FileLimits
from modular_results_registry.core_adapter import CoreRunContext, adapt_core_v04_export
from modular_results_registry.core_adapter_contract import _METRIC_CONTRACT
from modular_results_registry.errors import (
    IntegrityError,
    ResourceLimitError,
    UnsupportedSchemaError,
    ValidationError,
)
from modular_results_registry.models import (
    CandidateRole,
    ConfigurationRecord,
    RunStatus,
)


def _context(**changes: object) -> CoreRunContext:
    package = make_package()
    values: dict[str, object] = {
        "experiment": package.experiments[0],
        "configuration": package.configurations[0],
        "datasets": package.datasets,
        "stage_id": "stage-core",
        "split_id": "split-core",
        "scenario_id": "scenario-core",
        "seed": 7,
        "worker_id": "worker-core",
        "machine_id": "machine-core",
        "run_status": RunStatus.COMPLETED,
        "observed_completed_at": NOW,
        "duration_seconds": 1.0,
        "role": CandidateRole.CANDIDATE,
        "role_reason": "adapter candidate",
        "metric_split": "validation",
        "metric_period": "2026-01-01T00:00:00.000000Z/2026-01-02T00:00:00.000000Z",
        "metric_scenario": "scenario-core",
    }
    values.update(changes)
    return CoreRunContext(**values)  # type: ignore[arg-type]


def _write_core_export(root: Path, **manifest_changes: object) -> dict[str, object]:
    root.mkdir()
    manifest: dict[str, object] = {
        "run_id": "core-run-001",
        "kernel_version": "0.4.0",
        "data_hash": "a" * 64,
        "config_hash": "b" * 64,
        "module_hash": "c" * 64,
        "symbol": "BTCUSDT",
        "base_timeframe": "5m",
        "bars": 100,
        "module_manifests": [
            {
                "name": "strategy",
                "version": "1.0.0",
                "api_version": "1.0",
                "state_schema_version": "1",
                "parameters_schema": {"period": {"type": "integer"}},
            }
        ],
        "module_parameters": [{"period": 2}],
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    manifest.update(manifest_changes)
    run = {"manifest": manifest, "bars_processed": 100, "modules": ["strategy"]}
    metrics = {name: 1 for name in _METRIC_CONTRACT}
    (root / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (root / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (root / "journal.json").write_text('{"errors":[]}', encoding="utf-8")
    (root / "trades.csv").write_text("id,pnl\n", encoding="utf-8")
    (root / "equity.csv").write_text("time,equity\n", encoding="utf-8")
    return run


def _rewrite(path: Path, mutate) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_effective_module_binding_uses_loaded_core_names_not_order() -> None:
    from modular_results_registry.core_adapter_import import (
        _validate_effective_module_parameters,
    )

    schema = {"period": {"type": "integer"}}

    def configured(name: str, parameters: dict[str, int]) -> dict[str, object]:
        return {
            "core_manifest_name": name,
            "runtime_component_version": "1.0.0",
            "api_version": "1.0",
            "state_schema_version": "1",
            "parameter_schema": schema,
            "parameters": parameters,
        }

    effective = {
        "module_identity": {
            "configuration": {
                "modules": [
                    configured("core-fast", {"period": 3}),
                    configured("core-slow", {"period": 5}),
                ]
            }
        }
    }
    manifests = [
        {
            "name": "core-slow",
            "version": "1.0.0",
            "api_version": "1.0",
            "state_schema_version": "1",
            "parameters_schema": schema,
        },
        {
            "name": "core-fast",
            "version": "1.0.0",
            "api_version": "1.0",
            "state_schema_version": "1",
            "parameters_schema": schema,
        },
    ]
    _validate_effective_module_parameters(
        effective,
        module_manifests=manifests,
        module_parameters=[{"period": 5}, {"period": 3}],
    )

    duplicate_effective = {
        "module_identity": {
            "configuration": {
                "modules": [
                    configured("core-fast", {"period": 3}),
                    configured("core-fast", {"period": 5}),
                ]
            }
        }
    }
    with pytest.raises(IntegrityError, match="manifest names contain duplicates"):
        _validate_effective_module_parameters(
            duplicate_effective,
            module_manifests=manifests,
            module_parameters=[{"period": 5}, {"period": 3}],
        )

    missing_name = {"module_identity": {"configuration": {"modules": [{"parameters": {}}]}}}
    with pytest.raises(IntegrityError, match="core_manifest_name is missing"):
        _validate_effective_module_parameters(
            missing_name,
            module_manifests=manifests[:1],
            module_parameters=[{"period": 5}],
        )


def test_core_adapter_builds_complete_deterministic_package(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    (export / "events.jsonl").write_text('{"event":"done"}\n', encoding="utf-8")
    first = adapt_core_v04_export(export, _context())
    second = adapt_core_v04_export(export, _context())
    assert first.package == second.package
    assert len(first.package.artifacts) == 6
    assert len(first.package.metrics) == len(_METRIC_CONTRACT)
    assert len(first.package.metric_definitions) == len(_METRIC_CONTRACT)
    assert first.package.runs[0].bars_processed == 100
    assert first.package.evidence[0].status.value == "PASS"
    assert all(
        item.relative_path != "effective-backtest-config.json" for item in first.package.artifacts
    )
    assert tuple(first.source_inventory) == (
        "run.json",
        "metrics.json",
        "journal.json",
        "trades.csv",
        "equity.csv",
        "events.jsonl",
    )


def test_core_adapter_registers_context_bound_effective_config_artifact(tmp_path: Path) -> None:
    context = _context()
    export = tmp_path / "effective-export"
    _write_core_export(export)
    _rewrite(export / "metrics.json", lambda metrics: metrics.update(initial_equity=1000.0))
    document: dict[str, object] = {
        "schema_id": "modular-backtest.effective-backtest-config",
        "schema_version": "3",
        "context": {
            "run_id": "runner-run-001",
            "engine_run_id": "core-run-001",
            "experiment_id": context.experiment.experiment_id,
            "stage_id": context.stage_id,
            "stage_role": "validation",
            "split_id": context.split_id,
            "dataset_id": context.datasets[0].dataset_id,
            "dataset_sha256": context.datasets[0].sha256,
            "scenario_id": context.scenario_id,
            "seed": context.seed,
            "repetition_index": 0,
            "period_start": "2026-01-01T00:00:00.000000Z",
            "period_end": "2026-01-02T00:00:00.000000Z",
            "configuration_sha256": "b" * 64,
            "execution_performance_mode": "canonical_audit",
        },
        "effective_configuration": {
            "engine_contract_profile": "official-extended-registered.v1",
            "app": {
                "initial_capital": 1000.0,
                "requested_timeframes": ["15m"],
                "max_base_history": 65536,
                "max_timeframe_history": 4096,
                "performance": {
                    "schema_id": "modular-backtest.performance-config",
                    "schema_version": "1",
                    "values": {
                        "mode": "canonical_audit",
                        "max_memory_mb": 9216,
                        "max_events": 2000000,
                        "max_skip_ranges": 250000,
                        "max_diagnostics": 250000,
                        "max_equity_points": 5000000,
                        "progress_interval_bars": 10000,
                        "retained_event_window": 2000,
                        "equity_sampling": "every_bar",
                        "equity_every_n_bars": 12,
                        "max_base_history": 65536,
                        "max_timeframe_history": 4096,
                        "disable_cyclic_gc_in_hot_loop": False,
                    },
                },
                "seed": context.seed,
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
                "start": "2026-01-01T00:00:00.000000Z",
                "end": "2026-01-02T00:00:00.000000Z",
                "columns": {
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                },
                "dataset_id": context.datasets[0].dataset_id,
                "dataset_sha256": context.datasets[0].sha256,
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
                    "max_base_history": 65536,
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
                "configuration_sha256": "",
                "configuration_path_sha256": "e" * 64,
                "configuration": {
                    "modules": [
                        {
                            "core_manifest_name": "strategy",
                            "runtime_component_id": "noop",
                            "runtime_component_version": "1.0.0",
                            "import_path": "pkg:Noop",
                            "api_version": "1.0",
                            "state_schema_version": "1",
                            "source_commit_sha": "c" * 40,
                            "parameter_schema": {"period": {"type": "integer"}},
                            "parameters": {"period": 2},
                        }
                    ],
                    "rendered": {
                        "modules": [
                            {"class": "pkg:Noop", "enabled": True, "parameters": {"period": 2}}
                        ]
                    },
                },
                "components": ["noop"],
            },
        },
    }

    rendered = document["effective_configuration"]["module_identity"]["configuration"]["rendered"]
    rendered_payload = json.dumps(
        rendered,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    document["effective_configuration"]["module_identity"]["configuration_sha256"] = hashlib.sha256(
        rendered_payload
    ).hexdigest()

    def seal(*, bind_config_hash: bool = True) -> None:
        effective_value = document.get("effective_configuration")
        app_value = effective_value.get("app") if isinstance(effective_value, dict) else None
        perf_value = app_value.get("performance") if isinstance(app_value, dict) else None
        perf_values = perf_value.get("values") if isinstance(perf_value, dict) else None
        producer_performance = copy.deepcopy(perf_values) if isinstance(perf_values, dict) else {}
        if isinstance(app_value, dict):
            for key in ("max_base_history", "max_timeframe_history"):
                if key in app_value:
                    producer_performance[key] = app_value[key]
        (export / "application-run.json").write_text(
            json.dumps(
                {
                    "core_run_id": "core-run-001",
                    "performance": producer_performance,
                }
            ),
            encoding="utf-8",
        )
        body = {key: value for key, value in document.items() if key != "projection_sha256"}
        payload = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        document["projection_sha256"] = hashlib.sha256(payload).hexdigest()
        if bind_config_hash:
            effective = document.get("effective_configuration")
            engine = copy.deepcopy(effective.get("engine")) if isinstance(effective, dict) else None
            if not isinstance(engine, dict):
                return
            execution = engine["execution"]
            risk = engine["risk"]
            if not isinstance(execution, dict) or not isinstance(risk, dict):
                return
            if execution.get("allow_limit_stop_on_entry_bar") is False:
                execution.pop("allow_limit_stop_on_entry_bar")
            if execution.get("close_request_timing") == "next_open":
                execution.pop("close_request_timing")
            if risk.get("sizing_model") == "stop_distance":
                risk.pop("sizing_model")
            core_config = json.dumps(
                engine,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            run_path = export / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["manifest"]["config_hash"] = hashlib.sha256(core_config).hexdigest()
            run_path.write_text(json.dumps(run), encoding="utf-8")

    seal()
    proof = export / "effective-backtest-config.json"
    proof.write_text(json.dumps(document), encoding="utf-8")
    imported = adapt_core_v04_export(export, context)
    artifact = next(
        item
        for item in imported.package.artifacts
        if item.relative_path == "effective-backtest-config.json"
    )
    assert artifact.artifact_id in imported.package.runs[0].artifact_ids
    assert artifact.sha256 == hashlib.sha256(proof.read_bytes()).hexdigest()
    assert artifact.schema_id == "modular-backtest.effective-backtest-config"
    assert artifact.schema_version == "3"
    producer_path = export / "application-run.json"
    producer_run = json.loads(producer_path.read_text(encoding="utf-8"))
    producer_run["performance"]["max_timeframe_history"] = 512
    producer_path.write_text(json.dumps(producer_run), encoding="utf-8")
    with pytest.raises(IntegrityError, match="does not match producer evidence"):
        adapt_core_v04_export(export, context)
    seal()

    _rewrite(export / "run.json", lambda run: run["manifest"].pop("module_parameters"))
    with pytest.raises(IntegrityError, match="module_parameters are missing or incomplete"):
        adapt_core_v04_export(export, context)
    _rewrite(
        export / "run.json",
        lambda run: run["manifest"].update(module_parameters=[{"period": 2}]),
    )

    resealed_sidecar = copy.deepcopy(document)
    effective = resealed_sidecar["effective_configuration"]
    effective["data_source"]["symbol"] = "ETHUSDT"
    effective["instrument"]["symbol"] = "ETHUSDT"
    body = {key: value for key, value in resealed_sidecar.items() if key != "projection_sha256"}
    resealed_sidecar["projection_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof.write_text(json.dumps(resealed_sidecar), encoding="utf-8")
    with pytest.raises(IntegrityError, match="instrument disagrees with Core manifest"):
        adapt_core_v04_export(export, context)

    resealed_sidecar = copy.deepcopy(document)
    effective = resealed_sidecar["effective_configuration"]
    effective["app"]["initial_capital"] = 2000.0
    effective["engine"]["initial_cash"] = 2000.0
    body = {key: value for key, value in resealed_sidecar.items() if key != "projection_sha256"}
    resealed_sidecar["projection_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof.write_text(json.dumps(resealed_sidecar), encoding="utf-8")
    with pytest.raises(IntegrityError, match="capital disagrees with Core metrics initial equity"):
        adapt_core_v04_export(export, context)

    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")

    extended_document = copy.deepcopy(document)
    module_identity = document["effective_configuration"]["module_identity"]
    identity = module_identity["configuration"]
    nested_parameters = {"period": 1, "tuning": {"steps": [1, 2], "enabled": True}}
    _rewrite(
        export / "run.json",
        lambda run: run["manifest"].update(
            module_parameters=[nested_parameters],
            module_manifests=[
                {
                    **run["manifest"]["module_manifests"][0],
                    "parameters_schema": {
                        "period": {"type": "integer"},
                        "tuning": {"type": "object"},
                    },
                }
            ],
        ),
    )
    identity["modules"][0]["parameters"] = nested_parameters
    identity["modules"][0]["parameter_schema"] = {
        "period": {"type": "integer"},
        "tuning": {"type": "object"},
    }
    identity["rendered"]["modules"][0]["parameters"] = nested_parameters
    module_identity["configuration_sha256"] = hashlib.sha256(
        json.dumps(
            identity["rendered"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    nested_import = adapt_core_v04_export(export, context)
    nested_artifact = next(
        item
        for item in nested_import.package.artifacts
        if item.relative_path == "effective-backtest-config.json"
    )
    assert nested_artifact.artifact_id in nested_import.package.runs[0].artifact_ids
    extended_document = copy.deepcopy(document)

    resealed_tampering = copy.deepcopy(document)
    tampered_params = resealed_tampering["effective_configuration"]["module_identity"][
        "configuration"
    ]["modules"][0]["parameters"]
    tampered_params["period"] = 99
    resealed_tampering["effective_configuration"]["module_identity"]["configuration"]["rendered"][
        "modules"
    ][0]["parameters"]["period"] = 99
    module_identity = resealed_tampering["effective_configuration"]["module_identity"]
    module_identity["configuration_sha256"] = hashlib.sha256(
        json.dumps(
            module_identity["configuration"]["rendered"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    seal_document = {
        key: value for key, value in resealed_tampering.items() if key != "projection_sha256"
    }
    resealed_tampering["projection_sha256"] = hashlib.sha256(
        json.dumps(
            seal_document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof.write_text(json.dumps(resealed_tampering), encoding="utf-8")
    with pytest.raises(IntegrityError, match="module parameters disagree with Core manifest"):
        adapt_core_v04_export(export, context)

    for identity_value, rendered_value in ((1, True), (0, False), (1, 1.0)):
        malformed = copy.deepcopy(extended_document)
        module_identity = malformed["effective_configuration"]["module_identity"]
        module_config = module_identity["configuration"]
        module_config["modules"][0]["parameters"]["period"] = identity_value
        module_config["rendered"]["modules"][0]["parameters"]["period"] = rendered_value
        module_identity["configuration_sha256"] = hashlib.sha256(
            json.dumps(
                module_config["rendered"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        document.clear()
        document.update(malformed)
        seal()
        proof.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(
            IntegrityError,
            match="rendered module identity|module parameters disagree with Core manifest",
        ):
            adapt_core_v04_export(export, context)

    file_only_document = copy.deepcopy(extended_document)
    effective = file_only_document["effective_configuration"]
    effective["engine_contract_profile"] = "official-file-only.v1"
    effective["app"]["max_base_history"] = None
    effective["app"]["performance"]["values"]["max_base_history"] = None
    source = effective["data_source"]
    source.update(
        source_kind="file",
        path_sha256="f" * 64,
        storage_root_sha256=None,
        format="csv",
        dataset_version=None,
        registered_mode=None,
        stream_batch_size=None,
    )
    execution = effective["engine"]["execution"]
    execution.pop("allow_limit_stop_on_entry_bar")
    execution.pop("close_request_timing")
    effective["engine"]["risk"].pop("sizing_model")
    effective["engine"]["data"].pop("max_base_history")
    document.clear()
    document.update(file_only_document)
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    file_only_import = adapt_core_v04_export(export, context)
    assert any(
        item.relative_path == "effective-backtest-config.json"
        for item in file_only_import.package.artifacts
    )
    mismatched_profile = copy.deepcopy(file_only_document)
    mismatched_profile["effective_configuration"]["engine_contract_profile"] = (
        "official-extended-registered.v1"
    )
    document.clear()
    document.update(mismatched_profile)
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="engine.execution fields are invalid"):
        adapt_core_v04_export(export, context)
    document.clear()
    document.update(extended_document)
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")

    valid_document = copy.deepcopy(document)
    tampered_engine = copy.deepcopy(valid_document)
    tampered_engine["effective_configuration"]["engine"]["execution"]["slippage_bps"] = 3.0
    document.clear()
    document.update(tampered_engine)
    seal(bind_config_hash=False)
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="does not match Core manifest config_hash"):
        adapt_core_v04_export(export, context)
    document.clear()
    document.update(valid_document)
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    for field_path, bad_value, message in (
        (
            ("effective_configuration", "data_source", "storage_root_sha256"),
            "bad",
            "storage_root_sha256",
        ),
        (
            ("effective_configuration", "app", "output_directory_sha256"),
            "G" * 64,
            "output_directory_sha256",
        ),
        (
            ("effective_configuration", "module_identity", "configuration_path_sha256"),
            "bad",
            "configuration_path_sha256",
        ),
        (("context", "execution_performance_mode"), True, "execution mode"),
    ):
        document.clear()
        document.update(copy.deepcopy(valid_document))
        target = document
        for key in field_path[:-1]:
            target = target[key]  # type: ignore[index]
        target[field_path[-1]] = bad_value  # type: ignore[index]
        seal()
        proof.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(IntegrityError, match=message):
            adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))

    document["context"]["seed"] = context.seed + 1  # type: ignore[index]
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="does not match Core run"):
        adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    for labels in (
        {"metric_split": "training"},
        {"metric_period": "all"},
        {"metric_scenario": "other"},
    ):
        with pytest.raises(IntegrityError, match="labels do not match published Core metrics"):
            adapt_core_v04_export(export, _context(**labels))

    invalid_cases: list[tuple[dict[str, object], type[Exception], str]] = []
    candidate = copy.deepcopy(valid_document)
    candidate["schema_id"] = "unknown"
    invalid_cases.append((candidate, UnsupportedSchemaError, "schema"))
    candidate = copy.deepcopy(valid_document)
    candidate["schema_version"] = "1"
    invalid_cases.append((candidate, UnsupportedSchemaError, "version"))
    candidate = copy.deepcopy(valid_document)
    candidate.pop("schema_id")
    invalid_cases.append((candidate, IntegrityError, "document fields"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine_contract_profile"] = "unknown"
    invalid_cases.append((candidate, UnsupportedSchemaError, "engine contract profile"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["dataset_id"] = "unregistered-dataset"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "dataset does not match"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"].pop("run_id")  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "context fields"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["run_id"] = " padded "  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "context.run_id"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["configuration_sha256"] = "bad"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "context.configuration_sha256"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["period_start"] = "yesterday"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "context.period_start"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["period_start"] = "2026-01-01T00:00:00Z"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "canonical UTC timestamp"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["repetition_index"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "context.repetition_index"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["repetition_index"] = -1  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be non-negative"))
    candidate = copy.deepcopy(valid_document)
    candidate["context"]["execution_performance_mode"] = []  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "execution mode"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"] = []
    invalid_cases.append((candidate, IntegrityError, "fields are invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["app"].pop("seed")  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "fields are invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["one_position"] = 1  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be boolean"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["fees"] = []  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "engine.fees fields are invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["fees"]["maker_rate"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "finite number"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["execution"]["max_fill_quantity_per_bar"] = "all"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "finite number"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["risk"]["max_stop_distance_pct"] = False  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "finite number"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["data"]["expected_interval_seconds"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be an integer"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["app"]["seed"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "seed must be an integer"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["app"]["initial_capital"] = "bad"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "finite number"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["app"]["initial_capital"] = 10**400  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "finite number"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["app"]["checkpoint"]["restart_at_bar"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be an integer"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["source_kind"] = "remote"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "source kind is invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["source_kind"] = []  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "source kind is invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["columns"] = []  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "data_source.columns fields are invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["dataset_version"] = False  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be an integer"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["start"] = "2026-01-01T00:00:00Zx"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "canonical UTC timestamp"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"]["start"] = "2026-01-01T00:00:00Z"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "canonical UTC timestamp"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["data_source"].update(  # type: ignore[index, union-attr]
        path_sha256=None,
        storage_root_sha256=None,
    )
    invalid_cases.append((candidate, IntegrityError, "registered path identity is invalid"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["initial_cash"] = 1001.0  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "values disagree with context"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["close_open_position_at_end"] = False  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "end-of-test policy disagrees"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["data"]["expected_interval_seconds"] = 301  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "engine data config disagrees"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["data"]["gap_policy"] = "allow"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "engine data config disagrees"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["module_identity"]["configuration"]["modules"] = []  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "non-empty list"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["module_identity"]["configuration"].pop(  # type: ignore[index, union-attr]
        "rendered"
    )
    invalid_cases.append((candidate, IntegrityError, "module configuration fields"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["module_identity"]["configuration"]["rendered"]["modules"][
        0
    ]["class"] = "pkg:Changed"  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "rendered module identity does not match"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["instrument"]["price_precision"] = True  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must be an integer"))
    constrained_values = (
        (
            ("effective_configuration", "app", "requested_timeframes"),
            ["2h"],
            "timeframes are unsupported",
        ),
        (
            ("effective_configuration", "app", "checkpoint", "mode"),
            "resume",
            "checkpoint.mode is unsupported",
        ),
        (
            ("effective_configuration", "app", "end_of_test_policy"),
            "drop",
            "end_of_test_policy is unsupported",
        ),
        (
            ("effective_configuration", "data_source", "dataset_version"),
            0,
            "dataset version is invalid",
        ),
        (
            ("effective_configuration", "data_source", "registered_mode"),
            "download",
            "registered_mode is unsupported",
        ),
        (
            ("effective_configuration", "data_source", "stream_batch_size"),
            0,
            "stream batch size is invalid",
        ),
        (
            ("effective_configuration", "data_source", "market_type"),
            "spot",
            "market_type is unsupported",
        ),
        (
            ("effective_configuration", "data_source", "columns", "high"),
            "open",
            "columns must map to distinct fields",
        ),
        (
            ("effective_configuration", "data_source", "end"),
            "2025-12-31T00:00:00.000000Z",
            "end must be later than start",
        ),
        (("effective_configuration", "engine", "fees", "maker_rate"), -0.01, "cannot be negative"),
        (
            ("effective_configuration", "engine", "execution", "intrabar_priority"),
            "first",
            "intrabar_priority is unsupported",
        ),
        (
            ("effective_configuration", "engine", "execution", "limit_fill_policy"),
            "best",
            "limit_fill_policy is unsupported",
        ),
        (
            ("effective_configuration", "engine", "execution", "close_request_timing"),
            "later",
            "close_request_timing is unsupported",
        ),
        (
            ("effective_configuration", "engine", "execution", "target_liquidity"),
            "unknown",
            "target_liquidity is unsupported",
        ),
        (
            ("effective_configuration", "engine", "execution", "stop_limit_same_bar_policy"),
            "always",
            "stop_limit_same_bar_policy is unsupported",
        ),
        (
            ("effective_configuration", "engine", "execution", "max_fill_fraction_per_bar"),
            0,
            "max fill fraction",
        ),
        (
            ("effective_configuration", "engine", "execution", "max_fill_quantity_per_bar"),
            0,
            "must be positive",
        ),
        (
            ("effective_configuration", "engine", "risk", "default_risk_fraction"),
            0.06,
            "risk fractions are invalid",
        ),
        (
            ("effective_configuration", "engine", "risk", "sizing_model"),
            "kelly",
            "sizing_model is unsupported",
        ),
        (
            ("effective_configuration", "engine", "data", "expected_interval_seconds"),
            0,
            "must be positive",
        ),
        (
            ("effective_configuration", "engine", "data", "gap_policy"),
            "skip",
            "gap_policy is unsupported",
        ),
        (
            ("effective_configuration", "engine", "data", "timestamp_semantics"),
            "close",
            "timestamp_semantics is unsupported",
        ),
        (("effective_configuration", "engine", "data", "max_base_history"), 0, "must be positive"),
        (("effective_configuration", "instrument", "price_precision"), -1, "must be non-negative"),
        (
            ("effective_configuration", "instrument", "base_asset"),
            " BTC ",
            "instrument.base_asset",
        ),
        (
            ("effective_configuration", "instrument", "quote_asset"),
            " USDT ",
            "instrument.quote_asset",
        ),
        (
            ("effective_configuration", "instrument", "max_quantity"),
            0.0005,
            "maximum quantity is below minimum",
        ),
        (
            ("effective_configuration", "instrument", "max_notional"),
            4.0,
            "maximum notional is below minimum",
        ),
        (("effective_configuration", "instrument", "contract_size"), 0, "must be positive"),
    )
    for field_path, bad_value, message in constrained_values:
        candidate = copy.deepcopy(valid_document)
        target = candidate
        for key in field_path[:-1]:
            target = target[key]  # type: ignore[index]
        target[field_path[-1]] = bad_value  # type: ignore[index]
        invalid_cases.append((candidate, IntegrityError, message))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["risk"]["sizing_model"] = "historical_limit_cost"  # type: ignore[index, union-attr]
    candidate["effective_configuration"]["engine"]["execution"]["limit_fill_policy"] = (
        "price_improvement"  # type: ignore[index, union-attr]
    )
    invalid_cases.append((candidate, IntegrityError, "historical sizing requires at-limit"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["risk"].update(  # type: ignore[index, union-attr]
        min_stop_distance_pct=0.05,
        max_stop_distance_pct=0.04,
    )
    invalid_cases.append((candidate, IntegrityError, "maximum stop distance is below minimum"))
    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["engine"]["one_position"] = False  # type: ignore[index, union-attr]
    invalid_cases.append((candidate, IntegrityError, "must preserve one-position mode"))
    for candidate, error_type, message in invalid_cases:
        effective = candidate.get("effective_configuration")
        if isinstance(effective, dict):
            module_identity = effective.get("module_identity")
            if isinstance(module_identity, dict):
                module_configuration = module_identity.get("configuration")
                if isinstance(module_configuration, dict):
                    rendered_configuration = module_configuration.get("rendered")
                    if isinstance(rendered_configuration, dict):
                        module_identity["configuration_sha256"] = hashlib.sha256(
                            json.dumps(
                                rendered_configuration,
                                ensure_ascii=False,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
        document.clear()
        document.update(candidate)
        seal()
        proof.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(error_type, match=message):
            adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))
    document["context"]["stage_role"] = "candidate"  # type: ignore[index]
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="labels do not match published Core metrics"):
        adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))
    document["context"]["period_start"] = "2025-12-31T00:00:00.000000Z"  # type: ignore[index]
    document["effective_configuration"]["data_source"]["start"] = (  # type: ignore[index, union-attr]
        "2025-12-31T00:00:00.000000Z"
    )
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="labels do not match published Core metrics"):
        adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))
    document["context"]["scenario_id"] = "other"  # type: ignore[index]
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="does not match Core run"):
        adapt_core_v04_export(export, context)

    non_json_parameter = copy.deepcopy(valid_document)
    non_json_parameter["effective_configuration"]["module_identity"]["configuration"]["modules"][0][
        "parameters"
    ]["period"] = float("nan")  # type: ignore[index, union-attr]
    non_json_parameter["effective_configuration"]["module_identity"]["configuration"]["rendered"][
        "modules"
    ][0]["parameters"]["period"] = float("nan")  # type: ignore[index, union-attr]
    proof.write_text(json.dumps(non_json_parameter), encoding="utf-8")
    with pytest.raises(IntegrityError, match="not canonical JSON"):
        adapt_core_v04_export(export, context)

    nondefault_document = copy.deepcopy(valid_document)
    nondefault_effective = nondefault_document["effective_configuration"]
    nondefault_effective["app"]["checkpoint"] = {"mode": "replay", "restart_at_bar": 120}  # type: ignore[index, union-attr]
    nondefault_effective["engine"]["execution"]["max_fill_quantity_per_bar"] = 0.75  # type: ignore[index, union-attr]
    nondefault_effective["engine"]["risk"]["max_stop_distance_pct"] = 0.04  # type: ignore[index, union-attr]
    nondefault_effective["engine"]["data"]["max_base_history"] = 400  # type: ignore[index, union-attr]
    nondefault_effective["app"]["max_base_history"] = 400  # type: ignore[index, union-attr]
    nondefault_effective["app"]["performance"]["values"]["max_base_history"] = 400  # type: ignore[index, union-attr]
    nondefault_effective["instrument"]["price_precision"] = 2  # type: ignore[index, union-attr]
    document.clear()
    document.update(nondefault_document)
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    nondefault_import = adapt_core_v04_export(export, context)
    nondefault_artifact = next(
        item
        for item in nondefault_import.package.artifacts
        if item.relative_path == "effective-backtest-config.json"
    )
    assert nondefault_artifact.artifact_id in nondefault_import.package.runs[0].artifact_ids

    document.clear()
    document.update(copy.deepcopy(valid_document))
    seal()
    document["projection_sha256"] = "0" * 64
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="checksum does not match"):
        adapt_core_v04_export(export, context)

    document.clear()
    document.update(copy.deepcopy(valid_document))
    document["effective_configuration"]["data_source"].update(  # type: ignore[index, union-attr]
        source_kind="file",
        path_sha256="f" * 64,
        storage_root_sha256=None,
        format="csv",
        dataset_version=None,
        registered_mode=None,
        stream_batch_size=None,
    )
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    imported_file_source = adapt_core_v04_export(export, context)
    assert any(
        item.relative_path == "effective-backtest-config.json"
        for item in imported_file_source.package.artifacts
    )

    document["effective_configuration"]["data_source"]["path_sha256"] = None  # type: ignore[index, union-attr]
    seal()
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="file path identity is invalid"):
        adapt_core_v04_export(export, context)

    candidate = copy.deepcopy(valid_document)
    candidate["effective_configuration"]["instrument"]["tick_size"] = float("nan")  # type: ignore[index, union-attr]
    document.clear()
    document.update(candidate)
    proof.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IntegrityError, match="finite number"):
        adapt_core_v04_export(export, context)

    proof.write_text("not JSON", encoding="utf-8")
    with pytest.raises(IntegrityError, match="artifact is invalid"):
        adapt_core_v04_export(export, context)

    proof.write_text(json.dumps(valid_document), encoding="utf-8")
    metrics_path = export / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["return_pct"] = {"unexpected": "object"}
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    with pytest.raises(IntegrityError, match="unsupported value type"):
        adapt_core_v04_export(export, context)

    metrics["return_pct"] = 1
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    journal_path = export / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["errors"] = "not-a-list"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(IntegrityError, match="journal.errors must be a list"):
        adapt_core_v04_export(export, context)


@pytest.mark.parametrize(
    "missing", ["run.json", "metrics.json", "journal.json", "trades.csv", "equity.csv"]
)
def test_core_adapter_rejects_missing_required_files(tmp_path: Path, missing: str) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    (export / missing).unlink()
    with pytest.raises(IntegrityError, match="missing required"):
        adapt_core_v04_export(export, _context())


def test_core_adapter_rejects_invalid_root_and_resource_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="real directory"):
        adapt_core_v04_export(tmp_path / "missing", _context())
    export = tmp_path / "export"
    _write_core_export(export)
    with pytest.raises(ResourceLimitError):
        adapt_core_v04_export(export, _context(), limits=FileLimits(max_file_bytes=2))


@pytest.mark.parametrize(
    ("mutator", "error", "message"),
    [
        (
            lambda d: d["manifest"].update(kernel_version="0.3.0"),
            UnsupportedSchemaError,
            "unsupported Core",
        ),
        (lambda d: d["manifest"].update(data_hash="bad"), IntegrityError, "not SHA-256"),
        (lambda d: d.update(bars_processed=99), IntegrityError, "bars does not match"),
        (lambda d: d.update(modules="strategy"), IntegrityError, "run.modules"),
        (lambda d: d.update(modules=["strategy", "strategy"]), IntegrityError, "duplicates"),
        (lambda d: d["manifest"].update(module_manifests={}), IntegrityError, "must be a list"),
        (
            lambda d: d["manifest"].update(
                module_manifests=[
                    {"name": "strategy", "version": "1.0.0"},
                    {"name": "strategy", "version": "1.0.0"},
                ]
            ),
            IntegrityError,
            "duplicate module",
        ),
        (lambda d: d["manifest"].update(created_at=3), IntegrityError, "ISO timestamp"),
        (
            lambda d: d["manifest"].update(created_at="not-a-date"),
            IntegrityError,
            "created_at is invalid",
        ),
    ],
)
def test_core_adapter_rejects_malformed_run_contract(
    tmp_path: Path, mutator, error: type[Exception], message: str
) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    _rewrite(export / "run.json", mutator)
    with pytest.raises(error, match=message):
        adapt_core_v04_export(export, _context())


def test_core_adapter_rejects_module_and_configuration_hash_mismatch(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    experiment = replace(
        make_package().experiments[0],
        module_versions={"core": "0.4.0", "strategy": "2.0.0"},
    )
    with pytest.raises(IntegrityError, match="module version mismatch"):
        adapt_core_v04_export(export, _context(experiment=experiment))

    base = make_package().configurations[0]
    configuration = ConfigurationRecord.create(
        strategy_id=base.strategy_id,
        schema_version=base.schema_version,
        canonical_configuration=base.canonical_configuration,
        stage=base.stage,
        execution_assumptions=base.execution_assumptions,
        risk_settings=base.risk_settings,
        cost_settings=base.cost_settings,
        funding_settings=base.funding_settings,
        creation_source=base.creation_source,
        producer_configuration_hash="f" * 64,
    )
    with pytest.raises(IntegrityError, match="producer hash"):
        adapt_core_v04_export(export, _context(configuration=configuration))


def test_core_adapter_rejects_metric_and_journal_contract_mismatch(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    _rewrite(export / "metrics.json", lambda d: d.pop("return_pct"))
    with pytest.raises(UnsupportedSchemaError, match="metric contract mismatch"):
        adapt_core_v04_export(export, _context())

    _write_core_export(tmp_path / "second")
    _rewrite(tmp_path / "second/journal.json", lambda d: d.update(errors=["boom"]))
    with pytest.raises(IntegrityError, match="COMPLETED"):
        adapt_core_v04_export(tmp_path / "second", _context())


def test_core_adapter_preserves_failed_run_errors(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _write_core_export(export)
    _rewrite(export / "journal.json", lambda d: d.update(errors=["boom"]))
    result = adapt_core_v04_export(export, _context(run_status=RunStatus.FAILED))
    assert result.package.runs[0].errors == ("boom",)


@pytest.mark.parametrize(
    "changes",
    [
        {"datasets": ()},
        {"seed": -1},
        {"run_status": "COMPLETED"},
        {"observed_completed_at": NOW - timedelta(seconds=1)},
        {"duration_seconds": -1.0},
        {"role": "CANDIDATE"},
        {"role_reason": ""},
        {"stage_id": "bad id"},
    ],
)
def test_core_run_context_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _context(**changes)
