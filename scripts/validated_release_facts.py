"""Dependency-free typed facts captured by the platform release validator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    schema_version: int
    manifest_digest: str
    candidate_id: str
    profile_id: str
    candidate_digest: str
    profile_digest: str
    source_commit: str


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    schema_digest: str
    component_inventory_digest: str
    compatibility_policy_digest: str
    acceptance_policy_digest: str


@dataclass(frozen=True, slots=True)
class VerifiedReference:
    role: str
    origin: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ComponentFact:
    component_id: str
    kind: str
    version: str
    descriptor_reference: VerifiedReference
    source_references: tuple[VerifiedReference, ...]


@dataclass(frozen=True, slots=True)
class LockRequirementFact:
    component_id: str
    ecosystem: str
    purpose: str
    environment_id: str
    runtime_id: str | None


@dataclass(frozen=True, slots=True)
class LockFact:
    lock_id: str
    ecosystem: str
    purpose: str
    environment_id: str
    runtime_id: str | None
    covered_components: tuple[str, ...]
    reference: VerifiedReference


@dataclass(frozen=True, slots=True)
class AcceptanceRequirementFact:
    gate_id: str
    component_id: str
    environment_id: str


@dataclass(frozen=True, slots=True)
class RuntimeScopeFact:
    component_id: str
    environment_id: str
    runtime_id: str | None
    runtime_status: str


@dataclass(frozen=True, slots=True)
class RelationFact:
    relation_id: str
    component_id: str
    environment_id: str
    runtime_id: str | None
    format_id: str
    disposition: str


@dataclass(frozen=True, slots=True)
class CompatibilityProviderFact:
    component_id: str
    status: str
    inventory_status: str
    format_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompatibilitySubjectFact:
    digest: str
    component_id: str
    environment_id: str
    runtime_id: str | None
    runtime_status: str


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    evidence_sha256: str
    gate_id: str
    component_id: str
    environment_id: str
    runtime_id: str | None
    compatibility_subject_sha256: str | None
    outcome: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class MigrationRollbackFact:
    kind: str
    item_id: str
    evidence_required: bool


@dataclass(frozen=True, slots=True)
class ReconstructionFact:
    lock_id: str
    runtime_id: str
    environment_id: str
    outcome: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class BuildContextFact:
    context_sha256: str
    component_id: str
    runtime_id: str
    environment_id: str
    bootstrap_lock_id: str
    bootstrap_lock_sha256: str
    bootstrap_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class HookFact:
    attestation_sha256: str
    context_sha256: str
    operation: str
    status: str
    final_lock_id: str | None
    final_lock_sha256: str | None
    reconstruction_receipt_sha256: str | None


@dataclass(frozen=True, slots=True)
class BuildFact:
    attestation_sha256: str
    context_sha256: str
    hook_attestation_sha256: str
    runtime_id: str
    environment_id: str
    operation: str
    final_lock_id: str
    final_lock_sha256: str
    reconstruction_receipt_sha256: str
    outcome: str
    artifact_sha256: str | None


@dataclass(frozen=True, slots=True)
class ArtifactFact:
    component_id: str
    kind: str
    reference: VerifiedReference


@dataclass(frozen=True, slots=True)
class WebArtifactMemberFact:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class WebConsoleFact:
    component_id: str
    npm_lock_id: str
    source_inputs: tuple[VerifiedReference, ...]
    npm_lock_reference: VerifiedReference
    artifact_reference: VerifiedReference
    artifact_members: tuple[WebArtifactMemberFact, ...]
    member_manifest_sha256: str
    pending_bindings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatedReleaseFacts:
    candidate: CandidateFacts
    policies: PolicyFacts
    components: tuple[ComponentFact, ...]
    verified_references: tuple[VerifiedReference, ...]
    lock_requirements: tuple[LockRequirementFact, ...]
    locks: tuple[LockFact, ...]
    acceptance_requirements: tuple[AcceptanceRequirementFact, ...]
    compatibility_scopes: tuple[RuntimeScopeFact, ...]
    compatibility_providers: tuple[CompatibilityProviderFact, ...]
    compatibility_relations: tuple[RelationFact, ...]
    compatibility_subjects: tuple[CompatibilitySubjectFact, ...]
    compatibility_evidence: tuple[str, ...]
    evidence: tuple[EvidenceFact, ...]
    migrations_rollbacks: tuple[MigrationRollbackFact, ...]
    reconstructions: tuple[ReconstructionFact, ...]
    build_contexts: tuple[BuildContextFact, ...]
    hooks: tuple[HookFact, ...]
    builds: tuple[BuildFact, ...]
    artifacts: tuple[ArtifactFact, ...]
    web_console: WebConsoleFact | None = None


class ReferenceCollector:
    """Capture verified references at their existing successful checks."""

    __slots__ = ("_references",)

    def __init__(self) -> None:
        self._references: list[VerifiedReference] = []

    def capture(self, role: str, reference: dict[str, str]) -> VerifiedReference:
        fact = VerifiedReference(
            role=role,
            origin=reference["origin"],
            path=reference["path"],
            sha256=reference["sha256"],
        )
        self._references.append(fact)
        return fact

    def freeze(self) -> tuple[VerifiedReference, ...]:
        return tuple(self._references)
