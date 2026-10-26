from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.adapters.base import AcquiredData
from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.errors import AdapterError, RegistryError, StorageError
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def make_request(request_id: str = "request-001", *, end_minutes: int = 60) -> MarketDataRequest:
    return MarketDataRequest(
        request_id=request_id,
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=end_minutes),
    )


def test_fake_end_to_end_and_identical_request_reuse(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    first = service.execute(make_request(), format_name="csv")
    assert first.final_state is RunState.COMPLETED
    assert first.dataset is not None
    assert set(first.dataset.timeframes) == {"5m", "15m", "1h"}
    assert (storage.root / first.dataset.manifest_uri).is_file()
    second = service.execute(make_request(), format_name="csv")
    assert second.dataset == first.dataset
    assert second.final_state is RunState.COMPLETED_WITH_WARNINGS


def test_same_request_id_different_content_fails_closed(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    service.execute(make_request(), format_name="csv")
    with pytest.raises(RegistryError, match="different canonical content"):
        service.execute(make_request(end_minutes=65), format_name="csv")


def test_conflicting_duplicate_rejects_dataset(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    # Fake duplicate is exact, so it produces a warning and remains valid.
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter(duplicate_page=True)},
        storage=storage,
        registry=registry,
    )
    result = service.execute(make_request(), format_name="csv")
    assert result.final_state is RunState.COMPLETED_WITH_WARNINGS
    assert result.validation is not None and result.validation.duplicates


def test_empty_required_interval_stream_is_rejected_without_registration(
    tmp_path,
) -> None:
    class EmptyAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            del instrument
            return AcquiredData(
                chunk=chunk,
                source_uri="fake://empty",
                raw_payload=b"[]",
                records=(),
                response_metadata={"records": 0},
            )

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": EmptyAdapter()},
        storage=storage,
        registry=registry,
    ).execute(make_request("empty-required"), format_name="csv")

    assert result.final_state is RunState.DATASET_REJECTED
    assert result.dataset is None
    assert result.validation is not None
    assert result.validation.gaps[0].missing_intervals == 12
    assert registry.health()["datasets"] == 0


def test_adapter_cannot_substitute_a_different_planned_chunk(tmp_path) -> None:
    class MismatchedChunkAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            acquired = super().acquire(chunk, instrument)
            return replace(
                acquired,
                chunk=replace(
                    acquired.chunk,
                    end=acquired.chunk.end + timedelta(minutes=5),
                ),
            )

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": MismatchedChunkAdapter()},
        storage=storage,
        registry=registry,
    )

    with pytest.raises(AdapterError) as captured:
        service.execute(make_request("chunk-substitution"), format_name="csv")

    assert captured.value.code == "ACQUIRED_CHUNK_MISMATCH"
    assert "end" in captured.value.details
    assert registry.health()["datasets"] == 0


def test_storage_lock_is_exclusive(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    with (
        storage.lock("same-run"),
        pytest.raises(StorageError, match="already exists"),
        storage.lock("same-run"),
    ):
        pass


def test_request_identity_includes_storage_and_adapter_versions(tmp_path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    service.execute(make_request("execution-identity"), format_name="csv")

    with pytest.raises(RegistryError, match="different canonical content"):
        service.execute(make_request("execution-identity"), format_name="parquet")

    class NewFakeAdapter(FakeExchangeAdapter):
        adapter_version = "2.0.0"

    upgraded = IngestionService(
        adapters={"fake": NewFakeAdapter()}, storage=storage, registry=registry
    )
    with pytest.raises(RegistryError, match="different canonical content"):
        upgraded.execute(make_request("execution-identity"), format_name="csv")


def test_dataset_hash_is_stable_across_independent_identical_ingestions(tmp_path) -> None:
    first_storage = LocalStorage(tmp_path / "first")
    second_storage = LocalStorage(tmp_path / "second")
    first = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=first_storage,
        registry=SQLiteDatasetRegistry(first_storage.root / "registry.sqlite3"),
    ).execute(make_request("stable-hash-one"), format_name="csv")
    second = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=second_storage,
        registry=SQLiteDatasetRegistry(second_storage.root / "registry.sqlite3"),
    ).execute(make_request("stable-hash-two"), format_name="csv")
    assert first.dataset is not None and second.dataset is not None
    assert first.dataset.dataset_hash == second.dataset.dataset_hash


def test_adapter_version_changes_dataset_hash(tmp_path) -> None:
    class NewAdapterVersion(FakeExchangeAdapter):
        adapter_version = "1.3.0"

    first_storage = LocalStorage(tmp_path / "first-version")
    second_storage = LocalStorage(tmp_path / "second-version")
    first = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=first_storage,
        registry=SQLiteDatasetRegistry(first_storage.root / "registry.sqlite3"),
    ).execute(make_request("adapter-hash-one"), format_name="csv")
    second = IngestionService(
        adapters={"fake": NewAdapterVersion()},
        storage=second_storage,
        registry=SQLiteDatasetRegistry(second_storage.root / "registry.sqlite3"),
    ).execute(make_request("adapter-hash-two"), format_name="csv")
    assert first.dataset is not None and second.dataset is not None
    assert first.dataset.dataset_hash != second.dataset.dataset_hash


def test_registry_migrates_legacy_unique_hash_schema(tmp_path) -> None:
    import sqlite3
    from contextlib import closing

    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE datasets (
                dataset_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                dataset_hash TEXT NOT NULL CHECK(length(dataset_hash) = 64),
                descriptor_json TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                PRIMARY KEY(dataset_id, version),
                UNIQUE(dataset_hash)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE ingestion_requests (
                request_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
                descriptor_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
    SQLiteDatasetRegistry(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        schema = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='datasets'"
            ).fetchone()[0]
        )
        assert "UNIQUE(dataset_hash)" not in schema.replace(" ", "")
        digest = "ab" * 32
        connection.execute(
            "INSERT INTO datasets VALUES(?,?,?,?,?)",
            ("same", 1, digest, "{}", datetime.now(UTC).isoformat()),
        )
        connection.execute(
            "INSERT INTO datasets VALUES(?,?,?,?,?)",
            ("same", 2, digest, "{}", datetime.now(UTC).isoformat()),
        )


def test_validation_policy_changes_execution_identity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market_data_ingestion import pipeline as pipeline_module

    request = make_request("policy-identity")
    first_storage = LocalStorage(tmp_path / "first")
    first = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=first_storage,
        registry=SQLiteDatasetRegistry(first_storage.root / "registry.sqlite3"),
    ).execute(request, format_name="csv")
    assert first.dataset is not None

    monkeypatch.setattr(
        pipeline_module,
        "VALIDATION_POLICY_VERSION",
        "999.0.0",
    )
    second_storage = LocalStorage(tmp_path / "second")
    second = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=second_storage,
        registry=SQLiteDatasetRegistry(second_storage.root / "registry.sqlite3"),
    ).execute(request, format_name="csv")
    assert second.dataset is not None

    assert first.run_id != second.run_id
    assert first.dataset.dataset_hash != second.dataset.dataset_hash
