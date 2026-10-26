from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from dataclasses import fields as dataclass_fields
from types import SimpleNamespace

import pytest
from modular_core import ModuleDecision, ModuleManifest, StrategyModule
from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_CONTRACT,
    ENGINE_CONTRACT_EXTENDED_REGISTERED_V1,
    PreparedBacktestConfiguration,
)
from modular_research_runner.models import TimeRange

from strategy_factory.backtest_config import TrustedRuntimeModule
from strategy_factory.errors import CompatibilityError
from strategy_factory.multi_strategy_owner import (
    MS5A_COST_STRESS_MULTIPLIER,
    MS5A_COST_STRESS_SCENARIO_ID,
    MS5A_DATASET_ID,
    MS5A_INITIAL_CAPITAL,
    MS5A_RISK_FRACTION,
)
from strategy_factory.multi_strategy_research_runtime import (
    _OWNER_EXECUTION_WINDOW_SECONDS,
    _OWNER_RESEARCH_RETURN_MARGIN_SECONDS,
    _OWNER_WORKFLOW_CLEANUP_MARGIN_SECONDS,
    _OWNER_WORKFLOW_TIMEOUT_SECONDS,
    MS5ABacktestConfigFactory,
    RuntimePaths,
    _execution_config_for_performance_mode,
    _ms5a_execution_window_seconds,
    _OwnerFundingBacktestRunner,
    _scenario_cost_multiplier,
    build_ms5a_research_runtime,
    ms5a_backtest_policy,
)
from strategy_factory.multi_strategy_search_surface import MS5ASearchSurface
from strategy_factory.registry import StrategyModuleCatalog
from strategy_factory.run import StrategyFactoryRun

_HEAD = "a" * 40
_GOVERNED_SEARCH_PARENT_TIMEOUT_SECONDS = 1560 * 60


class _FixtureSignalModule(StrategyModule):
    name = "signal"

    def __init__(self) -> None:
        self.parameters = {}
        self.manifest = ModuleManifest(
            name=self.name,
            version="1.0.0",
            api_version="1.0",
            parameters_schema={},
            state_schema_version="1",
        )

    def on_bar(self, view):
        return ModuleDecision()


@dataclass(frozen=True)
class _Checkpoint:
    mode: str
    restart_at_bar: int | None = None


@dataclass(frozen=True)
class _Config:
    checkpoint: _Checkpoint | None


def test_research_fast_disables_replay_checkpoint_only() -> None:
    replay = _Config(_Checkpoint("replay", 17))
    disabled = _execution_config_for_performance_mode(replay, "research_fast")

    assert disabled is not replay
    assert disabled.checkpoint == _Checkpoint("disabled", None)
    assert _execution_config_for_performance_mode(replay, "canonical_audit") is replay
    no_checkpoint = _Config(None)
    assert _execution_config_for_performance_mode(no_checkpoint, "research_fast") is no_checkpoint


def test_owner_runtime_timeout_geometry_preserves_full_frozen_budget() -> None:
    assert _OWNER_WORKFLOW_TIMEOUT_SECONDS == _GOVERNED_SEARCH_PARENT_TIMEOUT_SECONDS
    assert _OWNER_WORKFLOW_CLEANUP_MARGIN_SECONDS == 60 * 60
    assert _OWNER_RESEARCH_RETURN_MARGIN_SECONDS == 60 * 60
    assert _OWNER_EXECUTION_WINDOW_SECONDS == 24 * 60 * 60
    assert (
        _ms5a_execution_window_seconds(
            SimpleNamespace(
                request=SimpleNamespace(
                    budget=SimpleNamespace(maximum_duration_seconds=24 * 60 * 60)
                )
            )
        )
        == 24 * 60 * 60
    )


