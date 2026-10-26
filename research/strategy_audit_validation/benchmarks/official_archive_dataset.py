"""Materialize BTCUSDT USD-M klines from checksum-verified Binance archives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._official_archive import ArchiveDownloadEvidence, download_archive_rows
from .official_usdtm_dataset import (
    DRIVE_RAW_SHA256,
    EXPECTED_END,
    EXPECTED_ROWS,
    EXPECTED_START,
    Kline,
    _epoch_ms,
    compare_drive_corpus,
    content_sha256,
    parse_kline,
    sha256_file,
    write_parquet,
)


def _archive_payload(evidence: ArchiveDownloadEvidence) -> dict[str, Any]:
    return {
        "base_url": evidence.base_url,
        "compressed_bytes": evidence.compressed_bytes,
        "manifest_sha256": evidence.manifest_sha256,
        "object_count": evidence.object_count,
        "objects": [
            {
                "accepted_rows": item.accepted_rows,
                "checksum_file_sha256": item.checksum_file_sha256,
                "filename": item.filename,
                "zip_sha256": item.zip_sha256,
            }
            for item in evidence.objects
        ],
        "request_count": evidence.request_count,
    }


def build_archive_provenance(
    *,
    rows: list[Kline],
    evidence: ArchiveDownloadEvidence,
    parquet_path: Path,
    drive_path: Path | None,
    generated_from_commit: str,
    drive_mismatch_counts: dict[str, int] | None,
) -> dict[str, Any]:
    drive_compared = drive_path is not None
    return {
        "api_attempt_status": "HTTP_451_RESTRICTED_LOCATION",
        "archive": _archive_payload(evidence),
        "content_sha256": content_sha256(rows),
        "drive_comparison_status": "MATCH" if drive_compared else "NOT_REQUESTED",
        "drive_content_parity": True if drive_compared else None,
        "drive_mismatch_counts": drive_mismatch_counts,
        "drive_observed_raw_sha256": sha256_file(drive_path) if drive_path else None,
        "retained_drive_raw_sha256": DRIVE_RAW_SHA256,
        "endpoint": {
            "base_url": "https://fapi.binance.com",
            "method": "GET",
            "path": "/fapi/v1/klines",
            "security": "NONE",
        },
        "generated_from_commit": generated_from_commit,
        "interval": "5m",
        "last_open_time": EXPECTED_END,
        "parquet_sha256": sha256_file(parquet_path),
        "row_count": len(rows),
        "schema_version": 3,
        "source": "OFFICIAL_BINANCE_PUBLIC_DATA_ARCHIVE",
        "source_contract": (
            "Binance Public Data USD-M kline archives are generated from /fapi/v1/klines"
        ),
        "symbol": "BTCUSDT",
        "first_open_time": EXPECTED_START,
    }


def run(
    *,
    output: Path,
    provenance_path: Path,
    generated_from_commit: str,
    drive_input: Path | None = None,
) -> dict[str, Any]:
    start_ms = _epoch_ms(EXPECTED_START)
    end_ms = _epoch_ms(EXPECTED_END)
    rows, evidence = download_archive_rows(
        start_ms=start_ms,
        end_ms=end_ms,
        parse_row=parse_kline,
        open_time=lambda row: row.open_time_ms,
    )
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"official archive row count mismatch: {len(rows)}")
    drive = drive_input.resolve(strict=True) if drive_input else None
    write_parquet(rows, output)
    mismatch_counts = compare_drive_corpus(output, drive) if drive else None
    provenance = build_archive_provenance(
        rows=rows,
        evidence=evidence,
        parquet_path=output,
        drive_path=drive,
        generated_from_commit=generated_from_commit,
        drive_mismatch_counts=mismatch_counts,
    )
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return provenance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive-input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    arguments = parser.parse_args()
    provenance = run(
        output=arguments.output.resolve(),
        provenance_path=arguments.provenance.resolve(),
        generated_from_commit=arguments.generated_from_commit,
        drive_input=arguments.drive_input,
    )
    print(json.dumps(provenance, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
