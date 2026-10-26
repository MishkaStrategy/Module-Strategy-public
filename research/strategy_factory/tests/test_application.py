from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from strategy_factory.application import StrategyFactoryApplication
from strategy_factory.contracts import FactoryStage, FactoryStatus
from strategy_factory.coordinator import StageBudgetRequest, StageOutcome
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy


class EchoHandler:
    def __init__(self, stage: FactoryStage) -> None:
        self.stage = stage

    def budget_request(self, context):
        del context
        return StageBudgetRequest()

    def execute(self, context):
        return StageOutcome(
            artifact_type=f"application_{self.stage.value}",
            artifact={
                "schema_version": 1,
                "stage": self.stage.value,
                "run_id": context.checkpoint.run_id,
            },
            reason_codes=("APPLICATION_STAGE_COMPLETED",),
            subject_id=context.checkpoint.run_id,
        )


class Assembly:
    def __init__(self, stages) -> None:
        self.stages = tuple(stages)

    def dataset_analyzer(self, request):
        del request
        return DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=Decimal("0"),
            )
        )

    def stage_handlers(self, run):
        del run
        return {stage: EchoHandler(stage) for stage in self.stages}


def _non_data_stages():
    return tuple(
        stage
        for stage in FactoryStage
        if stage not in {FactoryStage.DATA_GATE, FactoryStage.INSTRUMENT_PROFILE}
    )


def test_application_runs_complete_foreground_lifecycle(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly(_non_data_stages()),
    )

    result = application.start(
        request=factory_request,
        commit_sha="a" * 40,
        module_versions={"fvg": "1.0.0"},
    )

    assert result.completed
    assert result.checkpoint.status is FactoryStatus.COMPLETED
    assert result.steps[0].stage is FactoryStage.DATA_GATE
    assert result.steps[-1].stage is FactoryStage.PAPER_ADMISSION

    repeated = application.continue_run(run_id=result.run_id)
    assert repeated.checkpoint.status is FactoryStatus.COMPLETED
    assert repeated.steps == ()


def test_application_resumes_blocked_run_with_new_assembly(factory_request, tmp_path: Path) -> None:
    incomplete = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly((FactoryStage.REGIME_DISCOVERY,)),
    )
    blocked = incomplete.start(
        request=factory_request,
        commit_sha="a" * 40,
        module_versions={"fvg": "1.0.0"},
    )
    assert blocked.checkpoint.status is FactoryStatus.BLOCKED
    assert blocked.checkpoint.stage is FactoryStage.HYPOTHESIS_GENERATION

    complete = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly(_non_data_stages()),
    )
    resumed = complete.continue_run(run_id=blocked.run_id)

    assert resumed.checkpoint.status is FactoryStatus.COMPLETED
    assert resumed.steps[0].stage is FactoryStage.HYPOTHESIS_GENERATION
    assert resumed.steps[-1].stage is FactoryStage.PAPER_ADMISSION


def test_application_cancel_is_durable(factory_request, tmp_path: Path) -> None:
    application = StrategyFactoryApplication(
        state_root=tmp_path,
        assembly=Assembly((FactoryStage.REGIME_DISCOVERY,)),
    )
    blocked = application.start(
        request=factory_request,
        commit_sha="a" * 40,
        module_versions={"fvg": "1.0.0"},
    )

    canceled = application.cancel(run_id=blocked.run_id)
    assert canceled.status is FactoryStatus.CANCELED
    repeated = application.continue_run(run_id=blocked.run_id)
    assert repeated.checkpoint.status is FactoryStatus.CANCELED
    assert repeated.steps == ()
