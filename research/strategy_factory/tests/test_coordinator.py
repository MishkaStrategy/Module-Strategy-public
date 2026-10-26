from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from strategy_factory.contracts import (
    FactoryMode,
    FactoryStage,
    FactoryStatus,
    TimeRange,
)
from strategy_factory.coordinator import (
    FactoryCoordinator,
    FactoryStageContext,
    StageBudgetRequest,
    StageOutcome,
)
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.errors import ExternalComponentUnavailableError, StageBlockedError
from strategy_factory.run import StrategyFactoryRun


def make_run(factory_request, tmp_path: Path) -> StrategyFactoryRun:
    return StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path,
        module_versions={"fvg": "1.0.0"},
    )


def analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )


class RecordingHandler:
    def __init__(
        self,
        stage: FactoryStage,
        *,
        budget: StageBudgetRequest | None = None,
    ) -> None:
        self.stage = stage
        self.budget = budget or StageBudgetRequest()
        self.calls = 0

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        assert context.checkpoint.stage is self.stage
        return self.budget

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        self.calls += 1
        return StageOutcome(
            artifact_type=f"{self.stage.value}_evidence",
            artifact={
                "stage": self.stage.value,
                "run_id": context.checkpoint.run_id,
                "sequence": context.checkpoint.stage_sequence,
            },
            reason_codes=(f"{self.stage.value.upper()}_PASSED",),
            subject_id=f"subject-{self.stage.value}",
        )


def all_handlers() -> dict[FactoryStage, RecordingHandler]:
    handlers: dict[FactoryStage, RecordingHandler] = {}
    for stage in FactoryStage:
        if stage in {FactoryStage.DATA_GATE, FactoryStage.INSTRUMENT_PROFILE}:
            continue
        budget = StageBudgetRequest()
        if stage is FactoryStage.ARCHITECTURE_GENERATION:
            budget = StageBudgetRequest(architectures=2)
        elif stage is FactoryStage.COARSE_SEARCH:
            budget = StageBudgetRequest(experiments=2)
        elif stage is FactoryStage.SIMPLICITY_SELECTION:
            budget = StageBudgetRequest(finalists=1)
        handlers[stage] = RecordingHandler(stage, budget=budget)
    return handlers


def test_coordinator_completes_full_create_lifecycle(factory_request, tmp_path: Path) -> None:
    handlers = all_handlers()
    run = make_run(factory_request, tmp_path)
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=analyzer(),
        handlers=handlers,
    )

    results = coordinator.run_until_stop(maximum_steps=32)

    assert results[-1].checkpoint.status is FactoryStatus.COMPLETED
    assert results[-1].checkpoint.stage is FactoryStage.PAPER_ADMISSION
    assert results[-1].checkpoint.stage_sequence == len(tuple(FactoryStage))
    assert all(handler.calls == 1 for handler in handlers.values())
    budget = results[-1].checkpoint.budget_state
    assert budget["experiments_reserved"] == 2
    assert budget["architectures_generated"] == 2
    assert budget["finalists_selected"] == 1
    assert budget["stage_reservations"][FactoryStage.COARSE_SEARCH.value]["experiments"] == 2
    assert "paper_admission" in results[-1].checkpoint.completed_artifacts


def test_validate_mode_skips_optimization_without_reusing_holdout(
    factory_request,
    tmp_path: Path,
) -> None:
    period = factory_request.dataset.period
    midpoint = period.start + (period.end - period.start) / 2
    request = replace(
        factory_request,
        mode=FactoryMode.VALIDATE,
        baseline_strategy_id="strategy-existing-v1",
        explicit_holdout=TimeRange(midpoint, period.end),
    )
    active = {
        stage: RecordingHandler(stage)
        for stage in (
            FactoryStage.REGIME_DISCOVERY,
            FactoryStage.WALK_FORWARD,
            FactoryStage.ROBUSTNESS,
            FactoryStage.HOLDOUT,
            FactoryStage.SIMPLICITY_SELECTION,
            FactoryStage.FINAL_AUDIT,
            FactoryStage.PACKAGE,
            FactoryStage.PAPER_ADMISSION,
        )
    }
    coordinator = FactoryCoordinator(
        run=make_run(request, tmp_path),
        dataset_analyzer=analyzer(),
        handlers=active,
    )

    results = coordinator.run_until_stop(maximum_steps=32)

    assert results[-1].checkpoint.status is FactoryStatus.COMPLETED
    skipped = {item.stage for item in results if item.action == "skipped"}
    assert FactoryStage.HYPOTHESIS_GENERATION in skipped
    assert FactoryStage.COARSE_SEARCH in skipped
    assert FactoryStage.LOCAL_REFINEMENT in skipped
    assert FactoryStage.WALK_FORWARD not in skipped
    assert FactoryStage.SIMPLICITY_SELECTION not in skipped
    assert "coarse_search" not in results[-1].checkpoint.completed_artifacts
    assert all(handler.calls == 1 for handler in active.values())


class AmbiguousBatchHandler:
    def __init__(self) -> None:
        self.calls = 0

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest(experiments=2)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        self.calls += 1
        first_registration = context.register_batch(
            batch_id="batch-regime-1",
            batch_fingerprint="b" * 64,
        )
        if self.calls == 1:
            assert first_registration
            raise StageBlockedError("lost Research Runner acknowledgement")
        assert not first_registration
        return StageOutcome(
            artifact_type="regime_evidence",
            artifact={"batch_id": "batch-regime-1"},
            reason_codes=("BATCH_RECONCILED",),
            subject_id="batch-regime-1",
        )


def test_blocked_stage_resumes_without_double_budget_or_batch(
    factory_request,
    tmp_path: Path,
) -> None:
    run = make_run(factory_request, tmp_path)
    handler = AmbiguousBatchHandler()
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=analyzer(),
        handlers={FactoryStage.REGIME_DISCOVERY: handler},
    )

    assert coordinator.step().action == "completed"  # data gate
    blocked = coordinator.step()
    assert blocked.action == "blocked"
    assert blocked.checkpoint.status is FactoryStatus.BLOCKED
    assert blocked.checkpoint.budget_state["experiments_reserved"] == 2
    assert coordinator.step().action == "waiting"

    run.resume()
    completed = coordinator.step()
    assert completed.action == "completed"
    assert completed.checkpoint.stage is FactoryStage.HYPOTHESIS_GENERATION
    assert completed.checkpoint.budget_state["experiments_reserved"] == 2
    assert completed.checkpoint.submitted_batches == {"batch-regime-1": "b" * 64}
    assert handler.calls == 2


def test_missing_handler_blocks_and_can_be_replaced(factory_request, tmp_path: Path) -> None:
    run = make_run(factory_request, tmp_path)
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=analyzer(),
        handlers={},
    )
    coordinator.step()
    missing = coordinator.step()
    assert missing.action == "blocked"
    assert missing.error_type == "MissingHandler"

    run.resume()
    handler = RecordingHandler(FactoryStage.REGIME_DISCOVERY)
    coordinator.set_handler(FactoryStage.REGIME_DISCOVERY, handler)
    assert coordinator.step().action == "completed"
    assert handler.calls == 1


class ExhaustingHandler:
    executed = False

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest(experiments=101)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        self.executed = True
        raise AssertionError("execution must not begin after budget exhaustion")


def test_budget_exhaustion_stops_before_external_work(factory_request, tmp_path: Path) -> None:
    run = make_run(factory_request, tmp_path)
    handler = ExhaustingHandler()
    coordinator = FactoryCoordinator(
        run=run,
        dataset_analyzer=analyzer(),
        handlers={FactoryStage.REGIME_DISCOVERY: handler},
    )
    coordinator.step()
    result = coordinator.step()
    assert result.action == "budget_exhausted"
    assert result.checkpoint.status is FactoryStatus.BUDGET_EXHAUSTED
    assert result.checkpoint.budget_state["experiments_reserved"] == 0
    assert handler.executed is False


class UnavailableHandler(RecordingHandler):
    def execute(self, context: FactoryStageContext) -> StageOutcome:
        raise ExternalComponentUnavailableError("Results Aggregator unavailable")


def test_external_component_failure_blocks_instead_of_failing(
    factory_request,
    tmp_path: Path,
) -> None:
    coordinator = FactoryCoordinator(
        run=make_run(factory_request, tmp_path),
        dataset_analyzer=analyzer(),
        handlers={FactoryStage.REGIME_DISCOVERY: UnavailableHandler(FactoryStage.REGIME_DISCOVERY)},
    )
    coordinator.step()
    result = coordinator.step()
    assert result.action == "blocked"
    assert result.checkpoint.status is FactoryStatus.BLOCKED
    assert result.error_type == "ExternalComponentUnavailableError"


class InvalidOutcomeHandler(RecordingHandler):
    def execute(self, context: FactoryStageContext):
        return {"not": "a StageOutcome"}


def test_invalid_handler_contract_fails_closed(factory_request, tmp_path: Path) -> None:
    coordinator = FactoryCoordinator(
        run=make_run(factory_request, tmp_path),
        dataset_analyzer=analyzer(),
        handlers={
            FactoryStage.REGIME_DISCOVERY: InvalidOutcomeHandler(FactoryStage.REGIME_DISCOVERY)
        },
    )
    coordinator.step()
    result = coordinator.step()
    assert result.action == "failed"
    assert result.checkpoint.status is FactoryStatus.FAILED
    assert result.error_type == "ContractError"
