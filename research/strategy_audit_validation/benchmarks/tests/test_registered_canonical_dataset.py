from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BENCHMARK_ROOT))
subject = import_module("benchmarks.registered_canonical_dataset")


def _record(open_time: datetime, price: str) -> SimpleNamespace:
    return SimpleNamespace(
        open_time=open_time,
        close_time=open_time + timedelta(minutes=5) - timedelta(milliseconds=1),
        open=price,
        high=str(float(price) + 2.0),
        low=str(float(price) - 1.0),
        close=str(float(price) + 1.0),
        volume="10",
        quote_volume="1000",
        trade_count=5,
        taker_buy_base_volume="4",
        taker_buy_quote_volume="400",
        symbol="BTCUSDT",
        timeframe="5m",
        is_closed=True,
    )


def _selection(*, dataset_hash: str, records: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        descriptor=SimpleNamespace(
            dataset_id=subject.REGISTERED_DATASET_ID,
            version=subject.REGISTERED_DATASET_VERSION,
            dataset_hash=dataset_hash,
            validation_status=SimpleNamespace(value="VALID"),
        ),
        manifest=SimpleNamespace(instrument=SimpleNamespace(symbol="BTCUSDT")),
        timeframe="5m",
        row_count=len(records),
        partitions=(SimpleNamespace(),),
        records=records,
    )


def _configure_small_corpus(monkeypatch: pytest.MonkeyPatch) -> tuple[list[SimpleNamespace], str]:
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    records = [_record(first, "100"), _record(first + timedelta(minutes=5), "101")]
    rows = [subject._record_to_kline(record) for record in records]
    digest = subject.content_sha256(rows)
    monkeypatch.setattr(subject, "EXPECTED_PARTITIONS", 1)
    monkeypatch.setattr(subject, "EXPECTED_ROWS", 2)
    monkeypatch.setattr(subject, "EXPECTED_START", first.isoformat())
    monkeypatch.setattr(subject, "EXPECTED_END", (first + timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(subject, "OFFICIAL_REST_CONTENT_SHA256", digest)
    return records, digest


def test_epoch_ms_is_exact_and_rejects_submillisecond_input() -> None:
    opened = datetime(1970, 1, 1, tzinfo=UTC)
    closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)

    assert subject._epoch_ms(opened) == 0
    assert subject._epoch_ms(closed) == 299_999
    with pytest.raises(RuntimeError, match="millisecond-aligned"):
        subject._epoch_ms(opened + timedelta(microseconds=1))


def test_load_registered_rows_uses_deep_owner_reader_and_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records, digest = _configure_small_corpus(monkeypatch)
    selection = _selection(dataset_hash=subject.REGISTERED_DATASET_HASH, records=records)
    observed: dict[str, Any] = {}

    def resolve(*args: object, **kwargs: object) -> SimpleNamespace:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return selection

    rows, metadata = subject.load_registered_rows(
        tmp_path,
        resolve_dataset=resolve,
        iter_klines=lambda value: value.records,
    )

    assert subject.content_sha256(rows) == digest
    assert observed["args"] == (
        tmp_path,
        subject.REGISTERED_DATASET_ID,
        subject.REGISTERED_DATASET_VERSION,
    )
    assert observed["kwargs"] == {
        "timeframe": "5m",
        "consumer": "modular_research_runner",
        "deep": True,
    }
    assert metadata == {
        "consumer": "modular_research_runner",
        "dataset_hash": subject.REGISTERED_DATASET_HASH,
        "dataset_id": subject.REGISTERED_DATASET_ID,
        "dataset_version": 1,
        "partition_count": 1,
        "row_count": 2,
        "source_uri": subject.REGISTERED_SOURCE_URI,
        "validation_status": "VALID",
    }


def test_load_registered_rows_fails_closed_on_dataset_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records, _ = _configure_small_corpus(monkeypatch)
    selection = _selection(dataset_hash="0" * 64, records=records)

    with pytest.raises(RuntimeError, match="dataset hash mismatch"):
        subject.load_registered_rows(
            tmp_path,
            resolve_dataset=lambda *args, **kwargs: selection,
            iter_klines=lambda value: value.records,
        )


def test_load_registered_rows_fails_closed_on_official_content_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records, _ = _configure_small_corpus(monkeypatch)
    monkeypatch.setattr(subject, "OFFICIAL_REST_CONTENT_SHA256", "0" * 64)
    selection = _selection(dataset_hash=subject.REGISTERED_DATASET_HASH, records=records)

    with pytest.raises(RuntimeError, match="differs from accepted official REST content"):
        subject.load_registered_rows(
            tmp_path,
            resolve_dataset=lambda *args, **kwargs: selection,
            iter_klines=lambda value: value.records,
        )


def test_registered_provenance_is_explicit_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records, digest = _configure_small_corpus(monkeypatch)
    rows = [subject._record_to_kline(record) for record in records]
    output = tmp_path / "canonical.parquet"
    output.write_bytes(b"canonical")
    monkeypatch.setattr(subject, "OFFICIAL_REST_PARQUET_SHA256", subject.sha256_file(output))
    metadata = {
        "consumer": "modular_research_runner",
        "dataset_hash": subject.REGISTERED_DATASET_HASH,
        "dataset_id": subject.REGISTERED_DATASET_ID,
        "dataset_version": 1,
        "partition_count": 1,
        "row_count": 2,
        "source_uri": subject.REGISTERED_SOURCE_URI,
        "validation_status": "VALID",
    }

    provenance = subject.build_registered_provenance(
        rows=rows,
        metadata=metadata,
        parquet_path=output,
        generated_from_commit="a" * 40,
    )

    assert provenance["source"] == subject.REGISTERED_SOURCE
    assert provenance["api_attempt_status"] == "HTTP_451_RESTRICTED_LOCATION"
    assert provenance["official_rest_content_sha256"] == digest
    assert provenance["official_content_parity"] is True
    assert provenance["registered_dataset"] == metadata
    assert provenance["drive_comparison_status"] == "NOT_REQUESTED"
    assert provenance["drive_content_parity"] is None
    serialized = json.dumps(provenance, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "/Users/" not in serialized
    assert "/home/" not in serialized


def test_registered_provenance_rejects_parquet_container_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records, _ = _configure_small_corpus(monkeypatch)
    rows = [subject._record_to_kline(record) for record in records]
    output = tmp_path / "canonical.parquet"
    output.write_bytes(b"drifted")
    monkeypatch.setattr(subject, "OFFICIAL_REST_PARQUET_SHA256", "0" * 64)

    with pytest.raises(RuntimeError, match="Parquet differs"):
        subject.build_registered_provenance(
            rows=rows,
            metadata={},
            parquet_path=output,
            generated_from_commit="a" * 40,
        )


def test_module_import_does_not_require_market_data_package() -> None:
    assert util.find_spec("benchmarks.registered_canonical_dataset") is not None
