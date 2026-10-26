from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from strategy_audit_validation.canonical import canonical_sha256
from strategy_audit_validation.errors import (
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
)
from strategy_audit_validation.governance import (
    ApprovalAuthority,
    ApprovalOutcome,
    ApprovalRecord,
    AuditCheckpoint,
    CANONICAL_COMPOSITION_NAME,
    COMPOSITION_SCHEMA_VERSION,
    CompositionCandidate,
    CompositionState,
    GovernanceLedger,
    ImmutableEvidence,
    ModuleContract,
    load_composition,
)

SOURCE_SHA = "a" * 40
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
KEY = b"wave2f-independent-approval-key-material-32+"


def _contracts(*, complete: bool = True) -> dict[str, ModuleContract]:
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


def _composition() -> dict[str, Any]:
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
                "parameters": {"threshold": "0.45"},
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


def _candidate() -> CompositionCandidate:
    return load_composition(_composition(), contracts=_contracts())


def _evidence_payload(candidate: CompositionCandidate) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "candidate_id": candidate.candidate_id,
        "composition_hash": candidate.composition_hash,
        "composition_version": candidate.composition_version,
        "generated_at": NOW.isoformat().replace("+00:00", "Z"),
        "source_commit_sha": SOURCE_SHA,
        "provenance": {
            "workload_id": "governance-synthetic-v1",
            "utc_only": True,
            "unfinished_htf_allowed": False,
            "lookahead_allowed": False,
        },
        "results": {"checks": ["composition"], "tags": {"stable"}},
    }


def _evidence(candidate: CompositionCandidate) -> ImmutableEvidence:
    return ImmutableEvidence.from_mapping(_evidence_payload(candidate))


def _approval(
    candidate: CompositionCandidate,
    evidence: ImmutableEvidence,
) -> tuple[ApprovalAuthority, ApprovalRecord]:
    authority = ApprovalAuthority(authority_id="independent-auditor", signing_key=KEY)
    record = authority.issue(
        candidate=candidate,
        evidence=evidence,
        outcome=ApprovalOutcome.APPROVED_FOR_PAPER,
        actor_id="independent-auditor",
        actor_role="independent_auditor",
        now=NOW,
    )
    return authority, record


