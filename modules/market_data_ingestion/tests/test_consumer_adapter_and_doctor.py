from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import namedtuple
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.doctor import doctor
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.results_registry_adapter import (
    to_results_registry_dataset_record,
)
from market_data_ingestion.storage import LocalStorage


def _registered_multitype_dataset(tmp_path: Path):  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = MarketDataRequest(
        request_id="consumer-adapter",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES, DataType.FUNDING_RATE),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    descriptor = (
        IngestionService(
            adapters={"fake": FakeExchangeAdapter()},
            storage=storage,
            registry=registry,
        )
        .execute(request, format_name="csv")
        .dataset
    )
    assert descriptor is not None
    return storage, registry, descriptor, load_manifest(storage.root, descriptor)


def test_results_registry_adapter_uses_stream_specific_lineage_hashes(
    tmp_path: Path,
) -> None:
    _, _, descriptor, manifest = _registered_multitype_dataset(tmp_path)

    record = to_results_registry_dataset_record(descriptor, manifest)

    assert record["dataset_id"] == f"{descriptor.dataset_id}:v1"
    assert record["row_count"] == 12
    assert record["timeframe"] == "5m"
    assert record["sha256"] == descriptor.dataset_hash
    assert record["metadata_hash"] not in {None, descriptor.dataset_hash}
    assert record["funding_data_hash"] not in {None, descriptor.dataset_hash}
    assert record["metadata_hash"] != record["funding_data_hash"]
    assert record["timezone_name"] == "UTC"
    assert set(record) == {
        "aggregation_version",
        "dataset_id",
        "duplicate_summary",
        "funding_data_hash",
        "gap_summary",
        "instrument",
        "market",
        "market_type",
        "max_timestamp",
        "metadata_hash",
        "min_timestamp",
        "preprocessing_version",
        "quality_status",
        "row_count",
        "sha256",
        "source",
        "timeframe",
        "timezone_name",
    }


def test_results_registry_adapter_rejects_descriptor_manifest_mismatch(
    tmp_path: Path,
) -> None:
    _, _, descriptor, manifest = _registered_multitype_dataset(tmp_path)
    mismatched = replace(
        descriptor,
        instrument_id="fake:usdt_m_perpetual:ethusdt",
    )

    try:
        to_results_registry_dataset_record(mismatched, manifest)
    except ValueError as exc:
        assert "instrument_id" in str(exc)
    else:
        raise AssertionError("mismatched descriptor must be rejected")


def test_results_registry_adapter_counts_event_only_primary_stream(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "event-only")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = MarketDataRequest(
        request_id="funding-only-adapter",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.FUNDING_RATE,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 9, tzinfo=UTC),
    )
    descriptor = (
        IngestionService(
            adapters={"fake": FakeExchangeAdapter()},
            storage=storage,
            registry=registry,
        )
        .execute(request, format_name="csv")
        .dataset
    )
    assert descriptor is not None
    manifest = load_manifest(storage.root, descriptor)

    record = to_results_registry_dataset_record(descriptor, manifest)

    assert record["row_count"] == 2
    assert record["funding_data_hash"] is not None
    assert record["metadata_hash"] is not None


def test_results_registry_adapter_is_deterministic(tmp_path: Path) -> None:
    _, _, descriptor, manifest = _registered_multitype_dataset(tmp_path)

    first = to_results_registry_dataset_record(descriptor, manifest)
    second = to_results_registry_dataset_record(descriptor, manifest)

    assert first == second


def test_doctor_reports_advisory_dns_failure_without_failing_local_health(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "doctor")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "market_data_ingestion.doctor.shutil.disk_usage",
        lambda _path: disk_usage(10_000_000_000, 1_000_000_000, 9_000_000_000),
    )

    def no_dns(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("DNS disabled in test")

    monkeypatch.setattr("market_data_ingestion.doctor.socket.getaddrinfo", no_dns)

    report = doctor(
        storage_root=storage.root,
        registry=registry,
        adapters={"fake": FakeExchangeAdapter()},
    )

    assert report["status"] == "ok"
    assert report["network"] == {"dns_data_binance_vision": False}
    assert report["registry"]["status"] == "ok"  # type: ignore[index]
    assert not list(storage.root.glob(".doctor-write-probe-*"))


def test_doctor_fails_when_storage_probe_cannot_be_created(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "doctor-unwritable")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "market_data_ingestion.doctor.shutil.disk_usage",
        lambda _path: disk_usage(10_000_000_000, 1_000_000_000, 9_000_000_000),
    )
    real_open = os.open

    def deny_probe(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        if ".doctor-write-probe-" in os.fspath(path):
            raise PermissionError("denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr("market_data_ingestion.doctor.os.open", deny_probe)

    report = doctor(
        storage_root=storage.root,
        registry=registry,
        adapters={"fake": FakeExchangeAdapter()},
    )

    assert report["status"] == "error"
    assert report["storage"]["writable"] is False  # type: ignore[index]
    assert not list(storage.root.glob(".doctor-write-probe-*"))


def test_module_entrypoint_lists_adapters_without_network(tmp_path: Path) -> None:
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-m", "market_data_ingestion", "exchanges"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert {item["exchange"] for item in payload} == {"binance", "fake"}
