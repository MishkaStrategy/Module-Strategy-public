from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

_GIT_BLOB_OID = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_HASH = "b4bb0191a60cb46ffe305e8b04ae6536890c380a8a2a55ff412fec47d884966d"
EXPECTED_CANDIDATE_ID = "strategy-candidate-b4bb0191a60cb46ffe305e8b"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _manifest() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "compositions"
        / "canonical-strategy-recovery-evidence-v2.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _git_blob_oid(path: str) -> str:
    payload = (_REPOSITORY_ROOT / path).read_bytes()
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def test_recovery_evidence_v2_freezes_complete_recovery_handoff() -> None:
    manifest = _manifest()

    assert manifest["schema_version"] == 2
    assert manifest["status"] == "PARTIAL_RECOVERY_FAIL_CLOSED"
    assert manifest["canonical"] is False
    assert manifest["executable"] is False
    assert manifest["composition_hash"] == EXPECTED_HASH
    assert manifest["recovered_candidate_id"] == EXPECTED_CANDIDATE_ID
    assert manifest["source_head_before_manifest"] == "71694dd6c097b0fff5ae59fa8eca64d5c9486380"

    assets = manifest["assets"]
    assert len(assets) == 11
    assert all(_GIT_BLOB_OID.fullmatch(item["git_blob_oid"]) for item in assets)
    assert all(item["path"] and item["role"] for item in assets)

    paths = {item["path"] for item in assets}
    assert "docs/strategy-contracts/CANONICAL_STRATEGY_OWNER_HANDOFF.md" in paths
    assert (
        "research/strategy_audit_validation/compositions/"
        "canonical-fvg-rt-final-15m-frozen-1h-4h.recovered-gap-v2.json" in paths
    )
    assert (
        "research/strategy_audit_validation/src/strategy_audit_validation/"
        "recovery_contracts.py" in paths
    )


def test_recovery_evidence_v2_git_blob_identities_match_repository_bytes() -> None:
    manifest = _manifest()

    for asset in manifest["assets"]:
        assert _git_blob_oid(asset["path"]) == asset["git_blob_oid"]


def test_recovery_evidence_v2_keeps_missing_authority_explicit() -> None:
    manifest = _manifest()

    limitation = manifest["identity_limitation"]
    assert "Frozen 4H authority" in limitation
    assert "endpoint URI lineage" in limitation
    assert "component/combined trade ledgers" in limitation
