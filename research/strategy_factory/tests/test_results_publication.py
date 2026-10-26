from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from conftest import module

from strategy_factory.adapters import (
    ResearchRunnerAdapter,
    ResearchRunnerExecutionAdapter,
    ResultsAggregatorAdapter,
)
from strategy_factory.backtest_config import (
    BacktestEnginePolicy,
    OfficialBacktestConfigFactory,
    TrustedRuntimeModule,
)
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    Direction,
    FactoryStage,
    ModuleCategory,
    ParameterSpace,
    StrategyArchitecture,
)
from strategy_factory.results_publication import (
    ResearchResultsPublicationAdapter,
    ResultsPublicationPolicy,
)
from strategy_factory.stage_handlers import default_research_split

_SOURCE_SHA = "c" * 40
_LOCK_SHA = "d" * 64


def _write_runtime_modules(root: Path) -> dict[str, str]:
    path = root / "publication_runtime.py"
    classes = {
        "signal": "SignalModule",
        "setup_adapter": "SetupAdapterModule",
        "entry": "EntryModule",
        "protection": "ProtectionModule",
        "target": "TargetModule",
        "risk_request_adapter": "RiskRequestAdapterModule",
        "risk": "RiskModule",
    }
    blocks = [
        "from types import MappingProxyType",
        "from modular_core import ModuleDecision, ModuleManifest, StrategyModule",
        "",
    ]
    for module_id, class_name in classes.items():
        period_arg = ", period=1" if module_id == "signal" else ""
        parameters = '{"period": period}' if module_id == "signal" else "{}"
        schema = '{"period": {"type": "integer", "minimum": 1}}' if module_id == "signal" else "{}"
        blocks.extend(
            [
                f"class {class_name}(StrategyModule):",
                f'    name = "{module_id}"',
                f"    def __init__(self{period_arg}):",
                f"        self.parameters = MappingProxyType({parameters})",
                "        self.manifest = ModuleManifest(",
                "            name=self.name,",
                '            version="1.0.0",',
                '            api_version="1.0",',
                f"            parameters_schema={schema},",
                '            state_schema_version="1",',
                "        )",
                "    def on_bar(self, view):",
                "        return ModuleDecision()",
                "",
            ]
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return {
        module_id: f"publication_runtime:{class_name}" for module_id, class_name in classes.items()
    }


def _architecture(imports: dict[str, str], market_type) -> StrategyArchitecture:
    def ref(module_id: str, category: ModuleCategory, **kwargs):
        return replace(
            module(
                module_id,
                category,
                import_path=imports[module_id],
                **kwargs,
            ),
            source_commit_sha=_SOURCE_SHA,
        )

    return StrategyArchitecture(
        architecture_id="arch-publication",
        market_type=market_type,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        signal=ref(
            "signal",
            ModuleCategory.SIGNAL,
            parameter_schema={"period": {"type": "integer", "minimum": 1}},
            parameter_search_hints={"period": {"values": (2,)}},
        ),
        setup_adapters=(ref("setup_adapter", ModuleCategory.SETUP_ADAPTER),),
        context_filters=(),
        entry=ref("entry", ModuleCategory.ENTRY),
        protection=ref("protection", ModuleCategory.PROTECTION),
        target=ref("target", ModuleCategory.TARGET),
        risk_adapters=(ref("risk_request_adapter", ModuleCategory.RISK_REQUEST_ADAPTER),),
        risk=ref("risk", ModuleCategory.RISK),
    )


def _backtest_policy() -> BacktestEnginePolicy:
    return BacktestEnginePolicy(
        execution={
            "slippage_bps": 2.5,
            "stop_slippage_bps": 3.0,
            "intrabar_priority": "stop",
        },
        fees={"maker_rate": 0.0002, "taker_rate": 0.00055},
        instrument={
            "base_asset": "ETH",
            "quote_asset": "USDT",
            "market_type": "linear_futures",
            "tick_size": 0.1,
            "quantity_step": 0.001,
            "min_quantity": 0.001,
            "min_notional": 5.0,
            "contract_size": 1.0,
        },
    )


def _synthetic_backtest(config, observed_configurations, *, data_sha256: str, performance):
    """Write deterministic synthetic Engine artifacts without running a backtest."""

    observed_configurations.append(config)
    output = config.output.directory
    output.mkdir(parents=True, exist_ok=True)
    from modular_core import ModuleRegistry
    from modular_core.audit import to_primitive
    from modular_core.manifest import parameters_for
    from modular_results_registry.core_adapter_contract import _METRIC_CONTRACT

    module_bytes = config.modules.read_bytes()
    module_registry = ModuleRegistry()
    loaded_modules = module_registry.load_config(config.modules)
    plan = module_registry.validate_and_plan(
        loaded_modules,
        base_timeframe=config.data.base_timeframe,
    )
    from modular_core.run_manifest import _hash
    engine_config = config.core_engine_config()
    engine_config = replace(engine_config, data=replace(
        engine_config.data, max_base_history=performance.max_base_history))
    engine_run_id = f"synthetic-engine-{config.seed}"
    manifest = {
        "run_id": engine_run_id,
        "kernel_version": "0.4.0",
        "data_hash": data_sha256,
        "config_hash": _hash(engine_config),
        "module_hash": hashlib.sha256(module_bytes).hexdigest(),
        "symbol": config.data.symbol,
        "base_timeframe": config.data.base_timeframe,
        "bars": 20,
        "module_manifests": [to_primitive(item) for item in plan.manifests],
        "module_parameters": [
            to_primitive(parameters_for(module)) for module in plan.modules
        ],
        "created_at": "2026-01-03T00:00:00Z",
    }
    run = {
        "manifest": manifest,
        "bars_processed": 20,
        "modules": [module.name for module in plan.modules],
    }
    metrics = {name: 0 for name in _METRIC_CONTRACT}
    metrics["initial_equity"] = config.initial_capital
    metrics["final_equity"] = config.initial_capital
    metrics["average_r_multiple"] = None
    metrics["average_holding_bars"] = None
    contents = {
        "run.json": json.dumps(run, sort_keys=True) + "\n",
        "metrics.json": json.dumps(metrics, sort_keys=True) + "\n",
        "journal.json": '{"errors":[]}\n',
        "trades.csv": "id,pnl\n",
        "equity.csv": "time,equity\n",
        "events.jsonl": '{"event":"done"}\n',
        "application-run.json": json.dumps(
            {"core_run_id": engine_run_id, "application_run_id": engine_run_id,
             "funding_model": "disabled", "performance": asdict(performance)},
            sort_keys=True,
        )
        + "\n",
        "metrics-extended.json": '{"synthetic":true}\n',
        "data-validation.json": '{"synthetic":true}\n',
        "restart-evidence.json": '{"synthetic":true}\n',
    }
    for name, payload in contents.items():
        (output / name).write_text(payload, encoding="utf-8")
    files = {
        path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    (output / "artifact-manifest.json").write_text(
        json.dumps(
            {"schema_version": "1", "run_id": engine_run_id, "files": files},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        run_id=engine_run_id,
        output_directory=output,
        bars_processed=20,
        restart_verified=True,
    )


def test_real_research_package_registers_idempotently_in_results_aggregator(
    factory_request, tmp_path: Path, monkeypatch
) -> None:
    from modular_research_runner import (
        AtomicJsonArtifactStore,
        EnvironmentManifest,
        GitProvenance,
        LocalResearchScheduler,
        ModularBacktestEngineAdapter,
        ResearchPlanBuilder,
        ResearchRunner,
        ResumePolicy,
        RetryPolicy,
        SqliteExperimentRepository,
    )
    from modular_research_runner.effective_config import EFFECTIVE_CONFIG_CONTRACT

    imports = _write_runtime_modules(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    architecture = _architecture(imports, factory_request.market_type)
    parameter_space = ParameterSpace(
        space_id="space-publication",
        architecture_id=architecture.architecture_id,
        dimensions={"signal.period": (2,)},
        maximum_combinations=1,
    )
    runs_root = tmp_path / "research-runs"
    official_factory = OfficialBacktestConfigFactory(
        request=factory_request,
        policy=_backtest_policy(),
        trusted_modules=tuple(
            TrustedRuntimeModule(
                runtime_component_id=module_id,
                runtime_component_version="1.0.0",
                import_path=import_path,
                source_commit_sha=_SOURCE_SHA,
            )
            for module_id, import_path in sorted(imports.items())
        ),
        allowed_output_root=runs_root,
        preflight_modules=False,
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="canonical_audit",
    )
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite3")
    observed_configurations: list[object] = []
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(
            official_factory,
            lambda config, *, performance: _synthetic_backtest(
                config,
                observed_configurations,
                data_sha256=factory_request.dataset.sha256,
                performance=performance,
            ),
        ),
        retry_policy=RetryPolicy.from_mapping(
            {
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            }
        ),
        resume_policy=ResumePolicy.from_mapping(
            {
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            }
        ),
        maximum_workers=1,
        poll_interval_seconds=0.001,
    )
    runner = ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "research-artifacts"),
        plan_builder=ResearchPlanBuilder(runs_root),
        scheduler=scheduler,
        backtest_engine_version="0.1.0",
        metric_schema_version="core.metrics.v0.4.0",
        module_api_version="1.0",
    )
    execution = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=GitProvenance(
            branch="local-publication-test",
            commit_sha="a" * 40,
            clean=True,
        ),
        environment_manifest=EnvironmentManifest.current(_LOCK_SHA),
    )
    planning = ResearchRunnerAdapter(output_root=runs_root)
    prepared = planning.build_plan(
        request=factory_request,
        factory_run_id="sf-run-publication",
        manifest_id="sf-manifest-publication",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=parameter_space,
        split=default_research_split(factory_request),
        search_method="grid",
        experiment_count=1,
    )
    submission = execution.ensure_submission(prepared)
    result = execution.execute(submission.experiment_id)
    assert result.failure is None
    assert result.snapshot is not None
    assert result.snapshot.status == "completed"
    # The actual callback above receives the typed configuration constructed by
    # OfficialBacktestConfigFactory; the Runner persists its v2 reference before
    # package publication below.
    assert len(observed_configurations) == 1
    from modular_backtest import BacktestAppConfig

    assert isinstance(observed_configurations[0], BacktestAppConfig)

    # Reopen the native repository with a fresh Runner/Factory adapter and recover
    # the immutable v2 envelope before publishing its package.
    database_path = tmp_path / "research.sqlite3"
    cold_repository = SqliteExperimentRepository(database_path)
    cold_runner = ResearchRunner(
        repository=cold_repository,
        artifact_store=AtomicJsonArtifactStore(tmp_path / "research-artifacts"),
        plan_builder=ResearchPlanBuilder(runs_root),
        scheduler=LocalResearchScheduler(
            repository=cold_repository,
            engine=ModularBacktestEngineAdapter(official_factory, _synthetic_backtest),
            retry_policy=RetryPolicy.from_mapping(
                {
                    "max_attempts": 2,
                    "initial_backoff_seconds": 1.0,
                    "backoff_multiplier": 2.0,
                    "maximum_backoff_seconds": 30.0,
                    "retryable_categories": ("TRANSIENT", "RESOURCE"),
                }
            ),
            resume_policy=ResumePolicy.from_mapping(
                {
                    "lease_seconds": 900,
                    "heartbeat_seconds": 60,
                    "recover_expired_leases": True,
                    "verify_success_artifacts": True,
                }
            ),
            maximum_workers=1,
            poll_interval_seconds=0.001,
        ),
        backtest_engine_version="0.1.0",
        metric_schema_version="core.metrics.v0.4.0",
        module_api_version="1.0",
    )
    cold_execution = ResearchRunnerExecutionAdapter(
        runner=cold_runner,
        repository=cold_repository,
        git_provenance=GitProvenance(
            branch="local-publication-test",
            commit_sha="a" * 40,
            clean=True,
        ),
        environment_manifest=EnvironmentManifest.current(_LOCK_SHA),
    )
    cold_execution.restore_submission(
        prepared,
        expected_experiment_id=submission.experiment_id,
    )
    restored_run = cold_repository.get_run(result.snapshot.run_ids[0])
    assert restored_run.result is not None
    assert restored_run.result.effective_config_artifact is not None

    registry_path = tmp_path / "results.sqlite3"
    publication = ResearchResultsPublicationAdapter(
        execution=cold_execution,
        request=factory_request,
        package_root=tmp_path / "registry-packages",
        registry_path=registry_path,
        producer_module_version="0.3.0",
        policy=ResultsPublicationPolicy(
            execution_assumptions={"intrabar_priority": "stop"},
            risk_settings={"research_risk_fraction": str(factory_request.research_risk_fraction)},
            cost_settings={"maker_rate": "0.0002", "taker_rate": "0.00055"},
        ),
    )
    observed_at = datetime(2026, 1, 3, tzinfo=UTC)
    first = publication.publish_experiment(
        submission.experiment_id,
        observed_at=observed_at,
    )
    assert len(first) == 1
    assert first[0].registry_state == "REGISTERED"
    assert first[0].registered_entities > 0
    from modular_research_runner.effective_config import EFFECTIVE_CONFIG_FILENAME
    from modular_results_registry.query import RegistryQueryAPI

    query = RegistryQueryAPI(publication._registry)
    run_record = query.run(first[0].run_id)
    assert run_record is not None
    effective_artifacts = tuple(
        artifact
        for artifact_id in run_record.artifact_ids
        if (artifact := query.artifact(artifact_id)) is not None
        and artifact.relative_path == EFFECTIVE_CONFIG_FILENAME
    )
    assert len(effective_artifacts) == 1
    assert effective_artifacts[0].sha256
    assert (
        first[0].native_configuration_hash
        == cold_execution.frozen_configurations(submission.experiment_id)[
            0
        ].native_configuration_hash
    )

    second = publication.publish_experiment(
        submission.experiment_id,
        observed_at=observed_at,
    )
    assert len(second) == 1
    assert second[0].registry_state == "DUPLICATE"
    assert second[0].registration_package_id == first[0].registration_package_id

    aggregator = ResultsAggregatorAdapter(registry_path)
    candidates = aggregator.candidates_for_experiment(
        submission.experiment_id,
        generated_at=observed_at,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    frozen = cold_execution.frozen_configurations(submission.experiment_id)[0]
    assert candidate.configuration_hash == frozen.factory_configuration_hash
    assert candidate.producer_configuration_hash == frozen.native_configuration_hash
    assert candidate.canonical_configuration == frozen.parameters
    assert candidate.run_ids == (first[0].run_id,)
    assert canonical_sha256(candidate.canonical_configuration) == candidate.configuration_hash
    aggregator.close()
    publication.close()
