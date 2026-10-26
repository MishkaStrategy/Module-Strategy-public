from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from .contracts import ContractModel

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_MANIFEST_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
OpaqueIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
Sha256Digest = Annotated[str, Field(pattern=_MANIFEST_SHA256.pattern)]
GitCommitSha = Annotated[str, Field(pattern=_GIT_COMMIT_SHA_PATTERN)]


class RecoveryOperation(StrEnum):
    BACKUP = "backup"
    VERIFY = "verify"
    RESTORE = "restore"


class RecoveryProfileRequest(ContractModel):
    """Bounded browser envelope; authority and all filesystem data remain server-owned."""

    operation: RecoveryOperation
    request_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    profile_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    package_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN
    )

    @model_validator(mode="after")
    def validate_package_binding(self) -> RecoveryProfileRequest:
        if self.operation is RecoveryOperation.BACKUP and self.package_id is not None:
            raise ValueError("backup package identity is assigned by the recovery owner")
        if self.operation is not RecoveryOperation.BACKUP and self.package_id is None:
            raise ValueError("verify and restore require an opaque package identity")
        return self


class _RecoveryIntentModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RecoveryTargetReference(_RecoveryIntentModel):
    """Typed target identity; it contains no host or filesystem location."""

    target_id: OpaqueIdentifier
    target_generation_id: OpaqueIdentifier


