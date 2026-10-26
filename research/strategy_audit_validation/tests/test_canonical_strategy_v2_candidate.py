from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from strategy_audit_validation.errors import EvidenceMalformedError
from strategy_audit_validation.governance import ModuleContract, load_composition

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSITIONS = _ROOT / "compositions"
_EXPECTED_HASH = "51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9"
_EXPECTED_CANDIDATE = "strategy-candidate-51659c6ed582f2843357bd33"


def _load(name: str) -> dict[str, Any]:
    payload = json.loads((_COMPOSITIONS / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _contracts() -> dict[str, ModuleContract]:
    registry = _load("module-contracts-v2.json")
    result: dict[str, ModuleContract] = {}
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
        result[contract.module_id] = contract
    return result


def test_variant_two_composition_loads_as_complete_canonical_candidate() -> None:
    payload = _load("canonical-fvg-rt-final-15m-frozen-1h-4h.v2.json")
    candidate = load_composition(payload, contracts=_contracts(), require_canonical=True)

    assert candidate.complete is True
    assert candidate.missing_contracts == ()
    assert candidate.state.value == "CANDIDATE"
    assert candidate.composition_hash == _EXPECTED_HASH
    assert candidate.candidate_id == _EXPECTED_CANDIDATE
    assert load_composition(payload, contracts=_contracts(), require_canonical=True) == candidate


def test_candidate_evidence_matches_governance_identity() -> None:
    evidence = _load("canonical-strategy-v2-candidate-evidence.json")
    assert evidence["composition_hash"] == _EXPECTED_HASH
    assert evidence["candidate_id"] == _EXPECTED_CANDIDATE
    assert evidence["historical_counts_mandatory"] is False
    assert evidence["state"] == "CANDIDATE"


def test_variant_two_composition_rejects_contract_and_ordering_drift() -> None:
    payload = _load("canonical-fvg-rt-final-15m-frozen-1h-4h.v2.json")
    contracts = _contracts()

    changed = json.loads(json.dumps(payload))
    changed["modules"][2]["module_version"] = "9.9.9"
    with pytest.raises(EvidenceMalformedError, match="incompatible module version"):
        load_composition(changed, contracts=contracts, require_canonical=True)

    changed = json.loads(json.dumps(payload))
    changed["execution_order"] = list(reversed(changed["execution_order"]))
    drift = load_composition(changed, contracts=contracts, require_canonical=True)
    assert drift.composition_hash != _EXPECTED_HASH

    incomplete = dict(contracts)
    incomplete["frozen_4h_short"] = replace(incomplete["frozen_4h_short"], contract_complete=False)
    with pytest.raises(EvidenceMalformedError, match="missing contract complete"):
        load_composition(payload, contracts=incomplete, require_canonical=True)
