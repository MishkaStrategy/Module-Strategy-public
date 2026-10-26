from __future__ import annotations

import hashlib
from threading import Event, Thread

from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.execution_identity import ingestion_execution_identity
from market_data_ingestion.operations import DatasetOperations
from market_data_ingestion.registry import descriptor_from_dict
from market_data_ingestion.state_machine import observe_transitions

from .application import MarketDataApplication as ProcessLocalMarketDataApplication
from .catalog import DatasetCatalog
from .catalog_contracts import (
    DatasetDetails,
    DatasetListResponse,
    ExchangeCapabilitiesResponse,
    ExchangeListResponse,
    InstrumentMetadataResponse,
    MarketListResponse,
)
from .contracts import CreateDownloadRequest, JobCreatedResponse, JobDetails, JobOperation
from .control_contracts import JobCancellationResponse
from .errors import (
    ApplicationError,
    DatasetNotFoundError,
    JobBackendIdentityConflictError,
    JobCancellationConflictError,
    JobCancellationUnavailableError,
    JobLeaseLostError,
    RepairConfirmationConflictError,
    RepairPreviewStaleError,
)
from .inspection import DatasetInspection
from .inspection_contracts import DatasetValidationResponse, RepairPlanPreviewResponse
from .lease_store import JobLease, activate_job_lease
from .repair_confirmation_contracts import (
    CreateRepairConfirmationRequest,
    RepairConfirmationResponse,
    RepairConfirmationStatusResponse,
)
from .repair_confirmation_store import RepairConfirmationRecord
from .repair_execution_contracts import CreateRepairExecutionRequest
from .update_contracts import CreateDatasetUpdateRequest, DatasetUpdateJobRequest

_TERMINAL = frozenset(
    {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_WARNINGS,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.DATASET_REJECTED,
    }
)
_CANCELLABLE = frozenset(
    {
        RunState.CREATED,
        RunState.VALIDATING_REQUEST,
        RunState.DISCOVERING_CAPABILITIES,
        RunState.RESOLVING_INSTRUMENT,
        RunState.PLANNING,
        RunState.DOWNLOADING,
        RunState.PAUSED,
    }
)


