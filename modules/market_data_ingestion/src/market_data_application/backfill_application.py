from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from threading import Event, Thread

from market_data_ingestion.checkpoint import load_checkpoint
from market_data_ingestion.contracts import IngestionRunResult, NormalizedPartitionDescriptor
from market_data_ingestion.enums import DataType, RunState
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.execution_identity import ingestion_execution_identity
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.operations import DatasetOperations
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.progress import (
    CommittedChunkProgressHandler,
    DownloadProgressTracker,
    IngestionProgressSnapshot,
    ProgressObservingAdapter,
    observe_progress,
    progress_logger,
)
from market_data_ingestion.registry import descriptor_from_dict
from market_data_ingestion.state_machine import observe_transitions
from market_data_ingestion.timeframes import timeframe_seconds

from .application import MarketDataApplication as ProcessLocalMarketDataApplication
from .backfill_contracts import CreateDatasetBackfillRequest, DatasetBackfillJobRequest
from .composition import ApplicationContext
from .contracts import (
    CreateDownloadRequest,
    JobCreatedResponse,
    JobDetails,
    JobOperation,
    JobProgress,
)
from .durable_application import MarketDataApplication as UpdateMarketDataApplication
from .errors import ApplicationError, DatasetNotFoundError, JobLeaseLostError
from .lease_store import activate_job_lease

_TERMINAL = frozenset(
    {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_WARNINGS,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.DATASET_REJECTED,
    }
)


