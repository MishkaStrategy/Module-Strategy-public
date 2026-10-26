from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from strategy_audit_validation.errors import EvidenceMalformedError
from strategy_audit_validation.governance import (
    CompositionState,
    ModuleContract,
    load_composition,
)

EXPECTED_HASH = "b4bb0191a60cb46ffe305e8b04ae6536890c380a8a2a55ff412fec47d884966d"
EXPECTED_CANDIDATE_ID = "strategy-candidate-b4bb0191a60cb46ffe305e8b"
SOURCE_SHA = "ff7bf327e2c647882c3fe0ad5e85bf71979ebfa2"


def _compositions() -> Path:
    return Path(__file__).resolve().parents[1] / "compositions"


def _load(name: str) -> dict[str, Any]:
    value = json.loads((_compositions() / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _contracts() -> dict[str, ModuleContract]:
    registry = _load("module-contracts-recovery-v2.json")
    result: dict[str, ModuleContract] = {}
    for raw_value in registry["contracts"]:
        raw = cast(dict[str, Any], raw_value)
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


def _composition() -> dict[str, Any]:
    asset = _load("canonical-fvg-rt-final-15m-frozen-1h-4h.recovered-gap-v2.json")
    composition = asset["composition"]
    assert isinstance(composition, dict)
    return cast(dict[str, Any], composition)


def test_recovered_gap_v2_has_stable_identity_and_remains_incomplete() -> None:
    asset = _load("canonical-fvg-rt-final-15m-frozen-1h-4h.recovered-gap-v2.json")
    first = load_composition(_composition(), contracts=_contracts())
    second = load_composition(deepcopy(_composition()), contracts=_contracts())

    assert first.composition_hash == second.composition_hash == EXPECTED_HASH
    assert first.candidate_id == second.candidate_id == EXPECTED_CANDIDATE_ID
    assert asset["composition_hash"] == first.composition_hash
    assert asset["candidate_id"] == first.candidate_id
    assert first.state is CompositionState.INCOMPLETE
    assert first.complete is False
    assert first.missing_contracts == (
        "frozen_1h_short",
        "frozen_4h_short",
        "fvg_rt_final_15m",
    )
    assert asset["canonical"] is False
    assert asset["executable"] is False
    assert asset["approvable"] is False
    assert asset["ordering_status"] == "placeholder-only-blocked-by-STRATEGY-312-03"


def test_recovered_gap_v2_cannot_pass_canonical_gate() -> None:
    with pytest.raises(EvidenceMalformedError, match="canonical composition is incomplete"):
        load_composition(_composition(), contracts=_contracts(), require_canonical=True)


def test_recovered_gap_v2_rejects_false_contract_completion() -> None:
    payload = _composition()
    modules = cast(list[dict[str, Any]], payload["modules"])
    modules[0]["contract_complete"] = True

    with pytest.raises(EvidenceMalformedError, match="cannot declare a missing contract complete"):
        load_composition(payload, contracts=_contracts())


def test_recovered_gap_v2_identity_changes_when_control_binding_changes() -> None:
    original = load_composition(_composition(), contracts=_contracts())
    changed_payload = _composition()
    modules = cast(list[dict[str, Any]], changed_payload["modules"])
    modules[1]["parameters"]["expected_control_trades"] = 92
    changed = load_composition(changed_payload, contracts=_contracts())

    assert changed.composition_hash != original.composition_hash
    assert changed.candidate_id != original.candidate_id
    assert changed.complete is False


def test_recovered_gap_v2_sources_are_bound_to_recovery_head() -> None:
    registry = _load("module-contracts-recovery-v2.json")
    assert registry["inspected_commit_sha"] == SOURCE_SHA
    assert registry["status"] == "PARTIAL_RECOVERY_FAIL_CLOSED"
    assert all(item["source_commit_sha"] == SOURCE_SHA for item in registry["contracts"])

    original = _load("canonical-fvg-rt-final-15m-frozen-1h-4h.gap.json")
    assert original["composition"]["composition_version"] == "0.1.0-gap"
    assert original["composition_hash"] == (
        "954bb639db1fc250d14584e1abfb802ccb482c12840f29812b3f22256c427b04"
    )
