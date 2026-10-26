from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from strategy_audit_validation import (
    AuditPolicy,
    EvidenceValidator,
    HmacDecisionSigner,
    IndependentStrategyAuditor,
    SQLiteAuditStore,
    StaticDigestDatasetVerifier,
    StaticDigestResultsVerifier,
)
from strategy_audit_validation.canonical import canonical_sha256

NOW = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
MANIFEST_HASH = "1" * 64
DATASET_HASH = "2" * 64
FUNDING_HASH = "3" * 64
RESULT_HASH = "4" * 64
ROBUSTNESS_HASH = "5" * 64
SIGNING_KEY = b"strategy-audit-test-key-32-bytes-minimum!!"


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def valid_evidence(now: datetime) -> dict[str, Any]:
    parameters = {
        "entry_model.minimum_score": "0.70",
        "protection_target.reward_r": "4.5",
    }
    return {
        "schema_version": 1,
        "package_id": "audit-package-btc-001",
        "created_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "candidate": {
            "candidate_id": "candidate-btc-001",
            "configuration_hash": canonical_sha256(parameters),
            "parameters": parameters,
            "live_trading_authorized": False,
        },
        "dataset_lineage": {
            "dataset_id": "binance-btcusdt-usdtm-v2",
            "registry_version": "registry-v2",
            "instrument": "BTCUSDT",
            "exchange": "binance",
            "market_type": "linear_futures",
            "manifest_sha256": MANIFEST_HASH,
            "dataset_sha256": DATASET_HASH,
            "funding_lineage_sha256": FUNDING_HASH,
            "timeframes": ["5m", "15m", "1h", "4h", "1d"],
            "immutable": True,
            "tamper_verified": True,
        },
        "research": {
            "candidate_id": "candidate-btc-001",
            "configuration_hash": canonical_sha256(parameters),
            "periods": {
                "train": {
                    "start": "2020-01-01T00:00:00Z",
                    "end": "2022-01-01T00:00:00Z",
                },
                "validation": {
                    "start": "2022-01-01T00:00:00Z",
                    "end": "2023-01-01T00:00:00Z",
                },
                "test": {
                    "start": "2023-01-01T00:00:00Z",
                    "end": "2024-01-01T00:00:00Z",
                },
                "holdout": {
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2025-01-01T00:00:00Z",
                },
            },
            "holdout_isolated": True,
            "holdout_opened_once": True,
            "random_seeds": [7, 19, 41],
            "results_evidence": {
                "registry_package_hashes": [RESULT_HASH],
            },
        },
        "robustness": {
            "candidate_id": "candidate-btc-001",
            "report_sha256": ROBUSTNESS_HASH,
            "passed": True,
        },
        "execution_assumptions": {
            "mode": "paper",
            "exchange": "binance",
            "market_type": "linear_futures",
            "live_trading_authorized": False,
            "private_exchange_access": False,
            "order_placement": False,
        },
        "risk_policy": {
            "paper_only": True,
            "maximum_risk_fraction": "0.02",
            "maximum_drawdown_fraction": "0.30",
        },
    }


@pytest.fixture
def evidence_copy(valid_evidence: dict[str, Any]):
    def factory() -> dict[str, Any]:
        return deepcopy(valid_evidence)

    return factory


def build_auditor(
    path: Path,
    *,
    now: datetime = NOW,
    manifest_hashes: set[str] | None = None,
    result_hashes: set[str] | None = None,
    key: bytes = SIGNING_KEY,
    policy: AuditPolicy | None = None,
    dataset_verifier: Any | None = None,
    results_verifier: Any | None = None,
    contract_version: str = "strategy-audit-port-v1",
) -> tuple[IndependentStrategyAuditor, SQLiteAuditStore]:
    store = SQLiteAuditStore(path)
    validator = EvidenceValidator(
        policy=policy or AuditPolicy(),
        dataset_verifier=(
            dataset_verifier
            if dataset_verifier is not None
            else StaticDigestDatasetVerifier(manifest_hashes or {MANIFEST_HASH})
        ),
        results_verifier=(
            results_verifier
            if results_verifier is not None
            else StaticDigestResultsVerifier(result_hashes or {RESULT_HASH})
        ),
    )
    auditor = IndependentStrategyAuditor(
        auditor_id="independent-strategy-auditor-v1",
        policy=policy or AuditPolicy(),
        validator=validator,
        signer=HmacDecisionSigner(key),
        store=store,
        clock=lambda: now,
        contract_version=contract_version,
    )
    return auditor, store
