from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_ingestion.canonical import primitive, sha256_canonical
from market_data_ingestion.enums import DataType, ValidationStatus
from market_data_ingestion.spool import RecordSpool
from market_data_ingestion.validation import validate_sorted_records


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
def test_spool_rejects_invalid_batch_size(tmp_path: Path, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RecordSpool(tmp_path / "records.sqlite3", batch_size=value)  # type: ignore[arg-type]


def test_spool_exposes_configured_batch_size(tmp_path: Path) -> None:
    spool = RecordSpool(tmp_path / "records.sqlite3", batch_size=17)
    try:
        assert spool.batch_size == 17
    finally:
        spool.close(remove=True)


def test_validated_stage_reuses_acquired_payload_rows(
    tmp_path: Path,
    five_minute_records,
) -> None:
    path = tmp_path / "records.sqlite3"
    spool = RecordSpool(path, reset=True, batch_size=2)
    try:
        assert spool.append("acquired", five_minute_records) == len(five_minute_records)
        acquired = tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))
        assert acquired == five_minute_records

        assert spool.append("validated", acquired) == len(acquired)
        assert spool.count("validated", DataType.KLINES, "5m") == len(acquired)

        physical_rows = spool._connection.execute("SELECT COUNT(*) FROM records").fetchone()
        validation_links = spool._connection.execute(
            "SELECT COUNT(*) FROM validated_records"
        ).fetchone()
        assert physical_rows == (len(acquired),)
        assert validation_links == (len(acquired),)
        assert tuple(spool.iter_sorted("validated", DataType.KLINES, "5m")) == acquired
    finally:
        spool.close(remove=True)


def test_spool_identity_remains_canonical_sha256(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "identity.sqlite3", reset=True)
    try:
        record = five_minute_records[0]
        spool.append("acquired", (record,))
        stored_identity, payload = spool._connection.execute(
            "SELECT identity, payload FROM records"
        ).fetchone()

        row = primitive(record.to_row())
        assert stored_identity == sha256_canonical(row)
        assert stored_identity == hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert payload == json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    finally:
        spool.close(remove=True)


def test_validation_duplicate_across_append_boundary_promotes_only_first(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "duplicate.sqlite3", reset=True, batch_size=2)
    try:
        spool.append("acquired", five_minute_records[:2])
        spool.append(
            "acquired",
            (five_minute_records[1], five_minute_records[2]),
        )
        accepted = []

        def accept(record) -> None:
            accepted.append(record)
            if len(accepted) >= 2:
                spool.append("validated", accepted)
                accepted.clear()

        report = validate_sorted_records(
            spool.iter_sorted("acquired", DataType.KLINES, "5m"),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            accept=accept,
            now=datetime(2027, 1, 1, tzinfo=UTC),
            expected_start=five_minute_records[0].timestamp,
            expected_end=five_minute_records[2].timestamp + timedelta(minutes=5),
        )
        if accepted:
            spool.append("validated", accepted)

        assert report.status is ValidationStatus.VALID_WITH_WARNINGS
        assert len(report.duplicates) == 1
        assert report.duplicates[0].exact is True
        assert spool.count("acquired", DataType.KLINES, "5m") == 4
        assert spool.count("validated", DataType.KLINES, "5m") == 3
        assert tuple(spool.iter_sorted("validated", DataType.KLINES, "5m")) == (
            five_minute_records[0],
            five_minute_records[1],
            five_minute_records[2],
        )
        assert spool._connection.execute(
            "SELECT COUNT(*) FROM records WHERE stage = 'validated'"
        ).fetchone() == (0,)
    finally:
        spool.close(remove=True)


def test_validation_gap_across_append_boundary_is_preserved(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "gap.sqlite3", reset=True)
    try:
        spool.append("acquired", (five_minute_records[0],))
        spool.append("acquired", (five_minute_records[2],))
        report = validate_sorted_records(
            spool.iter_sorted("acquired", DataType.KLINES, "5m"),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            now=datetime(2027, 1, 1, tzinfo=UTC),
            expected_start=five_minute_records[0].timestamp,
            expected_end=five_minute_records[2].timestamp + timedelta(minutes=5),
        )

        assert report.status is ValidationStatus.INVALID
        assert len(report.gaps) == 1
        assert report.gaps[0].missing_intervals == 1
    finally:
        spool.close(remove=True)


def test_direct_validated_append_remains_backward_compatible(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "direct.sqlite3", reset=True)
    try:
        spool.append("validated", five_minute_records)
        assert spool._connection.execute(
            "SELECT COUNT(*) FROM records WHERE stage = 'validated'"
        ).fetchone() == (len(five_minute_records),)
        assert spool._connection.execute("SELECT COUNT(*) FROM validated_records").fetchone() == (
            0,
        )
        assert tuple(spool.iter_sorted("validated", DataType.KLINES, "5m")) == five_minute_records
    finally:
        spool.close(remove=True)


def test_clear_validated_stream_removes_links_but_keeps_acquired(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "clear-validated.sqlite3", reset=True)
    try:
        spool.append("acquired", five_minute_records)
        acquired = tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))
        spool.append("validated", acquired)

        spool.clear_stream("validated", DataType.KLINES, "5m")

        assert spool.count("validated", DataType.KLINES, "5m") == 0
        assert spool.count("acquired", DataType.KLINES, "5m") == len(acquired)
        assert spool._connection.execute("SELECT COUNT(*) FROM validated_records").fetchone() == (
            0,
        )
    finally:
        spool.close(remove=True)


def test_clear_acquired_stream_removes_validation_links(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "clear-acquired.sqlite3", reset=True)
    try:
        spool.append("acquired", five_minute_records)
        acquired = tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))
        spool.append("validated", acquired)

        spool.clear_stream("acquired", DataType.KLINES, "5m")

        assert spool.count("acquired", DataType.KLINES, "5m") == 0
        assert spool.count("validated", DataType.KLINES, "5m") == 0
        assert spool._connection.execute("SELECT COUNT(*) FROM validated_records").fetchone() == (
            0,
        )
    finally:
        spool.close(remove=True)


def test_spool_rejects_noncanonical_payload_even_with_matching_json(
    tmp_path: Path,
    five_minute_records,
) -> None:
    spool = RecordSpool(tmp_path / "noncanonical.sqlite3", reset=True)
    try:
        spool.append("acquired", (five_minute_records[0],))
        payload = spool._connection.execute("SELECT payload FROM records").fetchone()[0]
        parsed = json.loads(payload)
        noncanonical = json.dumps(parsed, sort_keys=False, indent=1)
        spool._connection.execute(
            "UPDATE records SET payload = ?, identity = ?",
            (
                noncanonical,
                hashlib.sha256(noncanonical.encode("utf-8")).hexdigest(),
            ),
        )
        spool._connection.commit()

        with pytest.raises(Exception) as captured:
            tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))

        assert getattr(captured.value, "code", None) == "SPOOL_METADATA_CORRUPT"
    finally:
        spool.close(remove=True)
