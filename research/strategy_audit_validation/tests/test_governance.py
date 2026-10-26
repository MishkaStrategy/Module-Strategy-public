from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from strategy_audit_validation.errors import (
    EvidenceMalformedError,
    EvidenceTamperedError,
)
from strategy_audit_validation.governance import (
    CANONICAL_COMPOSITION_NAME,
    COMPOSITION_SCHEMA_VERSION,
    ApprovalAuthority,
    ApprovalOutcome,
    AuditCheckpoint,
    CompositionState,
    GovernanceLedger,
    ImmutableEvidence,
    ModuleContract,
    load_composition,
)

SOURCE_SHA = "a" * 40
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
KEY = b"wave2f-independent-approval-key-material-32+"


def contracts(*, complete: bool = True) -> dict[str, ModuleContract]:
    return {
        "module.signal": ModuleContract(
            module_id="module.signal",
            module_version="1.0.0",
            required_parameters=frozenset({"threshold"}),
            allowed_timeframes=frozenset({"15m"}),
            contract_complete=complete,
            source_path="modules/signal.py",
            source_commit_sha=SOURCE_SHA,
        ),
        "module.execution": ModuleContract(
            module_id="module.execution",
            module_version="2.0.0",
            required_parameters=frozenset({"ttl"}),
            allowed_timeframes=frozenset({"5m"}),
            contract_complete=complete,
            source_path="modules/execution.py",
            source_commit_sha=SOURCE_SHA,
        ),
    }


def composition_payload(*, threshold: str = "0.45") -> dict[str, object]:
    return {
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "composition_name": "governance-synthetic-v1",
        "composition_version": "1.0.0",
        "state": CompositionState.CANDIDATE.value,
        "timezone": "UTC",
        "lookahead_allowed": False,
        "unfinished_htf_allowed": False,
        "created_by": "strategy-factory",
        "source_provenance": {
            "repository": "MishkaStrategy/Module-Strategy",
            "commit_sha": SOURCE_SHA,
            "paths": ["modules/signal.py", "modules/execution.py"],
        },
        "modules": [
            {
                "module_id": "module.signal",
                "module_version": "1.0.0",
                "role": "signal",
                "parameters": {"threshold": threshold},
                "timeframes": ["15m"],
                "dependencies": [],
                "contract_complete": True,
                "source_provenance": {
                    "path": "modules/signal.py",
                    "commit_sha": SOURCE_SHA,
                },
            },
            {
                "module_id": "module.execution",
                "module_version": "2.0.0",
                "role": "execution",
                "parameters": {"ttl": 36},
                "timeframes": ["5m"],
                "dependencies": ["module.signal"],
                "contract_complete": True,
                "source_provenance": {
                    "path": "modules/execution.py",
                    "commit_sha": SOURCE_SHA,
                },
            },
        ],
        "execution_order": ["module.signal", "module.execution"],
        "dependency_order": ["module.signal", "module.execution"],
    }


def evidence_payload(candidate_id: str, composition_hash: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "composition_hash": composition_hash,
        "composition_version": "1.0.0",
        "generated_at": NOW.isoformat().replace("+00:00", "Z"),
        "source_commit_sha": SOURCE_SHA,
        "provenance": {
            "workload_id": "governance-synthetic-v1",
            "utc_only": True,
            "unfinished_htf_allowed": False,
            "lookahead_allowed": False,
        },
        "results": {
            "deterministic_digest": "b" * 64,
            "checks": ["composition", "evidence", "approval"],
        },
    }


def test_composition_hash_and_candidate_identity_are_deterministic() -> None:
    first = load_composition(composition_payload(), contracts=contracts())
    second = load_composition(deepcopy(composition_payload()), contracts=contracts())
    assert first.composition_hash == second.composition_hash
    assert first.candidate_id == second.candidate_id
    assert first.complete is True


def test_meaningful_parameter_change_changes_candidate_identity() -> None:
    first = load_composition(composition_payload(threshold="0.45"), contracts=contracts())
    second = load_composition(composition_payload(threshold="0.50"), contracts=contracts())
    assert first.composition_hash != second.composition_hash
    assert first.candidate_id != second.candidate_id


def test_unknown_module_is_rejected() -> None:
    payload = composition_payload()
    payload["modules"][0]["module_id"] = "module.unknown"  # type: ignore[index]
    with pytest.raises(EvidenceMalformedError, match="unknown strategy module"):
        load_composition(payload, contracts=contracts())