def _seed(path: Path) -> tuple[GovernanceLedger, CompositionCandidate, ImmutableEvidence]:
    candidate = _candidate()
    evidence = _evidence(candidate)
    ledger = GovernanceLedger(path)
    ledger.register_candidate(candidate)
    ledger.register_evidence(evidence)
    return ledger, candidate, evidence


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"source_path": " "}, "source_path"),
        ({"required_parameters": frozenset({""})}, "required parameter"),
        ({"allowed_timeframes": frozenset({"bad"})}, "canonical timeframe"),
        ({"contract_complete": 1}, "completeness"),
        ({"source_commit_sha": "x"}, "Git SHA"),
    ],
)
def test_module_contract_rejects_invalid_fields(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    values: dict[str, Any] = {
        "module_id": "module.signal",
        "module_version": "1.0.0",
        "required_parameters": frozenset({"threshold"}),
        "allowed_timeframes": frozenset({"15m"}),
        "contract_complete": True,
        "source_path": "modules/signal.py",
        "source_commit_sha": SOURCE_SHA,
    }
    values.update(kwargs)
    with pytest.raises(EvidenceMalformedError, match=match):
        ModuleContract(**values)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p: p.pop("timezone"), "fields differ"),
        (lambda p: p.__setitem__("extra", True), "fields differ"),
        (lambda p: p.__setitem__("schema_version", "bad"), "schema_version"),
        (lambda p: p.__setitem__("composition_name", " "), "composition_name"),
        (lambda p: p.__setitem__("composition_version", "bad id"), "portable identifier"),
        (lambda p: p.__setitem__("lookahead_allowed", True), "look-ahead"),
        (
            lambda p: p["source_provenance"].__setitem__("repository", "other/repo"),
            "repository mismatch",
        ),
        (lambda p: p["source_provenance"].__setitem__("paths", []), "paths"),
        (lambda p: p.__setitem__("modules", []), "at least one module"),
        (
            lambda p: p["modules"].append(deepcopy(p["modules"][0])),
            "module IDs must be unique",
        ),
        (lambda p: p["modules"][0].__setitem__("timeframes", []), "at least one timeframe"),
        (
            lambda p: p["modules"][0].__setitem__("timeframes", ["5m"]),
            "unsupported timeframe",
        ),
        (
            lambda p: p["modules"][0]["source_provenance"].__setitem__(
                "path", "wrong.py"
            ),
            "source path mismatch",
        ),
        (
            lambda p: p["modules"][0]["source_provenance"].__setitem__(
                "commit_sha", "b" * 40
            ),
            "source commit mismatch",
        ),
        (
            lambda p: p["modules"][0].__setitem__("contract_complete", "yes"),
            "contract_complete must be bool",
        ),
        (
            lambda p: p["modules"][0].__setitem__("dependencies", ["missing.module"]),
            "unknown dependencies",
        ),
        (
            lambda p: p["modules"][0].__setitem__("dependencies", ["module.signal"]),
            "depend on itself",
        ),
        (
            lambda p: p.__setitem__("execution_order", ["module.signal"]),
            "execution_order",
        ),
        (
            lambda p: p.__setitem__("dependency_order", ["module.signal"]),
            "dependency_order must",
        ),
    ],
)
def test_composition_schema_negative_paths(mutator: Any, match: str) -> None:
    payload = _composition()
    mutator(payload)
    with pytest.raises((EvidenceMalformedError, ValueError), match=match):
        load_composition(payload, contracts=_contracts())


def test_canonical_module_set_mismatch_and_noncanonical_request() -> None:
    payload = _composition()
    payload["composition_name"] = CANONICAL_COMPOSITION_NAME
    with pytest.raises(EvidenceMalformedError, match="module set mismatch"):
        load_composition(payload, contracts=_contracts())
    with pytest.raises(EvidenceMalformedError, match="incomplete or misnamed"):
        load_composition(_composition(), contracts=_contracts(), require_canonical=True)


def test_incomplete_contract_cannot_be_declared_complete_or_candidate() -> None:
    payload = _composition()
    with pytest.raises(EvidenceMalformedError, match="cannot declare"):
        load_composition(payload, contracts=_contracts(complete=False))
    for module in payload["modules"]:
        module["contract_complete"] = False
    with pytest.raises(EvidenceMalformedError, match="incomplete composition"):
        load_composition(payload, contracts=_contracts(complete=False))


def test_candidate_manual_invariants_and_frozen_accessors() -> None:
    candidate = _candidate()
    assert candidate.composition_name == "governance-synthetic-v1"
    assert candidate.as_mapping()["modules"][0]["parameters"]["threshold"] == "0.45"
    assert _evidence(candidate).payload["results"]["tags"] == ("stable",)
    with pytest.raises(EvidenceTamperedError, match="identity"):
        replace(candidate, candidate_id="strategy-candidate-" + "0" * 24)
    with pytest.raises(EvidenceMalformedError, match="incomplete composition"):
        replace(candidate, complete=False)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda p: p.__setitem__("schema_version", 2), "schema_version"),
        (lambda p: p["provenance"].__setitem__("utc_only", False), "utc_only"),
        (
            lambda p: p["provenance"].__setitem__("unfinished_htf_allowed", True),
            "unfinished HTF",
        ),
        (lambda p: p["provenance"].__setitem__("workload_id", "bad id"), "identifier"),
        (lambda p: p.__setitem__("results", []), "must be a mapping"),
        (lambda p: p.__setitem__("generated_at", "not-a-date"), "ISO-8601"),
        (lambda p: p.__setitem__("generated_at", "2026-08-02T12:00:00"), "timezone-aware"),
        (lambda p: p.__setitem__("source_commit_sha", "bad"), "Git SHA"),
    ],
)
def test_evidence_schema_negative_paths(mutator: Any, match: str) -> None:
    candidate = _candidate()
    payload = _evidence_payload(candidate)
    mutator(payload)
    with pytest.raises(EvidenceMalformedError, match=match):
        ImmutableEvidence.from_mapping(payload)


