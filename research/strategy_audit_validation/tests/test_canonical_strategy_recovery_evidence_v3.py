from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

_GIT_BLOB_OID = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _manifest() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "compositions"
        / "canonical-strategy-recovery-evidence-v3.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _git_blob_oid(path: str) -> str:
    payload = (_REPOSITORY_ROOT / path).read_bytes()
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def test_dataset_recovery_evidence_v3_is_fail_closed() -> None:
    manifest = _manifest()

    assert manifest["schema_version"] == 3
    assert manifest["evidence_id"] == "canonical-strategy-recovery-evidence-v3"
    assert manifest["parent_evidence_id"] == "canonical-strategy-recovery-evidence-v2"
    assert manifest["status"] == "DATASET_ATTESTED_AND_REGISTRATION_ACCEPTED_FAIL_CLOSED"
    assert manifest["attestation_id"] == "btcusdt-drive-history-20260719-v1"
    assert manifest["attestation_canonical_sha256"] == (
        "e3cc285ba9fe4e7a50879ae52660f380d7177bb35a20f13c264f993cb6975a91"
    )
    assert manifest["registered_dataset_identity_proven"] is True
    assert manifest["market_endpoint_proven"] is False
    assert manifest["control_evidence_complete"] is False
    assert manifest["canonical"] is False
    assert manifest["executable"] is False
    assert manifest["source_head_before_manifest"] == ("c864dee0df9376241639b8fdc059959e82c42eb6")


def test_dataset_recovery_evidence_v3_blob_identities_match() -> None:
    manifest = _manifest()
    assets = manifest["assets"]

    assert len(assets) == 10
    paths = set()
    for blob_oid, path in assets:
        assert _GIT_BLOB_OID.fullmatch(blob_oid)
        assert path not in paths
        paths.add(path)
        assert _git_blob_oid(path) == blob_oid

    assert (
        "research/strategy_audit_validation/evidence/"
        "btcusdt-drive-dataset-attestation-v1.json" in paths
    )
    assert (
        "research/strategy_audit_validation/src/strategy_audit_validation/"
        "dataset_attestation.py" in paths
    )
    assert "docs/strategy-contracts/BTCUSDT_DRIVE_DATASET_ATTESTATION.md" in paths
