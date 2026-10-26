from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_GIT_BLOB_OID = re.compile(r"^[0-9a-f]{40}$")


def _load_manifest() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "compositions"
        / "canonical-strategy-recovery-evidence-v1.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_recovery_evidence_manifest_is_non_executable_and_complete() -> None:
    manifest = _load_manifest()

    assert manifest["status"] == "PARTIAL_RECOVERY_FAIL_CLOSED"
    assert manifest["canonical"] is False
    assert manifest["executable"] is False
    assert manifest["identity_algorithm"] == "git_blob_oid_sha1"
    assert manifest["issue"] == 312
    assert manifest["pr"] == 316

    assets = manifest["assets"]
    paths = {item["path"] for item in assets}
    assert paths == {
        "docs/strategy-contracts/CANONICAL_STRATEGY_RECOVERY_AUDIT.md",
        "research/strategy_audit_validation/compositions/recovered-contracts-v1.json",
        "research/strategy_audit_validation/compositions/fvg-rt-final-15m.recovered.json",
        "research/strategy_audit_validation/compositions/frozen-1h-short.recovered-gap.json",
        "research/strategy_audit_validation/compositions/frozen-4h-short.gap.json",
        "research/strategy_audit_validation/compositions/canonical-strategy-decision-queue-v1.json",
    }
    assert all(_GIT_BLOB_OID.fullmatch(item["git_blob_oid"]) for item in assets)
    assert all(item["role"] for item in assets)


def test_manifest_does_not_claim_missing_trade_ledger_sha256() -> None:
    manifest = _load_manifest()

    assert "SHA-256 signal and trade ledgers remain required" in manifest["identity_limitation"]
    assert manifest["source_head_before_manifest"] == ("0cec22962e2bbe24bd35e9c22caa5be56532d650")
