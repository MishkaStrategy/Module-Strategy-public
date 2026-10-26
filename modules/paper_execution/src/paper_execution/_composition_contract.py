"""Inert value contracts for a future PAPER composition owner.

This module defines no owner, storage, allocator, or execution behavior. Every
authority and evidence state is supplied explicitly by a future caller.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


CONTRACT_VERSION = "paper-composition-contract-v1"


class EvidenceState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"


class AggregateState(StrEnum):
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"


class ComponentKind(StrEnum):
    CHILD_POSITIONS = "child_positions"
    CHILD_ORDERS = "child_orders"
    CHILD_FILLS = "child_fills"
    SHARED_ACCOUNTING = "shared_accounting"
    RESERVATIONS = "reservations"
    ALLOCATION = "allocation"
    REPLAY = "replay"
    RISK = "risk"


REQUIRED_COMPONENTS = tuple(sorted(ComponentKind, key=lambda item: item.value))


@dataclass(frozen=True, slots=True)
class CompositionScope:
    """Exact immutable identity and stable membership of one composition."""

    composition_id: str
    membership_version: str
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonblank(self.composition_id, "composition_id")
        _nonblank(self.membership_version, "membership_version")
        members = _stable_members(self.members)
        if not members:
            raise ValueError("composition scope must have at least one member")
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class CompositionVersion:
    """Explicit commit and snapshot sequence numbers for an owner generation."""

    state_version: int
    snapshot_version: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.state_version, "state_version")
        _nonnegative_int(self.snapshot_version, "snapshot_version")


@dataclass(frozen=True, slots=True)
class OwnerFence:
    """Expected writer identity and version for a future serialized commit."""

    owner_id: str
    owner_generation: str
    expected_version: CompositionVersion

    def __post_init__(self) -> None:
        _nonblank(self.owner_id, "owner_id")
        _nonblank(self.owner_generation, "owner_generation")
        if not isinstance(self.expected_version, CompositionVersion):
            raise TypeError("expected_version must be CompositionVersion")


@dataclass(frozen=True, slots=True)
class ComponentEvidenceReceipt:
    """Digest reference bound to one component and the exact whole-owner scope."""

    component: ComponentKind
    scope: CompositionScope
    owner_id: str
    owner_generation: str
    version: CompositionVersion
    covered_members: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.component, ComponentKind):
            raise TypeError("component must be ComponentKind")
        if not isinstance(self.scope, CompositionScope):
            raise TypeError("scope must be CompositionScope")
        _nonblank(self.owner_id, "owner_id")
        _nonblank(self.owner_generation, "owner_generation")
        if not isinstance(self.version, CompositionVersion):
            raise TypeError("version must be CompositionVersion")
        members = _stable_members(self.covered_members)
        object.__setattr__(self, "covered_members", members)
        if members != self.scope.members:
            raise ValueError("component receipt must cover the exact stable membership")
        _sha256_digest(self.evidence_digest, "evidence_digest")


@dataclass(frozen=True, slots=True)
class CompositionEvidence:
    """Explicit status and receipt for each required evidence component."""

    child_positions: EvidenceState
    child_orders: EvidenceState
    child_fills: EvidenceState
    shared_accounting: EvidenceState
    reservations: EvidenceState
    allocation: EvidenceState
    replay: EvidenceState
    risk: EvidenceState
    receipts: tuple[ComponentEvidenceReceipt, ...]

    def __post_init__(self) -> None:
        names = tuple(component.value for component in REQUIRED_COMPONENTS)
        for name in names:
            if not isinstance(getattr(self, name), EvidenceState):
                raise TypeError(f"{name} must be EvidenceState")
        if not isinstance(self.receipts, tuple):
            raise TypeError("receipts must be a tuple")
        if any(not isinstance(receipt, ComponentEvidenceReceipt) for receipt in self.receipts):
            raise TypeError("receipts must contain ComponentEvidenceReceipt values")
        components = tuple(receipt.component for receipt in self.receipts)
        if components != tuple(sorted(set(components), key=lambda item: item.value)):
            raise ValueError("component receipts must be unique and canonically ordered")
        receipt_components = set(components)
        for component in REQUIRED_COMPONENTS:
            available = getattr(self, component.value) is EvidenceState.AVAILABLE
            has_receipt = component in receipt_components
            if available != has_receipt:
                raise ValueError(f"{component.value} availability must match its explicit receipt")


@dataclass(frozen=True, slots=True)
class CompositionSnapshot:
    """Immutable owner-bound snapshot metadata and coverage evidence."""

    contract_version: str
    scope: CompositionScope
    owner_id: str
    owner_generation: str
    version: CompositionVersion
    observed_at: datetime
    state: AggregateState
    evidence: CompositionEvidence

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported composition contract version")
        if not isinstance(self.scope, CompositionScope):
            raise TypeError("scope must be CompositionScope")
        _nonblank(self.owner_id, "owner_id")
        _nonblank(self.owner_generation, "owner_generation")
        if not isinstance(self.version, CompositionVersion):
            raise TypeError("version must be CompositionVersion")
        _aware_datetime(self.observed_at, "observed_at")
        if not isinstance(self.state, AggregateState):
            raise TypeError("state must be AggregateState")
        if not isinstance(self.evidence, CompositionEvidence):
            raise TypeError("evidence must be CompositionEvidence")
        _validate_component_bindings(
            self.scope,
            self.owner_id,
            self.owner_generation,
            self.version,
            self.evidence,
        )
        if self.state is AggregateState.COMPLETE:
            _require_complete_evidence(self.scope, self.owner_id, self.evidence)


@dataclass(frozen=True, slots=True)
class CompositionCheckpoint:
    """Immutable lineage for a whole-scope checkpoint, without restore logic."""

    contract_version: str
    scope: CompositionScope
    source_owner_id: str
    source_owner_generation: str
    version: CompositionVersion
    evidence: CompositionEvidence
    manifest_digest: str

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported composition contract version")
        if not isinstance(self.scope, CompositionScope):
            raise TypeError("scope must be CompositionScope")
        _nonblank(self.source_owner_id, "source_owner_id")
        _nonblank(self.source_owner_generation, "source_owner_generation")
        if not isinstance(self.version, CompositionVersion):
            raise TypeError("version must be CompositionVersion")
        if not isinstance(self.evidence, CompositionEvidence):
            raise TypeError("evidence must be CompositionEvidence")
        _validate_component_bindings(
            self.scope,
            self.source_owner_id,
            self.source_owner_generation,
            self.version,
            self.evidence,
        )
        _sha256_digest(self.manifest_digest, "manifest_digest")
        if self.manifest_digest != _checkpoint_manifest_digest(
            self.scope,
            self.source_owner_id,
            self.source_owner_generation,
            self.version,
            self.evidence,
        ):
            raise ValueError("checkpoint manifest digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        scope: CompositionScope,
        source_owner_id: str,
        source_owner_generation: str,
        version: CompositionVersion,
        evidence: CompositionEvidence,
    ) -> CompositionCheckpoint:
        """Create a structurally sealed manifest; this digest is not authentication."""

        return cls(
            contract_version=CONTRACT_VERSION,
            scope=scope,
            source_owner_id=source_owner_id,
            source_owner_generation=source_owner_generation,
            version=version,
            evidence=evidence,
            manifest_digest=_checkpoint_manifest_digest(
                scope,
                source_owner_id,
                source_owner_generation,
                version,
                evidence,
            ),
        )


def validate_snapshot_binding(
    snapshot: CompositionSnapshot,
    *,
    expected_scope: CompositionScope,
    expected_fence: OwnerFence,
) -> None:
    """Reject snapshots that do not match the exact requested scope and fence."""

    if not isinstance(snapshot, CompositionSnapshot):
        raise TypeError("snapshot must be CompositionSnapshot")
    if snapshot.scope != expected_scope:
        raise ValueError("snapshot composition scope or membership is stale")
    if (
        snapshot.owner_id != expected_fence.owner_id
        or snapshot.owner_generation != expected_fence.owner_generation
    ):
        raise ValueError("snapshot owner generation does not match the fence")
    if snapshot.version != expected_fence.expected_version:
        raise ValueError("snapshot version does not match the expected fence version")


def require_complete_snapshot(snapshot: CompositionSnapshot) -> None:
    """Fail closed unless all whole-owner evidence is explicitly available."""

    if not isinstance(snapshot, CompositionSnapshot):
        raise TypeError("snapshot must be CompositionSnapshot")
    if snapshot.state is not AggregateState.COMPLETE:
        raise ValueError("snapshot is not complete")
    _require_complete_evidence(snapshot.scope, snapshot.owner_id, snapshot.evidence)


def require_complete_checkpoint(checkpoint: CompositionCheckpoint) -> None:
    """Fail closed unless a checkpoint explicitly covers the whole owner scope."""

    if not isinstance(checkpoint, CompositionCheckpoint):
        raise TypeError("checkpoint must be CompositionCheckpoint")
    _require_complete_evidence(checkpoint.scope, checkpoint.source_owner_id, checkpoint.evidence)


def validate_checkpoint_bindings(
    checkpoint: CompositionCheckpoint,
    *,
    expected_scope: CompositionScope,
    expected_source_owner_id: str,
    expected_source_owner_generation: str,
    expected_version: CompositionVersion,
) -> CompositionCheckpoint:
    """Return a checkpoint only after full structure and expected bindings verify.

    This is a pure validation/staging boundary. It performs no restore or I/O.
    """

    if not isinstance(checkpoint, CompositionCheckpoint):
        raise TypeError("checkpoint must be CompositionCheckpoint")
    if checkpoint.scope != expected_scope:
        raise ValueError("checkpoint scope or membership is stale")
    if (
        checkpoint.source_owner_id != expected_source_owner_id
        or checkpoint.source_owner_generation != expected_source_owner_generation
    ):
        raise ValueError("checkpoint source owner generation is stale")
    if checkpoint.version != expected_version:
        raise ValueError("checkpoint state version is stale")
    _require_complete_evidence(checkpoint.scope, checkpoint.source_owner_id, checkpoint.evidence)
    return checkpoint


def _validate_component_bindings(
    scope: CompositionScope,
    owner_id: str,
    owner_generation: str,
    version: CompositionVersion,
    evidence: CompositionEvidence,
) -> None:
    for receipt in evidence.receipts:
        if receipt.scope != scope:
            raise ValueError(f"{receipt.component.value} receipt scope binding mismatch")
        if receipt.owner_id != owner_id:
            raise ValueError(f"{receipt.component.value} receipt owner binding mismatch")
        if receipt.owner_generation != owner_generation:
            raise ValueError(f"{receipt.component.value} receipt generation binding mismatch")
        if receipt.version != version:
            raise ValueError(f"{receipt.component.value} receipt state version binding mismatch")


def _require_complete_evidence(
    scope: CompositionScope,
    owner_id: str,
    evidence: CompositionEvidence,
) -> None:
    if tuple(receipt.component for receipt in evidence.receipts) != REQUIRED_COMPONENTS:
        raise ValueError("complete state requires the full required component receipt set")
    states = tuple(getattr(evidence, component.value) for component in REQUIRED_COMPONENTS)
    if any(state is not EvidenceState.AVAILABLE for state in states):
        raise ValueError("whole-owner evidence is unavailable or unconfigured")
    _validate_component_bindings(
        scope,
        owner_id,
        evidence.receipts[0].owner_generation,
        evidence.receipts[0].version,
        evidence,
    )


def _checkpoint_manifest_digest(
    scope: CompositionScope,
    source_owner_id: str,
    source_owner_generation: str,
    version: CompositionVersion,
    evidence: CompositionEvidence,
) -> str:
    """Digest canonical component bindings for integrity, not trust/authentication."""

    payload = {
        "contract_version": CONTRACT_VERSION,
        "scope": {
            "composition_id": scope.composition_id,
            "membership_version": scope.membership_version,
            "members": scope.members,
        },
        "source_owner_id": source_owner_id,
        "source_owner_generation": source_owner_generation,
        "version": {
            "state_version": version.state_version,
            "snapshot_version": version.snapshot_version,
        },
        "components": [
            {
                "component": receipt.component.value,
                "composition_id": receipt.scope.composition_id,
                "membership_version": receipt.scope.membership_version,
                "members": receipt.scope.members,
                "owner_id": receipt.owner_id,
                "owner_generation": receipt.owner_generation,
                "state_version": receipt.version.state_version,
                "snapshot_version": receipt.version.snapshot_version,
                "covered_members": receipt.covered_members,
                "evidence_digest": receipt.evidence_digest,
            }
            for receipt in evidence.receipts
        ],
        "availability": {
            component.value: getattr(evidence, component.value).value
            for component in REQUIRED_COMPONENTS
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_members(value: tuple[str, ...], *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError("members must be a tuple")
    for member in value:
        _nonblank(member, "member")
    if value != tuple(sorted(set(value))):
        raise ValueError("members must be unique and sorted")
    if not allow_empty and not value:
        raise ValueError("members must not be empty")
    return value


def _nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _nonnegative_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _sha256_digest(value: str, name: str) -> None:
    _nonblank(value, name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _aware_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
