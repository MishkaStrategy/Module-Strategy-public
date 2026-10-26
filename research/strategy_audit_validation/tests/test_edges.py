from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from conftest import MANIFEST_HASH, NOW, RESULT_HASH, SIGNING_KEY, build_auditor

from strategy_audit_validation import (
    AuditDecision,
    AuditOutcome,
    AuditPolicy,
    EvidenceIdentityConflictError,
    EvidenceMalformedError,
    EvidenceTamperedError,
    EvidenceValidator,
    HmacDecisionSigner,
    IndependentStrategyAuditor,
    LocalStrategyAuditPort,
    SQLiteAuditStore,
    StaticDigestDatasetVerifier,
    StaticDigestResultsVerifier,
    VerificationResult,
)
from strategy_audit_validation.canonical import (
    CanonicalizationError,
    canonical_sha256,
    canonical_value,
    strict_json_loads,
)


class ExampleEnum(StrEnum):
    VALUE = "value"


@dataclass(frozen=True)
class ExampleData:
    value: Decimal


def test_canonical_value_covers_supported_and_forbidden_types(tmp_path: Path) -> None:
    assert canonical_value(ExampleData(Decimal("1.20"))) == {"value": {"$decimal": "1.20"}}
    assert canonical_value(ExampleEnum.VALUE) == "value"
    assert canonical_value(tmp_path / "a") == (tmp_path / "a").as_posix()
    assert canonical_value({"b": 2, "a": 1}) == {"a": 1, "b": 2}
    assert canonical_value({3, 1, 2}) == [1, 2, 3]
    assert canonical_value((1, "a")) == [1, "a"]
    assert canonical_value(1.5) == 1.5
    assert canonical_value(None) is None
    with pytest.raises(CanonicalizationError, match="nesting depth"):
        canonical_value("x", depth=97)
    with pytest.raises(CanonicalizationError, match="finite"):
        canonical_value(Decimal("NaN"))
    with pytest.raises(CanonicalizationError, match="finite"):
        canonical_value(float("inf"))
    with pytest.raises(CanonicalizationError, match="non-empty string"):
        canonical_value({1: "bad"})
    with pytest.raises(CanonicalizationError, match="unsupported"):
        canonical_value(b"bytes")
    with pytest.raises(CanonicalizationError, match="invalid JSON document"):
        strict_json_loads("{")


def test_verification_result_contract_rejects_invalid_values() -> None:
    with pytest.raises(EvidenceMalformedError, match="verified"):
        VerificationResult(verified=1, reason_codes=("A",), evidence={})  # type: ignore[arg-type]
    with pytest.raises(EvidenceMalformedError, match="requires reason"):
        VerificationResult(verified=True, reason_codes=(), evidence={})
    with pytest.raises(EvidenceMalformedError, match="non-empty strings"):
        VerificationResult(verified=True, reason_codes=("",), evidence={})


def _valid_decision(tmp_path: Path, valid_evidence: dict[str, Any]) -> AuditDecision:
    auditor, store = build_auditor(tmp_path / "contract.sqlite")
    decision = auditor.audit(
        evidence_package_reference="artifact:contract", evidence=valid_evidence
    )
    store.close()
    return decision


@pytest.mark.parametrize(
    "change",
    [
        {"audit_id": ""},
        {"candidate_id": "bad id"},
        {"auditor_id": ""},
        {"evidence_package_reference": ""},
        {"evidence_hash": "bad"},
        {"contract_version": "v2"},
        {"signature": "not-hmac"},
        {"reason_codes": ()},
        {"issued_at": datetime(2026, 1, 1)},
        {"live_trading_authorized": True},
        {"schema_version": 2},
        {"decision_hash": "0" * 64},
    ],
)
def test_audit_decision_contract_rejects_invalid_fields(
    tmp_path: Path, valid_evidence: dict[str, Any], change: dict[str, Any]
) -> None:
    decision = _valid_decision(tmp_path, valid_evidence)
    expected = (
        EvidenceTamperedError if change.get("decision_hash") == "0" * 64 else EvidenceMalformedError
    )
    with pytest.raises(expected):
        replace(decision, **change)


