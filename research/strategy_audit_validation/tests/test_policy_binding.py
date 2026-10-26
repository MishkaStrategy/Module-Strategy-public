from __future__ import annotations

from decimal import Decimal
from pathlib import Path

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


def test_auditor_rejects_validator_built_for_different_policy(tmp_path: Path) -> None:
    validator_policy = AuditPolicy(maximum_risk_fraction=Decimal("0.03"))
    auditor_policy = AuditPolicy(maximum_risk_fraction=Decimal("0.02"))
    validator = EvidenceValidator(
        policy=validator_policy,
        dataset_verifier=StaticDigestDatasetVerifier({"1" * 64}),
        results_verifier=StaticDigestResultsVerifier({"2" * 64}),
    )
    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    try:
        with pytest.raises(ValueError, match="policy fingerprints must match"):
            IndependentStrategyAuditor(
                auditor_id="independent-strategy-auditor-v1",
                policy=auditor_policy,
                validator=validator,
                signer=HmacDecisionSigner(b"policy-binding-test-key-32-bytes!!"),
                store=store,
            )
    finally:
        store.close()
