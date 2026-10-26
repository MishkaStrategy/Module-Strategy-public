from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy_audit_validation import EvidenceMalformedError
from strategy_audit_validation.governance import ModuleContract, load_composition


def test_committed_canonical_gap_asset_is_fail_closed() -> None:
    package_root = Path(__file__).resolve().parents[1]
    compositions = package_root / "compositions"
    registry = json.loads((compositions / "module-contracts-v1.json").read_text(encoding="utf-8"))
    asset = json.loads(
        (compositions / "canonical-fvg-rt-final-15m-frozen-1h-4h.gap.json").read_text(
            encoding="utf-8"
        )
    )
    contracts = {
        item["module_id"]: ModuleContract(
            module_id=item["module_id"],
            module_version=item["module_version"],
            required_parameters=frozenset(item["required_parameters"]),
            allowed_timeframes=frozenset(item["allowed_timeframes"]),
            contract_complete=item["contract_complete"],
            source_path=item["source_path"],
            source_commit_sha=item["source_commit_sha"],
        )
        for item in registry["contracts"]
    }
    candidate = load_composition(asset["composition"], contracts=contracts)
    assert candidate.complete is False
    assert candidate.composition_hash == asset["composition_hash"]
    assert candidate.candidate_id == asset["candidate_id"]
    assert asset["canonical"] is False
    assert asset["executable"] is False
    assert asset["approvable"] is False
    with pytest.raises(EvidenceMalformedError, match="canonical composition is incomplete"):
        load_composition(
            asset["composition"],
            contracts=contracts,
            require_canonical=True,
        )