class MarketDataApplication(ProcessLocalMarketDataApplication):
    """Application facade with durable cross-process execution ownership."""

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
                    result = (
                        self._run_update_job(job)
                        if job.operation is JobOperation.UPDATE and job.state not in _TERMINAL
                        else super().run_job(job_id)
                    )
                except JobLeaseLostError:
                    return self.context.job_store.get_job(job_id)
                if lost.is_set():
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
            if job.operation is JobOperation.UPDATE and job.job_id not in known:
                self._schedule(job.job_id)
                scheduled.append(job.job_id)
                known.add(job.job_id)
        return tuple(scheduled)

    def create_update(
        self,
        dataset_id: str,
        request: CreateDatasetUpdateRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        actor = actor_id.strip()
        key = idempotency_key.strip()
        if not actor or len(actor) > 128:
            raise ApplicationError(
                "UPDATE_ACTOR_INVALID",
                "update actor_id must be non-empty and at most 128 characters",
                False,
                "update_creation",
            )
        if not key or len(key) > 255:
            raise ApplicationError(
                "UPDATE_IDEMPOTENCY_KEY_INVALID",
                "update idempotency key must be non-empty and at most 255 characters",
                False,
                "update_creation",
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
        if request.end <= descriptor.end:
            raise ApplicationError(
                "UPDATE_RANGE_EMPTY",
                "update end must be after the pinned parent dataset end",
                False,
                "update_creation",
                {
                    "dataset_id": descriptor.dataset_id,
                    "version": descriptor.version,
                    "parent_end": descriptor.end.isoformat(),
                    "requested_end": request.end.isoformat(),
                },
            )
        unknown = tuple(
            sorted(
                set(request.required_data) - set(descriptor.data_types),
                key=lambda item: item.value,
            )
        )
        if unknown:
            raise ApplicationError(
                "UPDATE_DATA_TYPE_UNAVAILABLE",
                "update requested data types that are absent from the pinned parent dataset",
                False,
                "update_creation",
                {
                    "dataset_id": descriptor.dataset_id,
                    "version": descriptor.version,
                    "missing": [item.value for item in unknown],
                },
            )
        persisted = DatasetUpdateJobRequest(
            dataset_id=descriptor.dataset_id,
            version=descriptor.version,
            parent_dataset_hash=descriptor.dataset_hash,
            parent_end=descriptor.end,
            end=request.end,
            required_data=request.required_data,
            storage_format=request.storage_format,
            compression=request.compression,
        )
        scoped_key = "update:" + hashlib.sha256(f"{actor}\0{key}".encode()).hexdigest()
        return self.context.job_store.create_job(
            operation=JobOperation.UPDATE,
            request=persisted,
            idempotency_key=scoped_key,
            correlation_id=correlation_id,
            actor_id=actor,
        )

    def submit_update(
        self,
        dataset_id: str,
        request: CreateDatasetUpdateRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        created = self.create_update(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        if created.job.state not in _TERMINAL:
            self._schedule(created.job.job_id)
        return created

    def cancel_job(self, job_id: str) -> JobCancellationResponse:
        job = self.context.job_store.get_job(job_id)
        if job.state in _TERMINAL:
            if job.backend_run_id is not None:
                self.context.storage.clear_cancel(job.backend_run_id)
            return JobCancellationResponse(
                status="already_terminal",
                job=job,
                backend_run_id=job.backend_run_id,
            )
        if job.state not in _CANCELLABLE:
            raise JobCancellationConflictError(job_id=job_id, state=job.state.value)
        if job.operation is not JobOperation.DOWNLOAD:
            raise JobCancellationConflictError(job_id=job_id, state=job.state.value)

        request = CreateDownloadRequest.model_validate(job.request)
        adapter = self.context.adapters.get(request.exchange_id)
        if adapter is None:
            raise JobCancellationUnavailableError(
                job_id=job_id,
                exchange_id=request.exchange_id,
            )
        identity = ingestion_execution_identity(
            request.to_domain(request_id=job.job_id),
            adapter,
            format_name=request.storage_format.value,
            compression=request.compression,
            reject_unresolved_required_gaps=request.reject_unresolved_required_gaps,
            concurrency=request.concurrency,
            max_records_in_memory=request.max_records_in_memory,
        )
        if job.backend_run_id is not None and job.backend_run_id != identity.run_id:
            raise JobBackendIdentityConflictError(
                job_id=job_id,
                stored=job.backend_run_id,
                derived=identity.run_id,
            )

        lease = self.context.lease_store.claim(
            job_id,
            owner_id=self.context.settings.instance_id,
            ttl_seconds=self.context.settings.lease_ttl_seconds,
        )
        if lease is not None:
            try:
                with activate_job_lease(lease):
                    latest = self.context.job_store.get_job(job_id)
                    if latest.state in _TERMINAL:
                        return JobCancellationResponse(
                            status="already_terminal",
                            job=latest,
                            backend_run_id=latest.backend_run_id or identity.run_id,
                        )
                    if latest.state not in _CANCELLABLE:
                        raise JobCancellationConflictError(
                            job_id=job_id,
                            state=latest.state.value,
                        )
                    cancelled = self.context.job_store.transition(
                        job_id,
                        RunState.CANCELLED,
                        backend_run_id=identity.run_id,
                        warnings=("cancellation requested by user",),
                    )
                    self.context.storage.clear_cancel(identity.run_id)
                    return JobCancellationResponse(
                        status="cancelled",
                        job=cancelled,
                        backend_run_id=identity.run_id,
                    )
            finally:
                self.context.lease_store.release(lease)

        latest = self.context.job_store.get_job(job_id)
        if latest.state in _TERMINAL:
            return JobCancellationResponse(
                status="already_terminal",
                job=latest,
                backend_run_id=latest.backend_run_id or identity.run_id,
            )
        if latest.state not in _CANCELLABLE:
            raise JobCancellationConflictError(job_id=job_id, state=latest.state.value)
        self.context.storage.request_cancel(identity.run_id)
        return JobCancellationResponse(
            status="requested",
            job=latest,
            backend_run_id=identity.run_id,
        )

    def list_datasets(self, *, limit: int = 50, offset: int = 0) -> DatasetListResponse:
        return self._catalog().list_datasets(limit=limit, offset=offset)

    def get_dataset(self, dataset_id: str, *, version: int | None = None) -> DatasetDetails:
        return self._catalog().get_dataset(dataset_id, version=version)

    def list_exchanges(self) -> ExchangeListResponse:
        return self._catalog().list_exchanges()

    def get_exchange(self, exchange_id: str) -> ExchangeCapabilitiesResponse:
        return self._catalog().get_exchange(exchange_id)

    def list_markets(self, exchange_id: str) -> MarketListResponse:
        return self._catalog().list_markets(exchange_id)

    def resolve_instrument(
        self,
        exchange_id: str,
        market_type: MarketType,
        symbol: str,
    ) -> InstrumentMetadataResponse:
        return self._catalog().resolve_instrument(exchange_id, market_type, symbol)

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        deep: bool = False,
    ) -> DatasetValidationResponse:
        return self._inspection().validate_dataset(
            dataset_id,
            version=version,
            deep=deep,
        )

    def preview_repair(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        data_type: DataType | None = None,
        deep: bool = False,
    ) -> RepairPlanPreviewResponse:
        return self._inspection().preview_repair(
            dataset_id,
            version=version,
            data_type=data_type,
            deep=deep,
        )

    def create_repair_confirmation(
        self,
        dataset_id: str,
        request: CreateRepairConfirmationRequest,
        *,
        actor_id: str,
    ) -> RepairConfirmationResponse:
        actor = actor_id.strip()
        if not actor or len(actor) > 128:
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="actor_id_invalid",
            )
        preview = self.preview_repair(
            dataset_id,
            version=request.version,
            data_type=request.selected_data_type,
            deep=False,
        )
        if preview.preview_id != request.preview_id:
            raise RepairPreviewStaleError(
                dataset_id=dataset_id,
                supplied=request.preview_id,
                current=preview.preview_id,
            )
        if not preview.confirmation_required or preview.action == "no_action":
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="no_repair_required",
            )
        if request.acknowledged_mutations != preview.expected_mutations:
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="mutation_acknowledgement_mismatch",
            )
        if request.acknowledgement != f"CONFIRM {preview.preview_id}":
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="confirmation_phrase_mismatch",
            )
        issued = self.context.repair_confirmation_store.issue(
            dataset_id=preview.dataset_id,
            version=preview.version,
            preview_id=preview.preview_id,
            action=preview.action,
            selected_data_type=preview.selected_data_type,
            acknowledged_mutations=preview.expected_mutations,
            actor_id=actor,
            ttl_seconds=self.context.settings.repair_confirmation_ttl_seconds,
        )
        record = issued.record
        return RepairConfirmationResponse(
            confirmation_id=record.confirmation_id,
            confirmation_token=issued.token,
            dataset_id=record.dataset_id,
            version=record.version,
            preview_id=record.preview_id,
            action=record.action,
            selected_data_type=record.selected_data_type,
            acknowledged_mutations=record.acknowledged_mutations,
            actor_id=record.actor_id,
            created_at=record.created_at,
            expires_at=record.expires_at,
            consumed=False,
        )

    def create_repair(
        self,
        dataset_id: str,
        request: CreateRepairExecutionRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        actor = actor_id.strip()
        if not actor or len(actor) > 128:
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="actor_id_invalid",
            )
        record = self.context.repair_confirmation_store.get(request.confirmation_id)
        if record.action != "stream_redownload_and_rebuild":
            raise RepairConfirmationConflictError(
                dataset_id=dataset_id,
                reason="execution_action_not_yet_supported",
            )
        persisted = request.persisted(dataset_id=dataset_id, action=record.action)
        return self.context.job_store.create_repair_job(
            request=persisted,
            confirmation_token=request.confirmation_token.get_secret_value(),
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor,
        )

    def submit_repair(
        self,
        dataset_id: str,
        request: CreateRepairExecutionRequest,
        *,
        idempotency_key: str,
        actor_id: str,
        correlation_id: str | None = None,
    ) -> JobCreatedResponse:
        created = self.create_repair(
            dataset_id,
            request,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        if created.job.state not in _TERMINAL:
            self._schedule(created.job.job_id)
        return created

    def get_repair_confirmation(
        self,
        confirmation_id: str,
    ) -> RepairConfirmationStatusResponse:
        return self._confirmation_status(
            self.context.repair_confirmation_store.get(confirmation_id)
        )

    def _run_update_job(self, job: JobDetails) -> JobDetails:
        request = DatasetUpdateJobRequest.model_validate(job.request)
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
                    "UPDATE_PARENT_CHANGED",
                    "pinned update parent no longer matches the registered dataset hash",
                    False,
                    "update_execution",
                    {
                        "dataset_id": request.dataset_id,
                        "version": request.version,
                        "expected_hash": request.parent_dataset_hash,
                        "actual_hash": descriptor.dataset_hash,
                    },
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
                ).update(
                    request.dataset_id,
                    version=request.version,
                    end=request.end,
                    required_data=request.required_data or None,
                    request_id=job.job_id,
                    format_name=request.storage_format.value,
                    compression=request.compression,
                )
            return self._record_result(job.job_id, result)
        except (StructuredIngestionError, ApplicationError) as exc:
            return self._record_execution_error(job.job_id, exc)
        except Exception as exc:
            safe_error = ApplicationError(
                "UNEXPECTED_UPDATE_ERROR",
                "dataset update failed at an unexpected application boundary",
                False,
                "update_execution",
                {"error_type": type(exc).__name__},
            )
            return self._record_execution_error(job.job_id, safe_error)

    @staticmethod
    def _confirmation_status(
        record: RepairConfirmationRecord,
    ) -> RepairConfirmationStatusResponse:
        return RepairConfirmationStatusResponse(
            confirmation_id=record.confirmation_id,
            dataset_id=record.dataset_id,
            version=record.version,
            preview_id=record.preview_id,
            action=record.action,
            actor_id=record.actor_id,
            created_at=record.created_at,
            expires_at=record.expires_at,
            consumed_at=record.consumed_at,
            revoked_at=record.revoked_at,
            status=record.status,
            reserved_job_id=record.reserved_job_id,
            reserved_at=record.reserved_at,
            consumed_job_id=record.consumed_job_id,
        )

    def _catalog(self) -> DatasetCatalog:
        return DatasetCatalog(
            self.context.dataset_registry,
            self.context.adapters,
        )

    def _inspection(self) -> DatasetInspection:
        return DatasetInspection(
            self.context.dataset_registry,
            self.context.storage.root,
        )

    def _heartbeat(self, lease: JobLease, stop: Event, lost: Event) -> None:
        interval = self.context.settings.lease_heartbeat_seconds
        while not stop.wait(interval):
            try:
                renewed = self.context.lease_store.renew(
                    lease,
                    ttl_seconds=self.context.settings.lease_ttl_seconds,
                )
            except Exception:
                lost.set()
                return
            if renewed is None:
                lost.set()
                return
