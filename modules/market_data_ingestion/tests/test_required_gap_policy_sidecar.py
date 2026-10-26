from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_ingestion.enums import DataType
from market_data_ingestion.required_gap_policy import (
    GapArchiveEvidence,
    RequiredGapException,
    RequiredGapPolicy,
    load_required_gap_policy,
    persist_required_gap_policy,
    policy_adapter_version,
    policy_hash_from_adapter_version,
)

_START = datetime(2020, 1, 19, 13, 10, tzinfo=UTC)
_END = datetime(2020, 1, 19, 13, 35, tzinfo=UTC)
_MONTHLY = (
    "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/"
    "BTCUSDT/5m/BTCUSDT-5m-2020-01.zip"
)
_DAILY = (
    "https://data.binance.vision/data/futures/um/daily/markPriceKlines/"
    "BTCUSDT/5m/BTCUSDT-5m-2020-01-19.zip"
)


def _policy() -> RequiredGapPolicy:
    return RequiredGapPolicy(
        exceptions=(
            RequiredGapException(
                data_type=DataType.MARK_PRICE,
                timeframe="5m",
                start=_START,
                end=_END,
                missing_intervals=5,
                evidence=(
                    GapArchiveEvidence(_MONTHLY, "1" * 64),
                    GapArchiveEvidence(_DAILY, "2" * 64),
                ),
            ),
        )
    )


def test_required_gap_policy_sidecar_roundtrip_and_adapter_binding(tmp_path: Path) -> None:
    policy = _policy()
    path = persist_required_gap_policy(tmp_path, policy)

    assert path.is_file()
    adapter_version = policy_adapter_version("binance-test", policy)
    assert policy_hash_from_adapter_version(adapter_version) == policy.canonical_hash
    assert load_required_gap_policy(tmp_path, policy.canonical_hash) == policy


def test_required_gap_policy_sidecar_tampering_fails_closed(tmp_path: Path) -> None:
    policy = _policy()
    path = persist_required_gap_policy(tmp_path, policy)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_required_gap_policy(tmp_path, policy.canonical_hash)


def test_required_gap_policy_rejects_non_mark_price_exception() -> None:
    with pytest.raises(ValueError, match="only permits MARK_PRICE"):
        RequiredGapException(
            data_type=DataType.KLINES,
            timeframe="5m",
            start=_START,
            end=_END,
            missing_intervals=5,
            evidence=(
                GapArchiveEvidence(_MONTHLY, "1" * 64),
                GapArchiveEvidence(_DAILY, "2" * 64),
            ),
        )


def test_required_gap_policy_requires_monthly_and_daily_official_evidence() -> None:
    with pytest.raises(ValueError, match="one monthly and one daily"):
        RequiredGapException(
            data_type=DataType.MARK_PRICE,
            timeframe="5m",
            start=_START,
            end=_END,
            missing_intervals=5,
            evidence=(
                GapArchiveEvidence(_MONTHLY, "1" * 64),
                GapArchiveEvidence(
                    _MONTHLY.replace("2020-01.zip", "2020-02.zip"),
                    "2" * 64,
                ),
            ),
        )
