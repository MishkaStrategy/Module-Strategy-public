"""Read-only binding checks between local committed outputs and candidate evidence.

The report produced here is local integration proof only. A valid signature under
the publisher's configured key does not authenticate provider observations,
controller key custody, executor isolation, or a release trust anchor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from scripts.external_execution_publisher import (
    CommittedPublicationSnapshot,
    LocalExecutionPublisher,
    PublicationError,
    _expected,
)
from scripts.platform_release_manifest import (
    ManifestError,
    canonical_json_bytes,
    object_identity,
)
from scripts.verify_external_execution_receipt import ReceiptError, verify_receipt_bytes

_SHA256_LENGTH = 64


class EvidenceBridgeError(ValueError):
    """Candidate evidence does not match the committed local publication."""


@dataclass(frozen=True)
class OutputEvidenceRequirement:
    output_path: str
    gate: str
    component_id: str
    artifact_sha256: str


@dataclass(frozen=True)
class FixedWorkloadMapping:
    """Locally configured workload and exact output-to-evidence coverage."""

    workload_id: str
    workload_digest: str
    policy_id: str
    policy_digest: str
    material_id: str
    material_digest: str
    environment_id: str
    target_environment: str
    runtime_id: str
    required_outputs: tuple[OutputEvidenceRequirement, ...]


@dataclass(frozen=True)
class EvidenceBindingReport:
    publication_id: str
    admission_id: str
    candidate_id: str
    profile_id: str
    source_sha: str
    outcome: str
    bound_evidence_ids: tuple[str, ...]
    output_digests: tuple[tuple[str, str], ...]
    bindings_valid: bool
    receipt_signature_verified_under_configured_key: bool
    external_trust_established: bool
    acceptance_eligible: bool


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _frozen_digest(value: Mapping[str, Any], label: str) -> str:
    try:
        return _sha256(canonical_json_bytes(dict(value)))
    except ManifestError as exc:
        raise EvidenceBridgeError(f"{label} cannot be canonically encoded: {exc}") from exc


def _snapshot_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Canonicalize once, then use only a private decoded copy for all checks."""
    try:
        raw = canonical_json_bytes(dict(value))
        snapshot = json.loads(raw.decode("utf-8"))
    except (ManifestError, TypeError, ValueError, UnicodeError) as exc:
        raise EvidenceBridgeError(f"{label} cannot be snapshotted: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise EvidenceBridgeError(f"{label} snapshot must be an object")
    return snapshot


def _identity(value: Mapping[str, Any], field: str, label: str) -> str:
    try:
        result = object_identity(dict(value), field)
    except ManifestError as exc:
        raise EvidenceBridgeError(f"{label} identity is invalid: {exc}") from exc
    if len(result) != _SHA256_LENGTH:
        raise EvidenceBridgeError(f"{label} identity is invalid")
    return result


def _check_mapping(mapping: FixedWorkloadMapping) -> None:
    if type(mapping) is not FixedWorkloadMapping:
        raise EvidenceBridgeError("a fixed workload/output mapping is required")
    for name in (
        "workload_id",
        "policy_id",
        "material_id",
        "environment_id",
        "target_environment",
        "runtime_id",
    ):
        value = getattr(mapping, name)
        if not isinstance(value, str) or not value:
            raise EvidenceBridgeError(f"fixed workload mapping {name} is invalid")
    for name in ("workload_digest", "policy_digest", "material_digest"):
        value = getattr(mapping, name)
        if (
            not isinstance(value, str)
            or len(value) != _SHA256_LENGTH
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise EvidenceBridgeError(f"fixed workload mapping {name} is invalid")
    if not isinstance(mapping.required_outputs, tuple) or not mapping.required_outputs:
        raise EvidenceBridgeError("fixed workload mapping must require at least one output")
    if any(type(row) is not OutputEvidenceRequirement for row in mapping.required_outputs):
        raise EvidenceBridgeError("fixed workload mapping contains an invalid output requirement")
    paths = [row.output_path for row in mapping.required_outputs]
    if len(paths) != len(set(paths)):
        raise EvidenceBridgeError("fixed workload mapping contains duplicate output paths")
    keys = [(row.gate, row.component_id, row.artifact_sha256) for row in mapping.required_outputs]
    if len(keys) != len(set(keys)):
        raise EvidenceBridgeError("fixed workload mapping contains duplicate evidence requirements")
    for row in mapping.required_outputs:
        if not all(
            isinstance(value, str) and value
            for value in (row.output_path, row.gate, row.component_id)
        ):
            raise EvidenceBridgeError(
                "fixed workload mapping contains an incomplete output requirement"
            )
        if (
            not isinstance(row.artifact_sha256, str)
            or len(row.artifact_sha256) != _SHA256_LENGTH
            or any(c not in "0123456789abcdef" for c in row.artifact_sha256)
        ):
            raise EvidenceBridgeError("fixed workload mapping contains an invalid artifact digest")


def verify_candidate_evidence_publication(
    publisher: LocalExecutionPublisher,
    publication_id: str,
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    workload: FixedWorkloadMapping,
    *,
    manifest_schema_version: int = 2,
) -> EvidenceBindingReport:
    """Verify local byte and identity bindings without granting release authority.

    ``workload`` must come from the caller's fixed local policy inventory; this
    function does not authenticate that inventory. Candidate evidence rows and
    the output-to-evidence mapping are treated as claims to check.
    """
    if type(workload) is not FixedWorkloadMapping:
        raise EvidenceBridgeError("a fixed workload/output mapping is required")
    if type(manifest_schema_version) is not int or manifest_schema_version not in (2, 3):
        raise EvidenceBridgeError("manifest schema version must be 2 or 3")
    try:
        required_outputs = tuple(workload.required_outputs)
        if any(type(row) is not OutputEvidenceRequirement for row in required_outputs):
            raise EvidenceBridgeError(
                "fixed workload mapping contains an invalid output requirement"
            )
        workload = FixedWorkloadMapping(
            workload_id=workload.workload_id,
            workload_digest=workload.workload_digest,
            policy_id=workload.policy_id,
            policy_digest=workload.policy_digest,
            material_id=workload.material_id,
            material_digest=workload.material_digest,
            environment_id=workload.environment_id,
            target_environment=workload.target_environment,
            runtime_id=workload.runtime_id,
            required_outputs=tuple(
                OutputEvidenceRequirement(
                    output_path=row.output_path,
                    gate=row.gate,
                    component_id=row.component_id,
                    artifact_sha256=row.artifact_sha256,
                )
                for row in required_outputs
            ),
        )
    except EvidenceBridgeError:
        raise
    except (AttributeError, TypeError) as exc:
        raise EvidenceBridgeError("fixed workload mapping cannot be snapshotted") from exc
    _check_mapping(workload)
    if not isinstance(candidate, Mapping) or not isinstance(profile, Mapping):
        raise EvidenceBridgeError("candidate and profile must be mappings")
    if not isinstance(evidence_rows, Sequence) or isinstance(evidence_rows, (str, bytes)):
        raise EvidenceBridgeError("evidence_rows must be a sequence of evidence mappings")
    evidence_input = tuple(evidence_rows)
    if any(not isinstance(row, Mapping) for row in evidence_input):
        raise EvidenceBridgeError("every evidence row must be a mapping")
    candidate = _snapshot_mapping(candidate, "candidate")
    profile = _snapshot_mapping(profile, "profile")
    evidence_rows = tuple(
        _snapshot_mapping(row, f"evidence_rows[{index}]")
        for index, row in enumerate(evidence_input)
    )
    try:
        snapshot: CommittedPublicationSnapshot = publisher.read_committed(publication_id)
    except PublicationError as exc:
        raise EvidenceBridgeError(f"committed publication verification failed: {exc}") from exc

    admission = snapshot.admission
    try:
        receipt = verify_receipt_bytes(
            snapshot.receipt_bytes,
            publisher.trusted_controller_keys,
            publisher.openssl,
            _expected(admission),
            list(admission.required_output_paths),
        )
    except ReceiptError as exc:
        raise EvidenceBridgeError(
            f"receipt signature or contract verification failed: {exc}"
        ) from exc
    if receipt["outcome"] != "passed" or receipt["exit_code"] != 0:
        raise EvidenceBridgeError("non-passing receipt cannot bind release evidence")

    candidate_id = _identity(candidate, "candidate_id", "candidate")
    profile_id = _identity(profile, "profile_id", "profile")
    source = candidate.get("source")
    if not isinstance(source, Mapping) or not isinstance(source.get("commit"), str):
        raise EvidenceBridgeError("candidate source commit is missing")
    source_sha = source["commit"]
    if (
        candidate.get("candidate_id") != candidate_id
        or candidate_id != admission.candidate_id
        or _frozen_digest(candidate, "candidate") != admission.candidate_digest
        or profile.get("profile_id") != profile_id
        or profile_id != admission.profile_id
        or _frozen_digest(profile, "profile") != admission.profile_digest
        or source_sha != admission.source_sha
    ):
        raise EvidenceBridgeError(
            "candidate/profile/source identity differs from immutable admission"
        )

    expected_admission_fields = (
        ("workload_id", admission.workload_id),
        ("workload_digest", admission.workload_digest),
        ("policy_id", admission.policy_id),
        ("policy_digest", admission.policy_digest),
        ("material_id", admission.material_id),
        ("material_digest", admission.material_digest),
        ("environment_id", admission.environment_id),
        ("runtime_id", admission.runtime_id),
    )
    if any(getattr(workload, field) != value for field, value in expected_admission_fields):
        raise EvidenceBridgeError("fixed workload mapping differs from immutable admission")
    target_environments = profile.get("target_environments")
    runtimes = profile.get("python_runtimes")
    if manifest_schema_version == 3:
        acceptance_requirements = profile.get("acceptance_requirements")
        if not isinstance(acceptance_requirements, list):
            raise EvidenceBridgeError("v3 profile acceptance requirements are malformed")
        acceptance_tuples: set[tuple[str, str, str]] = set()
        for row in acceptance_requirements:
            if (
                not isinstance(row, Mapping)
                or set(row) != {"gate", "component_id", "environment"}
                or any(not isinstance(row.get(field), str) or not row[field] for field in row)
            ):
                raise EvidenceBridgeError("v3 profile acceptance requirement is malformed")
            acceptance_tuples.add((row["gate"], row["component_id"], row["environment"]))
        for requirement in workload.required_outputs:
            if (
                requirement.gate,
                requirement.component_id,
                workload.target_environment,
            ) not in acceptance_tuples:
                raise EvidenceBridgeError(
                    "fixed workload output does not match a declared v3 acceptance requirement"
                )
    if (
        not isinstance(target_environments, list)
        or (manifest_schema_version == 2 and workload.target_environment not in target_environments)
        or not isinstance(runtimes, list)
        or workload.runtime_id
        not in {row.get("id") for row in runtimes if isinstance(row, Mapping)}
    ):
        raise EvidenceBridgeError(
            "workload environment/runtime is outside the candidate support profile"
        )

    outputs = {row.path: row for row in snapshot.outputs}
    required_paths = {row.output_path for row in workload.required_outputs}
    if required_paths != set(admission.required_output_paths) or required_paths != set(outputs):
        raise EvidenceBridgeError("workload, admission, and committed output inventories differ")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list):
        raise EvidenceBridgeError("candidate artifact inventory is malformed")
    evidence_by_requirement: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in evidence_rows:
        key = (row.get("gate"), row.get("component_id"), row.get("artifact_sha256"))
        evidence_by_requirement.setdefault(key, []).append(row)

    bound_ids: list[str] = []
    output_digests: list[tuple[str, str]] = []
    for requirement in workload.required_outputs:
        output = outputs[requirement.output_path]
        if output.sha256 != requirement.artifact_sha256:
            raise EvidenceBridgeError(
                "committed output digest differs from required candidate artifact"
            )
        artifact_matches = [
            artifact
            for artifact in artifacts
            if isinstance(artifact, Mapping)
            and artifact.get("component") == requirement.component_id
            and isinstance(artifact.get("file"), Mapping)
            and artifact["file"].get("sha256") == requirement.artifact_sha256
        ]
        if len(artifact_matches) != 1:
            raise EvidenceBridgeError(
                "required evidence artifact is absent or ambiguous in candidate inventory"
            )
        matches = evidence_by_requirement.get(
            (requirement.gate, requirement.component_id, requirement.artifact_sha256), []
        )
        if len(matches) != 1:
            raise EvidenceBridgeError(
                "required candidate evidence is absent, duplicate, or conflicting"
            )
        evidence = matches[0]
        if (
            evidence.get("candidate_id") != candidate_id
            or evidence.get("profile_id") != profile_id
            or evidence.get("source_commit") != source_sha
            or evidence.get("environment") != workload.target_environment
            or evidence.get("run_id") != admission.provider_run_id
            or isinstance(evidence.get("attempt"), bool)
            or evidence.get("attempt") != admission.provider_attempt
            or evidence.get("runtime_id") != workload.runtime_id
            or evidence.get("outcome") != "passed"
        ):
            raise EvidenceBridgeError(
                "candidate evidence identity, runtime, or outcome differs from admitted execution"
            )
        evidence_id = evidence.get("evidence_sha256")
        if not isinstance(evidence_id, str) or evidence_id != _identity(
            evidence, "evidence_sha256", "evidence"
        ):
            raise EvidenceBridgeError("candidate evidence self-identity is invalid")
        if evidence_id in bound_ids:
            raise EvidenceBridgeError("candidate evidence is linked more than once")
        bound_ids.append(evidence_id)
        output_digests.append((requirement.output_path, output.sha256))

    return EvidenceBindingReport(
        publication_id=snapshot.publication_id,
        admission_id=admission.admission_id,
        candidate_id=candidate_id,
        profile_id=profile_id,
        source_sha=source_sha,
        outcome=receipt["outcome"],
        bound_evidence_ids=tuple(sorted(bound_ids)),
        output_digests=tuple(sorted(output_digests)),
        bindings_valid=True,
        receipt_signature_verified_under_configured_key=True,
        external_trust_established=False,
        acceptance_eligible=False,
    )
