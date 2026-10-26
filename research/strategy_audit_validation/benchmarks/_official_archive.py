"""Checksum-verified Binance Public Data archive fallback for USD-M klines."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

ARCHIVE_BASE_URL = "https://data.binance.vision/data/futures/um"
_SYMBOL = "BTCUSDT"
_INTERVAL = "5m"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    period_kind: str
    period: str
    filename: str
    url: str
    checksum_url: str


@dataclass(frozen=True, slots=True)
class ArchiveObjectEvidence:
    filename: str
    checksum_file_sha256: str
    zip_sha256: str
    accepted_rows: int


@dataclass(frozen=True, slots=True)
class ArchiveDownloadEvidence:
    base_url: str
    object_count: int
    request_count: int
    compressed_bytes: int
    objects: tuple[ArchiveObjectEvidence, ...]
    manifest_sha256: str


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def archive_objects(start_ms: int, end_ms: int) -> tuple[ArchiveObject, ...]:
    if start_ms > end_ms:
        raise ValueError("archive boundaries must be ordered")
    start = datetime.fromtimestamp(start_ms / 1_000, UTC).date()
    end = datetime.fromtimestamp(end_ms / 1_000, UTC).date()
    end_month = end.replace(day=1)
    current = start.replace(day=1)
    objects: list[ArchiveObject] = []
    while current < end_month:
        period = current.strftime("%Y-%m")
        filename = f"{_SYMBOL}-{_INTERVAL}-{period}.zip"
        url = f"{ARCHIVE_BASE_URL}/monthly/klines/{_SYMBOL}/{_INTERVAL}/{filename}"
        objects.append(
            ArchiveObject(
                period_kind="monthly",
                period=period,
                filename=filename,
                url=url,
                checksum_url=f"{url}.CHECKSUM",
            )
        )
        current = _next_month(current)
    current_day = end_month
    while current_day <= end:
        period = current_day.isoformat()
        filename = f"{_SYMBOL}-{_INTERVAL}-{period}.zip"
        url = f"{ARCHIVE_BASE_URL}/daily/klines/{_SYMBOL}/{_INTERVAL}/{filename}"
        objects.append(
            ArchiveObject(
                period_kind="daily",
                period=period,
                filename=filename,
                url=url,
                checksum_url=f"{url}.CHECKSUM",
            )
        )
        current_day += timedelta(days=1)
    if not objects:
        raise ValueError("archive plan is empty")
    return tuple(objects)


def _fetch_bytes(url: str, *, attempts: int = 4) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream,text/plain,*/*",
            "User-Agent": "module-strategy-canonical-v2-archive/1.0",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status != 200:
                    raise RuntimeError(f"archive returned HTTP {response.status}: {url}")
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt == attempts:
                body = exc.read(300).decode("utf-8", errors="replace")
                raise RuntimeError(f"archive HTTP {exc.code} for {url}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt == attempts:
                raise RuntimeError(f"archive unavailable for {url}: {exc.reason}") from exc
        time.sleep(attempt * 2)
    raise AssertionError("unreachable archive retry state")


def parse_checksum(payload: bytes, *, expected_filename: str) -> str:
    try:
        rendered = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("archive checksum file is not UTF-8") from exc
    parts = rendered.split()
    if not parts or _SHA256.fullmatch(parts[0]) is None:
        raise RuntimeError("archive checksum file does not contain a lowercase SHA-256")
    if len(parts) > 1 and parts[-1].lstrip("*") != expected_filename:
        raise RuntimeError("archive checksum filename mismatch")
    return parts[0]


def _csv_rows(payload: bytes, *, expected_filename: str) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("archive payload is not a valid ZIP") from exc
    with archive:
        files = [name for name in archive.namelist() if not name.endswith("/")]
        expected_csv = expected_filename.removesuffix(".zip") + ".csv"
        if files != [expected_csv]:
            raise RuntimeError(f"archive member inventory mismatch: {files}")
        with archive.open(expected_csv) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
            rows: list[list[str]] = []
            for row in reader:
                if not row:
                    continue
                first = row[0].strip().lower().replace(" ", "_")
                if first == "open_time":
                    continue
                rows.append(row)
            return rows


def _manifest_sha256(objects: tuple[ArchiveObjectEvidence, ...]) -> str:
    payload = [
        {
            "accepted_rows": item.accepted_rows,
            "checksum_file_sha256": item.checksum_file_sha256,
            "filename": item.filename,
            "zip_sha256": item.zip_sha256,
        }
        for item in objects
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def download_archive_rows[T](
    *,
    start_ms: int,
    end_ms: int,
    parse_row: Callable[[object], T],
    open_time: Callable[[T], int],
    fetch_bytes: Callable[[str], bytes] = _fetch_bytes,
) -> tuple[list[T], ArchiveDownloadEvidence]:
    rows: list[T] = []
    object_evidence: list[ArchiveObjectEvidence] = []
    compressed_bytes = 0
    requests = 0
    for item in archive_objects(start_ms, end_ms):
        checksum_payload = fetch_bytes(item.checksum_url)
        zip_payload = fetch_bytes(item.url)
        requests += 2
        expected_sha = parse_checksum(checksum_payload, expected_filename=item.filename)
        actual_sha = hashlib.sha256(zip_payload).hexdigest()
        if actual_sha != expected_sha:
            raise RuntimeError(f"archive ZIP checksum mismatch: {item.filename}")
        accepted = 0
        for raw in _csv_rows(zip_payload, expected_filename=item.filename):
            parsed = parse_row(raw)
            timestamp = open_time(parsed)
            if start_ms <= timestamp <= end_ms:
                expected = start_ms + len(rows) * 300_000
                if timestamp != expected:
                    raise RuntimeError("archive data contains a gap, duplicate, or ordering drift")
                rows.append(parsed)
                accepted += 1
        compressed_bytes += len(zip_payload)
        object_evidence.append(
            ArchiveObjectEvidence(
                filename=item.filename,
                checksum_file_sha256=hashlib.sha256(checksum_payload).hexdigest(),
                zip_sha256=actual_sha,
                accepted_rows=accepted,
            )
        )
    if not rows or open_time(rows[-1]) != end_ms:
        raise RuntimeError("archive did not cover the requested final open timestamp")
    evidence_objects = tuple(object_evidence)
    return rows, ArchiveDownloadEvidence(
        base_url=ARCHIVE_BASE_URL,
        object_count=len(evidence_objects),
        request_count=requests,
        compressed_bytes=compressed_bytes,
        objects=evidence_objects,
        manifest_sha256=_manifest_sha256(evidence_objects),
    )


__all__ = [
    "ARCHIVE_BASE_URL",
    "ArchiveDownloadEvidence",
    "ArchiveObject",
    "ArchiveObjectEvidence",
    "archive_objects",
    "download_archive_rows",
    "parse_checksum",
]
