from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.canonical import primitive
from market_data_ingestion.contracts import DatasetDescriptor
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.errors import RegistryError
from market_data_ingestion.registry import descriptor_from_dict


def descriptor() -> DatasetDescriptor:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return DatasetDescriptor(
        dataset_id="fake.usdt_m_perpetual.btcusdt.5m",
        version=1,
        dataset_hash="a" * 64,
        manifest_uri="manifests/fake/v1.json",
        storage_uri="file:///tmp/market-data",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument_id="fake.usdt_m_perpetual.btcusdt",
        data_types=(DataType.KLINES,),
        timeframes=("5m",),
        start=start,
        end=start + timedelta(hours=1),
        schema_version=2,
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        lineage=("request:" + "b" * 64,),
    )


def payload() -> dict[str, object]:
    return dict(primitive(descriptor()))


def test_descriptor_round_trip_is_strict_and_lossless() -> None:
    assert descriptor_from_dict(payload()) == descriptor()


@pytest.mark.parametrize("field", ["version", "schema_version", "descriptor_version"])
def test_descriptor_rejects_boolean_integer_fields(field: str) -> None:
    data = payload()
    data[field] = True
    with pytest.raises(RegistryError) as captured:
        descriptor_from_dict(data)
    assert captured.value.code == "DESCRIPTOR_SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_uri", "../escape.json"),
        ("manifest_uri", "/absolute/manifest.json"),
        ("storage_uri", "relative/storage"),
        ("storage_uri", "https://user:secret@example.test/data"),
        ("storage_uri", "https://example.test/data?token=secret"),
    ],
)
def test_descriptor_rejects_unsafe_uris(field: str, value: str) -> None:
    data = payload()
    data[field] = value
    with pytest.raises(RegistryError) as captured:
        descriptor_from_dict(data)
    assert captured.value.code == "DESCRIPTOR_SCHEMA_INVALID"


def test_descriptor_rejects_unknown_and_missing_fields() -> None:
    unknown = payload()
    unknown["injected"] = "payload"
    with pytest.raises(RegistryError, match="unknown"):
        descriptor_from_dict(unknown)

    missing = payload()
    missing.pop("lineage")
    with pytest.raises(RegistryError, match="missing"):
        descriptor_from_dict(missing)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_types", "klines"),
        ("timeframes", {"5m": True}),
        ("unresolved_issues", ["gap", 7]),
        ("lineage", ["ok", None]),
        ("compatible_consumers", ["modular_backtest", 1]),
    ],
)
def test_descriptor_rejects_wrong_collection_types(field: str, value: object) -> None:
    data = payload()
    data[field] = value
    with pytest.raises(RegistryError) as captured:
        descriptor_from_dict(data)
    assert captured.value.code == "DESCRIPTOR_SCHEMA_INVALID"


def test_descriptor_contract_rejects_duplicates_and_interval_without_timeframe() -> None:
    base = descriptor()
    with pytest.raises(ValueError, match="data_types must be unique"):
        replace(base, data_types=(DataType.KLINES, DataType.KLINES))
    with pytest.raises(ValueError, match="timeframes must be unique"):
        replace(base, timeframes=("5m", "5M"))
    with pytest.raises(ValueError, match="require at least one timeframe"):
        replace(base, timeframes=())
    with pytest.raises(ValueError, match="unresolved_issues must be unique"):
        replace(base, unresolved_issues=("gap", "gap"))
    with pytest.raises(ValueError, match="compatible_consumers must be unique"):
        replace(
            base,
            compatible_consumers=("modular_backtest", "MODULAR_BACKTEST"),
        )


def test_event_only_descriptor_allows_no_timeframe() -> None:
    result = replace(
        descriptor(),
        data_types=(DataType.FUNDING_RATE,),
        timeframes=(),
    )
    assert result.timeframes == ()