def test_execution_window_is_bounded_and_rejects_invalid_budget() -> None:
    assert (
        _ms5a_execution_window_seconds(
            SimpleNamespace(
                request=SimpleNamespace(budget=SimpleNamespace(maximum_duration_seconds=90))
            )
        )
        == 90
    )
    assert _ms5a_execution_window_seconds(SimpleNamespace(request=None)) > 90

    for invalid in (True, 0, -1, "90"):
        with pytest.raises(CompatibilityError, match="maximum_duration_seconds"):
            _ms5a_execution_window_seconds(
                SimpleNamespace(
                    request=SimpleNamespace(
                        budget=SimpleNamespace(maximum_duration_seconds=invalid)
                    )
                )
            )


def test_owner_funding_runner_preserves_mode_deadline_and_funding_source() -> None:
    calls = []
    deadlines = []

    @contextmanager
    def deadline_scope(deadline):
        deadlines.append(deadline)
        yield

    def performance_config(*, mode):
        return SimpleNamespace(mode=mode, execution_deadline_scope=deadline_scope)

    def run_backtest(config, performance, *, funding_source):
        calls.append((config, performance.mode, funding_source))
        return "ok"

    source = object()
    runner = _OwnerFundingBacktestRunner(
        run_backtest=run_backtest,
        performance_config=performance_config,
        funding_source=source,
        execution_window_seconds=30,
    )
    replay = _Config(_Checkpoint("replay", 4))

    assert runner(replay) == "ok"
    assert calls[-1][0] is replay
    assert calls[-1][1] == "canonical_audit"
    assert calls[-1][2] is source

    assert runner.run_with_performance_mode(replay, performance_mode="research_fast") == "ok"
    assert calls[-1][0].checkpoint == _Checkpoint("disabled", None)
    assert calls[-1][1] == "research_fast"
    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1]

    with pytest.raises(CompatibilityError, match="unsupported MS5A performance mode"):
        runner.run_with_performance_mode(replay, performance_mode="turbo")


def test_owner_funding_runner_accepts_real_backtest_deadline_contract() -> None:
    from modular_backtest.scalability import PerformanceConfig

    calls = []

    def run_backtest(config, performance, *, funding_source):
        calls.append((config, performance.mode, funding_source))
        return "ok"

    source = object()
    runner = _OwnerFundingBacktestRunner(
        run_backtest=run_backtest,
        performance_config=PerformanceConfig,
        funding_source=source,
        execution_window_seconds=60,
    )

    assert runner(_Config(None)) == "ok"
    assert calls == [(_Config(None), "canonical_audit", source)]


def test_owner_funding_runner_rejects_genuine_engine_without_deadline_contract() -> None:
    performance_type = type("PerformanceConfig", (), {})
    performance_type.__module__ = "modular_backtest.scalability"
    performance_type.mode = "canonical_audit"

    runner = _OwnerFundingBacktestRunner(
        run_backtest=lambda *args, **kwargs: None,
        performance_config=lambda **kwargs: performance_type(),
        funding_source=object(),
        execution_window_seconds=1,
    )

    with pytest.raises(CompatibilityError, match="execution deadline contract"):
        runner(_Config(None))