def test_incompatible_version_is_rejected() -> None:
    payload = composition_payload()
    payload["modules"][0]["module_version"] = "9.9.9"  # type: ignore[index]
    with pytest.raises(EvidenceMalformedError, match="incompatible module version"):
        load_composition(payload, contracts=contracts())


def test_missing_required_parameter_is_rejected() -> None:
    payload = composition_payload()
    payload["modules"][0]["parameters"] = {}  # type: ignore[index]
    with pytest.raises(EvidenceMalformedError, match="missing required parameters"):
        load_composition(payload, contracts=contracts())


def test_incomplete_canonical_composition_cannot_be_canonical() -> None:
    incomplete_contracts = {
        name: ModuleContract(
            module_id=name,
            module_version="missing-contract",
            required_parameters=frozenset(),
            allowed_timeframes=frozenset({"15m", "1h", "4h"}),
            contract_complete=False,
            source_path=f"missing/{name}.json",
            source_commit_sha=SOURCE_SHA,
        )
        for name in ("fvg_rt_final_15m", "frozen_1h_short", "frozen_4h_short")
    }
    payload = {
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "composition_name": CANONICAL_COMPOSITION_NAME,
        "composition_version": "0.1.0-gap",
        "state": CompositionState.INCOMPLETE.value,
        "timezone": "UTC",
        "lookahead_allowed": False,
        "unfinished_htf_allowed": False,
        "created_by": "strategy-governance-wave2f",
        "source_provenance": {
            "repository": "MishkaStrategy/Module-Strategy",
            "commit_sha": SOURCE_SHA,
            "paths": ["reports/strategy-governance-wave2f/contract-gaps.json"],
        },
        "modules": [
            {
                "module_id": name,
                "module_version": "missing-contract",
                "role": "signal",
                "parameters": {},
                "timeframes": [timeframe],
                "dependencies": [],
                "contract_complete": False,
                "source_provenance": {
                    "path": f"missing/{name}.json",
                    "commit_sha": SOURCE_SHA,
                },
            }
            for name, timeframe in (
                ("fvg_rt_final_15m", "15m"),
                ("frozen_1h_short", "1h"),
                ("frozen_4h_short", "4h"),
            )
        ],
        "execution_order": [
            "fvg_rt_final_15m",
            "frozen_1h_short",
            "frozen_4h_short",
        ],
        "dependency_order": [
            "fvg_rt_final_15m",
            "frozen_1h_short",
            "frozen_4h_short",
        ],
    }
    candidate = load_composition(payload, contracts=incomplete_contracts)
    assert candidate.complete is False
    assert candidate.missing_contracts == (
        "frozen_1h_short",
        "frozen_4h_short",
        "fvg_rt_final_15m",
    )
    with pytest.raises(EvidenceMalformedError, match="canonical composition is incomplete"):
        load_composition(payload, contracts=incomplete_contracts, require_canonical=True)


def test_utc_and_unfinished_htf_contracts_are_fail_closed() -> None:
    non_utc = composition_payload()
    non_utc["timezone"] = "Europe/Berlin"
    with pytest.raises(EvidenceMalformedError, match="timezone must be UTC"):
        load_composition(non_utc, contracts=contracts())

    unfinished = composition_payload()
    unfinished["unfinished_htf_allowed"] = True
    with pytest.raises(EvidenceMalformedError, match="unfinished HTF"):
        load_composition(unfinished, contracts=contracts())


def test_dependency_order_is_validated() -> None:
    payload = composition_payload()
    payload["dependency_order"] = ["module.execution", "module.signal"]
    with pytest.raises(EvidenceMalformedError, match="dependency_order violates"):
        load_composition(payload, contracts=contracts())


def test_evidence_is_immutable_and_bound_to_candidate() -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    original = evidence_payload(candidate.candidate_id, candidate.composition_hash)
    evidence = ImmutableEvidence.from_mapping(original)
    original["results"]["checks"].append("mutated")  # type: ignore[index,union-attr]
    assert evidence.payload["results"]["checks"] == (
        "composition",
        "evidence",
        "approval",
    )
    evidence.verify_for(candidate, now=NOW, maximum_age=timedelta(days=1))


