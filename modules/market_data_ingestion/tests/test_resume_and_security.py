from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_ingestion.adapters.binance import BinanceAdapter
from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.canonical import CanonicalizationError, sha256_canonical
from market_data_ingestion.checkpoint import load_checkpoint
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference, MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState, SourceKind
from market_data_ingestion.errors import (
    AdapterError,
    StorageError,
    StructuredIngestionError,
)
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.version import (
    NORMALIZATION_VERSION,
    VALIDATION_POLICY_VERSION,
)


def _request(request_id: str = "resume-request-001") -> MarketDataRequest:
    return MarketDataRequest(
        request_id=request_id,
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )


def test_resume_restores_completed_raw_without_network(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    failing = FakeExchangeAdapter(
        chunk_bars=4,
        fail_chunks=frozenset({"resume-request-001:klines:000002"}),
    )
    service = IngestionService(adapters={"fake": failing}, storage=storage, registry=registry)
    with pytest.raises(AdapterError, match="injected adapter failure"):
        service.execute(_request(), format_name="csv")
    execution_hash = sha256_canonical(
        {
            "request": _request(),
            "adapter_id": failing.exchange_id,
            "adapter_version": failing.adapter_version,
            "normalization_version": NORMALIZATION_VERSION,
            "validation_policy_version": VALIDATION_POLICY_VERSION,
            "storage_format": "csv",
            "compression": "zstd",
            "reject_unresolved_required_gaps": True,
            "concurrency": 1,
            "max_records_in_memory": 5_000_000,
        }
    )
    checkpoint_path = storage.root / "checkpoints" / f"mdi:{execution_hash[:24]}.json"
    checkpoint = load_checkpoint(checkpoint_path)
    assert checkpoint.state is RunState.PAUSED
    assert checkpoint.completed_chunks == ("resume-request-001:klines:000001",)

    healthy = FakeExchangeAdapter(chunk_bars=4)
    resumed = IngestionService(adapters={"fake": healthy}, storage=storage, registry=registry)
    result = resumed.execute(_request(), format_name="csv")
    assert result.dataset is not None
    assert healthy.restored_chunks == ["resume-request-001:klines:000001"]
    assert "resume-request-001:klines:000001" not in healthy.acquired_chunks
    assert set(healthy.acquired_chunks) == {
        "resume-request-001:klines:000002",
        "resume-request-001:klines:000003",
    }


def test_corrupt_checkpoint_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StorageError, match="cannot be decoded"):
        load_checkpoint(path)