def test_cost_scenario_contract_is_exact() -> None:
    assert (
        _scenario_cost_multiplier(
            SimpleNamespace(
                scenario_id="base",
                metadata={"scenario_category": "base", "scenario_overrides": {}},
            )
        )
        == 1.0
    )
    assert _scenario_cost_multiplier(
        SimpleNamespace(
            scenario_id=MS5A_COST_STRESS_SCENARIO_ID,
            metadata={
                "scenario_category": "cost",
                "scenario_overrides": {"execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER},
            },
        )
    ) == float(MS5A_COST_STRESS_MULTIPLIER)

    with pytest.raises(CompatibilityError, match="unsupported MS5A scenario override"):
        _scenario_cost_multiplier(
            SimpleNamespace(
                scenario_id="base",
                metadata={
                    "scenario_category": "base",
                    "scenario_overrides": {"execution_cost_multiplier": 2},
                },
            )
        )
    with pytest.raises(CompatibilityError, match="unsupported MS5A scenario category"):
        _scenario_cost_multiplier(
            SimpleNamespace(
                scenario_id="latency",
                metadata={"scenario_category": "latency", "scenario_overrides": {}},
            )
        )


def _run_and_surface(factory_request, tmp_path, *, import_path="fixture:signal"):
    dataset = replace(factory_request.dataset, dataset_id=MS5A_DATASET_ID)
    request = replace(
        factory_request,
        dataset=dataset,
        initial_capital=MS5A_INITIAL_CAPITAL,
        research_risk_fraction=MS5A_RISK_FRACTION,
    )
    run = StrategyFactoryRun(
        request=request,
        commit_sha=_HEAD,
        state_root=tmp_path / "factory",
        module_versions={},
        now=request.created_at,
    )
    trusted = (
        TrustedRuntimeModule(
            runtime_component_id="fixture.signal",
            runtime_component_version="1.0.0",
            import_path=import_path,
            source_commit_sha=_HEAD,
        ),
    )
    surface = MS5ASearchSurface(
        catalog=StrategyModuleCatalog(),
        allowed_strategy_families=request.allowed_strategy_families,
        trusted_runtime_modules=trusted,
        runtime_closure=SimpleNamespace(blueprint=(), feature_contracts=frozenset()),
        source_commit_sha=_HEAD,
    )
    return run, surface


def test_ms5a_backtest_policy_keeps_canonical_cost_risk_and_checkpoint() -> None:
    policy = ms5a_backtest_policy()

    assert policy.execution == {"slippage_bps": 2.5, "stop_slippage_bps": 2.5}
    assert policy.fees == {"maker_rate": 0.0002, "taker_rate": 0.00055}
    assert policy.risk["default_risk_fraction"] == float(MS5A_RISK_FRACTION)
    assert policy.risk["max_risk_fraction"] == float(MS5A_RISK_FRACTION)
    assert policy.gap_policy == "reject"
    assert policy.checkpoint_mode == "replay"
    assert policy.end_of_test_policy == "force_close"


def test_runtime_factory_rejects_surface_source_drift(factory_request, tmp_path) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)
    drifted = replace(surface, source_commit_sha="b" * 40)

    with pytest.raises(CompatibilityError, match="surface differs"):
        MS5ABacktestConfigFactory(
            run=run,
            surface=drifted,
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output",
        )


def test_runtime_effective_config_contract_requires_explicit_opt_in(
    factory_request, tmp_path
) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)
    default = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=tmp_path,
        allowed_output_root=tmp_path / "default-output",
    )
    opted_in = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=tmp_path,
        allowed_output_root=tmp_path / "v2-output",
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode="research_fast",
    )
    assert default._renderer._effective_config_contract is None
    assert opted_in._renderer._effective_config_contract == EFFECTIVE_CONFIG_CONTRACT


