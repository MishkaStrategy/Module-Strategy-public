from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from strategy_audit_validation.governance import ModuleContract, load_composition

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _PACKAGE_ROOT.parents[1]
_COMPOSITIONS = _PACKAGE_ROOT / "compositions"
_REPORTS = _REPOSITORY_ROOT / "reports" / "strategy-governance-wave2f"
_EXPECTED_HASH = "51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9"
_EXPECTED_CANDIDATE = "strategy-candidate-51659c6ed582f2843357bd33"
_EXPECTED_SEMANTIC_DIGEST = "63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb"
_EXPECTED_SEMANTIC_CONTRACT = "canonical-strategy-v2-result-v1"


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _contracts() -> dict[str, ModuleContract]:
    registry = _load(_COMPOSITIONS / "module-contracts-v2.json")
    contracts: dict[str, ModuleContract] = {}
    for raw in registry["contracts"]:
        contract = ModuleContract(
            module_id=raw["module_id"],
            module_version=raw["module_version"],
            required_parameters=frozenset(raw["required_parameters"]),
            allowed_timeframes=frozenset(raw["allowed_timeframes"]),
            contract_complete=raw["contract_complete"],
            source_path=raw["source_path"],
            source_commit_sha=raw["source_commit_sha"],
        )
        contracts[contract.module_id] = contract
    return contracts


def test_wave2f_resolution_and_stream_a_handoff_match_canonical_identity() -> None:
    composition_path = _COMPOSITIONS / "canonical-fvg-rt-final-15m-frozen-1h-4h.v2.json"
    candidate = load_composition(
        _load(composition_path),
        contracts=_contracts(),
        require_canonical=True,
    )
    resolution = _load(_REPORTS / "contract-gaps.json")
    handoff = _load(_REPORTS / "handoff-to-stream-a.json")

    assert candidate.complete is True
    assert candidate.composition_hash == _EXPECTED_HASH
    assert candidate.candidate_id == _EXPECTED_CANDIDATE

    assert resolution["result"] == "RESOLVED_BY_OWNER_AUTHORIZED_VARIANT_2"
    assert resolution["remaining_gaps"] == []
    assert resolution["resolution"]["canonical_load_result"] == "PASS"
    assert resolution["resolution"]["composition_hash"] == candidate.composition_hash
    assert resolution["resolution"]["candidate_id"] == candidate.candidate_id
    assert resolution["historical_gap"]["state"] == "INCOMPLETE"

    assert handoff["status"] == ("CANONICAL_STRATEGY_READY_FOR_RESEARCH_PLATFORM_CONSUMPTION")
    assert handoff["remaining_gaps"] == []
    assert handoff["composition_hash"] == candidate.composition_hash
    assert handoff["candidate_id"] == candidate.candidate_id
    reference = handoff["expected_strategy_reference"]
    assert reference["combined_trade_count"] == 438
    assert reference["semantic_digest"] == _EXPECTED_SEMANTIC_DIGEST
    assert reference["semantic_digest_contract"] == _EXPECTED_SEMANTIC_CONTRACT
    assert reference["status"] == "REFERENCE_TO_REPRODUCE_NOT_A_PREAPPROVED_RESEARCH_RESULT"


def test_wave2f_evidence_checksum_manifest_matches_repository_bytes() -> None:
    manifest = _load(_REPORTS / "evidence-checksums.json")
    assert manifest["algorithm"] == "sha256"
    for relative_path, expected_digest in manifest["files"].items():
        path = _REPOSITORY_ROOT / relative_path
        assert path.is_file(), relative_path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest
