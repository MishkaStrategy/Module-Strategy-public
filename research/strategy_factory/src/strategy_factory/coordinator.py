"""Fail-closed staged orchestration for Strategy Factory runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import (
    FactoryMode,
    FactoryStage,
    FactoryStatus,
    StrategyFactoryCheckpoint,
    StrategyFactoryManifest,
    StrategyFactoryRequest,
)
from .dataset import DatasetAnalyzer
from .errors import (
    BudgetExhaustedError,
    CompatibilityError,
    ContractError,
    ExternalComponentUnavailableError,
    StageBlockedError,
    StrategyFactoryError,
)
from .run import StrategyFactoryRun


@dataclass(frozen=True, slots=True)
class StageBudgetRequest:
    """Capacity that must be reserved before a stage may perform external work."""

    experiments: int = 0
    architectures: int = 0
    finalists: int | None = None

    def __post_init__(self) -> None:
        for name in ("experiments", "architectures"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{name} budget request must be a non-negative int")
        if self.finalists is not None and (
            isinstance(self.finalists, bool)
            or not isinstance(self.finalists, int)
            or self.finalists < 0
        ):
            raise ContractError("finalists budget request must be a non-negative int or None")


@dataclass(frozen=True, slots=True)
class FactoryStageContext:
    """Immutable stage identity plus narrow idempotency operations."""

    run: StrategyFactoryRun
    request: StrategyFactoryRequest
    manifest: StrategyFactoryManifest
    checkpoint: StrategyFactoryCheckpoint

    def register_batch(self, *, batch_id: str, batch_fingerprint: str) -> bool:
        return self.run.register_batch(
            batch_id=batch_id,
            batch_fingerprint=batch_fingerprint,
        )

    def process_event(self, event_id: str) -> bool:
        return self.run.process_event(event_id)

    def artifact_id(self, key: str) -> str:
        if not isinstance(key, str) or not key.strip():
            raise ContractError("artifact key must be a non-blank string")
        artifact_id = self.checkpoint.completed_artifacts.get(key)
        if artifact_id is None:
            raise CompatibilityError(f"required artifact is unavailable: {key}")
        return artifact_id

    def load_artifact(self, key: str) -> Any:
        return self.run.load_artifact(self.artifact_id(key))

    def store_artifact(self, *, artifact_type: str, value: Any) -> str:
        return self.run.store_artifact(artifact_type=artifact_type, value=value)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """One accepted stage artifact and its explainable evidence."""

    artifact_type: str
    artifact: Any
    reason_codes: tuple[str, ...]
    subject_id: str
    architectures_generated: int = 0
    finalists_selected: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_type, str) or not self.artifact_type.strip():
            raise ContractError("stage artifact_type must be a non-blank string")
        if not isinstance(self.subject_id, str) or not self.subject_id.strip():
            raise ContractError("stage subject_id must be a non-blank string")
        if not self.reason_codes or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reason_codes
        ):
            raise ContractError("stage outcome requires non-blank reason codes")
        if (
            isinstance(self.architectures_generated, bool)
            or not isinstance(self.architectures_generated, int)
            or self.architectures_generated < 0
        ):
            raise ContractError("architectures_generated must be a non-negative int")
        if self.finalists_selected is not None and (
            isinstance(self.finalists_selected, bool)
            or not isinstance(self.finalists_selected, int)
            or self.finalists_selected < 0
        ):
            raise ContractError("finalists_selected must be a non-negative int or None")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))


class StageHandler(Protocol):
    """Port implemented by stage-specific planners and integration adapters."""

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest: ...

    def execute(self, context: FactoryStageContext) -> StageOutcome: ...


@dataclass(frozen=True, slots=True)
class CoordinatorStepResult:
    checkpoint: StrategyFactoryCheckpoint
    stage: FactoryStage
    action: str
    artifact_id: str | None = None
    error_type: str | None = None


_STAGE_GROUPS: Mapping[int, tuple[FactoryStage, ...]] = {
    0: (
        FactoryStage.DATA_GATE,
        FactoryStage.INSTRUMENT_PROFILE,
        FactoryStage.REGIME_DISCOVERY,
    ),
    1: (
        FactoryStage.HYPOTHESIS_GENERATION,
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
    ),
    2: (FactoryStage.COARSE_SEARCH,),
    3: (FactoryStage.LOCAL_REFINEMENT,),
    4: (FactoryStage.WALK_FORWARD,),
    5: (FactoryStage.ROBUSTNESS,),
    6: (FactoryStage.HOLDOUT,),
    7: (FactoryStage.SIMPLICITY_SELECTION,),
    8: (
        FactoryStage.FINAL_AUDIT,
        FactoryStage.PACKAGE,
        FactoryStage.PAPER_ADMISSION,
    ),
}

_VALIDATE_STAGES = frozenset(
    {
        FactoryStage.DATA_GATE,
        FactoryStage.INSTRUMENT_PROFILE,
        FactoryStage.REGIME_DISCOVERY,
        FactoryStage.WALK_FORWARD,
        FactoryStage.ROBUSTNESS,
        FactoryStage.HOLDOUT,
        FactoryStage.SIMPLICITY_SELECTION,
        FactoryStage.FINAL_AUDIT,
        FactoryStage.PACKAGE,
        FactoryStage.PAPER_ADMISSION,
    }
)


class FactoryCoordinator:
    """Advance a run one durable stage at a time without owning backtest computation."""

    def __init__(
        self,
        *,
        run: StrategyFactoryRun,
        dataset_analyzer: DatasetAnalyzer,
        handlers: Mapping[FactoryStage, StageHandler],
    ) -> None:
        self._run = run
        self._dataset_analyzer = dataset_analyzer
        self._handlers = dict(handlers)

    @property
    def stage_groups(self) -> Mapping[int, tuple[FactoryStage, ...]]:
        return _STAGE_GROUPS

    def set_handler(self, stage: FactoryStage, handler: StageHandler) -> None:
        if not isinstance(stage, FactoryStage):
            raise TypeError("stage must be FactoryStage")
        self._handlers[stage] = handler

    def step(self) -> CoordinatorStepResult:
        checkpoint = self._run.initialize()
        stage = checkpoint.stage
        if checkpoint.status in _STOP_STATUSES:
            return CoordinatorStepResult(checkpoint, stage, "stopped")
        if checkpoint.status in {FactoryStatus.PAUSED, FactoryStatus.BLOCKED}:
            return CoordinatorStepResult(checkpoint, stage, "waiting")

        skipped = self._skip_inapplicable_stages()
        if skipped is not None:
            return skipped
        stage = self._run.checkpoint.stage

        if stage is FactoryStage.DATA_GATE:
            analysis = self._run.run_data_gate(self._dataset_analyzer)
            action = "dataset_rejected" if analysis.profile is None else "completed"
            return CoordinatorStepResult(self._run.checkpoint, stage, action)

        handler = self._handlers.get(stage)
        if handler is None:
            checkpoint = self._run.block(
                reason_codes=("STAGE_HANDLER_MISSING",),
                evidence={"stage": stage.value},
            )
            return CoordinatorStepResult(checkpoint, stage, "blocked", error_type="MissingHandler")

        context = FactoryStageContext(
            run=self._run,
            request=self._run.request,
            manifest=self._run.manifest,
            checkpoint=self._run.checkpoint,
        )
        try:
            self._run.ensure_budget_time()
            reservation = handler.budget_request(context)
            if not isinstance(reservation, StageBudgetRequest):
                raise ContractError("stage handler returned an invalid budget request")
            self._run.reserve_budget(stage=stage, request=reservation)
            outcome = handler.execute(
                FactoryStageContext(
                    run=self._run,
                    request=self._run.request,
                    manifest=self._run.manifest,
                    checkpoint=self._run.checkpoint,
                )
            )
            if not isinstance(outcome, StageOutcome):
                raise ContractError("stage handler returned an invalid outcome")
            if outcome.architectures_generated or outcome.finalists_selected is not None:
                self._run.reserve_budget(
                    stage=stage,
                    request=StageBudgetRequest(
                        architectures=outcome.architectures_generated,
                        finalists=outcome.finalists_selected,
                    ),
                )
            artifact_id = self._run.complete_stage(
                stage=stage,
                artifact_type=outcome.artifact_type,
                artifact=outcome.artifact,
                reason_codes=outcome.reason_codes,
                subject_id=outcome.subject_id,
            )
            return CoordinatorStepResult(
                self._run.checkpoint,
                stage,
                "completed",
                artifact_id=artifact_id,
            )
        except BudgetExhaustedError as exc:
            checkpoint = self._run.exhaust_budget(
                reason_codes=("BUDGET_EXHAUSTED",),
                evidence={"stage": stage.value, "detail": str(exc)},
            )
            return CoordinatorStepResult(
                checkpoint,
                stage,
                "budget_exhausted",
                error_type=type(exc).__name__,
            )
        except (ExternalComponentUnavailableError, StageBlockedError) as exc:
            checkpoint = self._run.block(
                reason_codes=("EXTERNAL_DEPENDENCY_BLOCKED",),
                evidence={
                    "stage": stage.value,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
            return CoordinatorStepResult(
                checkpoint,
                stage,
                "blocked",
                error_type=type(exc).__name__,
            )
        except (CompatibilityError, ContractError, StrategyFactoryError) as exc:
            checkpoint = self._run.fail(
                reason_codes=("STAGE_EXECUTION_FAILED",),
                evidence={
                    "stage": stage.value,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
            return CoordinatorStepResult(
                checkpoint,
                stage,
                "failed",
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # pragma: no cover - defensive isolation boundary
            checkpoint = self._run.fail(
                reason_codes=("UNEXPECTED_STAGE_FAILURE",),
                evidence={
                    "stage": stage.value,
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
            return CoordinatorStepResult(
                checkpoint,
                stage,
                "failed",
                error_type=type(exc).__name__,
            )

    def run_until_stop(self, *, maximum_steps: int = 100) -> tuple[CoordinatorStepResult, ...]:
        if isinstance(maximum_steps, bool) or not isinstance(maximum_steps, int):
            raise TypeError("maximum_steps must be int")
        if maximum_steps < 1:
            raise ValueError("maximum_steps must be positive")
        results: list[CoordinatorStepResult] = []
        for _ in range(maximum_steps):
            result = self.step()
            results.append(result)
            if result.checkpoint.status in _STOP_STATUSES | {
                FactoryStatus.PAUSED,
                FactoryStatus.BLOCKED,
            }:
                return tuple(results)
        raise CompatibilityError("coordinator step limit reached before a durable stop")

    def _skip_inapplicable_stages(self) -> CoordinatorStepResult | None:
        stage = self._run.checkpoint.stage
        if self._run.request.mode is not FactoryMode.VALIDATE or stage in _VALIDATE_STAGES:
            return None
        checkpoint = self._run.skip_stage(
            stage=stage,
            reason_codes=("MODE_VALIDATE_NO_REOPTIMIZATION",),
            subject_id=self._run.request.baseline_strategy_id or self._run.request.request_id,
        )
        return CoordinatorStepResult(checkpoint, stage, "skipped")


_STOP_STATUSES = frozenset(
    {
        FactoryStatus.CANCELED,
        FactoryStatus.COMPLETED,
        FactoryStatus.FAILED,
        FactoryStatus.DATASET_REJECTED,
        FactoryStatus.BUDGET_EXHAUSTED,
        FactoryStatus.CORRUPTED,
    }
)