def test_evidence_binding_and_tamper_negative_paths() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)
    with pytest.raises(EvidenceTamperedError, match="digest mismatch"):
        replace(evidence, evidence_digest="0" * 64).verify_for(
            candidate, now=NOW, maximum_age=timedelta(days=1)
        )
    noncanonical = evidence.encoded + b" "
    with pytest.raises(EvidenceTamperedError, match="canonical"):
        replace(
            evidence,
            encoded=noncanonical,
            evidence_digest=hashlib.sha256(noncanonical).hexdigest(),
        ).verify_for(candidate, now=NOW, maximum_age=timedelta(days=1))
    other = load_composition(
        {**_composition(), "created_by": "another-factory"}, contracts=_contracts()
    )
    with pytest.raises(EvidenceMalformedError, match="another candidate"):
        evidence.verify_for(other, now=NOW, maximum_age=timedelta(days=1))
    with pytest.raises(EvidenceMalformedError, match="composition hash"):
        replace(evidence, candidate_id=other.candidate_id).verify_for(
            other, now=NOW, maximum_age=timedelta(days=1)
        )
    with pytest.raises(EvidenceMalformedError, match="composition version"):
        replace(
            evidence,
            candidate_id=other.candidate_id,
            composition_hash=other.composition_hash,
            composition_version="9.0.0",
        ).verify_for(other, now=NOW, maximum_age=timedelta(days=1))
    with pytest.raises(EvidenceMalformedError, match="future-dated"):
        evidence.verify_for(
            candidate,
            now=NOW - timedelta(seconds=1),
            maximum_age=timedelta(days=1),
        )


def test_approval_record_and_authority_negative_paths() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)
    authority, record = _approval(candidate, evidence)
    with pytest.raises(ValueError, match="32 bytes"):
        ApprovalAuthority(authority_id="auditor", signing_key=b"short")
    with pytest.raises(EvidenceMalformedError, match="configured authority"):
        authority.issue(
            candidate=candidate,
            evidence=evidence,
            outcome=ApprovalOutcome.REJECTED,
            actor_id="another-auditor",
            actor_role="independent_auditor",
            now=NOW,
        )
    incomplete = replace(candidate, state=CompositionState.INCOMPLETE, complete=False)
    with pytest.raises(EvidenceMalformedError, match="incomplete composition"):
        authority.issue(
            candidate=incomplete,
            evidence=evidence,
            outcome=ApprovalOutcome.REJECTED,
            actor_id="independent-auditor",
            actor_role="independent_auditor",
            now=NOW,
        )
    with pytest.raises(EvidenceMalformedError, match="actor_role"):
        replace(record, actor_role="factory")
    with pytest.raises(EvidenceMalformedError, match="HMAC"):
        replace(record, signature="invalid")
    with pytest.raises(EvidenceTamperedError, match="approval digest"):
        replace(record, approval_digest="0" * 64)
    forged_signature = "hmac-sha256:" + "0" * 64
    forged = replace(
        record,
        signature=forged_signature,
        approval_digest=canonical_sha256(
            {**record.signature_payload(), "signature": forged_signature}
        ),
    )
    with pytest.raises(EvidenceTamperedError, match="signature mismatch"):
        authority.verify(forged)


