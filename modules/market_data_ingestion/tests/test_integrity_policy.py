from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.cli import main
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def make_request(request_id: str) -> MarketDataRequest:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return MarketDataRequest(
        request_id=request_id,
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=start,
        end=start + timedelta(hours=1),
    )


def _ingest(tmp_path: Path, request_id: str = "integrity-request"):
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    ).execute(make_request(request_id), format_name="csv")
    assert result.dataset is not None
    descriptor = registry.inspect(result.dataset.dataset_id, result.dataset.version)
    assert descriptor is not None
    return storage, registry, result.dataset, descriptor


def test_manifest_lineage_tamper_is_detected(tmp_path) -> None:
    storage, _, dataset, descriptor = _ingest(tmp_path, "manifest-tamper")
    manifest_path = storage.root / dataset.manifest_uri
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["normalization_version"] = "999.0.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = verify_registered_dataset(storage.root, descriptor)

    assert report["status"] == "error"
    assert any("dataset_hash mismatch" in issue for issue in report["issues"])


def test_deep_validation_detects_raw_payload_tamper(tmp_path) -> None:
    storage, _, dataset, descriptor = _ingest(tmp_path, "raw-tamper")
    manifest = json.loads((storage.root / dataset.manifest_uri).read_text(encoding="utf-8"))
    artifact_id = manifest["source_artifacts"][0]["artifact_id"]
    matching_metadata = []
    for metadata_path in storage.root.glob("raw/**/*.meta.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata["artifact_id"] == artifact_id:
            matching_metadata.append(metadata_path)
    assert len(matching_metadata) == 1
    payload_path = matching_metadata[0].with_name(matching_metadata[0].name[: -len(".meta.json")])
    payload_path.write_bytes(payload_path.read_bytes() + b"tamper")

    shallow = verify_registered_dataset(storage.root, descriptor)
    deep = verify_registered_dataset(storage.root, descriptor, deep=True)

    assert shallow["status"] == "ok"
    assert deep["status"] == "error"
    assert any("raw artifact verification failed" in issue for issue in deep["issues"])


def test_cli_deep_validate_succeeds_for_registered_csv_dataset(tmp_path, capsys) -> None:
    storage, _, dataset, _ = _ingest(tmp_path, "cli-deep")

    exit_code = main(
        [
            "validate",
            "--storage-root",
            str(storage.root),
            "--dataset-id",
            dataset.dataset_id,
            "--version",
            str(dataset.version),
            "--deep",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "ok"
    assert output["deep"] is True
    assert output["raw_artifacts_checked"] > 0
    assert output["rows_checked"] > 0


def test_dataset_hash_tracks_source_lineage_even_when_partition_bytes_do_not(tmp_path) -> None:
    storage, _, dataset, _ = _ingest(tmp_path, "source-lineage")
    from market_data_ingestion.hashing import dataset_hash
    from market_data_ingestion.manifest_io import load_manifest

    manifest = load_manifest(storage.root, dataset)
    source = manifest.source_artifacts[0]
    changed_source = replace(source, source_uri=source.source_uri + "-alternate")

    assert dataset_hash(manifest) != dataset_hash(
        replace(
            manifest,
            source_artifacts=(changed_source, *manifest.source_artifacts[1:]),
        )
    )
