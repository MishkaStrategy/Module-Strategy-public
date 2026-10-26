from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_factory.adapters import (
    ResearchRunnerAdapter,
    ResearchSplitPlan,
    UnavailableStrategyAuditPort,
)
from strategy_factory.contracts import FactoryStage, ParameterSpace, TimeRange
from strategy_factory.errors import CompatibilityError, ExternalComponentUnavailableError


def test_research_runner_adapter_builds_native_plan(
    factory_request, architecture, tmp_path: Path
) -> None:
    start = factory_request.dataset.period.start
    split = ResearchSplitPlan(
        train=TimeRange(start, start + timedelta(minutes=25)),
        validation=TimeRange(start + timedelta(minutes=25), start + timedelta(minutes=50)),
        test=TimeRange(start + timedelta(minutes=50), start + timedelta(minutes=75)),
        holdout=TimeRange(start + timedelta(minutes=75), start + timedelta(minutes=100)),
    )
    space = ParameterSpace(
        space_id="space-fixture",
        architecture_id=architecture.architecture_id,
        dimensions={"target_r": (2, 3, 4), "risk": ("0.005", "0.01")},
        maximum_combinations=6,
    )
    prepared = ResearchRunnerAdapter(output_root=tmp_path / "research").build_plan(
        request=factory_request,
        factory_run_id="sf-run-adapter",
        manifest_id="manifest-adapter",
        stage=FactoryStage.COARSE_SEARCH,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="grid",
        experiment_count=6,
    )
    assert prepared.factory_plan.factory_run_id == "sf-run-adapter"
    assert prepared.native_specification.datasets[0].sha256 == factory_request.dataset.sha256
    assert len(prepared.native_plan.jobs) > 0
    assert prepared.native_specification.split_policy.final_untouched_test is not None


def test_research_runner_adapter_factory_plan_identity_ignores_output_root(
    factory_request, architecture, tmp_path: Path
) -> None:
    start = factory_request.dataset.period.start
    split = ResearchSplitPlan(
        train=TimeRange(start, start + timedelta(minutes=25)),
        validation=TimeRange(start + timedelta(minutes=25), start + timedelta(minutes=50)),
        test=TimeRange(start + timedelta(minutes=50), start + timedelta(minutes=75)),
        holdout=TimeRange(start + timedelta(minutes=75), start + timedelta(minutes=100)),
    )
    space = ParameterSpace(
        space_id="space-root-independent",
        architecture_id=architecture.architecture_id,
        dimensions={"target_r": (2,)},
        maximum_combinations=1,
    )

    first = ResearchRunnerAdapter(output_root=tmp_path / "first").build_plan(
        request=factory_request,
        factory_run_id="sf-run-root-independent",
        manifest_id="manifest-root-independent",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="grid",
        experiment_count=1,
    )
    second = ResearchRunnerAdapter(output_root=tmp_path / "second").build_plan(
        request=factory_request,
        factory_run_id="sf-run-root-independent",
        manifest_id="manifest-root-independent",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="grid",
        experiment_count=1,
    )

    assert first.factory_plan == second.factory_plan
    assert first.factory_plan.plan_id == second.factory_plan.plan_id


def test_research_runner_adapter_normalizes_float_search_values(
    factory_request, architecture, tmp_path: Path
) -> None:
    start = factory_request.dataset.period.start
    split = ResearchSplitPlan(
        train=TimeRange(start, start + timedelta(minutes=25)),
        validation=TimeRange(start + timedelta(minutes=25), start + timedelta(minutes=50)),
        test=TimeRange(start + timedelta(minutes=50), start + timedelta(minutes=75)),
        holdout=TimeRange(start + timedelta(minutes=75), start + timedelta(minutes=100)),
    )
    space = ParameterSpace(
        space_id="space-float-values",
        architecture_id=architecture.architecture_id,
        dimensions={"signal.threshold": (0.1,)},
        maximum_combinations=1,
    )

    prepared = ResearchRunnerAdapter(output_root=tmp_path / "research-floats").build_plan(
        request=factory_request,
        factory_run_id="sf-run-float-values",
        manifest_id="manifest-float-values",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="random",
        experiment_count=1,
    )

    dimension = prepared.native_specification.search_space.dimensions[0]
    assert dimension.values == (Decimal("0.1"),)
    assert prepared.native_specification.resource_limits["random_sample_count"] == 1
    assert len(prepared.native_plan.jobs) == 1