@pytest.mark.parametrize(
    ("performance_mode", "projected_checkpoint"),
    (("canonical_audit", "replay"), ("research_fast", "disabled")),
)
def test_runtime_effective_config_records_callback_checkpoint(
    factory_request, tmp_path, performance_mode, projected_checkpoint
) -> None:
    from modular_research_runner.effective_config import verify_actual_backtest_configuration

    run, surface = _run_and_surface(
        factory_request,
        tmp_path,
        import_path=f"{__name__}:_FixtureSignalModule",
    )
    factory = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=tmp_path,
        allowed_output_root=tmp_path / "output",
        effective_config_contract=EFFECTIVE_CONFIG_CONTRACT,
        execution_performance_mode=performance_mode,
    )
    job_output = (tmp_path / "output" / "run-v2").resolve()
    module = surface.trusted_runtime_modules[0]
    job = SimpleNamespace(
        run_id="run-v2",
        experiment_id="experiment-v2",
        stage_id="stage-v2",
        stage_role="validation",
        split_id="split-v2",
        dataset_id=MS5A_DATASET_ID,
        scenario_id="base",
        seed=7,
        repetition_index=0,
        period=TimeRange(run.request.dataset.period.start, run.request.dataset.period.end),
        output_directory=job_output,
        metadata={"configuration_sha256": "d" * 64},
        parameters={
            "runtime_modules": (
                {
                    "runtime_component_id": module.runtime_component_id,
                    "runtime_component_version": module.runtime_component_version,
                    "import_path": module.import_path,
                    "source_commit_sha": module.source_commit_sha,
                    "api_version": "1.0",
                    "state_schema_version": "1",
                    "fixed_parameters": {},
                    "parameter_schema": {},
                },
            ),
            "factory_run_id": "factory-run-v2",
            "factory_manifest_id": "factory-manifest-v2",
            "factory_candidate_hash": "e" * 64,
        },
    )

    prepared = factory(job)

    assert isinstance(prepared, PreparedBacktestConfiguration)
    assert (
        prepared.effective_configuration["engine_contract_profile"]
        == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
    )
    assert prepared.config.checkpoint.mode == projected_checkpoint
    assert prepared.effective_configuration["app"]["checkpoint"]["mode"] == projected_checkpoint
    assert prepared.context["execution_performance_mode"] == performance_mode
    from modular_backtest.scalability import PerformanceConfig

    expected_performance = PerformanceConfig(mode=performance_mode)
    assert prepared.effective_configuration["app"]["max_base_history"] == (
        expected_performance.max_base_history
    )
    assert prepared.effective_configuration["app"]["max_timeframe_history"] == (
        expected_performance.max_timeframe_history
    )
    assert prepared.effective_configuration["engine"]["data"]["max_base_history"] == (
        expected_performance.max_base_history
    )
    verify_actual_backtest_configuration(prepared)
    source = prepared.effective_configuration["data_source"]
    assert source["source_kind"] == "registered"
    assert source["path_sha256"] is None
    assert source["storage_root_sha256"] is not None
    assert source["dataset_id"] == MS5A_DATASET_ID
    assert source["registered_mode"] == prepared.config.data.registered_mode
    assert source["stream_batch_size"] == prepared.config.data.stream_batch_size
    assert set(prepared.effective_configuration["engine"]) == {
        field.name for field in dataclass_fields(prepared.config.core_engine_config())
    }
    assert set(prepared.effective_configuration["instrument"]) == {
        field.name for field in dataclass_fields(prepared.config.instrument_spec())
    }

    callback_configs = []
    callback = _OwnerFundingBacktestRunner(
        run_backtest=lambda config, performance, *, funding_source: callback_configs.append(
            (config, performance)
        ),
        performance_config=PerformanceConfig,
        funding_source=object(),
        expected_effective_performance_mode=performance_mode,
    )
    callback.run_with_performance_mode(
        prepared.config,
        performance_mode=performance_mode,
        performance=prepared.performance_config,
    )
    assert callback_configs[-1][0].checkpoint.mode == projected_checkpoint
    assert callback_configs[-1][1] is prepared.performance_config

    mismatched_callback = _OwnerFundingBacktestRunner(
        run_backtest=lambda *args, **kwargs: pytest.fail("must reject before backtest callback"),
        performance_config=PerformanceConfig,
        funding_source=object(),
        expected_effective_performance_mode=performance_mode,
    )
    with pytest.raises(CompatibilityError, match="performance mode differs"):
        mismatched_callback.run_with_performance_mode(
            prepared.config,
            performance_mode=performance_mode,
            performance=PerformanceConfig(
                mode="research_fast" if performance_mode == "canonical_audit" else "canonical_audit"
            ),
        )


def test_runtime_effective_config_rejects_callback_mode_mismatch() -> None:
    callback = _OwnerFundingBacktestRunner(
        run_backtest=lambda *args, **kwargs: pytest.fail("must not invoke mismatched callback"),
        performance_config=lambda **kwargs: SimpleNamespace(),
        funding_source=object(),
        expected_effective_performance_mode="canonical_audit",
    )
    with pytest.raises(CompatibilityError, match="differs from Backtest callback"):
        callback.run_with_performance_mode(
            SimpleNamespace(checkpoint=SimpleNamespace(mode="replay")),
            performance_mode="research_fast",
        )