def test_checkpoint_negative_paths_and_deduplication() -> None:
    candidate = _candidate()
    evidence = _evidence(candidate)
    checkpoint = AuditCheckpoint.create(
        session_id="session-1",
        candidate_id=candidate.candidate_id,
        evidence_digest=evidence.evidence_digest,
        completed_steps=("composition", "composition", "evidence"),
    )
    assert checkpoint.completed_steps == ("composition", "evidence")
    with pytest.raises(EvidenceMalformedError, match="portable identifier"):
        AuditCheckpoint.create(
            session_id="bad id",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=(),
        )
    with pytest.raises(EvidenceTamperedError, match="checkpoint digest"):
        replace(checkpoint, checkpoint_digest="0" * 64).verify()


def test_canonical_stored_payload_guards(tmp_path: Path) -> None:
    ledger, _, _ = _seed(tmp_path / "root.sqlite3")
    ledger._connection.execute(
        "UPDATE composition_candidates SET payload_json = ?",
        (b"[]",),
    )
    with pytest.raises(EvidenceTamperedError, match="root is not a mapping"):
        ledger.verify_integrity()
    ledger.close()

    ledger, _, _ = _seed(tmp_path / "canonical.sqlite3")
    row = ledger._connection.execute(
        "SELECT payload_json FROM composition_candidates"
    ).fetchone()
    ledger._connection.execute(
        "UPDATE composition_candidates SET payload_json = ?",
        (bytes(row[0]) + b" ",),
    )
    with pytest.raises(EvidenceTamperedError, match="not canonical JSON"):
        ledger.verify_integrity()
    ledger.close()


