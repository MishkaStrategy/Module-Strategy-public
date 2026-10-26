from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXPECTED_DECISIONS = {
    "STRATEGY-312-01",
    "STRATEGY-312-02",
    "STRATEGY-312-03",
    "STRATEGY-312-04",
    "STRATEGY-312-05",
    "STRATEGY-312-06",
}


def _load_queue() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "compositions"
        / "canonical-strategy-decision-queue-v1.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_open_decision_queue_blocks_canonical_transition() -> None:
    queue = _load_queue()

    assert queue["status"] == "OWNER_DECISIONS_REQUIRED"
    assert queue["all_decisions_resolved"] is False
    assert queue["canonical_transition_allowed"] is False
    assert queue["issue"] == 312
    assert queue["pr"] == 316

    decisions = queue["decisions"]
    assert {item["decision_id"] for item in decisions} == EXPECTED_DECISIONS
    assert all(item["status"] == "OPEN" for item in decisions)
    assert all(item["required_fields"] for item in decisions)
    assert all(item["blocks"] for item in decisions)


def test_control_evidence_decision_records_registration_but_stays_open() -> None:
    queue = _load_queue()
    decision = next(item for item in queue["decisions"] if item["decision_id"] == "STRATEGY-312-05")

    assert decision["status"] == "OPEN"
    progress = decision["progress"]
    assert progress["status"] == "DATASET_ATTESTED_AND_REGISTRATION_ACCEPTED"
    assert progress["attestation_id"] == "btcusdt-drive-history-20260719-v1"
    assert progress["attestation_canonical_sha256"] == (
        "e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91"
    )
    assert progress["dataset_bytes_attested"] is True
    assert progress["derived_timeframes_match_5m"] is True
    assert progress["corpus_hash"] == (
        "6d32c075f5adf3623679edd49c20ee8ee8758d3ea0e75b7f06613593e065c44e"
    )

    registration = progress["dataset_registration"]
    assert registration["registration_identity_proven"] is True
    assert registration["backtest_smoke_passed"] is True
    assert registration["persistent_registry_instance_retained"] is False
    assert registration["dataset_id"] == "binance_usdt-m-perpetual_btcusdt_5m"
    assert registration["dataset_version"] == 1
    assert registration["dataset_hash"] == (
        "49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5"
    )
    assert registration["evidence_canonical_sha256"] == (
        "016e397c3067777a927cdd012e6fc12bfa1056c81504d4c2653d9a6e58a3c573"
    )

    provenance = progress["market_provenance"]
    assert provenance["repository_market_type_declared"] is True
    assert provenance["repository_market_type"] == "usdt_m_perpetual"
    assert provenance["api_endpoint_uri_proven"] is False
    assert provenance["repository_index_blob_oid"] == ("4a79dc860d1a582cfc3a2e54847467b9b02cf327")

    assert progress["row_counts"] == {
        "10m": 360798,
        "15m": 240532,
        "1h": 60133,
        "4h": 15032,
        "5m": 721597,
    }
    assert progress["raw_file_sha256"]["10m"] == (
        "570cb34a910ae36d7cf74d2a68fb93048f2c4f3e86abd262f298e9a8919cdef3"
    )
    assert progress["raw_file_sha256"]["5m"] == (
        "2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1"
    )

    required = set(decision["required_fields"])
    assert "283-trade Final 15m ledger and SHA-256" in required
    assert "91-trade Frozen 1H ledger and SHA-256" in required
    assert "21-trade Frozen 4H ledger and SHA-256" in required
    assert "395-trade combined ledger and SHA-256" in required
    assert (
        "authoritative originating API endpoint URI or equivalent immutable source lineage"
        in required
    )
    assert all("registered dataset ID/version" not in item for item in required)


def test_frozen_4h_decision_requires_execution_and_recovery_semantics() -> None:
    queue = _load_queue()
    decision = next(item for item in queue["decisions"] if item["decision_id"] == "STRATEGY-312-02")

    required = set(decision["required_fields"])
    assert "entry formula and first eligible execution bar" in required
    assert "stop formula buffer and maximum distance" in required
    assert "target R and fee treatment" in required
    assert "no-progress timing MFE formula and event priority" in required
    assert "checkpoint and restart contract" in required
