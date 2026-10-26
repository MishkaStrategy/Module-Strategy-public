from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.external_corpus import (
    ExternalCorpusFile,
    ExternalCorpusSpec,
    ExternalCorpusVerifier,
)
from market_data_ingestion.external_registration import (
    ExternalCorpusRegistrar,
    write_external_registration_evidence,
)
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.registry import SQLiteDatasetRegistry, descriptor_from_dict
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.timeframes import timeframe_delta


def _write_klines(
    path: Path,
    *,
    timeframe: str,
    open_times: list[datetime],
    price_offset: float = 0.0,
) -> None:
    step = timeframe_delta(timeframe)
    count = len(open_times)
    columns = {
        "open_time": open_times,
        "open": [100.0 + price_offset + index for index in range(count)],
        "high": [101.0 + price_offset + index for index in range(count)],
        "low": [99.0 + price_offset + index for index in range(count)],
        "close": [100.5 + price_offset + index for index in range(count)],
        "volume": [10.0 + index for index in range(count)],
        "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
        "quote_volume": [1000.0 + index for index in range(count)],
        "trades": [10 + index for index in range(count)],
        "taker_buy_base": [5.0 + index for index in range(count)],
        "taker_buy_quote": [500.0 + index for index in range(count)],
    }
    pq.write_table(pa.table(columns), path)


def _source(path: Path, *, timeframe: str, open_times: list[datetime]) -> ExternalCorpusFile:
    return ExternalCorpusFile(
        symbol="BTCUSDT",
        timeframe=timeframe,
        path=path,
        source_uri=f"gdrive://test-{timeframe}/{path.name}",
        expected_rows=len(open_times),
        expected_first_open_time=open_times[0],
        expected_last_open_time=open_times[-1],
        expected_sha256=sha256_file(path),
    )


def _spec(tmp_path: Path) -> ExternalCorpusSpec:
    five_path = tmp_path / "BTCUSDT_5m.parquet"
    ten_path = tmp_path / "BTCUSDT_10m.parquet"
    five_times = [
        datetime(2024, 1, 31, 23, 55, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
    ]
    ten_times = [
        datetime(2024, 1, 31, 23, 50, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
    ]
    _write_klines(five_path, timeframe="5m", open_times=five_times)
    _write_klines(ten_path, timeframe="10m", open_times=ten_times)
    return ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m", "10m"),
        files=(
            _source(five_path, timeframe="5m", open_times=five_times),
            _source(ten_path, timeframe="10m", open_times=ten_times),
        ),
    )


def test_external_registration_uses_existing_storage_and_registry_contracts(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    registrar = ExternalCorpusRegistrar(storage=storage, registry=registry)

    result = registrar.register(spec)

    assert result.total_rows == 5
    assert len(result.datasets) == 1
    registered = result.datasets[0]
    assert registered.reused is False
    assert registered.raw_artifact_count == 2
    assert registered.partition_count == 4
    assert registered.descriptor.dataset_id == ("binance_usdt-m-perpetual_btcusdt_5m")
    assert registered.descriptor.version == 1
    assert registered.descriptor.timeframes == ("5m", "10m")

    payload = registry.inspect(registered.descriptor.dataset_id)
    assert payload is not None
    assert verify_registered_dataset(storage.root, payload, deep=True)["status"] == "ok"
    descriptor = descriptor_from_dict(payload)
    manifest = load_manifest(storage.root, descriptor)
    assert len(manifest.source_artifacts) == 2
    assert len(manifest.partitions) == 4
    assert sum(item.row_count for item in manifest.partitions) == 5
    assert {item.timeframe for item in manifest.partitions} == {"5m", "10m"}
    assert all(item.storage_uri.startswith("normalized/") for item in manifest.partitions)
    assert f"external-corpus:{result.corpus_hash}" in manifest.lineage

    evidence_path = write_external_registration_evidence(
        tmp_path / "evidence" / "registration.json",
        result,
    )
    evidence_text = evidence_path.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["status"] == "PASS"
    assert evidence["dataset_count"] == 1
    assert evidence["raw_market_data_uploaded"] is False
    assert str(storage.root) not in evidence_text
    assert "manifest_uri" not in evidence_text
    assert "storage_uri" not in evidence_text

    second = registrar.register(spec)
    assert second.corpus_hash == result.corpus_hash
    assert second.datasets[0].reused is True
    assert second.datasets[0].descriptor.version == 1
    assert registry.health()["datasets"] == 1


def test_external_registration_rejects_tampered_manifest_hash(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    verified = ExternalCorpusVerifier().verify(spec)
    tampered = replace(verified, corpus_hash="0" * 64)
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")

    with pytest.raises(StructuredIngestionError, match="hash"):
        ExternalCorpusRegistrar(storage=storage, registry=registry).register_verified(
            spec,
            tampered,
        )

    assert registry.health()["datasets"] == 0


def test_external_registration_rejects_source_change_after_verification(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    verified = ExternalCorpusVerifier().verify(spec)
    source = spec.files[0]
    open_times = [
        source.expected_first_open_time,
        source.expected_first_open_time + timeframe_delta(source.timeframe),
        source.expected_last_open_time,
    ]
    _write_klines(
        source.path,
        timeframe=source.timeframe,
        open_times=open_times,
        price_offset=1000.0,
    )
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")

    with pytest.raises(StructuredIngestionError, match="changed after verification"):
        ExternalCorpusRegistrar(storage=storage, registry=registry).register_verified(
            spec,
            verified,
        )

    assert registry.health()["datasets"] == 0
