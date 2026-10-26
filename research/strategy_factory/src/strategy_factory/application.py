"""Application service for starting and resuming complete Strategy Factory runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import (
    FactoryStage,
    FactoryStatus,
    StrategyFactoryCheckpoint,
    StrategyFactoryRequest,
)
from .coordinator import CoordinatorStepResult, FactoryCoordinator, StageHandler
from .dataset import DatasetAnalyzer
from .errors import CompatibilityError, ContractError
from .run import StrategyFactoryRun


class RuntimeAssembly(Protocol):
    """Code-owned composition boundary for stage handlers and dataset policy."""

    def dataset_analyzer(self, request: StrategyFactoryRequest) -> DatasetAnalyzer: ...

    def stage_handlers(
        self,
        run: StrategyFactoryRun,
    ) -> Mapping[FactoryStage, StageHandler]: ...


@dataclass(frozen=True, slots=True)
class ApplicationRunResult:
    """Durable result of one foreground application invocation."""

    run_id: str
    checkpoint: StrategyFactoryCheckpoint
    steps: tuple[CoordinatorStepResult, ...]

    @property
    def completed(self) -> bool:
        return self.checkpoint.status is FactoryStatus.COMPLETED


class StrategyFactoryApplication:
    """Run lifecycle stages in the foreground using an injected trusted assembly."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        assembly: RuntimeAssembly,
    ) -> None:
        if not callable(getattr(assembly, "dataset_analyzer", None)):
            raise TypeError("assembly must implement dataset_analyzer()")
        if not callable(getattr(assembly, "stage_handlers", None)):
            raise TypeError("assembly must implement stage_handlers()")
        self._state_root = Path(state_root)
        self._assembly = assembly

    def start(
        self,
        *,
        request: StrategyFactoryRequest,
        commit_sha: str,
        module_versions: Mapping[str, str],
        maximum_steps: int = 100,
    ) -> ApplicationRunResult:
        if not isinstance(request, StrategyFactoryRequest):
            raise TypeError("request must be StrategyFactoryRequest")
        normalized_versions = self._module_versions(module_versions)
        run = StrategyFactoryRun(
            request=request,
            commit_sha=commit_sha,
            state_root=self._state_root,
            module_versions=normalized_versions,
        )
        return self._execute(run, maximum_steps=maximum_steps)

    def continue_run(
        self,
        *,
        run_id: str,
        maximum_steps: int = 100,
        resume_blocked: bool = True,
    ) -> ApplicationRunResult:
        run = StrategyFactoryRun.restore_existing(
            state_root=self._state_root,
            run_id=run_id,
        )
        checkpoint = run.restore()
        if checkpoint.status in {FactoryStatus.PAUSED, FactoryStatus.BLOCKED}:
            if not resume_blocked:
                return ApplicationRunResult(run.run_id, checkpoint, ())
            run.resume()
        elif checkpoint.status in _TERMINAL:
            return ApplicationRunResult(run.run_id, checkpoint, ())
        return self._execute(run, maximum_steps=maximum_steps)

    def cancel(self, *, run_id: str) -> StrategyFactoryCheckpoint:
        run = StrategyFactoryRun.restore_existing(
            state_root=self._state_root,
            run_id=run_id,
        )
        run.restore()
        return run.cancel()

    def _execute(
        self,
        run: StrategyFactoryRun,
        *,
        maximum_steps: int,
    ) -> ApplicationRunResult:
        analyzer = self._assembly.dataset_analyzer(run.request)
        if not isinstance(analyzer, DatasetAnalyzer):
            raise ContractError("runtime assembly returned an invalid DatasetAnalyzer")
        handlers = self._assembly.stage_handlers(run)
        if not isinstance(handlers, Mapping):
            raise ContractError("runtime assembly stage_handlers must return a mapping")
        normalized: dict[FactoryStage, StageHandler] = {}
        for stage, handler in handlers.items():
            if not isinstance(stage, FactoryStage):
                raise ContractError("runtime assembly handler key must be FactoryStage")
            if not callable(getattr(handler, "budget_request", None)) or not callable(
                getattr(handler, "execute", None)
            ):
                raise ContractError(f"invalid stage handler for {stage.value}")
            normalized[stage] = handler
        coordinator = FactoryCoordinator(
            run=run,
            dataset_analyzer=analyzer,
            handlers=normalized,
        )
        steps = coordinator.run_until_stop(maximum_steps=maximum_steps)
        return ApplicationRunResult(
            run_id=run.run_id,
            checkpoint=run.checkpoint,
            steps=steps,
        )

    @staticmethod
    def _module_versions(values: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(values, Mapping):
            raise TypeError("module_versions must be a mapping")
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
            ):
                raise CompatibilityError("module_versions must contain non-blank string identities")
            normalized[key] = value
        return dict(sorted(normalized.items()))


_TERMINAL = frozenset(
    {
        FactoryStatus.CANCELED,
        FactoryStatus.COMPLETED,
        FactoryStatus.FAILED,
        FactoryStatus.DATASET_REJECTED,
        FactoryStatus.BUDGET_EXHAUSTED,
        FactoryStatus.CORRUPTED,
    }
)
