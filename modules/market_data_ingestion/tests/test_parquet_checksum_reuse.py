from __future__ import annotations

import hashlib
from pathlib import Path

import market_data_ingestion.storage as storage_module
from market_data_ingestion.storage import LocalStorage


def test_parquet_publication_reuses_generated_payload_checksum(
    tmp_path: Path,
    instrument,
    five_minute_records,
    monkeypatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    payload = b"PAR1-deterministic-test-payload"
    monkeypatch.setattr(
        LocalStorage,
        "_parquet_bytes",
        staticmethod(lambda rows, compression: payload),
    )
    checksum_reads: list[Path] = []
    real_sha256_file = storage_module.sha256_file

    def tracked_sha256_file(path: Path) -> str:
        checksum_reads.append(path)
        return real_sha256_file(path)

    monkeypatch.setattr(storage_module, "sha256_file", tracked_sha256_file)

    descriptor = storage.write_kline_partition(
        dataset_id="dataset-checksum-reuse",
        version=1,
        layer="normalized",
        instrument=instrument,
        timeframe="5m",
        records=five_minute_records,
        source_artifact_ids=("raw:test",),
        format_name="parquet",
        compression="zstd",
    )

    published = storage.root / descriptor.storage_uri
    expected_checksum = hashlib.sha256(payload).hexdigest()
    assert published.read_bytes() == payload
    assert descriptor.checksum_sha256 == expected_checksum
    assert descriptor.partition_id == f"part:{expected_checksum[:24]}"
    assert checksum_reads == []

    repeated = storage.write_kline_partition(
        dataset_id="dataset-checksum-reuse",
        version=1,
        layer="normalized",
        instrument=instrument,
        timeframe="5m",
        records=five_minute_records,
        source_artifact_ids=("raw:test",),
        format_name="parquet",
        compression="zstd",
    )

    assert repeated == descriptor
    assert checksum_reads == [published]