def test_ledger_write_conflicts_and_restart_guards(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    candidate = _candidate()
    evidence = _evidence(candidate)
    with GovernanceLedger(path) as ledger:
        with pytest.raises(EvidenceMalformedError, match="not registered"):
            ledger.register_evidence(evidence)
        ledger.register_candidate(candidate)
        ledger._connection.execute(
            "UPDATE composition_candidates SET creator_id = 'mutated'"
        )
        with pytest.raises(EvidenceIdentityConflictError, match="reused"):
            ledger.register_candidate(candidate)
        ledger._connection.execute(
            "UPDATE composition_candidates SET creator_id = ?",
            (candidate.created_by,),
        )
        ledger.register_evidence(evidence)
        ledger._connection.execute(
            "UPDATE governance_evidence SET source_commit_sha = ?",
            ("b" * 40,),
        )
        with pytest.raises(EvidenceIdentityConflictError, match="reused"):
            ledger.register_evidence(evidence)
        ledger._connection.execute(
            "UPDATE governance_evidence SET source_commit_sha = ?",
            (SOURCE_SHA,),
        )
        checkpoint = AuditCheckpoint.create(
            session_id="session-1",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition", "evidence"),
        )
        ledger.save_checkpoint(checkpoint)
        ledger.save_checkpoint(checkpoint)
        rebound = AuditCheckpoint.create(
            session_id="session-1",
            candidate_id="other-candidate",
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition", "evidence"),
        )
        with pytest.raises(EvidenceIdentityConflictError, match="rebound"):
            ledger.save_checkpoint(rebound)
        backwards = AuditCheckpoint.create(
            session_id="session-1",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition",),
        )
        with pytest.raises(EvidenceMalformedError, match="move backwards"):
            ledger.save_checkpoint(backwards)
        with pytest.raises(EvidenceMalformedError, match="does not exist"):
            ledger.resume_checkpoint(
                session_id="missing-session",
                candidate_id=candidate.candidate_id,
                evidence_digest=evidence.evidence_digest,
            )
        with pytest.raises(EvidenceMalformedError, match="candidate digest changed"):
            ledger.resume_checkpoint(
                session_id="session-1",
                candidate_id="other-candidate",
                evidence_digest=evidence.evidence_digest,
            )
        ledger._connection.execute(
            "UPDATE audit_checkpoints SET completed_steps_json = ?",
            (b"{}",),
        )
        with pytest.raises(EvidenceTamperedError, match="steps are malformed"):
            ledger.resume_checkpoint(
                session_id="session-1",
                candidate_id=candidate.candidate_id,
                evidence_digest=evidence.evidence_digest,
            )


def test_approval_duplicate_conflict(tmp_path: Path) -> None:
    ledger, candidate, evidence = _seed(tmp_path / "approval.sqlite3")
    authority, record = _approval(candidate, evidence)
    ledger.record_approval(record, authority=authority)
    ledger._connection.execute(
        "UPDATE governance_approvals SET payload_json = ?",
        (b"{}",),
    )
    with pytest.raises(EvidenceIdentityConflictError, match="reused"):
        ledger.record_approval(record, authority=authority)
    ledger.close()


@pytest.mark.parametrize(
    ("statement", "parameters", "match"),
    [
        (
            "UPDATE composition_candidates SET payload_digest = ?",
            ("0" * 64,),
            "candidate payload digest",
        ),
        (
            "UPDATE composition_candidates SET composition_hash = ?",
            ("0" * 64,),
            "composition hash mismatch",
        ),
        (
            "UPDATE composition_candidates SET candidate_id = ?",
            ("strategy-candidate-" + "0" * 24,),
            "candidate identity mismatch",
        ),
        (
            "UPDATE governance_evidence SET evidence_digest = ?",
            ("0" * 64,),
            "evidence digest mismatch",
        ),
        (
            "UPDATE audit_checkpoints SET completed_steps_json = ?",
            (b"{}",),
            "checkpoint steps are malformed",
        ),
        (
            "UPDATE audit_checkpoints SET checkpoint_digest = ?",
            ("0" * 64,),
            "checkpoint digest mismatch",
        ),
        (
            "UPDATE governance_approvals SET approval_digest = ?",
            ("0" * 64,),
            "approval digest mismatch",
        ),
        (
            "UPDATE governance_chain SET previous_record_hash = 'broken' WHERE sequence = 2",
            (),
            "chain linkage mismatch",
        ),
        (
            "UPDATE governance_chain SET record_hash = ? WHERE sequence = 1",
            ("0" * 64,),
            "record hash mismatch",
        ),
    ],
)
def test_ledger_integrity_tamper_matrix(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
    match: str,
) -> None:
    ledger, candidate, evidence = _seed(tmp_path / f"{match[:8]}.sqlite3")
    authority, record = _approval(candidate, evidence)
    ledger.record_approval(record, authority=authority)
    ledger.save_checkpoint(
        AuditCheckpoint.create(
            session_id="session-integrity",
            candidate_id=candidate.candidate_id,
            evidence_digest=evidence.evidence_digest,
            completed_steps=("composition",),
        )
    )
    ledger._connection.execute("PRAGMA foreign_keys = OFF")
    ledger._connection.execute(statement, parameters)
    with pytest.raises((EvidenceMalformedError, EvidenceTamperedError), match=match):
        ledger.verify_integrity()
    ledger.close()


def test_ledger_missing_reference_and_sequence_detection(tmp_path: Path) -> None:
    ledger, candidate, evidence = _seed(tmp_path / "missing-reference.sqlite3")
    authority, record = _approval(candidate, evidence)
    ledger.record_approval(record, authority=authority)
    checkpoint = AuditCheckpoint.create(
        session_id="session-missing",
        candidate_id=candidate.candidate_id,
        evidence_digest=evidence.evidence_digest,
        completed_steps=("composition",),
    )
    ledger.save_checkpoint(checkpoint)
    ledger._connection.execute("PRAGMA foreign_keys = OFF")
    ledger._connection.execute(
        "UPDATE governance_evidence SET candidate_id = 'missing-candidate'"
    )
    with pytest.raises(EvidenceTamperedError, match="evidence candidate is missing"):
        ledger.verify_integrity()
    ledger.close()

    ledger, candidate, evidence = _seed(tmp_path / "sequence.sqlite3")
    ledger._connection.execute("DELETE FROM governance_chain WHERE sequence = 1")
    with pytest.raises(EvidenceTamperedError, match="sequence is not contiguous"):
        ledger.verify_integrity()
    ledger.close()
