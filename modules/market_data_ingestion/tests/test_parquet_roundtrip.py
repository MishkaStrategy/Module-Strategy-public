from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from market_data_ingestion.enums import DataType
from market_data_ingestion.storage import LocalStorage


def test_parquet_partition_round_trip_is_typed(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    storage = LocalStorage(tmp_path / "data")
    descriptor = storage.write_partition(
        dataset_id="parquet-round-trip",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.KLINES,
        timeframe="5m",
        records=iter(five_minute_records),
        source_artifact_ids=("raw:test",),
        format_name="parquet",
        compression="zstd",
    )

    restored = tuple(storage.iter_partition_records(descriptor))

    assert descriptor.format == "parquet"
    assert descriptor.row_count == len(five_minute_records)
    assert restored == five_minute_records
