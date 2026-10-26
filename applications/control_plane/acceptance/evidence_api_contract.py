from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from modular_control_plane import ComponentId, ControlPlaneService, EvidenceControlService
from modular_control_plane.evidence import (
    AuditDecisionSnapshot,
    AuditEvidenceSnapshot,
    AuditIntegritySnapshot,
    EvidenceStateInvalid,
    ResultsEvidenceSnapshot,
    ResultsIntegritySnapshot,
)
from modular_control_plane.evidence_api import create_evidence_router
from modular_control_plane.server import ServerSettings, create_server

NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


@dataclass
class FakeAuditReader:
    broken: bool = False

    def integrity(self) -> AuditIntegritySnapshot:
        if self.broken:
            raise EvidenceStateInvalid("private audit path /Users/example/secret.sqlite3")
        return AuditIntegritySnapshot(
            evidence_count=1,
            decision_count=1,
            chain_head="f" * 64,
        )

    def list_decisions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[AuditDecisionSnapshot, ...]:
        if self.broken:
            raise EvidenceStateInvalid("TOP-SECRET")
        if offset:
            return ()
        return (
            AuditDecisionSnapshot(
                sequence=1,
                audit_id="audit-1",
                candidate_id="candidate-1",
                outcome="APPROVED_FOR_PAPER",
                evidence_reference="evidence://candidate-1/package-v1",
                evidence_hash="a" * 64,
                reason_codes=("PAPER_ADMISSION_APPROVED",),
                issued_at=NOW,
                auditor_id="independent-auditor",
                contract_version="strategy-audit-port-v1",
                decision_hash="b" * 64,
            ),
        )[:limit]

    def list_evidence(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[AuditEvidenceSnapshot, ...]:
        if self.broken:
            raise EvidenceStateInvalid("TOP-SECRET")
        if offset:
            return ()
        return (
            AuditEvidenceSnapshot(
                reference="evidence://candidate-1/package-v1",
                evidence_hash="a" * 64,
                candidate_id="candidate-1",
                first_seen_at=NOW,
                decision_count=1,
            ),
        )[:limit]


@dataclass
class FakeResultsReader:
    def integrity(self) -> ResultsIntegritySnapshot:
        return ResultsIntegritySnapshot(registry_revision=9, evidence_count=1)

    def list_evidence(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[ResultsEvidenceSnapshot, ...]:
        if offset:
            return ()
        return (
            ResultsEvidenceSnapshot(
                evidence_id="evidence-results-1",
                experiment_id="experiment-1",
                evidence_type="robustness",
                producer="results-aggregator",
                producer_version="1",
                status="PASS",
                severity="INFO",
                created_at=NOW,
                evidence_hash="c" * 64,
                content_hash="d" * 64,
                registered_revision=9,
                run_count=2,
                artifact_count=3,
                limitations_count=1,
            ),
        )[:limit]


def _client(control: EvidenceControlService | None) -> TestClient:
    service = ControlPlaneService(
        [control] if control is not None else [],
        required_components=(ComponentId.EVIDENCE,),
        clock=fixed_clock,
    )
    app = create_server(
        ServerSettings(),
        service=service,
        evidence_control=control,
    )
    return TestClient(app)


def test_evidence_api_projects_integrity_without_raw_payload_or_signature() -> None:
    control = EvidenceControlService(
        audit_reader=FakeAuditReader(),
        results_reader=FakeResultsReader(),
        clock=fixed_clock,
    )
    client = _client(control)

    health = client.get("/api/control/v1/components/evidence/health")
    overview = client.get("/api/control/v1/evidence/overview")
    decisions = client.get("/api/control/v1/evidence/audit/decisions")
    audit_evidence = client.get("/api/control/v1/evidence/audit/evidence")
    results = client.get("/api/control/v1/evidence/results")

    assert health.status_code == 200
    assert health.json()["status"] == "HEALTHY"
    assert overview.status_code == 200
    assert overview.json()["audit_integrity"] == {
        "schema_version": 1,
        "evidence_count": 1,
        "decision_count": 1,
        "chain_head": "f" * 64,
        "hash_chain_verified": True,
        "hmac_signatures_reverified": False,
    }
    assert decisions.status_code == 200
    decision = decisions.json()["decisions"][0]
    assert decision["outcome"] == "APPROVED_FOR_PAPER"
    assert decision["hash_chain_verified"] is True
    assert decision["hmac_signature_present"] is True
    assert decision["hmac_signature_reverified"] is False
    assert "signature" not in decision
    assert "request_hash" not in decision
    rendered = " ".join([decisions.text, audit_evidence.text, results.text])
    for forbidden in ("payload_json", "scope", "artifact_ids", "/Users/", "TOP-SECRET"):
        assert forbidden not in rendered
    result = results.json()["evidence"][0]
    assert result["run_count"] == 2
    assert result["artifact_count"] == 3
    assert result["limitations_count"] == 1


def test_evidence_source_not_configured_is_typed_and_non_retryable() -> None:
    control = EvidenceControlService(audit_reader=FakeAuditReader(), clock=fixed_clock)
    response = _client(control).get("/api/control/v1/evidence/results")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "CONTROL_EVIDENCE_SOURCE_NOT_CONFIGURED"
    assert error["retryable"] is False
    assert error["technical_details"] == {"source": "results_registry"}


def test_evidence_integrity_failure_is_redacted_and_fail_closed() -> None:
    control = EvidenceControlService(audit_reader=FakeAuditReader(broken=True), clock=fixed_clock)
    response = _client(control).get("/api/control/v1/evidence/overview")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTROL_EVIDENCE_STATE_INVALID"
    assert "TOP-SECRET" not in response.text
    assert "/Users/example" not in response.text


def test_evidence_router_honors_reader_auth_for_every_read() -> None:
    control = EvidenceControlService(audit_reader=FakeAuditReader(), clock=fixed_clock)

    def authorize(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer reader-token":
            raise HTTPException(status_code=401, detail="reader authentication required")

    app = create_server(
        ServerSettings(reader_token="reader-token"),
        service=ControlPlaneService([control], clock=fixed_clock),
        evidence_control=control,
    )
    client = TestClient(app)

    paths = (
        "/api/control/v1/evidence/overview",
        "/api/control/v1/evidence/audit/decisions",
        "/api/control/v1/evidence/audit/evidence",
    )
    for path in paths:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer reader-token"}).status_code == 200

    standalone = create_evidence_router(control, authorize_reader=authorize, clock=fixed_clock)
    assert standalone.routes
