from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import market_data_ingestion.funding_dataset_reader as funding_reader
from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.contracts import NormalizedPartitionDescriptor, RawArtifactDescriptor
from market_data_ingestion.enums import DataType, MarketType, SourceKind, ValidationStatus
from market_data_ingestion.errors import RegistryError, StorageError
from market_data_ingestion.funding_dataset_reader import (
    RegisteredFundingSelection,
    _funding_partition_records,
    iter_registered_funding,
    resolve_registered_funding_dataset,
)

START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
END = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def _write_funding(
    path: Path,
    timestamps: list[datetime],
    *,
    symbol: str = "BTCUSDT",
    source_kind: str = "official_rest",
) -> None:
    count = len(timestamps)
    pq.write_table(
        pa.table(
            {
                "funding_time": timestamps,
                "funding_rate": ["0.0001"] * count,
                "mark_price": ["90000"] * count,
                "symbol": [symbol] * count,
                "exchange_id": ["binance"] * count,
                "market_type": ["usdt_m_perpetual"] * count,
                "source_kind": [source_kind] * count,
            }
        ),
        path,
    )


def _raw(
    *,
    record_count: int | None = 2,
    artifact_id: str = "raw-funding",
    start: datetime = START,
    end: datetime = END,
) -> RawArtifactDescriptor:
    return RawArtifactDescriptor(
        artifact_id=artifact_id,
        source=SourceKind.OFFICIAL_REST,
        source_uri=f"https://data.binance.example/{artifact_id}",
        retrieved_at=END,
        checksum_sha256="a" * 64,
        byte_size=1024,
        record_count=record_count,
        start=start,
        end=end,
        adapter_version="1.0.0",
    )


def _partition(
    path: Path,
    *,
    row_count: int = 2,
    partition_id: str = "funding-2026-01",
    source_ids: tuple[str, ...] = ("raw-funding",),
    start: datetime = START,
    end: datetime = END,
) -> NormalizedPartitionDescriptor:
    return NormalizedPartitionDescriptor(
        partition_id=partition_id,
        data_type=DataType.FUNDING_RATE,
        timeframe=None,
        storage_uri=path.name,
        checksum_sha256=sha256_file(path),
        row_count=row_count,
        start=start,
        end=end,
        schema_version=1,
        source_artifact_ids=source_ids,
    )


def _selection(
    root: Path,
    partition: NormalizedPartitionDescriptor,
    *,
    raw: RawArtifactDescriptor | None = None,
) -> RegisteredFundingSelection:
    source = raw or _raw(record_count=partition.row_count)
    descriptor = SimpleNamespace(
        dataset_id="binance-btcusdt-funding",
        version=1,
        dataset_hash="b" * 64,
        instrument_id="binance:usdt-m:btcusdt",
        validation_status=ValidationStatus.VALID,
    )
    manifest = SimpleNamespace(
        instrument=SimpleNamespace(symbol="BTCUSDT"),
        exchange=SimpleNamespace(id="binance"),
        market_type=MarketType.USDT_M_PERPETUAL,
    )
    return RegisteredFundingSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        partitions=(partition,),
        source_artifacts=(source,),
    )


