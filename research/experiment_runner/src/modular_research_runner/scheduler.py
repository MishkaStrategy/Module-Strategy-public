from __future__ import annotations

import math
import multiprocessing
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import (
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .effective_config import read_legacy_v2_sidecar_for_job, read_sidecar_for_job
from .engine import (
    BacktestJob,
    BacktestRunReference,
    EngineContractError,
    ModularBacktestEngineAdapter,
)
from .models import ExperimentStatus, SpecificationError
from .output_manifest import verify_output_manifest
from .policies import ResumePolicy, RetryPolicy
from .repository import RunLease, SqliteExperimentRepository
from .state import (
    FailureCategory,
    FailureRecord,
    RunState,
    RunStatus,
    StateTransitionError,
)


class SchedulerError(RuntimeError):
    """Raised when the local scheduler cannot make safe forward progress."""


class ExecutorMode(StrEnum):
    THREAD = "thread"
    PROCESS = "process"


class Clock(Protocol):
    def __call__(self) -> datetime: ...


class FailureClassifier(Protocol):
    def __call__(self, error: BaseException) -> FailureCategory: ...


class ResultVerifier(Protocol):
    def __call__(self, result: BacktestRunReference) -> bool: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _new_lease_token() -> str:
    return secrets.token_urlsafe(32)


def verify_output_directory(
    result: BacktestRunReference,
    *,
    job: BacktestJob | None = None,
) -> bool:
    """Verify output inventory and any effective-config sidecar bound to a result."""

    if not verify_output_manifest(result):
        return False
    reference = result.effective_config_artifact
    if reference is None:
        return True
    try:
        if reference.sidecar_root is None:
            if job is None or result.requested_run_id != job.run_id:
                return False
            job_root = job.output_directory
        else:
            job_root = Path(reference.sidecar_root)
        if job_root.is_symlink() or not job_root.is_dir():
            return False
        resolved_job_root = job_root.resolve(strict=True)
        if not job_root.is_absolute() or resolved_job_root != job_root:
            return False
        result_root = result.output_directory.resolve(strict=True)
        result_root.relative_to(resolved_job_root)
        if reference.sidecar_root is None:
            _payload, document = read_legacy_v2_sidecar_for_job(resolved_job_root, reference)
            if job is None or not _effective_context_matches_job(document, job, result):
                return False
        else:
            _payload, document = read_sidecar_for_job(resolved_job_root, reference)
            if (job is None or job.period is None
                    or resolved_job_root != job.output_directory.resolve(strict=True)):
                return False
            if not _effective_context_matches_job(document, job, result):
                return False
    except (OSError, ValueError, SpecificationError):
        return False
    context = document["context"]
    engine_run_id = context.get("engine_run_id")
    run_id = context.get("run_id")
    return (
        isinstance(engine_run_id, str)
        and engine_run_id == result.engine_run_id
        and isinstance(run_id, str)
        and run_id == result.requested_run_id
    )


def _effective_context_matches_job(
    document: Mapping[str, Any], job: BacktestJob, result: BacktestRunReference
) -> bool:
    context = document["context"]
    if (
        result.requested_run_id != job.run_id
        or context["run_id"] != job.run_id
        or context["engine_run_id"] != result.engine_run_id
        or context["experiment_id"] != job.experiment_id
        or context["stage_id"] != job.stage_id
        or context["stage_role"] != job.stage_role
        or context["split_id"] != job.split_id
        or context["dataset_id"] != job.dataset_id
        or context["scenario_id"] != job.scenario_id
        or context["seed"] != job.seed
        or context["repetition_index"] != job.repetition_index
    ):
        return False
    configuration_sha256 = job.metadata.get("configuration_sha256")
    if (
        not isinstance(configuration_sha256, str)
        or len(configuration_sha256) != 64
        or any(character not in "0123456789abcdef" for character in configuration_sha256)
        or context["configuration_sha256"] != configuration_sha256
    ):
        return False
    if job.period is not None:
        expected_start = (
            job.period.start.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        expected_end = (
            job.period.end.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        if context["period_start"] != expected_start or context["period_end"] != expected_end:
            return False
    return True


def default_failure_classifier(error: BaseException) -> FailureCategory:
    if isinstance(error, MemoryError):
        return FailureCategory.RESOURCE
    if isinstance(error, TimeoutError | ConnectionError | InterruptedError):
        return FailureCategory.TRANSIENT
    if isinstance(error, EngineContractError):
        return FailureCategory.ENGINE
    if isinstance(error, ValueError | TypeError | FileNotFoundError | PermissionError):
        return FailureCategory.INPUT
    if isinstance(error, OSError):
        return FailureCategory.RESOURCE
    return FailureCategory.UNKNOWN


class CancellationToken:
    """Thread-safe cancellation token with a linearized publication boundary."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._publication_lock = threading.RLock()

    @property
    def canceled(self) -> bool:
        with self._publication_lock:
            return self._event.is_set()

    def cancel(self) -> None:
        with self._publication_lock:
            self._event.set()

    @contextmanager
    def publication_guard(self) -> Iterator[bool]:
        """Serialize cancellation against durable result publication."""

        with self._publication_lock:
            yield not self._event.is_set()


@dataclass(frozen=True, slots=True)
class SchedulerSummary:
    experiment_id: str
    status: ExperimentStatus
    runs: tuple[RunState, ...]

    @property
    def counts(self) -> dict[RunStatus, int]:
        result: dict[RunStatus, int] = {}
        for run in self.runs:
            result[run.status] = result.get(run.status, 0) + 1
        return result


@dataclass(slots=True)
class _ActiveRun:
    job: BacktestJob
    lease: RunLease
    future: Future[BacktestRunReference]
    next_heartbeat: datetime


class LocalResearchScheduler:
    """Bounded local scheduler with fenced durable claims and recovery."""

    def __init__(
        self,
        *,
        repository: SqliteExperimentRepository,
        engine: ModularBacktestEngineAdapter,
        retry_policy: RetryPolicy | None = None,
        resume_policy: ResumePolicy | None = None,
        maximum_workers: int = 1,
        executor_mode: ExecutorMode = ExecutorMode.THREAD,
        clock: Clock = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        failure_classifier: FailureClassifier = default_failure_classifier,
        result_verifier: ResultVerifier = verify_output_directory,
        poll_interval_seconds: float = 0.05,
        session_id_factory: Callable[[], str] = _new_session_id,
        lease_token_factory: Callable[[], str] = _new_lease_token,
    ) -> None:
        if (
            not isinstance(maximum_workers, int)
            or isinstance(maximum_workers, bool)
            or maximum_workers < 1
        ):
            raise SpecificationError("maximum_workers must be positive")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise SpecificationError("retry_policy must be a RetryPolicy")
        if resume_policy is not None and not isinstance(resume_policy, ResumePolicy):
            raise SpecificationError("resume_policy must be a ResumePolicy")
        if not isinstance(poll_interval_seconds, int | float) or isinstance(
            poll_interval_seconds, bool
        ):
            raise SpecificationError("poll_interval_seconds must be positive")
        try:
            rendered_poll_interval = float(poll_interval_seconds)
        except OverflowError as exc:
            raise SpecificationError("poll_interval_seconds must be positive") from exc
        if not math.isfinite(rendered_poll_interval) or rendered_poll_interval <= 0:
            raise SpecificationError("poll_interval_seconds must be positive")
        callbacks = (
            ("clock", clock),
            ("sleep", sleep),
            ("failure_classifier", failure_classifier),
            ("result_verifier", result_verifier),
            ("session_id_factory", session_id_factory),
            ("lease_token_factory", lease_token_factory),
        )
        for name, callback in callbacks:
            if not callable(callback):
                raise SpecificationError(f"{name} must be callable")
        self.repository = repository
        self.engine = engine
        self.retry_policy = RetryPolicy() if retry_policy is None else retry_policy
        self.resume_policy = ResumePolicy() if resume_policy is None else resume_policy
        try:
            self.executor_mode = ExecutorMode(executor_mode)
        except (TypeError, ValueError) as exc:
            raise SpecificationError("executor_mode must be thread or process") from exc
        self.maximum_workers = maximum_workers
        self.clock = clock
        self.sleep = sleep
        self.failure_classifier = failure_classifier
        self.result_verifier = result_verifier
        self._uses_default_result_verifier = result_verifier is verify_output_directory
        self.poll_interval_seconds = rendered_poll_interval
        self.session_id_factory = session_id_factory
        self.lease_token_factory = lease_token_factory

    @staticmethod
    def _require_generated_identifier(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SchedulerError(f"{name} factory returned an invalid identifier")
        return value

    def _verify_result(self, result: BacktestRunReference, job: BacktestJob | None) -> bool:
        if self._uses_default_result_verifier:
            return verify_output_directory(result, job=job)
        return self.result_verifier(result)

    def _cancel_active_run(self, item: _ActiveRun, *, at: datetime) -> None:
        item.future.cancel()
        try:
            self.repository.cancel_claim(
                item.job.run_id,
                worker_id=item.lease.worker_id,
                session_id=item.lease.session_id,
                lease_token=item.lease.lease_token,
                expected_revision=item.lease.revision,
                at=at,
            )
        except StateTransitionError:
            # A recovered or already-terminal claim is deliberately not mutated.
            return

    def _publish_failure(
        self,
        item: _ActiveRun,
        error: BaseException,
        *,
        completed_at: datetime,
    ) -> None:
        category = self.failure_classifier(error)
        current = self.repository.get_run(item.job.run_id)
        retryable = self.retry_policy.can_retry(category, current.attempt_count)
        retry_at = None
        if retryable:
            retry_at = completed_at + timedelta(
                seconds=self.retry_policy.delay_seconds(current.attempt_count)
            )
        self.repository.fail(
            item.job.run_id,
            worker_id=item.lease.worker_id,
            session_id=item.lease.session_id,
            lease_token=item.lease.lease_token,
            expected_revision=item.lease.revision,
            failure=FailureRecord(
                category=category,
                message=str(error) or type(error).__name__,
                occurred_at=completed_at,
                retryable=retryable,
                exception_type=type(error).__name__,
            ),
            retry_at=retry_at,
            at=completed_at,
        )

    def run(
        self,
        experiment_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> SchedulerSummary:
        token = cancellation or CancellationToken()
        session_id = self._require_generated_identifier("session_id", self.session_id_factory())
        now = self.clock()
        if self.resume_policy.verify_success_artifacts:
            for state in self.repository.list_runs(experiment_id, statuses=(RunStatus.SUCCEEDED,)):
                if state.result is None:
                    raise SchedulerError("successful run is missing its result reference")
                reference = state.result.effective_config_artifact
                job = (
                    self.repository.get_job(state.run_id)
                    if self._uses_default_result_verifier
                    and reference is not None
                    else None
                )
                if not self._verify_result(state.result, job):
                    self.repository.mark_corrupted(
                        state.run_id,
                        detail="successful run artifact verification failed",
                        at=now,
                    )
        if self.resume_policy.recover_expired_leases:
            self.repository.recover_expired_leases(experiment_id=experiment_id, at=now)
        self.repository.queue_planned(experiment_id, at=now)
        self.repository.set_experiment_status(experiment_id, ExperimentStatus.RUNNING, at=now)
        active: dict[str, _ActiveRun] = {}
        worker_sequence = 0
        executor: Executor
        if self.executor_mode is ExecutorMode.THREAD:
            executor = ThreadPoolExecutor(
                max_workers=self.maximum_workers,
                thread_name_prefix="research-runner",
            )
        else:
            executor = ProcessPoolExecutor(
                max_workers=self.maximum_workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
        canceled_shutdown = False
        try:
            while True:
                now = self.clock()
                if token.canceled:
                    self.repository.cancel_pending(experiment_id, at=now)
                    for run_id, item in sorted(active.items()):
                        self._cancel_active_run(item, at=now)
                        del active[run_id]
                    canceled_shutdown = True

                self.repository.queue_planned(experiment_id, at=now)
                while not token.canceled and len(active) < self.maximum_workers:
                    worker_sequence += 1
                    worker_id = f"local-{session_id}-{worker_sequence:06d}"
                    lease_token = self._require_generated_identifier(
                        "lease_token", self.lease_token_factory()
                    )
                    claimed = self.repository.claim_next(
                        experiment_id=experiment_id,
                        worker_id=worker_id,
                        session_id=session_id,
                        lease_token=lease_token,
                        at=now,
                        lease_seconds=self.resume_policy.lease_seconds,
                    )
                    if claimed is None:
                        worker_sequence -= 1
                        break
                    job, _, lease = claimed
                    active[job.run_id] = _ActiveRun(
                        job=job,
                        lease=lease,
                        future=executor.submit(self.engine.execute, job),
                        next_heartbeat=now
                        + timedelta(seconds=self.resume_policy.heartbeat_seconds),
                    )

                completed = sorted(
                    (item for item in active.values() if item.future.done()),
                    key=lambda item: item.job.run_id,
                )
                for item in completed:
                    completed_at = self.clock()
                    with token.publication_guard() as publication_allowed:
                        if not publication_allowed:
                            self._cancel_active_run(item, at=completed_at)
                            canceled_shutdown = True
                        else:
                            try:
                                result = item.future.result()
                                if not self._verify_result(result, item.job):
                                    raise EngineContractError(
                                        "Backtest Engine output manifest verification failed"
                                    )
                            except Exception as error:  # state must survive engine failures
                                self._publish_failure(
                                    item,
                                    error,
                                    completed_at=completed_at,
                                )
                            else:
                                self.repository.succeed(
                                    item.job.run_id,
                                    worker_id=item.lease.worker_id,
                                    session_id=item.lease.session_id,
                                    lease_token=item.lease.lease_token,
                                    expected_revision=item.lease.revision,
                                    result=result,
                                    at=completed_at,
                                )
                    del active[item.job.run_id]

                heartbeat_at = self.clock()
                for item in active.values():
                    if heartbeat_at >= item.next_heartbeat:
                        _, renewed = self.repository.heartbeat(
                            item.job.run_id,
                            worker_id=item.lease.worker_id,
                            session_id=item.lease.session_id,
                            lease_token=item.lease.lease_token,
                            expected_revision=item.lease.revision,
                            at=heartbeat_at,
                            lease_seconds=self.resume_policy.lease_seconds,
                        )
                        item.lease = renewed
                        item.next_heartbeat = heartbeat_at + timedelta(
                            seconds=self.resume_policy.heartbeat_seconds
                        )

                if not active and not self.repository.has_nonterminal_runs(experiment_id):
                    break
                if not active:
                    next_available = self.repository.next_available_at(experiment_id)
                    delay = self.poll_interval_seconds
                    if next_available is not None:
                        delay = min(
                            max((next_available - self.clock()).total_seconds(), 0.0),
                            self.poll_interval_seconds,
                        )
                    self.sleep(max(delay, 0.001))
                elif not completed:
                    self.sleep(self.poll_interval_seconds)
        finally:
            executor.shutdown(wait=not canceled_shutdown, cancel_futures=canceled_shutdown)

        runs = self.repository.list_runs(experiment_id)
        statuses = {run.status for run in runs}
        if token.canceled or RunStatus.CANCELED in statuses:
            status = ExperimentStatus.CANCELED
        elif RunStatus.CORRUPTED in statuses:
            status = ExperimentStatus.CORRUPTED
        elif RunStatus.BLOCKED in statuses:
            status = ExperimentStatus.BLOCKED
        elif RunStatus.FAILED in statuses:
            status = (
                ExperimentStatus.FAILED
                if statuses <= {RunStatus.FAILED}
                else ExperimentStatus.COMPLETED_WITH_FAILURES
            )
        elif statuses <= {RunStatus.SUCCEEDED}:
            status = ExperimentStatus.COMPLETED
        else:
            raise SchedulerError(f"scheduler stopped with nonterminal states: {statuses}")
        self.repository.set_experiment_status(experiment_id, status, at=self.clock())
        return SchedulerSummary(experiment_id=experiment_id, status=status, runs=runs)
