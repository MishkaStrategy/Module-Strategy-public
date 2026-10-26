from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from market_data_ingestion.drive_corpus import (
    build_external_corpus_spec,
    load_drive_index,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_INDEX = (
    REPOSITORY_ROOT
    / "modules/market_data_ingestion/configs/market_data/binance_usdtm_baseline_drive_index.json"
)
SUMMARY_COLUMNS = (
    "symbol",
    "timeframe",
    "status",
    "rows",
    "first_open_time",
    "last_open_time",
    "gap_events",
    "estimated_missing_5m_rows",
)


def _write_fixture(root: Path) -> Path:
    entries = [
        (
            "BTCUSDT",
            "5m",
            "drive-file-5m",
            3,
            2,
            "2024-01-01 00:00:00+00:00",
            "2024-01-01 00:05:00+00:00",
        ),
        (
            "BTCUSDT",
            "1h",
            "drive-file-1h",
            4,
            2,
            "2024-01-01 00:00:00+00:00",
            "2024-01-01 01:00:00+00:00",
        ),
    ]
    symbol_root = root / "BTCUSDT"
    symbol_root.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    for symbol, timeframe, file_id, byte_size, count, first, last in entries:
        filename = f"{symbol}_{timeframe}.parquet"
        (symbol_root / filename).write_bytes(b"x" * byte_size)
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "ok",
                "rows": count,
                "first_open_time": first,
                "last_open_time": last,
                "gap_events": "0" if timeframe == "5m" else "",
                "estimated_missing_5m_rows": "0" if timeframe == "5m" else "",
            }
        )
        files.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "drive_file_id": file_id,
                "filename": filename,
                "byte_size": byte_size,
            }
        )

    summary = root / "download_summary.csv"
    with summary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary_sha = hashlib.sha256(summary.read_bytes()).hexdigest()
    index = {
        "schema_version": 1,
        "folder_id": "folder-id-0001",
        "summary_file_id": "summary-id-001",
        "summary_sha256": summary_sha,
        "exchange_id": "binance",
        "market_type": "usdt_m_perpetual",
        "required_timeframes": ["5m", "1h"],
        "files": files,
    }
    index_path = root.parent / "drive-index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return index_path


def test_builds_local_spec_and_sanitized_report(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    index = _write_fixture(root)
    spec_path = tmp_path / "private" / "spec.json"
    report_path = tmp_path / "evidence" / "inventory.json"

    build_external_corpus_spec(
        corpus_root=root,
        drive_index_path=index,
        output_path=spec_path,
        report_path=report_path,
    )

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(spec["files"]) == 2
    assert spec["files"][0]["source_uri"].startswith("gdrive://")
    assert Path(spec["files"][0]["path"]).is_absolute()
    assert report == {
        "schema_version": 1,
        "folder_id": "folder-id-0001",
        "summary_file_id": "summary-id-001",
        "summary_sha256": hashlib.sha256((root / "download_summary.csv").read_bytes()).hexdigest(),
        "drive_index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "file_count": 2,
        "symbol_count": 1,
        "timeframe_count": 2,
        "total_bytes": 7,
        "total_rows": 4,
        "source_uri_scheme": "gdrive",
        "contains_local_paths": False,
    }
    assert str(root) not in report_path.read_text(encoding="utf-8")


def test_rejects_changed_summary(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    index = _write_fixture(root)
    with (root / "download_summary.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ValueError, match="SHA-256"):
        build_external_corpus_spec(
            corpus_root=root,
            drive_index_path=index,
            output_path=tmp_path / "spec.json",
            report_path=tmp_path / "report.json",
        )


def test_rejects_changed_file_size(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    index = _write_fixture(root)
    (root / "BTCUSDT" / "BTCUSDT_5m.parquet").write_bytes(b"changed")

    with pytest.raises(ValueError, match="size mismatch"):
        build_external_corpus_spec(
            corpus_root=root,
            drive_index_path=index,
            output_path=tmp_path / "spec.json",
            report_path=tmp_path / "report.json",
        )


def test_rejects_symlinked_corpus_file(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    index = _write_fixture(root)
    target = root / "BTCUSDT" / "target.parquet"
    target.write_bytes(b"xxx")
    file_path = root / "BTCUSDT" / "BTCUSDT_5m.parquet"
    file_path.unlink()
    file_path.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        build_external_corpus_spec(
            corpus_root=root,
            drive_index_path=index,
            output_path=tmp_path / "spec.json",
            report_path=tmp_path / "report.json",
        )


def test_committed_drive_index_is_exact_baseline() -> None:
    index = load_drive_index(COMMITTED_INDEX)
    assert index.folder_id == "1mm_1z1alqfTUF_8stXoaccCQergs6Sk_"
    assert index.summary_sha256 == (
        "a5f763fed5912cf0a988f50ac163d1b47f2506d46412aec120cd56a4eaf43893"
    )
    assert index.required_timeframes == ("5m", "10m", "15m", "1h", "4h")
    assert len(index.files) == 50
    assert len({item.symbol for item in index.files}) == 10
    assert sum(item.byte_size for item in index.files) == 615_395_637
    assert all(item.symbol != "XAUUSDT" for item in index.files)
