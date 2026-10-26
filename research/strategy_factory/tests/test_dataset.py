from __future__ import annotations

import csv
import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from strategy_factory.contracts import DatasetDescriptor, DatasetGateStatus
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy


def analyzer() -> DatasetAnalyzer:
    return DatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )


def test_valid_dataset_creates_profile(descriptor: DatasetDescriptor) -> None:
    result = analyzer().analyze(descriptor)
    assert result.validation.status is DatasetGateStatus.ACCEPTED
    assert result.profile is not None
    assert result.profile.observations == descriptor.row_count
    assert result.profile.volatility_metrics["atr_fraction_mean"] > 0


def test_hash_mismatch_rejected(descriptor: DatasetDescriptor) -> None:
    result = analyzer().analyze(replace(descriptor, sha256="0" * 64))
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert result.validation.reason_codes == ("DATASET_HASH_MISMATCH",)


def test_duplicate_timestamp_rejected(descriptor: DatasetDescriptor, tmp_path: Path) -> None:
    source = Path(descriptor.uri)
    rows = list(csv.DictReader(source.open(encoding="utf-8")))
    rows.append(dict(rows[-1]))
    path = tmp_path / "duplicate.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    changed = replace(
        descriptor,
        uri=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        row_count=len(rows),
    )
    result = analyzer().analyze(changed)
    assert result.validation.status is DatasetGateStatus.REJECTED
    assert "DUPLICATE_TIMESTAMPS" in result.validation.reason_codes


def test_invalid_ohlc_rejected(descriptor: DatasetDescriptor, tmp_path: Path) -> None:
    source = Path(descriptor.uri)
    rows = list(csv.DictReader(source.open(encoding="utf-8")))
    rows[3]["high"] = "1"
    path = tmp_path / "bad-ohlc.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    changed = replace(
        descriptor,
        uri=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    result = analyzer().analyze(changed)
    assert "INVALID_OHLC" in result.validation.reason_codes
    assert result.profile is None