def test_split_plan_rejects_overlap(factory_request) -> None:
    start = factory_request.dataset.period.start
    with pytest.raises(CompatibilityError, match="overlap"):
        ResearchSplitPlan(
            train=TimeRange(start, start + timedelta(minutes=50)),
            validation=TimeRange(start + timedelta(minutes=40), start + timedelta(minutes=60)),
            test=TimeRange(start + timedelta(minutes=60), start + timedelta(minutes=80)),
            holdout=TimeRange(start + timedelta(minutes=80), start + timedelta(minutes=100)),
        )


def test_unavailable_audit_port_fails_closed() -> None:
    with pytest.raises(ExternalComponentUnavailableError, match="not implemented"):
        UnavailableStrategyAuditPort().audit("evidence-package")


def test_results_pagination_streams_all_pages_and_rejects_cycles() -> None:
    from types import SimpleNamespace

    from strategy_factory.adapters import ResultsAggregatorAdapter

    pages = {
        0: SimpleNamespace(items=(1, 2), next_offset=2),
        2: SimpleNamespace(items=(3,), next_offset=None),
    }
    assert tuple(ResultsAggregatorAdapter._paginate(lambda offset: pages[offset])) == (
        1,
        2,
        3,
    )
    cycle = {0: SimpleNamespace(items=(), next_offset=0)}
    with pytest.raises(CompatibilityError, match="invalid next_offset"):
        tuple(ResultsAggregatorAdapter._paginate(lambda offset: cycle[offset]))


