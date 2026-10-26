from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_ingestion.drive_corpus import _atomic_json, load_drive_index
from market_data_ingestion.enums import MarketType
from market_data_ingestion.external_corpus import (
    ExternalCorpusFile,
    ExternalCorpusManifest,
    load_external_corpus_spec,
    write_external_corpus_manifest,
)
from market_data_ingestion.external_registration import (
    ExternalRegistrationResult,
    write_external_registration_evidence,
)


def test_external_corpus_file_rejects_symlink_source(tmp_path: Path) -> None:
    target = tmp_path / "target.parquet"
    target.write_bytes(b"parquet-placeholder")
    link = tmp_path / "linked.parquet"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        ExternalCorpusFile(
            symbol="BTCUSDT",
            timeframe="5m",
            path=link,
            source_uri="gdrive://symlink-source",
            expected_rows=1,
            expected_first_open_time=datetime(2024, 1, 1, tzinfo=UTC),
            expected_last_open_time=datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_external_corpus_spec_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "spec-target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "spec.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        load_external_corpus_spec(link)


def test_manifest_writer_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "manifest-target.json"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    manifest = ExternalCorpusManifest(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(),
        corpus_hash="a" * 64,
        verified_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="symlink"):
        write_external_corpus_manifest(link, manifest)

    assert target.read_text(encoding="utf-8") == "sentinel"


def test_drive_index_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "index-target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "index.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        load_drive_index(link)


def test_atomic_json_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "output-target.json"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "output.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        _atomic_json(link, {"status": "PASS"}, mode=0o600)

    assert target.read_text(encoding="utf-8") == "sentinel"


def test_registration_writer_rejects_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "registration-target.json"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "registration.json"
    link.symlink_to(target)
    result = ExternalRegistrationResult(
        corpus_hash="b" * 64,
        datasets=(),
        total_rows=0,
        registered_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="symlink"):
        write_external_registration_evidence(link, result)

    assert target.read_text(encoding="utf-8") == "sentinel"