def test_registered_funding_stream_verifies_rows_and_public_evidence(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    timestamps = [START, START + timedelta(hours=8)]
    _write_funding(path, timestamps)
    selection = _selection(root, _partition(path))

    records = list(iter_registered_funding(selection))
    payload = selection.to_payload()

    assert [item.funding_time for item in records] == timestamps
    assert selection.event_count == 2
    assert selection.source_record_count == 2
    assert payload["event_count"] == 2
    assert payload["source_record_count"] == 2
    assert payload["dataset_hash"] == "b" * 64
    assert payload["coverage_start_inclusive"] == START.isoformat()
    assert payload["coverage_end_exclusive"] == END.isoformat()
    assert payload["coverage_semantics"] == "[start,end)"
    assert payload["coverage_basis"] == "raw_source_artifact_intervals"
    assert payload["event_span_start_inclusive"] == START.isoformat()
    assert payload["event_span_end_exclusive"] == END.isoformat()
    assert "coverage_start" not in payload
    assert "coverage_end" not in payload
    assert "storage_root" not in payload
    artifacts = payload["source_artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts[0]["checksum_sha256"] == "a" * 64
    assert "source_uri" not in artifacts[0]


def test_selection_coverage_uses_raw_interval_not_sparse_event_span(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    event_time = START + timedelta(hours=8)
    event_end = event_time + timedelta(microseconds=1)
    _write_funding(path, [event_time])
    partition = _partition(path, row_count=1, start=event_time, end=event_end)
    selection = _selection(root, partition, raw=_raw(record_count=1))

    payload = selection.to_payload()

    assert payload["coverage_start_inclusive"] == START.isoformat()
    assert payload["coverage_end_exclusive"] == END.isoformat()
    assert payload["event_span_start_inclusive"] == event_time.isoformat()
    assert payload["event_span_end_exclusive"] == event_end.isoformat()


def test_funding_partition_rejects_checksum_and_row_count_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])
    partition = _partition(path)
    selection = _selection(root, partition)

    wrong_hash = replace(partition, checksum_sha256="0" * 64)
    with pytest.raises(StorageError, match="checksum"):
        list(_funding_partition_records(selection, wrong_hash))

    wrong_count = replace(partition, row_count=3)
    with pytest.raises(StorageError, match="row count"):
        list(_funding_partition_records(selection, wrong_count))


def test_funding_partition_rejects_wrong_record_identity(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    _write_funding(path, [START], symbol="ETHUSDT")
    partition = _partition(path, row_count=1)
    selection = _selection(root, partition)

    with pytest.raises(StorageError, match="identity"):
        list(_funding_partition_records(selection, partition))


def test_registered_funding_stream_rejects_duplicate_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    timestamp = START + timedelta(hours=8)
    _write_funding(path, [timestamp, timestamp])
    partition = _partition(path, row_count=2)
    selection = _selection(root, partition)

    with pytest.raises(StorageError, match="duplicate timestamp"):
        list(iter_registered_funding(selection))


def _patch_resolver(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    partition: NormalizedPartitionDescriptor,
    raw: RawArtifactDescriptor | tuple[RawArtifactDescriptor, ...],
    *,
    descriptor_data_types: tuple[DataType, ...] = (DataType.FUNDING_RATE,),
) -> None:
    descriptor = SimpleNamespace(
        dataset_id="funding-dataset",
        version=1,
        dataset_hash="b" * 64,
        instrument_id="binance:usdt-m:btcusdt",
        storage_uri=root.as_uri(),
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        compatible_consumers=("modular_backtest",),
        data_types=descriptor_data_types,
        start=START,
        end=END,
    )
    raw_artifacts = raw if isinstance(raw, tuple) else (raw,)
    manifest = SimpleNamespace(
        validation=SimpleNamespace(status=ValidationStatus.VALID),
        partitions=(partition,),
        source_artifacts=raw_artifacts,
        instrument=SimpleNamespace(symbol="BTCUSDT"),
        exchange=SimpleNamespace(id="binance"),
        market_type=MarketType.USDT_M_PERPETUAL,
    )

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            pass

        def inspect(self, dataset_id: str, version: int) -> dict[str, object] | None:
            assert dataset_id == "funding-dataset"
            assert version == 1
            return {"dataset_id": dataset_id, "version": version}

    monkeypatch.setattr(funding_reader, "SQLiteDatasetRegistry", FakeRegistry)
    monkeypatch.setattr(funding_reader, "descriptor_from_dict", lambda _payload: descriptor)
    monkeypatch.setattr(
        funding_reader, "verify_registered_dataset", lambda *_a, **_kw: {"status": "ok"}
    )
    monkeypatch.setattr(funding_reader, "load_manifest", lambda *_a, **_kw: manifest)


def test_resolver_binds_expected_count_to_raw_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    path = root / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])
    partition = _partition(path)
    _patch_resolver(monkeypatch, root, partition, _raw(record_count=2))

    selection = resolve_registered_funding_dataset(root, "funding-dataset", 1)

    assert selection.event_count == 2
    assert selection.source_record_count == 2
    payload = selection.to_payload()
    assert payload["validation_status"] == "VALID"
    assert payload["coverage_semantics"] == "[start,end)"
    assert payload["coverage_basis"] == "raw_source_artifact_intervals"


def test_resolver_ignores_unreferenced_nonfunding_raw_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    path = root / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])
    partition = _partition(path)
    _patch_resolver(
        monkeypatch,
        root,
        partition,
        (
            _raw(record_count=2),
            _raw(record_count=1, artifact_id="raw-unrelated"),
        ),
    )

    selection = resolve_registered_funding_dataset(root, "funding-dataset", 1)

    assert [item.artifact_id for item in selection.source_artifacts] == ["raw-funding"]
    assert selection.source_record_count == 2


def test_resolver_rejects_gap_in_raw_funding_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    path = root / "funding.parquet"
    _write_funding(path, [START + timedelta(hours=1), START + timedelta(hours=9)])
    partition = _partition(path, source_ids=("raw-a", "raw-b"))
    raw_a = _raw(
        record_count=1,
        artifact_id="raw-a",
        start=START,
        end=START + timedelta(hours=4),
    )
    raw_b = _raw(
        record_count=1,
        artifact_id="raw-b",
        start=START + timedelta(hours=8),
        end=END,
    )
    _patch_resolver(monkeypatch, root, partition, (raw_a, raw_b))

    with pytest.raises(StorageError, match="uncovered acquisition interval"):
        resolve_registered_funding_dataset(root, "funding-dataset", 1)


@pytest.mark.parametrize("raw_count", [None, 1, 3])
def test_resolver_fails_closed_without_exact_raw_record_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_count: int | None,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    path = root / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])
    partition = _partition(path)
    _patch_resolver(monkeypatch, root, partition, _raw(record_count=raw_count))

    with pytest.raises(StorageError, match="count"):
        resolve_registered_funding_dataset(root, "funding-dataset", 1)


def test_resolver_rejects_dataset_without_funding_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    path = root / "funding.parquet"
    _write_funding(path, [START])
    partition = _partition(path, row_count=1)
    _patch_resolver(
        monkeypatch,
        root,
        partition,
        _raw(record_count=1),
        descriptor_data_types=(DataType.KLINES,),
    )

    with pytest.raises(RegistryError, match="does not contain funding"):
        resolve_registered_funding_dataset(root, "funding-dataset", 1)
