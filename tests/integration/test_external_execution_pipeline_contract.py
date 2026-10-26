from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.candidate_evidence_bridge import (
    EvidenceBridgeError,
    FixedWorkloadMapping,
    OutputEvidenceRequirement,
    verify_candidate_evidence_publication,
)
from scripts.external_admission_ledger import (
    CANONICAL_REPOSITORY,
    AdmissionAlreadyClaimed,
    AdmissionLedger,
    NetworkPolicy,
    ResourcePolicy,
    admission_digest,
)
from scripts.external_execution_publisher import LocalExecutionPublisher, PublicationError
from scripts.platform_release_manifest import canonical_json_bytes
from scripts.protected_admission_controller import (
    AuthenticatedExecutorObservation,
    AuthenticatedProviderObservation,
    ProtectedAdmissionController,
    ProtectedControllerPolicy,
    TrustedControllerInputs,
)

_SOURCE_SHA = "d" * 40
_PROVIDER_ID = "synthetic-provider"
_RUN_ID = "synthetic-run-001"
_ATTEMPT = 1
_RUNTIME_ID = "python312-linux-x86_64"
_ENVIRONMENT_ID = "synthetic-paper-linux"
_WORKLOAD_ID = "synthetic-paper-smoke"
_COMPONENT_ID = "synthetic-engine"
_GATE = "paper:synthetic-smoke"
_OUTPUT_PATH = "evidence/paper.json"
_DARWIN_ENVIRONMENT_ID = "synthetic-acceptance-darwin"
_BACKTEST_ENVIRONMENT_ID = "synthetic-historical-backtester"


class _TrustedLoader:
    def __init__(self, bundle: TrustedControllerInputs) -> None:
        self.bundle = bundle

    def load_for_source(self, source_sha: str) -> TrustedControllerInputs:
        if source_sha != _SOURCE_SHA:
            raise ValueError("synthetic source mismatch")
        return self.bundle


class _FakeProvider:
    def __init__(self, facts: AuthenticatedProviderObservation) -> None:
        self.facts = facts
        self.calls = 0

    def authenticate_current_run(self) -> AuthenticatedProviderObservation:
        self.calls += 1
        return self.facts


class _FakeExecutor:
    def authenticate_executor(
        self, *, credential: str, admission_id: str, grant_digest: str
    ) -> AuthenticatedExecutorObservation:
        if credential != "synthetic-test-credential":
            raise ValueError("invalid synthetic executor credential")
        return AuthenticatedExecutorObservation(
            executor_id="synthetic-executor",
            admission_id=admission_id,
            grant_digest=grant_digest,
            authentication_evidence_digest="b" * 64,
        )


