from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import Any

from market_data_ingestion.canonical import primitive
from market_data_ingestion.contracts import IngestionRunResult, RepairResult
from market_data_ingestion.enums import RepairStatus, RunState
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.execution_identity import ingestion_execution_identity
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.operations import DatasetOperations
from market_data_ingestion.registry import descriptor_from_dict
from market_data_ingestion.state_machine import observe_transitions

from .artifact_contracts import (
    ArtifactContent,
    ArtifactDescriptor,
    CreateDatasetExportRequest,
    DatasetExportJobRequest,
)
from .composition import ApplicationContext
from .contracts import (
    CreateDownloadRequest,
    JobCreatedResponse,
    JobDetails,
    JobError,
    JobEvent,
    JobListResponse,
    JobOperation,
)
from .errors import (
    ApplicationError,
    DatasetNotFoundError,
    InvalidJobTransitionError,
    RepairPreviewStaleError,
)
from .inspection import DatasetInspection
from .repair_execution_contracts import RepairJobRequest

_TERMINAL = frozenset(
    {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_WARNINGS,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.DATASET_REJECTED,
    }
)

_STATE_ORDER = {
    RunState.CREATED: 0,
    RunState.VALIDATING_REQUEST: 1,
    RunState.DISCOVERING_CAPABILITIES: 2,
    RunState.RESOLVING_INSTRUMENT: 3,
    RunState.PLANNING: 4,
    RunState.DOWNLOADING: 5,
    RunState.VERIFYING_RAW: 6,
    RunState.NORMALIZING: 7,
    RunState.VALIDATING_DATA: 8,
    RunState.REPAIRING: 9,
    RunState.BUILDING_DERIVED: 10,
    RunState.HASHING: 11,
    RunState.REGISTERING: 12,
    RunState.PAUSED: 13,
    RunState.EXPORTING: 14,
    RunState.COMPLETED: 20,
    RunState.COMPLETED_WITH_WARNINGS: 20,
    RunState.CANCELLED: 20,
    RunState.FAILED: 20,
    RunState.DATASET_REJECTED: 20,
}


