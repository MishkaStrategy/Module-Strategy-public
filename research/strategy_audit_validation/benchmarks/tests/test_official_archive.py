from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime

import pytest

from benchmarks import _official_archive as subject


def _zip(filename: str, rows: list[list[object]]) -> bytes:
    output = io.BytesIO()
    csv_name = filename.removesuffix(".zip") + ".csv"
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        payload = "\n".join(",".join(str(value) for value in row) for row in rows) + "\n"
        archive.writestr(csv_name, payload)
    return output.getvalue()


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1_000)


def test_archive_plan_uses_monthly_history_and_daily_end_month() -> None:
    objects = subject.archive_objects(
        _ms("2019-09-08T17:55:00"),
        _ms("2026-07-19T06:55:00"),
    )

    assert len(objects) == 101
    assert objects[0].filename == "BTCUSDT-5m-2019-09.zip"
    assert objects[-1].filename == "BTCUSDT-5m-2026-07-19.zip"
    assert objects[0].checksum_url.endswith(".zip.CHECKSUM")
    assert all("/futures/um/" in item.url for item in objects)


def test_checksum_parser_is_fail_closed() -> None:
    filename = "BTCUSDT-5m-2026-07-19.zip"
    digest = "a" * 64

    assert (
        subject.parse_checksum(f"{digest}  {filename}\n".encode(), expected_filename=filename)
        == digest
    )
    assert (
        subject.parse_checksum(f"{digest} *{filename}\n".encode(), expected_filename=filename)
        == digest
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        subject.parse_checksum(b"bad value", expected_filename=filename)
    with pytest.raises(RuntimeError, match="filename mismatch"):
        subject.parse_checksum(f"{digest}  other.zip\n".encode(), expected_filename=filename)


def test_archive_download_verifies_checksum_and_contiguous_rows() -> None:
    objects = subject.archive_objects(0, 600_000)
    assert len(objects) == 1
    item = objects[0]
    payload = _zip(item.filename, [[0], [300_000], [600_000]])
    checksum = hashlib.sha256(payload).hexdigest()
    responses = {
        item.url: payload,
        item.checksum_url: f"{checksum}  {item.filename}\n".encode(),
    }

    rows, evidence = subject.download_archive_rows(
        start_ms=0,
        end_ms=600_000,
        parse_row=lambda row: int(row[0]),
        open_time=lambda value: value,
        fetch_bytes=responses.__getitem__,
    )

    assert rows == [0, 300_000, 600_000]
    assert evidence.object_count == 1
    assert evidence.request_count == 2
    assert evidence.objects[0].accepted_rows == 3
    assert len(evidence.manifest_sha256) == 64


def test_archive_download_rejects_checksum_and_gap_drift() -> None:
    item = subject.archive_objects(0, 600_000)[0]
    payload = _zip(item.filename, [[0], [600_000]])
    responses = {
        item.url: payload,
        item.checksum_url: f"{'b' * 64}  {item.filename}\n".encode(),
    }
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        subject.download_archive_rows(
            start_ms=0,
            end_ms=600_000,
            parse_row=lambda row: int(row[0]),
            open_time=lambda value: value,
            fetch_bytes=responses.__getitem__,
        )

    digest = hashlib.sha256(payload).hexdigest()
    responses[item.checksum_url] = f"{digest}  {item.filename}\n".encode()
    with pytest.raises(RuntimeError, match="gap, duplicate, or ordering"):
        subject.download_archive_rows(
            start_ms=0,
            end_ms=600_000,
            parse_row=lambda row: int(row[0]),
            open_time=lambda value: value,
            fetch_bytes=responses.__getitem__,
        )