def _identity(value: dict[str, Any], field: str) -> None:
    body = {key: item for key, item in value.items() if key != field}
    value[field] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _sha256_mapping(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _trusted_inputs(output_digest: str) -> TrustedControllerInputs:
    resource = ResourcePolicy(2000, 4_294_967_296, 10_737_418_240, 128, 3600)
    network = NetworkPolicy("disabled", ())
    resource_digest = _sha256_mapping(resource.to_mapping())
    network_digest = _sha256_mapping(network.to_mapping())

    candidate: dict[str, Any] = {
        "candidate_id": "",
        "source": {"repository": CANONICAL_REPOSITORY, "commit": _SOURCE_SHA},
        "components": [
            {"id": _COMPONENT_ID},
            {"id": "synthetic-darwin-check"},
            {"id": "synthetic-backtester"},
        ],
        "artifacts": [
            {"component": _COMPONENT_ID, "file": {"sha256": output_digest}},
            {"component": "synthetic-darwin-check", "file": {"sha256": "b" * 64}},
            {"component": "synthetic-backtester", "file": {"sha256": "c" * 64}},
        ],
    }
    _identity(candidate, "candidate_id")
    profile: dict[str, Any] = {
        "profile_id": "",
        "target_environments": [_ENVIRONMENT_ID],
        "acceptance_requirements": [
            {
                "gate": _GATE,
                "component_id": _COMPONENT_ID,
                "environment": _ENVIRONMENT_ID,
            },
            {
                "gate": "synthetic:darwin-check",
                "component_id": "synthetic-darwin-check",
                "environment": _DARWIN_ENVIRONMENT_ID,
            },
            {
                "gate": "backtest:synthetic-parity",
                "component_id": "synthetic-backtester",
                "environment": _BACKTEST_ENVIRONMENT_ID,
            },
        ],
        "python_runtimes": [{"id": _RUNTIME_ID}],
    }
    _identity(profile, "profile_id")
    environments = [
        {
            "environment_id": _ENVIRONMENT_ID,
            "node_class": "paper_linux",
            "os": "linux",
            "architecture": "x86_64",
            "image_sha256": "f" * 64,
            "runtime_ids": [_RUNTIME_ID],
        },
        {
            "environment_id": _DARWIN_ENVIRONMENT_ID,
            "node_class": "acceptance_darwin",
            "os": "darwin",
            "architecture": "arm64",
            "image_sha256": "e" * 64,
            "runtime_ids": [_RUNTIME_ID],
        },
        {
            "environment_id": _BACKTEST_ENVIRONMENT_ID,
            "node_class": "historical_backtester",
            "os": "linux",
            "architecture": "x86_64",
            "image_sha256": "d" * 64,
            "runtime_ids": [_RUNTIME_ID],
        },
    ]
    common_policy = {
        "resource_policy": {
            "policy_id": "synthetic-resource-v1",
            "sha256": resource_digest,
        },
        "network_policy": {
            "policy_id": "synthetic-network-disabled",
            "sha256": network_digest,
        },
    }
    workloads = [
        {
            "workload_id": _WORKLOAD_ID,
            "workload_kind": "gate",
            "gate": _GATE,
            "component_id": _COMPONENT_ID,
            "node_class": "paper_linux",
            "environment_id": _ENVIRONMENT_ID,
            "runtime_id": _RUNTIME_ID,
            "required_outputs": [
                {
                    "output_path": _OUTPUT_PATH,
                    "gate": _GATE,
                    "component_id": _COMPONENT_ID,
                    "artifact_sha256": output_digest,
                },
            ],
            "predecessor_material": None,
            "input_materials": [
                {"material_id": "synthetic-config", "sha256": "8" * 64},
            ],
            **common_policy,
        },
        {
            "workload_id": "synthetic-darwin-acceptance",
            "workload_kind": "gate",
            "gate": "synthetic:darwin-check",
            "component_id": "synthetic-darwin-check",
            "node_class": "acceptance_darwin",
            "environment_id": _DARWIN_ENVIRONMENT_ID,
            "runtime_id": _RUNTIME_ID,
            "required_outputs": [
                {
                    "output_path": "evidence/darwin.json",
                    "gate": "synthetic:darwin-check",
                    "component_id": "synthetic-darwin-check",
                    "artifact_sha256": "b" * 64,
                },
            ],
            "predecessor_material": None,
            "input_materials": [
                {"material_id": "synthetic-darwin-input", "sha256": "7" * 64},
            ],
            **common_policy,
        },
        {
            "workload_id": "synthetic-backtest",
            "workload_kind": "historical_backtest",
            "gate": "backtest:synthetic-parity",
            "component_id": "synthetic-backtester",
            "node_class": "historical_backtester",
            "environment_id": _BACKTEST_ENVIRONMENT_ID,
            "runtime_id": _RUNTIME_ID,
            "required_outputs": [
                {
                    "output_path": "evidence/backtest.json",
                    "gate": "backtest:synthetic-parity",
                    "component_id": "synthetic-backtester",
                    "artifact_sha256": "c" * 64,
                },
            ],
            "predecessor_material": None,
            "input_materials": [
                {"material_id": "synthetic-backtest-input", "sha256": "6" * 64},
            ],
            **common_policy,
        },
    ]
    inventory: dict[str, Any] = {
        "inventory_id": "",
        "schema_version": 1,
        "environments": environments,
        "resource_policies": [
            {"policy_id": "synthetic-resource-v1", "sha256": resource_digest},
        ],
        "network_policies": [
            {"policy_id": "synthetic-network-disabled", "sha256": network_digest},
        ],
        "workloads": workloads,
    }
    _identity(inventory, "inventory_id")

    policy = ProtectedControllerPolicy(
        schema_version=1,
        policy_id="synthetic-protected-policy",
        repository=CANONICAL_REPOSITORY,
        provider_id=_PROVIDER_ID,
        workflow_id="synthetic-release-controller.yml",
        workflow_digest="c" * 64,
        event="workflow_dispatch",
        ref="refs/heads/synthetic-contract",
        audience="synthetic-module-strategy-controller",
        grant_ttl_seconds=300,
        resource_policy_id="synthetic-resource-v1",
        resource_policy_digest=resource_digest,
        network_policy_id="synthetic-network-disabled",
        network_policy_digest=network_digest,
        resource_policy=resource,
        network_policy=network,
    )
    return TrustedControllerInputs(
        policy=policy,
        candidate=candidate,
        profile=profile,
        inventory=inventory,
        material_id="synthetic-input-material-set",
        material_digest="9" * 64,
    )


def test_authenticated_issue_claim_receipt_publish_and_evidence_binding(
    tmp_path: Path,
) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL CLI is required for the synthetic Ed25519 receipt")

    output_bytes = b"synthetic output; no workload code executed\n"
    output_digest = hashlib.sha256(output_bytes).hexdigest()
    trusted = _trusted_inputs(output_digest)
    facts = AuthenticatedProviderObservation(
        provider_id=_PROVIDER_ID,
        run_id=_RUN_ID,
        attempt=_ATTEMPT,
        repository=CANONICAL_REPOSITORY,
        source_sha=_SOURCE_SHA,
        workflow_id="synthetic-release-controller.yml",
        workflow_digest="c" * 64,
        event="workflow_dispatch",
        ref="refs/heads/synthetic-contract",
        audience="synthetic-module-strategy-controller",
        authentication_evidence_digest="a" * 64,
    )
    provider = _FakeProvider(facts)
    clock_calls = 0

    def controller_clock() -> datetime:
        nonlocal clock_calls
        value = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=clock_calls)
        clock_calls += 1
        return value

    ledger = AdmissionLedger(tmp_path / "admissions.sqlite3")
    controller = ProtectedAdmissionController(
        trusted_loader=_TrustedLoader(trusted),
        provider_authenticator=provider,
        ledger=ledger,
        grant_signing_key=b"synthetic-controller-grant-key-at-least-32-bytes",
        executor_authenticator=_FakeExecutor(),
        utc_clock=controller_clock,
    )

    issued = controller.issue()
    assert len(issued.admissions) == 3
    issued_admission = next(row for row in issued.admissions if row.workload_id == _WORKLOAD_ID)
    admission = ledger.get(issued_admission.admission_id)
    assert admission is not None
    assert issued.external_trust_established is False
    assert issued.acceptance_eligible is False
    assert issued.dispatch_authorized is False
    assert issued.plan.external_trust_established is False
    assert issued.plan.acceptance_eligible is False
    assert issued.plan.dispatch_authorized is False
    assert admission.workload_id == _WORKLOAD_ID
    assert admission.provider_id == facts.provider_id
    assert admission.provider_run_id == facts.run_id
    assert admission.provider_attempt == facts.attempt
    assert admission.source_sha == facts.source_sha
    assert admission.candidate_id == trusted.candidate["candidate_id"]
    assert admission.profile_id == trusted.profile["profile_id"]
    planned_workload = next(row for row in issued.plan.workloads if row.workload_id == _WORKLOAD_ID)
    assert admission.workload_digest == planned_workload.workload_sha256

    claim = controller.claim(
        admission_id=issued_admission.admission_id,
        grant=issued_admission.grant,
        credential="synthetic-test-credential",
    )
    assert claim.admission_id == admission.admission_id
    assert claim.executor_id == "synthetic-executor"
    assert claim.execution_deadline == admission.execution_deadline
    assert provider.calls == 2

    keys = tmp_path / "trusted-controller-keys"
    keys.mkdir()
    private_key = tmp_path / "synthetic-controller.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(keys / "controller.pub.pem"),
        ],
        check=True,
        capture_output=True,
    )
    receipt_payload = {
        "admission_id": admission.admission_id,
        "repository": admission.repository,
        "source_sha": admission.source_sha,
        "workflow_digest": admission.workflow_digest,
        "runtime_id": admission.runtime_id,
        "workload_id": admission.workload_id,
        "workload_digest": admission.workload_digest,
        "material_digest": admission.material_digest,
        "policy_id": admission.policy_id,
        "policy_digest": admission.policy_digest,
        "environment_id": admission.environment_id,
        "admission_digest": admission_digest(admission),
        "observed_provider": {
            "provider_id": facts.provider_id,
            "run_id": facts.run_id,
            "attempt": facts.attempt,
        },
        "observed_resource_policy_digest": _sha256_mapping(admission.resource_policy.to_mapping()),
        "observed_network_policy_digest": _sha256_mapping(admission.network_policy.to_mapping()),
        "executor": {
            "executor_id": claim.executor_id,
            "ephemeral": True,
            "image_digest": admission.image_digest,
            "image_os": admission.image_os,
            "image_arch": admission.image_arch,
            "observed_os": admission.image_os,
            "observed_arch": admission.image_arch,
            "created_at": "2030-01-01T00:00:30Z",
        },
        "started_at": "2030-01-01T00:02:00Z",
        "ended_at": "2030-01-01T00:03:00Z",
        "destroyed_at": "2030-01-01T00:04:00Z",
        "outcome": "passed",
        "exit_code": 0,
        "outputs": [
            {"path": _OUTPUT_PATH, "sha256": output_digest, "size_bytes": len(output_bytes)},
        ],
    }
    signed_bytes = canonical_json_bytes(
        {"schema_version": 1, "key_id": "controller", "payload": receipt_payload}
    )
    payload_path = tmp_path / "receipt-payload.json"
    signature_path = tmp_path / "receipt-signature.bin"
    payload_path.write_bytes(signed_bytes)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(payload_path),
            "-out",
            str(signature_path),
        ],
        check=True,
        capture_output=True,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "key_id": "controller",
                "payload": receipt_payload,
                "signature": base64.b64encode(signature_path.read_bytes()).decode("ascii"),
            }
        )
    )
    stage = tmp_path / "staged"
    output_path = stage / _OUTPUT_PATH
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(output_bytes)

    publisher = LocalExecutionPublisher(
        ledger,
        tmp_path / "outbox.sqlite3",
        tmp_path / "evidence-store",
        keys,
    )
    mismatched_payload = dict(receipt_payload)
    mismatched_payload["observed_provider"] = {
        **receipt_payload["observed_provider"],
        "run_id": "different-provider-run",
    }
    mismatched_signed_bytes = canonical_json_bytes(
        {"schema_version": 1, "key_id": "controller", "payload": mismatched_payload}
    )
    mismatched_payload_path = tmp_path / "mismatched-receipt-payload.json"
    mismatched_signature_path = tmp_path / "mismatched-receipt-signature.bin"
    mismatched_payload_path.write_bytes(mismatched_signed_bytes)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(mismatched_payload_path),
            "-out",
            str(mismatched_signature_path),
        ],
        check=True,
        capture_output=True,
    )
    mismatched_receipt_path = tmp_path / "mismatched-receipt.json"
    mismatched_receipt_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "key_id": "controller",
                "payload": mismatched_payload,
                "signature": base64.b64encode(mismatched_signature_path.read_bytes()).decode(
                    "ascii"
                ),
            }
        )
    )
    with pytest.raises(PublicationError, match="receipt verification failed"):
        publisher.prepare(
            admission.admission_id,
            mismatched_receipt_path,
            stage,
            lambda: "2030-01-01T00:05:00Z",
        )

    mismatched_attempt_payload = dict(receipt_payload)
    mismatched_attempt_payload["observed_provider"] = {
        **receipt_payload["observed_provider"],
        "attempt": facts.attempt + 1,
    }
    mismatched_attempt_signed_bytes = canonical_json_bytes(
        {"schema_version": 1, "key_id": "controller", "payload": mismatched_attempt_payload}
    )
    mismatched_attempt_payload_path = tmp_path / "mismatched-attempt-receipt-payload.json"
    mismatched_attempt_signature_path = tmp_path / "mismatched-attempt-receipt-signature.bin"
    mismatched_attempt_payload_path.write_bytes(mismatched_attempt_signed_bytes)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(mismatched_attempt_payload_path),
            "-out",
            str(mismatched_attempt_signature_path),
        ],
        check=True,
        capture_output=True,
    )
    mismatched_attempt_receipt_path = tmp_path / "mismatched-attempt-receipt.json"
    mismatched_attempt_receipt_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "key_id": "controller",
                "payload": mismatched_attempt_payload,
                "signature": base64.b64encode(
                    mismatched_attempt_signature_path.read_bytes()
                ).decode("ascii"),
            }
        )
    )
    with pytest.raises(PublicationError, match="receipt verification failed"):
        publisher.prepare(
            admission.admission_id,
            mismatched_attempt_receipt_path,
            stage,
            lambda: "2030-01-01T00:05:00Z",
        )
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0

    mismatched_executor_payload = dict(receipt_payload)
    mismatched_executor_payload["executor"] = {
        **receipt_payload["executor"],
        "executor_id": "different-claimed-executor",
    }
    mismatched_executor_signed_bytes = canonical_json_bytes(
        {
            "schema_version": 1,
            "key_id": "controller",
            "payload": mismatched_executor_payload,
        }
    )
    mismatched_executor_payload_path = tmp_path / "mismatched-executor-payload.json"
    mismatched_executor_signature_path = tmp_path / "mismatched-executor-signature.bin"
    mismatched_executor_payload_path.write_bytes(mismatched_executor_signed_bytes)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(mismatched_executor_payload_path),
            "-out",
            str(mismatched_executor_signature_path),
        ],
        check=True,
        capture_output=True,
    )
    mismatched_executor_receipt_path = tmp_path / "mismatched-executor-receipt.json"
    mismatched_executor_receipt_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "key_id": "controller",
                "payload": mismatched_executor_payload,
                "signature": base64.b64encode(
                    mismatched_executor_signature_path.read_bytes()
                ).decode("ascii"),
            }
        )
    )
    with pytest.raises(
        PublicationError, match="receipt executor does not match the persisted claim"
    ):
        publisher.prepare(
            admission.admission_id,
            mismatched_executor_receipt_path,
            stage,
            lambda: "2030-01-01T00:05:00Z",
        )

    publication_id = publisher.prepare(
        admission.admission_id,
        receipt_path,
        stage,
        lambda: "2030-01-01T00:05:00Z",
    )
    bundle_path = publisher.commit(publication_id)
    assert bundle_path.is_file()
    snapshot = publisher.read_committed(publication_id)
    stored_payload = json.loads(snapshot.receipt_bytes)["payload"]
    assert snapshot.publication_id == publication_id
    assert snapshot.admission == admission
    assert snapshot.claim == claim
    assert stored_payload["admission_id"] == admission.admission_id
    assert stored_payload["repository"] == admission.repository
    assert stored_payload["source_sha"] == admission.source_sha
    assert stored_payload["workflow_digest"] == admission.workflow_digest
    assert stored_payload["runtime_id"] == admission.runtime_id
    assert stored_payload["observed_provider"] == {
        "provider_id": admission.provider_id,
        "run_id": admission.provider_run_id,
        "attempt": admission.provider_attempt,
    }
    assert stored_payload["workload_id"] == admission.workload_id
    assert stored_payload["workload_digest"] == admission.workload_digest
    assert stored_payload["material_digest"] == admission.material_digest
    assert stored_payload["policy_id"] == admission.policy_id
    assert stored_payload["policy_digest"] == admission.policy_digest
    assert stored_payload["environment_id"] == admission.environment_id
    assert stored_payload["admission_digest"] == admission_digest(admission)
    assert stored_payload["observed_resource_policy_digest"] == _sha256_mapping(
        admission.resource_policy.to_mapping()
    )
    assert stored_payload["observed_network_policy_digest"] == _sha256_mapping(
        admission.network_policy.to_mapping()
    )
    assert stored_payload["executor"]["executor_id"] == claim.executor_id
    assert stored_payload["executor"]["image_digest"] == admission.image_digest
    assert stored_payload["outputs"] == [
        {"path": _OUTPUT_PATH, "sha256": output_digest, "size_bytes": len(output_bytes)},
    ]
    assert stored_payload["outcome"] == "passed"
    assert len(snapshot.outputs) == 1
    assert snapshot.outputs[0].path == _OUTPUT_PATH
    assert snapshot.outputs[0].sha256 == output_digest
    assert snapshot.outputs[0].content == output_bytes

    mapping = FixedWorkloadMapping(
        workload_id=admission.workload_id,
        workload_digest=admission.workload_digest,
        policy_id=admission.policy_id,
        policy_digest=admission.policy_digest,
        material_id=admission.material_id,
        material_digest=admission.material_digest,
        environment_id=admission.environment_id,
        target_environment=trusted.profile["target_environments"][0],
        runtime_id=admission.runtime_id,
        required_outputs=tuple(
            OutputEvidenceRequirement(
                output_path=output.output_path,
                gate=output.gate,
                component_id=output.component_id,
                artifact_sha256=output.artifact_sha256,
            )
            for output in planned_workload.required_outputs
        ),
    )
    evidence: dict[str, Any] = {
        "candidate_id": admission.candidate_id,
        "profile_id": admission.profile_id,
        "source_commit": admission.source_sha,
        "component_id": _COMPONENT_ID,
        "artifact_sha256": output_digest,
        "gate": _GATE,
        "environment": _ENVIRONMENT_ID,
        "runtime_id": admission.runtime_id,
        "run_id": admission.provider_run_id,
        "attempt": admission.provider_attempt,
        "outcome": "passed",
    }
    evidence["evidence_sha256"] = _sha256_mapping(evidence)
    report = verify_candidate_evidence_publication(
        publisher,
        publication_id,
        trusted.candidate,
        trusted.profile,
        [evidence],
        mapping,
    )
    assert report.bindings_valid is True
    assert report.receipt_signature_verified_under_configured_key is True
    assert report.outcome == stored_payload["outcome"]
    assert report.admission_id == admission.admission_id
    assert report.publication_id == publication_id
    assert report.bound_evidence_ids == (evidence["evidence_sha256"],)
    assert report.output_digests == ((_OUTPUT_PATH, output_digest),)
    assert report.external_trust_established is False
    assert report.acceptance_eligible is False

    mismatched_evidence = dict(evidence)
    mismatched_evidence["run_id"] = "different-provider-run"
    mismatched_evidence["evidence_sha256"] = _sha256_mapping(
        {key: value for key, value in mismatched_evidence.items() if key != "evidence_sha256"}
    )
    with pytest.raises(EvidenceBridgeError, match="identity, runtime, or outcome differs"):
        verify_candidate_evidence_publication(
            publisher,
            publication_id,
            trusted.candidate,
            trusted.profile,
            [mismatched_evidence],
            mapping,
        )

    with pytest.raises(AdmissionAlreadyClaimed):
        controller.claim(
            admission_id=issued_admission.admission_id,
            grant=issued_admission.grant,
            credential="synthetic-test-credential",
        )
    assert clock_calls == 2