class MarketDataApplication:
    """Transport-neutral facade for Market Data CLI and HTTP API."""

    def __init__(self, context: ApplicationContext) -> None:
        self.context = context
        self._executor = ThreadPoolExecutor(
            max_workers=context.settings.worker_count,
            thread_name_prefix="market-data-job",
        )
        self._lock = RLock()
        self._futures: dict[str, Future[JobDetails]] = {}
        self._running: set[str] = set()

    def create_download(
        self,
        request: CreateDownloadRequest,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        return self.context.job_store.create_job(
            operation=JobOperation.DOWNLOAD,
            request=request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

    def submit_download(
        self,
        request: CreateDownloadRequest,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        created = self.create_download(
            request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )
        if created.job.state not in _TERMINAL:
            self._schedule(created.job.job_id)
        return created

    def create_export(
        self,
        dataset_id: str,
        request: CreateDatasetExportRequest,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        descriptor_payload = self.context.dataset_registry.inspect(
            dataset_id,
            version=request.version,
        )
        if descriptor_payload is None:
            raise DatasetNotFoundError(dataset_id=dataset_id, version=request.version)
        descriptor = descriptor_from_dict(descriptor_payload)
        persisted = DatasetExportJobRequest(
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
        )
        return self.context.job_store.create_job(
            operation=JobOperation.EXPORT,
            request=persisted,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

    def submit_export(
        self,
        dataset_id: str,
        request: CreateDatasetExportRequest,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        created = self.create_export(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )
        if created.job.state not in _TERMINAL:
            self._schedule(created.job.job_id)
        return created

    def run_job(self, job_id: str) -> JobDetails:
        with self._lock:
            if job_id in self._running:
                raise ApplicationError(
                    "JOB_ALREADY_RUNNING",
                    "job is already running in this application process",
                    True,
                    "job_execution",
                    {"job_id": job_id},
                )
            self._running.add(job_id)
        try:
            job = self.context.job_store.get_job(job_id)
            if job.state in _TERMINAL:
                return job
            if job.operation is JobOperation.EXPORT:
                return self._run_export_job(job)
            if job.operation is JobOperation.REPAIR:
                return self._run_repair_job(job)
            if job.operation is not JobOperation.DOWNLOAD:
                raise ApplicationError(
                    "JOB_OPERATION_UNSUPPORTED",
                    "this application executor does not support the stored operation",
                    False,
                    "job_execution",
                    {"job_id": job_id, "operation": job.operation.value},
                )
            request = CreateDownloadRequest.model_validate(job.request)
            domain_request = request.to_domain(request_id=job.job_id)
            adapter = self.context.adapters.get(request.exchange_id)
            identity = (
                ingestion_execution_identity(
                    domain_request,
                    adapter,
                    format_name=request.storage_format.value,
                    compression=request.compression,
                    reject_unresolved_required_gaps=(request.reject_unresolved_required_gaps),
                    concurrency=request.concurrency,
                    max_records_in_memory=request.max_records_in_memory,
                )
                if adapter is not None
                else None
            )
            if identity is not None and self.context.storage.is_cancel_requested(identity.run_id):
                cancelled = self.context.job_store.transition(
                    job.job_id,
                    RunState.CANCELLED,
                    backend_run_id=identity.run_id,
                    warnings=("cancellation requested by user",),
                )
                self.context.storage.clear_cancel(identity.run_id)
                return cancelled

            def transition_observer(
                _previous: RunState,
                target: RunState,
                _at: object,
            ) -> None:
                self._record_backend_transition(
                    job.job_id,
                    target,
                    backend_run_id=(identity.run_id if identity is not None else None),
                )

            try:
                with observe_transitions(transition_observer):
                    result = self.context.ingestion_service.execute(
                        domain_request,
                        format_name=request.storage_format.value,
                        compression=request.compression,
                        reject_unresolved_required_gaps=(request.reject_unresolved_required_gaps),
                        concurrency=request.concurrency,
                        max_records_in_memory=request.max_records_in_memory,
                    )
            except StructuredIngestionError as exc:
                return self._record_execution_error(job.job_id, exc)
            except Exception as exc:
                safe_error = ApplicationError(
                    "UNEXPECTED_APPLICATION_ERROR",
                    "market data job failed at an unexpected application boundary",
                    False,
                    "job_execution",
                    {"error_type": type(exc).__name__},
                )
                return self._record_execution_error(job.job_id, safe_error)
            return self._record_result(job.job_id, result)
        finally:
            with self._lock:
                self._running.discard(job_id)

    def recover_jobs(self, *, limit: int = 200) -> tuple[str, ...]:
        scheduled: list[str] = []
        for job in self.context.job_store.recoverable_jobs(limit=limit):
            if job.operation in {
                JobOperation.DOWNLOAD,
                JobOperation.REPAIR,
                JobOperation.EXPORT,
            }:
                self._schedule(job.job_id)
                scheduled.append(job.job_id)
        return tuple(scheduled)

    def _run_export_job(self, job: JobDetails) -> JobDetails:
        request = DatasetExportJobRequest.model_validate(job.request)
        try:
            self._record_backend_transition(job.job_id, RunState.VALIDATING_REQUEST)
            descriptor_payload = self.context.dataset_registry.inspect(
                request.dataset_id,
                version=request.version,
            )
            if descriptor_payload is None:
                raise DatasetNotFoundError(
                    dataset_id=request.dataset_id,
                    version=request.version,
                )
            descriptor = descriptor_from_dict(descriptor_payload)
            manifest = load_manifest(self.context.storage.root, descriptor)
            self._record_backend_transition(job.job_id, RunState.EXPORTING)
            artifact = self.context.artifact_store.export(descriptor, manifest)
            return self.context.job_store.transition(
                job.job_id,
                RunState.COMPLETED,
                result=artifact.model_dump(mode="json"),
            )
        except (StructuredIngestionError, ApplicationError) as exc:
            return self._record_execution_error(job.job_id, exc)
        except Exception as exc:
            safe_error = ApplicationError(
                "UNEXPECTED_EXPORT_ERROR",
                "dataset export failed at an unexpected application boundary",
                False,
                "artifact_export",
                {"error_type": type(exc).__name__},
            )
            return self._record_execution_error(job.job_id, safe_error)

    def _run_repair_job(self, job: JobDetails) -> JobDetails:
        request = RepairJobRequest.model_validate(job.request)
        try:
            for target in (
                RunState.VALIDATING_REQUEST,
                RunState.DISCOVERING_CAPABILITIES,
                RunState.RESOLVING_INSTRUMENT,
                RunState.PLANNING,
            ):
                self._record_backend_transition(job.job_id, target)
            preview = DatasetInspection(
                self.context.dataset_registry,
                self.context.storage.root,
            ).preview_repair(
                request.dataset_id,
                version=request.version,
                data_type=request.selected_data_type,
                deep=False,
            )
            if preview.preview_id != request.preview_id or preview.action != request.action:
                raise RepairPreviewStaleError(
                    dataset_id=request.dataset_id,
                    supplied=request.preview_id,
                    current=preview.preview_id,
                )
            if request.action != "stream_redownload_and_rebuild":
                raise ApplicationError(
                    "REPAIR_EXECUTION_ACTION_UNSUPPORTED",
                    "the confirmed repair action is not yet execution-safe",
                    False,
                    "repair_execution",
                    {"action": request.action},
                )
            self.context.repair_confirmation_store.consume_reserved(
                request.confirmation_id,
                job_id=job.job_id,
                actor_id=job.actor_id or "",
                preview_id=request.preview_id,
            )
            self._record_backend_transition(job.job_id, RunState.REPAIRING)
            result = DatasetOperations(
                service=self.context.ingestion_service,
                storage=self.context.storage,
                registry=self.context.dataset_registry,
            ).repair(
                request.dataset_id,
                version=request.version,
                data_type=request.selected_data_type,
                request_id=job.job_id,
                format_name=request.storage_format.value,
                compression=request.compression,
            )
        except (StructuredIngestionError, ApplicationError) as exc:
            return self._record_execution_error(job.job_id, exc)
        except Exception as exc:
            safe_error = ApplicationError(
                "UNEXPECTED_REPAIR_EXECUTION_ERROR",
                "repair job failed at an unexpected application boundary",
                False,
                "repair_execution",
                {"error_type": type(exc).__name__},
            )
            return self._record_execution_error(job.job_id, safe_error)
        return self._record_repair_result(job.job_id, result)

    def get_artifact(self, artifact_id: str) -> ArtifactDescriptor:
        return self.context.artifact_store.get(artifact_id)

    def artifact_content(self, artifact_id: str) -> ArtifactContent:
        return self.context.artifact_store.content(artifact_id)

    def get_job(self, job_id: str) -> JobDetails:
        return self.context.job_store.get_job(job_id)

    def list_jobs(self, **kwargs: Any) -> JobListResponse:
        return self.context.job_store.list_jobs(**kwargs)

    def events_after(
        self,
        job_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> tuple[JobEvent, ...]:
        return self.context.job_store.events_after(
            job_id,
            after_event_id=after_event_id,
            limit=limit,
        )

    def wait(self, job_id: str, *, timeout: float | None = None) -> JobDetails:
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            return self.get_job(job_id)
        return future.result(timeout=timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _schedule(self, job_id: str) -> None:
        with self._lock:
            existing = self._futures.get(job_id)
            if existing is not None and not existing.done():
                return
            future = self._executor.submit(self.run_job, job_id)
            self._futures[job_id] = future

            def cleanup(completed: Future[JobDetails]) -> None:
                self._forget_future(job_id, completed)

            future.add_done_callback(cleanup)

    def _forget_future(self, job_id: str, future: Future[JobDetails]) -> None:
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)

    def _record_backend_transition(
        self,
        job_id: str,
        target: RunState,
        *,
        backend_run_id: str | None = None,
    ) -> None:
        current = self.context.job_store.get_job(job_id).state
        if current is target or current in _TERMINAL:
            return
        try:
            self.context.job_store.transition(
                job_id,
                target,
                backend_run_id=backend_run_id,
            )
        except InvalidJobTransitionError:
            # A resumed backend rebuilds its local machine from CREATED and may replay
            # earlier phases before reaching the persisted application state. Ignore only
            # those older/equal observations; forward progress remains fail-closed.
            if _STATE_ORDER[target] <= _STATE_ORDER[current]:
                return
            raise

    def _record_result(self, job_id: str, result: IngestionRunResult) -> JobDetails:
        payload = primitive(result)
        error: JobError | None = None
        if result.final_state is RunState.DATASET_REJECTED:
            validation = result.validation
            error = JobError(
                code="DATASET_REJECTED",
                message="dataset was rejected by the configured validation policy",
                retryable=False,
                stage="validation",
                details={
                    "validation_status": (
                        validation.status.value if validation is not None else None
                    ),
                    "issues": len(validation.issues) if validation is not None else 0,
                    "gaps": len(validation.gaps) if validation is not None else 0,
                },
                correlation_id=self.context.job_store.get_job(job_id).correlation_id,
            )
        elif result.final_state is RunState.FAILED:
            error = JobError(
                code="INGESTION_FAILED",
                message="ingestion returned a failed terminal result",
                retryable=False,
                stage="job_execution",
                correlation_id=self.context.job_store.get_job(job_id).correlation_id,
            )
        return self.context.job_store.transition(
            job_id,
            result.final_state,
            backend_run_id=result.run_id,
            result=payload,
            error=error,
            warnings=result.warnings,
        )

    def _record_repair_result(self, job_id: str, result: RepairResult) -> JobDetails:
        payload = primitive(result)
        if not isinstance(payload, dict):
            raise TypeError("repair result must serialize to an object")
        if result.status in {RepairStatus.REPAIRED, RepairStatus.PARTIALLY_REPAIRED}:
            for target in (
                RunState.VALIDATING_DATA,
                RunState.HASHING,
                RunState.REGISTERING,
            ):
                self._record_backend_transition(job_id, target)
            warnings = (
                ("repair completed with unresolved gaps",)
                if result.status is RepairStatus.PARTIALLY_REPAIRED
                else ()
            )
            terminal = RunState.COMPLETED_WITH_WARNINGS if warnings else RunState.COMPLETED
            return self.context.job_store.transition(
                job_id,
                terminal,
                result=payload,
                warnings=warnings,
            )
        job = self.context.job_store.get_job(job_id)
        error = JobError(
            code=f"REPAIR_{result.status.value}",
            message="confirmed repair did not produce a complete repaired dataset",
            retryable=result.status is RepairStatus.SOURCE_UNAVAILABLE,
            stage="repair_execution",
            details={
                "audit_uri": result.audit_uri,
                "new_dataset_version": result.new_dataset_version,
                "unresolved_gaps": len(result.unresolved_gaps),
            },
            correlation_id=job.correlation_id,
        )
        return self.context.job_store.transition(
            job_id,
            RunState.FAILED,
            result=payload,
            error=error,
        )

    def _record_execution_error(
        self,
        job_id: str,
        error: StructuredIngestionError | ApplicationError,
    ) -> JobDetails:
        job = self.context.job_store.get_job(job_id)
        details = self._safe_details(error.details)
        job_error = JobError(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            stage=error.stage,
            details=details,
            correlation_id=job.correlation_id,
        )
        current = job.state
        if current in _TERMINAL:
            return job
        if current is RunState.PAUSED:
            return self.context.job_store.transition(
                job_id,
                RunState.PAUSED,
                error=job_error,
            )
        if current is RunState.CREATED:
            self.context.job_store.transition(job_id, RunState.VALIDATING_REQUEST)
        try:
            return self.context.job_store.transition(
                job_id,
                RunState.FAILED,
                error=job_error,
            )
        except InvalidJobTransitionError:
            latest = self.context.job_store.get_job(job_id)
            return self.context.job_store.transition(
                job_id,
                latest.state,
                error=job_error,
            )

    @staticmethod
    def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
        try:
            converted = primitive(dict(details))
        except (TypeError, ValueError):
            return {"detail_keys": sorted(str(key) for key in details)}
        return converted if isinstance(converted, dict) else {}
