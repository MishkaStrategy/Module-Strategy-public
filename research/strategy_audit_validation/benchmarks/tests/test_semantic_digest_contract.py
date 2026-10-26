from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

_PANDAS_AVAILABLE = importlib.util.find_spec("pandas") is not None
pytestmark = pytest.mark.skipif(
    not _PANDAS_AVAILABLE,
    reason="canonical benchmark digest requires the dedicated pandas backtest environment",
)

if _PANDAS_AVAILABLE:
    _BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_BENCHMARK_ROOT))
    from benchmarks import _portfolio as portfolio
else:
    portfolio = None


def test_semantic_digest_ignores_commit_provenance_and_detects_result_drift() -> None:
    assert portfolio is not None
    summary: dict[str, object] = {
        "candidate_id": "strategy-candidate-example",
        "composition_hash": "a" * 64,
        "generated_from_commit": "b" * 40,
        "ledgers": {"combined": {"sha256": "c" * 64, "trade_count": 438}},
        "metrics": {"trade_count": 438, "final_equity": 15266.31855203826},
        "schema_version": 2,
        "semantic_digest_contract": portfolio.SEMANTIC_DIGEST_CONTRACT,
        "status": "OFFICIAL_ENDPOINT_BACKTEST_COMPLETE",
    }

    first = portfolio._semantic_digest(summary)
    summary["generated_from_commit"] = "d" * 40
    second = portfolio._semantic_digest(summary)
    assert first == second

    summary["metrics"] = {"trade_count": 439, "final_equity": 15266.31855203826}
    assert portfolio._semantic_digest(summary) != first


def test_semantic_digest_normalizes_only_proven_registered_canonical_transport() -> None:
    assert portfolio is not None
    rest: dict[str, object] = {
        "candidate_id": "strategy-candidate-example",
        "composition_hash": "a" * 64,
        "dataset": {
            "content_sha256": portfolio._OFFICIAL_REST_CONTENT_SHA256,
            "drive_comparison_status": "NOT_REQUESTED",
            "drive_content_parity": None,
            "endpoint": {
                "base_url": "https://fapi.binance.com",
                "method": "GET",
                "path": "/fapi/v1/klines",
                "security": "NONE",
            },
            "input_kind": portfolio._OFFICIAL_REST_SOURCE,
            "raw_5m_sha256": "d" * 64,
            "registered_dataset_relation": "UNPROVEN_FOR_THIS_RUN",
            "retained_registered_dataset_hash": portfolio.REGISTERED_DATASET_HASH,
            "retained_drive_raw_sha256": "e" * 64,
            "row_count": 721_597,
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "timezone": "UTC",
        },
        "ledgers": {"combined": {"sha256": "c" * 64, "trade_count": 438}},
        "metrics": {"trade_count": 438, "final_equity": 15266.31855203826},
        "schema_version": 2,
        "semantic_digest_contract": portfolio.SEMANTIC_DIGEST_CONTRACT,
        "status": "OFFICIAL_ENDPOINT_BACKTEST_COMPLETE",
    }
    registered = deepcopy(rest)
    registered_dataset = registered["dataset"]
    assert isinstance(registered_dataset, dict)
    registered_dataset["input_kind"] = portfolio._REGISTERED_CANONICAL_SOURCE
    registered_dataset["registered_dataset_relation"] = portfolio._REGISTERED_CANONICAL_RELATION

    assert portfolio._semantic_digest(registered) == portfolio._semantic_digest(rest)

    drifted = deepcopy(registered)
    drifted_dataset = drifted["dataset"]
    assert isinstance(drifted_dataset, dict)
    drifted_dataset["content_sha256"] = "0" * 64
    assert portfolio._semantic_digest(drifted) != portfolio._semantic_digest(rest)


def test_semantic_digest_does_not_hash_itself() -> None:
    assert portfolio is not None
    summary: dict[str, object] = {
        "generated_from_commit": "a" * 40,
        "semantic_digest_contract": portfolio.SEMANTIC_DIGEST_CONTRACT,
        "status": "OFFICIAL_ENDPOINT_BACKTEST_COMPLETE",
    }
    digest = portfolio._semantic_digest(summary)
    summary["semantic_digest"] = digest
    assert portfolio._semantic_digest(summary) == digest
