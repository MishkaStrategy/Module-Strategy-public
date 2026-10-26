from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Protocol, cast

from ..research import (
    ResearchExperimentSnapshot,
    ResearchNotFound,
    ResearchRunSnapshot,
    ResearchStateInvalid,
)


class _NativeStatus(Protocol):
    @property
    def value(self) -> str: ...


class _NativeExperiment(Protocol):
    experiment_id: str
    specification_sha256: str
    status: _NativeStatus
    created_at: datetime
    updated_at: datetime
    run_counts: Mapping[_NativeStatus, int]


class _NativeRun(Protocol):
    run_id: str
    experiment_id: str
    status: _NativeStatus
    stage_id: str
    split_id: str
    dataset_id: str
    scenario_id: str
    stage_role: str
    repetition_index: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    revision: int
    lease_expires_at: datetime | None
    failure_category: _NativeStatus | None
    failure_retryable: bool | None
    engine_run_id: str | None
    bars_processed: int | None
    restart_verified: bool | None


class _NativeRepository(Protocol):
    def list_experiments(self, *, limit: int, offset: int) -> tuple[_NativeExperiment, ...]: ...

    def get_experiment(self, experiment_id: str) -> _NativeExperiment: ...

    def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[_NativeRun, ...]: ...

    def get_run(self, run_id: str) -> _NativeRun: ...


class ResearchRunnerStateReader:
    """Lazy adapter over Research Runner's explicitly read-only SQLite query port."""

    def __init__(self, state_path: str | Path) -> None:
        raw = Path(state_path).expanduser()
        if raw.is_symlink():
            raise ValueError("Research state database must not be a symlink")
        if not raw.exists() or not raw.is_file():
            raise ValueError("Research state database must be an existing regular file")
        self._state_path = raw.resolve(strict=True)
        self._repository: _NativeRepository | None = None

    def list_experiments(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResearchExperimentSnapshot, ...]:
        try:
            snapshots = self._reader().list_experiments(limit=limit, offset=offset)
        except Exception as exc:
            self._raise_native(exc)
        return tuple(self._experiment(item) for item in snapshots)

    def get_experiment(self, experiment_id: str) -> ResearchExperimentSnapshot:
        try:
            snapshot = self._reader().get_experiment(experiment_id)
        except Exception as exc:
            self._raise_native(exc)
        return self._experiment(snapshot)

    def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResearchRunSnapshot, ...]:
        try:
            snapshots = self._reader().list_runs(experiment_id, limit=limit, offset=offset)
        except Exception as exc:
            self._raise_native(exc)
        return tuple(self._run(item) for item in snapshots)

    def get_run(self, run_id: str) -> ResearchRunSnapshot:
        try:
            snapshot = self._reader().get_run(run_id)
        except Exception as exc:
            self._raise_native(exc)
        return self._run(snapshot)

    def _reader(self) -> _NativeRepository:
        if self._repository is None:
            try:
                from modular_research_runner import ReadOnlyExperimentRepository
            except ImportError as exc:
                raise ResearchStateInvalid("Research read-only dependency is unavailable") from exc
            try:
                native = ReadOnlyExperimentRepository(self._state_path)
            except Exception as exc:
                self._raise_native(exc)
            self._repository = cast(_NativeRepository, native)
        return self._repository

    @staticmethod
    def _experiment(snapshot: _NativeExperiment) -> ResearchExperimentSnapshot:
        return ResearchExperimentSnapshot(
            experiment_id=snapshot.experiment_id,
            specification_sha256=snapshot.specification_sha256,
            native_status=snapshot.status.value,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            run_counts={status.value: count for status, count in snapshot.run_counts.items()},
        )

    @staticmethod
    def _run(snapshot: _NativeRun) -> ResearchRunSnapshot:
        failure = snapshot.failure_category
        return ResearchRunSnapshot(
            run_id=snapshot.run_id,
            experiment_id=snapshot.experiment_id,
            native_status=snapshot.status.value,
            stage_id=snapshot.stage_id,
            split_id=snapshot.split_id,
            dataset_id=snapshot.dataset_id,
            scenario_id=snapshot.scenario_id,
            stage_role=snapshot.stage_role,
            repetition_index=snapshot.repetition_index,
            attempt_count=snapshot.attempt_count,
            max_attempts=snapshot.max_attempts,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            available_at=snapshot.available_at,
            revision=snapshot.revision,
            lease_expires_at=snapshot.lease_expires_at,
            failure_category=failure.value if failure is not None else None,
            failure_retryable=snapshot.failure_retryable,
            engine_run_id=snapshot.engine_run_id,
            bars_processed=snapshot.bars_processed,
            restart_verified=snapshot.restart_verified,
        )

    @staticmethod
    def _raise_native(exc: Exception) -> NoReturn:
        try:
            from modular_research_runner import (
                ReadOnlyRepositoryError,
                ReadOnlyRepositoryNotFound,
            )
        except ImportError as import_exc:
            raise ResearchStateInvalid(
                "Research read-only dependency is unavailable"
            ) from import_exc
        if isinstance(exc, ReadOnlyRepositoryNotFound):
            raise ResearchNotFound("Research resource was not found") from exc
        if isinstance(exc, ReadOnlyRepositoryError):
            raise ResearchStateInvalid("Research durable state failed validation") from exc
        raise ResearchStateInvalid("Research durable state could not be read") from exc