def test_build_ms5a_research_runtime_assembles_owner_bound_components(
    factory_request,
    tmp_path,
) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)
    research_root = tmp_path / "research"
    paths = RuntimePaths(
        factory_state_root=tmp_path / "factory-state",
        research_state_path=research_root / "research.sqlite3",
        research_artifact_root=research_root / "artifacts",
        research_output_root=research_root / "output",
        results_package_root=tmp_path / "results" / "packages",
        results_registry_path=tmp_path / "results" / "registry.sqlite3",
    )
    research_root.mkdir(parents=True)
    owner = SimpleNamespace(dataset_file_size=1234, funding_source=object())
    provenance = object()
    environment = object()

    runner, repository, adapter, got_provenance, got_environment = build_ms5a_research_runtime(
        run=run,
        owner=owner,
        surface=surface,
        paths=paths,
        market_data_root=tmp_path,
        git_provenance=provenance,
        environment_manifest=environment,
    )

    assert runner is not None
    assert repository is not None
    assert adapter is not None
    assert runner.scheduler.engine._config_factory._renderer._effective_config_contract is None
    assert got_provenance is provenance
    assert got_environment is environment

    research_run = runner.scheduler.engine._run_function
    assert research_run.keywords == {"performance_mode": "research_fast"}
    assert research_run.func.__name__ == "run_with_performance_mode"
    assert isinstance(research_run.func.__self__, _OwnerFundingBacktestRunner)


def test_research_fast_rejects_incompatible_checkpoint_dataclass() -> None:
    config = SimpleNamespace(checkpoint=SimpleNamespace(mode="replay", restart_at_bar=3))

    with pytest.raises(CompatibilityError, match="checkpoint contract is incompatible"):
        _execution_config_for_performance_mode(config, "research_fast")


@pytest.mark.parametrize(
    ("run_backtest", "performance_config", "window", "exc", "match"),
    (
        (None, lambda **kwargs: None, 1, TypeError, "run_backtest must be callable"),
        (lambda *args, **kwargs: None, None, 1, TypeError, "performance_config must be callable"),
        (
            lambda *args, **kwargs: None,
            lambda **kwargs: None,
            0,
            ValueError,
            "positive integer",
        ),
        (
            lambda *args, **kwargs: None,
            lambda **kwargs: None,
            True,
            ValueError,
            "positive integer",
        ),
    ),
)
def test_owner_funding_runner_constructor_fails_closed(
    run_backtest,
    performance_config,
    window,
    exc,
    match: str,
) -> None:
    with pytest.raises(exc, match=match):
        _OwnerFundingBacktestRunner(
            run_backtest=run_backtest,
            performance_config=performance_config,
            funding_source=object(),
            execution_window_seconds=window,
        )


def test_owner_funding_runner_preserves_test_double_without_deadline_scope() -> None:
    calls = []

    runner = _OwnerFundingBacktestRunner(
        run_backtest=lambda config, performance, *, funding_source: calls.append(
            (config, performance, funding_source)
        ),
        performance_config=lambda **kwargs: SimpleNamespace(mode=kwargs["mode"]),
        funding_source="funding",
        execution_window_seconds=1,
    )

    config = _Config(None)
    runner(config)

    assert calls == [(config, SimpleNamespace(mode="canonical_audit"), "funding")]