class MarketDataApplication(UpdateMarketDataApplication):
    """Canonical durable facade including BACKFILL and measured DOWNLOAD jobs."""

    def __init__(self, context: ApplicationContext) -> None:
        super().__init__(context)
        self._durable_transition_failures: dict[str, Exception] = {}

    def run_job(self, job_id: str) -> JobDetails:
        self.context.job_store.get_job(job_id)
        lease = self.context.lease_store.claim(
            job_id,
            owner_id=self.context.settings.instance_id,
            ttl_seconds=self.context.settings.lease_ttl_seconds,
        )
        if lease is None:
            return self.context.job_store.get_job(job_id)

        stop = Event()
        lost = Event()
        heartbeat = Thread(
            target=self._heartbeat,
            args=(lease, stop, lost),
            name=f"market-data-lease-{job_id[-8:]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            with activate_job_lease(lease):
                try:
                    job = self.context.job_store.get_job(job_id)
                    if job.state in _TERMINAL:
                        result = job
                    elif job.operation is JobOperation.BACKFILL:
                        result = self._run_backfill_job(job)
                    elif job.operation is JobOperation.UPDATE:
                        result = self._run_update_job(job)
                    elif job.operation is JobOperation.DOWNLOAD:
                        result = self._run_measured_download_job(job)
                    else:
                        result = ProcessLocalMarketDataApplication.run_job(self, job_id)
                except JobLeaseLostError:
                    self._pop_durable_transition_failure(job_id)
                    return self.context.job_store.get_job(job_id)
                if lost.is_set():
                    self._pop_durable_transition_failure(job_id)
                    return self.context.job_store.get_job(job_id)
                return result
        finally:
            stop.set()
            heartbeat.join(timeout=self.context.settings.lease_heartbeat_seconds + 1.0)
            self.context.lease_store.release(lease)

    def recover_jobs(self, *, limit: int = 200) -> tuple[str, ...]:
        scheduled = list(super().recover_jobs(limit=limit))
        known = set(scheduled)
        for job in self.context.job_store.recoverable_jobs(limit=limit):
            if job.operation is JobOperation.BACKFILL and job.job_id not in known:
                self._schedule(job.job_id)
                scheduled.append(job.job_id)
                known.add(job.job_id)
        return tuple(scheduled)

    def create_backfill(
        self,
        dataset_id: str,
        request: CreateDatasetBackfillRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        actor = actor_id.strip()
        key = idempotency_key.strip()
        if not actor or len(actor) > 128:
            raise ApplicationError(
                "BACKFILL_ACTOR_INVALID",
                "backfill actor_id must be non-empty and at most 128 characters",
                False,
                "backfill_creation",
            )
        if not key or len(key) > 255:
            raise ApplicationError(
                "BACKFILL_IDEMPOTENCY_KEY_INVALID",
                "backfill idempotency key must be non-empty and at most 255 characters",
                False,
                "backfill_creation",
            )

        normalized_dataset_id = dataset_id.strip().lower()
        descriptor_payload = self.context.dataset_registry.inspect(
            normalized_dataset_id,
            version=request.version,
        )
        if descriptor_payload is None:
            raise DatasetNotFoundError(
                dataset_id=normalized_dataset_id,
                version=request.version,
            )
        descriptor = descriptor_from_dict(descriptor_payload)
        manifest = load_manifest(self.context.storage.root, descriptor)
        stream_timeframe = self._backfill_stream_timeframe(
            manifest.partitions,
            request.data_type,
        )
        self._assert_backfill_non_overlap(
            manifest.partitions,
            data_type=request.data_type,
            stream_timeframe=stream_timeframe,
            start=request.start,
            end=request.end,
        )

        persisted = DatasetBackfillJobRequest(
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
            parent_dataset_hash=descriptor.dataset_hash,
            data_type=request.data_type,
            stream_timeframe=stream_timeframe,
            start=request.start,
            end=request.end,
            storage_format=request.storage_format,
            compression=request.compression,
        )
        scoped_key = "backfill:" + hashlib.sha256(f"{actor}\0{key}".encode()).hexdigest()
        return self.context.job_store.create_job(
            operation=JobOperation.BACKFILL,
            request=persisted,
            idempotency_key=scoped_key,
            correlation_id=correlation_id,
            actor_id=actor,
        )

    def submit_backfill(
        self,
        dataset_id: str,
        request: CreateDatasetBackfillRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        created = self.create_backfill(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        if created.job.state not in _TERMINAL:
            self._schedule(created.job.job_id)
        return created

    def _run_measured_download_job(self, job: JobDetails) -> JobDetails:
        request = CreateDownloadRequest.model_validate(job.request)
        adapter = self.context.adapters.get(request.exchange_id)
        if adapter is None:
            return ProcessLocalMarketDataApplication.run_job(self, job.job_id)

        domain_request = request.to_domain(request_id=job.job_id)
        identity = ingestion_execution_identity(
            domain_request,
            adapter,
            format_name=request.storage_format.value,
            compression=request.compression,
            reject_unresolved_required_gaps=request.reject_unresolved_required_gaps,
            concurrency=request.concurrency,
            max_records_in_memory=request.max_records_in_memory,
        )
        if self.context.storage.is_cancel_requested(identity.run_id):
            cancelled = self.context.job_store.transition(
                job.job_id,
                RunState.CANCELLED,
                backend_run_id=identity.run_id,
                warnings=("cancellation requested by user",),
            )
            self.context.storage.clear_cancel(identity.run_id)
            return cancelled

        checkpoint_chunks = self._validated_checkpoint_chunks(
            identity.run_id,
            identity.execution_hash,
        )
        tracker = DownloadProgressTracker(
            checkpoint_completed_chunks=checkpoint_chunks,
            initial_counters=job.progress.counters if job.progress is not None else None,
        )
        observing_adapter = ProgressObservingAdapter(adapter, tracker)
        handler = CommittedChunkProgressHandler(tracker, request_id=job.job_id)
        logger = progress_logger(handler, name=f"market-data-progress-{job.job_id}")
        service = IngestionService(
            adapters={observing_adapter.exchange_id: observing_adapter},
            storage=self.context.storage,
            registry=self.context.dataset_registry,
            logger=logger,
        )

        def transition_observer(
            _previous: RunState,
            target: RunState,
            _at: object,
        ) -> None:
            self._record_backend_transition(
                job.job_id,
                target,
                backend_run_id=identity.run_id,
            )
            if target is RunState.DOWNLOADING:
                tracker.begin_downloading()

        def progress_observer(snapshot: IngestionProgressSnapshot) -> None:
            self._record_measured_progress(job.job_id, self._job_progress(snapshot))

        try:
            with observe_progress(progress_observer), observe_transitions(transition_observer):
                result = service.execute(
                    domain_request,
                    format_name=request.storage_format.value,
                    compression=request.compression,
                    reject_unresolved_required_gaps=request.reject_unresolved_required_gaps,
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
        finally:
            logger.removeHandler(handler)
            handler.close()
        return self._record_result(job.job_id, result)

    def _run_backfill_job(self, job: JobDetails) -> JobDetails:
        request = DatasetBackfillJobRequest.model_validate(job.request)
        try:
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
            if descriptor.dataset_hash != request.parent_dataset_hash:
                raise ApplicationError(
                    "BACKFILL_PARENT_CHANGED",
                    "pinned backfill parent no longer matches the registered dataset hash",
                    False,
                    "backfill_execution",
                    {
                        "dataset_id": request.dataset_id,
                        "version": request.version,
                        "expected_hash": request.parent_dataset_hash,
                        "actual_hash": descriptor.dataset_hash,
                    },
                )
            manifest = load_manifest(self.context.storage.root, descriptor)
            current_timeframe = self._backfill_stream_timeframe(
                manifest.partitions,
                request.data_type,
            )
            if current_timeframe != request.stream_timeframe:
                raise ApplicationError(
                    "BACKFILL_PARENT_CHANGED",
                    "pinned backfill stream timeframe no longer matches the parent manifest",
                    False,
                    "backfill_execution",
                    {
                        "dataset_id": request.dataset_id,
                        "version": request.version,
                        "expected_timeframe": request.stream_timeframe,
                        "actual_timeframe": current_timeframe,
                    },
                )
            self._assert_backfill_non_overlap(
                manifest.partitions,
                data_type=request.data_type,
                stream_timeframe=request.stream_timeframe,
                start=request.start,
                end=request.end,
            )

            def transition_observer(
                _previous: RunState,
                target: RunState,
                _at: object,
            ) -> None:
                self._record_backend_transition(job.job_id, target)

            with observe_transitions(transition_observer):
                result = DatasetOperations(
                    service=self.context.ingestion_service,
                    storage=self.context.storage,
                    registry=self.context.dataset_registry,
                ).backfill(
                    request.dataset_id,
                    version=request.version,
                    data_type=request.data_type,
                    start=request.start,
                    end=request.end,
                    request_id=job.job_id,
                    format_name=request.storage_format.value,
                    compression=request.compression,
                )
            return self._record_result(job.job_id, result)
        except (StructuredIngestionError, ApplicationError) as exc:
            return self._record_execution_error(job.job_id, exc)
        except Exception as exc:
            safe_error = ApplicationError(
                "UNEXPECTED_BACKFILL_ERROR",
                "dataset backfill failed at an unexpected application boundary",
                False,
                "backfill_execution",
                {"error_type": type(exc).__name__},
            )
            return self._record_execution_error(job.job_id, safe_error)

    def _record_backend_transition(
        self,
        job_id: str,
        target: RunState,
        *,
        backend_run_id: str | None = None,
    ) -> None:
        with self._lock:
            recorded_failure = self._durable_transition_failures.get(job_id)
        if recorded_failure is not None:
            raise recorded_failure

        try:
            previous_job = self.context.job_store.get_job(job_id)
            super()._record_backend_transition(
                job_id,
                target,
                backend_run_id=backend_run_id,
            )
            current = self.context.job_store.get_job(job_id)
        except Exception as exc:
            with self._lock:
                self._durable_transition_failures.setdefault(job_id, exc)
            raise

        if (
            previous_job.state is current.state
            or current.state is not target
            or target in _TERMINAL
        ):
            return
        previous_progress = previous_job.progress
        with suppress(Exception):
            self._record_measured_progress(
                job_id,
                JobProgress(
                    phase=target,
                    determinate=False,
                    completed_units=(
                        previous_progress.completed_units if previous_progress is not None else 0
                    ),
                    message=f"phase: {target.value}",
                    counters=(previous_progress.counters if previous_progress is not None else {}),
                    current_range_start=(
                        previous_progress.current_range_start
                        if previous_progress is not None
                        else None
                    ),
                    current_range_end=(
                        previous_progress.current_range_end
                        if previous_progress is not None
                        else None
                    ),
                ),
            )

    def _record_result(self, job_id: str, result: IngestionRunResult) -> JobDetails:
        self._raise_durable_transition_failure(job_id)
        return super()._record_result(job_id, result)

    def _record_execution_error(
        self,
        job_id: str,
        error: StructuredIngestionError | ApplicationError,
    ) -> JobDetails:
        self._raise_durable_transition_failure(job_id)
        return super()._record_execution_error(job_id, error)

    def _raise_durable_transition_failure(self, job_id: str) -> None:
        failure = self._pop_durable_transition_failure(job_id)
        if failure is not None:
            raise failure

    def _pop_durable_transition_failure(self, job_id: str) -> Exception | None:
        with self._lock:
            return self._durable_transition_failures.pop(job_id, None)

    def _record_measured_progress(self, job_id: str, progress: JobProgress) -> JobDetails:
        current = self.context.job_store.get_job(job_id)
        if current.state is not progress.phase:
            raise ValueError("progress phase must match the durable job state")
        previous = current.progress
        if previous is not None and previous.phase is progress.phase:
            if previous.determinate and not progress.determinate:
                raise ValueError("determinate progress must not regress to indeterminate")
            if previous.determinate and progress.determinate:
                if previous.total_units != progress.total_units:
                    raise ValueError("determinate progress total must remain stable")
                if progress.completed_units < previous.completed_units:
                    raise ValueError("completed progress units must be monotonic")
            for name, value in previous.counters.items():
                if progress.counters.get(name, 0) < value:
                    raise ValueError(f"progress counter regressed: {name}")
        return self.context.job_store.record_progress(job_id, progress)

    @staticmethod
    def _job_progress(snapshot: IngestionProgressSnapshot) -> JobProgress:
        current_stream = (
            f" {snapshot.current_data_type.value}" if snapshot.current_data_type is not None else ""
        )
        current_timeframe = (
            f" {snapshot.current_timeframe}" if snapshot.current_timeframe is not None else ""
        )
        message = (
            f"{snapshot.message or 'historical acquisition'}{current_stream}{current_timeframe}"
        )
        return JobProgress(
            phase=snapshot.phase,
            determinate=snapshot.determinate,
            completed_units=snapshot.completed_chunks,
            total_units=snapshot.total_chunks if snapshot.determinate else None,
            message=message,
            counters={
                "planned_chunks": snapshot.planned_chunks,
                "checkpoint_completed_chunks": snapshot.checkpoint_completed_chunks,
                "downloaded_chunks": snapshot.downloaded_chunks,
                "restored_chunks": snapshot.restored_chunks,
                "downloaded_records": snapshot.downloaded_records,
                "restored_records": snapshot.restored_records,
                "downloaded_bytes": snapshot.downloaded_bytes,
            },
            current_range_start=snapshot.current_range_start,
            current_range_end=snapshot.current_range_end,
        )

    def _validated_checkpoint_chunks(
        self,
        run_id: str,
        execution_hash: str,
    ) -> tuple[str, ...]:
        checkpoint_path = self.context.storage.root / "checkpoints" / f"{run_id}.json"
        if not checkpoint_path.is_file():
            return ()
        with suppress(Exception):
            checkpoint = load_checkpoint(checkpoint_path)
            if checkpoint.request_hash == execution_hash:
                return checkpoint.completed_chunks
        return ()

    @staticmethod
    def _backfill_stream_timeframe(
        partitions: Iterable[NormalizedPartitionDescriptor],
        data_type: DataType,
    ) -> str | None:
        candidates = [
            item.timeframe
            for item in partitions
            if item.timeframe is not None and item.storage_uri.startswith("normalized/")
        ]
        if not candidates:
            raise ApplicationError(
                "BACKFILL_BASE_TIMEFRAME_UNAVAILABLE",
                "pinned parent has no normalized interval timeframe for backfill planning",
                False,
                "backfill_creation",
            )
        base_timeframe = min(candidates, key=lambda item: (timeframe_seconds(item), item))
        return None if data_type is DataType.FUNDING_RATE else base_timeframe

    @staticmethod
    def _assert_backfill_non_overlap(
        partitions: Iterable[NormalizedPartitionDescriptor],
        *,
        data_type: DataType,
        stream_timeframe: str | None,
        start: datetime,
        end: datetime,
    ) -> None:
        for partition in partitions:
            if partition.data_type is not data_type or partition.timeframe != stream_timeframe:
                continue
            if start < partition.end and partition.start < end:
                raise ApplicationError(
                    "BACKFILL_RANGE_OVERLAP",
                    "backfill range overlaps a registered stream; use repair for replacement",
                    False,
                    "backfill_creation",
                    {
                        "partition_id": partition.partition_id,
                        "data_type": data_type.value,
                        "timeframe": stream_timeframe,
                        "use_repair": True,
                    },
                )
