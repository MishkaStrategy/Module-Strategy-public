from __future__ import annotations

from copy import deepcopy

import pytest

from strategy_audit_validation.errors import EvidenceMalformedError
from strategy_audit_validation.recovery_contracts import (
    EXPECTED_COMBINED_TRADES,
    EXPECTED_COMPONENT_TRADES,
    load_control_evidence_manifest,
)


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "composition_name": "FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short",
        "dataset": {
            "dataset_id": "btc-usdtm-5m-2017-2026",
            "version": 1,
            "dataset_hash": "a" * 64,
            "exchange": "binance",
            "market": "usdt_m_perpetual",
            "symbol": "BTCUSDT",
            "base_timeframe": "5m",
            "timezone": "UTC",
            "start": "2017-08-17T00:00:00Z",
            "end": "2026-06-30T23:55:00Z",
            "row_count": 900_000,
            "gap_count": 0,
            "duplicate_count": 0,
        },
        "execution_contract_sha256": "b" * 64,
        "component_ledgers": [
            {
                "module_id": "fvg_rt_final_15m",
                "expected_trade_count": 283,
                "path": "evidence/final-15m-trades.jsonl",
                "sha256": "c" * 64,
            },
            {
                "module_id": "frozen_1h_short",
                "expected_trade_count": 91,
                "path": "evidence/frozen-1h-trades.jsonl",
                "sha256": "d" * 64,
            },
            {
                "module_id": "frozen_4h_short",
                "expected_trade_count": 21,
                "path": "evidence/frozen-4h-trades.jsonl",
                "sha256": "e" * 64,
            },
        ],
        "combined_ledger": {
            "expected_trade_count": 395,
            "path": "evidence/combined-395-trades.jsonl",
            "sha256": "f" * 64,
        },
        "signal_counts_before_portfolio": {
            "fvg_rt_final_15m": 1000,
            "frozen_1h_short": 250,
            "frozen_4h_short": 50,
        },
        "semantic_digest": "1" * 64,
        "generated_from_commit": "2" * 40,
    }


def test_control_evidence_manifest_accepts_exact_contract() -> None:
    manifest = load_control_evidence_manifest(_payload())

    assert manifest.dataset.exchange == "binance"
    assert manifest.dataset.market == "usdt_m_perpetual"
    assert manifest.dataset.symbol == "BTCUSDT"
    assert manifest.dataset.base_timeframe == "5m"
    assert manifest.dataset.row_count == 900_000
    component_counts = {
        item.module_id: item.expected_trade_count for item in manifest.component_ledgers
    }
    assert component_counts == dict(EXPECTED_COMPONENT_TRADES)
    assert manifest.combined_ledger.expected_trade_count == EXPECTED_COMBINED_TRADES
    assert dict(manifest.signal_counts_before_portfolio) == {
        "frozen_1h_short": 250,
        "frozen_4h_short": 50,
        "fvg_rt_final_15m": 1000,
    }


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("exchange", "other", "exchange must be binance"),
        ("market", "spot", "market must be usdt_m_perpetual"),
        ("symbol", "ETHUSDT", "symbol must be BTCUSDT"),
        ("base_timeframe", "15m", "base_timeframe must be 5m"),
        ("timezone", "Europe/Berlin", "timezone must be UTC"),
    ],
)
def test_control_dataset_identity_is_exact(field: str, value: str, match: str) -> None:
    payload = _payload()
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset[field] = value

    with pytest.raises(EvidenceMalformedError, match=match):
        load_control_evidence_manifest(payload)


def test_control_dataset_timestamps_require_zero_utc_offset() -> None:
    payload = _payload()
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset["start"] = "2017-08-17T03:00:00+03:00"

    with pytest.raises(EvidenceMalformedError, match="must use UTC offset"):
        load_control_evidence_manifest(payload)


def test_component_trade_count_mismatch_is_rejected() -> None:
    payload = _payload()
    ledgers = payload["component_ledgers"]
    assert isinstance(ledgers, list)
    first = ledgers[0]
    assert isinstance(first, dict)
    first["expected_trade_count"] = 282

    with pytest.raises(EvidenceMalformedError, match="trade count mismatch"):
        load_control_evidence_manifest(payload)


def test_duplicate_and_missing_component_ledgers_are_rejected() -> None:
    duplicate = _payload()
    ledgers = duplicate["component_ledgers"]
    assert isinstance(ledgers, list)
    ledgers[1] = deepcopy(ledgers[0])
    with pytest.raises(EvidenceMalformedError, match="duplicate component ledger"):
        load_control_evidence_manifest(duplicate)

    missing = _payload()
    missing_ledgers = missing["component_ledgers"]
    assert isinstance(missing_ledgers, list)
    missing_ledgers.pop()
    with pytest.raises(EvidenceMalformedError, match="component control ledgers incomplete"):
        load_control_evidence_manifest(missing)


def test_combined_trade_count_must_be_395() -> None:
    payload = _payload()
    combined = payload["combined_ledger"]
    assert isinstance(combined, dict)
    combined["expected_trade_count"] = 394

    with pytest.raises(EvidenceMalformedError, match="must be 395"):
        load_control_evidence_manifest(payload)


def test_ledger_hash_and_path_are_fail_closed() -> None:
    bad_hash = _payload()
    combined = bad_hash["combined_ledger"]
    assert isinstance(combined, dict)
    combined["sha256"] = "not-a-sha"
    with pytest.raises(EvidenceMalformedError, match="lowercase SHA-256"):
        load_control_evidence_manifest(bad_hash)

    traversal = _payload()
    ledgers = traversal["component_ledgers"]
    assert isinstance(ledgers, list)
    first = ledgers[0]
    assert isinstance(first, dict)
    first["path"] = "../outside.jsonl"
    with pytest.raises(EvidenceMalformedError, match="normalized relative path"):
        load_control_evidence_manifest(traversal)


def test_dataset_range_and_counts_are_validated() -> None:
    reversed_range = _payload()
    dataset = reversed_range["dataset"]
    assert isinstance(dataset, dict)
    dataset["start"] = dataset["end"]
    with pytest.raises(EvidenceMalformedError, match="start must be before end"):
        load_control_evidence_manifest(reversed_range)

    bad_rows = _payload()
    bad_dataset = bad_rows["dataset"]
    assert isinstance(bad_dataset, dict)
    bad_dataset["row_count"] = 0
    with pytest.raises(EvidenceMalformedError, match="positive integer"):
        load_control_evidence_manifest(bad_rows)


def test_signal_counts_must_cover_all_components() -> None:
    payload = _payload()
    counts = payload["signal_counts_before_portfolio"]
    assert isinstance(counts, dict)
    counts.pop("frozen_4h_short")

    with pytest.raises(EvidenceMalformedError, match="cover every component"):
        load_control_evidence_manifest(payload)