@pytest.mark.parametrize(
    "job",
    (
        SimpleNamespace(metadata="bad"),
        SimpleNamespace(scenario_id=1, metadata={}),
        SimpleNamespace(metadata={"scenario_category": 1}),
        SimpleNamespace(metadata={"scenario_overrides": []}),
        SimpleNamespace(
            scenario_id="wrong",
            metadata={
                "scenario_category": "cost",
                "scenario_overrides": {"execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER},
            },
        ),
        SimpleNamespace(
            scenario_id=MS5A_COST_STRESS_SCENARIO_ID,
            metadata={
                "scenario_category": "cost",
                "scenario_overrides": {"execution_cost_multiplier": 3},
            },
        ),
    ),
)
def test_cost_scenario_contract_rejects_malformed_metadata(job) -> None:
    with pytest.raises(CompatibilityError, match="scenario"):
        _scenario_cost_multiplier(job)


def test_runtime_factory_constructor_rejects_contract_drift(factory_request, tmp_path) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)

    with pytest.raises(TypeError, match="run must be StrategyFactoryRun"):
        MS5ABacktestConfigFactory(
            run=SimpleNamespace(),
            surface=surface,
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output-a",
        )
    with pytest.raises(TypeError, match="surface must be MS5ASearchSurface"):
        MS5ABacktestConfigFactory(
            run=run,
            surface=SimpleNamespace(),
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output-b",
        )

    def make_run(request, suffix: str) -> StrategyFactoryRun:
        return StrategyFactoryRun(
            request=request,
            commit_sha=_HEAD,
            state_root=tmp_path / f"factory-{suffix}",
            module_versions={},
            now=request.created_at,
        )

    wrong_dataset = replace(
        run.request,
        dataset=replace(run.request.dataset, dataset_id="wrong-dataset"),
    )
    with pytest.raises(CompatibilityError, match="canonical dataset owner"):
        MS5ABacktestConfigFactory(
            run=make_run(wrong_dataset, "dataset"),
            surface=surface,
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output-c",
        )

    wrong_capital = replace(
        run.request,
        initial_capital=run.request.initial_capital * 2,
    )
    with pytest.raises(CompatibilityError, match="initial capital"):
        MS5ABacktestConfigFactory(
            run=make_run(wrong_capital, "capital"),
            surface=surface,
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output-d",
        )

    wrong_risk = replace(
        run.request,
        research_risk_fraction=run.request.research_risk_fraction * 2,
    )
    with pytest.raises(CompatibilityError, match="research risk"):
        MS5ABacktestConfigFactory(
            run=make_run(wrong_risk, "risk"),
            surface=surface,
            market_data_root=tmp_path,
            allowed_output_root=tmp_path / "output-e",
        )


def test_runtime_factory_call_requires_owner_dataset_and_mapping_parameters(
    factory_request,
    tmp_path,
) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)
    factory = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=tmp_path,
        allowed_output_root=tmp_path / "output",
    )

    with pytest.raises(CompatibilityError, match="canonical registered dataset"):
        factory(SimpleNamespace(dataset_id="wrong"))
    with pytest.raises(CompatibilityError, match="parameters must be a mapping"):
        factory(SimpleNamespace(dataset_id=MS5A_DATASET_ID, parameters=None))


def test_runtime_factory_renders_registered_streaming_config(
    factory_request,
    tmp_path,
) -> None:
    run, surface = _run_and_surface(factory_request, tmp_path)
    factory = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=tmp_path,
        allowed_output_root=tmp_path / "output",
    )
    start = run.request.dataset.period.start
    end = run.request.dataset.period.end
    modules_path = tmp_path / "modules.json"
    output_path = tmp_path / "output" / "job"

    factory._renderer = SimpleNamespace(
        _job_output_directory=lambda job: output_path,
        _job_period=lambda job: (start, end),
        _module_configuration=lambda parameters: {"parameters": dict(parameters)},
        _write_modules_configuration=lambda directory, payload: modules_path,
    )
    job = SimpleNamespace(
        dataset_id=MS5A_DATASET_ID,
        parameters={"fixture.signal.value": 1},
        scenario_id=MS5A_COST_STRESS_SCENARIO_ID,
        metadata={
            "scenario_category": "cost",
            "scenario_overrides": {"execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER},
        },
        seed=312,
    )

    rendered = factory(job)

    assert rendered.data.dataset_id == MS5A_DATASET_ID
    assert rendered.data.dataset_version == 1
    assert rendered.data.registered_mode == "streaming"
    assert rendered.data.start == start
    assert rendered.data.end == end
    assert rendered.modules == modules_path
    assert rendered.seed == 312
    assert rendered.checkpoint.mode == "replay"
    assert rendered.execution["slippage_bps"] == 5.0
    assert rendered.fees["taker_rate"] == 0.0011
    assert tuple(rendered.requested_timeframes) == tuple(
        timeframe
        for timeframe in run.request.allowed_timeframes
        if timeframe != run.request.dataset.base_timeframe
    )
