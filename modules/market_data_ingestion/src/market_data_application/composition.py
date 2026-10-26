from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from market_data_ingestion.adapters import (
    BinanceAdapter,
    ExchangeAdapter,
    FakeExchangeAdapter,
)
from market_data_ingestion.network import HttpClient
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry_schema import (
    VersionedSQLiteDatasetRegistry,
    open_versioned_registry,
)
from market_data_ingestion.storage_recovery import (
    RecoverableLocalStorage,
    open_recoverable_storage,
)

from .artifact_store import ArtifactStore
from .job_store import SQLiteJobStore
from .lease_store import FencedJobStore, SQLiteJobLeaseStore
from .repair_confirmation_store import SQLiteRepairConfirmationStore
from .settings import ApplicationSettings


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    settings: ApplicationSettings
    storage: RecoverableLocalStorage
    artifact_store: ArtifactStore
    dataset_registry: VersionedSQLiteDatasetRegistry
    job_store: FencedJobStore
    lease_store: SQLiteJobLeaseStore
    repair_confirmation_store: SQLiteRepairConfirmationStore
    ingestion_service: IngestionService
    adapters: Mapping[str, ExchangeAdapter]


def build_application_context(
    settings: ApplicationSettings,
    *,
    adapters: Mapping[str, ExchangeAdapter] | None = None,
) -> ApplicationContext:
    """Build one backend context used by all transports.

    Tests may inject adapters. Production composition exposes the public Binance adapter
    and adds the deterministic fake adapter only when explicitly enabled.
    """

    settings.storage_root.mkdir(parents=True, exist_ok=True)
    configured_adapters: dict[str, ExchangeAdapter]
    if adapters is None:
        configured_adapters = {
            "binance": BinanceAdapter(
                HttpClient(
                    timeout_seconds=settings.network_timeout_seconds,
                    retries=settings.network_retries,
                )
            )
        }
        if settings.include_fake_adapter:
            configured_adapters["fake"] = FakeExchangeAdapter()
    else:
        configured_adapters = dict(adapters)
    if not configured_adapters:
        raise ValueError("at least one exchange adapter must be configured")
    for exchange_id, adapter in configured_adapters.items():
        if exchange_id != adapter.exchange_id:
            raise ValueError("adapter mapping key must equal adapter.exchange_id")

    storage = open_recoverable_storage(settings.storage_root)
    artifact_store = ArtifactStore(
        storage,
        max_artifact_bytes=settings.artifact_max_bytes,
        retention_count=settings.artifact_retention_count,
        retention_total_bytes=settings.artifact_retention_total_bytes,
    )
    dataset_registry = open_versioned_registry(settings.registry_path)
    raw_job_store = SQLiteJobStore(settings.jobs_path)
    lease_store = SQLiteJobLeaseStore(settings.jobs_path)
    job_store = FencedJobStore(raw_job_store, lease_store)
    repair_confirmation_store = SQLiteRepairConfirmationStore(settings.jobs_path)
    ingestion_service = IngestionService(
        adapters=configured_adapters,
        storage=storage,
        registry=dataset_registry,
    )
    return ApplicationContext(
        settings=settings,
        storage=storage,
        artifact_store=artifact_store,
        dataset_registry=dataset_registry,
        job_store=job_store,
        lease_store=lease_store,
        repair_confirmation_store=repair_confirmation_store,
        ingestion_service=ingestion_service,
        adapters=configured_adapters,
    )