def test_factory_mapping_is_immutable(tmp_path: Path, valid_evidence: dict[str, Any]) -> None:
    decision = _valid_decision(tmp_path, valid_evidence)
    mapping = decision.as_factory_mapping()
    with pytest.raises(TypeError):
        mapping["status"] = "REJECTED"  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"maximum_evidence_age_seconds": 0},
        {"maximum_evidence_age_seconds": True},
        {"maximum_drawdown_fraction": Decimal("NaN")},
        {"maximum_drawdown_fraction": Decimal("1.1")},
        {"minimum_results_packages": True},
        {"require_funding_lineage_for_futures": "yes"},
        {"require_external_dataset_verifier": 1},
        {"require_external_results_verifier": None},
        {"schema_version": 2},
    ],
)
def test_policy_rejects_invalid_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(EvidenceMalformedError):
        AuditPolicy(**kwargs)


def test_auditor_constructor_and_candidate_envelope_fail_closed(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    validator = EvidenceValidator(
        policy=AuditPolicy(),
        dataset_verifier=StaticDigestDatasetVerifier({MANIFEST_HASH}),
        results_verifier=StaticDigestResultsVerifier({RESULT_HASH}),
    )
    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    with pytest.raises(ValueError, match="auditor_id"):
        IndependentStrategyAuditor(
            auditor_id=" bad ",
            policy=AuditPolicy(),
            validator=validator,
            signer=HmacDecisionSigner(SIGNING_KEY),
            store=store,
        )
    auditor = IndependentStrategyAuditor(
        auditor_id="auditor",
        policy=AuditPolicy(),
        validator=validator,
        signer=HmacDecisionSigner(SIGNING_KEY),
        store=store,
        clock=lambda: NOW,
    )
    with pytest.raises(EvidenceMalformedError, match="candidate must be"):
        auditor.audit(evidence_package_reference="artifact:none", evidence={})
    no_id = dict(valid_evidence)
    no_id["candidate"] = {}
    with pytest.raises(EvidenceMalformedError, match="candidate_id"):
        auditor.audit(evidence_package_reference="artifact:no-id", evidence=no_id)
    store.close()


def test_validator_identity_change_is_rejected(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    class BadValidator:
        def evaluate(
            self, evidence: Any, *, now: datetime
        ) -> tuple[str, AuditOutcome, tuple[str, ...]]:
            del evidence, now
            return "other-candidate", AuditOutcome.APPROVED_FOR_PAPER, ("BAD",)

    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    auditor = IndependentStrategyAuditor(
        auditor_id="auditor",
        policy=AuditPolicy(),
        validator=BadValidator(),  # type: ignore[arg-type]
        signer=HmacDecisionSigner(SIGNING_KEY),
        store=store,
        clock=lambda: NOW,
    )
    with pytest.raises(EvidenceMalformedError, match="identity changed"):
        auditor.audit(evidence_package_reference="artifact:bad-validator", evidence=valid_evidence)
    store.close()


def test_port_preserves_domain_error_and_rejects_nonmapping(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")

    def domain_error(_: str) -> dict[str, Any]:
        raise EvidenceTamperedError("tampered")

    with pytest.raises(EvidenceTamperedError):
        LocalStrategyAuditPort(auditor=auditor, evidence_resolver=domain_error).audit("artifact:x")
    with pytest.raises(EvidenceMalformedError, match="mapping"):
        LocalStrategyAuditPort(
            auditor=auditor,
            evidence_resolver=lambda _: "not-a-mapping",  # type: ignore[arg-type,return-value]
        ).audit("artifact:y")
    store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.__setitem__("schema_version", 2),
        lambda e: e.__setitem__("unknown", True),
        lambda e: e.pop("risk_policy"),
        lambda e: e.__setitem__("created_at", "2099-01-01T00:00:00Z"),
        lambda e: e["candidate"].__setitem__("live_trading_authorized", True),
        lambda e: e["dataset_lineage"].__setitem__("timeframes", ["5m", "5m"]),
        lambda e: e["dataset_lineage"].__setitem__("immutable", False),
        lambda e: e["robustness"].__setitem__("candidate_id", "other"),
        lambda e: e["research"].__setitem__("candidate_id", "other"),
        lambda e: e["research"].__setitem__("configuration_hash", "e" * 64),
        lambda e: e["research"].__setitem__(
            "periods", {"train": e["research"]["periods"]["train"]}
        ),
        lambda e: e["research"].__setitem__("random_seeds", []),
        lambda e: e["research"].__setitem__("random_seeds", [True]),
        lambda e: e["research"]["results_evidence"].__setitem__("registry_package_hashes", []),
        lambda e: e["risk_policy"].__setitem__("paper_only", False),
        lambda e: e["risk_policy"].__setitem__("maximum_risk_fraction", True),
        lambda e: e["risk_policy"].__setitem__("maximum_risk_fraction", "nan"),
        lambda e: e["risk_policy"].__setitem__("maximum_risk_fraction", "0"),
        lambda e: e["risk_policy"].__setitem__("maximum_drawdown_fraction", "2"),
        lambda e: e["execution_assumptions"].pop("private_exchange_access"),
    ],
)
def test_additional_malformed_evidence_paths_are_durable_rejections(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    mutation: Any,
) -> None:
    mutation(valid_evidence)
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(evidence_package_reference="artifact:edge", evidence=valid_evidence)
    assert decision.outcome is AuditOutcome.REJECTED
    store.close()


def test_spot_lineage_does_not_require_funding(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    valid_evidence["dataset_lineage"]["market_type"] = "spot"
    valid_evidence["dataset_lineage"].pop("funding_lineage_sha256")
    valid_evidence["execution_assumptions"]["market_type"] = "spot"
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(evidence_package_reference="artifact:spot", evidence=valid_evidence)
    assert decision.outcome is AuditOutcome.APPROVED_FOR_PAPER
    store.close()


def test_datetime_objects_are_accepted_in_evidence(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    valid_evidence["created_at"] = NOW
    valid_evidence["research"]["periods"]["train"]["start"] = datetime(2020, 1, 1, tzinfo=UTC)
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(
        evidence_package_reference="artifact:datetime", evidence=valid_evidence
    )
    assert decision.outcome is AuditOutcome.APPROVED_FOR_PAPER
    store.close()


@pytest.mark.parametrize(
    "reference",
    ["", " whitespace "],
)
def test_store_rejects_bad_evidence_reference(tmp_path: Path, reference: str) -> None:
    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    with pytest.raises(ValueError, match="reference"):
        store.register_evidence(
            reference=reference,
            payload={"candidate": {"candidate_id": "candidate"}},
            candidate_id="candidate",
            observed_at=NOW,
        )
    store.close()


def test_store_rejects_invalid_request_digest(tmp_path: Path) -> None:
    store = SQLiteAuditStore(tmp_path / "audit.sqlite")
    with pytest.raises(EvidenceTamperedError, match="request_hash"):
        store.decision_for_request("bad")
    store.close()


def test_store_save_decision_replays_and_rejects_id_collision(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    decision = auditor.audit(evidence_package_reference="artifact:save", evidence=valid_evidence)
    row = store._connection.execute("SELECT request_hash FROM audit_decisions").fetchone()
    request_hash = str(row["request_hash"])
    assert store.save_decision(request_hash=request_hash, decision=decision) == decision
    with pytest.raises(EvidenceIdentityConflictError):
        store.save_decision(request_hash="a" * 64, decision=decision)
    store.close()


def test_store_rejects_decision_without_registered_evidence(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    decision = _valid_decision(tmp_path, valid_evidence)
    empty = SQLiteAuditStore(tmp_path / "empty.sqlite")
    with pytest.raises(EvidenceTamperedError, match="evidence is missing"):
        empty.save_decision(request_hash="a" * 64, decision=decision)
    empty.close()


@pytest.mark.parametrize(
    ("statement", "params", "message"),
    [
        ("UPDATE audit_metadata SET value = '99'", (), "metadata"),
        ("UPDATE audit_evidence SET first_seen_at = 'not-a-date'", (), "first_seen_at"),
        (
            "UPDATE audit_evidence SET payload_json = ?",
            (b'{ "candidate": {"candidate_id": "candidate-btc-001"}}',),
            "content hash",
        ),
        ("UPDATE audit_evidence SET candidate_id = 'other'", (), "candidate identity"),
        ("UPDATE audit_decisions SET previous_record_hash = ?", ("f" * 64,), "linkage"),
        ("UPDATE audit_decisions SET reason_codes_json = '{}'", (), "reason codes"),
        ("UPDATE audit_decisions SET issued_at = 'bad'", (), "issued_at"),
        ("UPDATE audit_decisions SET outcome = 'NOPE'", (), "decision is malformed"),
    ],
)
def test_store_detects_additional_tampering(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    statement: str,
    params: tuple[Any, ...],
    message: str,
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    auditor.audit(evidence_package_reference="artifact:tamper-more", evidence=valid_evidence)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(statement, params)
    connection.commit()
    connection.close()
    if "audit_metadata" in statement:
        with pytest.raises(EvidenceTamperedError, match=message):
            SQLiteAuditStore(path)
        return
    reopened = SQLiteAuditStore(path)
    with pytest.raises(EvidenceTamperedError, match=message):
        reopened.verify_integrity()
    reopened.close()


@pytest.mark.parametrize(
    ("statement", "params", "message"),
    [
        (
            "UPDATE audit_evidence SET payload_json = ?, evidence_hash = ?",
            (b"[]", canonical_sha256([])),
            "root is not a mapping",
        ),
        (
            "UPDATE audit_evidence SET payload_json = ?",
            (b"{",),
            "stored evidence JSON is invalid",
        ),
        (
            "UPDATE audit_evidence SET first_seen_at = ?",
            ("2024-01-01T00:00:00",),
            "timezone-aware",
        ),
    ],
)
def test_store_detects_structural_evidence_and_sequence_tampering(
    tmp_path: Path,
    valid_evidence: dict[str, Any],
    statement: str,
    params: tuple[Any, ...],
    message: str,
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    auditor.audit(evidence_package_reference="artifact:integrity-shape", evidence=valid_evidence)
    store._connection.execute(statement, params)
    with pytest.raises(EvidenceTamperedError, match=message):
        store.verify_integrity()
    store.close()


def test_store_detects_decision_rebound_to_different_evidence(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(
        evidence_package_reference="artifact:decision-evidence-rebind", evidence=valid_evidence
    )
    row = store._connection.execute(
        "SELECT request_hash, previous_record_hash FROM audit_decisions WHERE audit_id = ?",
        (decision.audit_id,),
    ).fetchone()
    payload = dict(decision.signature_payload())
    payload["evidence_hash"] = "f" * 64
    changed_decision_hash = canonical_sha256({**payload, "signature": decision.signature})
    changed_record_hash = canonical_sha256(
        {
            "request_hash": row["request_hash"],
            "decision_hash": changed_decision_hash,
            "previous_record_hash": row["previous_record_hash"],
        }
    )
    store._connection.execute(
        "UPDATE audit_decisions SET evidence_hash = ?, decision_hash = ?, record_hash = ? "
        "WHERE audit_id = ?",
        ("f" * 64, changed_decision_hash, changed_record_hash, decision.audit_id),
    )

    with pytest.raises(EvidenceTamperedError, match="different stored evidence"):
        store.verify_integrity()
    store.close()


def test_store_refuses_to_save_decision_against_unrelated_evidence(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    decision = auditor.audit(
        evidence_package_reference="artifact:save-evidence-rebind", evidence=valid_evidence
    )
    payload = dict(decision.signature_payload())
    payload["evidence_hash"] = "f" * 64
    payload["audit_id"] = "audit-unrelated-evidence"
    changed = replace(
        decision,
        audit_id="audit-unrelated-evidence",
        evidence_hash="f" * 64,
        decision_hash=canonical_sha256({**payload, "signature": decision.signature}),
    )

    with pytest.raises(EvidenceTamperedError, match="durable evidence identity differ"):
        store.save_decision(request_hash="a" * 64, decision=changed)
    store.close()


def test_store_rejects_noncontiguous_decision_sequence(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    auditor, store = build_auditor(tmp_path / "audit.sqlite")
    auditor.audit(evidence_package_reference="artifact:sequence-one", evidence=valid_evidence)
    second = dict(valid_evidence)
    second["candidate"] = {**valid_evidence["candidate"], "candidate_id": "candidate-btc-002"}
    auditor.audit(evidence_package_reference="artifact:sequence-two", evidence=second)
    store._connection.execute("UPDATE audit_decisions SET sequence = 3 WHERE sequence = 2")

    with pytest.raises(EvidenceTamperedError, match="sequence is not contiguous"):
        store.verify_chain()
    store.close()


def test_store_detects_missing_evidence_with_foreign_keys_disabled(
    tmp_path: Path, valid_evidence: dict[str, Any]
) -> None:
    path = tmp_path / "audit.sqlite"
    auditor, store = build_auditor(path)
    auditor.audit(evidence_package_reference="artifact:missing-row", evidence=valid_evidence)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM audit_evidence")
    connection.commit()
    connection.close()
    reopened = SQLiteAuditStore(path)
    with pytest.raises(EvidenceTamperedError, match="evidence is missing"):
        reopened.verify_integrity()
    reopened.close()