class OwnerConsistentCaptureBoundary(_RecoveryIntentModel):
    """Server-resolved capture boundary declaration for an exact owner set."""

    boundary_id: OpaqueIdentifier
    owner_ids: tuple[OpaqueIdentifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_canonical_owner_set(self) -> OwnerConsistentCaptureBoundary:
        _require_canonical_owner_set(self.owner_ids, "capture boundary owner_ids")
        return self


class RecoveryProfileDefinition(_RecoveryIntentModel):
    """Immutable catalog bindings; references do not prove their source is trusted."""

    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    candidate_id: OpaqueIdentifier
    source_commit: GitCommitSha
    platform_release_id: OpaqueIdentifier
    owner_ids: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    recovery_target: RecoveryTargetReference
    backup_target: RecoveryTargetReference | None = None

    @model_validator(mode="after")
    def require_canonical_owners(self) -> RecoveryProfileDefinition:
        _require_canonical_owner_set(self.owner_ids, "profile owner_ids")
        return self


class RecoveryAuthorizationGrant(_RecoveryIntentModel):
    """Recovery-specific authorization result from an injected policy port."""

    actor_id: OpaqueIdentifier
    request_envelope_sha256: Sha256Digest
    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    operation: RecoveryOperation
    allowed: bool
    evidence_ref: OpaqueIdentifier


class OwnerReadinessSnapshot(_RecoveryIntentModel):
    """Read-only owner readiness and capture boundary resolution."""

    state: Literal["READY", "STALE", "DENIED"]
    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    candidate_id: OpaqueIdentifier
    source_commit: GitCommitSha
    platform_release_id: OpaqueIdentifier
    owner_ids: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    capture_boundary: OwnerConsistentCaptureBoundary

    @model_validator(mode="after")
    def require_canonical_owners(self) -> OwnerReadinessSnapshot:
        _require_canonical_owner_set(self.owner_ids, "readiness owner_ids")
        return self


class BackupPackageProposal(_RecoveryIntentModel):
    """Read-only proposed package identity and destination; this creates nothing."""

    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    candidate_id: OpaqueIdentifier
    source_commit: GitCommitSha
    platform_release_id: OpaqueIdentifier
    package_id: OpaqueIdentifier
    destination: RecoveryTargetReference


class ExistingRecoveryPackageEvidence(_RecoveryIntentModel):
    """Trusted port result for a package that already exists."""

    state: Literal["TRUSTED", "STALE", "DENIED"]
    package_id: OpaqueIdentifier
    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    candidate_id: OpaqueIdentifier
    source_commit: GitCommitSha
    platform_release_id: OpaqueIdentifier
    owner_ids: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    capture_boundary: OwnerConsistentCaptureBoundary
    expected_manifest_sha256: Sha256Digest
    manifest_evidence_ref: OpaqueIdentifier
    target: RecoveryTargetReference

    @model_validator(mode="after")
    def require_canonical_owners(self) -> ExistingRecoveryPackageEvidence:
        _require_canonical_owner_set(self.owner_ids, "package evidence owner_ids")
        return self


class RecoveryProfileCatalog(Protocol):
    def get_profile(self, profile_id: str) -> RecoveryProfileDefinition | None: ...


class RecoveryAuthorizationPolicy(Protocol):
    def authorize(
        self,
        actor_id: str,
        request: RecoveryProfileRequest,
    ) -> RecoveryAuthorizationGrant | None: ...


class OwnerReadinessSource(Protocol):
    def resolve(self, profile: RecoveryProfileDefinition) -> OwnerReadinessSnapshot | None: ...


class BackupPackageProposalSource(Protocol):
    def propose(
        self,
        profile: RecoveryProfileDefinition,
        request: RecoveryProfileRequest,
    ) -> BackupPackageProposal | None: ...


class ExistingPackageEvidenceSource(Protocol):
    def get_evidence(
        self,
        package_id: str,
        profile: RecoveryProfileDefinition,
        operation: RecoveryOperation,
    ) -> ExistingRecoveryPackageEvidence | None: ...


class _RecoveryIntentBindings(_RecoveryIntentModel):
    """Server-owned bindings shared by immutable operation intents."""

    actor_id: OpaqueIdentifier
    request_envelope_sha256: Sha256Digest
    request_id: OpaqueIdentifier
    profile_id: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    candidate_id: OpaqueIdentifier
    source_commit: GitCommitSha
    platform_release_id: OpaqueIdentifier
    owner_ids: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    capture_boundary: OwnerConsistentCaptureBoundary
    authorization_evidence_ref: OpaqueIdentifier

    @model_validator(mode="after")
    def validate_owner_bindings(self) -> _RecoveryIntentBindings:
        _require_canonical_owner_set(self.owner_ids, "intent owner_ids")
        if self.capture_boundary.owner_ids != self.owner_ids:
            raise ValueError("capture boundary must cover the exact complete intent owner set")
        return self


class RecoveryBackupIntent(_RecoveryIntentBindings):
    """Immutable pre-effect backup bindings; the future package digest is absent by design."""

    operation: Literal["backup"]
    package_id: OpaqueIdentifier
    destination: RecoveryTargetReference


class RecoveryVerifyIntent(_RecoveryIntentBindings):
    """Immutable verification bindings to existing package evidence and target generation."""

    operation: Literal["verify"]
    package_id: OpaqueIdentifier
    expected_manifest_sha256: Sha256Digest
    manifest_evidence_ref: OpaqueIdentifier
    target: RecoveryTargetReference


class RecoveryRestoreIntent(_RecoveryIntentBindings):
    """Immutable restore bindings to existing package evidence and target generation."""

    operation: Literal["restore"]
    package_id: OpaqueIdentifier
    expected_manifest_sha256: Sha256Digest
    manifest_evidence_ref: OpaqueIdentifier
    target: RecoveryTargetReference


RecoveryProfileIntent = RecoveryBackupIntent | RecoveryVerifyIntent | RecoveryRestoreIntent


def _require_canonical_owner_set(owner_ids: tuple[str, ...], field_name: str) -> None:
    if not owner_ids:
        raise ValueError(f"{field_name} must be non-empty")
    if len(set(owner_ids)) != len(owner_ids):
        raise ValueError(f"{field_name} must not contain duplicates")
    if tuple(sorted(owner_ids)) != owner_ids:
        raise ValueError(f"{field_name} must be in canonical sorted order")


class RecoveryProfileCapability(ContractModel):
    """Configuration facts; resolution readiness is separate from effect enablement."""

    status: Literal["NOT_CONFIGURED", "RESOLUTION_ONLY"] = "NOT_CONFIGURED"
    resolution_enabled: bool = False
    profile_catalog_configured: bool = False
    owner_readiness_source_configured: bool = False
    recovery_authorization_policy_configured: bool = False
    backup_package_proposal_source_configured: bool = False
    existing_package_evidence_source_configured: bool = False
    durable_intent_store_configured: Literal[False] = False
    recovery_effects_enabled: Literal[False] = False
    execution_admission_enabled: Literal[False] = False


class RecoveryResolutionSummary(ContractModel):
    """Sanitized resolution receipt. It exposes no intent bindings or effects."""

    status: Literal["RESOLUTION_ONLY"] = "RESOLUTION_ONLY"
    operation: RecoveryOperation
    fingerprint: str = Field(pattern=_MANIFEST_SHA256.pattern)


class RecoveryResolutionUnavailable(Exception):
    """An injected source is missing, invalid, denied, stale, or inconsistent."""


class RecoveryProfileService(Protocol):
    def capability(self) -> RecoveryProfileCapability: ...

    def resolve(
        self,
        request: RecoveryProfileRequest,
        *,
        actor_id: str,
    ) -> RecoveryResolutionSummary: ...


class NamedRecoveryProfileService:
    """Resolve immutable intents from explicit read-only ports, without execution or storage."""

    def __init__(
        self,
        *,
        profile_catalog: RecoveryProfileCatalog,
        authorization_policy: RecoveryAuthorizationPolicy,
        owner_readiness: OwnerReadinessSource,
        backup_proposals: BackupPackageProposalSource,
        package_evidence: ExistingPackageEvidenceSource,
    ) -> None:
        self._profile_catalog = profile_catalog
        self._authorization_policy = authorization_policy
        self._owner_readiness = owner_readiness
        self._backup_proposals = backup_proposals
        self._package_evidence = package_evidence

    def capability(self) -> RecoveryProfileCapability:
        return RecoveryProfileCapability(
            status="RESOLUTION_ONLY",
            resolution_enabled=True,
            profile_catalog_configured=True,
            owner_readiness_source_configured=True,
            recovery_authorization_policy_configured=True,
            backup_package_proposal_source_configured=True,
            existing_package_evidence_source_configured=True,
        )

    def resolve(
        self,
        request: RecoveryProfileRequest,
        *,
        actor_id: str,
    ) -> RecoveryResolutionSummary:
        try:
            expected_request_envelope_sha256 = request_envelope_fingerprint(request)
            grant = self._authorization_policy.authorize(actor_id, request)
            if grant is None or not grant.allowed:
                raise RecoveryResolutionUnavailable
            if (
                grant.actor_id != actor_id
                or grant.request_envelope_sha256 != expected_request_envelope_sha256
                or grant.profile_id != request.profile_id
                or grant.operation is not request.operation
            ):
                raise RecoveryResolutionUnavailable

            profile = self._profile_catalog.get_profile(request.profile_id)
            if profile is None or profile.profile_id != request.profile_id:
                raise RecoveryResolutionUnavailable
            if grant.profile_version != profile.profile_version:
                raise RecoveryResolutionUnavailable

            readiness = self._owner_readiness.resolve(profile)
            if readiness is None or readiness.state != "READY":
                raise RecoveryResolutionUnavailable
            _require_profile_binding(profile, readiness)

            if request.operation is RecoveryOperation.BACKUP:
                if profile.backup_target is None:
                    raise RecoveryResolutionUnavailable
                proposal = self._backup_proposals.propose(profile, request)
                if proposal is None:
                    raise RecoveryResolutionUnavailable
                _require_proposal_binding(profile, proposal)
                intent: RecoveryProfileIntent = RecoveryBackupIntent(
                    operation="backup",
                    actor_id=grant.actor_id,
                    request_envelope_sha256=grant.request_envelope_sha256,
                    request_id=request.request_id,
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    candidate_id=profile.candidate_id,
                    source_commit=profile.source_commit,
                    platform_release_id=profile.platform_release_id,
                    owner_ids=profile.owner_ids,
                    capture_boundary=readiness.capture_boundary,
                    authorization_evidence_ref=grant.evidence_ref,
                    package_id=proposal.package_id,
                    destination=proposal.destination,
                )
            else:
                if request.package_id is None:
                    raise RecoveryResolutionUnavailable
                evidence = self._package_evidence.get_evidence(
                    request.package_id, profile, request.operation
                )
                if evidence is None:
                    raise RecoveryResolutionUnavailable
                if evidence.state != "TRUSTED":
                    raise RecoveryResolutionUnavailable
                _require_package_binding(profile, readiness, request.package_id, evidence)
                if request.operation is RecoveryOperation.VERIFY:
                    intent = RecoveryVerifyIntent(
                        operation="verify",
                        actor_id=grant.actor_id,
                        request_envelope_sha256=grant.request_envelope_sha256,
                        request_id=request.request_id,
                        profile_id=profile.profile_id,
                        profile_version=profile.profile_version,
                        candidate_id=profile.candidate_id,
                        source_commit=profile.source_commit,
                        platform_release_id=profile.platform_release_id,
                        owner_ids=profile.owner_ids,
                        capture_boundary=readiness.capture_boundary,
                        authorization_evidence_ref=grant.evidence_ref,
                        package_id=evidence.package_id,
                        expected_manifest_sha256=evidence.expected_manifest_sha256,
                        manifest_evidence_ref=evidence.manifest_evidence_ref,
                        target=evidence.target,
                    )
                else:
                    intent = RecoveryRestoreIntent(
                        operation="restore",
                        actor_id=grant.actor_id,
                        request_envelope_sha256=grant.request_envelope_sha256,
                        request_id=request.request_id,
                        profile_id=profile.profile_id,
                        profile_version=profile.profile_version,
                        candidate_id=profile.candidate_id,
                        source_commit=profile.source_commit,
                        platform_release_id=profile.platform_release_id,
                        owner_ids=profile.owner_ids,
                        capture_boundary=readiness.capture_boundary,
                        authorization_evidence_ref=grant.evidence_ref,
                        package_id=evidence.package_id,
                        expected_manifest_sha256=evidence.expected_manifest_sha256,
                        manifest_evidence_ref=evidence.manifest_evidence_ref,
                        target=evidence.target,
                    )
            return RecoveryResolutionSummary(
                operation=request.operation,
                fingerprint=recovery_intent_fingerprint(intent),
            )
        except RecoveryResolutionUnavailable:
            raise
        except Exception as exc:
            raise RecoveryResolutionUnavailable from exc


class UnconfiguredRecoveryProfileService:
    """Production default: no read ports, resolution, persistence, or recovery effects."""

    def capability(self) -> RecoveryProfileCapability:
        return RecoveryProfileCapability()

    def resolve(
        self,
        request: RecoveryProfileRequest,
        *,
        actor_id: str,
    ) -> RecoveryResolutionSummary:
        del request, actor_id
        raise RecoveryResolutionUnavailable


def _require_profile_binding(
    profile: RecoveryProfileDefinition,
    readiness: OwnerReadinessSnapshot,
) -> None:
    if (
        readiness.profile_id != profile.profile_id
        or readiness.profile_version != profile.profile_version
        or readiness.candidate_id != profile.candidate_id
        or readiness.source_commit != profile.source_commit
        or readiness.platform_release_id != profile.platform_release_id
        or readiness.owner_ids != profile.owner_ids
        or readiness.capture_boundary.owner_ids != profile.owner_ids
    ):
        raise RecoveryResolutionUnavailable


def _require_proposal_binding(
    profile: RecoveryProfileDefinition,
    proposal: BackupPackageProposal,
) -> None:
    if (
        proposal.profile_id != profile.profile_id
        or proposal.profile_version != profile.profile_version
        or proposal.candidate_id != profile.candidate_id
        or proposal.source_commit != profile.source_commit
        or proposal.platform_release_id != profile.platform_release_id
        or profile.backup_target is None
        or proposal.destination != profile.backup_target
    ):
        raise RecoveryResolutionUnavailable


def _require_package_binding(
    profile: RecoveryProfileDefinition,
    readiness: OwnerReadinessSnapshot,
    requested_package_id: str,
    evidence: ExistingRecoveryPackageEvidence,
) -> None:
    if (
        evidence.package_id != requested_package_id
        or evidence.profile_id != profile.profile_id
        or evidence.profile_version != profile.profile_version
        or evidence.candidate_id != profile.candidate_id
        or evidence.source_commit != profile.source_commit
        or evidence.platform_release_id != profile.platform_release_id
        or evidence.owner_ids != profile.owner_ids
        or evidence.owner_ids != readiness.owner_ids
        or evidence.capture_boundary != readiness.capture_boundary
        or evidence.target != profile.recovery_target
    ):
        raise RecoveryResolutionUnavailable


def request_envelope_fingerprint(request: RecoveryProfileRequest) -> str:
    """Return deterministic content identity, not replay protection or idempotency."""

    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def recovery_intent_fingerprint(intent: RecoveryProfileIntent) -> str:
    """Return deterministic content identity, not durable replay protection or idempotency."""

    payload = intent.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def manifest_digest_matches(expected: str, observed: str) -> bool:
    """Compare digest syntax and value only; this does not establish source trust."""

    if _MANIFEST_SHA256.fullmatch(expected) is None:
        return False
    if _MANIFEST_SHA256.fullmatch(observed) is None:
        return False
    return hmac.compare_digest(expected, observed)