def test_research_execution_adapter_submit_status_execute_cancel_resume() -> None:
    from types import SimpleNamespace

    from strategy_factory.adapters import (
        PreparedResearchPlan,
        ResearchRunnerExecutionAdapter,
    )

    digest = "a" * 64

    class Repository:
        def __init__(self) -> None:
            self.status_value = "planned"
            self.cancel_calls = 0

        def experiment_status(self, experiment_id):
            return SimpleNamespace(value=self.status_value)

        def counts(self, experiment_id):
            return {"planned": 1}

        def list_runs(self, experiment_id):
            return (SimpleNamespace(run_id="run-1"),)

        def has_nonterminal_runs(self, experiment_id):
            return self.status_value not in {"completed", "completed_with_failures"}

        def cancel_pending(self, experiment_id, *, at):
            self.cancel_calls += 1
            self.status_value = "canceled"
            return 1

        def get_final_test_grant(self, grant_id):
            return self.final_grant

    class Runner:
        def __init__(self, repository) -> None:
            self.repository = repository
            self.fail = False
            self.prepared = None

        def prepare(self, specification, *, git):
            self.prepared = SimpleNamespace(
                specification=SimpleNamespace(fingerprint=digest),
                plan=SimpleNamespace(fingerprint=digest),
                manifest=SimpleNamespace(experiment_id="exp-1", fingerprint="b" * 64),
            )
            return self.prepared

        def authorize_final_test(self, prepared, *, candidate_configuration_hashes, issued_at=None):
            assert prepared is self.prepared
            grant = SimpleNamespace(
                grant_id="final-grant-1",
                experiment_id="exp-1",
                specification_sha256=digest,
                split_fingerprint="c" * 64,
                candidate_configuration_hashes=candidate_configuration_hashes,
                issued_at=issued_at or datetime(2026, 1, 2, tzinfo=UTC),
                consumed_at=None,
            )
            self.repository.final_grant = grant
            return grant

        def activate_final_test(self, prepared, *, grant_id, activated_at=None):
            assert prepared is self.prepared
            self.repository.final_grant = SimpleNamespace(
                **{
                    **self.repository.final_grant.__dict__,
                    "consumed_at": activated_at or datetime(2026, 1, 3, tzinfo=UTC),
                }
            )
            return ("final-run-1",)

        def activate_stage(
            self, prepared, *, role, candidate_configuration_hashes, activated_at=None
        ):
            assert prepared is self.prepared
            assert role.value == "validation"
            assert candidate_configuration_hashes == (digest,)
            return ("validation-run-1",)

        def activate_optimization_stage(
            self,
            prepared,
            *,
            stage_index,
            candidate_configuration_hashes,
            activated_at=None,
        ):
            assert prepared is self.prepared
            assert stage_index == 1
            assert candidate_configuration_hashes == (digest,)
            return ("optimization-run-1",)

        def execute(self, experiment_id, *, cancellation):
            if self.fail:
                raise RuntimeError("scheduler unavailable")
            self.repository.status_value = "completed"

    repository = Repository()
    runner = Runner(repository)
    adapter = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=object(),
    )
    prepared = PreparedResearchPlan(
        factory_plan=SimpleNamespace(plan_id="factory-plan"),
        native_specification=SimpleNamespace(fingerprint=digest),
        native_plan=SimpleNamespace(fingerprint=digest),
    )
    submission = adapter.submit(prepared)
    assert submission.experiment_id == "exp-1"
    parameters = {"risk": "0.005"}
    runner.prepared.plan.configurations = (SimpleNamespace(sha256=digest, parameters=parameters),)
    assert adapter.frozen_configuration_hashes("exp-1") == (digest,)
    frozen = adapter.frozen_configurations("exp-1")
    assert frozen[0].factory_configuration_hash != digest
    assert dict(frozen[0].parameters) == parameters
    validation_activation = adapter.activate_stage(
        "exp-1",
        role="validation",
        candidate_configuration_hashes=(digest, digest),
        activated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert validation_activation.target_role == "validation"
    assert validation_activation.run_ids == ("validation-run-1",)
    optimization_activation = adapter.activate_optimization_stage(
        "exp-1",
        stage_index=1,
        candidate_configuration_hashes=(digest,),
        activated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert optimization_activation.target_role == "optimization_001"
    assert optimization_activation.run_ids == ("optimization-run-1",)
    assert adapter.status("exp-1").run_ids == ("run-1",)
    candidate_hash = "d" * 64
    authorization = adapter.authorize_final_test(
        "exp-1",
        candidate_configuration_hashes=(candidate_hash,),
        issued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert authorization.candidate_configuration_hashes == (candidate_hash,)
    activation = adapter.activate_final_test(
        "exp-1",
        grant_id=authorization.grant_id,
        activated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert activation.run_ids == ("final-run-1",)
    assert activation.authorization.consumed_at is not None
    result = adapter.execute("exp-1")
    assert result.failure is None
    assert result.snapshot is not None and result.snapshot.status == "completed"
    assert adapter.resume("exp-1").snapshot is not None
    canceled = adapter.cancel("exp-1", at=datetime(2026, 1, 1, tzinfo=UTC))
    assert canceled.status == "canceled"


def test_research_execution_adapter_returns_structured_failure() -> None:
    from types import SimpleNamespace

    from strategy_factory.adapters import ResearchRunnerExecutionAdapter

    class Repository:
        def experiment_status(self, experiment_id):
            return SimpleNamespace(value="planned")

        def counts(self, experiment_id):
            return {}

        def list_runs(self, experiment_id):
            return ()

        def has_nonterminal_runs(self, experiment_id):
            return True

        def cancel_pending(self, experiment_id, *, at):
            return 0

        def get_final_test_grant(self, grant_id):
            raise AssertionError

    class Runner:
        def prepare(self, specification, *, git):
            raise AssertionError

        def authorize_final_test(self, prepared, *, candidate_configuration_hashes, issued_at=None):
            raise AssertionError

        def activate_final_test(self, prepared, *, grant_id, activated_at=None):
            raise AssertionError

        def activate_stage(
            self, prepared, *, role, candidate_configuration_hashes, activated_at=None
        ):
            raise AssertionError

        def activate_optimization_stage(
            self,
            prepared,
            *,
            stage_index,
            candidate_configuration_hashes,
            activated_at=None,
        ):
            raise AssertionError

        def execute(self, experiment_id, *, cancellation):
            raise RuntimeError("boom")

    adapter = ResearchRunnerExecutionAdapter(
        runner=Runner(), repository=Repository(), git_provenance=object()
    )
    result = adapter.execute("exp-1")
    assert result.snapshot is None
    assert result.failure is not None
    assert result.failure.reason_code == "RESEARCH_RUNNER_EXECUTION_FAILED"
    assert result.failure.retryable is True
    incompatible = adapter.resume("exp-1")
    assert incompatible.failure is not None or incompatible.snapshot is not None


def test_research_execution_adapter_matches_real_runner_v03(
    factory_request, architecture, tmp_path: Path
) -> None:
    import modular_research_runner as research

    from strategy_factory.adapters import ResearchRunnerExecutionAdapter
    from strategy_factory.contracts import ParameterSpace

    start = factory_request.dataset.period.start
    span = factory_request.dataset.period.end - start
    quarter = span / 4
    split = ResearchSplitPlan(
        train=TimeRange(start, start + quarter),
        validation=TimeRange(start + quarter, start + 2 * quarter),
        test=TimeRange(start + 2 * quarter, start + 3 * quarter),
        holdout=TimeRange(start + 3 * quarter, factory_request.dataset.period.end),
    )
    space = ParameterSpace(
        space_id="space-real-runner",
        architecture_id=architecture.architecture_id,
        dimensions={"target_r": (2, 3)},
        maximum_combinations=2,
    )
    preview = ResearchRunnerAdapter(output_root=tmp_path / "output").build_plan(
        request=factory_request,
        factory_run_id="sf-run-real-runner",
        manifest_id="manifest-real-runner",
        stage=FactoryStage.COARSE_SEARCH,
        architecture=architecture,
        parameter_space=space,
        split=split,
        search_method="grid",
        experiment_count=2,
    )
    repository = research.SqliteExperimentRepository(tmp_path / "research.sqlite")
    retry = research.RetryPolicy(
        max_attempts=2,
        initial_backoff_seconds=1.0,
        backoff_multiplier=2.0,
        maximum_backoff_seconds=30.0,
    )
    resume = research.ResumePolicy()
    scheduler = research.LocalResearchScheduler(
        repository=repository,
        engine=research.ModularBacktestEngineAdapter(
            lambda job: object(), run_function=lambda config: None
        ),
        retry_policy=retry,
        resume_policy=resume,
        maximum_workers=2,
    )
    runner = research.ResearchRunner(
        repository=repository,
        artifact_store=research.AtomicJsonArtifactStore(tmp_path / "artifacts"),
        plan_builder=research.ResearchPlanBuilder(tmp_path / "output"),
        scheduler=scheduler,
        backtest_engine_version="test-engine-1",
        metric_schema_version="1",
        module_api_version="1.0",
    )
    adapter = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=research.GitProvenance(branch="local-test", commit_sha="a" * 40, clean=True),
    )
    submission = adapter.submit(preview)
    assert adapter.restore_submission(preview) == submission
    snapshot = adapter.status(submission.experiment_id)
    assert snapshot.status == "planned"
    assert snapshot.run_status_counts == {"planned": 2}
    assert len(snapshot.run_ids) == 2


def test_frozen_configuration_separates_runtime_blueprint_from_strategy_identity() -> None:
    from types import SimpleNamespace

    from strategy_factory.adapters import ResearchRunnerExecutionAdapter
    from strategy_factory.canonical import canonical_sha256

    digest = "a" * 64

    class Repository:
        def experiment_status(self, experiment_id):
            return "planned"

        def counts(self, experiment_id):
            return {}

        def list_runs(self, experiment_id):
            return ()

        def has_nonterminal_runs(self, experiment_id):
            return False

        def cancel_pending(self, experiment_id, *, at):
            return 0

        def get_final_test_grant(self, experiment_id):
            return None

    configuration = SimpleNamespace(
        sha256=digest,
        parameters={
            "architecture_id": "arch-one",
            "architecture_fingerprint": "b" * 64,
            "runtime_modules": (
                {
                    "runtime_component_id": "signal",
                    "runtime_component_version": "1.0.0",
                    "import_path": "trusted.signal:SignalModule",
                    "fixed_parameters": {},
                    "source_commit_sha": "c" * 40,
                },
            ),
            "factory_run_id": "sf-run-one",
            "signal.period": 20,
        },
    )
    native = SimpleNamespace(
        specification=SimpleNamespace(fingerprint=digest),
        plan=SimpleNamespace(fingerprint=digest, configurations=(configuration,)),
        manifest=SimpleNamespace(experiment_id="exp-blueprint", fingerprint=digest),
    )

    class Runner:
        backtest_engine_version = "backtest-v1"
        metric_schema_version = "metrics-v1"
        module_api_version = "module-api-v1"

        def prepare(self, specification, **kwargs):
            return native

        def execute(self, experiment_id, *, cancellation):
            return None

        def authorize_final_test(self, *args, **kwargs):
            raise AssertionError("not used")

        def activate_final_test(self, *args, **kwargs):
            raise AssertionError("not used")

        def activate_stage(self, *args, **kwargs):
            raise AssertionError("not used")

        def activate_optimization_stage(self, *args, **kwargs):
            raise AssertionError("not used")

    adapter = ResearchRunnerExecutionAdapter(
        runner=Runner(), repository=Repository(), git_provenance=object()
    )
    adapter._prepared["exp-blueprint"] = native
    frozen = adapter.frozen_configurations("exp-blueprint")[0]

    assert dict(frozen.parameters) == {"signal.period": 20}
    assert frozen.factory_configuration_hash == canonical_sha256({"signal.period": 20})
    assert frozen.architecture_id == "arch-one"
    assert frozen.architecture_fingerprint == "b" * 64
    assert "runtime_modules" not in frozen.parameters