def test_stale_evidence_is_rejected() -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    payload = evidence_payload(candidate.candidate_id, candidate.composition_hash)
    payload["generated_at"] = (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    evidence = ImmutableEvidence.from_mapping(payload)
    with pytest.raises(EvidenceMalformedError, match="stale"):
        evidence.verify_for(candidate, now=NOW, maximum_age=timedelta(days=7))


def test_evidence_provenance_forbids_lookahead_and_unfinished_htf() -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    payload = evidence_payload(candidate.candidate_id, candidate.composition_hash)
    payload["provenance"]["lookahead_allowed"] = True  # type: ignore[index]
    with pytest.raises(EvidenceMalformedError, match="look-ahead"):
        ImmutableEvidence.from_mapping(payload)


def test_candidate_cannot_approve_itself() -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    authority = ApprovalAuthority(authority_id="strategy-factory", signing_key=KEY)
    with pytest.raises(EvidenceMalformedError, match="cannot approve"):
        authority.issue(
            candidate=candidate,
            evidence=evidence,
            outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
            actor_id="strategy-factory",
            actor_role="independent_auditor",
            now=NOW,
        )


def test_approval_actor_separation_and_signature() -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    authority = ApprovalAuthority(authority_id="independent-auditor", signing_key=KEY)
    with pytest.raises(EvidenceMalformedError, match="only independent_auditor"):
        authority.issue(
            candidate=candidate,
            evidence=evidence,
            outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
            actor_id="independent-auditor",
            actor_role="strategy_factory",
            now=NOW,
        )
    record = authority.issue(
        candidate=candidate,
        evidence=evidence,
        outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
        actor_id="independent-auditor",
        actor_role="independent_auditor",
        now=NOW,
    )
    authority.verify(record)
    assert record.candidate_id == candidate.candidate_id


def test_resume_with_same_digest_and_partial_audit(tmp_path: Path) -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    path = tmp_path / "governance.sqlite3"
    with GovernanceLedger(path) as ledger:
        assert ledger.register_candidate(candidate) is True
        assert ledger.register_candidate(candidate) is False
        assert ledger.register_evidence(evidence) is True
        partial = AuditCheckpoint.create(
            session_id="audit-session-1",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition",),
        )
        ledger.save_checkpoint(partial)
    with GovernanceLedger(path) as ledger:
        resumed = ledger.resume_checkpoint(
            session_id="audit-session-1",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
        )
        assert resumed.checkpoint_digest == partial.checkpoint_digest
        completed = AuditCheckpoint.create(
            session_id="audit-session-1",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition", "evidence", "approval"),
        )
        ledger.save_checkpoint(completed)
        ledger.verify_integrity()
        assert ledger.counts()["checkpoints"] == 1


def test_restart_rejects_changed_digest(tmp_path: Path) -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    path = tmp_path / "governance.sqlite3"
    with GovernanceLedger(path) as ledger:
        ledger.register_candidate(candidate)
        ledger.register_evidence(evidence)
        ledger.save_checkpoint(
            AuditCheckpoint.create(
                session_id="audit-session-2",
                candidate_id=candidate.candidate_id,
                evidence_digest=evidence.evidence_digest,
                completed_steps=("composition",),
            )
        )
        with pytest.raises(EvidenceMalformedError, match="evidence digest changed"):
            ledger.resume_checkpoint(
                session_id="audit-session-2",
                candidate_id=candidate.candidate_id,
                evidence_digest="f" * 64,
            )


def test_corruption_detection(tmp_path: Path) -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    path = tmp_path / "governance.sqlite3"
    ledger = GovernanceLedger(path)
    ledger.register_candidate(candidate)
    ledger.register_evidence(evidence)
    ledger._connection.execute(
        "UPDATE governance_evidence SET payload_json = ? WHERE evidence_digest = ?",
        (b"{}", evidence.evidence_digest),
    )
    with pytest.raises(EvidenceTamperedError, match="evidence digest mismatch"):
        ledger.verify_integrity()
    ledger.close()


def test_approval_is_durable_and_bound_to_evidence(tmp_path: Path) -> None:
    candidate = load_composition(composition_payload(), contracts=contracts())
    evidence = ImmutableEvidence.from_mapping(
        evidence_payload(candidate.candidate_id, candidate.composition_hash)
    )
    authority = ApprovalAuthority(authority_id="independent-auditor", signing_key=KEY)
    record = authority.issue(
        candidate=candidate,
        evidence=evidence,
        outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
        actor_id="independent-auditor",
        actor_role="independent_auditor",
        now=NOW,
    )
    with GovernanceLedger(tmp_path / "governance.sqlite3") as ledger:
        ledger.register_candidate(candidate)
        ledger.register_evidence(evidence)
        assert ledger.record_approval(record, authority=authority) is True
        assert ledger.record_approval(record, authority=authority) is False
        ledger.verify_integrity()
        assert ledger.counts()["approvals"] == 1
