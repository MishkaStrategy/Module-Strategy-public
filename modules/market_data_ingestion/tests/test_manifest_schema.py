from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import DatasetDescriptor, MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.errors import StorageError
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage

ManifestPayload = dict[str, Any]
Mutation = Callable[[ManifestPayload], None]


def _request() -> MarketDataRequest:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return MarketDataRequest(
        request_id="manifest-schema",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=start,
        end=start + timedelta(hours=1),
    )


def _registered_manifest(
    tmp_path: Path,
) -> tuple[LocalStorage, DatasetDescriptor, Path, ManifestPayload]:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    ).execute(_request(), format_name="csv")
    if result.dataset is None:
        raise AssertionError("fixture ingestion did not register a dataset")
    path = storage.root / result.dataset.manifest_uri
    payload = json.loads(path.read_text(encoding="utf-8"))
    return storage, result.dataset, path, payload


def _unknown_top(payload: ManifestPayload) -> None:
    payload["unexpected"] = "payload"


def _unknown_partition(payload: ManifestPayload) -> None:
    payload["partitions"][0]["unexpected"] = "payload"


def _missing_identity(payload: ManifestPayload) -> None:
    del payload["dataset_id"]


def _fractional_version(payload: ManifestPayload) -> None:
    payload["version"] = 1.5


def _numeric_data_type(payload: ManifestPayload) -> None:
    payload["data_types"][0] = 7


def _string_boolean(payload: ManifestPayload) -> None:
    payload["validation"]["duplicates"] = [
        {
            "timestamp": payload["start"],
            "exact": "false",
            "source_partition_ids": [],
            "evidence_hashes": [],
        }
    ]


def _boolean_row_count(payload: ManifestPayload) -> None:
    payload["partitions"][0]["row_count"] = True


def _secret_source_uri(payload: ManifestPayload) -> None:
    payload["source_artifacts"][0]["source_uri"] += "?api_key=secret"


def _reversed_artifact_range(payload: ManifestPayload) -> None:
    artifact = payload["source_artifacts"][0]
    artifact["start"], artifact["end"] = artifact["end"], artifact["start"]


def _missing_validation_policy(payload: ManifestPayload) -> None:
    del payload["validation_policy_version"]


def _legacy_manifest_version(payload: ManifestPayload) -> None:
    payload["manifest_version"] = 1


def _unknown_validation_field(payload: ManifestPayload) -> None:
    payload["validation"]["debug_payload"] = {"secret": "value"}


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("unknown-top", _unknown_top),
        ("unknown-partition", _unknown_partition),
        ("missing-identity", _missing_identity),
        ("fractional-version", _fractional_version),
        ("numeric-data-type", _numeric_data_type),
        ("string-boolean", _string_boolean),
        ("boolean-row-count", _boolean_row_count),
        ("secret-source-uri", _secret_source_uri),
        ("reversed-artifact-range", _reversed_artifact_range),
        ("unknown-validation-field", _unknown_validation_field),
        ("missing-validation-policy", _missing_validation_policy),
        ("legacy-manifest-version", _legacy_manifest_version),
    ],
)
def test_registered_manifest_rejects_schema_mutation(
    tmp_path: Path,
    name: str,
    mutation: Mutation,
) -> None:
    storage, descriptor, path, payload = _registered_manifest(tmp_path / name)
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageError) as captured:
        load_manifest(storage.root, descriptor)

    assert captured.value.code == "MANIFEST_SCHEMA_INVALID"


def test_strict_manifest_parser_accepts_current_writer_output(tmp_path: Path) -> None:
    storage, descriptor, _, _ = _registered_manifest(tmp_path)
    manifest = load_manifest(storage.root, descriptor)

    assert manifest.dataset_id == descriptor.dataset_id
    assert manifest.version == descriptor.version
