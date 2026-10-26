from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from modular_results_registry import (
    AuditEvidenceRecord,
    EvidenceStatus,
    ExperimentRecord,
    ExperimentStatus,
    RegistrationPackage,
    SQLiteRegistry,
    Severity,
)
from modular_results_registry.evidence_query import ReadOnlyEvidenceRegistry
from strategy_audit_validation import (
    AuditDecision,
    AuditOutcome,
    HmacDecisionSigner,
    SQLiteAuditStore,
)
from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.query import ReadOnlyAuditStore

AUDIT_ID = "audit-browser-evidence-1"
CANDIDATE_ID = "candidate-browser-evidence"
AUDIT_REFERENCE = "evidence://candidate-browser-evidence/package-v1"
RESULTS_EXPERIMENT_ID = "browser-evidence-experiment"
RESULTS_EVIDENCE_ID = "browser-results-evidence"
NOW = datetime(2026, 8, 7, 13, 45, tzinfo=UTC)


def seed_audit(path: Path) -> None:
    signer = HmacDecisionSigner(b"browser-evidence-hmac-key-32-bytes!!")
    with SQLiteAuditStore(path) as store:
        evidence = {
            "candidate": {"candidate_id": CANDIDATE_ID},
            "private_host_path": "/Users/private/browser-audit-evidence",
            "private_secret": "BROWSER-AUDIT-SECRET-DO-NOT-PROJECT",
        }
        evidence_hash = store.register_evidence(
            reference=AUDIT_REFERENCE,
            payload=evidence,
            candidate_id=CANDIDATE_ID,
            observed_at=NOW,
        )
        request_hash = canonical_sha256(
            {
                "reference": AUDIT_REFERENCE,
                "evidence_hash": evidence_hash,
                "auditor": "browser-independent-auditor",
            }
        )
        signature_payload = {
            "audit_id": AUDIT_ID,
            "candidate_id": CANDIDATE_ID,
            "status": "APPROVED_FOR_PAPER",
            "evidence_package_reference": AUDIT_REFERENCE,
            "evidence_hash": evidence_hash,
            "reason_codes": ("PAPER_ADMISSION_APPROVED",),
            "issued_at": NOW,
            "auditor_id": "browser-independent-auditor",
            "contract_version": "strategy-audit-port-v1",
            "schema_version": 1,
        }
        signature = signer.sign(signature_payload)
        decision = AuditDecision(
            audit_id=AUDIT_ID,
            candidate_id=CANDIDATE_ID,
            outcome=AuditOutcome.APPROVED_FOR_PAPER,
            evidence_package_reference=AUDIT_REFERENCE,
            evidence_hash=evidence_hash,
            reason_codes=("PAPER_ADMISSION_APPROVED",),
            issued_at=NOW,
            auditor_id="browser-independent-auditor",
            contract_version="strategy-audit-port-v1",
            signature=signature,
            decision_hash=canonical_sha256({**signature_payload, "signature": signature}),
        )
        store.save_legacy_unbound_decision(request_hash=request_hash, decision=decision)
        store.verify_integrity()

    reader = ReadOnlyAuditStore(path)
    integrity = reader.verify_integrity()
    decisions = reader.list_decisions()
    if integrity.decision_count != 1 or integrity.chain_head is None:
        raise RuntimeError("Evidence E2E audit ledger seed is incomplete")
    if len(decisions) != 1 or decisions[0].audit_id != AUDIT_ID:
        raise RuntimeError("Evidence E2E audit decision seed mismatch")
    if decisions[0].hmac_signature_reverified:
        raise RuntimeError("read-only audit observer must not claim HMAC re-verification")


def seed_results(path: Path) -> None:
    experiment = ExperimentRecord(
        experiment_id=RESULTS_EXPERIMENT_ID,
        research_id="browser-evidence-research",
        title="Browser Evidence experiment",
        description="Evidence Control real-browser fixture",
        strategy_id="browser-evidence-strategy",
        strategy_version="1.0.0",
        commit_sha="6c8a636700143e027eccd480c675f28ebd023cb6",
        branch="control-ui/evidence-fixture",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_id=None,
        module_versions={"results_aggregator": "0.1.0.dev0"},
        dependency_lock_hash="d" * 64,
        started_at=NOW,
        status=ExperimentStatus.COMPLETED,
        search_method="fixed",
        split_policy="holdout",
        completed_at=NOW,
        tags=("browser-e2e",),
    )
    evidence = AuditEvidenceRecord(
        evidence_id=RESULTS_EVIDENCE_ID,
        experiment_id=RESULTS_EXPERIMENT_ID,
        run_ids=(),
        evidence_type="robustness-summary",
        producer="results-aggregator",
        producer_version="0.1.0.dev0",
        status=EvidenceStatus.PASS,
        severity=Severity.INFO,
        artifact_ids=(),
        created_at=NOW,
        scope={
            "private_path": "/Users/private/browser-results-evidence",
            "private_secret": "BROWSER-RESULTS-SECRET-DO-NOT-PROJECT",
        },
        evidence_hash="e" * 64,
        limitations=("BROWSER-RESULTS-LIMITATION-DO-NOT-PROJECT",),
    )
    package = RegistrationPackage(
        package_id="browser-evidence-package",
        producer_module_id="browser-evidence-seed",
        producer_module_version="1",
        artifacts=(),
        datasets=(),
        configurations=(),
        experiments=(experiment,),
        runs=(),
        metric_definitions=(),
        metrics=(),
        evidence=(evidence,),
        roles=(),
    )
    with SQLiteRegistry(path) as registry:
        outcome = registry.register_package(package)
        if outcome.state != "REGISTERED":
            raise RuntimeError(f"Results evidence seed registration failed: {outcome.state}")

    reader = ReadOnlyEvidenceRegistry(path)
    integrity = reader.verify_integrity()
    rows = reader.list_evidence()
    if integrity.evidence_count != 1 or len(rows) != 1:
        raise RuntimeError("Evidence E2E Results registry seed is incomplete")
    if rows[0].evidence_id != RESULTS_EVIDENCE_ID:
        raise RuntimeError("Evidence E2E Results evidence identity mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-state-path", type=Path, required=True)
    parser.add_argument("--results-state-path", type=Path, required=True)
    args = parser.parse_args()

    audit_path = args.audit_state_path.expanduser().resolve()
    results_path = args.results_state_path.expanduser().resolve()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    seed_audit(audit_path)
    seed_results(results_path)
    print(
        " ".join(
            (
                f"audit_id={AUDIT_ID}",
                f"candidate_id={CANDIDATE_ID}",
                f"results_experiment_id={RESULTS_EXPERIMENT_ID}",
                f"results_evidence_id={RESULTS_EVIDENCE_ID}",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