def test_registered_partition_tamper_is_detected(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    result = service.execute(_request(), format_name="csv")
    assert result.dataset is not None
    descriptor = registry.inspect(result.dataset.dataset_id, 1)
    assert descriptor is not None
    manifest = json.loads((storage.root / result.dataset.manifest_uri).read_text())
    partition = storage.root / manifest["partitions"][0]["storage_uri"]
    partition.write_bytes(partition.read_bytes() + b"tamper")
    report = verify_registered_dataset(storage.root, descriptor)
    assert report["status"] == "error"
    assert any("checksum mismatch" in issue for issue in report["issues"])


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="safe:chunk",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    with pytest.raises(StorageError, match="unsafe run_id"):
        storage.write_raw(
            run_id="../escape",
            instrument=instrument,
            chunk=chunk,
            payload=b"[]",
            source_uri="fake://",
            adapter_version="1",
            response_metadata={"records": 0},
            upstream_checksum=None,
        )


def test_canonicalization_rejects_nonfinite() -> None:
    with pytest.raises(CanonicalizationError):
        sha256_canonical({"value": float("nan")})


def test_binance_rejects_unsafe_archive_member() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.csv", "1,2,3")
    with pytest.raises(AdapterError, match="unsafe or unexpected"):
        BinanceAdapter._safe_archive_rows(payload.getvalue())


def test_binance_rest_raw_frame_rejects_truncation() -> None:
    adapter = BinanceAdapter()
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="binance:chunk:001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    with pytest.raises(AdapterError, match="truncated"):
        adapter.restore_klines(
            chunk,
            instrument,
            b"MDI-RAW-HTTP-PAGES-V1\n" + (100).to_bytes(8, "big") + b"[]",
        )


def test_cli_cancel_paused_run_and_resume_from_persisted_spec(tmp_path: Path, capsys) -> None:
    from market_data_ingestion.cli import main

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = _request()
    failing = FakeExchangeAdapter(fail_chunks=frozenset({"resume-request-001:klines:000001"}))
    with pytest.raises(AdapterError):
        IngestionService(adapters={"fake": failing}, storage=storage, registry=registry).execute(
            request, format_name="csv"
        )
    checkpoints = tuple((storage.root / "checkpoints").glob("mdi:*.json"))
    assert len(checkpoints) == 1
    run_id = checkpoints[0].stem
    assert (storage.root / "runs" / f"{run_id}.json").is_file()

    assert main(["cancel", "--storage-root", str(storage.root), "--run-id", run_id]) == 0
    capsys.readouterr()
    assert load_checkpoint(checkpoints[0]).state is RunState.CANCELLED

    assert main(["resume", "--storage-root", str(storage.root), "--run-id", run_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["final_state"] == "COMPLETED_WITH_WARNINGS"
    assert output["dataset"] is not None
    assert load_checkpoint(checkpoints[0]).state is RunState.COMPLETED_WITH_WARNINGS


def test_preexisting_cancel_marker_stops_before_market_chunks(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = _request()
    adapter = FakeExchangeAdapter()
    execution_hash = sha256_canonical(
        {
            "request": request,
            "adapter_id": adapter.exchange_id,
            "adapter_version": adapter.adapter_version,
            "normalization_version": NORMALIZATION_VERSION,
            "validation_policy_version": VALIDATION_POLICY_VERSION,
            "storage_format": "csv",
            "compression": "zstd",
            "reject_unresolved_required_gaps": True,
            "concurrency": 1,
            "max_records_in_memory": 5_000_000,
        }
    )
    run_id = f"mdi:{execution_hash[:24]}"
    storage.request_cancel(run_id)
    result = IngestionService(
        adapters={"fake": adapter}, storage=storage, registry=registry
    ).execute(request, format_name="csv")
    assert result.final_state is RunState.CANCELLED
    assert result.dataset is None
    assert adapter.acquired_chunks == []
    assert (
        load_checkpoint(storage.root / "checkpoints" / f"{run_id}.json").state is RunState.CANCELLED
    )


def test_corrupt_resume_spec_fails_closed(tmp_path: Path) -> None:
    from market_data_ingestion.run_io import load_resume_spec

    storage = LocalStorage(tmp_path / "data")
    run_id = "mdi:0123456789abcdef01234567"
    path = storage.root / "runs" / f"{run_id}.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StorageError, match="cannot be decoded"):
        load_resume_spec(storage.root, run_id)


def test_bounded_concurrency_is_used_without_changing_dataset_hash(tmp_path: Path) -> None:
    import threading
    import time

    class ConcurrentFakeAdapter(FakeExchangeAdapter):
        def __init__(self) -> None:
            super().__init__(chunk_bars=1)
            self._guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.01)
                return super().acquire(chunk, instrument)
            finally:
                with self._guard:
                    self.active -= 1

    concurrent_storage = LocalStorage(tmp_path / "concurrent")
    concurrent_adapter = ConcurrentFakeAdapter()
    concurrent = IngestionService(
        adapters={"fake": concurrent_adapter},
        storage=concurrent_storage,
        registry=SQLiteDatasetRegistry(concurrent_storage.root / "registry.sqlite3"),
    ).execute(_request("concurrent-request"), format_name="csv", concurrency=4)

    sequential_storage = LocalStorage(tmp_path / "sequential")
    sequential = IngestionService(
        adapters={"fake": FakeExchangeAdapter(chunk_bars=1)},
        storage=sequential_storage,
        registry=SQLiteDatasetRegistry(sequential_storage.root / "registry.sqlite3"),
    ).execute(_request("sequential-request"), format_name="csv", concurrency=1)

    assert concurrent.dataset is not None and sequential.dataset is not None
    assert concurrent_adapter.max_active >= 2
    assert concurrent.dataset.dataset_hash == sequential.dataset.dataset_hash


def test_concurrent_batch_persists_successes_before_failure_and_resume(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    failing = FakeExchangeAdapter(
        chunk_bars=1,
        fail_chunks=frozenset({"concurrent-resume:klines:000002"}),
    )
    request = _request("concurrent-resume")
    with pytest.raises(AdapterError, match="injected adapter failure"):
        IngestionService(adapters={"fake": failing}, storage=storage, registry=registry).execute(
            request, format_name="csv", concurrency=4
        )

    checkpoints = tuple((storage.root / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 1
    checkpoint = load_checkpoint(checkpoints[0])
    assert checkpoint.state is RunState.PAUSED
    assert set(checkpoint.completed_chunks) >= {
        "concurrent-resume:klines:000001",
        "concurrent-resume:klines:000003",
        "concurrent-resume:klines:000004",
    }

    healthy = FakeExchangeAdapter(chunk_bars=1)
    result = IngestionService(
        adapters={"fake": healthy}, storage=storage, registry=registry
    ).execute(request, format_name="csv", concurrency=4)
    assert result.dataset is not None
    assert "concurrent-resume:klines:000002" in healthy.acquired_chunks
    assert {
        "concurrent-resume:klines:000001",
        "concurrent-resume:klines:000003",
        "concurrent-resume:klines:000004",
    }.issubset(set(healthy.restored_chunks))


def test_dead_process_lock_is_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = LocalStorage(tmp_path / "data")
    lock_path = storage.root / "locks" / "dead-worker.lock"
    lock_path.write_text(
        "pid=42424242\ncreated_at=2026-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "_process_alive", lambda pid: False)

    with storage.lock("dead-worker"):
        assert lock_path.is_file()
        assert f"pid={__import__('os').getpid()}" in lock_path.read_text(encoding="utf-8")

    assert not lock_path.exists()


def test_malformed_lock_fails_closed(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    lock_path = storage.root / "locks" / "corrupt.lock"
    lock_path.write_text("not-a-valid-lock", encoding="utf-8")

    with pytest.raises(StorageError) as raised, storage.lock("corrupt"):
        pass

    assert raised.value.code == "LOCK_CORRUPT"
    assert lock_path.is_file()


def test_symbolic_link_lock_fails_closed(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    target = tmp_path / "outside-lock"
    target.write_text("pid=99999999\n", encoding="utf-8")
    lock_path = storage.root / "locks" / "unsafe.lock"
    try:
        lock_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(StorageError) as raised, storage.lock("unsafe"):
        pass

    assert raised.value.code == "LOCK_PATH_UNSAFE"
    assert target.read_text(encoding="utf-8") == "pid=99999999\n"


def test_atomic_write_rejects_symbolic_link_destination(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    outside = tmp_path / "outside-checkpoint"
    outside.write_text("preserve-me", encoding="utf-8")
    destination = storage.root / "checkpoints" / "unsafe.json"
    try:
        destination.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(StorageError) as raised:
        storage._atomic_write(destination, b"replacement", immutable=False)

    assert raised.value.code == "SYMLINK_PATH"
    assert outside.read_text(encoding="utf-8") == "preserve-me"


def test_atomic_write_rejects_symbolic_link_parent(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    linked_parent = storage.root / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(StorageError) as raised:
        storage._atomic_write(linked_parent / "artifact.bin", b"payload", immutable=False)

    assert raised.value.code == "SYMLINK_PATH"
    assert not (outside / "artifact.bin").exists()


def test_atomic_write_maps_disk_full_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    storage = LocalStorage(tmp_path / "data")
    destination = storage.root / "checkpoints" / "disk-full.json"
    real_replace = os.replace

    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(StorageError) as raised:
        storage._atomic_write(destination, b"payload", immutable=False)
    monkeypatch.setattr(os, "replace", real_replace)

    assert raised.value.code == "DISK_SPACE_EXHAUSTED"
    assert raised.value.retryable is True
    assert not destination.exists()
    assert not tuple(destination.parent.glob(f".{destination.name}.*.tmp"))


def test_atomic_write_maps_permission_denied_before_temporary_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import tempfile

    storage = LocalStorage(tmp_path / "data")
    destination = storage.root / "checkpoints" / "forbidden.json"

    def deny_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        del args, kwargs
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", deny_mkstemp)
    with pytest.raises(StorageError) as raised:
        storage._atomic_write(destination, b"payload", immutable=False)

    assert raised.value.code == "STORAGE_PERMISSION_DENIED"
    assert raised.value.retryable is False
    assert not destination.exists()


def test_instrument_metadata_change_during_download_pauses_run(tmp_path: Path) -> None:
    from dataclasses import replace

    class ChangingMetadataAdapter(FakeExchangeAdapter):
        def __init__(self) -> None:
            super().__init__(chunk_bars=1)
            self.resolve_calls = 0

        def resolve_instrument(self, market_type: object, symbol: str):  # type: ignore[no-untyped-def]
            self.resolve_calls += 1
            metadata = super().resolve_instrument(market_type, symbol)
            return replace(metadata, tick_size="0.25") if self.resolve_calls >= 2 else metadata

    storage = LocalStorage(tmp_path / "data")
    adapter = ChangingMetadataAdapter()
    with pytest.raises(AdapterError) as raised:
        IngestionService(
            adapters={"fake": adapter},
            storage=storage,
            registry=SQLiteDatasetRegistry(storage.root / "registry.sqlite3"),
        ).execute(_request("metadata-change"), format_name="csv", concurrency=2)

    assert raised.value.code == "INSTRUMENT_METADATA_CHANGED"
    checkpoints = tuple((storage.root / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 1
    assert load_checkpoint(checkpoints[0]).state is RunState.PAUSED
    assert tuple((storage.root / "raw").rglob("*.raw"))


def test_in_memory_record_limit_pauses_before_validation(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    with pytest.raises(AdapterError) as raised:
        IngestionService(
            adapters={"fake": FakeExchangeAdapter()},
            storage=storage,
            registry=SQLiteDatasetRegistry(storage.root / "registry.sqlite3"),
        ).execute(
            _request("memory-bound"),
            format_name="csv",
            max_records_in_memory=3,
        )
    assert raised.value.code == "IN_MEMORY_RECORD_LIMIT"
    assert raised.value.details["maximum_records"] == 3
    checkpoints = tuple((storage.root / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 1
    assert load_checkpoint(checkpoints[0]).state is RunState.PAUSED
    assert tuple((storage.root / "raw").rglob("*.raw"))


def test_unexpected_adapter_exception_is_structured_and_checkpointed(tmp_path: Path) -> None:
    class ExplodingAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            del chunk, instrument
            raise RuntimeError("unexpected adapter bug")

    storage = LocalStorage(tmp_path / "data")
    with pytest.raises(StructuredIngestionError) as captured:
        IngestionService(
            adapters={"fake": ExplodingAdapter()},
            storage=storage,
            registry=SQLiteDatasetRegistry(storage.root / "registry.sqlite3"),
        ).execute(_request("unexpected-adapter"), format_name="csv")

    assert captured.value.code == "UNEXPECTED_ACQUISITION_FAILURE"
    assert captured.value.details["error_type"] == "RuntimeError"
    checkpoints = tuple((storage.root / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 1
    assert load_checkpoint(checkpoints[0]).state is RunState.PAUSED


@pytest.mark.parametrize("field_name", ["byte_size", "start", "artifact_id", "adapter_version"])
def test_raw_restore_rejects_descriptor_context_tamper(
    tmp_path: Path,
    field_name: str,
) -> None:
    storage = LocalStorage(tmp_path / f"raw-context-{field_name}")
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="raw-context:klines:000001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
        expected_uri="https://example.invalid/klines",
    )
    storage.write_raw(
        run_id="raw-context-run",
        instrument=instrument,
        chunk=chunk,
        payload=b"[]",
        source_uri="https://example.invalid/klines",
        adapter_version="1.0.0",
        response_metadata={"records": 0},
        upstream_checksum=None,
    )
    metadata_path = storage.raw_path(instrument, chunk).with_name(
        "raw-context:klines:000001.raw.meta.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if field_name == "byte_size":
        metadata[field_name] += 1
    elif field_name == "start":
        metadata[field_name] = datetime(
            2026,
            1,
            1,
            0,
            1,
            tzinfo=UTC,
        ).isoformat()
    elif field_name == "artifact_id":
        metadata[field_name] = "raw:000000000000000000000000"
    else:
        metadata[field_name] = "9.9.9"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(StorageError) as captured:
        storage.read_raw(instrument, chunk)

    assert captured.value.code == "RAW_DESCRIPTOR_CONTEXT_MISMATCH"
    expected_detail = "artifact_id" if field_name == "adapter_version" else field_name
    assert expected_detail in captured.value.details


def test_raw_restore_rejects_metadata_symlink(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "raw-symlink")
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="raw-symlink:klines:000001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    storage.write_raw(
        run_id="raw-symlink-run",
        instrument=instrument,
        chunk=chunk,
        payload=b"[]",
        source_uri="https://example.invalid/klines",
        adapter_version="1.0.0",
        response_metadata={"records": 0},
        upstream_checksum=None,
    )
    payload_path = storage.raw_path(instrument, chunk)
    metadata_path = payload_path.with_name(payload_path.name + ".meta.json")
    copied = metadata_path.with_name("safe-copy.meta.json")
    copied.write_bytes(metadata_path.read_bytes())
    metadata_path.unlink()
    metadata_path.symlink_to(copied.name)

    with pytest.raises(StorageError) as captured:
        storage.read_raw(instrument, chunk)

    assert captured.value.code == "SYMLINK_PATH"


def _stored_raw_fixture(
    tmp_path: Path,
    name: str,
) -> tuple[LocalStorage, InstrumentReference, DownloadChunk, Path, Path]:
    storage = LocalStorage(tmp_path / name)
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id=f"{name}:klines:000001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
        expected_uri="https://example.invalid/klines",
    )
    storage.write_raw(
        run_id=f"{name}-run",
        instrument=instrument,
        chunk=chunk,
        payload=b"[]",
        source_uri="https://example.invalid/klines",
        adapter_version="1.0.0",
        response_metadata={"records": 0},
        upstream_checksum=None,
    )
    payload_path = storage.raw_path(instrument, chunk)
    metadata_path = payload_path.with_name(payload_path.name + ".meta.json")
    return storage, instrument, chunk, payload_path, metadata_path


@pytest.mark.parametrize(
    ("mutation", "expected_error_type"),
    [
        ("invalid_json", "JSONDecodeError"),
        ("missing_field", "KeyError"),
        ("invalid_source", "ValueError"),
        ("invalid_timestamp", "ValueError"),
        ("invalid_response_metadata", "TypeError"),
    ],
)
def test_raw_restore_maps_malformed_metadata_to_structured_error(
    tmp_path: Path,
    mutation: str,
    expected_error_type: str,
) -> None:
    storage, instrument, chunk, _, metadata_path = _stored_raw_fixture(
        tmp_path,
        f"raw-malformed-{mutation}",
    )
    if mutation == "invalid_json":
        metadata_path.write_text("{", encoding="utf-8")
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if mutation == "missing_field":
            del metadata["artifact_id"]
        elif mutation == "invalid_source":
            metadata["source"] = "untrusted_mirror"
        elif mutation == "invalid_timestamp":
            metadata["retrieved_at"] = "not-a-timestamp"
        else:
            metadata["response_metadata"] = 7
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(StorageError) as captured:
        storage.read_raw(instrument, chunk)

    assert captured.value.code == "RAW_METADATA_CORRUPT"
    assert captured.value.details["error_type"] == expected_error_type
    assert captured.value.details["metadata_path"] == str(metadata_path)


def test_raw_restore_rejects_source_uri_tamper(tmp_path: Path) -> None:
    storage, instrument, chunk, _, metadata_path = _stored_raw_fixture(
        tmp_path,
        "raw-source-uri-tamper",
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_uri"] = "https://attacker.invalid/substitute"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(StorageError) as captured:
        storage.read_raw(instrument, chunk)

    assert captured.value.code == "RAW_DESCRIPTOR_CONTEXT_MISMATCH"
    assert "artifact_id" in captured.value.details


def test_raw_restore_rejects_payload_symlink(tmp_path: Path) -> None:
    storage, instrument, chunk, payload_path, _ = _stored_raw_fixture(
        tmp_path,
        "raw-payload-symlink",
    )
    copied = payload_path.with_name("safe-copy.raw")
    copied.write_bytes(payload_path.read_bytes())
    payload_path.unlink()
    payload_path.symlink_to(copied.name)

    with pytest.raises(StorageError) as captured:
        storage.read_raw(instrument, chunk)

    assert captured.value.code == "SYMLINK_PATH"


def test_write_raw_rejects_secret_bearing_uri_before_publication(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "raw-secret-uri")
    instrument = InstrumentReference(
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        canonical_id="fake:spot:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="raw-secret-uri:klines:000001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )

    with pytest.raises(ValueError, match="query or fragment"):
        storage.write_raw(
            run_id="raw-secret-uri-run",
            instrument=instrument,
            chunk=chunk,
            payload=b"[]",
            source_uri="https://example.invalid/klines?api_key=secret",
            adapter_version="1.0.0",
            response_metadata={"records": 0},
            upstream_checksum=None,
        )

    assert not tuple((storage.root / "raw").rglob("*.raw"))
    assert not tuple((storage.root / "raw").rglob("*.meta.json"))
